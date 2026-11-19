"""A single sleeping worker; closing the UI does not stop monitoring."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone, timedelta
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import __version__
from .network import Network
from .storage import Store, atomic_write
from .probes import ApplicationGuards
from .balance import BalanceClient, BalanceUnavailable
from .wlan import read_profile_passwords
from .updater import UpdateManager
from .proxy import ProxyProtection
from .boot import network_lease

TASK_NAME = "NjustWifiLocalMonitor"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_startup(enabled: bool) -> None:
    if os.name != "nt":
        raise ValueError("开机启动需要 Windows")
    script = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "scripts" / "startup.ps1"
    executable = Path(sys.executable)
    if not getattr(sys, "frozen", False):
        windowless = executable.with_name("pythonw.exe")
        if windowless.exists():
            executable = windowless
    command = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-Mode", "Install", "-Executable", str(executable),
               "-ProjectRoot", str(Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent)]
    if not enabled:
        command.append("-NoLogon")
    try:
        # The boot worker has a protected installed copy and requires one UAC
        # confirmation. The interactive task remains responsible after login.
        if getattr(sys, 'frozen', False):
            setup = Path(sys.executable).with_name('Boot-Network.ps1')
            if not setup.is_file():
                raise ValueError('开机联网组件缺失，请完整解压新版桌面包')
            exists = subprocess.run(['schtasks.exe', '/Query', '/TN', 'NjustWifiBootNetwork'],
                                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0
            if enabled or exists:
                result = subprocess.run(command[:5] + ['-File', str(setup), '-Mode', 'Install' if enabled else 'Remove',
                                        '-Executable', str(executable)], capture_output=True, timeout=150,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise ValueError('开机联网设置已取消或未完成，请确认管理员提示后重试')
        result = subprocess.run(command, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("开机启动设置失败，请重试") from None
    if result.returncode:
        raise ValueError("开机启动设置失败，请检查任务计划程序")


class Service:
    def __init__(self, store: Store, *, demo: bool = False, network_factory=Network, balance_factory=BalanceClient):
        self.store, self.demo = store, demo
        self.updater = UpdateManager(store.root, demo=demo)
        self.updater.local_proxy = lambda: self.proxy.local_proxy()
        self.network_factory = network_factory
        self.balance_factory = balance_factory
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.running = False
        self.busy = False
        self.once = False
        self.state = "idle"
        self.message = "已暂停自动连接"
        self.checked_at = self.next_check_at = None
        self.normal_count = self.failures = 0
        self.unchanged_since = None
        self.stable_signature = None
        self.cycle_recovered = False
        self.connection_path = 'normal'
        self.entries = deque(maxlen=100)
        self.health_spans = {}
        self.health_ticks = {}
        self.maintenance_states = {}
        self.balance_worker = None
        self.balance_generation = 0
        self.balance_attempt = 0.0
        self._balance = self._empty_balance()
        self.guards = ApplicationGuards(store.root)
        self.proxy = ProxyProtection(store.root, demo=demo)
        self.proxy_wake = threading.Event()
        self.proxy_check_requested = threading.Event()
        self.proxy_check = {'id': None, 'state': 'idle', 'client': self.proxy.client, 'result': None}
        if (store.root / "events.jsonl").exists():
            legacy_normal = None
            with (store.root / "events.jsonl").open("rb") as stream:
                stream.seek(max(0, (store.root / "events.jsonl").stat().st_size - 131072))
                for line in stream.read().splitlines()[-100:]:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and all(isinstance(entry.get(key), str) for key in ("time", "level", "message")):
                            if entry['message'] == '正在检测网络连接':
                                continue
                            clean = {key: entry[key] for key in ("time", "level", "message")}
                            if entry.get('private') is True:
                                clean['private'] = True
                            duration = entry.get('duration_seconds')
                            if isinstance(duration, (int, float)) and 0 <= duration < 10 ** 12:
                                # A restart cannot establish uninterrupted health during downtime.
                                clean.update(duration_seconds=int(duration), active=False)
                            elif entry['message'] in ('网络连接正常', '演示模式：网络连接正常'):
                                if legacy_normal is not None and self.entries and self.entries[-1] is legacy_normal:
                                    try:
                                        elapsed = datetime.fromisoformat(clean['time']) - datetime.fromisoformat(legacy_normal['time'])
                                        legacy_normal['duration_seconds'] = max(0, int(elapsed.total_seconds()))
                                    except (ValueError, TypeError):
                                        pass
                                    continue
                                clean.update(duration_seconds=0, active=False)
                                legacy_normal = clean
                            else:
                                legacy_normal = None
                            self.entries.append(clean)
                    except ValueError:
                        pass
        if not (store.root / 'events.jsonl').exists():
            self._save_logs()
        self.updater.event = self.guard_event
        self.worker = threading.Thread(target=self._work, name="wifi-monitor", daemon=True)
        self.worker.start()
        self.proxy_worker = threading.Thread(target=self._proxy_work, name="proxy-protection", daemon=True)
        self.proxy_worker.start()
        if store.credential_error:
            self.event("error", "已保存的凭据读取失败，请重新填写账号密码")

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "busy": self.busy, "state": self.state,
                    "message": self.message, "checked_at": self.checked_at,
                    "next_check_at": self.next_check_at, "normal_count": self.normal_count,
                    "version": __version__, "unchanged_since": self.unchanged_since,
                    "connection_path": self.connection_path}

    @staticmethod
    def _empty_balance():
        return {"balance": None, "currency": "CNY", "updated_at": None,
                "state": "unavailable", "message": "尚未查询"}

    def balance(self):
        with self.lock:
            return self._balance.copy()

    def _reset_balance(self):
        self.balance_generation += 1
        self.balance_attempt = 0.0
        self._balance = self._empty_balance()

    def refresh_balance(self, *, automatic=False):
        with self.lock:
            if self.closed.is_set() or (self.balance_worker and self.balance_worker.is_alive()):
                return
            if automatic and self.balance_attempt and time.monotonic() - self.balance_attempt < 300:
                return
            username = self.store.username
            if self.demo or not username:
                self._balance = self._empty_balance()
                self._balance["message"] = "演示模式" if self.demo else "请先设置账号"
                return
            generation = self.balance_generation
            self.balance_attempt = time.monotonic()
            self._balance.update(state="loading", message="正在刷新")
            self.balance_worker = threading.Thread(target=self._fetch_balance,
                args=(username, generation, not automatic), name="wifi-balance", daemon=True)
            self.balance_worker.start()

    def _fetch_balance(self, username, generation, requested=False):
        try:
            value = self.balance_factory().fetch(username)
            result = {"balance": value, "currency": "CNY", "updated_at": now(),
                      "state": "ready", "message": ""}
        except BalanceUnavailable as exc:
            result = {"state": "unavailable", "message": str(exc)}
        except Exception:
            result = {"state": "unavailable", "message": "余额暂不可用"}
        with self.lock:
            if generation == self.balance_generation and not self.closed.is_set():
                self._balance.update(result)
                message = '余额已刷新' if result['state'] == 'ready' else '余额查询未完成'
                if requested:
                    self._append_log('info' if result['state'] == 'ready' else 'error', message)
                else:
                    self.maintenance('balance', 'info' if result['state'] == 'ready' else 'error', message)

    def logs(self):
        with self.lock:
            current = time.monotonic()
            ticks = {id(entry): self.health_ticks[key] for key, entry in self.health_spans.items()}
            return [entry | ({'duration_seconds': max(0, int(current - ticks[id(entry)]))}
                             if id(entry) in ticks else {}) for entry in self.entries]

    def _save_logs(self):
        # Replace the bounded snapshot: healthy checks update a row, never append one.
        try:
            content = ''.join(json.dumps(entry, ensure_ascii=False) + '\n' for entry in self.logs())
            atomic_write(self.store.root / 'events.jsonl', content.encode('utf-8'))
        except OSError:
            pass

    def _append_log(self, state, message, *, private=False):
        self.entries.append({'time': now(), 'level': 'error' if state == 'error' else 'info', 'message': message,
                             **({'private': True} if private else {})})
        self._save_logs()

    def operation(self, path, raw):
        """Bounded labels only: payloads, credentials, groups and paths stay local."""
        labels = {'/api/config':'保存设置', '/api/start':'开启自动连接', '/api/check':'手动检测网络',
                  '/api/stop':'暂停自动连接', '/api/balance/refresh':'刷新余额',
                  '/api/credentials/forget':'清除凭据', '/api/probes/browse':'选择守护软件',
                  '/api/probes/add':'添加守护软件', '/api/probes/remove':'移除守护软件',
                  '/api/probes/clear':'清空守护软件', '/api/probes/elevate':'设置管理员守护',
                  '/api/proxy/client':'切换代理客户端', '/api/proxy/controller':'保存外部控制设置',
                  '/api/proxy/check':'手动检测代理出口', '/api/proxy/restore':'恢复代理和 TUN',
                  '/api/proxy/group':'设置分流代理组', '/api/proxy/policy':'设置软件网络方式',
                  '/api/proxy/groups':'连接代理客户端', '/api/proxy/options':'设置代理保护选项',
                  '/api/proxy/node':'切换代理节点',
                  '/api/proxy/switch':'切换代理开关', '/api/proxy/fallback':'设置故障处置顺序',
                  '/api/logs/clear':'清空日志',
                  '/api/logs/open':'打开日志目录', '/api/update/check':'检查更新',
                  '/api/update/download':'下载更新', '/api/update/install':'安装更新'}
        label = labels.get(path)
        if label is None:
            return None
        if path == '/api/proxy/node':
            label += ' · ' + ('FlClash' if self.proxy.client == 'flclash' else 'Clash Verge')
        if path == '/api/proxy/switch':
            label = {'proxy': '切换系统代理', 'tun': '切换 TUN'}.get(raw.get('field')) if isinstance(raw.get('field'), str) else None
            label = label or '切换代理开关'
            if type(raw.get('enabled')) is bool:
                label += ' · ' + ('开启' if raw['enabled'] else '关闭')
        private = False
        if path == '/api/config':
            fields = {'username':'认证账号', 'password':'认证密码', 'fallback_networks':'备用 WiFi',
                      'proxy_fail_open':'出口异常时自动处置', 'startup_enabled':'开机联网',
                      'auto_start_monitor':'自动值守', 'close_behavior':'关闭行为'}
            changed = [name for key,name in fields.items() if key in raw]
            if changed:
                label += '（' + '、'.join(changed) + '）'
        if path == '/api/proxy/options':
            names = {'auto_restore':'出口恢复后自动恢复', 'clear_stale':'清理残留系统代理'}
            chosen = [name + ('开启' if raw[key] is True else '关闭') for key, name in names.items() if key in raw]
            if chosen:
                label += '（' + '、'.join(chosen) + '）'
        with self.lock:
            paths = self.guards.paths
            if path == '/api/proxy/policy':
                paths = [p for p in paths if self.guards.identifier(p) == raw.get('id')]
                mode = {'direct':'直连', 'proxy':'代理组', 'follow':'跟随系统'}.get(raw.get('mode')) if isinstance(raw.get('mode'),str) else None
                if mode:
                    label += ' · ' + mode
            elif path in ('/api/probes/remove','/api/probes/elevate'):
                ids = raw.get('ids')
                paths = [p for p in paths if isinstance(ids,list) and self.guards.identifier(p) in ids]
            elif path != '/api/probes/clear':
                paths = []
            if paths:
                label += ' · ' + '、'.join(self.guards.name(p) for p in paths)
                private = True
        return label, private

    def audit(self, operation, success=True):
        if operation:
            label, private = operation
            with self.lock:
                queued = any(label.startswith(word) for word in ('手动检测', '刷新余额', '检查更新', '下载更新', '安装更新'))
                self._append_log('info' if success else 'error', label +
                                 ('：已提交' if success and queued else '：已完成' if success else '：未完成'), private=private)

    def maintenance(self, key, state, message):
        """Log transitions once, rather than repeating a failed timer tick."""
        with self.lock:
            signature = state, message
            if self.maintenance_states.get(key) != signature:
                self.maintenance_states[key] = signature
                self._append_log(state, message)

    def observe_proxy(self, *, requested=False):
        status = self.proxy.status()
        state, message = status['state'], status['message']
        exit_state = status.get('exit_state') or state
        self.health('proxy', '代理出口正常', exit_state == 'healthy')
        if exit_state != 'healthy' or state == 'error' or requested:
            if requested:
                self.guard_event('error' if state == 'error' or exit_state == 'failed' else 'info', '代理出口检测结果：' + message)
            else:
                self.maintenance('proxy', 'error' if state in ('error','failed') else state, message)
        else:
            with self.lock:
                self.maintenance_states['proxy'] = state, message

    def health(self, key: str, message: str, healthy: bool = True):
        with self.lock:
            entry = self.health_spans.get(key)
            if entry is not None and not any(item is entry for item in self.entries):
                self.health_spans.pop(key, None)
                self.health_ticks.pop(key, None)
                entry = None
            if not healthy:
                if entry is not None:
                    entry.update(duration_seconds=max(0, int(time.monotonic() - self.health_ticks.pop(key))), active=False)
                    self.health_spans.pop(key)
                    self._save_logs()
                return
            if entry is None:
                entry = {'time': now(), 'level': 'info', 'message': message, 'duration_seconds': 0, 'active': True}
                if key.startswith('guard:'):
                    entry['private'] = True
                self.entries.append(entry)
                self.health_spans[key] = entry
                self.health_ticks[key] = time.monotonic()
                self._save_logs()
            else:
                entry['message'] = message

    def guard_event(self, state, message, *, private=False):
        with self.lock:
            self._append_log(state, message, private=private)

    def event(self, state: str, message: str) -> None:
        with self.lock:
            if state == "recovering":
                self.cycle_recovered = True
            if state in ('recovering', 'error'):
                self.health('network', '', False)
            changed = message != self.message
            self.state, self.message = state, message
            if state == 'online':
                self.health('network', message)
            elif changed and state != 'checking':
                self._append_log(state, message)

    def start(self, *, once: bool = False) -> None:
        with self.lock:
            if self.busy:
                raise ValueError("正在检测，请等待当前操作完成")
            if not self.demo:
                campus = self.store.settings["ssid"] and self.store.username and self.store.password
                if not campus and not self.store.settings["fallback_networks"]:
                    raise ValueError("请设置 NJUST WIFI 账号或备用 WiFi")
            if not once and self.running:
                return
            self.cancel.clear()
            if once:
                self.once = True
            else:
                self.running = True
            if not once:
                self.maintenance('monitor', 'info', '自动网络值守已启动')
            self.wake.set()

    def stop(self) -> None:
        with self.lock:
            self.running = self.once = False
            self.cancel.set()
            self.next_check_at = None
            for key in list(self.health_spans):
                self.health(key, '', False)
            self.event("idle", "已暂停自动连接")
            self.maintenance_states.pop('monitor', None)
            self.wake.set()

    def config_view(self) -> dict:
        with self.lock:
            data = self.store.public()
            networks = [item.copy() for item in data['fallback_networks']]
        names = [item['ssid'] for item in networks if not item['password']]
        saved = read_profile_passwords(names) if names and not self.demo else {}
        data['fallback_networks'] = [item | {'profile_password': saved.get(item['ssid'], '') if not item['password'] else ''}
                                     for item in networks]
        return data

    def configure(self, raw: dict) -> None:
        with self.lock:
            previous = self.store.settings.copy()
            previous_username = self.store.username
            # Validate and save before touching Task Scheduler.
            self.store.update(raw)
            if previous_username != self.store.username:
                self._reset_balance()
            if previous["startup_enabled"] != self.store.settings["startup_enabled"]:
                try:
                    if self.demo:
                        raise ValueError("演示模式不修改开机启动")
                    set_startup(self.store.settings["startup_enabled"])
                except ValueError:
                    self.store.update({"startup_enabled": previous["startup_enabled"]})
                    raise
            if previous["proxy_fail_open"] != self.store.settings["proxy_fail_open"]:
                self.proxy.failures = 0
                self.proxy_wake.set()
            # New values apply to the next cycle; appearance changes never trigger reconnection.
            network_keys = {"ssid", "fallback_networks", "domain", "interval_minutes", "connectivity_timeout_ms", "connectivity_checks", "show_portal"}
            if self.running and (any(previous[k] != self.store.settings[k] for k in network_keys) or previous_username != self.store.username):
                self.wake.set()

    def forget(self) -> None:
        with self.lock:
            if self.busy:
                raise ValueError("请先暂停并等待当前检测完成，再清除凭据")
            self.stop()
            self.store.forget()
            self._reset_balance()
            self.event("idle", "已清除保存的账号和密码")

    def diagnostics(self) -> dict:
        with self.lock:
            # Allowlist only: no settings, SSIDs, account masks, paths or raw HTTP.
            events = [{key:value for key,value in entry.items() if key != 'private'} |
                      ({'message':'程序守护记录（软件名称已隐藏）'} if entry.get('private') else {})
                      for entry in self.logs()]
            return {"app": "NJUSTWiFi", "version": __version__, "exported_at": now(),
                    "status": self.status(), "events": events,
                    "privacy": "不包含账号、密码、网络名称、设备地址或个人路径"}

    def probe_items(self):
        with self.lock:
            return [item | {'routing':self.proxy.policies.get(item['id'], 'follow')} for item in self.guards.items()]

    def probe_action(self, action: str, raw: dict):
        with self.lock:
            if self.busy:
                raise ValueError("请等待当前检测完成")
            previous = {self.guards.identifier(path) for path in self.guards.paths}
            if action == "add":
                self.guards.add(raw.get("paths"))
                for path in self.guards.paths:
                    if self.guards.identifier(path) not in previous:
                        self._append_log('info', '已添加守护软件 · ' + self.guards.name(path), private=True)
            elif action == "remove":
                selected = self.guards.selected(raw.get("ids"))
                self.proxy.remove(self.guards.paths, {self.guards.identifier(path) for path in selected})
                self.guards.remove(raw.get("ids"))
            elif action == "clear":
                if self.guards.paths:
                    self.proxy.remove(self.guards.paths, {self.guards.identifier(path) for path in self.guards.paths})
                    self.guards.remove([self.guards.identifier(path) for path in self.guards.paths])
            elif action == "elevate":
                if self.demo:
                    raise ValueError("演示模式不修改管理员任务")
                for path in self.guards.selected(raw.get("ids")):
                    self.guards.configure_task(path)
            else:
                raise ValueError("程序操作不存在")
            for removed in previous - {self.guards.identifier(path) for path in self.guards.paths}:
                self.health('guard:' + removed, '', False)

    def proxy_action(self, action, raw):
        if action == 'client':
            self.proxy.set_client(raw.get('client'))
            with self.lock:
                if self.proxy_check['client'] != self.proxy.client and self.proxy_check['state'] in ('queued', 'running'):
                    self.proxy_check.update(state='cancelled', result=None)
        elif action == 'controller':
            self.proxy.configure_controller(raw)
        elif action == 'check':
            with self.lock:
                if self.proxy_check['state'] not in ('queued', 'running'):
                    self.proxy_check = {'id': secrets.token_hex(12), 'state': 'queued',
                                        'client': self.proxy.client, 'result': None}
                    self.proxy_check_requested.set()
                    self.proxy_wake.set()
                return {'check_id': self.proxy_check['id']}
        elif action == 'node':
            try:
                self.proxy.select_node(raw)
            finally:
                if self.proxy.exit_state == 'uncertain':
                    self.health('proxy', '', False)
        elif action == 'restore':
            self.proxy.restore()
        elif action == 'options':
            self.proxy.set_options(raw)
        elif action == 'fallback':
            result = self.proxy.set_fallback(raw)
            self.proxy_wake.set()
            return result
        elif action == 'switch':
            if (not isinstance(raw, dict) or set(raw) != {'client', 'field', 'enabled'}
                    or not isinstance(raw['client'], str)):
                raise ValueError('代理开关设置格式不正确')
            # Keep identity validation, the write and the warning in one routing
            # transaction. Never hold the service lock across a bridge call.
            with self.proxy.lock:
                result = self.proxy.set_switch(raw['field'], raw['enabled'], client=raw['client'])
                if raw['enabled'] and self.proxy.exit_state == 'failed' and self.store.settings['proxy_fail_open']:
                    result['warning'] = '出口仍异常，故障保护复核后会再次关闭'
            self.proxy_wake.set()
            return result
        elif action in ('policy', 'group'):
            with self.lock:
                paths = list(self.guards.paths)
            if action == 'policy':
                self.proxy.configure(paths, identifier=raw.get('id', ''), mode=raw.get('mode'))
            else:
                self.proxy.configure(paths, group=raw.get('group', ''))
        else:
            raise ValueError("代理操作不存在")
        self.proxy_wake.set()

    def proxy_check_status(self):
        with self.lock:
            return self.proxy_check.copy()

    def _proxy_work(self):
        while not self.closed.is_set():
            protect = self.store.settings["proxy_fail_open"] and not self.demo
            # Keep observing a closed proxy even when automatic restoration is off.
            pending = self.proxy.recovery_path.exists() and not self.demo
            stale = (self.proxy.clear_stale or self.proxy.stale_path.exists()) and not self.demo
            enabled = protect or pending
            with self.lock:
                requested = self.proxy_check_requested.is_set()
                self.proxy_check_requested.clear()
                check_id = self.proxy_check['id'] if requested else None
                check_client = self.proxy_check['client']
                if check_id:
                    self.proxy_check['state'] = 'running'
            # A queued manual check belongs to the client selected at submission.
            requested = requested and check_client == self.proxy.client
            synchronize = bool(self.proxy.policies or self.proxy.managed_rules) and not self.demo
            if synchronize:
                try:
                    with self.lock:
                        paths = list(self.guards.paths)
                    if self.proxy.ensure_rules(paths):
                        self.maintenance('rules', 'info', '客户端配置已变化，已自动恢复软件分流规则')
                    else:
                        self.maintenance_states.pop('rules', None)
                    self.maintenance_states.pop('rules-error', None)
                except Exception:
                    with self.proxy.lock:
                        self.proxy.state='error'; self.proxy.message='客户端分流待同步，请连接本地接口'
                    self.maintenance('rules-error', 'error', '软件分流规则同步未完成，请连接客户端外部控制接口')
            try:
                handled = False
                if stale:
                    # Needs no running client: only the registry and one loopback connect.
                    handled = self.proxy.stale_tick(self.closed, self.guard_event, lambda: self.proxy.clear_stale)
                    if handled:
                        self.observe_proxy(requested=requested)
                if (enabled or requested) and not handled:
                    self.proxy.tick(self.closed, self.guard_event,
                        lambda: requested or self.store.settings["proxy_fail_open"]
                                or self.proxy.recovery_path.exists(),
                        required_failures=1 if requested else 3,
                        switch_allowed=lambda: self.store.settings["proxy_fail_open"],
                        auto_restore=lambda: self.proxy.auto_restore, manual=requested,
                        **({'expected_client': check_client} if check_id else {}))
                    self.observe_proxy(requested=requested)
            except Exception:
                self.guard_event('error', '代理出口检测未完成，请重试')
                if check_id:
                    with self.proxy.lock:
                        self.proxy.exit_state = 'uncertain'; self.proxy.message = '代理出口检测未完成，请重试'
            finally:
                if check_id:
                    result = self.proxy.status()
                    with self.lock:
                        if self.proxy_check['id'] == check_id and self.proxy_check['state'] == 'running':
                            cancelled = result['client'] != check_client
                            self.proxy_check.update(state='cancelled' if cancelled else 'done', result=None if cancelled else result)
            self.proxy_wake.wait(15 if enabled or synchronize or stale else None)
            self.proxy_wake.clear()

    def candidates(self):
        if self.demo:
            return [{"name": path.stem, "path": str(path)} for path in (self.store.root / "fixtures").glob("*.exe")]
        return self.guards.candidates()

    def browse(self):
        return [] if self.demo else self.guards.browse()

    def clear_logs(self):
        with self.lock:
            self.entries.clear()
            self.health_spans.clear()
            self.health_ticks.clear()
            self.maintenance_states.clear()
            for suffix in ('.1', '.2'):
                (self.store.root / ('events.jsonl' + suffix)).unlink(missing_ok=True)
            self._save_logs()

    def open_logs(self):
        if not self.demo:
            if os.name != "nt":
                raise ValueError("打开日志目录需要 Windows")
            os.startfile(str(self.store.root))

    def _work(self):
        timeout = None
        while not self.closed.is_set():
            self.wake.wait(timeout)
            self.wake.clear()
            with self.lock:
                if self.closed.is_set():
                    break
                if not self.running and not self.once:
                    timeout = None
                    continue
                self.once = False
                self.busy = True
                self.next_check_at = None
                settings = self.store.settings.copy()
                username, password = self.store.username, self.store.password
                self.cycle_recovered = False
                self.event("checking", "正在检测网络连接")
            try:
                network = None
                if self.demo:
                    if self.cancel.wait(0.1):
                        raise InterruptedError()
                else:
                    self.guards.check(self.cancel, lambda state,message: self.guard_event(state,message,private=True), self.health)
                    network = self.network_factory(self.cancel)
                    with network_lease(self.cancel):
                        network.recover(settings, username, password, self.event)
                    if settings['proxy_fail_open'] and not self.cancel.is_set():
                        self.event('checking', '正在检测当前代理出口')
                        # This network check already established WiFi connectivity.
                        # Both HTTPS exit probes still have to fail, and tick
                        # reconfirms the physical direct path before switching.
                        self.proxy.tick(self.cancel, self.guard_event,
                            lambda: self.store.settings['proxy_fail_open'], required_failures=1,
                            auto_restore=lambda: self.proxy.auto_restore)
                        self.observe_proxy()
                with self.lock:
                    if self.cancel.is_set():
                        raise InterruptedError()
                    signature = ("online", tuple(sorted(self.guards.states.items())))
                    if signature != self.stable_signature or self.cycle_recovered:
                        self.unchanged_since = now()
                        self.normal_count = 0
                    else:
                        self.normal_count += 1
                    self.stable_signature = signature
                    self.failures = 0
                    self.event("online", "网络连接正常" if not self.demo else "演示模式：网络连接正常")
                self.refresh_balance(automatic=True)
            except InterruptedError:
                self.event("idle", "已暂停自动连接")
            except RuntimeError as exc:
                # Network raises only bounded, fixed product messages.
                with self.lock:
                    self.normal_count = 0
                    self.unchanged_since = now()
                    self.stable_signature = None
                    self.failures += 1
                    self.event("error", str(exc))
            except Exception:
                with self.lock:
                    self.failures += 1
                    self.event("error", "本次检测未完成，请稍后重试")
            finally:
                username = password = ""
                with self.lock:
                    path = getattr(getattr(network, 'opener', None), 'mode', 'normal')
                    self.connection_path = path if path in ('normal', 'wifi-direct', 'unavailable') else 'normal'
                    self.busy = False
                    self.checked_at = now()
                    self._save_logs()
                    timeout = (min(30 * 2 ** min(self.failures - 1, 4), 300) if self.failures else
                               self.store.settings["interval_minutes"] * 60) if self.running else None
                    self.next_check_at = ((datetime.now(timezone.utc) + timedelta(seconds=timeout)).isoformat(timespec="seconds")
                                          if timeout is not None else None)

    def close(self):
        self.stop()
        self.closed.set()
        self.proxy_wake.set()
        self.wake.set()
        self.worker.join(timeout=20)
        self.proxy_worker.join(timeout=15)
        if self.balance_worker:
            self.balance_worker.join(timeout=15)


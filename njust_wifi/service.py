"""A single sleeping worker; closing the UI does not stop monitoring."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone, timedelta
import json
import os
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
        result = subprocess.run(command, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("开机启动设置失败，请重试") from None
    if result.returncode:
        raise ValueError("开机启动设置失败，请检查任务计划程序")


class Service:
    def __init__(self, store: Store, *, demo: bool = False, network_factory=Network, balance_factory=BalanceClient):
        self.store, self.demo = store, demo
        self.updater = UpdateManager(store.root, demo=demo)
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
        self.balance_worker = None
        self.balance_generation = 0
        self.balance_attempt = 0.0
        self._balance = self._empty_balance()
        self.guards = ApplicationGuards(store.root)
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
        self.worker = threading.Thread(target=self._work, name="wifi-monitor", daemon=True)
        self.worker.start()
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
                args=(username, generation), name="wifi-balance", daemon=True)
            self.balance_worker.start()

    def _fetch_balance(self, username, generation):
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

    def _append_log(self, state, message):
        self.entries.append({'time': now(), 'level': 'error' if state == 'error' else 'info', 'message': message})
        self._save_logs()

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
                self.entries.append(entry)
                self.health_spans[key] = entry
                self.health_ticks[key] = time.monotonic()
                self._save_logs()
            else:
                entry['message'] = message

    def guard_event(self, state, message):
        with self.lock:
            self._append_log(state, message)

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
            self.wake.set()

    def stop(self) -> None:
        with self.lock:
            self.running = self.once = False
            self.cancel.set()
            self.next_check_at = None
            for key in list(self.health_spans):
                self.health(key, '', False)
            self.event("idle", "已暂停自动连接")
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
            if self.busy:
                raise ValueError("正在检测，请稍后保存设置")
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
            if self.running:
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
            return {"app": "NJUSTWiFi", "version": __version__, "exported_at": now(),
                    "status": self.status(), "events": self.logs(),
                    "privacy": "不包含账号、密码、网络名称、设备地址或个人路径"}

    def probe_items(self):
        with self.lock:
            return self.guards.items()

    def probe_action(self, action: str, raw: dict):
        with self.lock:
            if self.busy:
                raise ValueError("请等待当前检测完成")
            previous = {self.guards.identifier(path) for path in self.guards.paths}
            if action == "add":
                self.guards.add(raw.get("paths"))
            elif action == "remove":
                self.guards.remove(raw.get("ids"))
            elif action == "clear":
                if self.guards.paths:
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
                    self.guards.check(self.cancel, self.guard_event, self.health)
                    network = self.network_factory(self.cancel)
                    network.recover(settings, username, password, self.event)
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
        self.wake.set()
        self.worker.join(timeout=20)
        if self.balance_worker:
            self.balance_worker.join(timeout=15)


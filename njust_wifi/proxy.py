"""Optional Clash Verge/Mihomo integration. No resident helper or bundled core."""
from __future__ import annotations

import ctypes
from copy import deepcopy
from ctypes import wintypes
import http.client
import io
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import re
import socket
import threading
import time
import urllib.parse

from .storage import atomic_write
from .client_control import ClientSwitches, ClientControlError
from .subscription import SubscriptionReader

BEGIN = '// BEGIN SCHOOL-WIFI-CONNECT'
END = '// END SCHOOL-WIFI-CONNECT'
MAX_REPLY = 4 * 1024 * 1024
CLIENTS = {'clash-verge':'Clash Verge', 'flclash':'FlClash'}
RECOVERY_ROUNDS = 4       # healthy rounds before an automatic restore
MAX_FLAPS = 3             # failures soon after a restore before automation stops
FLAP_WINDOW = 1800        # seconds a restored exit has to stay healthy
MAX_AUTO_ATTEMPTS = 3     # unfinished automatic restores before waiting for the user
STALE_ROUNDS = 3          # refused rounds before a leftover system proxy is closed
STALE_RECOVERY_ROUNDS = 2
FALLBACK_STEPS = ('switch_node', 'close_tun', 'close_proxy')
NODE_SWITCH_WINDOW = 1800
NODE_SWITCH_LIMIT = 3
NODE_RETRY_INTERVAL = 300


def fallback_settings(raw=None):
    if (not isinstance(raw, dict) or set(raw) != {'order', 'enabled'}
            or not isinstance(raw['order'], list) or len(raw['order']) != 3
            or any(not isinstance(step, str) for step in raw['order'])
            or set(raw['order']) != set(FALLBACK_STEPS)
            or not isinstance(raw['enabled'], dict) or set(raw['enabled']) != set(FALLBACK_STEPS)
            or any(type(value) is not bool for value in raw['enabled'].values())):
        raise ValueError('故障处置设置格式不正确')
    return deepcopy(raw)


def default_fallback():
    return {'order': list(FALLBACK_STEPS),
            'enabled': {'switch_node': False, 'close_tun': True, 'close_proxy': True}}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class ControllerError(OSError):
    """Keep HTTP status without forwarding a config body containing private data."""
    def __init__(self, status):
        self.status = status
        super().__init__(f'controller HTTP {status}')


class SwitchStateError(OSError):
    """A verified state disagreement, distinct from an unavailable controller."""


def scalar(text, key, default=''):
    match = re.search(r'^' + re.escape(key) + r':\s*([^\r\n]*)', text, re.M)
    if not match:
        return default
    value = match[1].strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value.strip("'")


def root_value(text, key, value):
    """Only replace a scalar root key; retain unrelated YAML verbatim."""
    line = f'{key}: {value}'
    pattern = r'^' + re.escape(key) + r':[^\r\n]*'
    return re.sub(pattern, lambda _: line, text, count=1, flags=re.M) if re.search(pattern, text, re.M) else line + '\n' + text


def root_section(text, key, value):
    lines = text.splitlines(keepends=True)
    begin = next((i for i,line in enumerate(lines) if re.match(r'^' + re.escape(key) + r':',line)), len(lines))
    end = begin + 1
    while end < len(lines) and not re.match(r'^[A-Za-z_][\w-]*:',lines[end]):
        end += 1
    return ''.join(lines[:begin] + [key + ': ' + json.dumps(value,ensure_ascii=False) + '\n'] + lines[end:])


def strip_script(script):
    if BEGIN not in script:
        return script
    a = script.index(BEGIN)
    b = script.find(END, a)
    if b < 0 or BEGIN in script[a + len(BEGIN):]:
        raise ValueError('代理脚本的管理区块格式不正确')
    return script[:a] + script[b + len(END):].removeprefix('\n')


def make_script(original, rules):
    original = strip_script(original)
    if not re.search(r'\bfunction\s+main\s*\(', original):
        raise ValueError('当前全局脚本格式暂不支持，请保留 function main 入口')
    if not rules:
        return original
    # Append after the existing function: its custom transformations run first.
    return original + ('' if original.endswith('\n') else '\n') + BEGIN + '\nvar schoolWifiOriginalMain = main;\nmain = function(config) {\n' + \
        '  config = schoolWifiOriginalMain.apply(this, arguments) || config;\n' + \
        '  config.rules = ' + json.dumps(rules, ensure_ascii=False) + '.concat(config.rules || []);\n' + \
        '  config["find-process-mode"] = "always";\n  return config;\n};\n' + END + '\n'


def apply_rules(text, new_rules, old_rules=()):
    """Verge's generated block list; never round-trip subscription/proxy secrets."""
    lines = text.splitlines(keepends=True)
    found = None
    for index, line in enumerate(lines):
        if re.match(r'^rules\s*:', line):
            found = index
            if line.strip() not in ('rules:', 'rules: []'):
                raise ValueError('当前代理规则格式暂不支持，请使用标准规则列表')
            break
    remove = set(old_rules) | set(new_rules)
    if found is None:
        lines += ['\nrules:\n']
        found = len(lines) - 1
    end = found + 1
    while end < len(lines) and not re.match(r'^[A-Za-z_][\w-]*\s*:', lines[end]):
        end += 1
    retained = []
    for line in lines[found + 1:end]:
        match = re.match(r'^\s*-\s*(.*?)\s*$', line)
        value = match[1] if match else ''
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        if value not in remove:
            retained.append(line)
    # Verge emits indentationless sequences; mixing a new indented item with
    # existing unindented items makes the entire config invalid YAML.
    first = next((re.match(r'^(\s*)-\s', line) for line in retained if re.match(r'^\s*-\s', line)), None)
    indent = first[1] if first else '  '
    block = ['rules:\n'] + [indent + '- ' + json.dumps(rule, ensure_ascii=False) + '\n' for rule in new_rules] + retained
    return root_value(''.join(lines[:found] + block + lines[end:]), 'find-process-mode', 'always')


def pipe_request(name, request, timeout=7):
    """Overlapped Windows pipe IO with a total deadline and bounded response."""
    if os.name != 'nt':
        raise OSError('Windows named pipes required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    for fn in ('ReadFile', 'WriteFile'):
        getattr(kernel, fn).argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)]
    kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError('controller unavailable')
    event = kernel.CreateEventW(None, True, False, None)
    deadline = time.monotonic() + timeout
    def transfer(function, buffer, size):
        kernel.ResetEvent(event)
        operation = Overlapped(hEvent=event)
        count = wintypes.DWORD()
        if not function(handle, buffer, size, ctypes.byref(count), ctypes.byref(operation)):
            error = ctypes.get_last_error()
            if error in (109, 232):
                return 0
            if error != 997:
                raise OSError('controller IO failed')
            wait = max(1, int((deadline - time.monotonic()) * 1000))
            if kernel.WaitForSingleObject(event, wait) != 0:
                kernel.CancelIoEx(handle, ctypes.byref(operation))
                kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(count), True)
                raise TimeoutError('controller timeout')
            if not kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(count), False):
                if ctypes.get_last_error() in (109, 232):
                    return 0
                raise OSError('controller IO failed')
        return count.value
    try:
        written = 0
        while written < len(request):
            buf = ctypes.create_string_buffer(request[written:])
            count = transfer(kernel.WriteFile, buf, len(request) - written)
            if not count:
                raise OSError('controller write failed')
            written += count
        chunks, length = [], 0
        while length <= MAX_REPLY:
            buf = ctypes.create_string_buffer(65536)
            count = transfer(kernel.ReadFile, buf, len(buf))
            if not count:
                return b''.join(chunks)
            chunks.append(buf.raw[:count]); length += count
        raise OSError('controller response too large')
    finally:
        kernel.CloseHandle(handle)
        if event:
            kernel.CloseHandle(event)


class Controller:
    def __init__(self, root=None, *, kind='clash-verge', options=None):
        if kind not in CLIENTS:
            raise ValueError('请选择支持的代理客户端')
        self.kind = kind
        self.options = options or {}
        relative = 'com.follow/clash' if kind == 'flclash' else 'io.github.clash-verge-rev.clash-verge-rev'
        self.root = root or Path(os.environ.get('APPDATA', '')) / relative

    @property
    def runtime_path(self):
        return self.root / ('config.yaml' if self.kind == 'flclash' else 'clash-verge.yaml')

    def runtime(self):
        text = self.runtime_path.read_text('utf-8-sig')
        if self.kind=='flclash':
            # FlClash can patch its controller live before rebuilding config.yaml.
            # The persisted UI values carry the current port and rotated secret.
            try:
                prefs=json.loads((self.root/'shared_preferences.json').read_text('utf-8-sig'))
                value=prefs.get('flutter.config',prefs.get('config',{}))
                config=json.loads(value) if isinstance(value,str) else value
                overrides=config.get('patchClashConfig',{})
                for key in ('external-controller','secret'):
                    if isinstance(overrides.get(key),str):
                        text=root_value(text,key,json.dumps(overrides[key],ensure_ascii=False))
            except (OSError,ValueError,TypeError,AttributeError):
                pass
        return text

    def request(self, method, path, payload=None):
        try:
            text = self.runtime()
        except FileNotFoundError:
            if not self.options.get('url'):
                raise
            text = ''
        pipe = '' if self.options.get('url') else scalar(text, 'external-controller-pipe')
        secret = self.options.get('secret', scalar(text, 'secret'))
        body = b'' if payload is None else json.dumps(payload).encode()
        if any(c in secret for c in '\r\n'):
            raise OSError('invalid controller config')
        request = (f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {secret}\r\n'
                   f'Connection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        if pipe.startswith('\\\\.\\pipe\\') and len(pipe) <= 200:
            raw = pipe_request(pipe, request)
        else:
            address = urllib.parse.urlsplit(self.options['url'] if self.options.get('url') else '//' + scalar(text,'external-controller'))
            host = address.hostname
            if host not in ('localhost','127.0.0.1','::1','0.0.0.0','::') or not address.port or address.username or address.password or address.path:
                raise OSError('local controller not enabled')
            host = '127.0.0.1' if host in ('localhost','0.0.0.0') else '::1' if host == '::' else host
            connection = http.client.HTTPConnection(host,address.port,timeout=7)
            try:
                connection.request(method,path,body=body,headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json','Connection':'close'})
                response = connection.getresponse()
                data = response.read(MAX_REPLY+1)
                if response.status not in (200,204):
                    raise ControllerError(response.status)
                if len(data)>MAX_REPLY:
                    raise OSError('controller response too large')
                return json.loads(data) if data else {}
            finally:
                connection.close()
        class MemorySocket:
            def makefile(self, *args):
                return io.BytesIO(raw)
        response = http.client.HTTPResponse(MemorySocket())
        response.begin()
        data = response.read(MAX_REPLY + 1)
        if response.status not in (200, 204):
            raise ControllerError(response.status)
        if len(data) > MAX_REPLY:
            raise OSError('controller response too large')
        return json.loads(data) if data else {}


class ProxyProtection:
    def __init__(self, root, *, demo=False, controller=None, switches=None):
        self.root, self.demo = root, demo
        self.controller = controller
        supplied_kind = getattr(controller,'kind',None)
        self.client = supplied_kind if supplied_kind in CLIENTS else 'clash-verge'
        self.profiles = {}
        self.managed_rules = []
        self.controllers_path = root / 'proxy-controllers.json'
        self.controllers = {}
        try:
            self.controllers = json.loads(self.controllers_path.read_text('utf-8'))
        except (OSError, ValueError):
            pass
        if not isinstance(self.controllers, dict):
            self.controllers = {}
        self.lock = threading.RLock()
        self.policies = {}
        self.group = ''
        self.state = 'idle'
        self.message = '故障保护未开启，可手动检测出口'
        self.failures = 0
        self.egress = ''
        self.exit_chain = []
        self.exit_delay = 48 if demo else None
        self.fallback = default_fallback()
        self.last_fallback = None
        self.node_switches = []
        self.node_retry_at = None
        self._recovery_switched = False
        # A chain run that changed nothing is not repeated on every tick: hold it
        # for NODE_RETRY_INTERVAL and keep showing its result instead.
        self.fallback_hold_until = None
        self._fallback_result = None
        self._fallback_notice = None
        self._subscriptions = SubscriptionReader()
        self._demo_switches = {'proxy': True, 'tun': False}
        self._demo_at = datetime.now(timezone.utc)
        self.probe_reason = ''
        self.exit_state = ''
        self.checked_at = ''
        self.connected = None
        # Both are opt-in. They live here rather than in settings.json: that file
        # rejects unknown keys and is also read by an older, separately installed boot copy.
        self.auto_restore = False
        self.clear_stale = False
        self.recoveries = 0
        self.flaps = 0
        self.restored_at = None
        self.auto_attempts = 0
        self.recovery_exit = ''
        self.stale_failures = 0
        self.stale_clears = []
        self.path = root / 'routing-settings.json'
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text('utf-8'))
                options = raw.get('options') if isinstance(raw.get('options'), dict) else {}
                self.auto_restore = options.get('auto_restore') is True
                self.clear_stale = options.get('clear_stale') is True
                try:
                    self.fallback = fallback_settings(options.get('fallback'))
                except ValueError:
                    pass
                self.client = raw.get('client') if raw.get('client') in CLIENTS else self.client
                self.profiles = {key:value for key,value in raw.get('profiles',{}).items() if key in CLIENTS and isinstance(value,dict)}
                raw = self.profiles.get(self.client, raw)
                self.managed_rules = [v for v in raw.get('managed_rules',[]) if isinstance(v,str) and v.startswith('PROCESS-PATH,')]
                self.group = raw.get('group', '') if isinstance(raw.get('group'), str) else ''
                self.policies = {key: value for key, value in raw.get('policies', {}).items() if re.fullmatch(r'[0-9a-f]{12}', key) and value in ('direct', 'proxy')}
            except (ValueError, TypeError, AttributeError):
                pass
        if self.controller is None:
            self.controller = Controller(kind=self.client, options=self.controllers.get(self.client))

        # An injected controller must never accidentally operate a real GUI in a test.
        self.switches = switches if switches is not None else None if controller is not None else ClientSwitches(root, self.client)

    @property
    def recovery_path(self):
        return self.root / ('proxy-recovery-flclash.json' if self.client=='flclash' else 'proxy-recovery.json')

    @property
    def stale_path(self):
        return self.root / ('proxy-stale-flclash.json' if self.client=='flclash' else 'proxy-stale.json')

    def save(self):
        self.profiles[self.client] = {'group':self.group, 'policies':self.policies.copy(), 'managed_rules':self.managed_rules}
        atomic_write(self.path, json.dumps({'client':self.client, 'profiles':self.profiles, 'group':self.group, 'policies':self.policies, 'managed_rules':self.managed_rules,
                                            'options':{'auto_restore':self.auto_restore, 'clear_stale':self.clear_stale,
                                                       'fallback':self.fallback}}, ensure_ascii=False).encode())

    def set_fallback(self, raw):
        value = fallback_settings(raw)
        with self.lock:
            previous = self.fallback
            self.fallback = value
            try:
                self.save()
            except Exception:
                self.fallback = previous
                raise ValueError('故障处置设置保存未完成，请重试') from None
            self.node_switches = []
            self.node_retry_at = None
            self._release_fallback_hold()
            return {'fallback': deepcopy(self.fallback)}

    @property
    def node_switch_paused(self):
        now = time.monotonic()
        self.node_switches = [moment for moment in self.node_switches if now - moment < NODE_SWITCH_WINDOW]
        return len(self.node_switches) >= NODE_SWITCH_LIMIT

    def set_options(self, raw):
        if (not isinstance(raw, dict) or not raw or set(raw) - {'auto_restore', 'clear_stale'}
                or any(type(value) is not bool for value in raw.values())):
            raise ValueError('代理保护选项格式不正确')
        with self.lock:
            if 'auto_restore' in raw:
                self.auto_restore = raw['auto_restore']
                self._reset_automation()
            if 'clear_stale' in raw:
                self.clear_stale = raw['clear_stale']
                self.stale_failures = 0; self.stale_clears = []
            self.save()

    def _release_fallback_hold(self):
        self.fallback_hold_until = None; self._fallback_result = None; self._fallback_notice = None

    def _reset_automation(self):
        self.recoveries = 0; self.flaps = 0; self.auto_attempts = 0; self.restored_at = None
        self.recovery_exit = ''
        self._recovery_switched = False

    def set_client(self, client):
        if not isinstance(client,str) or client not in CLIENTS:
            raise ValueError('请选择支持的代理客户端')
        with self.lock:
            self.save()
            self.client = client
            profile = self.profiles.get(client,{})
            self.group = profile.get('group','')
            self.policies = profile.get('policies',{}).copy()
            self.managed_rules = profile.get('managed_rules',[]).copy()
            self.controller = Controller(kind=client, options=self.controllers.get(client))
            self.switches = ClientSwitches(self.root, client)
            self.failures = 0; self.egress = ''; self.state = 'idle'; self.message = '请选择连接客户端'
            self.exit_state = ''
            self.exit_chain = []; self.exit_delay = 48 if self.demo else None
            self.node_switches = []; self.last_fallback = None
            self.node_retry_at = None; self._release_fallback_hold()
            self.connected = None; self.stale_failures = 0; self._reset_automation()
            self.save()

    def status(self):
        with self.lock:
            closed, closed_at, cause = [], '', ''
            for path, kind in ((self.recovery_path, 'exit'), (self.stale_path, 'stale')):
                try:
                    saved = json.loads(path.read_text('utf-8'))
                except (OSError, ValueError):
                    continue
                if not isinstance(saved, dict):
                    continue
                fields = saved.get('completed') if kind == 'exit' else ['proxy']
                closed = [name for name in ('tun', 'proxy') if isinstance(fields, list) and name in fields]
                closed_at = saved['at'] if isinstance(saved.get('at'), str) else ''
                cause = kind
                break
            paused = self.flaps >= MAX_FLAPS or self.auto_attempts >= MAX_AUTO_ATTEMPTS
            return {'state':self.state, 'message':self.message, 'group':self.group, 'client':self.client,
                    'exit_state':self.exit_state,
                    'exit_delay':self.exit_delay, 'fallback':deepcopy(self.fallback),
                    'last_fallback':deepcopy(self.last_fallback), 'node_switch_paused':self.node_switch_paused,
                    'restore_available':self.recovery_path.exists(), 'egress':self.egress, 'controller':self.controller_status(),
                    'checked_at':self.checked_at, 'connected':self.connected, 'closed':closed, 'closed_at':closed_at, 'cause':cause,
                    'auto_restore':self.auto_restore, 'clear_stale':self.clear_stale, 'auto_restore_paused':paused,
                    'recovering':{'count':self.recoveries, 'needed':RECOVERY_ROUNDS * 2 ** min(self.flaps, MAX_FLAPS)}
                                 if cause == 'exit' and self.auto_restore and not paused else None}

    def controller_status(self):
        option = self.controllers.get(self.client,{})
        return {'url':option.get('url',''), 'secret_set':'secret' in option,
                'automatic':not bool(option.get('url'))}

    def controller_view(self):
        """Explicit local settings view. Never include this in status or diagnostics."""
        with self.lock:
            option = self.controllers.get(self.client, {})
            if not isinstance(option, dict):
                option = {}
            try:
                # An isolated demo must not read the real client's credentials.
                text = '' if self.demo else self.controller.runtime()
            except (OSError, ValueError, TypeError):
                text = ''
            manual = option.get('url', '')
            secret = option.get('secret', scalar(text, 'secret'))
            url, transport = manual, 'http' if manual else ''
            if not manual:
                pipe = scalar(text, 'external-controller-pipe')
                if pipe.startswith('\\\\.\\pipe\\') and len(pipe) <= 200:
                    url, transport = pipe, 'pipe'
                else:
                    try:
                        address = urllib.parse.urlsplit('//' + scalar(text, 'external-controller'))
                        if (address.hostname in ('127.0.0.1', 'localhost', '::1', '0.0.0.0', '::') and address.port
                                and not address.username and not address.password and not address.path):
                            host = '[::1]' if ':' in address.hostname else '127.0.0.1'
                            url, transport = f'http://{host}:{address.port}', 'http'
                    except ValueError:
                        pass
            return {'client': self.client, 'url': url, 'secret': secret,
                    'automatic': not bool(manual), 'url_override': manual,
                    'secret_override': 'secret' in option, 'transport': transport}

    def configure_controller(self, raw):
        with self.lock:
            if raw.get('client', self.client) != self.client:
                raise ValueError('客户端已切换，请重新填写')
            url = raw.get('url','')
            if not isinstance(url,str):
                raise ValueError('请输入本机控制器地址')
            url = url.strip()
            if url:
                try:
                    parsed = urllib.parse.urlsplit(url)
                    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1','localhost') or not parsed.port or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
                        raise ValueError()
                    url = 'http://' + ('[::1]' if parsed.hostname=='::1' else '127.0.0.1') + ':' + str(parsed.port)
                except ValueError:
                    raise ValueError('仅支持 http://127.0.0.1:端口 的本机控制器') from None
            option = self.controllers.get(self.client,{}).copy()
            secret = raw.get('secret','')
            if 'clear_secret' in raw and type(raw['clear_secret']) is not bool:
                raise ValueError('清除密钥选项应为开关值')
            if not isinstance(secret,str) or len(secret)>1024 or any(ord(c)<32 or ord(c)>126 for c in secret):
                raise ValueError('密钥请使用不含换行的英文字符')
            if raw.get('clear_secret'):
                option.pop('secret',None)
            elif secret:
                option['secret'] = secret
            option['url'] = url
            controllers = self.controllers | {self.client:option}
            atomic_write(self.controllers_path,json.dumps(controllers).encode())
            self.controllers = controllers
            self.controller = Controller(kind=self.client, options=option)
            self.failures=0; self.egress=''; self.state='idle'; self.message='控制器设置已更新，请连接客户端'
            self.exit_state = ''; self.recoveries = 0; self.recovery_exit = ''
            self.exit_chain = []; self.exit_delay = 48 if self.demo else None
            self.connected = None

    def connect(self):
        """Quiet probe for the settings page: it never raises and is not audited."""
        try:
            return {'connected': True, 'groups': self.groups()}
        except ValueError as error:
            return {'connected': False, 'groups': [], 'message': str(error)}

    def groups(self):
        if self.demo:
            self.connected = True
            return ['示例代理']
        try:
            rows = self.controller.request('GET', '/proxies').get('proxies', {})
            self.connected = True
            return sorted(name for name, row in rows.items() if row.get('type') in ('Selector','URLTest','Fallback','LoadBalance') and name not in ('GLOBAL',) and not any(c in name for c in ',\r\n'))
        except Exception:
            self.connected = False
            if self.client=='flclash':
                raise ValueError('请启动 FlClash，并在“外部控制”中开启本地接口后连接') from None
            raise ValueError('未连接 Clash Verge / Mihomo，请先启动代理客户端') from None

    def node_choices(self):
        """Expose names and existing delays only, never subscription credentials."""
        with self.lock:
            if self.demo:
                rows = {'示例代理': {'type': 'Selector', 'all': ['香港 01', '日本 01', '自动优选'], 'now': getattr(self, '_demo_node', '香港 01')},
                        '自动优选': {'type': 'URLTest', 'all': ['香港 01', '日本 01'], 'now': '香港 01'},
                        '香港 01': {'type': 'VLESS', 'history': [{'delay': 48}]},
                        '日本 01': {'type': 'Shadowsocks', 'history': [{'delay': 76}]}}
            else:
                try:
                    rows = self.controller.request('GET', '/proxies')['proxies']
                    if not isinstance(rows, dict):
                        raise ValueError()
                except Exception:
                    self.connected = False
                    raise ValueError('请连接客户端的外部控制器后重试') from None
            groups, nodes = [], {}
            for name, row in rows.items():
                if not isinstance(name, str) or not isinstance(row, dict):
                    continue
                kind = row.get('type', '')
                history = row.get('history')
                last = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
                delay = last.get('delay')
                nodes[name] = {'type': kind if isinstance(kind, str) else '',
                               'delay': delay if type(delay) in (int, float) and 0 < delay < 60000 else None}
                choices = row.get('all')
                if isinstance(choices, list):
                    choices = list(dict.fromkeys(c for c in choices if isinstance(c, str)))
                    groups.append({'name': name, 'type': nodes[name]['type'],
                                   'selected': row.get('now') if isinstance(row.get('now'), str) else '',
                                   'selectable': kind == 'Selector', 'all': choices})
            self.connected = True
            return {'client': self.client, 'groups': groups, 'nodes': nodes}

    def select_node(self, raw):
        if (not isinstance(raw, dict) or set(raw) != {'client', 'group', 'node'}
                or any(not isinstance(raw[k], str) or not raw[k] or len(raw[k]) > 1024 for k in raw)):
            raise ValueError('请选择有效的代理组和节点')
        with self.lock:
            if raw['client'] != self.client:
                raise ValueError('客户端已切换，请刷新节点列表')
            choices = self.node_choices()
            group = next((g for g in choices['groups'] if g['name'] == raw['group']), None)
            if not group or not group['selectable']:
                raise ValueError('此代理组由客户端自动选择，请选择手动代理组')
            if raw['node'] not in group['all']:
                raise ValueError('节点列表已变化，请刷新后选择')
            self.node_switches = []; self.node_retry_at = None; self._recovery_switched = False
            self._release_fallback_hold()
            if group['selected'] == raw['node']:
                return
            # Invalidate the previous exit even if a write succeeds but its
            # readback is interrupted. Keep the recovery record and switches.
            self.exit_state = 'uncertain'
            self.recoveries = 0; self.recovery_exit = ''; self.failures = 0; self.egress = ''
            self.exit_chain = []; self.exit_delay = 48 if self.demo else None
            self.message = '节点已变化，等待检测'
            if self.demo:
                self._demo_node = raw['node']
                return
            try:
                path = '/proxies/' + urllib.parse.quote(raw['group'], safe='')
                self.controller.request('PUT', path, {'name': raw['node']})
                current = self.controller.request('GET', '/proxies')['proxies'].get(raw['group'], {})
            except Exception:
                raise ValueError('节点切换未确认，请刷新当前选择') from None
            if current.get('now') != raw['node']:
                raise ValueError('客户端未确认该节点，请刷新后重试')

    def rules(self, paths, policies=None, group=None):
        from .probes import ApplicationGuards
        result = []
        policies = self.policies if policies is None else policies
        group = self.group if group is None else group
        for path in paths:
            mode = policies.get(ApplicationGuards.identifier(path), 'follow')
            if mode == 'follow':
                continue
            value = str(path)
            if any(c in value for c in ',\r\n'):
                raise ValueError('此程序路径包含规则分隔符，请调整目录名')
            if mode == 'proxy' and not group:
                raise ValueError('请先选择代理组')
            result.append(f'PROCESS-PATH,{value},{"DIRECT" if mode == "direct" else group}')
        return result

    def configure(self, paths, *, identifier=None, mode=None, group=None):
        from .probes import ApplicationGuards
        with self.lock:
            policies = self.policies.copy()
            if identifier is not None:
                if not isinstance(identifier, str) or mode not in ('follow', 'direct', 'proxy') or identifier not in {ApplicationGuards.identifier(path) for path in paths}:
                    raise ValueError('请选择有效的程序和网络方式')
                if mode == 'follow':
                    policies.pop(identifier, None)
                else:
                    policies[identifier] = mode
            selected = self.group if group is None else group
            if not isinstance(selected, str) or (selected and selected not in self.groups()):
                raise ValueError('请选择当前可用的代理组')
            rules, old = self.rules(paths, policies, selected), self.rules(paths)
            if not self.demo and (rules or old):
                self._apply(rules, old)
            self.policies, self.group = policies, selected
            self.save()

    def _apply(self, rules, old):
        applied=False
        try:
            general = self.controller.request('GET', '/configs')
            if general.get('mode') != 'rule':
                raise ValueError('请先将代理客户端切换为规则模式')
            runtime = self.controller.runtime()
            payload = apply_rules(runtime, rules, list(old) + self.managed_rules)
            previous = apply_rules(runtime, old, list(rules) + self.managed_rules)
            # Retain live TUN/port settings and selected nodes during a rule reload.
            if isinstance(general.get('tun'),dict):
                payload = root_section(payload,'tun',general['tun'])
                previous = root_section(previous,'tun',general['tun'])
            for key in ('mixed-port','port','socks-port','redir-port','tproxy-port'):
                if key in general:
                    payload = root_value(payload,key,str(general[key]))
                    previous = root_value(previous,key,str(general[key]))
            proxies = self.controller.request('GET','/proxies').get('proxies',{})
            selected = {name:row['now'] for name,row in proxies.items() if row.get('type')=='Selector' and row.get('now') in row.get('all',[])}
            # Let the running core validate and apply the payload. Client files stay untouched.
            self.controller.request('PUT', '/configs', {'payload':payload})
            applied=True
            for name,choice in selected.items():
                self.controller.request('PUT','/proxies/'+urllib.parse.quote(name,safe=''),{'name':choice})
            self.managed_rules = list(dict.fromkeys(self.managed_rules + list(old) + list(rules)))
        except Exception as error:
            if applied:
                # Retain cleanup history even if a subsequent selector call or rollback fails.
                self.managed_rules=list(dict.fromkeys(self.managed_rules+list(old)+list(rules)))
                self.save()
                try:
                    self.controller.request('PUT','/configs',{'payload':previous})
                    for name,choice in selected.items():
                        self.controller.request('PUT','/proxies/'+urllib.parse.quote(name,safe=''),{'name':choice})
                except Exception:
                    pass
            if isinstance(error,ControllerError):
                raise ValueError(f'代理配置未被内核接受（HTTP {error.status}），请检查客户端规则') from None
            if isinstance(error,ValueError):
                raise
            raise ValueError('代理分流应用未完成，请检查客户端连接后重试') from None

    @staticmethod
    def system_proxy():
        import winreg
        path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            result = {}
            for name, default in [('ProxyEnable',0),('ProxyServer','')]:
                try:
                    result[name] = winreg.QueryValueEx(key,name)[0]
                except OSError:
                    result[name] = default
            return result

    def active_exit(self):
        """Follow the running routing mode and selected chain, not a guard's group.

        GLOBAL can point to DIRECT while rule mode routes its default traffic
        through a different selector. URLTest on a group can pick a healthy
        alternative; test its *currently selected leaf* instead.
        """
        self.exit_chain = []
        config = self.controller.request('GET', '/configs')
        rows = self.controller.request('GET', '/proxies').get('proxies', {})
        mode = str(config.get('mode', '')).lower()
        if mode == 'direct':
            self.exit_chain = ['DIRECT']
            self.egress = 'DIRECT'; self.probe_reason = '当前客户端处于直连模式'
            return None
        if mode == 'global':
            target = 'GLOBAL'
        elif mode == 'rule':
            rules = self.controller.request('GET', '/rules').get('rules', [])
            target = next((r.get('proxy') for r in reversed(rules) if r.get('type', '').lower() == 'match'), None)
            if target in ('DIRECT', 'REJECT', 'REJECT-DROP', None):
                # A DIRECT catch-all may still coexist with one explicit proxy
                # selector. Several different selectors require user intent.
                references = {r.get('proxy') for r in rules if r.get('proxy') in rows
                              and rows[r['proxy']].get('type', '').lower() not in ('direct', 'reject', 'rejectdrop')}
                if len(references) != 1:
                    self.egress = ''; self.probe_reason = '未确认唯一代理出口，保留当前设置'
                    return None
                target = next(iter(references))
        else:
            self.egress = ''; self.probe_reason = '未识别代理路由模式，保留当前设置'
            return None
        chain = self.exit_chain
        for _ in range(16):
            if not isinstance(target, str) or target in chain or target not in rows:
                self.egress = ' → '.join(chain); self.probe_reason = '代理选择链尚未就绪，保留当前设置'
                return None
            chain.append(target)
            row = rows[target]
            kind = row.get('type', '').lower()
            if kind in ('direct', 'reject', 'rejectdrop', 'compatible', 'pass') or target in ('DIRECT', 'REJECT', 'REJECT-DROP'):
                self.egress = ' → '.join(chain); self.probe_reason = '当前出口为直连或拦截策略'
                return None
            if 'all' in row:
                target = row.get('now')
                continue
            self.egress = ' → '.join(chain); self.probe_reason = ''
            return target
        self.egress = ' → '.join(chain); self.probe_reason = '代理选择链过长，保留当前设置'
        return None

    def proxy_healthy(self):
        self.exit_delay = None
        try:
            leaf = self.active_exit()
        except Exception:
            self.connected = False
            self.probe_reason = '代理控制器未连接，出口状态待确认'
            return None
        self.connected = True
        if leaf is None:
            return None
        failures = 0
        for url in ('https://www.gstatic.com/generate_204', 'https://cp.cloudflare.com/generate_204'):
            try:
                query = urllib.parse.urlencode({'url':url, 'timeout':3500, 'expected':'204'})
                reply = self.controller.request('GET', '/proxies/' + urllib.parse.quote(leaf, safe='') + '/delay?' + query)
                delay = reply.get('delay')
                if type(delay) in (int, float) and 0 < delay <= 3500:
                    self.exit_delay = delay
                    return True
                self.probe_reason = '出口检测返回未识别结果，保留当前设置'
            except ControllerError as error:
                if error.status in (503, 504):
                    failures += 1
                else:
                    self.probe_reason = '代理出口检测接口未就绪，保留当前设置'
            except Exception:
                self.probe_reason = '代理控制器未响应，出口状态待确认'
        return False if failures == 2 else None

    def _probe_exit(self):
        """Keep node reachability separate from the switches' recovery state."""
        healthy = self.proxy_healthy()
        self.exit_state = 'healthy' if healthy is True else 'failed' if healthy is False else 'uncertain'
        return healthy

    def _switch_node(self, cancel):
        """Try two measured alternatives in the deepest manual selector.

        A round counts once as soon as a selection write is attempted, even if
        its reply is lost. Rollback is always attempted after an unfinished round.
        """
        def result(state, detail):
            return {'result': state, 'detail': detail}
        if self.demo:
            return result('skipped', '演示模式不自动切换节点')
        if cancel.is_set():
            return result('skipped', '操作已取消，保留当前设置')
        if self.node_switch_paused:
            return result('skipped', '节点切换过于频繁，已暂停')
        self.node_retry_at = time.monotonic()
        try:
            if self.active_exit() is None:
                return result('skipped', '当前出口由客户端自动选择')
            rows = self.controller.request('GET', '/proxies')['proxies']
            group = next((name for name in reversed(self.exit_chain)
                          if rows.get(name, {}).get('type') == 'Selector'), None)
            if group is None:
                return result('skipped', '当前出口由客户端自动选择')
            original = rows[group]['now']
            excluded = {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE'}
            blocked = {'direct', 'reject', 'rejectdrop', 'reject-drop', 'pass', 'compatible'}
            candidates = list(dict.fromkeys(name for name in rows[group].get('all', [])
                if isinstance(name, str) and name != original and name.upper() not in excluded
                and isinstance(rows.get(name), dict) and rows[name].get('type', '').lower() not in blocked))
            if not candidates:
                return result('skipped', '同组没有其他节点')
            query = urllib.parse.urlencode({'url': 'https://www.gstatic.com/generate_204', 'timeout': 3500, 'expected': '204'})
            def valid(value):
                return type(value) in (int, float) and 0 < value <= 3500
            delays = {}
            try:
                reply = self.controller.request('GET', '/group/' + urllib.parse.quote(group, safe='') + '/delay?' + query)
                if not isinstance(reply, dict) or not any(name in reply for name in candidates):
                    raise ValueError()
                delays = {name: reply[name] for name in candidates if valid(reply.get(name))}
            except Exception as exc:
                if isinstance(exc, ControllerError) and exc.status not in (400, 404, 405, 501):
                    return result('failed', '节点测速接口未就绪，请连接客户端外部控制接口')
                if not isinstance(exc, (ControllerError, ValueError, TypeError, KeyError)):
                    return result('failed', '节点测速未确认，请连接客户端外部控制接口')
                # Older cores do not implement the group endpoint. Bound work on
                # their per-node endpoint; never turn controller errors into data.
                for name in candidates[:12]:
                    if cancel.is_set():
                        return result('skipped', '操作已取消，保留当前设置')
                    try:
                        reply = self.controller.request('GET', '/proxies/' + urllib.parse.quote(name, safe='') + '/delay?' + query)
                        if valid(reply.get('delay')):
                            delays[name] = reply['delay']
                    except Exception:
                        pass
            if not delays:
                return result('failed', '同组没有可用节点')
            path = '/proxies/' + urllib.parse.quote(group, safe='')
            attempted = False
            try:
                for name in sorted(delays, key=delays.get)[:2]:
                    if cancel.is_set():
                        break
                    # A manual selection during measurement takes precedence.
                    current = self.controller.request('GET', '/proxies')['proxies'][group].get('now')
                    if not attempted and current != original:
                        return result('skipped', '出口已切换，等待重新检测')
                    if not attempted:
                        self.node_switches.append(time.monotonic())
                        self.node_retry_at = self.node_switches[-1]
                        attempted = True
                    self.exit_delay = None; self.exit_state = 'uncertain'
                    self.recoveries = 0; self.recovery_exit = ''
                    self.controller.request('PUT', path, {'name': name})
                    current = self.controller.request('GET', '/proxies')['proxies'][group].get('now')
                    if current == name and self._probe_exit() is True and not cancel.is_set():
                        self.failures = 0
                        return result('done', f'「{group}」从 {original} 切换至 {name}（{delays[name]:g} ms）')
            except Exception:
                pass
            if attempted:
                try:
                    self.controller.request('PUT', path, {'name': original})
                    current = self.controller.request('GET', '/proxies')['proxies'][group].get('now')
                    if current != original:
                        raise ValueError()
                    self.active_exit()
                except Exception:
                    self.exit_state = 'uncertain'; self.exit_delay = None
                    return result('failed', '原节点恢复未确认，请在客户端核对')
                self.exit_state = 'failed'; self.exit_delay = None
            return result('failed', '切换后出口仍未通过')
        except Exception:
            return result('failed', '节点切换未确认，请连接客户端外部控制接口')

    def direct_healthy(self, cancel):
        from .network import Network
        network = Network(cancel)
        network.timeout = 3
        # Force the existing physical WiFi adapter path to avoid circular TUN checks.
        return network.opener.try_wifi_path() and network.online()

    def _client_flags(self):
        """Read persisted client switches for settled-state verification, never write them."""
        if self.client=='flclash':
            document = json.loads((self.controller.root/'shared_preferences.json').read_text('utf-8-sig'))
            config = document.get('flutter.config',document.get('config',{}))
            if isinstance(config,str):
                config=json.loads(config)
            return {'proxy':bool(config.get('networkProps',{}).get('systemProxy',True)),
                    'tun':bool(config.get('patchClashConfig',{}).get('tun',{}).get('enable',False))}
        text=(self.controller.root/'verge.yaml').read_text('utf-8-sig')
        flags={name:scalar(text,key) for name,key in [('proxy','enable_system_proxy'),('tun','enable_tun_mode')]}
        if any(value not in ('true','false') for value in flags.values()):
            raise OSError('客户端开关状态待确认')
        return {key:value=='true' for key,value in flags.items()}

    def subscription(self):
        with self.lock:
            if self.demo:
                return {'client': self.client, 'available': True, 'name': '示例订阅', 'kind': 'remote',
                        'updated_at': (self._demo_at - timedelta(hours=2)).isoformat(timespec='seconds'),
                        'usage': {'used': 42 * 1024**3, 'total': 200 * 1024**3,
                                  'expire': (self._demo_at + timedelta(days=20)).isoformat(timespec='seconds')},
                        'message': ''}
            return self._subscriptions.read(self.client, self.controller.root)

    def switch_status(self):
        """Polling never navigates a client window or writes a recovery record."""
        with self.lock:
            if self.demo:
                return {'client': self.client, **self._demo_switches, 'proxy_foreign': False,
                        'guarded': [], 'restoring': False}
            proxy, tun, foreign, port = None, None, False, None
            try:
                config = self.controller.request('GET', '/configs')
                candidate = config.get('mixed-port') or config.get('port')
                if type(candidate) is int and 0 < candidate < 65536:
                    port = candidate
                enabled = config.get('tun', {}).get('enable')
                tun = enabled if type(enabled) is bool else None
            except Exception:
                pass
            try:
                self._client_flags()  # read-only persisted indicator; actual state wins
            except Exception:
                pass
            try:
                system = self.system_proxy()
                if system.get('ProxyEnable') == 0:
                    proxy = False
                elif system.get('ProxyEnable') == 1 and port is not None:
                    proxy = self._owns_proxy(system, port)
                    foreign = not proxy
            except Exception:
                pass
            guarded, restoring = [], False
            try:
                saved = json.loads(self.recovery_path.read_text('utf-8'))
                guarded = [field for field in saved['fields'] if field in ('proxy', 'tun') and field in saved['completed']]
                restoring = saved.get('restoring') is True
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            return {'client': self.client, 'proxy': proxy, 'tun': tun, 'proxy_foreign': foreign,
                    'guarded': guarded, 'restoring': restoring}

    def set_switch(self, field, enabled, *, client=None):
        if not isinstance(field, str) or field not in ('proxy', 'tun') or type(enabled) is not bool:
            raise ValueError('代理开关设置格式不正确')
        with self.lock:
            if client is not None and client != self.client:
                raise ValueError('客户端已切换，请刷新后重试')
            if self.demo:
                self._demo_switches[field] = enabled
                return self.switch_status()
            try:
                saved = self._snapshot() if self.recovery_path.exists() else None
                if saved and saved.get('restoring'):
                    raise ValueError('代理恢复尚未完成，请先点击恢复代理')
                config = self.controller.request('GET', '/configs')
                port = config.get('mixed-port') or config.get('port')
                if field == 'proxy' and enabled and (type(port) is not int or not 0 < port < 65536):
                    raise ValueError('客户端代理端口待确认，请连接客户端后重试')
                current = self.switches.read()[field]
                if type(current) is not bool:
                    raise SwitchStateError('客户端开关状态待确认')
                if current != enabled:
                    self.switches.set(field, current, enabled)
                self._verify_switch(field, enabled, {'port': port})
                if saved and field in saved['fields']:
                    for key in ('fields', 'completed', 'attempted', 'restored', 'restore_attempted'):
                        saved[key] = [name for name in saved.get(key, []) if name != field]
                    if saved['fields']:
                        atomic_write(self.recovery_path, json.dumps(saved).encode())
                    else:
                        self.recovery_path.unlink()
                        self._reset_automation()
                self.failures = 0; self._release_fallback_hold()
                self.state = 'direct' if self.recovery_path.exists() else 'idle'
                self.message = '已手动调整，继续检测出口'
                return self.switch_status()
            except ClientControlError as exc:
                if field == 'tun' and exc.code == 'switch_disabled':
                    raise ValueError('TUN 开关不可用，请先在客户端中安装服务模式') from None
                raise ValueError(str(exc)) from None
            except SwitchStateError as exc:
                raise ValueError(str(exc)) from None
            except ValueError:
                raise
            except Exception:
                raise ValueError('客户端开关尚未确认，请保持客户端运行后重试') from None

    def local_proxy(self):
        """The connected client's loopback HTTP port, or None.

        TUN mode usually leaves the system proxy off, so an updater that only
        follows the system or environment proxy would download directly.
        """
        if self.demo:
            return None
        try:
            config = self.controller.request('GET', '/configs')
            port = int(config.get('mixed-port') or config.get('port') or 0)
        except Exception:
            return None
        return f'http://127.0.0.1:{port}' if 0 < port < 65536 else None

    @staticmethod
    def _owns_proxy(proxy, port):
        return proxy.get('ProxyEnable')==1 and bool(port) and bool(proxy.get('ProxyServer')) and all(
            re.fullmatch(r'(?:https?=|socks=)?(?:127\.0\.0\.1|localhost):'+str(port), part.strip())
            for part in proxy['ProxyServer'].split(';'))

    def _verify_switch(self, field, desired, snapshot, *, wait=True):
        until=time.monotonic()+(5 if wait else 0)
        stable=0
        while True:
            flags=self._client_flags()
            config=self.controller.request('GET','/configs')
            system=self.system_proxy()
            actual=config.get('tun',{}).get('enable') if field=='tun' else system['ProxyEnable']==1
            matches=type(actual) is bool and flags[field]==desired and actual==desired
            if field=='proxy' and desired:
                matches=matches and self._owns_proxy(system,snapshot['port'])
            stable=stable+1 if matches else 0
            if stable >= (2 if wait else 1):
                return True
            if time.monotonic()>=until:
                label = 'TUN' if field == 'tun' else '系统代理'
                raise SwitchStateError(label + '：客户端开关与实际网络状态不一致')
            time.sleep(.2)

    def _snapshot(self):
        path=self.recovery_path
        saved=json.loads(path.read_text('utf-8'))
        if saved.get('schema')!=2:
            flags=saved.get('flags',{})
            if not all(flags.get(k) in ('true','false') for k in ('enable_system_proxy','enable_tun_mode')):
                raise ValueError('旧版恢复记录请在代理客户端中核对并恢复')
            fields=[key for key,active in [('tun',saved.get('tun')),('proxy',saved.get('owns_proxy'))] if active]
            completed=[key for key in fields if saved.get(key+'_disabled')]
            saved={'schema':2,'original':{'proxy':flags['enable_system_proxy']=='true','tun':flags['enable_tun_mode']=='true'},
                   'port':int(self.controller.request('GET','/configs').get('mixed-port',0)),
                   'fields':fields,'completed':completed,'attempted':completed.copy(),
                   'restored':[],'restore_attempted':[],'restoring':False,'other_proxy':False}
            atomic_write(path,json.dumps(saved).encode())
        return saved

    def fail_open(self, only=None):
        """Use the GUI's own switch handlers, then verify GUI, saved and OS/core states."""
        if self.demo:
            raise ValueError('演示模式不切换系统网络')
        requested = list(dict.fromkeys(only)) if only is not None else ['tun', 'proxy']
        if any(field not in ('tun', 'proxy') for field in requested):
            raise ValueError('代理开关设置格式不正确')
        if not requested:
            return False
        path=self.recovery_path
        if path.exists():
            saved=self._snapshot()
            if saved.get('restoring'):
                raise ValueError('代理恢复尚未完成，请点击恢复代理')
            missing = [field for field in requested if field not in saved['fields']]
            if missing:
                gui = self.switches.read()
                system = self.system_proxy()
                other_proxy = system['ProxyEnable'] == 1 and not self._owns_proxy(system, saved['port'])
                for field in missing:
                    if gui[field] and (field != 'proxy' or not other_proxy):
                        saved['fields'].append(field)
                        saved['original'][field] = gui[field]
                atomic_write(path, json.dumps(saved).encode())
        else:
            config=self.controller.request('GET','/configs')
            system=self.system_proxy()
            port=int(config.get('mixed-port',0))
            other_proxy=system['ProxyEnable']==1 and not self._owns_proxy(system,port)
            try:
                # Saved switches and live state agree there is nothing to close:
                # do not show or navigate the client window on every failed tick.
                flags=self._client_flags()
                active = {'tun': flags['tun'] or config.get('tun',{}).get('enable'),
                          'proxy': self._owns_proxy(system,port) or (flags['proxy'] and not other_proxy)}
                if not any(active[field] for field in requested):
                    return False
            except (OSError,ValueError,TypeError,AttributeError,KeyError):
                pass
            gui=self.switches.read()
            fields=[key for key in requested if gui[key] and (key!='proxy' or not other_proxy)]
            if not fields:
                if 'tun' in requested and bool(config.get('tun',{}).get('enable')):
                    raise OSError('客户端与内核开关不一致，请在客户端核对')
                return False
            saved={'schema':2,'original':gui,'fields':fields,'port':port,'completed':[],
                   'attempted':[],'restored':[],'restore_attempted':[],'restoring':False,'other_proxy':other_proxy,
                   'at':timestamp()}
            atomic_write(path,json.dumps(saved).encode())
        if saved.get('restoring'):
            raise ValueError('代理恢复尚未完成，请点击恢复代理')
        errors=[]
        fields = [field for field in requested if field in saved['fields']]
        for field in fields:
            try:
                if field in saved['completed']:
                    self._verify_switch(field,False,saved,wait=False)
                    continue
                current=self.switches.read()[field]
                if current:
                    if field in saved['attempted']:
                        raise OSError('客户端开关发生变化，请手动核对')
                    if field=='proxy':
                        system=self.system_proxy()
                        if system['ProxyEnable']==1 and not self._owns_proxy(system,saved['port']):
                            raise OSError('系统代理已由其他客户端接管')
                    saved['attempted'].append(field)
                    atomic_write(path,json.dumps(saved).encode())
                    self.switches.set(field,True,False)
                self._verify_switch(field,False,saved)
                saved['completed'].append(field)
            except Exception as exc:
                label = 'TUN' if field == 'tun' else '系统代理'
                errors.append(str(exc) if isinstance(exc,(SwitchStateError,ClientControlError)) else label)
            finally:
                atomic_write(path,json.dumps(saved).encode())
        if errors:
            raise SwitchStateError('客户端关闭尚未确认：'+'；'.join(errors))
        return bool(fields)

    def restore(self, *, automatic=False):
        with self.lock:
            if self.demo:
                raise ValueError('演示模式不切换系统网络')
            path=self.recovery_path
            if not path.exists():
                if self.stale_path.exists():
                    raise ValueError('代理客户端尚未运行；启动客户端后会自动处理')
                raise ValueError('没有待恢复的代理设置')
            saved=self._snapshot()
            try:
                # Reconcile a switch whose GUI call completed before a timeout/crash.
                gui=self.switches.read()
                for field in saved.get('restore_attempted',[]):
                    if field not in saved['restored'] and gui[field]==saved['original'][field]:
                        try:
                            self._verify_switch(field,saved['original'][field],saved,wait=False)
                        except SwitchStateError:
                            pass  # The explicit repair below handles a stale ON indicator.
                        else:
                            saved['restored'].append(field)
                for field in saved['attempted']:
                    if field not in saved['completed'] and not gui[field]:
                        self._verify_switch(field,False,saved,wait=False)
                        saved['completed'].append(field)
                if 'proxy' in saved['completed']:
                    system=self.system_proxy()
                    if system['ProxyEnable']==1 and not self._owns_proxy(system,saved['port']):
                        raise ValueError('系统代理已由其他客户端接管，请在客户端中核对')
                # Validate every switch before writes. An explicit restore may also
                # repair a stale ON indicator by invoking the client's OFF/ON handlers.
                reapply = set()
                for field in saved['completed']:
                    if gui[field] == saved['original'][field]:
                        try:
                            self._verify_switch(field,saved['original'][field],saved,wait=False)
                        except SwitchStateError:
                            reapply.add(field)
                            if field in saved['restored']:
                                saved['restored'].remove(field)
                        else:
                            if field not in saved['restored']:
                                saved['restored'].append(field)
                    else:
                        self._verify_switch(field,False,saved,wait=False)
                        if field in saved['restored']:
                            saved['restored'].remove(field)
                saved['restoring']=True
                atomic_write(path,json.dumps(saved).encode())
                for field in reversed(saved['completed']):
                    if field in saved['restored']:
                        continue
                    if field not in saved.setdefault('restore_attempted',[]):
                        saved['restore_attempted'].append(field)
                        atomic_write(path,json.dumps(saved).encode())
                    if field in reapply:
                        self.switches.set(field,True,False)
                        self._verify_switch(field,False,saved)
                    self.switches.set(field,False,saved['original'][field])
                    self._verify_switch(field,saved['original'][field],saved)
                    saved['restored'].append(field)
                    atomic_write(path,json.dumps(saved).encode())
                path.unlink()
                self.failures=0; self.state='restored'; self.message='客户端已恢复代理设置'
                if not automatic:
                    self._reset_automation()
            except (ValueError, SwitchStateError, ClientControlError) as exc:
                self.state='error'; self.message=str(exc)
                raise ValueError(str(exc)) from None
            except Exception:
                self.state='error'; self.message='客户端恢复尚未确认，请保持客户端运行后重试'
                raise ValueError(self.message) from None

    def tick(self, cancel, event, allowed=lambda: True, *, required_failures=3, switch_allowed=lambda: True,
             auto_restore=lambda: False, expected_client=None, manual=False):
        with self.lock:
            if expected_client is not None and expected_client != self.client:
                return
            if self.demo or cancel.is_set() or not allowed():
                return
            self.checked_at = timestamp()
            self.exit_state = 'uncertain'
            notice = self._tick(cancel, allowed, required_failures, switch_allowed, auto_restore, manual)
        # Do not hold the routing lock while the service persists a log record.
        if notice:
            event(*notice)

    def _tick(self, cancel, allowed, required_failures, switch_allowed, auto_restore, manual=False):
        notice = None
        sampled_exit = ''
        try:
            record = self.recovery_path
            if record.exists():
                saved=self._snapshot()
                if saved.get('restoring'):
                    self.state='error'; self.message='客户端恢复尚未完成，请点击恢复代理'; return None
                if set(saved['completed'])==set(saved['fields']):
                    try:
                        for field in saved['fields']:
                            self._verify_switch(field,False,saved,wait=False)
                    except SwitchStateError:
                        # A verified manual restore retires the old record; a
                        # partial or stale indicator needs explicit user repair.
                        # Saved switches and live state decide first, so an unresolved
                        # record does not operate the client window on every tick.
                        flags=self._client_flags()
                        if not all(flags[k]==saved['original'][k] for k in saved['fields']):
                            raise SwitchStateError('恢复记录与客户端状态不一致，请点击恢复代理')
                        for field in saved['fields']:
                            self._verify_switch(field,saved['original'][field],saved,wait=False)
                        gui=self.switches.read()
                        if not all(gui[k]==saved['original'][k] for k in saved['fields']):
                            raise SwitchStateError('恢复记录与客户端状态不一致，请点击恢复代理')
                        record.unlink()
                        self._reset_automation()
                        self.failures=0; self.state='restored'; self.message='客户端已手动恢复，继续检测出口'; return None
                    return self._await_recovery(cancel, allowed, saved, auto_restore, switch_allowed)
            else:
                previous_exit = self.egress
                healthy = self._probe_exit()
                sampled_exit = self.egress
                if sampled_exit != previous_exit:
                    self.failures = 0; self._release_fallback_hold()
                if healthy is True:
                    self._release_fallback_hold()
                    if self.restored_at is not None and time.monotonic() - self.restored_at >= FLAP_WINDOW:
                        self.flaps = 0; self.restored_at = None
                    self.failures = 0; self.state = 'healthy'; self.message = '代理出口正常'; return None
                if healthy is None:
                    self.failures = 0; self.state = 'uncertain'; self.message = self.probe_reason or '代理出口待确认'; return None
                if not switch_allowed():
                    self.failures = 0; self.state = 'failed'; self.message = '代理出口异常，自动处置未开启'; return None
                if cancel.is_set() or not allowed():
                    return None
                if not self.direct_healthy(cancel):
                    self.failures = 0; self.state = 'uncertain'; self.message = '直连也未通过，保留代理设置'; return None
                self.failures += 1
                self.state = 'checking'; self.message = f'代理出口异常，正在复核 {min(self.failures,required_failures)}/{required_failures}'
                if self.failures < required_failures:
                    return None
            if cancel.is_set() or not allowed() or not switch_allowed():
                return None
            fresh = not record.exists()
            if not fresh and not self.direct_healthy(cancel):
                self.state = 'uncertain'; self.message = '直连也未通过，保留代理设置'; return None
            if fresh and sampled_exit:
                confirmed_leaf = self.active_exit()
                if confirmed_leaf is None or self.egress != sampled_exit:
                    self.failures = 0; self.state = 'uncertain'; self.message = '出口已切换，等待重新检测'; return None
            if (fresh and not manual and self.fallback_hold_until is not None
                    and time.monotonic() < self.fallback_hold_until and self._fallback_result):
                # Same failure episode, already handled with no effect: show that
                # result instead of re-measuring every node on every tick.
                self.state, self.message = self._fallback_result
                return None
            notice = self._run_fallback(cancel, allowed, switch_allowed, auto_restore, fresh)
        except (SwitchStateError, ValueError, ClientControlError) as exc:
            self.state='error'; self.message=str(exc)
        except Exception:
            if self.state != 'error' and self.failures >= required_failures:
                notice = ('error', '代理切换未完成，请在代理客户端中检查 TUN 和系统代理')
            self.state = 'error'; self.message = '客户端开关尚未确认，请检查客户端窗口、权限或手动恢复'
        return notice

    def _run_fallback(self, cancel, allowed, switch_allowed, auto_restore, fresh):
        steps = self.fallback['order']
        enabled = self.fallback['enabled']
        expected_exit = self.egress
        exit_changed = False
        self.last_fallback = {'at': timestamp(), 'steps': []}
        records = self.last_fallback['steps']
        recovered, closed, errors, close_errors = False, False, False, False
        for step in steps:
            if not enabled[step]:
                outcome = {'result': 'skipped', 'detail': '此步骤未开启'}
            elif recovered:
                outcome = {'result': 'skipped', 'detail': '切换节点后出口已恢复'}
            elif cancel.is_set() or not allowed() or not switch_allowed():
                outcome = {'result': 'skipped', 'detail': '处置已取消，保留当前设置'}
            elif exit_changed:
                outcome = {'result': 'skipped', 'detail': '出口已切换，等待重新检测'}
            else:
                try:
                    if expected_exit:
                        leaf = self.active_exit()
                        exit_changed = leaf is None or self.egress != expected_exit
                    if exit_changed:
                        outcome = {'result': 'skipped', 'detail': '出口已切换，等待重新检测'}
                    elif step == 'switch_node':
                        outcome = self._switch_node(cancel)
                        recovered = outcome['result'] == 'done'
                        self._recovery_switched = recovered
                    else:
                        field = 'tun' if step == 'close_tun' else 'proxy'
                        changed = self.fail_open(only=[field])
                        closed = closed or changed
                        label = 'TUN' if field == 'tun' else '系统代理'
                        outcome = {'result': 'done' if changed else 'skipped',
                                   'detail': label + (' 已关闭，状态已核验' if changed else ' 未开启或不属于本客户端')}
                except (ValueError, SwitchStateError, ClientControlError) as exc:
                    outcome = {'result': 'failed', 'detail': str(exc)}
                except Exception:
                    outcome = {'result': 'failed', 'detail': '客户端开关尚未确认，请在客户端核对'}
            errors = errors or outcome['result'] == 'failed'
            close_errors = close_errors or (step != 'switch_node' and outcome['result'] == 'failed')
            records.append({'step': step, **outcome})
        if closed and fresh:
            recent = self.restored_at is not None and time.monotonic() - self.restored_at < FLAP_WINDOW
            self.flaps = self.flaps + 1 if recent else 0
            self.restored_at = None; self.recoveries = 0; self.auto_attempts = 0
        if exit_changed:
            self.failures = 0; self.exit_state = 'uncertain'
            self.state = 'uncertain'; self.message = '出口已切换，等待重新检测'
        elif not any(enabled.values()):
            self.state = 'failed'; self.message = '代理出口异常，故障处置均未开启'
        elif close_errors:
            self.state = 'error'; self.message = '故障处置尚未全部完成，请核对客户端状态'
        elif closed:
            self.state = 'direct'
            self.message = '客户端已关闭代理，状态已核验'
            if recovered:
                self.message = '已切换节点，出口恢复，' + ('确认稳定后自动恢复代理' if auto_restore() else '等待手动恢复代理')
        elif recovered:
            self.state = 'healthy'; self.message = '已切换节点，代理出口正常'
        elif not errors:
            self.state = 'idle'; self.message = '未检测到本客户端的系统代理或 TUN'
        else:
            self.state = 'failed'; self.message = '代理出口异常，未关闭本客户端开关'
        summary = '；'.join(row['detail'] for row in records if enabled[row['step']])
        notice = ('error' if errors else 'info', '代理故障处置：' + (summary or self.message))
        no_effect = fresh and not closed and not recovered and not exit_changed
        if not no_effect:
            self._release_fallback_hold()
            return notice
        self.fallback_hold_until = time.monotonic() + NODE_RETRY_INTERVAL
        self._fallback_result = (self.state, self.message)
        if notice == self._fallback_notice:
            return None  # the same no-op outcome is already in the log
        self._fallback_notice = notice
        return notice

    def _await_recovery(self, cancel, allowed, saved, auto_restore, switch_allowed=lambda: True):
        """The proxy is closed and verified. Optionally bring it back once the exit is stable."""
        self.state = 'direct'
        notice = None
        # Detection also runs when restoration is disabled or paused. The core's
        # node probe does not require system proxy or TUN to be enabled.
        healthy = self._probe_exit()
        if (healthy is False and self.fallback['enabled']['switch_node'] and not self.node_switch_paused
                and (self.node_retry_at is None or time.monotonic() - self.node_retry_at >= NODE_RETRY_INTERVAL)
                and not cancel.is_set() and allowed() and switch_allowed() and self.direct_healthy(cancel)):
            self.node_retry_at = time.monotonic()
            outcome = self._switch_node(cancel)
            self.last_fallback = {'at': timestamp(), 'steps': [{'step': 'switch_node', **outcome}]}
            notice = ('error' if outcome['result'] == 'failed' else 'info', '代理故障处置：' + outcome['detail'])
            if outcome['result'] == 'done':
                healthy = True
                self._recovery_switched = True
            else:
                self._recovery_switched = False
        sampled_exit = self.egress
        if not auto_restore():
            self.recoveries = 0; self.recovery_exit = ''
            label = '代理出口正常' if healthy is True else '代理出口异常' if healthy is False else '代理出口待确认'
            self.message = ('已切换节点，出口恢复' if healthy is True and self._recovery_switched else label) + '，等待手动恢复代理'
            return notice
        if self.flaps >= MAX_FLAPS:
            self.message = '代理出口反复异常，已停止自动恢复，请手动恢复'
            return notice
        if self.auto_attempts >= MAX_AUTO_ATTEMPTS:
            self.message = '自动恢复未完成，请点击恢复代理'
            return notice
        if healthy is not True:
            self.recoveries = 0; self.recovery_exit = ''
            self.message = '客户端已关闭代理，出口恢复后自动恢复'
            return notice
        if sampled_exit != self.recovery_exit:
            self.recoveries = 0; self.recovery_exit = sampled_exit
        self.recoveries += 1
        if self.recoveries < RECOVERY_ROUNDS * 2 ** self.flaps:
            self.message = '代理出口已恢复，确认稳定后自动恢复代理'
            return notice
        if cancel.is_set() or not allowed() or not auto_restore():
            return notice
        # Do not combine healthy samples from different nodes or restore after
        # the selection changed while a probe was in flight.
        try:
            same_exit = self.active_exit() is not None and self.egress == sampled_exit
        except Exception:
            same_exit = False
        if not same_exit:
            self.recoveries = 0; self.recovery_exit = ''; self.exit_state = 'uncertain'
            self.message = '出口已切换，重新确认后再恢复代理'
            return notice
        self.recoveries = 0
        labels = '、'.join('TUN' if key == 'tun' else '系统代理' for key in saved['fields'])
        try:
            self.restore(automatic=True)
        except ValueError:
            self.auto_attempts += 1  # restore() has already recorded the state and message
            return notice
        self.auto_attempts = 0
        self.restored_at = time.monotonic()
        self.message = '代理出口恢复正常，已自动恢复代理'
        return ('info', '代理出口恢复正常，已自动恢复 ' + labels)

    # ---- a system proxy left behind by a client that is no longer listening ----

    @staticmethod
    def _port_listening(port):
        """True, False for an explicit refusal, or None when it cannot be told.

        Windows answers a closed loopback port only after about two seconds,
        so a shorter timeout would never see the refusal.
        """
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=4):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            return None

    @staticmethod
    def write_system_proxy(enabled):
        """Only for a proxy whose owner is not listening; a live client uses its own switch."""
        import winreg
        path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'ProxyEnable', 0, winreg.REG_DWORD, 1 if enabled else 0)
        wininet = ctypes.WinDLL('wininet', use_last_error=True)
        wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        wininet.InternetSetOptionW.restype = wintypes.BOOL
        for option in (39, 37):  # INTERNET_OPTION_SETTINGS_CHANGED, INTERNET_OPTION_REFRESH
            wininet.InternetSetOptionW(None, option, None, 0)

    def _stale_port(self, system):
        """The selected client's own loopback port when the system proxy points only at it."""
        server = system.get('ProxyServer') or ''
        if system.get('ProxyEnable') != 1 or not server:
            return None
        ports = set()
        for part in server.split(';'):
            match = re.fullmatch(r'(?:https?=|socks=)?(?:127\.0\.0\.1|localhost):(\d{1,5})', part.strip())
            if not match:
                return None
            ports.add(int(match[1]))
        if len(ports) != 1:
            return None
        port = ports.pop()
        try:
            text = self.controller.runtime()
        except (OSError, ValueError, TypeError):
            return None
        # Another client's proxy is never touched: the port has to be this client's.
        if not isinstance(text, str) or str(port) not in {scalar(text, key) for key in ('mixed-port', 'port')}:
            return None
        return port if 0 < port < 65536 else None

    def _stale_record(self):
        try:
            saved = json.loads(self.stale_path.read_text('utf-8'))
            if isinstance(saved, dict) and isinstance(saved.get('server'), str) and type(saved.get('port')) is int:
                return saved
        except (OSError, ValueError):
            pass
        return None

    def stale_tick(self, cancel, event, enabled=lambda: True):
        """Close a system proxy whose client stopped listening, and retire the record later.

        Returns True while this is the state to show, so the caller can skip
        the exit probe that needs a running client.
        """
        notice = None
        with self.lock:
            if self.demo or cancel.is_set() or self.recovery_path.exists():
                return False
            try:
                handled, notice = self._stale(cancel, enabled)
                if handled:
                    self.exit_state = 'uncertain'
            except Exception:
                self.stale_failures = 0
                handled = False
        if notice:
            event(*notice)
        return handled

    def _stale(self, cancel, enabled):
        record = self.stale_path
        system = self.system_proxy()
        if record.exists():
            saved = self._stale_record()
            if saved is None:
                record.unlink(missing_ok=True)
                return False, None
            self.checked_at = timestamp()
            if system['ProxyEnable'] == 1 or system['ProxyServer'] != saved['server']:
                record.unlink(); self.recoveries = 0
                self.state = 'restored'; self.message = '系统代理已重新设置，继续检测'
                return False, ('info', '系统代理已由客户端重新设置')
            if self._port_listening(saved['port']) is not True:
                self.recoveries = 0
                self.state = 'direct'; self.message = '代理客户端未运行，已关闭残留的系统代理'
                return True, None
            wanted = False
            if self.auto_restore:
                try:
                    wanted = self._client_flags()['proxy'] is True
                except Exception:
                    wanted = False
            if not wanted:
                record.unlink(); self.recoveries = 0
                self.state = 'restored'; self.message = '代理客户端已重新运行，系统代理保持关闭'
                return False, ('info', '代理客户端已重新运行；系统代理保持关闭，可在客户端中开启')
            self.recoveries += 1
            if self.recoveries < STALE_RECOVERY_ROUNDS:
                self.state = 'direct'; self.message = '代理客户端已重新运行，确认后恢复系统代理'
                return True, None
            self.recoveries = 0
            record.unlink()
            try:
                self.write_system_proxy(True)
                if self.system_proxy()['ProxyEnable'] != 1:
                    raise OSError()
            except Exception:
                self.state = 'error'; self.message = '系统代理恢复未完成，请在客户端中开启'
                return True, ('error', '系统代理恢复未完成，请在代理客户端中开启')
            self.state = 'restored'; self.message = '代理客户端已重新运行，已恢复系统代理'
            return False, ('info', '代理客户端已重新运行，已恢复系统代理')

        if not enabled():
            self.stale_failures = 0
            return False, None
        port = self._stale_port(system)
        if port is None or self._port_listening(port) is not False:
            self.stale_failures = 0
            return False, None
        now = time.monotonic()
        self.stale_clears = [moment for moment in self.stale_clears if now - moment < 600]
        self.checked_at = timestamp()
        if len(self.stale_clears) >= 3:
            # Something keeps re-enabling a proxy nobody serves; stop fighting it.
            self.state = 'error'; self.message = '系统代理被反复重新开启且端口无响应，请在客户端中处理'
            return True, None
        self.stale_failures += 1
        if self.stale_failures < STALE_ROUNDS:
            self.state = 'checking'; self.message = '代理端口无响应，正在复核'
            return True, None
        if cancel.is_set():
            return False, None
        if not self.direct_healthy(cancel):
            self.stale_failures = 0
            self.state = 'uncertain'; self.message = '直连也未通过，保留系统代理'
            return True, None
        if self.system_proxy() != system or self._port_listening(port) is not False:
            self.stale_failures = 0
            return False, None
        self.stale_failures = 0
        atomic_write(record, json.dumps({'schema':1, 'server':system['ProxyServer'], 'port':port, 'at':timestamp()}).encode())
        try:
            self.write_system_proxy(False)
            if self.system_proxy()['ProxyEnable'] != 0:
                raise OSError()
        except Exception:
            record.unlink(missing_ok=True)
            self.state = 'error'; self.message = '残留的系统代理关闭未完成，请在系统设置中关闭代理'
            return True, ('error', '残留的系统代理关闭未完成，请在系统设置中关闭代理')
        self.stale_clears.append(now)
        self.state = 'direct'; self.message = '代理客户端未运行，已关闭残留的系统代理'
        return True, ('info', '代理客户端未运行且端口无响应，已关闭残留的系统代理')

    def remove(self, paths, identifiers):
        with self.lock:
            remaining = {key:value for key,value in self.policies.items() if key not in identifiers}
            old, new = self.rules(paths), self.rules(paths, remaining)
            if old != new and not self.demo:
                self._apply(new, old)
            self.policies = remaining
            self.save()

    def ensure_rules(self, paths):
        """Reapply through the external API when either client reloads a profile."""
        if self.demo or not (self.policies or self.managed_rules) or self.recovery_path.exists():
            return
        with self.lock:
            wanted = self.rules(paths)
            live = self.controller.request('GET','/rules').get('rules',[])
            expected = [(rule.split(',',2)[1],rule.split(',',2)[2]) for rule in wanted]
            present = [(row.get('payload'),row.get('proxy')) for row in live if row.get('type')=='ProcessPath']
            stale={(r.split(',',2)[1],r.split(',',2)[2]) for r in self.managed_rules} - set(expected)
            if present[:len(expected)]!=expected or stale.intersection(present):
                self._apply(wanted,wanted)
                return True
        return False

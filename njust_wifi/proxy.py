"""Optional Clash Verge/Mihomo integration. No resident helper or bundled core."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import http.client
import io
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse

from .storage import atomic_write

BEGIN = '// BEGIN SCHOOL-WIFI-CONNECT'
END = '// END SCHOOL-WIFI-CONNECT'
MAX_REPLY = 4 * 1024 * 1024
CLIENTS = {'clash-verge':'Clash Verge', 'flclash':'FlClash'}


class ControllerError(OSError):
    """Keep HTTP status without forwarding a config body containing private data."""
    def __init__(self, status):
        self.status = status
        super().__init__(f'controller HTTP {status}')


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
    def __init__(self, root=None, *, kind='clash-verge'):
        if kind not in CLIENTS:
            raise ValueError('请选择支持的代理客户端')
        self.kind = kind
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
        text = self.runtime()
        pipe = scalar(text, 'external-controller-pipe')
        secret = scalar(text, 'secret')
        body = b'' if payload is None else json.dumps(payload).encode()
        if any(c in secret for c in '\r\n'):
            raise OSError('invalid controller config')
        request = (f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {secret}\r\n'
                   f'Connection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        if pipe.startswith('\\\\.\\pipe\\') and len(pipe) <= 200:
            raw = pipe_request(pipe, request)
        else:
            address = urllib.parse.urlsplit('//' + scalar(text,'external-controller'))
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
    def __init__(self, root, *, demo=False, controller=None):
        self.root, self.demo = root, demo
        self.controller = controller
        supplied_kind = getattr(controller,'kind',None)
        self.client = supplied_kind if supplied_kind in CLIENTS else 'clash-verge'
        self.profiles = {}
        self.lock = threading.RLock()
        self.policies = {}
        self.group = ''
        self.state = 'idle'
        self.message = '未启用代理保护'
        self.failures = 0
        self.egress = ''
        self.probe_reason = ''
        self.path = root / 'routing-settings.json'
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text('utf-8'))
                self.client = raw.get('client') if raw.get('client') in CLIENTS else self.client
                self.profiles = {key:value for key,value in raw.get('profiles',{}).items() if key in CLIENTS and isinstance(value,dict)}
                raw = self.profiles.get(self.client, raw)
                self.group = raw.get('group', '') if isinstance(raw.get('group'), str) else ''
                self.policies = {key: value for key, value in raw.get('policies', {}).items() if re.fullmatch(r'[0-9a-f]{12}', key) and value in ('direct', 'proxy')}
            except (ValueError, TypeError, AttributeError):
                pass
        if self.controller is None:
            self.controller = Controller(kind=self.client)

    @property
    def recovery_path(self):
        return self.root / ('proxy-recovery-flclash.json' if self.client=='flclash' else 'proxy-recovery.json')

    def save(self):
        self.profiles[self.client] = {'group':self.group, 'policies':self.policies.copy()}
        atomic_write(self.path, json.dumps({'client':self.client, 'profiles':self.profiles, 'group':self.group, 'policies':self.policies}, ensure_ascii=False).encode())

    def set_client(self, client):
        if not isinstance(client,str) or client not in CLIENTS:
            raise ValueError('请选择支持的代理客户端')
        with self.lock:
            self.save()
            self.client = client
            profile = self.profiles.get(client,{})
            self.group = profile.get('group','')
            self.policies = profile.get('policies',{}).copy()
            self.controller = Controller(kind=client)
            self.failures = 0; self.egress = ''; self.state = 'idle'; self.message = '请选择连接客户端'
            self.save()

    def status(self):
        with self.lock:
            return {'state':self.state, 'message':self.message, 'group':self.group, 'client':self.client,
                    'restore_available':self.recovery_path.exists(), 'egress':self.egress}

    def groups(self):
        if self.demo:
            return ['示例代理']
        try:
            rows = self.controller.request('GET', '/proxies').get('proxies', {})
            return sorted(name for name, row in rows.items() if row.get('type') in ('Selector','URLTest','Fallback','LoadBalance') and name not in ('GLOBAL',) and not any(c in name for c in ',\r\n'))
        except Exception:
            if self.client=='flclash':
                raise ValueError('请启动 FlClash，并在“外部控制”中开启本地接口后连接') from None
            raise ValueError('未连接 Clash Verge / Mihomo，请先启动代理客户端') from None

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
        try:
            general = self.controller.request('GET', '/configs')
            if general.get('mode') != 'rule':
                raise ValueError('请先将代理客户端切换为规则模式')
            runtime = self.controller.runtime()
            payload = apply_rules(runtime, rules, old)
            # Retain live TUN/port settings and selected nodes during a rule reload.
            if isinstance(general.get('tun'),dict):
                payload = root_section(payload,'tun',general['tun'])
            for key in ('mixed-port','port','socks-port','redir-port','tproxy-port'):
                if key in general:
                    payload = root_value(payload,key,str(general[key]))
            proxies = self.controller.request('GET','/proxies').get('proxies',{})
            selected = {name:row['now'] for name,row in proxies.items() if row.get('type')=='Selector' and row.get('now') in row.get('all',[])}
            if self.client=='flclash':
                target = self.controller.runtime_path
                original = target.read_bytes()
                updated = payload.encode('utf-8')
                backup = self.root / 'proxy-flclash-original.yaml'
            else:
                target = self.controller.root / 'profiles/Script.js'
                original = target.read_bytes()
                updated = make_script(original.decode('utf-8-sig'), rules).encode('utf-8')
                backup = self.root / 'proxy-script-original.js'
            # Core validates the full config before applying. Existing custom rules remain.
            self.controller.request('PUT', '/configs', {'payload':payload})
            for name,choice in selected.items():
                self.controller.request('PUT','/proxies/'+urllib.parse.quote(name,safe=''),{'name':choice})
            try:
                if not backup.exists():
                    atomic_write(backup, original)
                if target.read_bytes()!=original:
                    raise OSError('client file changed during reload')
                atomic_write(target, updated)
            except OSError:
                self.controller.request('PUT', '/configs', {'payload':runtime})
                raise
        except ControllerError as error:
            raise ValueError(f'代理配置未被内核接受（HTTP {error.status}），请检查客户端规则') from None
        except ValueError:
            raise
        except Exception:
            raise ValueError('代理分流应用未完成，请检查客户端连接后重试') from None

    @staticmethod
    def system_proxy(values=None):
        import winreg
        path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | (winreg.KEY_SET_VALUE if values else 0)) as key:
            if values:
                for name, value in values.items():
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ, value)
                ctypes.windll.wininet.InternetSetOptionW(None, 39, None, 0)
                ctypes.windll.wininet.InternetSetOptionW(None, 37, None, 0)
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
        config = self.controller.request('GET', '/configs')
        rows = self.controller.request('GET', '/proxies').get('proxies', {})
        mode = str(config.get('mode', '')).lower()
        if mode == 'direct':
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
        chain = []
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
        try:
            leaf = self.active_exit()
        except Exception:
            self.probe_reason = '代理控制器未连接，出口状态待确认'
            return None
        if leaf is None:
            return None
        failures = 0
        for url in ('https://www.gstatic.com/generate_204', 'https://cp.cloudflare.com/generate_204'):
            try:
                query = urllib.parse.urlencode({'url':url, 'timeout':3500, 'expected':'204'})
                reply = self.controller.request('GET', '/proxies/' + urllib.parse.quote(leaf, safe='') + '/delay?' + query)
                if isinstance(reply.get('delay'), (int, float)) and reply['delay'] > 0:
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

    def direct_healthy(self, cancel):
        from .network import Network
        network = Network(cancel)
        network.timeout = 3
        # Force the existing physical WiFi adapter path to avoid circular TUN checks.
        return network.opener.try_wifi_path() and network.online()

    def _client_flags(self, enable=None, expected=None):
        if self.client=='flclash':
            path = self.controller.root / 'shared_preferences.json'
            original = path.read_bytes()
            document = json.loads(original.decode('utf-8-sig'))
            key = 'flutter.config' if 'flutter.config' in document else 'config'
            config = json.loads(document[key]) if isinstance(document[key],str) else document[key]
            flags = {'enable_system_proxy':str(bool(config.get('networkProps',{}).get('systemProxy',True))).lower(),
                     'enable_tun_mode':str(bool(config.get('patchClashConfig',{}).get('tun',{}).get('enable',False))).lower()}
            if expected is not None and flags!=expected:
                raise ValueError('代理客户端设置已变化，请在客户端中恢复')
            if enable:
                config.setdefault('networkProps',{})['systemProxy']=enable['enable_system_proxy']=='true'
                config.setdefault('patchClashConfig',{}).setdefault('tun',{})['enable']=enable['enable_tun_mode']=='true'
                document[key]=json.dumps(config,ensure_ascii=False) if isinstance(document[key],str) else config
                if path.read_bytes()!=original:
                    raise ValueError('客户端设置正在保存，请稍后重试')
                atomic_write(path,json.dumps(document,ensure_ascii=False).encode())
            return flags
        path = self.controller.root / 'verge.yaml'
        text = path.read_text('utf-8-sig')
        flags = {key:scalar(text,key) for key in ('enable_system_proxy','enable_tun_mode')}
        if expected is not None and any(flags[k] != expected[k] for k in flags):
            raise ValueError('代理客户端设置已变化，请在客户端中恢复')
        if enable:
            for key, value in enable.items():
                text = root_value(text, key, value)
            atomic_write(path, text.encode('utf-8'))
        return flags

    def fail_open(self):
        """Resume partial work after a controller outage; keep the original snapshot."""
        if self.demo:
            raise ValueError('演示模式不切换系统网络')
        record_path = self.recovery_path
        if record_path.exists():
            snapshot = json.loads(record_path.read_text('utf-8'))
        else:
            try:
                config = self.controller.request('GET', '/configs')
            except Exception:
                runtime = self.controller.runtime()
                tun_block = re.search(r'^tun:[^\n]*\n((?:[ \t]+[^\n]*\n?)*)', runtime, re.M)
                config = {'mixed-port':scalar(runtime, 'mixed-port', '0'),
                          'tun':{'enable':bool(tun_block and re.search(r'^\s+enable:\s*true\s*$', tun_block[1], re.M))}}
            proxy = self.system_proxy()
            port = int(config.get('mixed-port', 0))
            owns_proxy = proxy['ProxyEnable'] == 1 and bool(port) and all(
                re.fullmatch(r'(?:https?=|socks=)?(?:127\.0\.0\.1|localhost):' + str(port), part)
                for part in proxy['ProxyServer'].split(';'))
            tun = bool(config.get('tun', {}).get('enable'))
            if not owns_proxy and not tun:
                return False
            flags = self._client_flags()
            snapshot = {'proxy':proxy, 'owns_proxy':owns_proxy, 'tun':tun, 'flags':flags, 'proxy_disabled':False, 'tun_disabled':False}
            atomic_write(record_path, json.dumps(snapshot).encode())
        errors = []
        try:
            if snapshot['tun'] and not snapshot['tun_disabled']:
                try:
                    self.controller.request('PATCH', '/configs', {'tun':{'enable':False}})
                    snapshot['tun_disabled'] = True
                except Exception:
                    errors.append('tun')
            if snapshot['owns_proxy'] and not snapshot['proxy_disabled']:
                current = self.system_proxy()
                if current != snapshot['proxy']:
                    raise ValueError('系统代理设置已变化，请在客户端中检查')
                self.system_proxy({'ProxyEnable':0})
                snapshot['proxy_disabled'] = True
            # Persistence only changes the switches that actually completed.
            self._client_flags({'enable_tun_mode':'false' if snapshot['tun_disabled'] else snapshot['flags']['enable_tun_mode'],
                               'enable_system_proxy':'false' if snapshot['proxy_disabled'] else snapshot['flags']['enable_system_proxy']})
        finally:
            atomic_write(record_path, json.dumps(snapshot).encode())
        if errors:
            raise OSError('TUN controller unavailable')
        return True

    def restore(self):
        with self.lock:
            if self.demo:
                raise ValueError('演示模式不切换系统网络')
            path = self.recovery_path
            if not path.exists():
                raise ValueError('没有待恢复的代理设置')
            try:
                saved = json.loads(path.read_text('utf-8'))
                flags = self._client_flags()
                expected = {'enable_system_proxy':'false' if saved['proxy_disabled'] else saved['flags']['enable_system_proxy'], 'enable_tun_mode':'false' if saved['tun_disabled'] else saved['flags']['enable_tun_mode']}
                if flags != expected:
                    raise ValueError('代理客户端设置已变化，请在客户端中恢复')
                current = self.system_proxy()
                if saved['proxy_disabled'] and (current['ProxyEnable'] != 0 or current['ProxyServer'] != saved['proxy']['ProxyServer']):
                    raise ValueError('系统代理设置已变化，请在代理客户端中恢复')
                if saved['tun_disabled']:
                    self.controller.request('PATCH', '/configs', {'tun':{'enable':saved['tun']}})
                if saved['proxy_disabled']:
                    self.system_proxy(saved['proxy'])
                self._client_flags(saved['flags'])
                path.unlink()
                self.failures = 0; self.state = 'restored'; self.message = '已恢复代理设置'
            except ValueError:
                raise
            except Exception:
                raise ValueError('代理设置尚未恢复，请启动代理客户端后重试') from None

    def tick(self, cancel, event, allowed=lambda: True, *, required_failures=3, switch_allowed=lambda: True):
        notice = None
        sampled_exit = ''
        with self.lock:
            if self.demo or cancel.is_set() or not allowed():
                return
            try:
                record = self.recovery_path
                if record.exists():
                    saved = json.loads(record.read_text('utf-8'))
                    complete = (not saved['tun'] or saved['tun_disabled']) and (not saved['owns_proxy'] or saved['proxy_disabled'])
                    if complete:
                        self.state = 'direct'; self.message = '已切换直连，等待手动恢复'; return
                else:
                    previous_exit = self.egress
                    healthy = self.proxy_healthy()
                    sampled_exit = self.egress
                    if sampled_exit != previous_exit:
                        self.failures = 0
                    if healthy is True:
                        self.failures = 0; self.state = 'healthy'; self.message = '代理出口正常'; return
                    if healthy is None:
                        self.failures = 0; self.state = 'uncertain'; self.message = self.probe_reason or '代理出口待确认'; return
                    if not switch_allowed():
                        self.failures = 0; self.state = 'failed'; self.message = '代理出口异常，自动关闭尚未开启'; return
                    if cancel.is_set() or not allowed():
                        return
                    if not self.direct_healthy(cancel):
                        self.failures = 0; self.state = 'uncertain'; self.message = '直连也未通过，保留代理设置'; return
                    self.failures += 1
                    self.state = 'checking'; self.message = f'代理出口异常，正在复核 {min(self.failures,required_failures)}/{required_failures}'
                    if self.failures < required_failures:
                        return
                if cancel.is_set() or not allowed() or not switch_allowed():
                    return
                if not record.exists() and sampled_exit:
                    confirmed_leaf = self.active_exit()
                    if confirmed_leaf is None or self.egress != sampled_exit:
                        self.failures = 0; self.state = 'uncertain'; self.message = '出口已切换，等待重新检测'; return
                if self.fail_open():
                    self.state = 'direct'; self.message = '代理故障，已切换全机直连'
                    notice = ('info', '代理出口异常，已关闭系统代理和 TUN，等待手动恢复')
                else:
                    self.state = 'idle'; self.message = '未检测到本客户端的系统代理或 TUN'
            except Exception:
                if self.state != 'error' and self.failures >= required_failures:
                    notice = ('error', '代理切换未完成，请在代理客户端中检查 TUN 和系统代理')
                self.state = 'error'; self.message = '代理保护操作未完成，请检查客户端'
        # Do not hold the routing lock while the service persists a log record.
        if notice:
            event(*notice)

    def remove(self, paths, identifiers):
        with self.lock:
            remaining = {key:value for key,value in self.policies.items() if key not in identifiers}
            old, new = self.rules(paths), self.rules(paths, remaining)
            if old != new and not self.demo:
                self._apply(new, old)
            self.policies = remaining
            self.save()

    def ensure_rules(self, paths):
        """FlClash regenerates config.yaml on profile reload; restore only our rules."""
        if self.demo or self.client!='flclash' or not self.policies or self.recovery_path.exists():
            return
        with self.lock:
            wanted = self.rules(paths)
            live = self.controller.request('GET','/rules').get('rules',[])
            expected = [(rule.split(',',2)[1],rule.split(',',2)[2]) for rule in wanted]
            present = [(row.get('payload'),row.get('proxy')) for row in live if row.get('type')=='ProcessPath']
            if present[:len(expected)]!=expected:
                self._apply(wanted,wanted)

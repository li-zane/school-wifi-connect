"""Small Session 0 network worker; no HTTP server, GUI, app launcher or proxy control."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time

from . import __version__
from .network import Network
from .storage import atomic_write, validate_settings

SID_PATTERN = r'S-1-5-21-\d+-\d+-\d+-\d+'


def token_sid(token):
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    size = wintypes.DWORD()
    advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
    if not size.value:
        raise OSError('token unavailable')
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
        raise OSError('token unavailable')
    sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError('SID unavailable')
    try:
        return text.value
    finally:
        kernel.LocalFree(text)


def current_sid():
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError('process token unavailable')
    try:
        return token_sid(token)
    finally:
        kernel.CloseHandle(token)


def owner_logged_on(owner_sid):
    """SYSTEM checks every logged-on session, including locked/disconnected sessions."""
    class Session(ctypes.Structure):
        _fields_ = [('id', wintypes.DWORD), ('station', wintypes.LPWSTR), ('state', ctypes.c_int)]
    wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    wts.WTSEnumerateSessionsW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.POINTER(ctypes.POINTER(Session)), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    rows, count = ctypes.POINTER(Session)(), wintypes.DWORD()
    if not wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(rows), ctypes.byref(count)):
        raise OSError('sessions unavailable')
    try:
        for row in rows[:count.value]:
            if row.id == 0:
                continue
            token = wintypes.HANDLE()
            if not wts.WTSQueryUserToken(row.id, ctypes.byref(token)):
                # ERROR_NO_TOKEN is normal at the sign-in screen. Other errors
                # cannot prove the user is absent, so do not operate the network.
                if ctypes.get_last_error() != 1008:
                    raise OSError('session token unavailable')
                continue
            try:
                if token_sid(token) == owner_sid:
                    return True
            finally:
                kernel.CloseHandle(token)
        return False
    finally:
        wts.WTSFreeMemory(rows)


@contextmanager
def network_lease(cancel, owner_sid=None):
    """Serialize network recovery across user and boot workers, also during handoff."""
    if os.name != 'nt':
        yield
        return
    owner_sid = owner_sid or current_sid()
    if not re.fullmatch(SID_PATTERN, owner_sid):
        raise RuntimeError('联网任务身份待确认')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    class Security(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel.CreateMutexW.argtypes = [ctypes.POINTER(Security), wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;' + owner_sid + ')', 1, ctypes.byref(descriptor), None):
        raise RuntimeError('联网任务锁创建失败')
    security = Security(ctypes.sizeof(Security), descriptor, False)
    handle = kernel.CreateMutexW(ctypes.byref(security), False, 'Global\\SchoolWiFiConnect-Network-' + owner_sid)
    kernel.LocalFree(descriptor)
    if not handle:
        raise RuntimeError('联网任务锁不可用')
    held = False
    try:
        while not cancel.is_set():
            result = kernel.WaitForSingleObject(handle, 200)
            if result in (0, 0x80):
                held = True
                if cancel.is_set():
                    raise InterruptedError()
                yield
                return
            if result != 0x102:
                raise RuntimeError('联网任务锁不可用')
        raise InterruptedError()
    finally:
        if held:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def read_json(path, maximum=65536):
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('configuration too large')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('configuration must be an object')
    return result


def boot_configuration(directory):
    """Read only bounded data; never construct Store or migrate/write a user's files as SYSTEM."""
    settings = validate_settings(read_json(directory / 'settings.json'))
    settings['show_portal'] = False
    username = password = ''
    if settings['remember_credentials'] and (directory / 'credentials.json').exists():
        raw = read_json(directory / 'credentials.json')
        if any(not isinstance(raw.get(k), str) or len(raw[k]) > 1024 for k in ('username', 'password')):
            raise ValueError('invalid saved credentials')
        username, password = raw['username'], raw['password']
    active = settings['startup_enabled'] and settings['auto_start_monitor']
    configured = bool(settings['ssid'] and username and password) or bool(settings['fallback_networks'])
    return settings, username, password, bool(active and configured)


class BootWorker:
    def __init__(self, manifest, *, network_factory=Network, session_check=owner_logged_on,
                 lease=network_lease, clock=time.monotonic):
        self.owner_sid = manifest['owner_sid']
        if not re.fullmatch(SID_PATTERN, self.owner_sid):
            raise ValueError('invalid owner SID')
        self.directory = Path(manifest['data_directory'])
        if not self.directory.is_absolute():
            raise ValueError('absolute data directory required')
        self.network_factory, self.session_check, self.lease, self.clock = network_factory, session_check, lease, clock
        self.failures = 0
        self.cancel = threading.Event()
        self.closed = threading.Event()
        self.state = {'version': __version__, 'state': 'starting', 'message': '开机联网后台已启动',
                      'checked_at': None, 'online_since': None, 'duration_seconds': 0}
        self.online_start = None
        self.events = []

    def record(self, state, message):
        # Only Network's fixed product messages reach this bounded local record.
        if state == 'checking':
            return
        entry = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'state': state, 'message': message}
        if not self.events or self.events[-1]['state'] != state or self.events[-1]['message'] != message:
            self.events = (self.events + [entry])[-30:]

    def cycle(self):
        try:
            if self.session_check(self.owner_sid):
                self.online_start = None
                self.state.update(state='user_session', message='用户已登录，由用户后台接管', online_since=None, duration_seconds=0)
                return 10
            settings, username, password, enabled = boot_configuration(self.directory)
            if not enabled:
                self.online_start = None
                self.state.update(state='disabled', message='等待开启开机启动、自动监控并保存网络配置', online_since=None, duration_seconds=0)
                return 15
            self.cancel.clear()
            if self.session_check(self.owner_sid):
                return 1
            watcher_done = threading.Event()
            def watch_login():
                while not watcher_done.wait(1):
                    try:
                        logged_on = self.session_check(self.owner_sid)
                    except OSError:
                        logged_on = True
                    if self.closed.is_set() or logged_on:
                        self.cancel.set()
                        return
            watcher = threading.Thread(target=watch_login, name='boot-handoff', daemon=True)
            watcher.start()
            try:
                with self.lease(self.cancel, self.owner_sid):
                    if self.session_check(self.owner_sid) or self.closed.is_set():
                        raise InterruptedError()
                    network = self.network_factory(self.cancel, all_users=True)
                    network.recover(settings, username, password, self.record)
            finally:
                username = password = ''
                watcher_done.set()
                watcher.join(timeout=2)
            self.failures = 0
            if self.online_start is None:
                self.online_start = self.clock()
                self.state['online_since'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.state.update(state='online', message='登录前网络连接正常', duration_seconds=int(self.clock() - self.online_start))
            return settings['interval_minutes'] * 60
        except InterruptedError:
            self.state.update(state='handoff', message='正在交接用户后台')
            return 1
        except Exception:
            self.online_start = None
            self.failures += 1
            self.state.update(state='retrying', message='开机联网未完成，将自动重试', online_since=None, duration_seconds=0)
            return min(30 * 2 ** min(self.failures - 1, 4), 300)
        finally:
            self.state['checked_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')

    def run(self, status_path):
        # Poll sessions while idle so login/logoff handoff does not wait for a full interval.
        deadline = 0.0
        previous_session = None
        while not self.closed.is_set():
            try:
                session = self.session_check(self.owner_sid)
            except OSError:
                session = None
            if session != previous_session or self.clock() >= deadline:
                if session is None:
                    self.state.update(state='waiting_session', message='等待确认 Windows 用户会话')
                    delay = 10
                else:
                    delay = self.cycle()
                atomic_write(status_path, json.dumps(self.state | {'events': self.events}, ensure_ascii=False).encode())
                deadline = float('inf') if session is True else self.clock() + delay
                previous_session = session
            self.closed.wait(5)


def main(manifest_path):
    try:
        if os.name != 'nt' or current_sid() != 'S-1-5-18':
            return 2
        manifest = read_json(manifest_path)
        BootWorker(manifest).run(manifest_path.with_name('status.json'))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        # No interactive error dialog in Session 0. Task Scheduler can restart it.
        return 2

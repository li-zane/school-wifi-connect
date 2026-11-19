"""Invoke client switch handlers in the background without showing its window."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys


class ClientControlError(OSError):
    """Fixed product message from the native bridge, without raw process output."""
    def __init__(self, message, *, code=''):
        self.code = code
        super().__init__(message)


def build_bridge(source, destination):
    framework = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319'
    references = ['System.dll', 'System.Core.dll'] + [str(framework / 'WPF' / name) for name in
                 ('UIAutomationClient.dll', 'UIAutomationTypes.dll', 'WindowsBase.dll')]
    subprocess.run([str(framework / 'csc.exe'), '/nologo', '/optimize+', '/platform:x64',
                    '/target:exe', '/out:' + str(destination)] +
                   ['/reference:' + name for name in references] + [str(source)],
                   capture_output=True, check=True, timeout=30, creationflags=0x08000000)


class ClientSwitches:
    def __init__(self, root, client):
        self.root, self.client = root, client

    def _executable(self):
        if os.name != 'nt':
            raise OSError('客户端开关需要 Windows 桌面会话')
        if getattr(sys, 'frozen', False):
            return Path(sys.executable).with_name('NJUSTWiFiProxyBridge.exe')
        source = Path(__file__).resolve().parents[1] / 'scripts/ProxyClient.cs'
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        destination = self.root / 'helpers' / (digest + '.exe')
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            build_bridge(source, destination)
        return destination

    def _run(self, *args):
        message = '后台开关未确认：请保持客户端运行，并检查双方运行权限'
        code = ''
        try:
            result = subprocess.run([str(self._executable()), self.client, *args],
                                    capture_output=True, timeout=20, creationflags=0x08000000)
            reply = json.loads(result.stdout.decode('utf-8-sig'))
            messages = {'window_unavailable':'客户端后台界面未就绪，请退出轻量模式后重试',
                        'client_elevated':'代理客户端正以管理员身份运行，无法操作其开关；请以普通权限启动客户端',
                        'dialog_open':'代理客户端有设置弹窗，请关闭弹窗后重试',
                        'switch_unavailable':'未找到唯一的代理开关，请检查客户端版本和界面',
                        'settings_unavailable':'客户端后台控件未就绪，请关闭轻量模式后重试',
                        'switch_disabled':'代理客户端开关当前不可用',
                        'state_changed':'代理客户端开关已变化，请重新检测',
                        'switch_unconfirmed':'客户端未确认开关操作，请核对客户端状态'}
            message = messages.get(reply.get('error'), message)
            code = reply.get('error', '')
            if result.returncode or reply.get('ok') is not True or any(type(reply.get(k)) is not bool for k in ('proxy','tun')):
                raise OSError()
            return {key:reply[key] for key in ('proxy','tun')}
        except Exception:
            raise ClientControlError(message, code=code) from None

    def read(self):
        return self._run('read')

    def set(self, field, expected, desired):
        if field not in ('proxy','tun') or type(expected) is not bool or type(desired) is not bool:
            raise ValueError('无效的客户端开关操作')
        reply = self._run('set', field, str(expected).lower(), str(desired).lower())
        if reply[field] != desired:
            raise OSError('客户端开关已变化，请在客户端中核对')
        return reply

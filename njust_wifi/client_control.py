"""Invoke the proxy GUI's own switches using a short-lived Windows helper."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys


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
        try:
            result = subprocess.run([str(self._executable()), self.client, *args],
                                    capture_output=True, timeout=20, creationflags=0x08000000)
            reply = json.loads(result.stdout.decode('utf-8-sig'))
            if result.returncode or reply.get('ok') is not True or any(type(reply.get(k)) is not bool for k in ('proxy','tun')):
                raise OSError()
            return {key:reply[key] for key in ('proxy','tun')}
        except Exception:
            raise OSError('客户端开关未确认：请保持客户端运行、关闭设置弹窗，并检查双方运行权限') from None

    def read(self):
        return self._run('read')

    def set(self, field, expected, desired):
        if field not in ('proxy','tun') or type(expected) is not bool or type(desired) is not bool:
            raise ValueError('无效的客户端开关操作')
        reply = self._run('set', field, str(expected).lower(), str(desired).lower())
        if reply[field] != desired:
            raise OSError('客户端开关已变化，请在客户端中核对')
        return reply

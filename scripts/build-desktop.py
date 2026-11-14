"""Build a ~25KB WinForms shell with a pinned WebView2 SDK; no browser payload."""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import urllib.request
import zipfile

SDK_VERSION = '1.0.4314.35'
SDK_SHA256 = '90778deb1b77e36292828b4b5833c9255c27533f40e0ddd55891ae718944edc4'
ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path):
    icon = ROOT / 'assets/NJUSTWiFi.ico'
    shutil.copy2(icon, destination / 'NJUSTWiFi.ico')
    cache = ROOT / '.tmp' / 'desktop-sdk'
    cache.mkdir(parents=True, exist_ok=True)
    package = cache / ('microsoft.web.webview2.' + SDK_VERSION + '.nupkg')
    if not package.exists():
        url = 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/' + SDK_VERSION + '/' + package.name
        with urllib.request.urlopen(url, timeout=60) as response:
            package.write_bytes(response.read())
    if hashlib.sha256(package.read_bytes()).hexdigest() != SDK_SHA256:
        raise RuntimeError('WebView2 SDK checksum mismatch')
    with zipfile.ZipFile(package) as archive:
        for source in ('lib/net462/Microsoft.Web.WebView2.Core.dll',
                       'lib/net462/Microsoft.Web.WebView2.WinForms.dll',
                       'build/native/x64/WebView2Loader.dll'):
            (destination / Path(source).name).write_bytes(archive.read(source))
        for name in ('LICENSE.txt', 'LICENSE', 'LICENSE.md'):
            if name in archive.namelist():
                (destination / 'WebView2-LICENSE.txt').write_bytes(archive.read(name))
                break
    compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    references = ['System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll',
                  str(destination / 'Microsoft.Web.WebView2.Core.dll'), str(destination / 'Microsoft.Web.WebView2.WinForms.dll')]
    command = [str(compiler), '/nologo', '/optimize+', '/platform:x64', '/target:winexe',
               '/win32icon:' + str(icon),
               '/out:' + str(destination / 'NJUSTWiFiWindow.exe')]
    command += ['/reference:' + item for item in references]
    subprocess.run(command + [str(ROOT / 'scripts/Desktop.cs'), str(ROOT / 'scripts/WindowChrome.cs')], check=True)
    subprocess.run([str(compiler), '/nologo', '/optimize+', '/platform:x64', '/target:winexe',
                    '/win32icon:' + str(icon),
                    '/out:' + str(destination / 'NJUSTWiFi.exe'),
                    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll',
                    '/reference:System.Windows.Forms.dll', str(ROOT / 'scripts/Tray.cs')], check=True)


if __name__ == '__main__':
    import sys
    build(Path(sys.argv[1]).resolve())

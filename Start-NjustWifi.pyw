"""Start via Task Scheduler, then open the local UI on demand."""
from pathlib import Path
import os
import subprocess
import sys
import time
import webbrowser
from njust_wifi.network import direct_opener

root = Path(__file__).resolve().parent
from njust_wifi import __version__
desktop = root / "dist" / ("NJUSTWiFi-" + __version__) / "NJUSTWiFi.exe"
if desktop.exists():
    os.startfile(str(desktop))
    raise SystemExit(0)
powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
command = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(root / "scripts/startup.ps1")]
executable = Path(sys.executable).with_name("pythonw.exe")
# Manual launcher can host a background instance without enabling logon startup.
# Task registration is idempotent; its logon trigger follows the user's setting.
from njust_wifi.storage import Store
store = Store()
options = ["-Mode", "Install", "-Executable", str(executable), "-ProjectRoot", str(root)]
if not store.settings["startup_enabled"]:
    options.append("-NoLogon")
result = subprocess.run(command + options,
                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
if result.returncode:
    raise SystemExit(2)
subprocess.run(command + ["-Mode", "Start"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
for _ in range(40):
    try:
        with direct_opener().open("http://127.0.0.1:18089/api/health", timeout=1) as response:
            if response.status == 200:
                webbrowser.open("http://127.0.0.1:18089")
                break
    except OSError:
        time.sleep(0.25)

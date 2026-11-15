"""Build only allowlisted files; never package the development directory wholesale."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from njust_wifi import __version__
RUNTIME = ["app.py", "njust_wifi/__init__.py", "njust_wifi/boot.py", "njust_wifi/network.py", "njust_wifi/transport.py", "njust_wifi/wlan.py", "njust_wifi/desktop.py", "njust_wifi/balance.py", "njust_wifi/storage.py",
           "njust_wifi/service.py", "njust_wifi/server.py", "njust_wifi/probes.py", "njust_wifi/proxy.py", "njust_wifi/client_control.py", "frontend/autosave.js", "frontend/window.js", "frontend/proxy.js", "frontend/index.html",
           "frontend/styles.css", "frontend/app.js", "frontend/logo.webp", "frontend/onboarding.js", "frontend/onboarding.css", "frontend/updates.js", "frontend/proxy-guide.js", "frontend/proxy-guide.css", "frontend/guide/verge-settings.webp", "frontend/guide/verge-controller.webp", "frontend/guide/flclash-settings.webp", "frontend/guide/flclash-controller.webp", "njust_wifi/updater.py", "scripts/apply-update.ps1", "scripts/startup.ps1", "scripts/probe-task.ps1"]
SOURCE = RUNTIME + ["Start-NjustWifi.pyw", "scripts/boot-startup.ps1", "scripts/stop.ps1", "scripts/build-release.py", "scripts/build-desktop.py", "scripts/Desktop.cs", "scripts/WindowChrome.cs", "scripts/Tray.cs", "scripts/ProxyClient.cs", "README.md", "AGENTS.md", "requirements.txt", ".gitignore",
                    "tests/test_app.py", "tests/test_boot.py", "tests/test_guards.py", "tests/test_balance.py", "tests/test_fallbacks.py", "tests/test_transport.py", "tests/test_radio.py", "tests/test_updater.py", "tests/test_proxy.py", "tests/test_proxy_repair.py", "tests/test_audit.py", "tests/test_preferences.py",
                    "assets/NJUSTWiFi.ico", "assets/logo-prompt.txt", "assets/logo-generation.json", "assets/njust-logo.png", "assets/njust-logo.svg", ".github/workflows/release.yml"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="Build separately when the previous portable directory is running")
    args = parser.parse_args()
    dist = args.output_dir.resolve() if args.output_dir else ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in SOURCE:
        if not (ROOT / name).is_file():
            raise SystemExit("Missing required release file: " + name)
    source = dist / f"NJUSTWiFi-{__version__}-source.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in SOURCE:
            bundle.write(ROOT / name, "NJUSTWiFi/" + name)
    outputs = [source]
    if args.exe:
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
                   "--optimize", "2", "--name", "NJUSTWiFiBackend", "--icon", str(ROOT / 'assets/NJUSTWiFi.ico'), "--distpath", str(ROOT / '.tmp/frozen'), "--workpath", str(ROOT / ".tmp/build"),
                   "--specpath", str(ROOT / ".tmp/build"),
                   "--add-data", str(ROOT / "frontend") + ";frontend",
                   "--add-data", str(ROOT / "scripts/startup.ps1") + ";scripts",
                   "--add-data", str(ROOT / "scripts/apply-update.ps1") + ";scripts",
                   "--add-data", str(ROOT / "scripts/probe-task.ps1") + ";scripts"]
        for module in ("tkinter", "unittest", "test", "pydoc", "idlelib", "pip", "setuptools",
                       "_bz2", "_lzma", "_zstd", "bz2", "lzma", "compression.zstd"):
            command += ["--exclude-module", module]
        command += [str(ROOT / "app.py")]
        subprocess.run(command, cwd=ROOT, check=True)
        portable = dist / ('NJUSTWiFi-' + __version__)
        shutil.copytree(ROOT / '.tmp/frozen/NJUSTWiFiBackend', portable, dirs_exist_ok=True)
        subprocess.run([sys.executable, str(ROOT / 'scripts/build-desktop.py'), str(portable)], check=True)
        shutil.copy2(ROOT / 'README.md', portable / 'README.md')
        shutil.copy2(ROOT / 'scripts/boot-startup.ps1', portable / 'Boot-Network.ps1')
        release = dist / f"NJUSTWiFi-{__version__}-Windows-x64.zip"
        with zipfile.ZipFile(release, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for file in sorted(portable.rglob("*")):
                if file.is_file():
                    bundle.write(file, 'NJUSTWiFi/' + file.relative_to(portable).as_posix())
        outputs.append(release)
    metadata = []
    for path in sorted(dist.glob(f"NJUSTWiFi-{__version__}-*.zip")):
        metadata.append({"file": path.name, "bytes": path.stat().st_size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (dist / "SHA256.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()

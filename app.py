"""Entry point for source and dependency-free frozen desktop distribution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import webbrowser

from njust_wifi.network import direct_opener
from njust_wifi.server import Server
from njust_wifi.service import Service
from njust_wifi.storage import Store

PORT = 18089
URL = f"http://127.0.0.1:{PORT}"


def check_existing(url):
    import time
    opener = direct_opener()
    with opener.open(url + "/api/session", timeout=3) as response:
        token = json.load(response)["token"]
    headers = {"X-NJUST-Token": token, "Content-Type": "application/json"}
    def status():
        with opener.open(urllib.request.Request(url + "/api/status", headers=headers), timeout=3) as response:
            return json.load(response)
    previous = status()
    if not previous["busy"]:
        with opener.open(urllib.request.Request(url + "/api/check", data=b"{}", headers=headers), timeout=3) as response:
            json.load(response)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = status()
        if not result["busy"] and result["checked_at"] != previous["checked_at"]:
            print(result["message"])
            return 0 if result["state"] == "online" else 1
        time.sleep(0.2)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="NJUST WiFi — 本地自动登录")
    parser.add_argument("--serve", action="store_true", help="仅启动后台，不打开浏览器")
    parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--boot-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--stop-background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--nogui", action="store_true", help="兼容旧版后台启动")
    parser.add_argument("--once", action="store_true", help="仅检测并恢复一次")
    parser.add_argument("--interval", type=float, help="本次检测间隔（分钟）")
    parser.add_argument("--connectivity-checks", help="本次自定义联网探测")
    parser.add_argument("--headed", action="store_true", help="认证时打开系统浏览器门户")
    parser.add_argument("--from-startup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--demo", action="store_true", help="使用独立演示数据，不操作实际网络")
    parser.add_argument("--data-dir", type=Path, help="测试用独立数据目录")
    parser.add_argument("--port", type=int, default=PORT, help="仅供隔离测试；正式固定 18089")
    args = parser.parse_args()
    if args.boot_manifest:
        from njust_wifi.boot import main as boot_main
        return boot_main(args.boot_manifest)
    args.serve = args.serve or args.nogui or args.from_startup
    url = f"http://127.0.0.1:{args.port}"
    if args.demo and args.data_dir is None:
        parser.error("--demo 需要指定独立 --data-dir")
    if args.bootstrap or args.stop_background:
        from njust_wifi.desktop import bootstrap, stop_background
        try:
            if args.stop_background:
                stop_background(args)
            else:
                bootstrap(args, Store(args.data_dir))
            return 0
        except (RuntimeError, ValueError, OSError, __import__('subprocess').SubprocessError):
            return 2
    # Open an existing instance; a port conflict never chooses another port.
    try:
        if args.once and args.demo:
            raise OSError()
        with direct_opener().open(url + "/api/health", timeout=2) as response:
            if json.load(response).get("app") == "NJUSTWiFi":
                if args.once:
                    return check_existing(url)
                if not args.serve:
                    webbrowser.open(url)
                return 0
    except (OSError, ValueError, urllib.error.URLError):
        pass
    store = Store(args.data_dir)
    overrides = {}
    if args.interval is not None:
        overrides["interval_minutes"] = args.interval
    if args.connectivity_checks is not None:
        overrides["connectivity_checks"] = args.connectivity_checks
    if args.headed:
        overrides["show_portal"] = True
    if overrides:
        from njust_wifi.storage import validate_settings
        store.settings = validate_settings(store.settings | overrides)
    service = Service(store, demo=args.demo)
    if args.once:
        service.start(once=True)
        service.wake.wait(0.05)
        import time
        deadline = time.monotonic() + 600
        while (service.checked_at is None or service.busy) and time.monotonic() < deadline:
            time.sleep(0.05)
        ok = service.state == "online"
        print(service.message)
        service.close()
        return 0 if ok else 1
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "frontend"
    try:
        server = Server(service, assets, args.port)
    except OSError:
        service.close()
        print(f"端口 {args.port} 已被占用。", file=sys.stderr)
        return 2
    try:
        if not args.serve:
            webbrowser.open(url)
        if service.store.settings["auto_start_monitor"]:
            campus = service.store.settings["ssid"] and service.store.username and service.store.password
            if campus or service.store.settings["fallback_networks"]:
                service.start()
        server.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

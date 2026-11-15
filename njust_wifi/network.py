"""Bounded HTTP probes and portal authentication without a browser runtime."""
from __future__ import annotations

import ctypes
import http.cookiejar
import json
import locale
import os
import subprocess
from pathlib import Path
import urllib.error
import urllib.request
import urllib.parse
import webbrowser
from .wlan import install_profile, restore_wifi_radio
from .transport import AdaptiveOpener, DirectPathUnavailable

PORTAL = "http://m.njust.edu.cn"
PROBES = (("http://www.msftconnecttest.com/connecttest.txt", 200, b"Microsoft Connect Test"),
          ("http://connect.rom.miui.com/generate_204", 204, b""))


def parse_checks(raw: str) -> list[tuple[str, int | None, bytes]]:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ValueError("请填写联网检测地址")
    checks = []
    for item in raw.split(";"):
        parts = item.strip().split("|", 2)
        if len(parts) != 3:
            raise ValueError("检测地址格式应为 地址|状态码|匹配内容")
        url, code, expected = parts
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("检测地址应为 HTTP 或 HTTPS 地址")
        if code and (not code.isdigit() or not 100 <= int(code) <= 599):
            raise ValueError("检测状态码格式不正确")
        if not code and not expected:
            raise ValueError("检测地址需指定状态码或匹配内容")
        checks.append((url, int(code) if code else None, expected.encode("utf-8")))
    if len(checks) > 8:
        raise ValueError("最多设置 8 个检测地址")
    return checks


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def automatic_opener(cancel=None):
    return AdaptiveOpener(direct_opener(), cancel)


def decode_console(value: bytes) -> str:
    encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}" if os.name == "nt" else locale.getpreferredencoding(False)
    return value.decode(encoding, "replace")


class Network:
    def __init__(self, cancel, *, all_users=False):
        self.cancel = cancel
        self.all_users = all_users
        self.opener = automatic_opener(cancel)
        self.path_uncertain = False
        self.checks = PROBES
        self.timeout = 5

    def active(self):
        if self.cancel.is_set():
            raise InterruptedError()

    def online(self) -> bool:
        self.path_uncertain = False
        for attempt in range(2):
            for url, expected, text in self.checks:
                self.active()
                try:
                    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "NJUSTWiFi/3"})
                    try:
                        response = self.opener.open(request, timeout=self.timeout)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        if (expected is None or response.status == expected) and (not text or text in response.read(4096)):
                            self.path_uncertain = False
                            return True
                except DirectPathUnavailable:
                    self.path_uncertain = True
                except InterruptedError:
                    raise
                except (OSError, urllib.error.URLError):
                    pass
            if attempt or not isinstance(self.opener, AdaptiveOpener) or self.opener.compatibility_active or not self.opener.try_wifi_path():
                break
        return False

    def campus_session(self):
        """Read-only evidence; never log the returned account or response body."""
        self.active()
        try:
            with self.opener.open(PORTAL + '/api/selfservice/v1/userinfo', timeout=4) as response:
                data = response.read(65537)
            if len(data) > 65536:
                return None
            reply = json.loads(data)
            if not isinstance(reply, dict) or type(reply.get('reply_code')) is not int:
                return None
            if reply['reply_code'] != 0:
                return None
            result = reply.get('results')
            return True if isinstance(result, dict) and isinstance(result.get('username'), str) and result['username'] else None
        except urllib.error.HTTPError as error:
            result = False if error.code in (401, 403) else None
            error.close()
            return result
        except InterruptedError:
            raise
        except (OSError, ValueError, urllib.error.URLError, DirectPathUnavailable):
            return None

    def preserve_uncertain_link(self, campus: bool):
        if not isinstance(self.opener, AdaptiveOpener) or not self.opener.compatibility_active:
            return
        if campus:
            session = self.campus_session()
            if session is True:
                raise DirectPathUnavailable('校园网已有认证，外网检测暂不可用，已保留当前 WiFi')
            if session is False:
                return
        if self.path_uncertain:
            raise DirectPathUnavailable()

    def netsh(self, *args: str) -> str:
        self.active()
        if os.name != "nt":
            raise RuntimeError("Wi-Fi 管理需要 Windows")
        try:
            result = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Wi-Fi 操作超时") from None
        self.active()
        if result.returncode:
            # netsh output may contain an SSID, username or local path.
            raise RuntimeError("Wi-Fi 操作失败，请检查系统网络配置")
        return decode_console(result.stdout)

    def current_ssid(self) -> str:
        output = self.netsh("show", "interfaces")
        for line in output.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().casefold() == "ssid":
                return value.strip()
        return ""

    def connect(self, ssid: str, *, create_open: bool = False, password: str = "") -> None:
        self.active()
        if password:
            if self.all_users:
                install_profile(ssid, password, all_users=True)
            else:
                install_profile(ssid, password)
        try:
            self.netsh("connect", f"name={ssid}", f"ssid={ssid}")
        except RuntimeError:
            if not create_open:
                raise
            if self.all_users:
                install_profile(ssid, all_users=True)
            else:
                install_profile(ssid)
            self.netsh("connect", f"name={ssid}", f"ssid={ssid}")
        if self.cancel.wait(6):
            raise InterruptedError()
        if self.current_ssid().casefold() != ssid.casefold():
            raise RuntimeError("目标 Wi-Fi 尚未连接")
        if isinstance(self.opener, AdaptiveOpener):
            self.opener.invalidate()

    def login(self, username: str, password: str, domain: str) -> None:
        self.active()
        # Limit credential submission to the fixed campus host, never follow redirects.
        payload = json.dumps({"domain": domain, "username": username, "password": password}).encode()
        request = urllib.request.Request(PORTAL + "/api/portal/v1/login", data=payload, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": PORTAL, "Referer": PORTAL + "/portal/index.html",
            "User-Agent": "NJUSTWiFi/2", "X-Requested-With": "XMLHttpRequest"})
        try:
            with self.opener.open(request, timeout=15) as response:
                reply = json.loads(response.read(65536))
        except InterruptedError:
            raise
        except (OSError, ValueError, urllib.error.URLError):
            raise RuntimeError("校园认证接口暂不可用，请稍后重试") from None
        self.active()
        if not isinstance(reply, dict) or reply.get("reply_code") != 0:
            raise RuntimeError("校园认证失败，请检查账号、密码和认证域")

    def recover(self, settings: dict, username: str, password: str, event) -> None:
        if "connectivity_checks" in settings:
            self.checks = parse_checks(settings["connectivity_checks"])
        self.timeout = settings.get("connectivity_timeout_ms", 5000) / 1000
        preferred = settings["ssid"] if username and password else ""
        fallbacks = settings.get("fallback_networks")
        if fallbacks is None:
            legacy = settings.get("fallback_ssid", "")
            fallbacks = [{"ssid": legacy, "password": ""}] if legacy else []
        candidates = ([{"ssid": preferred, "campus": True}] if preferred else [])
        candidates += [entry | {"campus": False} for entry in fallbacks if entry["ssid"].casefold() != preferred.casefold()]
        current = self.current_ssid()
        if candidates and not current and restore_wifi_radio(self.cancel, event):
            if isinstance(self.opener, AdaptiveOpener):
                self.opener.invalidate()
            current = self.current_ssid()
        online = self.online()
        if not online and current:
            self.preserve_uncertain_link(bool(preferred and current.casefold() == preferred.casefold()))
        names = [item["ssid"].casefold() for item in candidates]
        if online and (not names or current.casefold() == names[0] or (preferred and current.casefold() not in names)):
            return
        if not online:
            event("recovering", "检测到网络中断，正在恢复连接")
        else:
            event("recovering", "正在尝试优先网络")
        last_error = None
        for index, item in enumerate(candidates):
            self.active()
            try:
                if not item["campus"]:
                    event("recovering", f"正在尝试备用网络 {index + (0 if preferred else 1)}")
                if current.casefold() != item["ssid"].casefold():
                    if item["campus"]:
                        self.connect(item["ssid"], create_open=item["ssid"] == "NJUST")
                    else:
                        self.connect(item["ssid"], password=item["password"])
                if self.online():
                    return
                self.preserve_uncertain_link(item['campus'])
                if item["campus"]:
                    self.active()
                    event("recovering", "正在进行校园网认证")
                    if settings.get("show_portal"):
                        webbrowser.open(PORTAL + "/portal/index.html")
                    self.login(username, password, settings["domain"])
                    if self.online():
                        return
                    self.preserve_uncertain_link(True)
                    last_error = RuntimeError("认证已提交，但外网检测尚未通过")
            except DirectPathUnavailable:
                raise
            except RuntimeError as exc:
                last_error = exc
            # A failed attempt may have switched the adapter; don't use its old SSID.
            current = ""
            online = False
        raise last_error or RuntimeError("网络尚未恢复，请检查 Wi-Fi 信号")


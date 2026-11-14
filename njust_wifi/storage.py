"""Per-user JSON settings; legacy encrypted files are migrated once."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess

DEFAULTS = {"ssid": "NJUST", "fallback_networks": [], "domain": "default",
            "interval_minutes": 5, "remember_credentials": False,
            "startup_enabled": False, "auto_start_monitor": False,
            "connectivity_timeout_ms": 5000,
            "connectivity_checks": "http://www.msftconnecttest.com/connecttest.txt|200|Microsoft Connect Test;http://connect.rom.miui.com/generate_204|204|",
            "show_portal": False, "close_behavior": "ask", "proxy_fail_open": False}


def data_directory() -> Path:
    if os.name == "nt":
        directory = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, directory) == 0:
            return Path(directory.value) / "NJUSTWiFi"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "NJUSTWiFi"


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def read_legacy_credentials(path: Path) -> dict:
    """Compatibility reader only. New credentials are never encrypted."""
    if os.name != "nt":
        raise RuntimeError("旧凭据需要在原 Windows 用户下读取")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    value = path.read_bytes()
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN, user scope (never LOCAL_MACHINE).
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("旧凭据读取失败")
    try:
        return json.loads(ctypes.string_at(target.data, target.size))
    finally:
        kernel.LocalFree(target.data)


def validate_settings(raw: dict) -> dict:
    raw = migrate_settings(raw)
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("设置包含未知字段")
    result = DEFAULTS | raw
    for key, limit in (("ssid", 32), ("domain", 64)):
        value = result[key]
        if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
            raise ValueError("网络名称或认证域过长")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("网络名称或认证域包含控制字符")
        if key == "domain" and not value.strip():
            raise ValueError("认证域不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", result["domain"]):
        raise ValueError("认证域格式不正确")
    result["ssid"] = result["ssid"].strip()
    networks = result["fallback_networks"]
    if not isinstance(networks, list) or len(networks) > 8:
        raise ValueError("备用 WiFi 最多设置 8 个")
    result["fallback_networks"] = []
    seen = {result["ssid"].casefold()} if result["ssid"] else set()
    for item in networks:
        if not isinstance(item, dict) or set(item) != {"ssid", "password"}:
            raise ValueError("备用 WiFi 格式不正确")
        ssid, password = item["ssid"], item["password"]
        if not isinstance(ssid, str) or not isinstance(password, str):
            raise ValueError("备用 WiFi 格式不正确")
        ssid = ssid.strip()
        if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
            raise ValueError("备用 WiFi 名称应为 1 到 32 字节")
        if ssid.casefold() in seen:
            raise ValueError("WiFi 名称不能重复")
        seen.add(ssid.casefold())
        if password and not (re.fullmatch(r"[\x20-\x7e]{8,63}", password) or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
            raise ValueError("WiFi 密码应为 8 到 63 位 ASCII 字符或 64 位十六进制密钥")
        result["fallback_networks"].append({"ssid": ssid, "password": password})
    interval = result["interval_minutes"]
    if isinstance(interval, bool) or not isinstance(interval, (float, int)) or not 1 <= interval <= 120:
        raise ValueError("检测间隔应为 1 到 120 分钟")
    timeout = result["connectivity_timeout_ms"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1000 <= timeout <= 30000:
        raise ValueError("HTTP 超时应为 1000 到 30000 毫秒")
    from .network import parse_checks
    parse_checks(result["connectivity_checks"])
    for key in ("remember_credentials", "startup_enabled", "auto_start_monitor", "show_portal"):
        if not isinstance(result[key], bool):
            raise ValueError("开关设置格式不正确")
    if result["close_behavior"] not in ("ask", "tray", "exit"):
        raise ValueError("关闭行为应为询问、托盘或退出")
    if not isinstance(result["proxy_fail_open"], bool):
        raise ValueError("代理保护开关格式不正确")
    return result


def migrate_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("设置格式不正确")
    raw = raw.copy()
    if "fallback_ssid" in raw:
        old = raw.pop("fallback_ssid")
        if not isinstance(old, str):
            raise ValueError("备用 WiFi 格式不正确")
        raw.setdefault("fallback_networks", [{"ssid": old, "password": ""}] if old else [])
    return raw


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or data_directory()
        new_directory = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        if new_directory and os.name == "nt":
            account = os.environ.get("USERDOMAIN", "") + "\\" + os.environ["USERNAME"]
            result = subprocess.run(["icacls", str(self.root), "/inheritance:r", "/grant:r",
                                     account + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError("个人数据目录权限设置失败")
        self.settings_path = self.root / "settings.json"
        self.secret_path = self.root / "credentials.json"
        self.legacy_secret_path = self.root / "credentials.dpapi"
        self.settings = validate_settings({})
        self.username = ""
        self.password = ""
        self.credential_error = False
        if self.settings_path.exists():
            raw = json.loads(self.settings_path.read_text("utf-8"))
            raw.setdefault("auto_start_monitor", bool(raw.get("startup_enabled", False)))
            self.settings = validate_settings(raw)
        if self.settings["remember_credentials"] and (self.secret_path.exists() or self.legacy_secret_path.exists()):
            try:
                legacy = not self.secret_path.exists()
                secret = read_legacy_credentials(self.legacy_secret_path) if legacy else json.loads(self.secret_path.read_text("utf-8"))
                if not isinstance(secret, dict) or not all(isinstance(secret.get(k), str) for k in ("username", "password")):
                    raise ValueError("账号数据格式不正确")
                self.username, self.password = secret["username"], secret["password"]
                if legacy:
                    atomic_write(self.secret_path, json.dumps(secret, ensure_ascii=False).encode())
                    self.legacy_secret_path.unlink()
            except (ValueError, KeyError, RuntimeError, OSError):
                self.credential_error = True

    def public(self) -> dict:
        name = self.username
        return self.settings | {"username_masked": name[:2] + "***" + name[-2:] if len(name) > 4 else "***" if name else "",
                                "has_credentials": bool(name and self.password)}

    def credentials(self) -> dict:
        return {"username": self.username, "password": self.password}

    def update(self, raw: dict) -> None:
        raw = migrate_settings(raw)
        if not isinstance(raw, dict) or set(raw) - (set(DEFAULTS) | {"username", "password"}):
            raise ValueError("设置包含未知字段")
        values = validate_settings(self.settings | {k: v for k, v in raw.items() if k in DEFAULTS})
        for key in ("username", "password"):
            if key in raw and (not isinstance(raw[key], str) or len(raw[key]) > 1024):
                raise ValueError("账号或密码格式不正确")
        username = raw.get("username") or self.username
        password = raw.get("password") or self.password
        if bool(raw.get("username")) and username != self.username and not raw.get("password"):
            raise ValueError("更换账号时请同时填写密码")
        secret = None
        if values["remember_credentials"] and username and password:
            secret = json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2).encode()
        if secret is not None:
            atomic_write(self.secret_path, secret)
        else:
            self.secret_path.unlink(missing_ok=True)
        self.legacy_secret_path.unlink(missing_ok=True)
        atomic_write(self.settings_path, json.dumps(values, ensure_ascii=False, indent=2).encode())
        self.settings, self.username, self.password = values, username, password
        self.credential_error = False

    def forget(self) -> None:
        self.secret_path.unlink(missing_ok=True)
        self.legacy_secret_path.unlink(missing_ok=True)
        self.username = self.password = ""
        self.settings["remember_credentials"] = False
        atomic_write(self.settings_path, json.dumps(self.settings, indent=2).encode())


"""Create current-user Windows WiFi profiles in memory, never a password XML file."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


def profile_xml(ssid: str, password: str = "") -> str:
    if password:
        keytype = "networkKey" if len(password) == 64 else "passPhrase"
        security = ("<authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption>"
                    "<useOneX>false</useOneX></authEncryption><sharedKey><keyType>" + keytype +
                    "</keyType><protected>false</protected><keyMaterial>" + escape(password) +
                    "</keyMaterial></sharedKey>")
    else:
        security = "<authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption>"
    return ('<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
            '<name>' + escape(ssid) + '</name><SSIDConfig><SSID><hex>' + ssid.encode('utf-8').hex() +
            '</hex><name>' + escape(ssid) + '</name></SSID></SSIDConfig><connectionType>ESS</connectionType>'
            '<connectionMode>manual</connectionMode><MSM><security>' + security + '</security></MSM></WLANProfile>')


class Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD),
                ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]


class Interface(ctypes.Structure):
    _fields_ = [("guid", Guid), ("description", wintypes.WCHAR * 256), ("state", wintypes.DWORD)]


class PhyRadioState(ctypes.Structure):
    _fields_ = [('index', wintypes.DWORD), ('software', wintypes.DWORD), ('hardware', wintypes.DWORD)]


class RadioController:
    """Short-lived WLAN API session; callers never change an adapter or route."""
    def __enter__(self):
        try:
            self.api = ctypes.WinDLL('wlanapi', use_last_error=True)
            api = self.api
            api.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.HANDLE)]
            api.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            api.WlanQueryInterface.argtypes = [wintypes.HANDLE, ctypes.POINTER(Guid), wintypes.DWORD, ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
            api.WlanSetInterface.argtypes = [wintypes.HANDLE, ctypes.POINTER(Guid), wintypes.DWORD,
                                            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
            api.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            api.WlanFreeMemory.argtypes = [ctypes.c_void_p]
            for method in ('WlanOpenHandle', 'WlanEnumInterfaces', 'WlanQueryInterface', 'WlanSetInterface', 'WlanCloseHandle'):
                getattr(api, method).restype = wintypes.DWORD
            api.WlanFreeMemory.restype = None
            self.handle, version = wintypes.HANDLE(), wintypes.DWORD()
            if api.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(self.handle)):
                raise RuntimeError('WiFi 开关读取失败，请检查 Windows 无线网络服务')
            return self
        except OSError:
            raise RuntimeError('WiFi 开关读取失败，请检查 Windows 无线网络服务') from None

    def __exit__(self, *args):
        self.api.WlanCloseHandle(self.handle, None)

    def states(self):
        interfaces = ctypes.c_void_p()
        rows = []
        try:
            if self.api.WlanEnumInterfaces(self.handle, None, ctypes.byref(interfaces)) or not interfaces.value:
                raise RuntimeError('WiFi 开关读取失败，请稍后重试')
            count = ctypes.cast(interfaces, ctypes.POINTER(wintypes.DWORD))[0]
            if not 0 <= count <= 64:
                raise RuntimeError('WiFi 开关状态暂不可确认，请稍后重试')
            for item in (Interface * count).from_address(interfaces.value + 8):
                data, size, kind = ctypes.c_void_p(), wintypes.DWORD(), wintypes.DWORD()
                try:
                    # wlan_intf_opcode_radio_state = 4. Query returns WLAN_RADIO_STATE.
                    status = self.api.WlanQueryInterface(self.handle, ctypes.byref(item.guid), 4, None,
                                                        ctypes.byref(size), ctypes.byref(data), ctypes.byref(kind))
                    if status or not data.value or size.value < 4:
                        continue
                    phys = ctypes.cast(data, ctypes.POINTER(wintypes.DWORD))[0]
                    if not 1 <= phys <= 64 or size.value < 4 + phys * ctypes.sizeof(PhyRadioState):
                        continue
                    guid = Guid.from_buffer_copy(bytes(item.guid))
                    for state in (PhyRadioState * phys).from_address(data.value + 4):
                        rows.append((guid, state.index, state.software, state.hardware))
                finally:
                    if data.value:
                        self.api.WlanFreeMemory(data)
            if count and not rows:
                raise RuntimeError('WiFi 开关状态暂不可确认，请稍后重试')
            return rows
        finally:
            if interfaces.value:
                self.api.WlanFreeMemory(interfaces)

    def enable(self, guid, index):
        # Set accepts a single WLAN_PHY_RADIO_STATE; hardware state is ignored.
        state = PhyRadioState(index, 1, 0)
        if self.api.WlanSetInterface(self.handle, ctypes.byref(guid), 4, ctypes.sizeof(state), ctypes.byref(state), None):
            raise RuntimeError('WiFi 自动开启未成功，请检查系统权限或开关状态')


def restore_wifi_radio(cancel, event):
    """Enable a software-disabled radio only when no working WiFi radio exists."""
    def active():
        if cancel.is_set():
            raise InterruptedError()

    active()
    if os.name != 'nt':
        return False
    try:
        with RadioController() as radio:
            states = radio.states()
            active()
            if any(software == hardware == 1 for _, _, software, hardware in states):
                return False
            available = [row for row in states if row[2] == 2 and row[3] == 1]
            if not available:
                if any(row[3] == 2 for row in states):
                    raise RuntimeError('WiFi 硬件开关已关闭，请开启后重试')
                if not states:
                    raise RuntimeError('未找到可用 WiFi 网卡，请检查网卡是否被禁用')
                raise RuntimeError('WiFi 开关状态暂不可确认，请稍后重试')
            target = bytes(available[0][0])
            selected = [row for row in available if bytes(row[0]) == target]
            event('recovering', 'WiFi 已关闭，正在尝试开启')
            for guid, index, _, _ in selected:
                active()
                radio.enable(guid, index)
            deadline = time.monotonic() + 4
            wanted = {row[1] for row in selected}
            while True:
                active()
                on = {index for guid, index, software, hardware in radio.states()
                      if bytes(guid) == target and software == hardware == 1}
                if wanted <= on:
                    active()
                    event('recovering', 'WiFi 已开启，正在恢复连接')
                    return True
                if time.monotonic() >= deadline:
                    raise RuntimeError('WiFi 开关尚未开启，将稍后重试')
                if cancel.wait(.25):
                    raise InterruptedError()
    except InterruptedError:
        raise
    except OSError:
        raise RuntimeError('WiFi 自动开启未成功，请检查系统权限或开关状态') from None


def profile_password(xml: str, ssid: str) -> str:
    """Accept only plaintext keys belonging to the explicitly configured SSID."""
    try:
        root = ET.fromstring(xml)
        ns = {'w': 'http://www.microsoft.com/networking/WLAN/profile/v1'}
        name = root.findtext('w:SSIDConfig/w:SSID/w:name', namespaces=ns)
        if name is None:
            name = bytes.fromhex(root.findtext('w:SSIDConfig/w:SSID/w:hex', '', ns)).decode('utf-8')
        key = root.find('w:MSM/w:security/w:sharedKey', ns)
        if name != ssid or key is None or key.findtext('w:protected', '', ns).lower() != 'false':
            return ''
        return key.findtext('w:keyMaterial', '', ns)
    except (ET.ParseError, ValueError, UnicodeError):
        return ''


def read_profile_passwords(ssids: list[str]) -> dict[str, str]:
    """Read only named fallback profiles in memory; never export XML or log keys."""
    if os.name != 'nt' or not ssids:
        return {}
    result = {}
    try:
        wlan = ctypes.WinDLL('wlanapi', use_last_error=True)
        wlan.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.HANDLE)]
        wlan.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        wlan.WlanGetProfile.argtypes = [wintypes.HANDLE, ctypes.POINTER(Guid), wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        handle, interfaces, version = wintypes.HANDLE(), ctypes.c_void_p(), wintypes.DWORD()
        if wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)):
            return result
        try:
            if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)) or not interfaces.value:
                return result
            count = ctypes.cast(interfaces, ctypes.POINTER(wintypes.DWORD))[0]
            if not 1 <= count <= 64:
                return result
            for item in (Interface * count).from_address(interfaces.value + 8):
                for ssid in dict.fromkeys(ssids[:8]):
                    if ssid in result:
                        continue
                    xml, flags, access = ctypes.c_void_p(), wintypes.DWORD(4), wintypes.DWORD()
                    try:
                        # WLAN_PROFILE_GET_PLAINTEXT_KEY; Windows applies current-user permissions.
                        status = wlan.WlanGetProfile(handle, ctypes.byref(item.guid), ssid, None,
                                                     ctypes.byref(xml), ctypes.byref(flags), ctypes.byref(access))
                        if not status and xml.value:
                            key = profile_password(ctypes.wstring_at(xml), ssid)
                            if key:
                                result[ssid] = key
                    finally:
                        if xml.value:
                            wlan.WlanFreeMemory(xml)
        finally:
            if interfaces.value:
                wlan.WlanFreeMemory(interfaces)
            wlan.WlanCloseHandle(handle, None)
    except OSError:
        pass
    return result


def install_profile(ssid: str, password: str = "", *, all_users=False):
    if os.name != 'nt':
        raise RuntimeError("Wi-Fi 管理需要 Windows")
    wlan = ctypes.WinDLL('wlanapi', use_last_error=True)
    wlan.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.HANDLE)]
    wlan.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    wlan.WlanSetProfile.argtypes = [wintypes.HANDLE, ctypes.POINTER(Guid), wintypes.DWORD,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    handle = wintypes.HANDLE()
    version, reason = wintypes.DWORD(), wintypes.DWORD()
    interfaces = ctypes.c_void_p()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)):
        raise RuntimeError("Wi-Fi 配置服务暂不可用")
    try:
        if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)) or not interfaces.value:
            raise RuntimeError("Wi-Fi 网卡读取失败")
        count = ctypes.cast(interfaces, ctypes.POINTER(wintypes.DWORD))[0]
        if not 1 <= count <= 64:
            raise RuntimeError("未找到 Wi-Fi 网卡")
        items = (Interface * count).from_address(interfaces.value + 8)
        item = next((i for i in items if i.state == 1), items[0])
        # Boot SYSTEM profiles must be available outside a user's logon session.
        flags = 0 if all_users else 2  # all-user profile / WLAN_PROFILE_USER
        if wlan.WlanSetProfile(handle, ctypes.byref(item.guid), flags, profile_xml(ssid, password), None, True, None, ctypes.byref(reason)):
            raise RuntimeError("Wi-Fi 密码配置失败，请检查网络加密类型")
    finally:
        if interfaces.value:
            wlan.WlanFreeMemory(interfaces)
        wlan.WlanCloseHandle(handle, None)

"""Optional migrated application guards, local-user configuration only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import csv
import io
import sys
import unicodedata
from functools import lru_cache
from .storage import atomic_write
from .network import decode_console


class ApplicationGuards:
    @staticmethod
    @lru_cache(maxsize=64)
    def name(path):
        # One bounded native metadata read per executable, no subprocess per poll.
        name = path.stem
        if os.name == 'nt':
            try:
                import ctypes
                api = ctypes.WinDLL('version', use_last_error=True)
                api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
                api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
                api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
                size = api.GetFileVersionInfoSizeW(str(path), None)
                if 0 < size <= 1024 * 1024:
                    buffer = ctypes.create_string_buffer(size)
                    if api.GetFileVersionInfoW(str(path), 0, size, buffer):
                        address, length = ctypes.c_void_p(), ctypes.c_uint()
                        if api.VerQueryValueW(buffer, r'\VarFileInfo\Translation', ctypes.byref(address), ctypes.byref(length)) and length.value >= 4:
                            translation = ctypes.cast(address, ctypes.POINTER(ctypes.c_ushort))
                            prefix = f'\\StringFileInfo\\{translation[0]:04x}{translation[1]:04x}\\'
                            for field in ('ProductName','FileDescription'):
                                if api.VerQueryValueW(buffer, prefix + field, ctypes.byref(address), ctypes.byref(length)) and 1 < length.value < 256:
                                    value = ctypes.wstring_at(address, length.value).rstrip('\0').strip()
                                    if value and not any(c in value for c in '/\\'):
                                        name = value
                                        break
            except (OSError, ValueError):
                pass
        return ''.join(c for c in name if not unicodedata.category(c).startswith('C'))[:80] or '未命名程序'

    def __init__(self, root):
        self.root = root
        self.paths = []
        self.states = {}
        self.elevated = {}
        path = root / "private-probes.json"
        if path.exists():
            raw = json.loads(path.read_text("utf-8"))
            if not isinstance(raw, list):
                raise ValueError("本地程序守护配置格式不正确")
            self.paths = [Path(item) for item in raw[:20] if isinstance(item, str) and Path(item).is_absolute() and Path(item).suffix.lower() == ".exe"]

    @staticmethod
    def key(path):
        return os.path.normcase(os.path.abspath(str(path)))

    @classmethod
    def identifier(cls, path):
        return hashlib.sha1(cls.key(path).encode()).hexdigest()[:12]

    @classmethod
    def task_name(cls, path):
        return "NJUST-WiFi-Probe-" + cls.identifier(path)

    @staticmethod
    def run(command, timeout=15):
        return subprocess.run(command, capture_output=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def task_exists(self, path):
        try:
            return os.name == "nt" and self.run(["schtasks.exe", "/Query", "/TN", self.task_name(path)]).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def items(self):
        result = []
        for path in self.paths:
            key = self.key(path)
            if key not in self.elevated:
                self.elevated[key] = self.task_exists(path)
            result.append({"id": self.identifier(path), "name": self.name(path), "path": str(path),
                           "state": self.states.get(str(path), "unknown"), "elevated": self.elevated[key]})
        return result

    def save(self):
        atomic_write(self.root / "private-probes.json", json.dumps([str(path) for path in self.paths], ensure_ascii=False, indent=2).encode())

    def add(self, paths):
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("请选择程序")
        selected = []
        known = {self.key(path) for path in self.paths}
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute() or path.suffix.lower() != ".exe" or not path.is_file():
                raise ValueError("请选择有效的 EXE 文件")
            key = self.key(path)
            if key not in known:
                selected.append(path)
                known.add(key)
        if len(self.paths) + len(selected) > 20:
            raise ValueError("最多守护 20 个程序")
        self.paths.extend(selected)
        self.save()

    def selected(self, ids):
        if not isinstance(ids, list) or not ids or not all(isinstance(value, str) for value in ids):
            raise ValueError("请选择程序")
        found = [path for path in self.paths if self.identifier(path) in ids]
        if len(found) != len(set(ids)):
            raise ValueError("程序列表已更新，请重新选择")
        return found

    def remove(self, ids):
        paths = self.selected(ids)
        for path in paths:
            if self.task_exists(path):
                result = self.run(["schtasks.exe", "/Delete", "/TN", self.task_name(path), "/F"])
                if result.returncode:
                    self.configure_task(path, remove=True)
        excluded = {self.key(path) for path in paths}
        self.paths = [path for path in self.paths if self.key(path) not in excluded]
        self.save()

    def configure_task(self, path, *, remove=False):
        if os.name != "nt":
            raise ValueError("程序管理需要 Windows")
        script = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "scripts/probe-task.ps1"
        executable = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        try:
            result = self.run([executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                               "-Mode", "Remove" if remove else "Register", "-Executable", str(path), "-TaskName", self.task_name(path)], timeout=110)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("管理员设置未完成") from None
        if result.returncode:
            raise ValueError("管理员设置已取消或未完成")
        self.elevated[self.key(path)] = not remove

    @staticmethod
    def powershell(code, timeout=20):
        if os.name != "nt":
            raise ValueError("程序管理需要 Windows")
        executable = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        result = ApplicationGuards.run([executable, "-NoProfile", "-STA", "-Command", code], timeout=timeout)
        if result.returncode:
            raise ValueError("程序列表读取未完成")
        try:
            return json.loads(result.stdout.decode("utf-8-sig") or "[]")
        except ValueError:
            raise ValueError("程序列表读取未完成") from None

    @classmethod
    def candidates(cls):
        raw = cls.powershell("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        rows = [raw] if isinstance(raw, dict) else raw
        found = {}
        for row in rows or []:
            path = Path(os.path.expandvars(row.get("AppID", "")))
            label = (row.get("Name", "") + " " + path.name).casefold()
            if path.is_absolute() and path.suffix.lower() == ".exe" and path.is_file() and not any(word in label for word in ("uninstall", "unins", "setup", "update", "repair")):
                found[cls.key(path)] = {"name": row.get("Name") or path.stem, "path": str(path)}
        return sorted(found.values(), key=lambda item: item["name"].casefold())

    @classmethod
    def browse(cls):
        code = """[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Add-Type -AssemblyName System.Windows.Forms; $d=New-Object Windows.Forms.OpenFileDialog; $d.Filter='Applications (*.exe)|*.exe'; $d.Multiselect=$true; $d.Title='选择守护程序'; try {if($d.ShowDialog() -eq 'OK') {ConvertTo-Json -InputObject @($d.FileNames) -Compress} else {'[]'}} finally {$d.Dispose()}"""
        raw = cls.powershell(code, timeout=120)
        paths = [raw] if isinstance(raw, str) else raw
        return [{"name": Path(path).stem, "path": path} for path in paths]

    def check(self, cancel, event, health=None):
        if not self.paths or os.name != "nt":
            return
        for path in self.paths:
            if cancel.is_set():
                raise InterruptedError()
            state = "error"
            key = 'guard:' + self.identifier(path)
            label = self.name(path)
            try:
                result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {path.name}", "/FO", "CSV", "/NH"],
                                        capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                # Only compare executable names; neither output nor paths enter logs.
                names = [row[0].casefold() for row in csv.reader(io.StringIO(decode_console(result.stdout))) if row]
                if path.name.casefold() in names and result.returncode == 0:
                    state = "running"
                elif result.returncode == 0 and path.is_file():
                    if health:
                        health(key, '', False)
                    event('idle', label + ' 未运行，正在启动')
                    normalized = os.path.normcase(os.path.abspath(str(path)))
                    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
                    task = "NJUST-WiFi-Probe-" + digest
                    query = subprocess.run(["schtasks.exe", "/Query", "/TN", task], capture_output=True, timeout=10,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                    if cancel.is_set():
                        raise InterruptedError()
                    if query.returncode == 0:
                        started = subprocess.run(["schtasks.exe", "/Run", "/TN", task], capture_output=True, timeout=10,
                                                 creationflags=subprocess.CREATE_NO_WINDOW)
                        state = "started" if started.returncode == 0 else "error"
                    else:
                        subprocess.Popen([str(path)], cwd=path.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                        state = "started"
            except (OSError, subprocess.TimeoutExpired):
                pass
            if health:
                health(key, label + ' 运行正常', state == 'running')
            if state == "started":
                event("idle", label + ' 已请求启动')
            elif state == "error":
                event("error", label + ' 守护未完成，请检查本机配置')
            self.states[str(path)] = state

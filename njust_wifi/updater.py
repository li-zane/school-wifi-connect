"""On-demand public GitHub releases; verified staging, independent installer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import threading
import urllib.parse
import urllib.error
import urllib.request
import uuid
import zipfile

from . import __version__
REPOSITORY = 'li-zane/school-wifi-connect'
RELEASE_API = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_EXPANDED = 350 * 1024 * 1024


def version(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match or len(str(value)) > 40:
        raise ValueError('发布版本格式不正确')
    return tuple(map(int, match.groups()))


def asset_url(value, tag, name):
    expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
    if value != expected:
        raise ValueError('发布文件地址不匹配')
    return value


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.hostname not in ('api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
            raise ValueError('发布下载跳转地址不匹配')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_release(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'school-wifi-connect/' + __version__, 'Accept': 'application/vnd.github+json' if url == RELEASE_API else 'application/octet-stream'})
    # GitHub may require the user's working proxy. Fall back to direct requests.
    try:
        return urllib.request.build_opener(ReleaseRedirect()).open(request, timeout=20)
    except (OSError, urllib.error.URLError):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirect()).open(request, timeout=20)


def read_json(url, limit=1024 * 1024):
    with open_release(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('发布信息过大')
    return json.loads(data)


def extract_verified(archive: Path, stage: Path):
    """No traversal, links, duplicate names, local data or ZIP bombs."""
    if stage.exists():
        raise ValueError('更新暂存目录已存在')
    stage.mkdir()
    manifest = {}
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > 2000 or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise ValueError('更新包展开体积异常')
            seen = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if ('\\' in entry.orig_filename or '\x00' in entry.orig_filename or ':' in entry.filename or path.is_absolute() or
                        not path.parts or path.parts[0] != 'NJUSTWiFi' or
                        any(p in ('..', '.', '') or p.endswith((' ', '.')) for p in path.parts) or
                        stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError('更新包路径不正确')
                relative = Path(*path.parts[1:])
                if not relative.parts:
                    if entry.is_dir(): continue
                    raise ValueError('更新包目录不正确')
                key = relative.as_posix().casefold()
                if key in seen: raise ValueError('更新包包含重复文件')
                seen.add(key)
                if any(p.casefold() in ('settings.json', 'credentials.json', 'credentials.dpapi', 'private-probes.json', '.env', '.git') for p in relative.parts):
                    raise ValueError('更新包包含个人配置')
                target = stage / relative
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with bundle.open(entry) as source, target.open('xb') as dest:
                    while block := source.read(65536):
                        dest.write(block); digest.update(block)
                manifest[relative.as_posix()] = digest.hexdigest()
        required = {'NJUSTWiFi.exe', 'NJUSTWiFiWindow.exe', 'NJUSTWiFiBackend.exe', 'NJUSTWiFi.ico', '_internal/frontend/index.html'}
        if not required <= manifest.keys():
            raise ValueError('更新包缺少桌面程序文件')
        return manifest
    except BaseException:
        shutil.rmtree(stage)
        raise


class UpdateManager:
    def __init__(self, data_root: Path, *, demo=False):
        self.root = data_root / 'updates'
        self.install_root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else None
        self.demo = demo
        self.lock = threading.Lock()
        self.state = {'state': 'idle', 'current_version': __version__, 'latest_version': None,
                      'progress': 0, 'message': '点击检查新版本', 'repository': REPOSITORY,
                      'can_install': bool(self.install_root and not demo)}
        self.release = None
        self.stage = None
        self.manifest = None

    def status(self):
        with self.lock:
            state = self.state.copy()
        try:
            result = json.loads((self.root.parent / 'update-result.json').read_text('utf-8-sig'))
            if result.get('state') in ('updated', 'rolled_back', 'error'):
                state['last_result'] = {'state': result['state'], 'message': result.get('message', '')}
        except (OSError, ValueError, AttributeError):
            pass
        return state

    def set(self, **values):
        with self.lock: self.state.update(values)

    def start(self, operation):
        with self.lock:
            if self.state['state'] in ('checking', 'downloading', 'installing'):
                raise ValueError('更新正在进行')
            if operation == 'download' and self.state['state'] != 'available':
                raise ValueError('请先检查新版本')
            self.state.update(state='checking' if operation == 'check' else 'downloading', progress=0, message='正在检查发布版本' if operation == 'check' else '正在下载更新')
        threading.Thread(target=self._run, args=(operation,), name='release-update', daemon=True).start()

    def _run(self, operation):
        try:
            self.check() if operation == 'check' else self.download()
        except Exception as exc:
            # No raw URL, local path, response body or proxy credentials in UI.
            message = str(exc) if isinstance(exc, ValueError) else '连接 GitHub 或处理更新失败，请稍后重试'
            self.set(state='error', message=message, progress=0)

    def check(self):
        release = read_json(RELEASE_API)
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            raise ValueError('尚无可用的正式版本')
        tag = release.get('tag_name', '')
        latest = version(tag)
        self.release = None
        if latest <= version(__version__):
            self.set(state='current', latest_version=tag.removeprefix('v'), message='已是最新版本')
            return
        name = f'NJUSTWiFi-{tag.removeprefix("v")}-Windows-x64.zip'
        assets = release.get('assets', [])
        if not isinstance(assets, list): raise ValueError('发布文件列表不正确')
        selected = {a.get('name'): a for a in assets if isinstance(a, dict)}
        if name not in selected or 'SHA256.json' not in selected:
            raise ValueError('新版本尚未上传完整，请稍后重试')
        size = selected[name].get('size')
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            raise ValueError('发布包体积异常')
        package_url = asset_url(selected[name].get('browser_download_url'), tag, name)
        checksum_url = asset_url(selected['SHA256.json'].get('browser_download_url'), tag, 'SHA256.json')
        self.release = dict(tag=tag, name=name, size=size, url=package_url, checksum_url=checksum_url)
        self.set(state='available', latest_version=tag.removeprefix('v'), message=f'发现新版本 {tag}\n下载后将重启软件，账号和设置会保留')

    def download(self):
        release = self.release
        if not release: raise ValueError('请先检查新版本')
        if not self.install_root or self.demo:
            raise ValueError('请使用 Windows 桌面发布版安装更新')
        rows = read_json(release['checksum_url'])
        matching = [r for r in rows if isinstance(r, dict) and r.get('file') == release['name']] if isinstance(rows, list) else []
        if len(matching) != 1 or matching[0].get('bytes') != release['size'] or not re.fullmatch('[a-f0-9]{64}', matching[0].get('sha256', '')):
            raise ValueError('发布校验信息不匹配')
        self.root.mkdir(parents=True, exist_ok=True)
        job = self.root / uuid.uuid4().hex
        job.mkdir()
        archive = job / 'release.zip'
        digest = hashlib.sha256(); received = 0
        try:
            with open_release(release['url']) as response, archive.open('xb') as stream:
                while block := response.read(65536):
                    received += len(block)
                    if received > release['size'] or received > MAX_DOWNLOAD:
                        raise ValueError('下载包体积与发布不符')
                    stream.write(block); digest.update(block)
                    self.set(progress=min(99, received * 100 // release['size']))
            if received != release['size'] or digest.hexdigest() != matching[0]['sha256']:
                raise ValueError('更新包校验失败，请重新下载')
            stage = self.install_root.parent / ('.school-wifi-update-' + job.name)
            manifest = extract_verified(archive, stage)
            self.stage, self.manifest, self.job = stage, manifest, job
            self.set(state='ready', progress=100, message='下载及校验完成，可以安装并重启')
        finally:
            archive.unlink(missing_ok=True)

    def install(self, *, running=False):
        with self.lock:
            if self.state['state'] != 'ready' or not self.stage or not self.install_root:
                raise ValueError('请先下载更新')
            self.state.update(state='installing', message='正在安装，软件即将重启')
        try:
            script = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'scripts/apply-update.ps1'
            helper = self.job / 'apply-update.ps1'
            shutil.copy2(script, helper)
            task = 'NjustWifiUpdate' + self.job.name[:12]
            plan = dict(install_root=str(self.install_root), stage=str(self.stage),
                        backup=str(self.install_root.parent / ('.school-wifi-backup-' + self.job.name)),
                        manifest=self.manifest, data_root=str(self.root.parent), expected_version=self.state['latest_version'],
                        task_name=task, demo=False, port=18089, was_running=running)
            plan_path = self.job / 'plan.json'
            plan_path.write_text(json.dumps(plan), 'utf-8')
            # Scheduler gives the helper its own lifetime beyond tray/monitor jobs.
            launch = self.job / 'launch.ps1'
            launch.write_text("param([string]$Helper,[string]$Plan,[string]$Task)\n$ErrorActionPreference='Stop'\n$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"'+$Helper+'\" -Plan \"'+$Plan+'\"')\n$p=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited\n$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries\nRegister-ScheduledTask -TaskName $Task -Action $a -Principal $p -Settings $s -Force | Out-Null\nStart-ScheduledTask -TaskName $Task\n", 'utf-8')
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(launch), '-Helper', str(helper), '-Plan', str(plan_path), '-Task', task], timeout=30, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            self.set(state='ready', message='更新安装启动失败，可再次尝试')
            raise ValueError('更新安装启动失败，请重试') from None

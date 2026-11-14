"""Independent scheduled hosting for the small native desktop shell."""
from pathlib import Path
import os
import subprocess
import sys


def task_options(args):
    if args.port != 18089 and not args.demo:
        raise ValueError("自定义桌面端口仅用于演示")
    name = "NjustWifiDesktopDemo" + str(args.port) if args.demo else "NjustWifiLocalMonitor"
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    scripts = Path(getattr(sys, '_MEIPASS', root)) / 'scripts'
    system = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return name, root, [str(system), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-File', str(scripts / 'startup.ps1'), '-TaskName', name]


def bootstrap(args, store):
    _, root, command = task_options(args)
    executable = Path(sys.executable)
    if not getattr(sys, 'frozen', False) and executable.with_name('pythonw.exe').exists():
        executable = executable.with_name('pythonw.exe')
    options = ['-Mode', 'Install', '-Executable', str(executable), '-ProjectRoot', str(root),
               '-Port', str(args.port), '-DataDirectory', str(store.root)]
    if args.demo or not store.settings['startup_enabled']:
        options.append('-NoLogon')
    if args.demo:
        options.append('-Demo')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    for tail in (options, ['-Mode', 'Start']):
        result = subprocess.run(command + tail, capture_output=True, timeout=30, creationflags=flags)
        if result.returncode:
            raise RuntimeError("后台启动失败，请检查任务计划程序")


def stop_background(args):
    _, _, command = task_options(args)
    subprocess.run(command + ['-Mode', 'Stop'], capture_output=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW, check=True)

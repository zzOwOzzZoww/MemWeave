"""Desktop launcher creation. The launcher never embeds API keys or runtime tokens."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import memweave_home


def _powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        capture_output=True, encoding='utf-8', errors='replace', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('桌面快捷方式创建失败，可先使用 memweave ui；运行 memweave shortcut 重试')
    return result.stdout.strip()


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_shortcut(desktop: Path | None = None) -> Path:
    home = memweave_home()
    launcher_dir = home / 'launchers'
    launcher_dir.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).resolve().parent / 'assets'
    if os.name != 'nt':
        raise RuntimeError('当前版本自动创建桌面快捷方式支持 Windows；其他系统请使用 memweave ui')
    if desktop is None:
        output = _powershell("[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); [Environment]::GetFolderPath('Desktop')")
        if not output:
            raise RuntimeError('无法定位桌面，请使用 memweave shortcut --desktop 指定位置')
        desktop = Path(output)
    desktop.mkdir(parents=True, exist_ok=True)
    icon = launcher_dir / 'memweave-icon.ico'
    shutil.copy2(assets / 'memweave-icon.ico', icon)
    # pythonw hides the terminal; failures appear in a message box and a local log.
    launcher = launcher_dir / 'open_memweave.pyw'
    launcher.write_text(
        'import os\n'
        f'os.environ["MEMWEAVE_HOME"] = {str(home)!r}\n'
        'from agent_knowledge_bridge.desktop import launch\nlaunch()\n', encoding='utf-8')
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('当前 Python 缺少 pythonw.exe，请修复 Python 安装或使用 memweave ui')
    shortcut = desktop / 'MemWeave知识管理.lnk'
    if shortcut.exists():
        backup = shortcut.with_suffix('.lnk.memweave-backup')
        if not backup.exists():
            shutil.copy2(shortcut, backup)
    _powershell(
        '$ErrorActionPreference="Stop"; $s=New-Object -ComObject WScript.Shell; '
        f'$l=$s.CreateShortcut({_ps(str(shortcut))}); '
        f'$l.TargetPath={_ps(str(pythonw))}; '
        f'$l.Arguments={_ps(subprocess.list2cmdline([str(launcher)]))}; '
        f'$l.WorkingDirectory={_ps(str(home))}; $l.IconLocation={_ps(str(icon)+",0")}; '
        '$l.Description="MemWeave 知识管理"; $l.WindowStyle=7; $l.Save()')
    return shortcut


def launch() -> None:
    import ctypes
    from contextlib import redirect_stdout, redirect_stderr
    from .cli import main
    log = memweave_home() / 'logs' / 'launcher.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open('a', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
            main(['ui', '--no-setup'])
    except (Exception, SystemExit):
        # Do not display untrusted model error bodies or credentials.
        ctypes.windll.user32.MessageBoxW(None,
            f'知识管理启动失败。请运行 memweave doctor 检查。\n日志：{log}', 'MemWeave', 0x10)

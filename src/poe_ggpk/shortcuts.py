"""Windows shortcuts (Start menu and desktop) for the GUI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .icon import write_ico
from .schema import default_cache_dir

APP_NAME = "PoE GGPK Extractor"
APP_ID = "poe-ggpk-extractor.gui"


def icon_path() -> Path:
    """The .ico file, created on first use."""
    path = default_cache_dir().parent / "poe-ggpk.ico"
    if not path.exists():
        write_ico(path)
    return path


def gui_command() -> tuple[str, str]:
    """(executable, arguments) that start the GUI without a console window."""
    scripts = Path(sys.executable).parent
    launcher = scripts / "poe-ggpk-gui.exe"
    if launcher.exists():
        return str(launcher), ""
    return str(scripts / "pythonw.exe"), "-m poe_ggpk.gui"


def _powershell(script: str) -> str:
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shortcut_folders(desktop: bool = True, start_menu: bool = True) -> list[Path]:
    names = (["Desktop"] if desktop else []) + (["Programs"] if start_menu else [])
    if not names:
        return []
    out = _powershell("; ".join(f"[Environment]::GetFolderPath('{n}')" for n in names))
    return [Path(line) for line in out.splitlines() if line.strip()]


def create_shortcuts(desktop: bool = True, start_menu: bool = True) -> list[Path]:
    target, args = gui_command()
    ico = icon_path()
    created = []
    for folder in shortcut_folders(desktop, start_menu):
        link = folder / f"{APP_NAME}.lnk"
        _powershell(
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(str(link)) + "); "
            f"$s.TargetPath = {_ps_quote(target)}; "
            f"$s.Arguments = {_ps_quote(args)}; "
            f"$s.WorkingDirectory = {_ps_quote(str(Path.home()))}; "
            f"$s.IconLocation = {_ps_quote(str(ico) + ',0')}; "
            f"$s.Description = {_ps_quote('Browse and extract Path of Exile game data')}; "
            "$s.Save()"
        )
        created.append(link)
    return created


def remove_shortcuts() -> list[Path]:
    removed = []
    for folder in shortcut_folders():
        link = folder / f"{APP_NAME}.lnk"
        if link.exists():
            link.unlink()
            removed.append(link)
    return removed

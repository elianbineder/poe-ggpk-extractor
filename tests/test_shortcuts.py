"""Tests of the application icon and the Windows shortcut helpers (no real shortcuts created)."""

from PIL import Image

from poe_ggpk import shortcuts
from poe_ggpk.icon import icon_image, write_ico


def test_icon_has_every_size(tmp_path):
    path = write_ico(tmp_path / "app.ico")
    with Image.open(path) as ico:
        assert {(16, 16), (32, 32), (256, 256)} <= set(ico.info["sizes"])
    assert icon_image(48).size == (48, 48)


def test_gui_command_prefers_the_console_less_launcher():
    exe, args = shortcuts.gui_command()
    assert exe.lower().endswith(("poe-ggpk-gui.exe", "pythonw.exe"))
    assert args in ("", "-m poe_ggpk.gui")


def test_create_shortcuts_builds_the_expected_script(tmp_path, monkeypatch):
    scripts = []
    monkeypatch.setattr(shortcuts, "shortcut_folders", lambda desktop=True, start_menu=True: [tmp_path])
    monkeypatch.setattr(shortcuts, "_powershell", scripts.append)
    monkeypatch.setattr(shortcuts, "icon_path", lambda: tmp_path / "app.ico")
    links = shortcuts.create_shortcuts()
    assert links == [tmp_path / "PoE GGPK Extractor.lnk"]
    assert "CreateShortcut" in scripts[0] and "app.ico,0" in scripts[0] and "$s.Save()" in scripts[0]


def test_quotes_are_escaped_for_powershell():
    assert shortcuts._ps_quote(r"C:\it's here") == r"'C:\it''s here'"

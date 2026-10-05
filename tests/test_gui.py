"""Smoke test of the GUI against the real installation (skipped if it is not installed)."""

import os
import time
from pathlib import Path

import pytest

LOCATION = os.environ.get("POE_GGPK", r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not Path(LOCATION).exists(), reason="Game not installed"),
]


def test_gui_open_search_preview_and_table(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    import poe_ggpk.gui as gui

    monkeypatch.setattr(gui, "SETTINGS_FILE", tmp_path / "gui.json")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no graphical environment")
    root.withdraw()
    app = gui.App(root)

    def pump_until_idle(timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            root.update()
            if not app.worker.busy:
                for _ in range(5):  # let pending callbacks run
                    root.update()
                    time.sleep(0.05)
                if not app.worker.busy:
                    return
            time.sleep(0.05)
        raise TimeoutError("the GUI did not finish in time")

    try:
        app.source_path.set(LOCATION)
        app.index_path.set("")
        app.open_source()
        pump_until_idle()
        assert app.fs is not None and app.tree.get_children()
        assert any(entry[0].endswith("mods.datc64") for entry in app.table_entries)

        app.file_query.set("currencyrerollrare.dds")
        app.search_files()
        app.tree.selection_set("art/2ditems/currency/currencyrerollrare.dds")
        pump_until_idle()
        assert app.preview_image is not None  # texture converted and displayed

        app.table_list.selection_set("data/baseitemtypes.datc64")
        pump_until_idle()
        assert any(r.get("Name") == "Chaos Orb" for r in app.current_rows)
        assert len(app.grid.get_children()) == min(len(app.current_rows), gui.MAX_GRID_ROWS)
    finally:
        root.destroy()


def test_gui_reopen_while_loading_does_not_fail(tmp_path, monkeypatch):
    """Regression: opening again while the previous load was still reading tables raised
    'ValueError: seek of closed file'."""
    tk = pytest.importorskip("tkinter")
    import poe_ggpk.gui as gui

    errors = []
    monkeypatch.setattr(gui, "SETTINGS_FILE", tmp_path / "gui.json")
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no graphical environment")
    root.withdraw()
    app = gui.App(root)
    try:
        app.source_path.set(LOCATION)
        app.index_path.set("")
        app.open_source()
        end = time.time() + 120
        while app.fs is None and time.time() < end:
            root.update()
            time.sleep(0.02)
        app.open_source()  # second click on "Open" while the table list is still loading
        end = time.time() + 180
        while (app.worker.busy or app.fs is None) and time.time() < end:
            root.update()
            time.sleep(0.05)
        assert errors == []
        assert app.table_entries
    finally:
        root.destroy()

"""Graphical interface (Tkinter) for poe-ggpk-extractor.

Opened with ``poe-ggpk-gui`` (no console), ``poe-ggpk gui`` or ``python -m poe_ggpk.gui``.
Long operations run on a separate thread; the window is never touched from those
threads: results come back through a queue polled with ``after``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__
from .convert import ConversionError, decode_text, dds_to_png
from .dat import LANGUAGES, DatError, DatFile, GameData, rows_to_csv, rows_to_json, schema_mismatch
from .filesystem import PoEFileSystem
from .schema import Schema, default_cache_dir

DEFAULT_LOCATIONS = {
    "Path of Exile": [r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile",
                      r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile"],
    "Path of Exile 2": [r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile 2",
                        r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile 2"],
}
OTHER_LOCATION = "Other location"
TEXT_EXTENSIONS = (".txt", ".ot", ".otc", ".it", ".itc", ".ais", ".ao", ".aoc", ".arm", ".epk", ".pet",
                   ".trl", ".mat", ".fxgraph", ".csd", ".gt", ".et", ".tgt", ".sm", ".amd", ".json", ".xml")
MAX_GRID_ROWS = 2000
MAX_SEARCH_RESULTS = 5000
SETTINGS_FILE = default_cache_dir() / "gui.json"


def _installed(path: str) -> bool:
    p = Path(path)
    return (p / "Content.ggpk").is_file() or (p / "Bundles2" / "_.index.bin").is_file()


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class Worker:
    """Runs functions in the background and hands results back to the UI thread."""

    def __init__(self, root: tk.Tk, on_status: Callable[[str, float | None], None]):
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.on_status = on_status
        self.busy = 0
        root.after(80, self._poll)

    def run(self, label: str, func: Callable[..., Any], on_done: Callable[[Any], None],
            on_error: Callable[[BaseException], None] | None = None) -> None:
        """``func(progress)`` runs on another thread; ``progress(text, fraction|None)`` reports progress."""
        self.busy += 1
        self.on_status(label, None)

        def progress(text: str, fraction: float | None = None) -> None:
            self.queue.put(("progress", text, fraction))

        def target() -> None:
            try:
                result = func(progress)
            except BaseException as exc:  # noqa: BLE001 - shown to the user
                traceback.print_exc()
                self.queue.put(("error", exc, on_error))
            else:
                self.queue.put(("done", result, on_done))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "progress":
                    self.on_status(a, b)
                    continue
                self.busy -= 1
                if not self.busy:
                    self.on_status("Ready", 0.0)  # before the callback, which may set its own message
                if kind == "done":
                    b(a)
                elif b is not None:
                    b(a)
                else:
                    messagebox.showerror("Error", f"{type(a).__name__}: {a}")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"PoE GGPK Extractor {__version__}")
        root.geometry("1280x820")
        root.minsize(960, 600)
        self._set_window_icon()
        self.settings = self._load_settings()
        self.fs: PoEFileSystem | None = None
        self.schema: Schema | None = None
        self.dirs: dict[str, list[str]] = {}
        self.sizes: dict[str, int] = {}
        self.current_rows: list[dict[str, Any]] = []
        self.current_table_label = ""
        self.preview_image = None
        self.change_report = None
        self.generation = 0  # changes on every open; results of older opens are discarded

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=22)

        self._build_source_bar()
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        self._build_files_tab()
        self._build_tables_tab()
        self._build_torrent_tab()
        self._build_info_tab()
        self._build_status_bar()
        self.worker = Worker(root, self._set_status)

        self._load_schema_async()
        if self.source_path.get() and _installed(self.source_path.get()):
            root.after(200, self.open_source)

    def _set_window_icon(self) -> None:
        try:
            from PIL import ImageTk

            from .icon import icon_image
            from .shortcuts import icon_path

            self._icon_images = [ImageTk.PhotoImage(icon_image(n)) for n in (16, 32, 64)]
            self.root.iconphoto(True, *self._icon_images)
            if os.name == "nt":
                self.root.iconbitmap(default=str(icon_path()))  # crisp taskbar icon
        except (tk.TclError, OSError, ImportError):
            pass

    # -- persistent settings ----------------------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        self.settings.update({
            "source": self.source_path.get(), "index": self.index_path.get(),
            "torrent": self.torrent_path.get(), "reference": self.reference_path.get(),
            "language": self.language.get(),
        })
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")
        except OSError:
            pass

    # -- source bar -------------------------------------------------------------------------------------
    def _build_source_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="Game:").pack(side="left")
        self.game_choice = ttk.Combobox(bar, values=list(DEFAULT_LOCATIONS) + [OTHER_LOCATION],
                                        state="readonly", width=16)
        self.game_choice.pack(side="left", padx=(4, 10))
        self.game_choice.bind("<<ComboboxSelected>>", self._on_game_choice)

        self.source_path = tk.StringVar(value=self.settings.get("source") or self._first_installed())
        ttk.Label(bar, text="Location:").pack(side="left")
        ttk.Entry(bar, textvariable=self.source_path, width=60).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(bar, text="Browse…", command=self._browse_source).pack(side="left")

        self.index_path = tk.StringVar(value=self.settings.get("index", ""))
        ttk.Label(bar, text="Reconstructed index (optional):").pack(side="left", padx=(12, 0))
        ttk.Entry(bar, textvariable=self.index_path, width=26).pack(side="left", padx=4)
        ttk.Button(bar, text="…", width=3, command=self._browse_index).pack(side="left")
        ttk.Button(bar, text="Open", command=self.open_source).pack(side="left", padx=(8, 0))
        self._sync_game_choice()

    def _first_installed(self) -> str:
        for locs in DEFAULT_LOCATIONS.values():
            for loc in locs:
                if _installed(loc):
                    return loc
        return ""

    def _sync_game_choice(self) -> None:
        current = os.path.normcase(self.source_path.get())
        for game, locs in DEFAULT_LOCATIONS.items():
            if any(os.path.normcase(l) == current for l in locs):
                self.game_choice.set(game)
                return
        self.game_choice.set(OTHER_LOCATION)

    def _on_game_choice(self, _event=None) -> None:
        game = self.game_choice.get()
        if game in DEFAULT_LOCATIONS:
            for loc in DEFAULT_LOCATIONS[game]:
                if _installed(loc):
                    self.source_path.set(loc)
                    self.index_path.set("")
                    self.open_source()
                    return
            messagebox.showwarning("Not found", f"{game} was not found in the usual locations.")
        else:
            self._browse_source()

    def _browse_source(self) -> None:
        path = filedialog.askopenfilename(title="Choose Content.ggpk (or cancel to choose a folder)",
                                          filetypes=[("Content.ggpk", "*.ggpk"), ("Index", "_.index.bin")])
        if not path:
            path = filedialog.askdirectory(title="Game installation folder")
        if path:
            self.source_path.set(path)
            self._sync_game_choice()

    def _browse_index(self) -> None:
        path = filedialog.askopenfilename(title="Reconstructed index",
                                          filetypes=[("Reconstructed index", "*.index.gz"), ("All files", "*.*")])
        if path:
            self.index_path.set(path)

    # -- status bar -------------------------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bar, length=260, mode="determinate", maximum=1.0)
        self.progress.pack(side="right")

    def _set_status(self, text: str, fraction: float | None) -> None:
        self.status.set(text)
        if fraction is None:
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            if str(self.progress["mode"]) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress["value"] = fraction

    # -- opening ---------------------------------------------------------------------------------------
    def _load_schema_async(self) -> None:
        def load(progress):
            progress("Loading table schema…")
            return Schema.load()

        def done(schema):
            self.schema = schema

        def failed(exc):
            self.status.set(f"No table schema ({exc}); tables will be shown without column names")

        self.worker.run("Loading schema…", load, done, failed)

    def open_source(self) -> None:
        source = self.source_path.get().strip()
        index = self.index_path.get().strip() or None
        if not source:
            messagebox.showinfo("Source", "Choose the game installation or a Content.ggpk.")
            return
        self._save_settings()
        self.generation += 1
        generation = self.generation

        def load(progress):
            progress(f"Opening {source}…")
            fs = PoEFileSystem(source, index_file=index)
            progress("Building the folder tree…")
            dirs: dict[str, set[str]] = {}
            sizes: dict[str, int] = {}
            for f in fs.index.files:
                if not f.path:
                    continue
                sizes[f.path] = f.size
                parent = ""
                parts = f.path.split("/")
                for depth, part in enumerate(parts):
                    is_dir = depth < len(parts) - 1
                    child = parent + part + ("/" if is_dir else "")
                    dirs.setdefault(parent, set()).add(child)
                    parent = child
            return fs, {k: sorted(v, key=lambda c: (not c.endswith("/"), c)) for k, v in dirs.items()}, sizes

        def done(result):
            if generation != self.generation:
                return  # the user opened another location in the meantime
            # The previous location is not closed explicitly: a running task may still be
            # reading it; its files are released once nothing references it.
            self.fs, self.dirs, self.sizes = result
            self.change_report = None
            self._populate_tree()
            self._show_info()
            self._load_table_list()
            kind = "reconstructed index" if getattr(self.fs.index, "status", None) is not None else "original index"
            self.root.title(f"PoE GGPK Extractor — {self.fs.location} ({kind})")

        self.worker.run("Opening…", load, done)

    # -- Files tab ---------------------------------------------------------------------------------------
    def _build_files_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="Files")
        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        pane.add(left, weight=2)
        search = ttk.Frame(left)
        search.pack(fill="x", pady=(0, 4))
        self.file_query = tk.StringVar()
        entry = ttk.Entry(search, textvariable=self.file_query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.search_files())
        ttk.Button(search, text="Search", command=self.search_files).pack(side="left", padx=4)
        ttk.Button(search, text="Show tree", command=self._populate_tree).pack(side="left")
        ttk.Label(left, text="Search: text or pattern with * (e.g. data/*.datc64, *currency*.dds)",
                  foreground="#666").pack(anchor="w")

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("size", "status"), selectmode="extended")
        self.tree.heading("#0", text="Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Confidence")
        self.tree.column("#0", width=460)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("status", width=80)
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(4, 0))
        self.convert_on_extract = tk.BooleanVar(value=True)
        ttk.Button(actions, text="Extract selection…", command=self.extract_selection).pack(side="left")
        ttk.Checkbutton(actions, text="Convert DDS→PNG and texts→UTF-8",
                        variable=self.convert_on_extract).pack(side="left", padx=8)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(right, weight=3)
        self.preview_title = tk.StringVar(value="Select a file to see its content")
        ttk.Label(right, textvariable=self.preview_title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.preview_buttons = ttk.Frame(right)
        self.preview_buttons.pack(fill="x", pady=4)
        self.preview_image_label = ttk.Label(right)
        self.preview_image_label.pack(anchor="nw")
        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True)
        self.preview_text = tk.Text(text_frame, wrap="none", font=("Consolas", 9), height=10)
        tsb = ttk.Scrollbar(text_frame, orient="vertical", command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=tsb.set)
        self.preview_text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")

    def _status_of(self, path: str) -> str:
        status = getattr(self.fs.index, "status", None) if self.fs else None
        return status.get(path, "") if status else ""

    def _insert_node(self, parent_id: str, path: str) -> None:
        name = path.rstrip("/").rsplit("/", 1)[-1] + ("/" if path.endswith("/") else "")
        if path.endswith("/"):
            node = self.tree.insert(parent_id, "end", iid=path, text=name, values=("", ""))
            self.tree.insert(node, "end", iid=path + "\0dummy", text="")
        else:
            self.tree.insert(parent_id, "end", iid=path, text=name,
                             values=(_human(self.sizes.get(path, 0)), self._status_of(path)))

    def _populate_tree(self) -> None:
        reconstructed = self.fs is not None and getattr(self.fs.index, "status", None) is not None
        self.tree.configure(displaycolumns=("size", "status") if reconstructed else ("size",))
        self.tree.delete(*self.tree.get_children())
        for child in self.dirs.get("", []):
            self._insert_node("", child)

    def _on_tree_open(self, _event=None) -> None:
        node = self.tree.focus()
        dummy = node + "\0dummy"
        if self.tree.exists(dummy):
            self.tree.delete(dummy)
            for child in self.dirs.get(node, []):
                self._insert_node(node, child)

    def search_files(self) -> None:
        query = self.file_query.get().strip().lower()
        if not self.fs or not query:
            self._populate_tree()
            return
        import fnmatch

        pattern = query if any(ch in query for ch in "*?[") else f"*{query}*"
        results = [p for p in self.sizes if fnmatch.fnmatchcase(p.lower(), pattern)]
        self.tree.delete(*self.tree.get_children())
        for p in sorted(results)[:MAX_SEARCH_RESULTS]:
            self.tree.insert("", "end", iid=p, text=p,
                             values=(_human(self.sizes.get(p, 0)), self._status_of(p)))
        more = f" (showing {MAX_SEARCH_RESULTS:,})" if len(results) > MAX_SEARCH_RESULTS else ""
        self.status.set(f"{len(results):,} matching files{more}")

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if len(sel) != 1 or sel[0].endswith("/") or not self.fs:
            return
        path = sel[0]
        fs = self.fs

        def load(progress):
            progress(f"Reading {path}…")
            return fs.read(path)

        self.worker.run("Reading…", load,
                        lambda data: self._show_preview(path, data) if fs is self.fs else None)

    def _clear_preview(self) -> None:
        for w in self.preview_buttons.winfo_children():
            w.destroy()
        self.preview_image_label.configure(image="")
        self.preview_image = None
        self.preview_text.delete("1.0", "end")

    def _show_preview(self, path: str, data: bytes) -> None:
        self._clear_preview()
        status = self._status_of(path)
        self.preview_title.set(f"{path}   ({_human(len(data))}{', ' + status if status else ''})")
        low = path.lower()
        if low.endswith(".dds"):
            try:
                from PIL import Image, ImageTk
                import io

                img = Image.open(io.BytesIO(dds_to_png(data)))
                info = f"Texture {img.width}×{img.height}"
                if max(img.size) < 200:  # small icons: enlarge them so they are visible
                    factor = 200 // max(img.size) + 1
                    img = img.resize((img.width * factor, img.height * factor), Image.LANCZOS)
                    info += f" (enlarged ×{factor})"
                img.thumbnail((640, 640))
                self.preview_image = ImageTk.PhotoImage(img)
                self.preview_image_label.configure(image=self.preview_image)
                self.preview_text.insert("end", info)
            except (ConversionError, ImportError, OSError) as exc:
                self.preview_text.insert("end", f"Could not display the texture: {exc}")
        elif low.endswith((".datc64", ".datcl64")):
            ttk.Button(self.preview_buttons, text="Open in the Tables tab",
                       command=lambda: self.open_table_path(path)).pack(side="left")
            try:
                dat = DatFile.from_path_and_bytes(path, data)
                self.preview_text.insert("end", f"Table: {dat.row_count:,} rows of {dat.row_size} bytes\n")
            except DatError as exc:
                self.preview_text.insert("end", f"Does not look like a valid table: {exc}\n")
        elif low.endswith(TEXT_EXTENSIONS) or path.startswith("_unknown/") and low.endswith(".txt"):
            self.preview_text.insert("end", decode_text(data[:400_000]))
        else:
            self.preview_text.insert("end", self._hexdump(data[:4096]))

    @staticmethod
    def _hexdump(data: bytes) -> str:
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{i:08x}  {chunk.hex(' '):<48}  {text}")
        return "\n".join(lines)

    def _selected_files(self) -> list[str]:
        out: list[str] = []
        for item in self.tree.selection():
            if item.endswith("/"):
                out.extend(p for p in self.sizes if p.startswith(item))
            elif "\0" not in item:
                out.append(item)
        return sorted(set(out))

    def extract_selection(self) -> None:
        if not self.fs:
            return
        paths = self._selected_files()
        if not paths:
            messagebox.showinfo("Extract", "Select files or folders in the tree.")
            return
        target = filedialog.askdirectory(title=f"Destination folder for {len(paths):,} files")
        if not target:
            return
        fs, convert = self.fs, self.convert_on_extract.get()

        def work(progress):
            fs.extract(paths, target, lambda d, t, p: progress(f"Extracting {d:,}/{t:,}", d / max(t, 1)))
            converted = failed = 0
            if convert:
                for n, p in enumerate(paths, 1):
                    src = Path(target) / p
                    low = p.lower()
                    try:
                        if low.endswith(".dds") and src.exists():
                            src.with_suffix(".png").write_bytes(dds_to_png(src.read_bytes()))
                            converted += 1
                        elif low.endswith(TEXT_EXTENSIONS) and src.exists():
                            src.with_name(src.name + ".utf8.txt").write_text(
                                decode_text(src.read_bytes()), encoding="utf-8")
                            converted += 1
                    except ConversionError:
                        failed += 1
                    if n % 50 == 0:
                        progress(f"Converting {n:,}/{len(paths):,}", n / len(paths))
            return len(paths) - len(fs.skipped), len(fs.skipped), converted, failed

        def done(r):
            ok, skipped, conv, bad = r
            msg = f"Extracted {ok:,} files to {target}."
            if skipped:
                msg += f"\nSkipped {skipped:,} (bundles not downloaded in this installation)."
            if convert:
                msg += f"\nConverted {conv:,}" + (f" ({bad:,} could not be converted)" if bad else "") + "."
            messagebox.showinfo("Extraction finished", msg)
            if hasattr(os, "startfile"):
                os.startfile(target)  # open the folder in Explorer

        self.worker.run("Extracting…", work, done)

    # -- Tables tab --------------------------------------------------------------------------------------
    def _build_tables_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab, text="Tables")
        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.table_filter = tk.StringVar()
        self.table_filter.trace_add("write", lambda *_: self._fill_table_list())
        ttk.Entry(top, textvariable=self.table_filter).pack(side="left", fill="x", expand=True)
        self.only_changed = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Changed only", variable=self.only_changed,
                        command=self._on_only_changed).pack(side="left", padx=4)
        self.table_list = ttk.Treeview(left, columns=("rows", "state"), show="tree headings", selectmode="browse")
        self.table_list.heading("#0", text="Table")
        self.table_list.heading("rows", text="Rows")
        self.table_list.heading("state", text="Status")
        self.table_list.column("#0", width=230)
        self.table_list.column("rows", width=110, anchor="e")
        self.table_list.column("state", width=150)
        lsb = ttk.Scrollbar(left, orient="vertical", command=self.table_list.yview)
        self.table_list.configure(yscrollcommand=lsb.set)
        self.table_list.pack(side="left", fill="both", expand=True, pady=(4, 0))
        lsb.pack(side="right", fill="y", pady=(4, 0))
        self.table_list.bind("<<TreeviewSelect>>", self._on_table_select)
        self.table_entries: list[tuple[str, str, str, str]] = []  # (id, name, rows, status)

        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(right, weight=4)
        opts = ttk.Frame(right)
        opts.pack(fill="x")
        ttk.Label(opts, text="Language:").pack(side="left")
        self.language = tk.StringVar(value=self.settings.get("language", "English"))
        lang = ttk.Combobox(opts, textvariable=self.language, values=LANGUAGES, state="readonly", width=18)
        lang.pack(side="left", padx=4)
        lang.bind("<<ComboboxSelected>>", lambda e: self._reload_table())
        self.resolve_refs = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Resolve references (show Id instead of numbers)",
                        variable=self.resolve_refs, command=self._reload_table).pack(side="left", padx=8)
        ttk.Button(opts, text="Export CSV…", command=lambda: self.export_table("csv")).pack(side="right")
        ttk.Button(opts, text="Export JSON…", command=lambda: self.export_table("json")).pack(side="right", padx=4)

        self.table_title = tk.StringVar(value="Choose a table from the list")
        ttk.Label(right, textvariable=self.table_title, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 0))
        self.table_note = tk.StringVar()
        ttk.Label(right, textvariable=self.table_note, foreground="#a15c00", wraplength=900).pack(anchor="w")
        frow = ttk.Frame(right)
        frow.pack(fill="x", pady=4)
        ttk.Label(frow, text="Filter rows:").pack(side="left")
        self.row_filter = tk.StringVar()
        fe = ttk.Entry(frow, textvariable=self.row_filter)
        fe.pack(side="left", fill="x", expand=True, padx=4)
        fe.bind("<Return>", lambda e: self._fill_grid())
        ttk.Button(frow, text="Apply", command=self._fill_grid).pack(side="left")

        grid_frame = ttk.Frame(right)
        grid_frame.pack(fill="both", expand=True)
        self.grid = ttk.Treeview(grid_frame, show="headings")
        gy = ttk.Scrollbar(grid_frame, orient="vertical", command=self.grid.yview)
        gx = ttk.Scrollbar(grid_frame, orient="horizontal", command=self.grid.xview)
        self.grid.configure(yscrollcommand=gy.set, xscrollcommand=gx.set)
        self.grid.grid(row=0, column=0, sticky="nsew")
        gy.grid(row=0, column=1, sticky="ns")
        gx.grid(row=1, column=0, sticky="ew")
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)
        self.grid.bind("<Double-1>", self._show_row_detail)

    def _game_data(self) -> GameData | None:
        if not self.fs:
            return None
        return GameData(self.fs, self.schema or Schema({"tables": [], "version": 7}), language=self.language.get())

    def _load_table_list(self) -> None:
        if not self.fs:
            return
        self.only_changed.set(False)
        fs = self.fs

        def load(progress):
            gd = GameData(fs, self.schema or Schema({"tables": [], "version": 7}))
            names = gd.list_tables()
            out = []
            for n, name in enumerate(names, 1):
                path = gd.table_path(name)
                try:
                    dat = DatFile.from_path_and_bytes(path, fs.read(path))
                    table = self.schema.table(name, fs.game) if self.schema else None
                    state = ("no schema" if table is None else
                             "ok" if not schema_mismatch(table, dat) else "schema outdated")
                    out.append((path, table.name if table else name, f"{dat.row_count:,}", state))
                except (DatError, FileNotFoundError) as exc:
                    out.append((path, name, "?", f"error: {exc}"))
                if n % 50 == 0:
                    progress(f"Reading tables {n:,}/{len(names):,}", n / len(names))
            return out

        def done(entries):
            if fs is not self.fs:
                return
            self.table_entries = entries
            self._fill_table_list()

        self.worker.run("Reading tables…", load, done)

    def _fill_table_list(self) -> None:
        q = self.table_filter.get().strip().lower()
        self.table_list.delete(*self.table_list.get_children())
        for iid, name, rows, state in self.table_entries:
            if q and q not in name.lower() and q not in iid.lower():
                continue
            self.table_list.insert("", "end", iid=iid, text=name, values=(rows, state))

    def _on_only_changed(self) -> None:
        if not self.only_changed.get():
            self._load_table_list()
            return
        if not self.fs:
            return
        ix = self.fs.index
        ref_loc = (ix.meta.get("reference") if getattr(ix, "status", None) is not None else None) \
            or self.reference_path.get().strip()
        if not ref_loc:
            messagebox.showinfo("Reference needed",
                                "Seeing what changed requires the previous version: set it in the "
                                "«Torrent / no index» tab (Reference field).")
            self.only_changed.set(False)
            return
        fs, schema = self.fs, self.schema

        def work(progress):
            from .changes import compare_tables
            from .reference import Reference

            progress(f"Loading reference {ref_loc}…")
            ref = Reference.load(ref_loc)
            progress("Comparing tables…")
            return compare_tables(fs, ref, schema)

        def done(report):
            if fs is not self.fs:
                return
            self.change_report = report
            entries = []
            for c in report.changed:
                before = "-" if c.rows_before is None else f"{c.rows_before:,}"
                delta = "" if c.delta is None else f" ({c.delta:+,})"
                entries.append((c.path, c.name, f"{before} → {c.rows_after:,}{delta}",
                                c.status + ("" if c.schema == "ok" else f", {c.schema}")))
            for u in report.unknown:
                entries.append((u.path, "(new, unnamed) " + (u.sample[:30] or u.path.rsplit("/", 1)[-1]),
                                f"{u.rows:,}", "new, unnamed"))
            self.table_entries = entries
            self._fill_table_list()
            self.status.set(f"Unchanged: {report.unchanged:,} · changed: {len(report.changed):,} · "
                            f"new unnamed: {len(report.unknown):,} · not found: {len(report.missing):,}")

        self.worker.run("Comparing…", work, done)

    def open_table_path(self, path: str) -> None:
        self.notebook.select(1)
        if not self.table_list.exists(path):
            self.table_list.insert("", 0, iid=path, text=path.rsplit("/", 1)[-1], values=("", "opened from Files"))
        self.table_list.selection_set(path)
        self.table_list.see(path)

    def _on_table_select(self, _event=None) -> None:
        sel = self.table_list.selection()
        if sel:
            self._load_table(sel[0])

    def _reload_table(self) -> None:
        sel = self.table_list.selection()
        if sel:
            self._load_table(sel[0])

    def _load_table(self, path: str) -> None:
        if not self.fs:
            return
        fs = self.fs
        gd = self._game_data()
        resolve = self.resolve_refs.get()
        lang = self.language.get()
        schema = self.schema

        def load(progress):
            progress(f"Reading {path}…")
            name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            table = None if path.startswith("_unknown/") or schema is None else schema.table(name, gd.game)
            if table is None:
                from .tablescan import read_unknown_table

                tg, rows = read_unknown_table(fs.read(path))
                kinds = {}
                for c in tg.columns:
                    kinds[c.kind] = kinds.get(c.kind, 0) + 1
                note = ("Table without schema: columns inferred from its content (name = type_offset). "
                        + ", ".join(f"{v} {k}" for k, v in kinds.items()))
                return path, rows, note
            dat_path = gd.table_path(table.name) if path == gd.table_path(table.name, "English") else path
            dat = DatFile.from_path_and_bytes(dat_path, fs.read(dat_path))
            note = schema_mismatch(table, dat) or ""
            if resolve and dat_path == gd.table_path(table.name):
                rows = gd.resolve(table.name)
            else:
                rows = dat.read_rows(table)
            label = table.name + ("" if lang == "English" else f" ({lang})")
            return label, rows, note

        def done(result):
            if fs is not self.fs:
                return
            label, rows, note = result
            self.current_table_label = label
            self.current_rows = rows
            self.table_title.set(f"{label}: {len(rows):,} rows")
            self.table_note.set(note)
            self._fill_grid()

        self.worker.run("Reading table…", load, done)

    def _fill_grid(self) -> None:
        rows = self.current_rows
        q = self.row_filter.get().strip().lower()
        if q:
            rows = [r for r in rows if any(q in _cell(v).lower() for v in r.values())]
        self.grid.delete(*self.grid.get_children())
        cols = list(self.current_rows[0].keys()) if self.current_rows else []
        self.grid.configure(columns=cols)
        for c in cols:
            self.grid.heading(c, text=c)
            self.grid.column(c, width=70 if c == "_index" else 140, stretch=False)
        for i, r in enumerate(rows[:MAX_GRID_ROWS]):
            self.grid.insert("", "end", iid=str(i), values=[_cell(r.get(c)) for c in cols])
        self._grid_rows = rows
        shown = min(len(rows), MAX_GRID_ROWS)
        extra = f" — showing {shown:,} of {len(rows):,}; export to see everything" if len(rows) > shown else ""
        filt = f" (filtered from {len(self.current_rows):,})" if q else ""
        self.status.set(f"{len(rows):,} rows{filt}{extra}")

    def _show_row_detail(self, _event=None) -> None:
        sel = self.grid.selection()
        if not sel:
            return
        row = self._grid_rows[int(sel[0])]
        win = tk.Toplevel(self.root)
        win.title(f"{self.current_table_label} — row {row.get('_index')}")
        win.geometry("700x600")
        text = tk.Text(win, wrap="word", font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        text.insert("end", json.dumps(row, ensure_ascii=False, indent=2))

    def export_table(self, fmt: str) -> None:
        if not self.current_rows:
            messagebox.showinfo("Export", "Open a table first.")
            return
        name = self.current_table_label.replace("/", "@").replace(" ", "_")
        path = filedialog.asksaveasfilename(defaultextension=f".{fmt}", initialfile=f"{name}.{fmt}",
                                            filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not path:
            return
        rows = self.current_rows
        Path(path).write_text(rows_to_csv(rows) if fmt == "csv" else rows_to_json(rows), encoding="utf-8")
        self.status.set(f"Exported {len(rows):,} rows to {path}")

    # -- Torrent tab --------------------------------------------------------------------------------------
    def _build_torrent_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Torrent / no index")
        intro = ("The Content.ggpk that GGG releases by torrent before a league does not include the index "
                 "(Bundles2/_.index.bin). Here it is reconstructed by comparing it with the version you have "
                 "installed (or with a snapshot saved before the patch).")
        ttk.Label(tab, text=intro, wraplength=1100, justify="left").pack(anchor="w", pady=(0, 10))

        form = ttk.LabelFrame(tab, text="1. Reconstruct the index", padding=10)
        form.pack(fill="x")
        self.torrent_path = tk.StringVar(value=self.settings.get("torrent", ""))
        self.reference_path = tk.StringVar(value=self.settings.get("reference", "") or self._first_installed())
        self.output_index = tk.StringVar(value=self.settings.get("output_index", ""))
        for row, (label, var, cmd) in enumerate([
            ("Torrent GGPK (no index):", self.torrent_path, self._browse_torrent),
            ("Reference (installation or snapshot):", self.reference_path, self._browse_reference),
            ("Save reconstructed index to:", self.output_index, self._browse_output_index),
        ]):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=var, width=90).grid(row=row, column=1, sticky="ew", padx=6)
            ttk.Button(form, text="Browse…", command=cmd).grid(row=row, column=2)
        form.columnconfigure(1, weight=1)
        btns = ttk.Frame(form)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Reconstruct index", command=self.reconstruct).pack(side="left")
        self.open_torrent_btn = ttk.Button(btns, text="Open the torrent with this index",
                                           command=self.open_reconstructed, state="disabled")
        self.open_torrent_btn.pack(side="left", padx=8)

        self.reconstruct_result = tk.Text(tab, height=12, font=("Consolas", 9))
        self.reconstruct_result.pack(fill="x", pady=8)

        snap = ttk.LabelFrame(tab, text="2. Snapshot of the current version (before a patch)", padding=10)
        snap.pack(fill="x")
        ttk.Label(snap, text="Saves the index and fingerprints of the installed version so the index can be "
                             "reconstructed even after the game has been updated (≈3.5 min and 80 MB for PoE1).",
                  wraplength=1100).grid(row=0, column=0, columnspan=3, sticky="w")
        self.snapshot_dir = tk.StringVar(value=str(Path.home() / "Documents" / "poe-ggpk" / "snapshots"
                                                   / time.strftime("snapshot-%Y%m%d")))
        ttk.Label(snap, text="Destination folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(snap, textvariable=self.snapshot_dir, width=90).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(snap, text="Browse…", command=lambda: self._browse_dir(self.snapshot_dir)).grid(row=1, column=2)
        ttk.Button(snap, text="Create snapshot of the opened location",
                   command=self.create_snapshot).grid(row=2, column=0, sticky="w", pady=(6, 0))
        snap.columnconfigure(1, weight=1)

    def _browse_torrent(self) -> None:
        path = filedialog.askopenfilename(title="Torrent Content.ggpk", filetypes=[("Content.ggpk", "*.ggpk")])
        if path:
            self.torrent_path.set(path)
            if not self.output_index.get():
                self.output_index.set(str(Path(path).with_name("reconstructed.index.gz")))

    def _browse_reference(self) -> None:
        path = filedialog.askdirectory(title="Previous game installation or snapshot folder")
        if path:
            self.reference_path.set(path)

    def _browse_output_index(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".index.gz", initialfile="reconstructed.index.gz",
                                            filetypes=[("Reconstructed index", "*.index.gz")])
        if path:
            self.output_index.set(path)

    def _browse_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def reconstruct(self) -> None:
        torrent, ref_loc = self.torrent_path.get().strip(), self.reference_path.get().strip()
        output = self.output_index.get().strip() or str(Path(torrent).with_name("reconstructed.index.gz"))
        if not torrent or not ref_loc:
            messagebox.showinfo("Missing data", "Set the torrent GGPK and the reference.")
            return
        self.output_index.set(output)
        self.settings["output_index"] = output
        self._save_settings()
        schema = self.schema

        def work(progress):
            from .reconstruct import STATUSES, reconstruct
            from .reference import Reference

            progress("Loading the reference…")
            ref = Reference.load(ref_loc)
            t = time.time()
            rec, report = reconstruct(torrent, ref,
                                      lambda d, n, name: progress(f"Bundle {d:,}/{n:,}: {name}", d / max(n, 1)),
                                      schema=schema)
            rec.save(output)
            lines = [f"Index reconstructed in {time.time() - t:.0f}s → {output}",
                     f"Bundles: identical {report.bundles['same']:,} · modified {report.bundles['changed']:,}"
                     f" · new {report.bundles['new']:,}", "", "Files by confidence level:"]
            labels = {"same": "identical bundle (exact)", "matched": "identical content (exact)",
                      "modified": "new version of an existing file",
                      "unknown": "new unnamed content (_unknown folder)"}
            for st in STATUSES:
                lines.append(f"  {st:<9} {report.files.get(st, 0):>10,}  {_human(report.bytes.get(st, 0)):>10}"
                             f"  {labels[st]}")
            lines.append(f"Reference files not found: {report.missing_old_files:,}")
            return "\n".join(lines)

        def done(text):
            self.reconstruct_result.delete("1.0", "end")
            self.reconstruct_result.insert("end", text)
            self.open_torrent_btn.configure(state="normal")

        self.worker.run("Reconstructing…", work, done)

    def open_reconstructed(self) -> None:
        self.source_path.set(self.torrent_path.get().strip())
        self.index_path.set(self.output_index.get().strip())
        self._sync_game_choice()
        self.open_source()
        self.notebook.select(1)

    def create_snapshot(self) -> None:
        if not self.fs or self.fs.ggpk is None and self.fs.bundles_dir is None:
            messagebox.showinfo("Snapshot", "Open the current game installation first.")
            return
        if getattr(self.fs.index, "status", None) is not None:
            messagebox.showinfo("Snapshot", "The opened location uses a reconstructed index; "
                                            "open the installation with its original index.")
            return
        target, location, schema = self.snapshot_dir.get().strip(), str(self.fs.location), self.schema

        def work(progress):
            from .reference import Reference

            progress("Reading the installation…")
            ref = Reference.from_location(location)
            ref.save_snapshot(target, with_fingerprints=True,
                              progress=lambda d, n: progress(f"Fingerprinting: batch {d:,}/{n:,}", d / n),
                              schema=schema)
            return target

        def done(path):
            self.reference_path.set(path)
            messagebox.showinfo("Snapshot created", f"Snapshot saved to {path}.\n"
                                                    "It has been selected as the reference.")

        self.worker.run("Creating snapshot…", work, done)

    # -- Info tab ----------------------------------------------------------------------------------------------
    def _build_info_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Info")
        self.info_text = tk.Text(tab, font=("Consolas", 10), height=20)
        self.info_text.pack(fill="both", expand=True)

    def _show_info(self) -> None:
        fs = self.fs
        ix = fs.index
        lines = [f"Location:         {fs.location}"]
        if fs.ggpk:
            lines.append(f"Type:             Content.ggpk (version {fs.ggpk.version}, "
                         f"{_human(os.path.getsize(fs.ggpk.path))})")
        lines.append(f"Game:             Path of Exile {fs.game}")
        lines.append(f"Bundles:          {len(ix.bundles):,}")
        lines.append(f"Files:            {len(ix.files):,} ({_human(sum(f.size for f in ix.files))} uncompressed)")
        status = getattr(ix, "status", None)
        if status is not None:
            counts: dict[str, int] = {}
            for v in status.values():
                counts[v] = counts.get(v, 0) + 1
            lines.append(f"Index:            RECONSTRUCTED (reference: {ix.meta.get('reference', '?')})")
            lines.append("Confidence:       " + ", ".join(f"{k}={v:,}" for k, v in sorted(counts.items())))
        elif ix.unresolved:
            lines.append(f"Unresolved:       {ix.unresolved} files")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("end", "\n".join(lines))


def main() -> int:
    try:
        import ctypes

        from .shortcuts import APP_ID

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # own taskbar icon
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

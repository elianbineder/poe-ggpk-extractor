"""Command line interface: ``poe-ggpk``."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from .convert import ConversionError, decode_text, dds_to_png
from .dat import LANGUAGES, DatError, DatFile, GameData, rows_to_csv, rows_to_json, schema_mismatch
from .filesystem import PoEFileSystem
from .reconstruct import STATUSES
from .schema import Schema, default_cache_dir, download_schema

DEFAULT_LOCATIONS = {
    1: [
        r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile",
        r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile",
        r"C:\Program Files\Epic Games\PathOfExile",
    ],
    2: [
        r"C:\Program Files (x86)\Grinding Gear Games\Path of Exile 2",
        r"C:\Program Files (x86)\Steam\steamapps\common\Path of Exile 2",
    ],
}


def _find_location(args) -> str:
    if args.ggpk:
        return args.ggpk
    env = os.environ.get("POE_GGPK")
    if env and args.game is None:
        return env
    for loc in DEFAULT_LOCATIONS[args.game or 1]:
        p = Path(loc)
        if (p / "Content.ggpk").is_file() or (p / "Bundles2" / "_.index.bin").is_file():
            return str(p)
    sys.exit("Game installation not found. Pass the path with --ggpk or the POE_GGPK variable.")


def _open(args) -> PoEFileSystem:
    loc = _find_location(args)
    t = time.time()
    fs = PoEFileSystem(loc, index_file=getattr(args, "index", None))
    if args.verbose:
        print(f"[index loaded in {time.time() - t:.1f}s from {loc}]", file=sys.stderr)
    return fs


def _matcher(patterns: list[str], use_regex: bool):
    """Glob patterns (default) or regex; case-insensitive."""
    if not patterns:
        return lambda p: True
    if use_regex:
        regs = [re.compile(p, re.IGNORECASE) for p in patterns]
        return lambda p: any(r.search(p) for r in regs)
    pats = [p.lower().replace("\\", "/") for p in patterns]
    return lambda p: any(fnmatch.fnmatchcase(p.lower(), pat) for pat in pats)


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# -- commands ----------------------------------------------------------------------
def cmd_info(args) -> None:
    fs = _open(args)
    ix = fs.index
    print(f"Location:         {fs.location}")
    if fs.ggpk:
        print(f"Type:             Content.ggpk (format version {fs.ggpk.version}, "
              f"{_human(os.path.getsize(fs.ggpk.path))})")
    else:
        print("Type:             loose Bundles2 folder (Steam/Epic)")
    print(f"Detected game:    Path of Exile {fs.game}")
    print(f"Bundles:          {len(ix.bundles):,}")
    print(f"Files:            {len(ix.files):,} ({_human(sum(f.size for f in ix.files))} uncompressed)")
    if ix.directories:
        print(f"Directories:      {len(ix.directories):,}")
    if getattr(ix, "status", None) is not None:
        counts = ix.status_counts()
        print(f"Index:            RECONSTRUCTED (reference: {ix.meta.get('reference', '?')})")
        print("Confidence:       " + ", ".join(f"{k}={counts.get(k, 0):,}" for k in STATUSES))
    else:
        print(f"Path hash:        {ix.hash_algorithm}")
        print(f"Index compressor: {ix.bundle.header.compressor_name}")
    if ix.unresolved:
        print(f"Unresolved:       {ix.unresolved} files whose hash matches no path in the index")
    missing = set(fs.missing_bundles())
    if missing:
        n = sum(1 for f in ix.files if f.bundle_index in missing)
        print(f"Not downloaded:   {len(missing):,} bundles ({n:,} files) listed in the index "
              f"but missing from this installation (e.g. shaders for other platforms)")
    if fs.ggpk:
        print(f"Loose files:      {len(fs.loose_ggpk_files()):,} (outside Bundles2)")


def cmd_ls(args) -> None:
    fs = _open(args)
    match = _matcher(args.patterns, args.regex)
    status = getattr(fs.index, "status", None)
    count = 0
    total = 0
    for vf in fs.iter_files(include_loose=args.loose):
        if not match(vf.path):
            continue
        count += 1
        total += vf.size
        if args.long:
            where = vf.bundle if vf.source == "bundle" else "[ggpk]"
            st = status.get(vf.path) if status else None
            print(f"{vf.size:>12,}  {vf.path}  ({where}{', ' + st if st else ''})")
        else:
            print(vf.path)
        if args.limit and count >= args.limit:
            break
    print(f"-- {count:,} files, {_human(total)}", file=sys.stderr)


def cmd_cat(args) -> None:
    fs = _open(args)
    data = fs.read(args.path)
    if args.raw:
        sys.stdout.buffer.write(data)
    else:
        sys.stdout.write(decode_text(data))


def cmd_extract(args) -> None:
    fs = _open(args)
    match = _matcher(args.patterns, args.regex)
    paths = [vf.path for vf in fs.iter_files(include_loose=args.loose) if match(vf.path)]
    if not paths:
        sys.exit("No file matches the patterns.")
    out = Path(args.output)
    print(f"Extracting {len(paths):,} files to {out} ...", file=sys.stderr)
    t = time.time()
    last = [0.0]

    def progress(done: int, total: int, path: str) -> None:
        now = time.time()
        if now - last[0] > 0.5 or done == total:
            last[0] = now
            print(f"\r  {done:,}/{total:,}", end="", file=sys.stderr, flush=True)

    fs.extract(paths, out, progress)
    print(f"\nDone in {time.time() - t:.1f}s", file=sys.stderr)
    if fs.skipped:
        print(f"Skipped {len(fs.skipped):,} files from bundles not downloaded in this installation",
              file=sys.stderr)
        skipped = set(fs.skipped)
        paths = [p for p in paths if p not in skipped]

    if args.convert:
        converted = failed = 0
        for p in paths:
            src = out / p
            low = p.lower()
            try:
                if low.endswith(".dds"):
                    src.with_suffix(".png").write_bytes(dds_to_png(src.read_bytes()))
                elif low.endswith((".txt", ".ot", ".otc", ".it", ".itc", ".ais", ".csd")):
                    src.with_name(src.name + ".utf8.txt").write_text(decode_text(src.read_bytes()), encoding="utf-8")
                else:
                    continue
                converted += 1
            except ConversionError as exc:
                failed += 1
                if args.verbose:
                    print(f"  not converted {p}: {exc}", file=sys.stderr)
        print(f"Converted: {converted:,}  (failed: {failed:,})", file=sys.stderr)


def _load_schema(args) -> Schema:
    schema = Schema.load(args.schema, refresh=getattr(args, "refresh_schema", False))
    if schema.warning:
        print(f"Warning: {schema.warning}", file=sys.stderr)
    return schema


def cmd_tables(args) -> None:
    if args.changed:
        return cmd_tables_changed(args)
    fs = _open(args)
    schema = _load_schema(args)
    gd = GameData(fs, schema, extension=args.ext)
    for name in gd.list_tables():
        if args.patterns and not _matcher(args.patterns, False)(name):
            continue
        table = schema.table(name, gd.game)
        dat = DatFile.from_path_and_bytes(gd.table_path(name), fs.read(gd.table_path(name)))
        rows = f"{dat.row_count:,}"
        if table is None:
            status = f"no schema (row {dat.row_size}B)"
        else:
            status = "ok" if not schema_mismatch(table, dat) else f"schema {table.row_size}B != row {dat.row_size}B"
            name = table.name
        print(f"{name:<50} {rows:>9} rows  {status}")


def cmd_tables_changed(args) -> None:
    """Tables that changed compared to a reference version (and new unnamed tables)."""
    from .changes import compare_tables
    from .reference import Reference

    fs = _open(args)
    ix = fs.index
    reconstructed = getattr(ix, "status", None) is not None
    ref_loc = args.reference or (ix.meta.get("reference") if reconstructed else None)
    if not ref_loc:
        sys.exit("Pass the previous version with --reference (installation or snapshot).")
    t = time.time()
    ref = Reference.load(ref_loc)
    print(f"Reference: {ref.label} (loaded in {time.time() - t:.1f}s)", file=sys.stderr)
    schema = _load_schema(args)
    match = _matcher(args.patterns, False) if args.patterns else None
    report = compare_tables(fs, ref, schema, args.ext, match)

    print(f"{'status':<9} {'table':<45} {'rows before':>11} {'after':>8} {'diff':>7}  {'width':>9}  schema")
    for c in report.changed:
        delta = f"{c.delta:+,}" if c.delta is not None else ""
        before = "-" if c.rows_before is None else str(c.rows_before)
        w0 = "?" if c.width_before is None and c.rows_before is not None else c.width_before
        width = f"{c.width_after}" if w0 in (None, c.width_after) else f"{w0}->{c.width_after}"
        print(f"{c.status:<9} {c.name:<45} {before:>11} {c.rows_after:>8,} {delta:>7}  {width:>9}  {c.schema}")
    if report.unknown:
        print(f"\nNew unnamed tables ({len(report.unknown)}); view them with: poe-ggpk dat <path>")
        for u in report.unknown:
            print(f"  {u.path:<60} {u.rows:>6,} rows  width {u.row_size:>4}  "
                  f"{u.text_columns} text col.  {u.sample[:40]!r}")
    print(f"\nUnchanged: {report.unchanged:,}   Changed: {len(report.changed):,}   "
          f"Not found (deleted or unidentified): {len(report.missing):,}")
    if report.missing and args.verbose:
        print("  " + ", ".join(report.missing))


def _inferred_output(raw: bytes, label: str) -> list:
    """Rows of a table without schema using inferred columns (summary on stderr)."""
    from collections import Counter

    from .tablescan import read_unknown_table

    tg, rows = read_unknown_table(raw)
    kinds = Counter(c.kind for c in tg.columns)
    print(f"{label}: table without schema, {tg.rows:,} rows of {tg.row_size} bytes, "
          f"{'UTF-32 (.datcl64)' if tg.wide else 'UTF-16 (.datc64)'} strings; inferred columns: "
          + ", ".join(f"{v} {k}" for k, v in kinds.most_common()), file=sys.stderr)
    return rows


def cmd_dat(args) -> None:
    fs = _open(args)
    schema = _load_schema(args)
    gd = GameData(fs, schema, language=args.lang, extension=args.ext)
    names = gd.list_tables() if args.all else args.tables
    if not names:
        sys.exit("Specify at least one table or use --all.")
    multiple = len(names) > 1
    out_dir = Path(args.output) if args.output else None
    if multiple and out_dir is None:
        sys.exit("To export several tables pass a directory with -o.")
    if out_dir and multiple:
        out_dir.mkdir(parents=True, exist_ok=True)

    ok = failed = 0
    for name in names:
        try:
            if "/" in name:
                # File path: a specific translation, or a new unnamed table (_unknown/...).
                path = name
                raw = fs.read(path)
                table = None if path.startswith("_unknown/") else schema.table(
                    path.rsplit("/", 1)[-1].rsplit(".", 1)[0], gd.game)
                if table is None:
                    rows = _inferred_output(raw, path)
                else:
                    dat = DatFile.from_path_and_bytes(path, raw)
                    warn = schema_mismatch(table, dat)
                    if warn:
                        print(f"Warning: {warn}", file=sys.stderr)
                    rows = dat.read_rows(table)
                text = rows_to_csv(rows) if args.format == "csv" else rows_to_json(rows)
                ok += 1
                _write_table_output(text, path.replace("/", "@"), rows, out_dir, multiple, args)
                continue
            if schema.table(name, gd.game) is None:
                # No schema: inferred columns (texts, keys, arrays, integers) or --raw.
                path = gd.table_path(name)
                raw = fs.read(path)
                if args.raw:
                    dat = DatFile.from_path_and_bytes(path, raw)
                    rows = [{"_index": i, "_raw": dat.raw_row(i).hex()} for i in range(dat.row_count)]
                else:
                    rows = _inferred_output(raw, name)
                text = rows_to_csv(rows) if args.format == "csv" else rows_to_json(rows)
                ok += 1
                _write_table_output(text, name, rows, out_dir, multiple, args)
                continue
            table, dat = gd.open(name)
            warn = schema_mismatch(table, dat)
            if warn:
                print(f"Warning: {warn}", file=sys.stderr)
            if args.resolve:
                rows = gd.resolve(table.name, enums=args.enums)
            else:
                rows = dat.read_rows(table)
            text = rows_to_csv(rows) if args.format == "csv" else rows_to_json(rows)
        except (DatError, FileNotFoundError, ValueError) as exc:
            failed += 1
            print(f"Error in {name}: {exc}", file=sys.stderr)
            continue
        ok += 1
        _write_table_output(text, table.name, rows, out_dir, multiple, args)
    if multiple:
        print(f"Exported {ok:,} tables ({failed} with errors) to {out_dir}", file=sys.stderr)


def _write_table_output(text: str, name: str, rows: list, out_dir: Path | None, multiple: bool, args) -> None:
    if out_dir is None:
        sys.stdout.write(text)
        return
    target = out_dir / f"{name}.{args.format}" if multiple or out_dir.suffix == "" else out_dir
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if args.verbose or not multiple:
        print(f"{name}: {len(rows):,} rows -> {target}", file=sys.stderr)


def cmd_snapshot(args) -> None:
    from .reference import Reference

    loc = _find_location(args)
    ref = Reference.from_location(loc)
    print(f"Creating snapshot of {loc} in {args.output} ...", file=sys.stderr)

    def progress(done: int, total: int) -> None:
        print(f"\r  fingerprints: batch {done:,}/{total:,}", end="", file=sys.stderr, flush=True)

    t = time.time()
    schema = None
    if args.fingerprints:
        try:
            schema = Schema.load(args.schema)
        except Exception as exc:
            print(f"Warning: no schema ({exc}); the list of verified tables will not be saved", file=sys.stderr)
    ref.save_snapshot(args.output, with_fingerprints=args.fingerprints, jobs=args.jobs, progress=progress,
                      schema=schema)
    print(f"\nDone in {time.time() - t:.0f}s ({len(ref.index.files):,} files, "
          f"{len(ref.index.bundles):,} bundles{', with fingerprints' if args.fingerprints else ''})", file=sys.stderr)


def cmd_reconstruct(args) -> None:
    from .reconstruct import compare_with_truth, reconstruct
    from .reference import Reference

    loc = _find_location(args)
    t = time.time()
    ref = Reference.load(args.reference)
    print(f"Reference loaded in {time.time() - t:.1f}s: {ref.label}", file=sys.stderr)
    last = [0.0]

    def progress(done: int, total: int, name: str) -> None:
        now = time.time()
        if now - last[0] > 0.5 or done == total:
            last[0] = now
            print(f"\r  bundle {done:,}/{total:,}", end="", file=sys.stderr, flush=True)

    t = time.time()
    try:
        schema = Schema.load(args.schema)
    except Exception as exc:  # works without a schema too, it just splits tables less well
        print(f"Warning: could not load the schema ({exc}); modified tables will be split less accurately",
              file=sys.stderr)
        schema = None
    rec, report = reconstruct(loc, ref, progress, schema=schema)
    rec.save(args.output)
    print(f"\nIndex reconstructed in {time.time() - t:.0f}s -> {args.output}", file=sys.stderr)
    print(f"Bundles: identical={report.bundles['same']:,}  modified={report.bundles['changed']:,}  "
          f"new={report.bundles['new']:,}")
    print("Files by confidence level:")
    labels = {
        "same": "bundle identical to the reference",
        "matched": "identical content, located by fingerprint",
        "modified": "new content of an existing file",
        "unknown": "unnamed (new content)",
    }
    for st in STATUSES:
        print(f"  {st:<9} {report.files.get(st, 0):>10,}  {_human(report.bytes.get(st, 0)):>10}  {labels[st]}")
    print(f"Reference files not found: {report.missing_old_files:,}")

    if args.validate:
        fs = PoEFileSystem(loc)
        print("Validation against the real index:")
        for k, v in sorted(compare_with_truth(rec, fs.index).items()):
            print(f"  {k:<32} {v:>10,}")


def cmd_shortcut(args) -> None:
    from .shortcuts import create_shortcuts, remove_shortcuts

    if args.remove:
        removed = remove_shortcuts()
        for link in removed:
            print(f"Removed {link}")
        if not removed:
            print("No shortcuts found")
        return
    for link in create_shortcuts(desktop=not args.no_desktop, start_menu=True):
        print(f"Created {link}")


def cmd_schema(args) -> None:
    target = default_cache_dir() / "schema.min.json"
    download_schema(target)
    s = Schema.from_file(target)
    print(f"Schema updated: {len(s.tables)} tables, {len(s.enumerations)} enumerations -> {target}")


# -- parser -------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ggpk", help="Content.ggpk, installation folder or _.index.bin "
                                       "(by default the installation is searched for, or POE_GGPK is used)")
    common.add_argument("--game", type=int, choices=(1, 2), help="Game to look for automatically (1 or 2)")
    common.add_argument("--index", help="Reconstructed index to use instead of _.index.bin "
                                        "(see 'poe-ggpk reconstruct')")
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(prog="poe-ggpk", description="Data extractor for Path of Exile's Content.ggpk")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("info", parents=[common], help="Summary of the GGPK and the bundle index")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("ls", parents=[common], help="List files (glob patterns, e.g. 'data/*.datc64')")
    s.add_argument("patterns", nargs="*")
    s.add_argument("-r", "--regex", action="store_true", help="Treat patterns as regular expressions")
    s.add_argument("-l", "--long", action="store_true", help="Show size and bundle")
    s.add_argument("--loose", action="store_true", help="Include loose GGPK files")
    s.add_argument("-n", "--limit", type=int, default=0)
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("cat", parents=[common], help="Print a file (UTF-16 texts are decoded)")
    s.add_argument("path")
    s.add_argument("--raw", action="store_true", help="Write the bytes without decoding")
    s.set_defaults(func=cmd_cat)

    s = sub.add_parser("extract", parents=[common], help="Extract files matching patterns")
    s.add_argument("patterns", nargs="+")
    s.add_argument("-o", "--output", default="extracted")
    s.add_argument("-r", "--regex", action="store_true")
    s.add_argument("--loose", action="store_true", help="Include loose GGPK files")
    s.add_argument("-c", "--convert", action="store_true",
                   help="Also convert .dds to .png and UTF-16 texts to UTF-8")
    s.set_defaults(func=cmd_extract)

    dat_common = argparse.ArgumentParser(add_help=False)
    dat_common.add_argument("--schema", help="Path to a local schema.min.json")
    dat_common.add_argument("--refresh-schema", action="store_true", help="Force downloading the schema")
    dat_common.add_argument("--ext", default="datc64", choices=("datc64", "datcl64"),
                            help="Table variant (datcl64 = UTF-32 strings, PoE1 only)")

    s = sub.add_parser("tables", parents=[common, dat_common], help="List tables and their status against the schema")
    s.add_argument("patterns", nargs="*")
    s.add_argument("--changed", action="store_true",
                   help="Only tables that changed compared to --reference (or to the reference of the "
                        "reconstructed index), with rows before/after and new unnamed tables")
    s.add_argument("--reference", help="Installation or snapshot of the previous version")
    s.set_defaults(func=cmd_tables)

    s = sub.add_parser("dat", parents=[common, dat_common], help="Export .datc64 tables to JSON/CSV")
    s.add_argument("tables", nargs="*",
                   help="Table names (e.g. Mods BaseItemTypes) or file paths "
                        "(e.g. data/spanish/mods.datc64 or _unknown/.../0000138918.datc64)")
    s.add_argument("--raw", action="store_true",
                   help="Tables without schema: rows as hex instead of inferred columns")
    s.add_argument("--all", action="store_true", help="Export every table")
    s.add_argument("-f", "--format", choices=("json", "csv"), default="json")
    s.add_argument("-o", "--output", help="File (one table) or directory (several)")
    s.add_argument("--lang", default="English", choices=LANGUAGES, help="Language of the texts")
    s.add_argument("--resolve", action="store_true",
                   help="Replace foreign keys with the Id/Name of the referenced row")
    s.add_argument("--enums", action="store_true",
                   help="With --resolve, also translate enumerations (may be outdated)")
    s.set_defaults(func=cmd_dat)

    s = sub.add_parser("snapshot", parents=[common],
                       help="Save the current version as a reference to reconstruct future indexes")
    s.add_argument("-o", "--output", required=True, help="Snapshot directory")
    s.add_argument("--fingerprints", action="store_true",
                   help="Fingerprint every file (several minutes; needed if the old GGPK "
                        "will no longer be available)")
    s.add_argument("-j", "--jobs", type=int, default=None, help="Parallel processes")
    s.add_argument("--schema", help="Path to a local schema.min.json")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("reconstruct", parents=[common],
                       help="Reconstruct the index of a GGPK that ships without one (e.g. the torrent one)")
    s.add_argument("--reference", required=True,
                   help="Old installation (folder or Content.ggpk) or snapshot directory")
    s.add_argument("-o", "--output", default="reconstructed.index.gz")
    s.add_argument("--schema", help="Path to a local schema.min.json")
    s.add_argument("--validate", action="store_true",
                   help="Compare the result with the GGPK's real index (if it has one)")
    s.set_defaults(func=cmd_reconstruct)

    s = sub.add_parser("gui", help="Open the graphical interface")
    s.set_defaults(func=lambda args: __import__("poe_ggpk.gui", fromlist=["main"]).main())

    s = sub.add_parser("shortcut", help="Create Start menu and desktop shortcuts for the GUI (Windows)")
    s.add_argument("--no-desktop", action="store_true", help="Only create the Start menu shortcut")
    s.add_argument("--remove", action="store_true", help="Remove the shortcuts")
    s.set_defaults(func=cmd_shortcut)

    s = sub.add_parser("schema", help="Download/update the table schema")
    s.set_defaults(func=cmd_schema)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

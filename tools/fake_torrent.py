"""Build a small Content.ggpk WITHOUT _.index.bin that mimics the pre-league torrent one.

* Art/2DItems/Currency_1          -> exact copy of the real bundle (must come out as "same")
* Folders/A/data.datc64           -> real bundle with changes: one modified table (another
                                     language version, valid), one deleted file and inserted
                                     new content (a texture and, with PoE2, three new tables)
* Folders/NEW/brandnew            -> bundle with a new name (new content only)

Modified bundles are stored uncompressed (chunks whose compressed size == real size),
which the reader accepts; the expected truth is saved to truth.json.
"""
import hashlib
import json
import struct
from pathlib import Path

from poe_ggpk import PoEFileSystem
from poe_ggpk.hashing import ggpk_name_hash
from poe_ggpk.reference import ggpk_bundle_entries

P1 = r"C:/Program Files (x86)/Grinding Gear Games/Path of Exile"
P2 = r"C:/Program Files (x86)/Grinding Gear Games/Path of Exile 2"
CHUNK = 256 * 1024


def raw_bundle(payload: bytes) -> bytes:
    chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    head = struct.pack("<iiiiiqqii4i", len(payload), len(payload), 48 + 4 * len(chunks), 8, 1,
                       len(payload), len(payload), len(chunks), CHUNK, 0, 0, 0, 0)
    return head + struct.pack(f"<{len(chunks)}i", *map(len, chunks)) + payload


def file_rec(name: str, data: bytes) -> bytes:
    n = (name + "\0").encode("utf-16-le")
    return (struct.pack("<I4sI", 12 + 32 + len(n) + len(data), b"FILE", len(name) + 1)
            + hashlib.sha256(data).digest() + n + data)


def dir_rec(name: str, children: list[tuple[str, int]]) -> bytes:
    n = (name + "\0").encode("utf-16-le")
    ents = b"".join(struct.pack("<IQ", ggpk_name_hash(c), o)
                    for c, o in sorted(children, key=lambda c: ggpk_name_hash(c[0])))
    return (struct.pack("<I4sII", 16 + 32 + len(n) + len(ents), b"PDIR", len(name) + 1, len(children))
            + b"\0" * 32 + n + ents)


def build_ggpk(files: dict[str, bytes], target: Path) -> None:
    """Write a GGPK containing the given files (paths with '/')."""
    body = bytearray()
    base = 28

    def write(rec: bytes) -> int:
        off = base + len(body)
        body.extend(rec)
        return off

    tree: dict = {}
    for path, data in files.items():
        node = tree
        parts = path.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = data

    def emit(name: str, node: dict) -> int:
        children = []
        for child, val in node.items():
            off = emit(child, val) if isinstance(val, dict) else write(file_rec(child, val))
            children.append((child, off))
        return write(dir_rec(name, children))

    root = emit("", tree)
    free = write(struct.pack("<I4sQ", 16, b"FREE", 0))
    target.write_bytes(struct.pack("<I4sIQQ", 28, b"GGPK", 3, root, free) + body)


def make_fake_torrent(out: Path, poe1: str = P1, poe2: str | None = None) -> dict:
    """Build the fake GGPK in ``out`` and return the expected truth.

    "New" content comes from PoE2 when given, or from synthetic DDS textures."""
    fs = PoEFileSystem(poe1)
    fs2 = PoEFileSystem(poe2) if poe2 else None

    def new_content(n: int) -> bytes:
        if fs2 is not None:
            return fs2.read(["art/2ditems/currency/currencyrerollrare.dds",
                             "art/2ditems/currency/currencyaddmodtorare.dds"][n])
        import random
        return b"DDS \x7c\x00\x00\x00" + random.Random(n).randbytes(60_000)

    entries = ggpk_bundle_entries(fs.ggpk)
    truth = {"same": [], "modified": [], "deleted": [], "new_content": [], "new_tables": []}

    new_tables: list[tuple[str, bytes]] = []
    if fs2 is not None:
        poe1_names = {f.path.rsplit("/", 1)[-1] for f in fs.index.files
                      if f.path and f.path.startswith("data/") and f.path.endswith(".datc64")}
        for wanted in ("data/balance/ascendancy.datc64", "data/balance/uncutgemadditionaltiers.datc64",
                       "data/balance/gemeffects.datc64"):
            name = wanted.rsplit("/", 1)[-1]
            if fs2.exists(wanted) and name not in poe1_names:
                new_tables.append((wanted, fs2.read(wanted)))
        if len(new_tables) < 3:  # fallback: PoE2 tables that PoE1 does not have
            for f in fs2.index.files:
                p = f.path
                if (p and p.startswith("data/balance/") and p.count("/") == 2 and p.endswith(".datc64")
                        and p.rsplit("/", 1)[-1] not in poe1_names and 2_000 < f.size < 200_000):
                    new_tables.append((p, fs2.read(p)))
                    if len(new_tables) == 3:
                        break

    # 1) identical bundle
    same_name = "Art/2DItems/Currency_1"
    same_bytes = fs.ggpk.read(entries[same_name])
    truth["same"] = [f.path for f in fs.index.files if fs.index.bundles[f.bundle_index].name == same_name]

    # 2) modified bundle
    mod_name = "Folders/A/data.datc64"
    bi = next(b.index for b in fs.index.bundles if b.name == mod_name)
    recs = sorted([f for f in fs.index.files if f.bundle_index == bi], key=lambda f: f.offset)
    data = fs._bundle(bi).read()
    spans = sorted({(f.offset, f.size) for f in recs})
    by_span = {}
    for f in recs:
        by_span.setdefault((f.offset, f.size), []).append(f.path)
    new = bytearray()
    existing = {hashlib.sha256(data[o:o + s]).digest() for o, s in spans}
    modified_done = deleted_done = inserted_done = False
    for o, s in spans:
        paths = by_span[(o, s)]
        content = data[o:o + s]
        p0 = paths[0]
        if not deleted_done and len(spans) > 4 and (o, s) == spans[3]:
            truth["deleted"] += paths
            deleted_done = True
            continue
        if not modified_done and p0.endswith(".datc64") and p0.count("/") == 1:
            for lang in ("french", "german", "spanish", "russian"):
                alt_path = p0.replace("data/", f"data/{lang}/")
                alt = fs.read(alt_path) if fs.exists(alt_path) else None
                if alt and alt != content and hashlib.sha256(alt).digest() not in existing:
                    truth["modified"].append({"paths": paths, "offset": len(new), "size": len(alt)})
                    new += alt
                    modified_done = True
                    break
            if modified_done:
                continue
        if not inserted_done and len(new) > 100_000:
            extra = new_content(0)
            truth["new_content"].append({"bundle": mod_name, "offset": len(new), "size": len(extra)})
            new += extra
            # "New league" tables: tables that only exist in PoE2, adjacent and without a signature.
            for name, raw in new_tables:
                truth["new_tables"].append({"bundle": mod_name, "offset": len(new), "size": len(raw),
                                            "source": name})
                new += raw
            inserted_done = True
        new += content

    # 3) bundle with a new name
    icon = new_content(1)
    text = ("\ufeff" + "new test content\r\n" * 2000).encode("utf-16-le")
    brand = icon + text
    truth["new_content"].append({"bundle": "Folders/NEW/brandnew", "offset": 0, "size": len(icon)})

    files = {
        f"Bundles2/{same_name}.bundle.bin": same_bytes,
        f"Bundles2/{mod_name}.bundle.bin": raw_bundle(bytes(new)),
        "Bundles2/Folders/NEW/brandnew.bundle.bin": raw_bundle(brand),
        "gateway_list.txt": fs.ggpk.read(fs.ggpk.find("gateway_list.txt")),
    }
    out.mkdir(parents=True, exist_ok=True)
    build_ggpk(files, out / "Content.ggpk")
    (out / "truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return truth


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "work/fake_torrent")
    t = make_fake_torrent(target, poe2=P2 if Path(P2).exists() else None)
    print("Fake GGPK created at", target / "Content.ggpk")
    print("modified:", t["modified"][0]["paths"][:2], "| deleted:", t["deleted"][:2])

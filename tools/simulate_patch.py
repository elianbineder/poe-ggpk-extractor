"""Simulate a patch on real bundles and measure the accuracy of the aligner.

Usage:
    python tools/simulate_patch.py [bundles|random] [seed] [--no-schema]

    bundles: comma-separated names (e.g. "Tiny_9,Folders/A/data.datc64")
             or "random" for 150 random bundles.

Simulated changes per file: 3 % get a new file inserted before them (taken from PoE2),
3 % are deleted and 10 % are modified. Modified tables are replaced by the same table in
another language (valid structure, different content); other files use their PoE2 version
or a byte splice. As in real bundles, identical content is never repeated.
"""
import collections
import random
import sys
import time

from poe_ggpk import PoEFileSystem, Schema
from poe_ggpk.reconstruct import Sizer, align
from poe_ggpk.reference import Reference, digest

P1 = r"C:/Program Files (x86)/Grinding Gear Games/Path of Exile"
P2 = r"C:/Program Files (x86)/Grinding Gear Games/Path of Exile 2"
LANGS = ["french", "german", "spanish", "russian", "portuguese", "japanese", "korean", "thai",
         "traditional chinese"]
TABLES = ("datc64", "datcl64")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
rng = random.Random(int(args[1]) if len(args) > 1 else 7)
ref = Reference.from_location(P1)
sizer = None if "--no-schema" in sys.argv else Sizer(Schema.load(), 1, ref)
fs2 = PoEFileSystem(P2)
_missing = set(fs2.missing_bundles())
p2_files = [f for f in fs2.index.files
            if f.path and 200 < f.size < 2_000_000 and f.bundle_index not in _missing]


def poe2_version(path: str) -> bytes | None:
    p = path.replace("data/", "data/balance/", 1) if path.startswith("data/") else path
    return fs2.read(p) if fs2.index.get(p) else None


def other_language_version(path: str, bundle: int, existing: set[bytes]) -> bytes | None:
    """The same table in another language and another bundle, without repeating existing content."""
    name = path.rsplit("/", 1)[-1]
    original = ref.fs.read(path)
    for lang in rng.sample(LANGS, len(LANGS)):
        cand = f"data/{lang}/{name}"
        rec = ref.fs.index.get(cand)
        if cand != path and rec is not None and rec.bundle_index != bundle:
            data = ref.fs.read(cand)
            if data != original and digest(data) not in existing:
                return data
    return None


def mutate(old: bytes, segs, bundle: int):
    existing = {s.digest for s in segs}
    new = bytearray()
    truth = []  # (offset, size, paths | None, kind)
    for s in segs:
        content = old[s.offset:s.offset + s.size]
        r = rng.random()
        if r < 0.03 and s.size:
            extra = fs2.read(rng.choice(p2_files).path)
            if digest(extra) not in existing:
                truth.append((len(new), len(extra), None, "new"))
                new += extra
        if r > 0.97:
            continue
        if 0.03 <= r < 0.13 and s.size > 64:
            is_table = s.paths[0].endswith(TABLES)
            alt = other_language_version(s.paths[0], bundle, existing) if is_table else poe2_version(s.paths[0])
            if alt is None and is_table:  # no valid version: the table stays the same
                alt = content
            if alt is None or (alt == content and not is_table):
                cut = rng.randint(16, s.size - 16)
                alt = (content[:cut] + rng.randbytes(rng.randint(1, max(2, s.size // 3)))
                       + content[cut + rng.randint(0, s.size - cut):])
            kind = "unchanged" if alt == content else "modified"
            truth.append((len(new), len(alt), s.paths, kind))
            new += alt
        else:
            truth.append((len(new), len(content), s.paths, "unchanged"))
            new += content
    return bytes(new), truth


names = args[0].split(",") if args and args[0] != "random" else None
bundles = [ref.by_name[n.lower()] for n in names] if names else rng.sample(range(len(ref.index.bundles)), 150)
score: collections.Counter = collections.Counter()
t0 = time.time()
for bi in bundles:
    segs = ref.segments(bi)
    new, truth = mutate(ref.fs._bundle(bi).read(), segs, bi)
    pieces = align(new, segs, sizer)
    got = {p.segment.paths[0]: p for p in pieces if p.segment}
    unknown = {(p.offset, p.size) for p in pieces if p.segment is None}
    for off, size, paths, kind in truth:
        if paths is None:
            score[f"{kind}: " + ("unnamed, exact boundaries" if (off, size) in unknown else "merged with another")] += 1
            continue
        g = got.get(paths[0])
        if g is None:
            score[f"{kind}: not located"] += 1
        elif size == 0 or (g.offset, g.size) == (off, size):
            score[f"{kind}: correct ({g.status})"] += 1
        else:
            score[f"{kind}: WRONG ({g.status})"] += 1
    true_paths = {p[0] for _, _, p, _ in truth if p}
    score["deleted files wrongly assigned"] += sum(
        1 for p in pieces if p.segment and p.segment.size and p.segment.paths[0] not in true_paths)

print(f"{len(bundles)} bundles in {time.time() - t0:.0f}s")
for k, v in sorted(score.items()):
    print(f"  {k:<45} {v:>8,}")

"""Community schema for .datc64 tables (poe-tool-dev/dat-schema).

.datc64 files do not describe their columns: the schema is maintained by the
community at https://github.com/poe-tool-dev/dat-schema and published as
``schema.min.json``. This module downloads it, caches it and exposes it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SCHEMA_URL = "https://github.com/poe-tool-dev/dat-schema/releases/download/latest/schema.min.json"
SCHEMA_FORMAT_VERSION = 7

# "validFor" bits in the schema.
VALID_POE1 = 1
VALID_POE2 = 2

# Size in bytes of every scalar type in 64-bit files.
SCALAR_SIZES = {
    "bool": 1,
    "i8": 1,
    "u8": 1,
    "i16": 2,
    "u16": 2,
    "i32": 4,
    "u32": 4,
    "f32": 4,
    "i64": 8,
    "u64": 8,
    "f64": 8,
    "string": 8,      # offset into the variable section
    "row": 8,         # row index in the same table
    "foreignrow": 16,  # row index + 8 unknown bytes
    "enumrow": 4,     # index into an enumeration
    "array": 16,      # array of unknown type
}
ARRAY_SIZE = 16  # uint64 count + uint64 offset


def default_cache_dir() -> Path:
    base = os.environ.get("POE_GGPK_CACHE")
    if base:
        return Path(base)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "poe-ggpk" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "poe-ggpk"


@dataclass(slots=True)
class Column:
    name: str | None
    type: str
    array: bool = False
    interval: bool = False
    references: str | None = None
    localized: bool = False
    unique: bool = False
    description: str | None = None

    @property
    def size(self) -> int:
        if self.array:
            return ARRAY_SIZE
        base = SCALAR_SIZES.get(self.type)
        if base is None:
            raise ValueError(f"Unknown column type: {self.type}")
        return base * (2 if self.interval else 1)


@dataclass(slots=True)
class Table:
    name: str
    valid_for: int
    columns: list[Column]
    tags: list[str]

    @property
    def row_size(self) -> int:
        return sum(c.size for c in self.columns)


@dataclass(slots=True)
class Enumeration:
    name: str
    valid_for: int
    indexing: int
    enumerators: list[str | None]


class Schema:
    def __init__(self, raw: dict):
        if raw.get("version") != SCHEMA_FORMAT_VERSION:
            # Try anyway: format changes are usually compatible.
            self.warning = (f"The schema has format version {raw.get('version')}, "
                            f"this tool was written for version {SCHEMA_FORMAT_VERSION}")
        else:
            self.warning = None
        self.created_at = raw.get("createdAt")
        self.tables: list[Table] = []
        for t in raw["tables"]:
            cols = [
                Column(
                    name=c.get("name"),
                    type=c["type"],
                    array=bool(c.get("array")),
                    interval=bool(c.get("interval")),
                    references=(c.get("references") or {}).get("table"),
                    localized=bool(c.get("localized")),
                    unique=bool(c.get("unique")),
                    description=c.get("description"),
                )
                for c in t["columns"]
            ]
            self.tables.append(Table(t["name"], t.get("validFor", 3), cols, t.get("tags", [])))
        self.enumerations = {
            e["name"].lower(): Enumeration(e["name"], e.get("validFor", 3), e.get("indexing", 0), e["enumerators"])
            for e in raw.get("enumerations", [])
        }

    def table(self, name: str, game: int = 1) -> Table | None:
        """Find a table (case-insensitive), preferring one valid for ``game``."""
        bit = VALID_POE2 if game == 2 else VALID_POE1
        matches = [t for t in self.tables if t.name.lower() == name.lower()]
        for t in matches:
            if t.valid_for & bit:
                return t
        return matches[0] if matches else None

    def enumeration(self, name: str) -> Enumeration | None:
        return self.enumerations.get(name.lower())

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "Schema":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, refresh: bool = False,
             max_age_hours: float = 24.0) -> "Schema":
        """Load the schema from ``path`` or from the cache (downloading it when needed)."""
        if path:
            return cls.from_file(path)
        cache = default_cache_dir() / "schema.min.json"
        stale = not cache.exists() or (time.time() - cache.stat().st_mtime) > max_age_hours * 3600
        if refresh or stale:
            try:
                download_schema(cache)
            except Exception:
                if not cache.exists():
                    raise
        return cls.from_file(cache)


def download_schema(target: Path, url: str = SCHEMA_URL) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "poe-ggpk-extractor"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    json.loads(data)  # validate before replacing the cache
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target

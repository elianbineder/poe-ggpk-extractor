"""Conversion of game formats to common formats.

* ``.dds`` textures -> PNG (requires Pillow).
* UTF-16LE texts (``.txt``, ``.ot``, ``.it``, ``.ais``...) -> str.
"""

from __future__ import annotations

import codecs
import io

# sRGB DXGI formats Pillow does not implement, mapped to their linear equivalent
# (the compressed blocks are identical; only the colour interpretation differs).
_SRGB_TO_UNORM = {
    29: 28,   # R8G8B8A8_UNORM_SRGB -> R8G8B8A8_UNORM
    72: 71,   # BC1_UNORM_SRGB -> BC1_UNORM
    75: 74,   # BC2_UNORM_SRGB -> BC2_UNORM
    78: 77,   # BC3_UNORM_SRGB -> BC3_UNORM
    91: 87,   # B8G8R8A8_UNORM_SRGB -> B8G8R8A8_UNORM
    93: 88,   # B8G8R8X8_UNORM_SRGB -> B8G8R8X8_UNORM
    99: 98,   # BC7_UNORM_SRGB -> BC7_UNORM
}


class ConversionError(Exception):
    pass


def dds_to_png(data: bytes) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise ConversionError("Pillow is required to convert DDS: pip install pillow") from exc
    if data[:4] != b"DDS ":
        # Some .dds files are "redirects": text with '*' + path to another texture.
        raise ConversionError("The file does not start with the DDS signature")
    if data[84:88] == b"DX10" and len(data) >= 132:
        fmt = int.from_bytes(data[128:132], "little")
        if fmt in _SRGB_TO_UNORM:
            data = data[:128] + _SRGB_TO_UNORM[fmt].to_bytes(4, "little") + data[132:]
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ConversionError(f"Pillow could not decode the texture: {exc}") from exc
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def decode_text(data: bytes) -> str:
    """Decode game texts (UTF-16LE with or without BOM, or UTF-8)."""
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace")
    # Heuristic: many null bytes at odd positions => UTF-16LE without BOM.
    sample = data[:512]
    if len(sample) >= 2 and sample[1::2].count(0) > len(sample) // 4:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")

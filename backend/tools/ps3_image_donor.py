#!/usr/bin/env python3
"""Extract native PS3 GfxImages (root + pixel payload) from a Retail PS3 zone.

Why this exists
---------------
A CoD4 PS3 map zone owns every texture it uses; there is no IWD on the console and
no cross-map asset sharing.  When the PC source installation is incomplete, the
porter has nothing to convert and emits a ``,name`` reference that cannot resolve
at load time.  The Retail PS3 map zones, however, already contain thousands of
*fully converted* GfxImages - correct PS3 format codes, PS3 mip layout, PS3
resolutions.  Reusing those bytes is strictly better than converting a PC IWI:
it is the exact data Infinity Ward shipped for this platform.

Serialization (measured on Retail mp_crash.ff / mp_shipment.ff)
--------------------------------------------------------------
GfxImage root = 0x34 bytes, big endian::

    +0x00 int   mapType            3 = 2D, 5 = cubemap
    +0x04 u8    ps3 format         0x85 A8R8G8B8, 0x86 DXT1, 0x87 DXT3, 0x88 DXT5, 0x9E ...
    +0x05 u8    levelCount
    +0x06 u8    2                  constant
    +0x07 u8    cubemap flag
    +0x08 u32   0x0001AAE4         constant magic - the anchor this scanner uses
    +0x0C u16x3 width, height, depth
    +0x1C u8    semantic
    +0x20 u32   resource byte count (already 0x80 padded)
    +0x24 u16x3 width, height, depth (repeated)
    +0x2A u8    pool
    +0x2B u8    delayLoadPixels
    +0x2C u32   resource pointer   FOLLOWING when the payload is in the stream
    +0x30 u32   name pointer       FOLLOWING - the NUL string follows the root

Pixel payloads of delayed images are not inline.  They are appended to one
contiguous FIFO at the tail of the zone, in the order the image roots appear in
the stream, with no padding between them (every declared size is already a
multiple of 0x80).  ``locate_fifo`` reconstructs that region from the sum of the
declared sizes and the last non-zero byte of the zone; ``verify`` proves the
reconstruction by re-deriving the mip chain size of every extracted image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.backend.v4_ffio import read_ps3_fastfile  # noqa: E402

MAGIC = b"\x00\x01\xAA\xE4"
ROOT_SIZE = 0x34
FOLLOWING = 0xFFFFFFFF
RESOURCE_ALIGN = 0x80

# PS3 texture format code -> (bytes per 4x4 block, or 0 for linear, bytes per pixel)
FORMATS = {
    0x85: ("argb8", 0, 4),
    0x86: ("dxt1", 8, 0),
    0x87: ("dxt3", 16, 0),
    0x88: ("dxt5", 16, 0),
}


@dataclass(frozen=True)
class DonorImage:
    name: str
    root_offset: int
    map_type: int
    ps3_format: int
    format_name: str
    level_count: int
    face_count: int
    semantic: int
    width: int
    height: int
    depth: int
    pool: int
    resource_bytes: int
    fifo_index: int
    resource_offset: int
    resource_sha256: str

    @property
    def is_cubemap(self) -> bool:
        return self.face_count == 6


def _mip_chain_bytes(fmt: int, width: int, height: int, levels: int, faces: int) -> int | None:
    """Expected payload size, or None for a format whose layout is not modelled."""
    entry = FORMATS.get(fmt)
    if entry is None:
        return None
    _, block, bpp = entry
    per_face = 0
    w, h = width, height
    for _ in range(levels):
        if block:
            per_face += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
        else:
            per_face += w * h * bpp
        w = max(1, w // 2)
        h = max(1, h // 2)
    if faces > 1:
        # Measured on Retail *reflection_probe*: a cubemap pads EVERY face to the
        # 0x80 resource alignment, so 6 x 21844 declares 131328 and not 131064.
        per_face = (per_face + RESOURCE_ALIGN - 1) & ~(RESOURCE_ALIGN - 1)
    return per_face * faces


def scan_roots(zone: bytes) -> list[tuple[int, dict]]:
    """Every structurally valid GfxImage root in stream order."""
    out: list[tuple[int, dict]] = []
    i = zone.find(MAGIC)
    while i != -1:
        r = i - 8
        if r >= 0 and r + ROOT_SIZE <= len(zone):
            row = _parse_root(zone, r)
            if row is not None:
                out.append((r, row))
        i = zone.find(MAGIC, i + 1)
    return out


def _parse_root(zone: bytes, r: int) -> dict | None:
    map_type = struct.unpack_from(">i", zone, r)[0]
    if map_type not in (3, 5):
        return None
    fmt = zone[r + 4]
    if fmt not in FORMATS and fmt != 0x9E:
        return None
    if zone[r + 6] != 2:
        return None
    levels = zone[r + 5]
    if not 1 <= levels <= 16:
        return None
    w, h, d = struct.unpack_from(">HHH", zone, r + 0x0C)
    w2, h2, d2 = struct.unpack_from(">HHH", zone, r + 0x24)
    if (w, h, d) != (w2, h2, d2) or not w or not h or d != 1:
        return None
    size = struct.unpack_from(">I", zone, r + 0x20)[0]
    if size == 0 or size % RESOURCE_ALIGN:
        return None
    delay = zone[r + 0x2B]
    if delay not in (0, 1):
        return None
    res_ptr, name_ptr = struct.unpack_from(">II", zone, r + 0x2C)
    if name_ptr != FOLLOWING:
        return None
    end = zone.find(b"\0", r + ROOT_SIZE, r + ROOT_SIZE + 160)
    if end == -1 or end == r + ROOT_SIZE:
        return None
    raw = zone[r + ROOT_SIZE:end]
    try:
        name = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if any(c < 0x20 or c > 0x7E for c in raw):
        return None
    return {
        "name": name,
        "map_type": map_type,
        "ps3_format": fmt,
        "level_count": levels,
        "face_count": 6 if zone[r + 7] else 1,
        "semantic": zone[r + 0x1C],
        "width": w, "height": h, "depth": d,
        "pool": zone[r + 0x2A],
        "resource_bytes": size,
        "delay_load_pixels": delay,
        "resource_pointer": res_ptr,
    }


def locate_fifo(zone: bytes, roots: list[tuple[int, dict]]) -> tuple[int, int]:
    """(start, total) of the contiguous delayed-pixel FIFO at the tail of the zone."""
    total = sum(row["resource_bytes"] for _, row in roots
                if row["delay_load_pixels"] == 1 and row["resource_pointer"] == FOLLOWING)
    last = len(zone)
    while last > 0 and zone[last - 1] == 0:
        last -= 1
    # The FIFO is the last thing in the stream; trailing zero bytes belong to the
    # 64-KiB container padding, not to a payload.  Round the end back up to the
    # payload alignment so a payload that legitimately ends in NUL bytes is kept.
    end = min(len(zone), (last + RESOURCE_ALIGN - 1) & ~(RESOURCE_ALIGN - 1))
    return end - total, total


def extract(path: str | Path) -> tuple[list[DonorImage], dict]:
    zone = read_ps3_fastfile(path).zone
    roots = scan_roots(zone)
    start, total = locate_fifo(zone, roots)
    images: list[DonorImage] = []
    payloads: dict[str, bytes] = {}
    cursor = start
    fifo_index = 0
    for offset, row in roots:
        if not (row["delay_load_pixels"] == 1 and row["resource_pointer"] == FOLLOWING):
            continue
        size = row["resource_bytes"]
        blob = zone[cursor:cursor + size]
        if len(blob) != size:
            raise ValueError(f"FIFO underrun at {row['name']!r}")
        images.append(DonorImage(
            name=row["name"], root_offset=offset, map_type=row["map_type"],
            ps3_format=row["ps3_format"],
            format_name=FORMATS.get(row["ps3_format"], ("unknown", 0, 0))[0],
            level_count=row["level_count"], face_count=row["face_count"],
            semantic=row["semantic"], width=row["width"], height=row["height"],
            depth=row["depth"], pool=row["pool"], resource_bytes=size,
            fifo_index=fifo_index, resource_offset=cursor,
            resource_sha256=hashlib.sha256(blob).hexdigest().upper(),
        ))
        payloads[row["name"]] = blob
        cursor += size
        fifo_index += 1
    info = {
        "zone": str(path), "zone_bytes": len(zone), "image_roots": len(roots),
        "delayed_images": len(images), "fifo_start": start, "fifo_bytes": total,
        "fifo_end": cursor,
    }
    return images, {"info": info, "payloads": payloads}


def verify(images: list[DonorImage]) -> dict:
    """Re-derive each declared payload size from the format and the mip chain."""
    checked = mismatched = unmodelled = 0
    bad: list[str] = []
    for image in images:
        expected = _mip_chain_bytes(image.ps3_format, image.width, image.height,
                                    image.level_count, image.face_count)
        if expected is None:
            unmodelled += 1
            continue
        padded = (expected + RESOURCE_ALIGN - 1) & ~(RESOURCE_ALIGN - 1)
        checked += 1
        if padded != image.resource_bytes:
            mismatched += 1
            if len(bad) < 20:
                bad.append(f"{image.name}: declared {image.resource_bytes}, mip chain {padded}")
    return {"checked": checked, "mismatched": mismatched, "unmodelled_format": unmodelled,
            "examples": bad}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="extract native PS3 GfxImages from a Retail zone")
    ap.add_argument("zones", nargs="+")
    ap.add_argument("--out", help="write the image index as JSON here")
    ap.add_argument("--dump", help="write every payload into this directory as <name>.bin")
    ap.add_argument("--want", help="file with one required image name per line; report coverage")
    ns = ap.parse_args(argv)

    index: dict[str, dict] = {}
    reports = []
    store: dict[str, bytes] = {}
    for path in ns.zones:
        images, extra = extract(path)
        report = verify(images)
        report.update(extra["info"])
        reports.append(report)
        for image in images:
            index.setdefault(image.name, {**image.__dict__, "zone": str(path)})
        for name, blob in extra["payloads"].items():
            store.setdefault(name, blob)
        print(f"{Path(path).name}: {len(images)} delayed images, FIFO "
              f"{extra['info']['fifo_start']}..{extra['info']['fifo_end']}, "
              f"size check {report['checked'] - report['mismatched']}/{report['checked']} ok, "
              f"{report['unmodelled_format']} unmodelled")
        for line in report["examples"]:
            print("   !", line)

    if ns.want:
        want = [l.strip() for l in Path(ns.want).read_text(encoding="utf-8").splitlines() if l.strip()]
        have = [w for w in want if w in index]
        print(f"\ncoverage: {len(have)}/{len(want)} requested names available from these zones")
        for name in want:
            if name not in index:
                print("   missing:", name)

    if ns.dump:
        d = Path(ns.dump)
        d.mkdir(parents=True, exist_ok=True)
        for name, blob in store.items():
            safe = name.replace("/", "%2F").replace("\\", "%5C")
            (d / f"{safe}.bin").write_bytes(blob)
        print(f"\nwrote {len(store)} payloads to {d}")

    if ns.out:
        Path(ns.out).write_text(json.dumps({"zones": reports, "images": index}, indent=2, default=str),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

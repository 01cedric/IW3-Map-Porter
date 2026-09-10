#!/usr/bin/env python3
"""Measure which shared Material names a PS3 map may actually reference.

Why this exists
---------------
Same reason as the TechniqueSet catalog, one asset family over.  A PC map zone happily
writes ``,mc/mtl_marine_glove`` because the PC installation resolves it out of another
installed zone.  On PS3 the map runs with ``common_mp.ff``, ``code_post_gfx_mp.ff``,
``code_pre_gfx_mp.ff`` and ``ui_mp.ff`` loaded and nothing else - no other map zone is
ever resident - so a name that only exists inside a Retail *map* zone cannot be found.

A PS3 ``Material`` root is 0x80 bytes; the XString name pointer is at +0x00 and, being
FOLLOWING, the name is serialized directly behind the root.  The four pointers at
+0x70 (techset) and +0x74/+0x78/+0x7C (texture / constant / state-bits tables) must all
be NULL, FOLLOWING, INSERT or a packed pointer inside a declared block, which is what
separates a real root from a coincidental 0xFFFFFFFF inside pixel data.

Usage
-----
    python tools/ps3_material_catalog.py common_mp.ff code_post_gfx_mp.ff ui_mp.ff \\
        [--merge] [--out cod4porter/reference/ps3_shared_material_availability.json]

Pass ONLY zones that stay loaded while a map runs.  Adding a Retail map zone would
record names this map can never resolve, which is exactly the mistake this catalog
exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.backend.v4_ffio import (  # noqa: E402
    parse_xasset_list,
    parse_zone_header,
    read_ps3_fastfile,
)

DEFAULT_OUT = ROOT / "cod4porter" / "reference" / "ps3_shared_material_availability.json"
MATERIAL_ROOT = 0x80


def _valid_pointer(word: int, blocks) -> bool:
    if word in (0, 0xFFFFFFFF, 0xFFFFFFFE):
        return True
    encoded = (word - 1) & 0xFFFFFFFF
    block, offset = encoded >> 29, encoded & 0x1FFFFFFF
    return block < 7 and blocks[block] > 0 and offset < blocks[block]


def scan(path: Path) -> tuple[set[str], dict]:
    document = read_ps3_fastfile(path)
    zone = document.zone
    blocks = parse_zone_header(zone, "ps3").block_sizes
    declared = sum(1 for a in parse_xasset_list(zone, "ps3").assets if a.type_name == "material")
    names: set[str] = set()
    cursor = 0
    while True:
        root = zone.find(b"\xff\xff\xff\xff", cursor)
        if root < 0 or root + MATERIAL_ROOT + 2 > len(zone):
            break
        cursor = root + 1
        pointers = struct.unpack_from(">4I", zone, root + 0x70)
        if not all(_valid_pointer(word, blocks) for word in pointers):
            continue
        end = zone.find(b"\0", root + MATERIAL_ROOT, root + MATERIAL_ROOT + 130)
        if end <= root + MATERIAL_ROOT:
            continue
        raw = zone[root + MATERIAL_ROOT:end]
        if not all(0x20 < byte < 0x7F for byte in raw):
            continue
        try:
            names.add(raw.decode("ascii").lstrip(","))
        except UnicodeDecodeError:
            continue
    return names, {
        "path": str(path),
        "zone_bytes": len(zone),
        "declared_material_xassets": declared,
        "material_names": len(names),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="measure PS3 shared Material availability")
    ap.add_argument("zones", nargs="+", help="always-loaded PS3 zones from the target installation")
    ap.add_argument("--merge", action="store_true", help="keep names already in the catalog")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ns = ap.parse_args(argv)

    names: set[str] = set()
    scanned = []
    out = Path(ns.out)
    if ns.merge and out.is_file():
        names |= set(json.loads(out.read_text(encoding="utf-8")).get("names", ()))
    for raw in ns.zones:
        path = Path(raw)
        stem = path.stem.casefold()
        if stem.startswith("mp_") or stem.startswith("sp_"):
            print(f"refusing {path.name}: a map zone is never loaded alongside another map")
            return 2
        found, info = scan(path)
        names |= found
        scanned.append(info)
        print(f"{path.name}: {len(found)} material names ({info['declared_material_xassets']} declared)")

    # ``default`` is guaranteed: every zone above carries it, and IW3 itself falls back
    # to it.  It is added explicitly because its root is written by code, not by the
    # material compiler, and does not always match the structural signature above.
    names.add("default")
    out.write_text(json.dumps({
        "schema": "iw3-ps3-shared-material-availability/v1",
        "note": ("a name here is owned by a zone that stays loaded while a PS3 map runs; "
                 "a shared Material reference outside this catalog cannot be resolved"),
        "zones_scanned": scanned,
        "names": sorted(names),
    }, indent=1), encoding="utf-8")
    print(json.dumps({"out": str(out), "zones_scanned": len(scanned), "names": len(names)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the PS3 TechniqueSet availability catalog from real PS3 zone files.

Why this exists
---------------
The porter never converts a shader graph.  A PC ``MaterialTechniqueSet`` becomes a
``,name`` reference that the console resolves through ``DB_FindXAssetHeader``.  If the
name does not exist on the target installation the reference cannot resolve, and IW3
reports nothing - the loading screen simply stops.

The PC and PS3 technique inventories are *not* the same.  Measured on a real PS3
installation, both shipping Retail PS3 map zones reference only ~30 technique names
each, and neither references a single ``_hsm_`` (hard shadow map) technique, while a
PC CoD4 map like mp_getaway declares 238 TechniqueSets including whole ``hsm``,
``skin``, ``flag`` and detail-map families.  Emitting those names verbatim is what
makes a generated map fail on hardware.

This tool measures what is actually available and writes
``cod4porter/reference/ps3_technique_availability.json``.  A name qualifies when it is

* owned by an installed support zone (``common_mp.ff``, ``code_post_gfx_mp.ff``,
  ``code_pre_gfx_mp.ff``, ``ui_mp.ff`` ...), or
* referenced as ``,name`` by a shipping Retail PS3 map zone - proof that a stock map
  resolves it on that installation.

Feed it every PS3 zone you have.  More zones can only make the catalog more complete,
and the porter fails closed with an exact list of names that are still missing.

Usage
-----
    python tools/ps3_technique_catalog.py ZONE.ff [ZONE.ff ...] [--merge] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import re
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

DEFAULT_OUT = ROOT / "cod4porter" / "reference" / "ps3_technique_availability.json"

# The closed IW3 technique-name grammar, including the PS3 ``m_``/``w_`` spellings.
GRAMMAR = re.compile(
    r"^(?:(?:m|mc|w|wc)_)?(?:"
    r"l_(?:(?:hsm|sm)_)?(?:flag_|skin_|scroll_)?[brt]0c0(?:d0)?(?:n0)?(?:s0)?"
    r"|unlit(?:_(?:add|multiply|replace|alphatest|distfalloff|falloff))*"
    r"|ambient_t0c0|shadowcaster|sky|water|threed_cinematic"
    r"|effect(?:_(?:zfeather|falloff|add|nofog|eyeoffset))*"
    r"|distortion_scale(?:_zfeather)?"
    r"|particle_cloud(?:_outdoor)?(?:_add)?"
    r")$"
)
EXTRA_NAMES = {"2d", "cinematic", "tools", "depthprepass", "shadowclear"}

PS3_TECHSET_ROOT = 0x70
PS3_TECHSET_SLOTS = 26


def _valid_pointer(word: int, blocks) -> bool:
    """NULL / FOLLOWING / INSERT, or a packed pointer inside a declared block."""
    if word in (0, 0xFFFFFFFF, 0xFFFFFFFE):
        return True
    encoded = (word - 1) & 0xFFFFFFFF
    block, offset = encoded >> 29, encoded & 0x1FFFFFFF
    return block < 7 and blocks[block] > 0 and offset < blocks[block]


def is_technique(name: str) -> bool:
    return bool(GRAMMAR.match(name)) or name.casefold() in EXTRA_NAMES


def classify(path: Path) -> str:
    stem = path.stem.casefold()
    return "retail_map" if stem.startswith("mp_") or stem.startswith("sp_") else "support"


def scan(path: Path) -> tuple[dict[str, set[str]], dict]:
    """Locate MaterialTechniqueSet roots structurally, not by string heuristics.

    A PS3 IW3 ``MaterialTechniqueSet`` is 0x70 bytes: a FOLLOWING name pointer, four
    scalar bytes (worldVertFormat, hasBeenUploaded, two zero pad bytes) and 26 technique
    pointers starting at +8.  The XString follows the root directly, and a ``,`` prefix
    marks a reference to a set that lives in another zone.  Validating every technique
    pointer against the zone's own declared block sizes is what separates a real root
    from a coincidental 0xFFFFFFFF inside pixel data.
    """
    document = read_ps3_fastfile(path)
    zone = document.zone
    blocks = parse_zone_header(zone, "ps3").block_sizes
    assets = parse_xasset_list(zone, "ps3")
    declared = sum(1 for a in assets.assets if a.type_name == "techset")
    kind = classify(path)
    found: dict[str, set[str]] = {}
    cursor = 0
    while True:
        root = zone.find(b"\xff\xff\xff\xff", cursor)
        if root < 0 or root + PS3_TECHSET_ROOT + 2 > len(zone):
            break
        cursor = root + 1
        if zone[root + 4] > 8 or zone[root + 5] > 1 or zone[root + 6:root + 8] != b"\0\0":
            continue
        slots = struct.unpack_from(f">{PS3_TECHSET_SLOTS}I", zone, root + 8)
        if not all(_valid_pointer(word, blocks) for word in slots):
            continue
        end = zone.find(b"\0", root + PS3_TECHSET_ROOT, root + PS3_TECHSET_ROOT + 130)
        if end <= root + PS3_TECHSET_ROOT:
            continue
        raw = zone[root + PS3_TECHSET_ROOT:end]
        if not all(32 < byte < 127 for byte in raw):
            continue
        name = raw.decode("ascii")
        bare = name.lstrip(",")
        if not is_technique(bare):
            continue
        role = "referenced" if name.startswith(",") else "owned"
        found.setdefault(bare, set()).add(f"{kind}:{path.stem}:{role}")
    return found, {
        "path": str(path),
        "kind": kind,
        "zone_bytes": len(zone),
        "declared_techset_xassets": declared,
        "technique_names": len(found),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="measure PS3 TechniqueSet availability")
    ap.add_argument("zones", nargs="+", help="PS3 .ff files from the target installation")
    ap.add_argument("--merge", action="store_true", help="keep the entries already in the catalog")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ns = ap.parse_args(argv)

    catalog: dict[str, set[str]] = {}
    out_path = Path(ns.out)
    if ns.merge and out_path.is_file():
        for entry in json.loads(out_path.read_text(encoding="utf-8")).get("entries", ()):
            catalog[str(entry["name"])] = set(entry.get("sources", ()))

    scanned = []
    for path in (Path(p) for p in ns.zones):
        if not path.is_file():
            print(f"skip (not found): {path}", file=sys.stderr)
            continue
        try:
            found, info = scan(path)
        except Exception as exc:  # noqa: BLE001
            print(f"skip ({exc}): {path}", file=sys.stderr)
            continue
        for name, sources in found.items():
            catalog.setdefault(name, set()).update(sources)
        scanned.append(info)

    document = {
        "schema": "iw3-ps3-technique-availability/v1",
        "note": (
            "a name here is available to a PS3 map zone: it is either owned by an installed "
            "support zone or referenced by a shipping Retail PS3 map zone"
        ),
        "zones_scanned": scanned,
        "entries": [
            {"name": name, "sources": sorted(catalog[name])}
            for name in sorted(catalog, key=str.casefold)
        ],
    }
    out_path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({
        "out": str(out_path),
        "zones_scanned": len(scanned),
        "technique_names": len(catalog),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

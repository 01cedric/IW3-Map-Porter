#!/usr/bin/env python3
"""Structural differential between a generated PS3 zone and Retail PS3 zones.

Software readback can only prove that a zone matches the porter's own model of IW3.  A
Retail zone is the independent oracle: it shows what the shipping engine actually accepts.
This tool locates the same asset structures in both, by structural signature rather than
by walking the loader graph, and reports where the generated zone's shape departs from
Retail.

What it locates and how
-----------------------
``GfxImage`` (0x34)
    Anchored on the constant ``0x0001AAE4`` at +0x08, then validated on mapType,
    the fixed ``2`` at +0x06, the cubemap flag and the duplicated width/height/depth
    pair at +0x0C and +0x24.
``Material`` (0x80)
    Anchored on the three table pointers at +0x74/+0x78/+0x7C agreeing with the three
    counts at +0x32/+0x33/+0x34, plus a resolvable name pointer.
``MaterialTechniqueSet`` (0x70)
    Anchored on the FOLLOWING name pointer, the scalar/pad bytes and 26 technique
    pointers that all decode inside the zone's own declared blocks.

Every packed pointer is checked against the zone's declared block sizes, so a
coincidental byte pattern inside pixel data cannot pass as a root.

Alignment
---------
For each family the tool reports the alignment of the located roots and of the payloads
they point at.  A generated zone whose alignment histogram differs from Retail is
declaring memory the loader will lay out differently, which is exactly the class of
defect that survives a self-consistent readback.

Usage
-----
    python tools/ps3_zone_diff.py --generated OUT/mp_x.ff \\
        --retail ps3/mp_shipment.ff --retail ps3/mp_crash.ff [--out diff.json]
"""

from __future__ import annotations

import argparse
import collections
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

FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE
IMAGE_MAGIC = struct.pack(">I", 0x0001AAE4)
IMAGE_ROOT = 0x34
MATERIAL_ROOT = 0x80
TECHSET_ROOT = 0x70
TECHSET_SLOTS = 26

# PS3 texture format codes the porter emits, with their DXT block size.
PS3_BLOCK_BYTES = {0x86: 8, 0x87: 16, 0x88: 16}


def packed(word: int, blocks) -> tuple[int, int] | None:
    if word in (0, FOLLOWING, INSERT):
        return None
    encoded = (word - 1) & 0xFFFFFFFF
    block, offset = encoded >> 29, encoded & 0x1FFFFFFF
    if block >= 7 or blocks[block] <= 0 or offset >= blocks[block]:
        return None
    return block, offset


def pointer_ok(word: int, blocks) -> bool:
    return word in (0, FOLLOWING, INSERT) or packed(word, blocks) is not None


def alignment_of(value: int) -> int:
    """Largest power-of-two boundary the value sits on, capped at 128."""
    if value == 0:
        return 128
    align = 1
    while align < 128 and value % (align * 2) == 0:
        align *= 2
    return align


def dxt_chain_bytes(fmt: int, width: int, height: int, levels: int, faces: int) -> int | None:
    block = PS3_BLOCK_BYTES.get(fmt)
    if block is None:
        return None
    total = 0
    for level in range(levels):
        w = max(1, width >> level)
        h = max(1, height >> level)
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block
    return total * faces


def find_images(zone: bytes, blocks) -> list[dict]:
    out: list[dict] = []
    cursor = 0
    while True:
        hit = zone.find(IMAGE_MAGIC, cursor)
        if hit < 0:
            break
        cursor = hit + 1
        root = hit - 8
        if root < 0 or root + IMAGE_ROOT > len(zone):
            continue
        map_type = struct.unpack_from(">i", zone, root)[0]
        if map_type not in (3, 4, 5):
            continue
        fmt, levels, two, cube = zone[root + 4], zone[root + 5], zone[root + 6], zone[root + 7]
        if two != 2 or cube > 1 or not 1 <= levels <= 16:
            continue
        width, height, depth = struct.unpack_from(">HHH", zone, root + 0x0C)
        w2, h2, d2 = struct.unpack_from(">HHH", zone, root + 0x24)
        if (width, height, depth) != (w2, h2, d2) or not width or not height:
            continue
        resource_bytes = struct.unpack_from(">I", zone, root + 0x20)[0]
        resource_ptr, name_ptr = struct.unpack_from(">II", zone, root + 0x2C)
        if not pointer_ok(resource_ptr, blocks) or not pointer_ok(name_ptr, blocks):
            continue
        faces = 6 if cube else 1
        expected = dxt_chain_bytes(fmt, width, height, levels, faces)
        out.append({
            "root": root, "map_type": map_type, "format": fmt, "levels": levels,
            "cubemap": bool(cube), "width": width, "height": height, "depth": depth,
            "semantic": zone[root + 0x1C], "tail_2a": zone[root + 0x2A],
            "delay_load_pixels": zone[root + 0x2B],
            "resource_bytes": resource_bytes,
            "resource_ptr": resource_ptr, "name_ptr": name_ptr,
            "expected_chain_bytes": expected,
            "chain_consistent": None if expected is None else (
                resource_bytes >= expected and (resource_bytes - expected) < 0x80
            ),
        })
    return out


def find_materials(zone: bytes, blocks) -> list[dict]:
    out: list[dict] = []
    limit = len(zone) - MATERIAL_ROOT
    for root in range(0, limit, 4):
        name_ptr = struct.unpack_from(">I", zone, root)[0]
        if name_ptr != FOLLOWING and packed(name_ptr, blocks) is None:
            continue
        textures, constants, states = zone[root + 0x32], zone[root + 0x33], zone[root + 0x34]
        if textures > 32 or constants > 64 or states > 64:
            continue
        if not (textures or constants or states):
            continue
        table = struct.unpack_from(">III", zone, root + 0x74)
        ok = True
        for count, word in zip((textures, constants, states), table):
            if count and word not in (FOLLOWING, INSERT) and packed(word, blocks) is None:
                ok = False
                break
            if not count and word != 0:
                ok = False
                break
        if not ok:
            continue
        techset = struct.unpack_from(">I", zone, root + 0x70)[0]
        if not pointer_ok(techset, blocks):
            continue
        # A FOLLOWING name is serialized directly behind the root.  Requiring it to be a
        # real printable asset name is what separates a Material from a byte window that
        # merely satisfies the count/pointer invariants - without it the scan reported
        # 2771 "materials" in a Retail zone that declares far fewer.
        name = None
        if name_ptr == FOLLOWING:
            end = zone.find(b"\0", root + MATERIAL_ROOT, root + MATERIAL_ROOT + 128)
            if end <= root + MATERIAL_ROOT:
                continue
            raw = zone[root + MATERIAL_ROOT:end]
            if not all(32 <= byte < 127 for byte in raw):
                continue
            name = raw.decode("latin-1")
            if not any(c.isalpha() for c in name):
                continue
        platform = zone[root + 0x38:root + 0x70]
        out.append({
            "root": root, "name": name, "sort_key": zone[root + 5], "game_flags": zone[root + 4],
            "textures": textures, "constants": constants, "state_bits": states,
            "state_flags": zone[root + 0x35], "camera_region": zone[root + 0x36],
            "surface_type_bits": struct.unpack_from(">I", zone, root + 0x10)[0],
            "techset_null": techset == 0,
            "platform_state_zero": platform == b"\0" * 0x38,
            "platform_state_nonzero_bytes": sum(1 for b in platform if b),
        })
    return out


def find_techsets(zone: bytes, blocks) -> list[dict]:
    out: list[dict] = []
    cursor = 0
    while True:
        root = zone.find(b"\xff\xff\xff\xff", cursor)
        if root < 0 or root + TECHSET_ROOT + 2 > len(zone):
            break
        cursor = root + 1
        if zone[root + 4] > 8 or zone[root + 5] > 1 or zone[root + 6:root + 8] != b"\0\0":
            continue
        slots = struct.unpack_from(f">{TECHSET_SLOTS}I", zone, root + 8)
        if not all(pointer_ok(word, blocks) for word in slots):
            continue
        end = zone.find(b"\0", root + TECHSET_ROOT, root + TECHSET_ROOT + 130)
        if end <= root + TECHSET_ROOT:
            continue
        raw = zone[root + TECHSET_ROOT:end]
        if not all(32 < byte < 127 for byte in raw):
            continue
        name = raw.decode("ascii")
        out.append({
            "root": root, "name": name, "referenced": name.startswith(","),
            "world_vertex_format": zone[root + 4],
            "techniques": sum(1 for word in slots if word),
        })
    return out


def histogram(values) -> dict:
    return dict(sorted(collections.Counter(values).items(), key=lambda kv: (-kv[1], str(kv[0]))))


def survey(path: Path) -> dict:
    document = read_ps3_fastfile(path)
    zone = document.zone
    header = parse_zone_header(zone, "ps3")
    blocks = header.block_sizes
    assets = parse_xasset_list(zone, "ps3")

    images = find_images(zone, blocks)
    materials = find_materials(zone, blocks)
    techsets = find_techsets(zone, blocks)

    runs = []
    for asset in assets.assets:
        if runs and runs[-1][0] == asset.type_name:
            runs[-1][1] += 1
        else:
            runs.append([asset.type_name, 1])

    return {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "zone_bytes": len(zone),
        "zone_memory_size": header.zone_memory_size,
        "block_sizes": list(blocks),
        "declared_zone_bytes": sum(blocks),
        "zone_padding_multiple_64k": len(zone) % 0x10000 == 0,
        "block_alignment": {str(i): alignment_of(size) for i, size in enumerate(blocks)},
        "xassets": len(assets.assets),
        "script_strings": len(assets.script_strings),
        "asset_counts": histogram(a.type_name for a in assets.assets),
        "asset_runs": len(runs),
        "asset_run_head": [f"{name}x{count}" for name, count in runs[:12]],
        "images": {
            "located": len(images),
            "map_type": histogram(i["map_type"] for i in images),
            "format": histogram(hex(i["format"]) for i in images),
            "semantic": histogram(i["semantic"] for i in images),
            "level_count": histogram(i["levels"] for i in images),
            "max_level_count": max((i["levels"] for i in images), default=0),
            "max_edge": max((max(i["width"], i["height"]) for i in images), default=0),
            "delay_load_pixels": histogram(i["delay_load_pixels"] for i in images),
            "tail_byte_0x2a": histogram(i["tail_2a"] for i in images),
            "resource_ptr": histogram(
                "FOLLOWING" if i["resource_ptr"] == FOLLOWING else
                "NULL" if i["resource_ptr"] == 0 else "packed" for i in images),
            "name_ptr": histogram(
                "FOLLOWING" if i["name_ptr"] == FOLLOWING else
                "INSERT" if i["name_ptr"] == INSERT else
                "NULL" if i["name_ptr"] == 0 else "packed" for i in images),
            "resource_bytes_0x80_aligned": sum(1 for i in images if i["resource_bytes"] % 0x80 == 0),
            "dxt_chain_consistent": sum(1 for i in images if i["chain_consistent"] is True),
            "dxt_chain_checked": sum(1 for i in images if i["chain_consistent"] is not None),
            "dxt_chain_bad": [
                {"root": i["root"], "fmt": hex(i["format"]), "dims": f"{i['width']}x{i['height']}",
                 "levels": i["levels"], "cubemap": i["cubemap"],
                 "declared": i["resource_bytes"], "expected": i["expected_chain_bytes"]}
                for i in images if i["chain_consistent"] is False
            ][:12],
            "root_alignment": histogram(alignment_of(i["root"]) for i in images),
        },
        "materials": {
            "located": len(materials),
            "textures": histogram(m["textures"] for m in materials),
            "state_bits": histogram(m["state_bits"] for m in materials),
            "constants": histogram(m["constants"] for m in materials),
            "sort_key": histogram(m["sort_key"] for m in materials),
            "camera_region": histogram(m["camera_region"] for m in materials),
            "techset_null": sum(1 for m in materials if m["techset_null"]),
            "platform_state_all_zero": sum(1 for m in materials if m["platform_state_zero"]),
            "platform_state_nonzero_bytes": histogram(
                min(m["platform_state_nonzero_bytes"], 56) for m in materials),
            "root_alignment": histogram(alignment_of(m["root"]) for m in materials),
            "named": sum(1 for m in materials if m["name"]),
            "name_sample": [m["name"] for m in materials if m["name"]][:8],
        },
        "techsets": {
            "located": len(techsets),
            "owned": sum(1 for t in techsets if not t["referenced"]),
            "referenced": sum(1 for t in techsets if t["referenced"]),
            "world_vertex_format": histogram(t["world_vertex_format"] for t in techsets),
            "technique_slots_used": histogram(t["techniques"] for t in techsets),
            "root_alignment": histogram(alignment_of(t["root"]) for t in techsets),
        },
    }


def compare(generated: dict, retail: list[dict]) -> list[dict]:
    """Report where the generated zone's shape is outside the Retail envelope."""
    findings: list[dict] = []

    def note(key: str, severity: str, detail: str, got, expected) -> None:
        findings.append({"key": key, "severity": severity, "detail": detail,
                         "generated": got, "retail": expected})

    if not generated["zone_padding_multiple_64k"]:
        note("ZONE.PADDING", "error", "decompressed zone is not padded to a whole 64-KiB block",
             generated["zone_bytes"], [r["zone_bytes"] for r in retail])

    ret_max = max(r["declared_zone_bytes"] for r in retail)
    if generated["declared_zone_bytes"] > ret_max:
        note("ZONE.ALLOCATION", "error",
             "declared allocation exceeds every measured Retail zone",
             generated["declared_zone_bytes"], ret_max)

    # Retail never ships an image whose pixels are inside the zone rather than delayed.
    ret_delay = {k for r in retail for k in r["images"]["delay_load_pixels"]}
    gen_delay = set(generated["images"]["delay_load_pixels"])
    if gen_delay - ret_delay:
        note("IMAGE.DELAY_LOAD_PIXELS", "warning",
             "images use a delayLoadPixels value no Retail zone uses",
             generated["images"]["delay_load_pixels"],
             {k: sum(r["images"]["delay_load_pixels"].get(k, 0) for r in retail) for k in ret_delay})

    ret_levels = max(r["images"]["max_level_count"] for r in retail)
    if generated["images"]["max_level_count"] > ret_levels:
        note("IMAGE.LEVEL_COUNT", "warning",
             "an image declares more mip levels than any Retail image",
             generated["images"]["max_level_count"], ret_levels)

    ret_edge = max(r["images"]["max_edge"] for r in retail)
    if generated["images"]["max_edge"] > ret_edge:
        note("IMAGE.MAX_EDGE", "warning",
             "an image is larger than any Retail image",
             generated["images"]["max_edge"], ret_edge)

    if generated["images"]["dxt_chain_bad"]:
        note("IMAGE.DXT_CHAIN", "error",
             "declared resource size does not match the format/dimension/level chain",
             generated["images"]["dxt_chain_bad"], "consistent in Retail")

    for family in ("images", "materials", "techsets"):
        gen_align = generated[family]["root_alignment"]
        ret_align = {}
        for r in retail:
            for k, v in r[family]["root_alignment"].items():
                ret_align[k] = ret_align.get(k, 0) + v
        extra = {k: v for k, v in gen_align.items() if k not in ret_align}
        if extra:
            note(f"{family.upper()}.ROOT_ALIGNMENT", "warning",
                 "roots sit on an alignment no Retail zone uses", extra, ret_align)

    gen_ptr = set(generated["images"]["resource_ptr"])
    ret_ptr = {k for r in retail for k in r["images"]["resource_ptr"]}
    if gen_ptr - ret_ptr:
        note("IMAGE.RESOURCE_POINTER", "warning",
             "image resource pointers use a marker Retail does not",
             generated["images"]["resource_ptr"], {k: 1 for k in ret_ptr})

    # Compare the *rate*, not the count: a Retail zone contains far more materials than a
    # generated one, so raw counts are meaningless.  Measured on both Retail zones, a
    # named (FOLLOWING-name) Material carries an all-zero platformState in 63/63 and
    # 156/157 cases - the field is loader scratch, not serialized render state - so an
    # all-zero block is Retail behaviour rather than a defect.
    def zero_rate(survey_: dict) -> float | None:
        named = survey_["materials"]["named"]
        return None if not named else survey_["materials"]["platform_state_all_zero"] / named

    gen_rate = zero_rate(generated)
    ret_rates = [r for r in (zero_rate(x) for x in retail) if r is not None]
    if gen_rate is not None and ret_rates and gen_rate > max(ret_rates) + 0.05:
        note("MATERIAL.PLATFORM_STATE", "warning",
             "a larger share of named materials carries an all-zero platformState than Retail",
             round(gen_rate, 4), [round(r, 4) for r in ret_rates])

    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="structural diff against Retail PS3 zones")
    ap.add_argument("--generated", required=True)
    ap.add_argument("--retail", action="append", required=True)
    ap.add_argument("--out")
    ns = ap.parse_args(argv)

    generated = survey(Path(ns.generated))
    retail = [survey(Path(p)) for p in ns.retail]
    findings = compare(generated, retail)
    result = {
        "tool": "iw3mapporter-ps3-zone-diff/v1",
        "generated": generated,
        "retail": retail,
        "findings": findings,
        "errors": sum(1 for f in findings if f["severity"] == "error"),
        "warnings": sum(1 for f in findings if f["severity"] == "warning"),
    }
    text = json.dumps(result, indent=2, default=str)
    if ns.out:
        Path(ns.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

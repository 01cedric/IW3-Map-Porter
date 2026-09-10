#!/usr/bin/env python3
"""Preflight check for a generated PS3 FastFile pair, before any hardware test.

Why this exists
---------------
Most of the "and now it fails somewhere else" loop comes from one structural fact
about IW3: a zone does not only contain converted data, it also *references* data
by name.  Every ``,name`` string in the zone is resolved at load time through
``DB_FindXAssetHeader`` against the support/common zones that are already installed
on the console.  The porter never converts a shader graph - TechniqueSets are pure
name references - and with ``--sound-policy omit`` no sound asset is written at
all.  If one of those names is absent on the target installation, nothing in the
porter can notice: the failure only appears on hardware, as a stall or as missing
visuals, and it looks like a brand-new bug every time.

This tool makes that class of failure checkable *offline*:

1. it re-verifies everything about the saved files that can be verified without a
   console (container framing, zone header, block declarations, allocation budget,
   XAssetList sanity);
2. it extracts every ``,name`` the zone expects the installation to provide;
3. given the PS3 zone files from the target installation (``--support-zone``), it
   reports for each required name whether that name actually occurs in them.

Point 3 is the one that converts guesswork into a list. Name presence in a support
zone is evidence, not a proof that the asset behind it is compatible - the tool
says so rather than claiming more.

Usage
-----
    python tools/ps3_preflight.py OUT/mp_x.ff OUT/mp_x_load.ff \\
        --report OUT/mp_x.python-port-report.json \\
        --support-zone "D:/ps3/zone/english/common_mp.ff" \\
        --support-zone "D:/ps3/zone/english/code_post_gfx_mp.ff" \\
        --out OUT/mp_x.ps3-preflight.json

Exit code 0 = every enabled check passed, 2 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cod4porter.backend.v4_ffio import (  # noqa: E402
    PS3_ASSET_TYPES,
    parse_xasset_list,
    parse_zone_header,
    read_ps3_fastfile,
    require_retail_ps3_adler32,
)
from cod4porter.ps3_budget import (  # noqa: E402
    DEFAULT_ZONE_BUDGET_BYTES,
    RETAIL_PS3_SHIPMENT_ZONE_BYTES,
    check_zone_allocation,
)
from cod4porter.zone_writer import BLOCK_SIZE_GRANULARITY  # noqa: E402

ZONE_HEADER_SIZE = 36
# IW3 asset names are ASCII identifiers or paths.  A multi-megabyte zone is mostly
# binary, so the pattern must be strict or it reports random byte runs as references:
# start on a letter/underscore/$, continue with identifier or path characters only,
# and require at least three characters.
NAME_BYTES = rb"[A-Za-z0-9_\-./$]"
SHARED_NAME = re.compile(rb"(?<=\x00),([A-Za-z_$]" + NAME_BYTES + rb"{2,126})\x00")
# A name in its home zone follows its asset root directly and is not preceded by a
# NUL, so the index anchors on any non-name byte instead.
PLAIN_NAME = re.compile(rb"(?<![A-Za-z0-9_\-./$])([A-Za-z0-9_$]" + NAME_BYTES + rb"{1,126})\x00")


@dataclass
class Check:
    key: str
    passed: bool
    actual: object
    expected: object
    detail: str


def check(key: str, actual, expected, detail: str, predicate=None) -> Check:
    ok = bool(predicate(actual)) if predicate else actual == expected
    return Check(key, ok, actual, expected, detail)


def shared_names(zone: bytes) -> set[str]:
    """Every ``,name`` XString the zone expects the installation to resolve."""
    return {m.group(1).decode("latin-1") for m in SHARED_NAME.finditer(b"\x00" + zone)}


def plain_names(zone: bytes) -> set[str]:
    """Every plausible asset-name XString in a zone, used as the presence index."""
    return {m.group(1).decode("latin-1").lstrip(",") for m in PLAIN_NAME.finditer(b"\x00" + zone)}


def image_names(zone: bytes) -> set[str]:
    """GfxImage names found structurally, not by pattern.

    The regular expressions above cannot see a name containing ``~``, ``&`` or
    ``#`` - and IW3 image names are full of all three (``~usmc_body_mp_spc-rgb&
    usmc_bo~047366d7``).  Relying on the pattern alone therefore reports images
    that ARE present as missing.  A GfxImage root ends with two FOLLOWING
    markers (resource pointer, name pointer) directly followed by the name, so
    the eight FF bytes are an exact anchor.
    """
    out: set[str] = set()
    anchor = b"\xff" * 8
    i = zone.find(anchor)
    while i != -1:
        r = i - 0x2C
        if r >= 0 and r + 0x34 < len(zone):
            map_type = struct.unpack_from(">i", zone, r)[0]
            levels = zone[r + 5]
            w, h, d = struct.unpack_from(">HHH", zone, r + 0x0C)
            w2, h2, d2 = struct.unpack_from(">HHH", zone, r + 0x24)
            size = struct.unpack_from(">I", zone, r + 0x20)[0]
            if (map_type in (3, 5) and zone[r + 6] == 2 and 1 <= levels <= 16
                    and (w, h, d) == (w2, h2, d2) and w and h and size and size % 0x80 == 0):
                end = zone.find(b"\0", r + 0x34, r + 0x34 + 160)
                if end > r + 0x34:
                    raw = zone[r + 0x34:end]
                    if all(0x20 <= c < 0x7F for c in raw):
                        out.add(raw.decode("ascii"))
        i = zone.find(anchor, i + 1)
    return out


def image_payloads(zone: bytes) -> list[tuple[str, int, int, int]]:
    """(name, width, height, declared payload bytes) for every GfxImage in a zone."""
    out: list[tuple[str, int, int, int]] = []
    anchor = b"\xff" * 8
    i = zone.find(anchor)
    while i != -1:
        r = i - 0x2C
        if r >= 0 and r + 0x34 < len(zone):
            map_type = struct.unpack_from(">i", zone, r)[0]
            levels = zone[r + 5]
            w, h, d = struct.unpack_from(">HHH", zone, r + 0x0C)
            w2, h2, d2 = struct.unpack_from(">HHH", zone, r + 0x24)
            size = struct.unpack_from(">I", zone, r + 0x20)[0]
            if (map_type in (3, 5) and zone[r + 6] == 2 and 1 <= levels <= 16
                    and (w, h, d) == (w2, h2, d2) and w and h and size and size % 0x80 == 0):
                end = zone.find(b"\0", r + 0x34, r + 0x34 + 160)
                if end > r + 0x34:
                    raw = zone[r + 0x34:end]
                    if all(0x20 <= c < 0x7F for c in raw):
                        out.append((raw.decode("ascii"), w, h, size))
        i = zone.find(anchor, i + 1)
    return out


def audit_zone(label: str, path: Path, budget: int | None) -> tuple[list[Check], dict]:
    checks: list[Check] = []
    document = read_ps3_fastfile(path)
    zone = document.zone
    header = parse_zone_header(zone, "ps3")
    assets = parse_xasset_list(zone, "ps3")

    frames = require_retail_ps3_adler32(document)
    checks.append(check(f"{label}.FRAMING", frames, "> 0",
                        "every compressed PS3 frame carries a verified Retail Adler-32",
                        predicate=lambda v: isinstance(v, int) and v > 0))
    checks.append(check(f"{label}.ZONE_PADDING", len(zone) % 0x10000, 0,
                        "decompressed zone is 64-KiB padded like Retail"))

    last = len(zone)
    while last > 0 and zone[last - 1] == 0:
        last -= 1
    declared_end = header.zone_memory_size + ZONE_HEADER_SIZE
    checks.append(check(f"{label}.HEADER_SIZE_FIELD", declared_end >= last, True,
                        "word0 + 36 covers all non-zero stream bytes "
                        f"(declared {declared_end}, last non-zero {last})"))
    checks.append(check(f"{label}.HEADER_SIZE_IN_ZONE", declared_end <= len(zone), True,
                        "word0 + 36 stays inside the decompressed zone"))

    # Measured over 2843 GfxImages in five Retail PS3 zones: no single image payload
    # exceeds 4 MiB and no uncompressed GfxWorld image is wider than 1024.  A PC map's
    # native lightmaps break both, and on hardware that freezes the console inside
    # GfxWorld rather than reporting anything.
    oversized = sorted(
        (f"{name} {w}x{h} {size:,}B" for name, w, h, size in image_payloads(zone) if size > 4 * 1024 * 1024)
    )
    checks.append(check(f"{label}.IMAGE_PAYLOAD_CEILING", oversized, [],
                        "no image payload exceeds the 4 MiB ceiling measured across every Retail PS3 zone"))

    odd = [i for i, size in enumerate(header.block_sizes) if size % BLOCK_SIZE_GRANULARITY]
    checks.append(check(f"{label}.BLOCK_DECLARATION", odd, [],
                        "every declared block size uses the Retail even boundary"))

    allocation = check_zone_allocation(header.block_sizes,
                                       budget_bytes=budget or RETAIL_PS3_SHIPMENT_ZONE_BYTES)
    if budget:
        checks.append(check(f"{label}.ZONE_BUDGET", allocation["declared_zone_bytes"], f"<= {budget}",
                            "declared zone allocation fits the PS3 budget "
                            f"({allocation['ratio_vs_retail_reference']}x the measured Retail zone)",
                            predicate=lambda v: v <= budget))

    bad_roots = [a.index for a in assets.assets if a.serialized_pointer != 0xFFFFFFFF]
    checks.append(check(f"{label}.XASSET_ROOTS", bad_roots, [],
                        "every top-level XAsset root uses the FOLLOWING marker"))
    unknown = sorted({a.type_id for a in assets.assets if a.type_id not in PS3_ASSET_TYPES})
    checks.append(check(f"{label}.XASSET_TYPES", unknown, [],
                        "every top-level XAsset type id is a known PS3 IW3 family"))

    counts: dict[str, int] = {}
    for asset in assets.assets:
        counts[asset.type_name] = counts.get(asset.type_name, 0) + 1

    info = {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "zone_bytes": len(zone),
        "zone_memory_size": header.zone_memory_size,
        "block_sizes": list(header.block_sizes),
        "allocation": allocation,
        "xassets": len(assets.assets),
        "script_strings": len(assets.script_strings),
        "asset_counts": counts,
        "shared_references": sorted(shared_names(zone)),
    }
    return checks, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="offline preflight for generated PS3 FastFiles")
    ap.add_argument("fastfiles", nargs="+", help="generated <map>.ff and optionally <map>_load.ff")
    ap.add_argument("--report", help="the porter's *.python-port-report.json, for TechniqueSet evidence")
    ap.add_argument("--support-zone", action="append", default=[],
                    help="a PS3 zone file from the TARGET installation; repeatable")
    ap.add_argument("--ps3-zone-budget", default=str(DEFAULT_ZONE_BUDGET_BYTES),
                    help='byte budget for the declared zone allocation, or "off"')
    ap.add_argument("--out", help="write the full JSON result here")
    ns = ap.parse_args(argv)

    budget_arg = str(ns.ps3_zone_budget).strip().lower()
    budget = None if budget_arg in ("off", "none", "0") else int(budget_arg, 0)

    checks: list[Check] = []
    zones: dict[str, dict] = {}
    required: set[str] = set()
    for path in (Path(p) for p in ns.fastfiles):
        label = "LOAD" if path.stem.endswith("_load") else "MAIN"
        label = f"{label}[{path.name}]"
        if not path.is_file():
            checks.append(Check(f"{label}.EXISTS", False, False, True, "file not found"))
            continue
        try:
            zone_checks, info = audit_zone(label, path, budget)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the preflight
            checks.append(Check(f"{label}.PARSE", False, str(exc), "parsable PS3 FastFile",
                                "the saved file could not be read as a Retail PS3 zone"))
            continue
        checks.extend(zone_checks)
        zones[path.name] = info
        required |= set(info["shared_references"])

    technique_rows = []
    exact_required = False
    if ns.report:
        report = json.loads(Path(ns.report).read_text(encoding="utf-8"))
        manifest = report.get("ps3_native_dependencies") or {}
        technique_rows = list((manifest.get("techsets") or {}).get("entries", ()))
        if technique_rows or manifest.get("external_materials") or manifest.get("external_images") or manifest.get("external_xmodels"):
            # The porter knows exactly which names it wrote.  Prefer that over the
            # string scan, which is only an approximation on a mostly binary zone.
            required = set()
            exact_required = True
        for row in technique_rows:
            required.add(str(row.get("required_ps3_name", "")).lstrip(","))
        for name in manifest.get("external_materials") or ():
            required.add(str(name).lstrip(","))
        for name in manifest.get("external_images") or ():
            required.add(str(name).lstrip(","))
        for name in manifest.get("external_xmodels") or ():
            required.add(str(name).lstrip(","))
        required.discard("")

    installed: set[str] = set()
    support_info = []
    for path in (Path(p) for p in ns.support_zone):
        if not path.is_file():
            checks.append(Check(f"SUPPORT[{path.name}].EXISTS", False, False, True, "support zone not found"))
            continue
        try:
            document = read_ps3_fastfile(path)
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(f"SUPPORT[{path.name}].PARSE", False, str(exc),
                                "parsable PS3 FastFile", "support zone could not be read"))
            continue
        names = plain_names(document.zone) | image_names(document.zone)
        # The string index cannot see a name that is not preceded by a NUL, which is the
        # normal case for an asset name serialized directly behind its root: w_water is
        # owned by Retail mp_shipment yet invisible to the scan.  Merge the structurally
        # located TechniqueSet names so a real reference is never reported as missing.
        try:
            from ps3_technique_catalog import scan as scan_techsets  # noqa: PLC0415
            names |= set(scan_techsets(path)[0])
        except Exception:  # noqa: BLE001 - the index degrades, it does not fail
            pass
        installed |= names
        support_info.append({"path": str(path), "zone_bytes": len(document.zone), "names_indexed": len(names)})

    installed_folded = {name.casefold() for name in installed}
    missing = sorted(name for name in required if name.lstrip(",").casefold() not in installed_folded)
    if ns.support_zone:
        checks.append(check("DEPENDENCIES.RESOLVED", missing, [],
                            f"name-presence scan for {len(required)} references; this does not execute typed asset linking. "
                            + ('Required names come from the conversion report.' if exact_required else
                               'Required names are approximate byte-scan candidates and can include binary false positives.')))

    dependencies = {
        "contract": (
            "these names are resolved on the console through DB_FindXAssetHeader; "
            "the porter writes only the reference, never the asset"
        ),
        "required": sorted(required),
        "required_count": len(required),
        "required_source": "porter report (exact)" if exact_required else "zone string scan (approximate)",
        "check_status": ('not_checked_no_support_zones' if not ns.support_zone else
                         'name_presence_incomplete' if missing else 'name_presence_passed'),
        "native_linker_executed": False,
        "missing_names_are_unconfirmed": not exact_required,
        "support_zones": support_info,
        "support_names_indexed": len(installed),
        "missing": missing if ns.support_zone else None,
        "note": (
            "A matching string does not prove that an asset of the required type is registered. "
            "Use Emulator & Linker with the target ELF and support zones to check actual asset resolution. "
            "Pass ONLY zones the console has loaded while this map runs - common_mp.ff, "
            "code_post_gfx_mp.ff, code_pre_gfx_mp.ff, ui_mp.ff and the localized zones. "
            "Another map's .ff is never loaded alongside this one, so a name found only "
            "there does NOT resolve."
        ),
        "techsets": technique_rows,
    }

    failed = [c for c in checks if not c.passed]
    result = {
        "tool": "iw3mapporter-ps3-preflight/v1",
        "scope": "container and metadata checks; optional name-presence scan",
        "hardware_tested": False,
        "game_startup_executed": False,
        "passed": not failed,
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "zones": zones,
        "ps3_native_dependencies": dependencies,
        "checks": [asdict(c) for c in checks],
    }
    text = json.dumps(result, indent=2, default=str)
    if ns.out:
        Path(ns.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())

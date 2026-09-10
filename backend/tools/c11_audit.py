#!/usr/bin/env python3
"""Generic, fail-closed audit for a Version-11 PC -> PS3 map build.

The conversion report contains the writer-side graph proof.  This audit opens the
saved FastFiles again, validates the retail PS3 frame/container contracts and
checks that every release-relevant report field is present.  Structural success
and exact/full-fidelity success are deliberately reported separately: neither is
a substitute for a real PS3/RPCS3 loader test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cod4porter.ps3_budget import RETAIL_PS3_SHIPMENT_ZONE_BYTES
from cod4porter.backend.v4_ffio import (
    parse_xasset_list,
    parse_zone_header,
    read_ps3_fastfile,
    require_retail_ps3_adler32,
)
from cod4porter.backend.v4_gate import evaluate_full_fidelity
from cod4porter.backend.v4_load import parse_load_zone
from cod4porter.load_zone import read_load_document


@dataclass(frozen=True)
class Check:
    check_id: str
    passed: bool
    observed: Any
    expected: Any
    detail: str
    fidelity_only: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _get(data: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return default
        value = value[key]
    return value


def _check(
    check_id: str,
    observed: Any,
    expected: Any,
    detail: str,
    *,
    predicate=None,
    fidelity_only: bool = False,
) -> Check:
    passed = predicate(observed) if predicate is not None else observed == expected
    return Check(check_id, bool(passed), observed, expected, detail, fidelity_only)


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: Any) -> bool:
    return _nonnegative_int(value) and value > 0


def _load_image(profile: Any, material_index: int, texture_index: int) -> Mapping[str, Any] | None:
    """Return one independently parsed load image without trusting report topology."""
    try:
        image = profile.materials[material_index].textures[texture_index]["image"]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None
    return image if isinstance(image, Mapping) else None


def _evaluate_saved_load(
    map_name: str,
    load_info: Mapping[str, Any],
    document: Any,
    typed: Any,
    profile: Any,
) -> list[Check]:
    """Cross-check the fresh load report against bytes parsed from the saved FastFile."""
    checks: list[Check] = []
    expected_materials = ["$victorybackdrop", ",$defeatbackdrop", "$levelbriefing"]
    expected_texture_counts = [1, 0, 1]
    shared_victory=load_info.get('victory_material_shared') is True
    if shared_victory:
        expected_materials[0]=',$victorybackdrop';expected_texture_counts[0]=0
    material_names = [material.name for material in profile.materials]
    texture_counts = [len(material.textures) for material in profile.materials]
    victory = _load_image(profile, 0, 0)
    level = _load_image(profile, 2, 0)
    expected_level_name = f"loadscreen_{map_name}"
    expected_victory_name = None if shared_victory else "mile_high_victory_screen"

    checks.append(_check("LOAD.ACTUAL_TECHNIQUE", profile.technique_name, ",2d", "saved load TechniqueSet identity"))
    checks.append(_check("LOAD.ACTUAL_MATERIALS", material_names, expected_materials, "saved load Material identity/order"))
    checks.append(_check("LOAD.ACTUAL_TEXTURE_CARDINALITY", texture_counts, expected_texture_counts, "saved load has exactly the retail 1/0/1 texture topology"))
    checks.append(_check("LOAD.RAWFILE_IDENTITY", profile.rawfile_name, f"{map_name}_load", "load RawFile identity belongs exactly to the target map"))
    checks.append(_check("LOAD.RAWFILE_EMPTY", profile.rawfile_bytes, 0, "saved load RawFile is an empty zone marker"))

    level_name = level.get("name") if level is not None else None
    victory_name = victory.get("name") if victory is not None else None
    checks.append(_check("LOAD.ACTUAL_LEVEL_NAME", level_name, expected_level_name, "$levelbriefing owns the target map loadscreen"))
    checks.append(_check("LOAD.REPORTED_LEVEL_NAME", load_info.get("levelbriefing_image_name"), expected_level_name, "reported loadscreen identity matches the target map"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_NAME", victory_name, expected_victory_name, "$victorybackdrop owns the stock victory image"))
    checks.append(_check("LOAD.REPORTED_VICTORY_NAME", load_info.get("victory_image_name"), expected_victory_name, "reported victory identity is the audited stock image"))

    level_geometry = [level.get(key) for key in ("width", "height", "depth")] if level is not None else None
    victory_geometry = [victory.get(key) for key in ("width", "height", "depth")] if victory is not None else None
    checks.append(_check("LOAD.ACTUAL_LEVEL_GEOMETRY", level_geometry, load_info.get("levelbriefing_dimensions"), "saved loadscreen width/height/depth match the converted IWD"))
    checks.append(_check("LOAD.ACTUAL_LEVEL_MIPS", level.get("mip_count") if level is not None else None, load_info.get("levelbriefing_mip_count"), "saved loadscreen mip count matches the converted IWD"))
    checks.append(_check("LOAD.ACTUAL_LEVEL_FORMAT", level.get("format") if level is not None else None, load_info.get("levelbriefing_ps3_format"), "saved loadscreen PS3 format matches the conversion report"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_GEOMETRY", victory_geometry, load_info.get("victory_dimensions"), "saved victory width/height/depth match the selected audited source"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_MIPS", victory.get("mip_count") if victory is not None else (0 if shared_victory else None), load_info.get("victory_mip_count"), "saved victory mip count matches the selected source"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_FORMAT", victory.get("format") if victory is not None else None, None if shared_victory else 0x86, "shared victory owns no pixels; owned victory uses PS3 DXT1"))

    checks.append(_check("LOAD.ACTUAL_LEVEL_RESOURCE", level.get("resource_sha256") if level is not None else None, load_info.get("levelbriefing_resource_sha256"), "saved loadscreen pixels match the converted IWD resource"))
    checks.append(_check("LOAD.ACTUAL_LEVEL_BYTES", level.get("resource_size") if level is not None else None, load_info.get("levelbriefing_resource_bytes"), "saved loadscreen resource size"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_RESOURCE", victory.get("resource_sha256") if victory is not None else None, load_info.get("victory_resource_sha256"), "saved victory pixels match the selected audited source"))
    checks.append(_check("LOAD.ACTUAL_VICTORY_BYTES", victory.get("resource_size") if victory is not None else (0 if shared_victory else None), load_info.get("victory_resource_bytes"), "saved victory resource size"))

    expected_resource_bytes = None
    if level is not None and (victory is not None or shared_victory):
        level_bytes = level.get("resource_size")
        victory_bytes = victory.get("resource_size") if victory is not None else 0
        if _nonnegative_int(level_bytes) and _nonnegative_int(victory_bytes):
            expected_resource_bytes = level_bytes + victory_bytes
    checks.append(_check("LOAD.ACTUAL_RESOURCE_TOTAL", profile.resource_bytes, expected_resource_bytes, "the two expected images account for the entire load resource block", predicate=lambda value: _nonnegative_int(value) and _nonnegative_int(expected_resource_bytes) and value == expected_resource_bytes))
    checks.append(_check("LOAD.ACTUAL_DECLARED_RESOURCE_TOTAL", profile.declared_resource_bytes, expected_resource_bytes, "declared load resource bytes equal the exact two-image payload", predicate=lambda value: _nonnegative_int(value) and _nonnegative_int(expected_resource_bytes) and value == expected_resource_bytes))

    checks.append(_check("LOAD.ACTUAL_BLOCK_SIZES", list(typed.block_sizes), list(load_info.get("block_sizes", ())), "saved load logical block sizes match the report"))
    checks.append(_check("LOAD.ACTUAL_ZONE_SHA256", document.zone_sha256, load_info.get("output_zone_sha256"), "saved decompressed load-zone hash"))
    checks.append(_check("LOAD.ACTUAL_ZONE_BYTES", len(document.zone), load_info.get("zone_bytes"), "saved padded load-zone size"))
    checks.append(_check("LOAD.ACTUAL_ZONE_MEMORY", profile.zone_memory_size, load_info.get("zone_memory_size"), "saved load parsed-content size"))
    checks.append(_check("LOAD.ACTUAL_TRAILING_ZERO", profile.trailing_nonzero, 0, "saved load capacity tail is zero-filled"))
    return checks


def _absolute_path(path: Path, base: Path) -> Path:
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: Path, base: Path) -> str:
    """Use platform filesystem case rules without resolving output symlinks as sources."""
    return os.path.normcase(os.fspath(_absolute_path(path, base)))


def _unexpected_output_iwds(
    report_path: Path,
    report: Mapping[str, Any],
    directories: Sequence[Path],
) -> tuple[list[str], list[str]]:
    """Recursively find non-source IWD artifacts, without following directory links."""
    raw_sources = report.get("iwd_paths")
    source_keys: set[str] = set()
    if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)):
        for value in raw_sources:
            if isinstance(value, str) and value:
                source_keys.add(_path_key(Path(value), report_path.parent))

    roots: dict[str, Path] = {}
    for directory in directories:
        absolute = _absolute_path(directory, report_path.parent)
        roots[_path_key(absolute, report_path.parent)] = absolute

    unexpected: dict[str, str] = {}
    errors: list[str] = []

    def record_error(error: OSError) -> None:
        errors.append(str(error))

    for root in roots.values():
        if not root.is_dir():
            continue
        for current, _subdirectories, filenames in os.walk(root, followlinks=False, onerror=record_error):
            current_path = Path(current)
            for filename in filenames:
                candidate = current_path / filename
                if candidate.suffix.casefold() != ".iwd":
                    continue
                key = _path_key(candidate, report_path.parent)
                if key not in source_keys:
                    unexpected[key] = os.fspath(_absolute_path(candidate, report_path.parent))
    return sorted(unexpected.values(), key=str.casefold), sorted(errors)


_FIDELITY_ONLY_BLOCKER_FRAGMENTS = (
    "Material PlatformState plans use non-exact compatibility families",
    "Material image slots use synthetic neutral resources",
    "FX Material visuals use the shared runtime default",
    "FX visuals use a runtime NULL compatibility target",
    "PhysPreset sound prefixes use an empty compatibility string",
)


def _structural_blockers(closure: Mapping[str, Any] | None) -> list[str] | None:
    if not isinstance(closure, Mapping):
        return None
    blockers = closure.get("blockers")
    if not isinstance(blockers, Sequence) or isinstance(blockers, (str, bytes)):
        return None
    # Exact-reference loss is fidelity debt only when the converter explicitly
    # accounts for the whole deficit with compatibility targets. Never hide an
    # unaccounted count mismatch or an unrelated structural blocker.
    source = closure.get('source_fx_visual_refs')
    exact = closure.get('resolved_exact_fx_visual_refs')
    fallbacks = closure.get('runtime_fx_visual_fallbacks')
    accounted_fx = None
    if (all(type(value) is int and value >= 0 for value in (source, exact, fallbacks))
            and source > exact and source == exact + fallbacks):
        accounted_fx = f'FX packed visuals: source={source} output/resolved={exact}'
    return [
        str(blocker)
        for blocker in blockers
        if str(blocker) != accounted_fx
        and not any(fragment in str(blocker) for fragment in _FIDELITY_ONLY_BLOCKER_FRAGMENTS)
    ]


def evaluate_report(report: Mapping[str, Any]) -> list[Check]:
    """Evaluate only explicit machine-readable conversion evidence."""
    checks: list[Check] = []
    map_name = report.get("map")
    checks.append(_check("REPORT.MAP", map_name, "non-empty map name", "conversion identity", predicate=lambda x: isinstance(x, str) and bool(x.strip())))
    checks.append(_check("REPORT.HARDWARE_CLAIM", report.get("hardware_tested"), False, "software build must not claim an unperformed hardware test"))
    checks.append(_check("POLICY.EXECUTION_MODE", report.get("execution_mode"), "owner-aware-visual-candidate", "Version 11 uses the live owner-aware visual graph"))
    checks.append(_check("POLICY.RUNTIME_COMPATIBLE", report.get("runtime_compatible"), True, "compatibility evidence is explicit in the report"))
    checks.append(_check("POLICY.NO_BOOT_ISOLATION", report.get("boot_isolation"), False, "boot-isolation graph is disabled"))
    checks.append(_check("POLICY.GFX_FASTFILE_DELAYED", report.get("gfxworld_resource_policy"), "fastfile-delayed", "GfxWorld pixels use the generated FF resource FIFO"))
    checks.append(_check(
        "POLICY.MATERIAL_FASTFILE_DELAYED",
        report.get("material_image_resource_policy"),
        "fastfile-delayed or balanced-split",
        "Material/IWD pixels use complete generated FF B3/B6 resource paths",
        predicate=lambda value: value in {"fastfile-delayed", "balanced-split"},
    ))
    checks.append(_check("POLICY.SOUND_OMITTED", report.get("sound_policy"), "omit", "no map-specific Sound assets"))
    asset_counts = _get(report, "main", "asset_counts", default={})
    emitted_sounds = asset_counts.get("sound", 0) if isinstance(asset_counts, Mapping) else None
    checks.append(_check("POLICY.NO_SOUND_XASSETS", emitted_sounds, 0, "destination XAsset list contains no Sound roots"))

    for check_id, section, require_complete in (
        ("RESOURCE.GFXWORLD", "gfxworld_resource_validation", True),
        ("RESOURCE.MATERIAL_IMAGES", "material_image_resource_validation", True),
        ("RESOURCE.GLOBAL_FIFO", "global_deferred_resource_validation", False),
        ("RESOURCE.IWD_FIDELITY", "image_fidelity_validation", False),
    ):
        value = report.get(section)
        checks.append(_check(check_id + ".PRESENT", isinstance(value, Mapping), True, f"{section} evidence is present"))
        checks.append(_check(check_id + ".PASSED", value.get("passed") if isinstance(value, Mapping) else None, True, f"{section} passed"))
        if require_complete:
            checks.append(_check(check_id + ".COMPLETE", value.get("complete") if isinstance(value, Mapping) else None, True, f"{section} contains every declared resource byte"))

    image = report.get("image_fidelity_validation")
    if isinstance(image, Mapping):
        source = image.get("source_iwd_images")
        destination = image.get("destination_iwd_images")
        checks.append(_check("RESOURCE.ALL_IWD_IMAGES", destination, source, "every source IWD image has a destination Image XAsset"))
        checks.append(_check("RESOURCE.IWD_MISMATCHES", len(image.get("mismatches", ())) if isinstance(image.get("mismatches", ()), Sequence) else None, 0, "image dimensions, faces, mip counts and mip hashes are identical"))

    global_fifo = report.get("global_deferred_resource_validation")
    if isinstance(global_fifo, Mapping):
        fifo_count = global_fifo.get("fifo_count")
        fifo_block = global_fifo.get("block")
        declared_bytes = global_fifo.get("declared_bytes")
        block_bytes = global_fifo.get("block_bytes")
        main_block_sizes = _get(report, "main", "block_sizes")
        main_block3 = (
            main_block_sizes[3]
            if isinstance(main_block_sizes, Sequence)
            and not isinstance(main_block_sizes, (str, bytes))
            and len(main_block_sizes) > 3
            else None
        )
        checks.append(_check("RESOURCE.GLOBAL_FIFO.NONEMPTY", fifo_count, "> 0", "Version-11 output contains a real delayed-resource FIFO", predicate=_positive_int))
        checks.append(_check("RESOURCE.GLOBAL_FIFO.BLOCK", fifo_block, 3, "delayed image resources occupy PS3 block 3", predicate=lambda value: _nonnegative_int(value) and value == 3))
        checks.append(_check("RESOURCE.GLOBAL_FIFO.BYTE_CLOSURE", [declared_bytes, block_bytes], "equal nonnegative integers", "declared deferred resources exactly fill block 3", predicate=lambda values: all(_nonnegative_int(value) for value in values) and values[0] == values[1]))
        checks.append(_check("RESOURCE.GLOBAL_FIFO.MAIN_BLOCK3", [block_bytes, main_block3], "equal nonnegative integers", "global FIFO byte count equals the saved main-zone block-3 declaration", predicate=lambda values: all(_nonnegative_int(value) for value in values) and values[0] == values[1]))
        checks.append(_check("RESOURCE.GLOBAL_FIFO.TAIL", global_fifo.get("physical_tail_exact"), True, "global FIFO is physically contiguous through the serialized-zone tail", predicate=lambda value: type(value) is bool and value))
        failures = global_fifo.get("failures")
        checks.append(_check("RESOURCE.GLOBAL_FIFO.FAILURES", list(failures) if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)) else None, [], "global FIFO has no gap/order/hash failure"))

    platform = report.get("platform_state_validation")
    checks.append(_check("PLATFORM_STATE.PRESENT", isinstance(platform, Mapping), True, "tiered PlatformState evidence is present"))
    checks.append(_check("PLATFORM_STATE.SERIALIZED", platform.get("passed") if isinstance(platform, Mapping) else None, True, "planner modes and serialized 0x38-byte payloads agree"))
    checks.append(_check("PLATFORM_STATE.EXACT", platform.get("retail_exact_passed") if isinstance(platform, Mapping) else None, True, "every target signature has exact retail evidence", fidelity_only=True))
    synthetic_count = _get(platform, "tier_counts", "synthetic_assignment") if isinstance(platform, Mapping) else None
    checks.append(_check("PLATFORM_STATE.NO_SYNTHETIC", synthetic_count, 0, "no PlatformState uses an unobserved target family", fidelity_only=True))

    readback = report.get("independent_readback")
    roundtrip = report.get("roundtrip")
    closure = report.get("closure")
    checks.append(_check("MAIN.READBACK", readback.get("passed") if isinstance(readback, Mapping) else None, True, "independent saved-file graph readback"))
    checks.append(_check("MAIN.ROUNDTRIP", roundtrip.get("passed") if isinstance(roundtrip, Mapping) else None, True, "writer/decompressor round trip"))
    structural_blockers = _structural_blockers(closure)
    closure_blockers = closure.get("blockers") if isinstance(closure, Mapping) else None
    closure_passed = closure.get("passed") if isinstance(closure, Mapping) else None
    closure_consistent = (
        isinstance(closure_blockers, Sequence)
        and not isinstance(closure_blockers, (str, bytes))
        and type(closure_passed) is bool
        and closure_passed == (len(closure_blockers) == 0)
    )
    checks.append(_check("GRAPH.CLOSURE_SCHEMA", closure_consistent, True, "closure.passed agrees exactly with the blocker list"))
    checks.append(_check("GRAPH.STRUCTURAL_CLOSURE", structural_blockers, [], "conversion closure has no structural blocker"))
    checks.append(_check("GRAPH.CLOSURE", closure.get("passed") if isinstance(closure, Mapping) else None, True, "conversion closure has no unresolved semantic or structural blockers", fidelity_only=True))
    checks.append(_check("GRAPH.OUTPUT_REFS", closure.get("unresolved_output_refs") if isinstance(closure, Mapping) else None, 0, "no unresolved destination XAsset references"))
    checks.append(_check("GRAPH.POINTER_LEAKS", closure.get("source_pointer_leaks") if isinstance(closure, Mapping) else None, 0, "no failed destination relocation checks"))
    checks.append(_check("GRAPH.DETERMINISTIC", closure.get("deterministic_write") if isinstance(closure, Mapping) else None, True, "two in-memory writes are byte-identical"))
    checks.append(_check("SEMANTIC.NEUTRAL_IMAGES", closure.get("neutral_image_fallbacks") if isinstance(closure, Mapping) else None, 0, "no synthetic neutral image binding", fidelity_only=True))
    checks.append(_check("SEMANTIC.PLATFORM_STATES", closure.get("unresolved_platform_states") if isinstance(closure, Mapping) else None, 0, "all Material PlatformStates have exact retail evidence", fidelity_only=True))
    source_fx_visuals = closure.get("source_fx_visual_refs") if isinstance(closure, Mapping) else None
    exact_fx_visuals = closure.get("resolved_exact_fx_visual_refs") if isinstance(closure, Mapping) else None
    checks.append(_check("SEMANTIC.FX_VISUAL_REFERENCES", exact_fx_visuals, source_fx_visuals, "every packed nested FX visual has an exact typed target", fidelity_only=True))
    checks.append(_check("SEMANTIC.FX_VISUAL_FALLBACKS", closure.get("runtime_fx_visual_fallbacks") if isinstance(closure, Mapping) else None, 0, "no nested FX visual uses a compatibility fallback", fidelity_only=True))
    fx_fallbacks = _get(report, "assembly_diagnostics", "fx_runtime_material_visuals")
    checks.append(_check("SEMANTIC.FX_MATERIAL_FALLBACKS", fx_fallbacks, 0, "all nested FX Material visuals resolve exactly", fidelity_only=True))
    checks.append(_check("FIDELITY.FULL_GATE", report.get("full_fidelity_passed"), True, "all independent fidelity criteria passed", fidelity_only=True))

    evidence_documents = report.get("evidence_documents")
    recorded_gate = report.get("full_fidelity_gate")
    if isinstance(evidence_documents, Sequence) and not isinstance(evidence_documents, (str, bytes)) and isinstance(recorded_gate, Mapping):
        recalculated_gate = evaluate_full_fidelity(*evidence_documents)
        comparable_keys = ("full_fidelity_passed", "passed_count", "required_count", "missing", "failed")
        gate_consistent = all(recorded_gate.get(key) == recalculated_gate.get(key) for key in comparable_keys)
        top_level_consistent = not bool(report.get("full_fidelity_passed")) or bool(recalculated_gate.get("full_fidelity_passed"))
    else:
        gate_consistent = False
        top_level_consistent = False
    checks.append(_check("FIDELITY.GATE_REEVALUATED", gate_consistent, True, "bundled evidence re-evaluates to the recorded 26-criterion gate"))
    checks.append(_check("FIDELITY.TOP_LEVEL_CONSISTENT", top_level_consistent, True, "top-level fidelity success cannot contradict the evidence gate"))

    load = report.get("load")
    checks.append(_check("LOAD.PRESENT", isinstance(load, Mapping), True, "fresh PS3 _load.ff was requested and generated"))
    if isinstance(load, Mapping):
        checks.append(_check("LOAD.MODE", load.get("mode"), "fresh-pc-load-conversion", "load output is rebuilt from the PC load zone"))
        checks.append(_check("LOAD.REPORT_PASSED", load.get("passed"), True, "load converter completed all internal checks"))
        checks.append(_check("LOAD.REPORT_READBACK", load.get("readback_passed"), True, "load converter independently read back the committed file"))
        checks.append(_check("LOAD.READBACK", load.get("strict_readback"), True, "saved load output passes strict typed readback"))
        checks.append(_check("LOAD.FIDELITY", load.get("load_full_fidelity"), True, "loadscreen and stock UI resources have exact source/reference provenance", fidelity_only=True))
        checks.append(_check("LOAD.MATERIALS", load.get("load_materials"), 3, "retail load-zone Material cardinality"))
        checks.append(_check("LOAD.NO_TEMPLATE_NAMES", load.get("template_map_specific_names_reused"), False, "no map-specific donor/reference identity is reused"))
        checks.append(_check("LOAD.NO_REFERENCE_MAP_PIXELS", load.get("map_specific_retail_image_payload_bytes_reused"), 0, "no map-specific reference image payload is copied"))
        checks.append(_check("LOAD.NO_SOUNDS", load.get("sound_assets_emitted"), 0, "load zone emits no Sound assets"))
        checks.append(_check("LOAD.NO_IWD", load.get("iwd_output_created"), False, "load conversion emits no IWD"))
    return checks


def _resolve_report_path(report_path: Path, value: Any, fallback: str) -> Path:
    if isinstance(value, str) and value:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = report_path.parent / candidate
        if candidate.is_file():
            return candidate
    return report_path.parent / fallback


def audit_files(report_path: Path, report: Mapping[str, Any], main_path: Path | None = None, load_path: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    map_name = str(report.get("map", ""))
    main_path = main_path or _resolve_report_path(report_path, report.get("main_output"), f"{map_name}.ff")
    load_info = report.get("load") if isinstance(report.get("load"), Mapping) else {}
    load_path = load_path or _resolve_report_path(report_path, load_info.get("output"), f"{map_name}_load.ff")

    checks.append(_check("MAIN.FILE_EXISTS", main_path.is_file(), True, str(main_path)))
    if main_path.is_file():
        expected_hash = _get(report, "main", "fastfile_sha256")
        checks.append(_check("MAIN.FILE_SHA256", _sha256(main_path), expected_hash, "saved main FastFile hash matches the build report"))
        try:
            document = read_ps3_fastfile(main_path)
            retail_frames = require_retail_ps3_adler32(document)
            header = parse_zone_header(document.zone, "ps3")
            assets = parse_xasset_list(document.zone, "ps3")
            actual_type_counts: dict[str, int] = {}
            for asset in assets.assets:
                actual_type_counts[asset.type_name] = actual_type_counts.get(asset.type_name, 0) + 1
            checks.append(_check("MAIN.RETAIL_FRAMES", retail_frames, "> 0", "every compressed PS3 frame has a verified Adler-32", predicate=lambda x: isinstance(x, int) and x > 0))
            checks.append(_check("MAIN.XASSET_COUNT", len(assets.assets), _get(report, "main", "asset_count"), "saved main XAsset cardinality"))
            checks.append(_check("MAIN.SCRIPT_STRINGS", len(assets.script_strings), _get(report, "main", "script_strings"), "saved main script-string cardinality"))
            checks.append(_check("MAIN.ACTUAL_NO_SOUND_XASSETS", actual_type_counts.get("sound", 0), 0, "reparsed destination XAsset list contains no Sound roots"))
            checks.append(_check("MAIN.ACTUAL_TYPE_COUNTS", actual_type_counts, _get(report, "main", "asset_counts"), "reparsed destination family cardinalities match the report"))
            checks.append(_check("MAIN.BLOCK_SIZES", list(header.block_sizes), list(_get(report, "main", "block_sizes", default=())), "saved PS3 zone block sizes"))
            checks.append(_check("MAIN.ZONE_SHA256", document.zone_sha256, _get(report, "main", "retail_zone_sha256"), "decompressed padded PS3 zone hash"))
            # A PS3 cannot satisfy an arbitrarily large DB_AllocXZoneMemory request and IW3
            # does not reject one cleanly, so measure the saved zone independently of the
            # build report instead of trusting the reported block sizes.
            declared_zone_bytes = sum(int(x) for x in header.block_sizes)
            reported_budget = _get(report, "ps3_zone_allocation", "budget_bytes")
            budget_bytes = int(reported_budget) if isinstance(reported_budget, int) else RETAIL_PS3_SHIPMENT_ZONE_BYTES
            checks.append(_check("MAIN.ACTUAL_ZONE_ALLOCATION", declared_zone_bytes, _get(report, "ps3_zone_allocation", "declared_zone_bytes"), "reparsed block sizes sum to the reported zone allocation"))
            checks.append(_check(
                "MAIN.ZONE_ALLOCATION_BUDGET", declared_zone_bytes, f"<= {budget_bytes}",
                "saved zone allocation fits the PS3 budget derived from the measured Retail zone",
                predicate=lambda value: isinstance(value, int) and value <= budget_bytes,
            ))
            checks.append(_check(
                "MAIN.BLOCK_SIZE_DECLARATION", list(header.block_sizes), 'seven nonnegative 29-bit block sizes',
                "block sizes fit PS3 packed offsets; retail blocks may have odd byte counts",
                predicate=lambda sizes: len(sizes)==7 and all(0<=int(x)<(1<<29) for x in sizes),
            ))
        except Exception as exc:
            checks.append(Check("MAIN.CONTAINER_PARSE", False, str(exc), "valid retail PS3 FastFile", "saved main could not be parsed"))

    checks.append(_check("LOAD.FILE_EXISTS", load_path.is_file(), True, str(load_path)))
    if load_path.is_file():
        expected_hash = load_info.get("output_file_sha256") or load_info.get("sha256")
        checks.append(_check("LOAD.FILE_SHA256", _sha256(load_path), expected_hash, "saved load FastFile hash matches the conversion report"))
        try:
            document = read_ps3_fastfile(load_path)
            retail_frames = require_retail_ps3_adler32(document)
            typed = read_load_document(load_path)
            profile = parse_load_zone(document.zone)
            checks.append(_check("LOAD.RETAIL_FRAMES", retail_frames, "> 0", "every compressed load frame has a verified Adler-32", predicate=lambda x: isinstance(x, int) and x > 0))
            checks.extend(_evaluate_saved_load(map_name, load_info, document, typed, profile))
        except Exception as exc:
            checks.append(Check("LOAD.CONTAINER_PARSE", False, str(exc), "valid typed PS3 load FastFile", "saved load could not be parsed"))

    output_directories = {report_path.parent, main_path.parent, load_path.parent}
    produced_iwds, scan_errors = _unexpected_output_iwds(report_path, report, tuple(output_directories))
    checks.append(_check("OUTPUT.IWD_SCAN", scan_errors, [], "recursive output IWD scan completed without filesystem errors"))
    checks.append(_check("OUTPUT.NO_IWD", produced_iwds, [], "the converter emits no non-source IWD artifact"))
    return checks


def audit(report_path: Path, main_path: Path | None = None, load_path: Path | None = None) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    checks = evaluate_report(report) + audit_files(report_path, report, main_path, load_path)
    structural = [check for check in checks if not check.fidelity_only]
    fidelity = checks
    return {
        "schema": "iw3-mapporter-version11-audit/v1",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "map": report.get("map"),
        "report": str(report_path.resolve()),
        "report_sha256": _sha256(report_path),
        "hardware_tested": bool(report.get("hardware_tested", False)),
        "structural_passed": all(check.passed for check in structural),
        "full_fidelity_passed": all(check.passed for check in fidelity),
        "structural_passed_count": sum(check.passed for check in structural),
        "structural_total": len(structural),
        "full_passed_count": sum(check.passed for check in fidelity),
        "full_total": len(fidelity),
        "checks": [asdict(check) for check in checks],
        "structural_failures": [asdict(check) for check in structural if not check.passed],
        "fidelity_failures": [asdict(check) for check in fidelity if not check.passed],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a generic Version-11 PS3 map build")
    parser.add_argument("report", type=Path)
    parser.add_argument("--main", type=Path)
    parser.add_argument("--load", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--require-full-fidelity", action="store_true", help="return failure when retail-only fidelity proof is incomplete")
    args = parser.parse_args(argv)
    result = audit(args.report.resolve(), args.main, args.load)
    output = args.out or args.report.with_name(args.report.stem + ".version11-audit.json")
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in result["checks"]:
        label = "PASS" if row["passed"] else "FAIL"
        scope = "fidelity" if row["fidelity_only"] else "structural"
        print(f"[{label}] {row['check_id']} ({scope})")
    print(
        f"STRUCTURAL {'PASSED' if result['structural_passed'] else 'BLOCKED'} "
        f"({result['structural_passed_count']}/{result['structural_total']}); "
        f"FULL FIDELITY {'PASSED' if result['full_fidelity_passed'] else 'BLOCKED'} "
        f"({result['full_passed_count']}/{result['full_total']})"
    )
    if not result["structural_passed"]:
        return 1
    if args.require_full_fidelity and not result["full_fidelity_passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Independent Version-10 binary audit for the IW3 PC -> PS3 porter.

The normal porter readback is intentionally only one layer of this audit.  This
program also reads the produced bytes directly and checks contracts that a
writer-derived relocation table cannot prove (most importantly a source
non-NULL pointer silently becoming NULL).

The Getaway Version-10 gate is fail closed.  A report is always written, and
the process exits non-zero if any hard check fails.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import dataclasses
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import traceback
from typing import Any, Iterable, Mapping, Sequence
import zipfile
import zlib


# Allow execution as ``python tools/c10_audit.py`` from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cod4porter.assembler import analyze_and_assemble  # noqa: E402
from cod4porter.assets.basic import LightDefNode, RawFileNode, StringTableNode  # noqa: E402
from cod4porter.assets.clipmap import (  # noqa: E402
    CLIENT_SIZE,
    COLLISION_SIZE,
    DYNDEF_SIZE,
    POSE_SIZE,
    STATIC_MODEL_SIZE,
    ClipMapNode,
)
from cod4porter.assets.fx import OwnedFxNode  # noqa: E402
from cod4porter.assets.gfxworld import (  # noqa: E402
    PS3_DRAW,
    PS3_SURFACE,
    GfxWorldNode,
    GfxWorldResourcePolicy,
)
from cod4porter.assets.image import ImageResourcePolicy  # noqa: E402
from cod4porter.assets.sound import ALIAS as SOUND_ALIAS_SIZE, SoundNode  # noqa: E402
from cod4porter.backend.v4_ffio import (  # noqa: E402
    FOLLOWING,
    INSERT,
    MAGIC,
    NULL,
    PS3_ASSET_TYPES,
    PS3_HEADER_SIZE,
    PS3_RAW_BLOCK,
    PS3_VERSION,
    decode_pc_pointer,
    decode_ps3_pointer,
    parse_xasset_list,
    parse_zone_header,
    read_pc_fastfile,
    read_ps3_fastfile,
)
from cod4porter.load_zone import compute_load_block_sizes, read_load_document  # noqa: E402
from cod4porter.physics import PhysPresetNode  # noqa: E402
from cod4porter.readback import inspect_main_fastfile  # noqa: E402
from cod4porter.world_core import ComWorldNode  # noqa: E402
from cod4porter.xmodel import (  # noqa: E402
    PS3_XMODEL_ROOT_SIZE,
    PS3_XSURFACE_ROOT_SIZE,
    XModelNode,
)


TOOL_SCHEMA = "iw3mapporter-version10-independent-audit/v1"
PS3_ZONE_HEADER_SIZE = 0x24
EXPECTED_GETAWAY_ASSETS = 502
EXPECTED_GETAWAY_SCRIPT_STRINGS = 469
EXPECTED_GETAWAY_B1 = 1_792_064
EXPECTED_GETAWAY_B3 = 49_417_984
EXPECTED_GETAWAY_B6 = 8_989_040
EXPECTED_SHIPMENT_B1 = 165_232
RAWFILE501_BAD_NAME = "__cp11_rawfile_0766"
RAWFILE501_GOOD_NAME = "mp_getaway"
LIGHTDEF_NAME = "light_point_linear"
ATTENUATION_IMAGE_NAME = ",falloff_linear"
# mp_getaway load content ends Block 4 at 197.  Retail mp_shipment_load.ff declares
# 198 for its own (one byte longer) names, which the same replay reproduces exactly.
EXPECTED_GETAWAY_LOAD_BLOCKS = (196, 0, 0, 0, 197, 0, 1_223_424)
XASSET_HEADER_SUFFIX = "::$xasset_header"
PS3_FX_XMODEL_ALIAS_ROOT_SIZE = 0xCC

# Independently frozen same-revision Getaway-PC / Shipment-PS3 stock sample.
# Surface cardinality alone is insufficient: e.g. com_laptop_2_open has a
# different geometry revision despite a superficially similar model identity.
RETAIL_XMODEL_MEM_ORACLE = (
    ("body_mp_opforce_assault", 398_009, 4, 398_073),
    ("body_mp_opforce_cqb", 385_885, 6, 385_989),
    ("body_mp_opforce_eningeer", 403_757, 9, 403_921),
    ("body_mp_opforce_support", 375_785, 6, 375_889),
    ("com_bomb_objective", 419_511, 13, 419_755),
    ("com_bomb_objective_d", 324_375, 9, 324_539),
    ("com_cellphone_on", 11_371, 3, 11_415),
    ("com_plasticcase_beige_big", 234_323, 4, 234_387),
    ("head_mp_opforce_3hole_mask", 149_763, 7, 149_887),
    ("head_mp_opforce_gasmask", 181_775, 15, 182_059),
    ("head_mp_opforce_headwrap", 144_385, 9, 144_549),
    ("viewhands_op_force", 111_775, 2, 111_799),
)

GETAWAY_GFX_B1_ALLOCATIONS = (
    {"label": "reflection runtime", "offset": 0, "alignment": 4, "count": 15, "stride": 20, "array_count": 1, "bytes": 300},
    {"label": "lightmap primary runtime", "offset": 1_324, "alignment": 4, "count": 4, "stride": 20, "array_count": 1, "bytes": 80},
    {"label": "lightmap secondary runtime", "offset": 1_404, "alignment": 4, "count": 4, "stride": 20, "array_count": 1, "bytes": 80},
    {"label": "scene dyn model", "offset": 1_488, "alignment": 4, "count": 235, "stride": 6, "array_count": 1, "bytes": 1_410},
    {"label": "lod", "offset": 1_757_728, "alignment": 16, "count": 108, "stride": 8, "array_count": 2, "bytes": 1_728},
    {"label": "surface materials runtime", "offset": 1_759_456, "alignment": 4, "count": 3_944, "stride": 4, "array_count": 1, "bytes": 15_776},
    {"label": "sun shadow", "offset": 1_775_232, "alignment": 16, "count": 124, "stride": 8, "array_count": 1, "bytes": 992},
)
EXPECTED_GETAWAY_GFX_B1_END = 1_777_024
EXPECTED_GETAWAY_CLIP_B1_ROWS = (
    ("modelPose", 1_777_024, 235, POSE_SIZE),
    ("modelClient", 1_784_544, 235, CLIENT_SIZE),
    ("modelCollision", 1_787_364, 235, COLLISION_SIZE),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _u16be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _i16be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def _u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _u32le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _i32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">i", data, offset)[0]


def _f32be(data: bytes, offset: int) -> float:
    return struct.unpack_from(">f", data, offset)[0]


def _read_z(data: bytes, offset: int, limit: int = 1 << 20) -> tuple[str, int]:
    if offset < 0 or offset >= len(data):
        raise ValueError(f"string starts outside zone: 0x{offset:X}")
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end < 0:
        raise ValueError(f"unterminated string at 0x{offset:X}")
    return data[offset:end].decode("latin-1"), end + 1


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": _sha(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "block") and hasattr(value, "offset"):
        return {"block": int(value.block), "offset": int(value.offset)}
    return str(value)


@dataclasses.dataclass(frozen=True)
class Check:
    check_id: str
    category: str
    passed: bool
    summary: str
    details: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    hard: bool = True


class Audit:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.inventory: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def add(
        self,
        check_id: str,
        category: str,
        passed: bool,
        summary: str,
        details: Mapping[str, Any] | None = None,
        *,
        hard: bool = True,
    ) -> bool:
        row = Check(check_id, category, bool(passed), summary, details or {}, hard)
        self.checks.append(row)
        if hard and not passed:
            self.failures.append(_jsonable(dataclasses.asdict(row)))
        return bool(passed)

    def require(
        self,
        check_id: str,
        category: str,
        condition: bool,
        summary: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.add(check_id, category, condition, summary, details, hard=True)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclasses.dataclass(frozen=True)
class Ps3Frame:
    index: int
    header_offset: int
    stored_bytes: int
    raw_bytes: int
    compressed: bool
    deflate_eof: bool
    trailing_bytes: int
    adler_present: bool
    adler_expected: int | None
    adler_actual: int | None
    adler_valid: bool


@dataclasses.dataclass(frozen=True)
class StrictPs3Document:
    path: str
    file_sha256: str
    zone_sha256: str
    zone: bytes
    frames: tuple[Ps3Frame, ...]
    end_marker_offset: int
    trailer_bytes: int


def read_ps3_fastfile_strict(path: str | Path) -> StrictPs3Document:
    """Read IW3 PS3 framing and expose the retail four-byte Adler suffix.

    ``zlib.decompress(..., wbits=-15)`` accepts trailing bytes, which is why the
    regular project reader correctly obtains the zone but cannot prove that the
    retail Adler suffix exists.  The decompressor object's ``unused_data`` is the
    independent framing oracle used here.
    """

    p = Path(path)
    blob = p.read_bytes()
    if len(blob) < PS3_HEADER_SIZE + 2 or blob[:8] != MAGIC:
        raise ValueError(f"{p}: invalid PS3 FastFile magic/header")
    version = _u32be(blob, 8)
    if version != PS3_VERSION:
        raise ValueError(f"{p}: expected PS3 version {PS3_VERSION}, got {version}")
    cursor = PS3_HEADER_SIZE
    output = bytearray()
    frames: list[Ps3Frame] = []
    end_marker = -1
    while cursor + 2 <= len(blob):
        header = cursor
        stored = _u16be(blob, cursor)
        cursor += 2
        if stored == 1:
            end_marker = header
            break
        if stored == 0:
            if cursor + PS3_RAW_BLOCK > len(blob):
                raise ValueError(f"{p}: raw frame {len(frames)} truncated")
            raw = blob[cursor : cursor + PS3_RAW_BLOCK]
            cursor += PS3_RAW_BLOCK
            frames.append(
                Ps3Frame(
                    len(frames), header, PS3_RAW_BLOCK, len(raw), False, True,
                    0, False, None, None, False,
                )
            )
            output += raw
            continue
        if cursor + stored > len(blob):
            raise ValueError(f"{p}: compressed frame {len(frames)} truncated")
        payload = blob[cursor : cursor + stored]
        cursor += stored
        decoder = zlib.decompressobj(wbits=-15)
        try:
            raw = decoder.decompress(payload) + decoder.flush()
        except zlib.error as exc:
            raise ValueError(f"{p}: frame {len(frames)} raw-DEFLATE failure") from exc
        trailing = decoder.unused_data
        expected = zlib.adler32(raw) & 0xFFFFFFFF
        actual = _u32be(trailing, 0) if len(trailing) == 4 else None
        valid = len(trailing) == 4 and actual == expected
        if not 1 <= len(raw) <= PS3_RAW_BLOCK:
            raise ValueError(f"{p}: frame {len(frames)} expands to {len(raw)} bytes")
        frames.append(
            Ps3Frame(
                len(frames), header, stored, len(raw), True, bool(decoder.eof),
                len(trailing), len(trailing) == 4, expected, actual, valid,
            )
        )
        output += raw
    if end_marker < 0:
        raise ValueError(f"{p}: PS3 FastFile end marker is missing")
    return StrictPs3Document(
        str(p.resolve()), _sha(blob), _sha(bytes(output)), bytes(output),
        tuple(frames), end_marker, len(blob) - cursor,
    )


def _frame_summary(doc: StrictPs3Document) -> dict[str, Any]:
    compressed = [x for x in doc.frames if x.compressed]
    return {
        "path": doc.path,
        "file_sha256": doc.file_sha256,
        "zone_sha256": doc.zone_sha256,
        "zone_bytes": len(doc.zone),
        "frames": len(doc.frames),
        "compressed_frames": len(compressed),
        "raw_frames": len(doc.frames) - len(compressed),
        "deflate_eof_frames": sum(x.deflate_eof for x in compressed),
        "adler_present_frames": sum(x.adler_present for x in compressed),
        "adler_valid_frames": sum(x.adler_valid for x in compressed),
        "trailer_bytes": doc.trailer_bytes,
        "bad_frames": [
            _jsonable(dataclasses.asdict(x))
            for x in doc.frames
            if not x.compressed or not x.deflate_eof or not x.adler_valid
        ][:32],
    }


def _assert_retail_frames(audit: Audit, check_id: str, label: str, doc: StrictPs3Document) -> None:
    frames = tuple(doc.frames)
    compressed = tuple(x for x in frames if x.compressed)
    ok = bool(frames) and len(compressed) == len(frames) and all(
        x.deflate_eof and x.trailing_bytes == 4 and x.adler_valid for x in compressed
    )
    audit.require(
        check_id,
        "container",
        ok,
        f"{label}: every PS3 frame is raw-DEFLATE plus Adler32_BE(raw)",
        _frame_summary(doc),
    )


def _validate_pc_and_iwd(audit: Audit, pc_ff: Path, iwd_paths: Sequence[Path]) -> Any:
    try:
        pc = read_pc_fastfile(pc_ff)
        h = parse_zone_header(pc.zone, "pc")
        al = parse_xasset_list(pc.zone, "pc")
        audit.require(
            "SOURCE.PC.FRAME",
            "source",
            bool(pc.zone) and len(h.block_sizes) == 9,
            "original PC FastFile zlib stream and zone header parse",
            {
                "file_sha256": pc.file_sha256,
                "zone_sha256": pc.zone_sha256,
                "zone_bytes": len(pc.zone),
                "xassets": len(al.assets),
                "script_strings": len(al.script_strings),
                "block_sizes": h.block_sizes,
            },
        )
    except Exception as exc:
        audit.require("SOURCE.PC.FRAME", "source", False, "original PC FastFile parses", {"error": str(exc)})
        raise

    iwd_rows = []
    for path in iwd_paths:
        row: dict[str, Any] = {"path": str(path.resolve())}
        try:
            with zipfile.ZipFile(path, "r") as archive:
                bad = archive.testzip()
                infos = archive.infolist()
                crc_bad = []
                for info in infos:
                    if info.is_dir():
                        continue
                    data = archive.read(info)
                    actual = zlib.crc32(data) & 0xFFFFFFFF
                    if actual != info.CRC:
                        crc_bad.append(info.filename)
                row.update({"entries": len(infos), "testzip_bad": bad, "crc_bad": crc_bad[:20]})
                ok = bad is None and not crc_bad
        except Exception as exc:
            row["error"] = str(exc)
            ok = False
        iwd_rows.append(row)
        audit.require(
            f"SOURCE.IWD.{len(iwd_rows)-1:02d}", "source", ok,
            f"IWD {path.name} ZIP/CRC integrity", row,
        )
    return pc


def ps3_memusage(pc_memory_usage: int, surface_count: int) -> int:
    """Retail-observed PS3 XModel memory accounting.

    PS3 XSurface is 0x14 bytes larger than the PC root.  The XModel-level
    accounting excludes one fixed 0x10-byte PC-only contribution.
    """

    if surface_count < 0:
        raise ValueError("negative XSurface count")
    return (int(pc_memory_usage) + 0x14 * int(surface_count) - 0x10) & 0xFFFFFFFF


def check_xsurface_contract(zone: bytes, root: int, part_bits: Sequence[int]) -> tuple[bool, dict[str, Any]]:
    if len(part_bits) != 4:
        raise ValueError("XSurface partBits contract requires four words")
    if root < 0 or root + PS3_XSURFACE_ROOT_SIZE > len(zone):
        return False, {"root": root, "error": "root outside zone"}
    reserved = _u32be(zone, root + 0x38)
    actual = tuple(_u32be(zone, root + 0x3C + i * 4) for i in range(4))
    expected = tuple(int(x) & 0xFFFFFFFF for x in part_bits)
    return reserved == 0 and actual == expected, {
        "root": root,
        "reserved_0x38": reserved,
        "part_bits": actual,
        "expected_part_bits": expected,
    }


def _packed_in_bounds(raw: int, block_sizes: Sequence[int]) -> bool:
    decoded = decode_ps3_pointer(raw, block_sizes)
    return decoded.kind == "packed" and decoded.block is not None and decoded.offset is not None


def _node_result_key(node: Any, results: Mapping[str, Any]) -> str | None:
    prefixes = {
        "ExternalTechniqueSetNode": ("techset.external:",),
        "ExternalMaterialNode": ("material.external:",),
        "MaterialNode": ("material:",),
        "ImageNode": ("image:",),
        "ExternalImageNode": ("image.external:",),
        "LightDefNode": ("lightdef:",),
        "PhysPresetNode": ("physpreset:",),
        "XModelNode": ("xmodel:",),
        "ComWorldNode": ("comworld:",),
        "GfxWorldNode": ("gfxworld:",),
        "GameWorldMpNode": ("gameworldmp:",),
        "ClipMapNode": ("clipmap:",),
        "RawFileNode": ("rawfile:",),
        "StringTableNode": ("stringtable:",),
        "OwnedFxNode": ("fx.owned:",),
        "ExternalFxNode": ("fx.external:",),
        "ImpactFxNode": ("impactfx:",),
        "SoundNode": ("sound:",),
        "SoundCurveAssetNode": ("sndcurve:",),
        "LoadedSoundAssetNode": ("loadedsound:",),
    }
    hits = [p + node.symbol for p in prefixes.get(node.__class__.__name__, ()) if p + node.symbol in results]
    return hits[0] if len(hits) == 1 else None


def _logical_location(row: Mapping[str, Any]) -> tuple[int, int] | None:
    loc = row.get("root_location")
    if loc is None:
        return None
    if isinstance(loc, Mapping):
        return int(loc["block"]), int(loc["offset"])
    if hasattr(loc, "block") and hasattr(loc, "offset"):
        return int(loc.block), int(loc.offset)
    return None


def _location_tuple(value: Any) -> tuple[int, int] | None:
    """Normalize a graph-result logical address without trusting its class."""

    if value is None:
        return None
    if isinstance(value, Mapping) and "block" in value and "offset" in value:
        return int(value["block"]), int(value["offset"])
    if hasattr(value, "block") and hasattr(value, "offset"):
        return int(value.block), int(value.offset)
    return None


def _topology_rows(assembly: Any, expected: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct loader-safe ordering independently from ``PortPlan.top_level_assets``.

    The first returned list contains dependency/top-owner violations.  The
    second contains raw relocation fields that target a header which has not
    yet been loaded at that physical point.  This uses plan edges plus actual
    serialized pointer words; it does not accept the writer's chosen ordering
    merely because a packed pointer happens to be in bounds.
    """

    plan = assembly.plan
    top = tuple(plan.top_level_assets())
    active = tuple(plan.serialized_assets())
    by_symbol = {node.symbol: node for node in active}
    top_index = {node.symbol: index for index, node in enumerate(top)}
    owner_by_child = {
        edge.child_symbol: edge.owner_symbol
        for edge in plan.active_ownership_edges()
    }

    def top_owner(symbol: str) -> str | None:
        seen: set[str] = set()
        while symbol not in top_index:
            if symbol in seen:
                return None
            seen.add(symbol)
            symbol = owner_by_child.get(symbol, "")
            if not symbol:
                return None
        return symbol

    dependency_failures: list[dict[str, Any]] = []
    prerequisite_pairs: set[tuple[str, str]] = set()
    for node in active:
        consumer = top_owner(node.symbol)
        if consumer is None:
            dependency_failures.append(
                {"reason": "consumer has no top-level owner", "consumer": node.symbol}
            )
            continue
        for dependency in getattr(node, "dependencies", ()):
            required = top_owner(dependency.target_symbol)
            if required is None:
                dependency_failures.append(
                    {
                        "reason": "dependency has no top-level owner",
                        "consumer": node.symbol,
                        "dependency": dependency.target_symbol,
                    }
                )
                continue
            if required == consumer:
                continue
            prerequisite_pairs.add((consumer, required))
            if top_index[required] >= top_index[consumer]:
                dependency_failures.append(
                    {
                        "reason": "top-level prerequisite is not earlier",
                        "consumer_node": node.symbol,
                        "consumer_top": consumer,
                        "consumer_index": top_index[consumer],
                        "dependency": dependency.target_symbol,
                        "required_top": required,
                        "required_index": top_index[required],
                        "description": dependency.description,
                    }
                )

    # Stable Kahn replay: among all currently-loadable roots, retain the first
    # source occurrence.  This independently proves stability, not just DAG-ness.
    source_top = tuple(plan._source_top_level_assets())
    prerequisites = {node.symbol: set() for node in source_top}
    for consumer, required in prerequisite_pairs:
        prerequisites.setdefault(consumer, set()).add(required)
    emitted: set[str] = set()
    stable: list[str] = []
    while len(stable) < len(source_top):
        ready = next(
            (
                node.symbol
                for node in source_top
                if node.symbol not in emitted
                and prerequisites.get(node.symbol, set()) <= emitted
            ),
            None,
        )
        if ready is None:
            dependency_failures.append(
                {
                    "reason": "independent stable topology replay found a cycle",
                    "pending": {
                        node.symbol: sorted(prerequisites.get(node.symbol, set()) - emitted)
                        for node in source_top
                        if node.symbol not in emitted
                    },
                }
            )
            break
        stable.append(ready)
        emitted.add(ready)
    actual_order = [node.symbol for node in top]
    if stable != actual_order:
        dependency_failures.append(
            {
                "reason": "top-level order is not the stable source-order topology",
                "actual": actual_order,
                "stable_replay": stable,
            }
        )

    header_base = len(plan.script_strings) * 4
    header_base += sum(
        len(str(value).encode("latin-1")) + 1
        for value in plan.script_strings
        if value is not None
    )
    header_base = (header_base + 3) & ~3
    header_offsets = {
        node.symbol: header_base + index * 8 + 4
        for index, node in enumerate(top)
    }
    roots: list[int] = []
    for node in top:
        key = _node_result_key(node, expected.graph_results)
        row = expected.graph_results.get(key, {}) if key else {}
        roots.append(int(row.get("root_physical", -1)))
    header_failures: list[dict[str, Any]] = []
    for relocation in expected.relocation_rows:
        target = str(relocation.get("target_symbol", ""))
        if not target.endswith(XASSET_HEADER_SUFFIX):
            continue
        required = target[: -len(XASSET_HEADER_SUFFIX)]
        field = int(relocation["physical_field_offset"])
        consumer_index = bisect_right(roots, field) - 1
        if consumer_index < 0 or consumer_index >= len(top):
            header_failures.append(
                {
                    "reason": "header relocation is outside every top-level load interval",
                    "field": field,
                    "target": required,
                }
            )
            continue
        consumer = top[consumer_index].symbol
        target_index = top_index.get(required)
        expected_header_offset = header_offsets.get(required)
        failure = (
            target_index is None
            or target_index >= consumer_index
            or int(relocation.get("target_block", -1)) != 4
            or int(relocation.get("target_offset", -1)) != expected_header_offset
        )
        if failure:
            header_failures.append(
                {
                    "reason": "forward/invalid XAssetHeader reference",
                    "field": field,
                    "consumer": consumer,
                    "consumer_index": consumer_index,
                    "target": required,
                    "target_index": target_index,
                    "target_block": relocation.get("target_block"),
                    "target_offset": relocation.get("target_offset"),
                    "expected_header_offset": expected_header_offset,
                    "description": relocation.get("description"),
                }
            )
    return dependency_failures, header_failures


def _root_alignment_for(node: Any) -> int:
    # GraphContext.serialize_node applies this common allocator gate to every
    # PS3 XAsset root, including inline-owned support assets.  This is a logical
    # destination alignment; the FastFile bytes deliberately stay adjacent.
    del node
    return 4


def _build_inventory(
    audit: Audit,
    assembly: Any,
    expected: Any,
    candidate_zone: bytes,
    readback_report: Any,
    xasset_list: Any,
) -> None:
    top = tuple(assembly.plan.top_level_assets())
    active = tuple(assembly.plan.serialized_assets())
    top_symbols = {node.symbol for node in top}
    rows: list[dict[str, Any]] = []
    root_failures: list[dict[str, Any]] = []
    alignment_failures: list[dict[str, Any]] = []
    family_by_symbol = {
        (str(x.get("symbol")), str(x.get("type"))): x
        for x in readback_report.family_checks
    }
    for node in active:
        key = _node_result_key(node, expected.graph_results)
        result = expected.graph_results.get(key, {}) if key else {}
        physical = int(result.get("root_physical", -1))
        logical = _logical_location(result)
        alignment = _root_alignment_for(node)
        in_bounds = PS3_ZONE_HEADER_SIZE <= physical < len(candidate_zone)
        aligned = logical is not None and logical[0] == int(node.root_block) and logical[1] % alignment == 0
        row = {
            "placement": "top-level" if node.symbol in top_symbols else "owned-support",
            "type": node.type.name,
            "class": node.__class__.__name__,
            "symbol": node.symbol,
            "result_key": key,
            "root_physical": physical,
            "root_logical": None if logical is None else {"block": logical[0], "offset": logical[1]},
            "required_alignment": alignment,
            "root_in_bounds": in_bounds,
            "root_aligned": aligned,
        }
        family = family_by_symbol.get((node.symbol, node.type.name))
        row["family_passed"] = bool(family and family.get("passed", False))
        row["family_errors"] = [] if family is None else list(family.get("errors", ()))
        row["family_proof"] = {} if family is None else _jsonable(family.get("proof", {}))
        rows.append(row)
        if not in_bounds:
            root_failures.append(row)
        if not aligned:
            alignment_failures.append(row)
    audit.inventory = rows
    audit.require(
        "ASSET.ROOTS.ALL",
        "asset_inventory",
        not root_failures and len(rows) == len(active),
        "every serialized top-level/support asset has a typed root inside the candidate zone",
        {"checked": len(rows), "failures": root_failures[:32]},
    )
    audit.require(
        "ALIGN.ROOTS.ALL",
        "alignment",
        not alignment_failures,
        "every top-level and inline-owned root is in its declared block and uint32-aligned logically",
        {"checked": len(rows), "failures": alignment_failures[:32]},
    )
    family_failures = [x for x in rows if not x["family_passed"]]
    audit.require(
        "ASSET.FAMILIES.ALL",
        "asset_struct",
        not family_failures,
        "every serialized asset/support node passes its family-specific PS3 struct contract",
        {"checked": len(rows), "failures": family_failures[:32]},
    )

    # Root-boundary proof in physical loader order.  Header pointer cells are
    # persistent B4 entries.  Inline-owned roots must occur after their owner
    # and before the next top-level root, even when the child itself uses TEMP.
    row_by_symbol = {x["symbol"]: x for x in rows}
    top_roots = [int(row_by_symbol[node.symbol]["root_physical"]) for node in top]
    expected_type_order = [int(node.type) for node in top]
    actual_type_order = [int(asset.type_id) for asset in xasset_list.assets]
    boundary_failures: list[dict[str, Any]] = []
    if not top_roots or top_roots[0] != int(xasset_list.asset_data_offset):
        boundary_failures.append(
            {
                "reason": "first root is not immediately after the XAssetHeader pool",
                "first_root": top_roots[0] if top_roots else None,
                "asset_data_offset": int(xasset_list.asset_data_offset),
            }
        )
    if any(left >= right for left, right in zip(top_roots, top_roots[1:])):
        boundary_failures.append({"reason": "top-level roots are not strictly increasing", "roots": top_roots[:32]})
    if actual_type_order != expected_type_order:
        boundary_failures.append(
            {
                "reason": "XAssetHeader type order differs from the typed graph",
                "actual": actual_type_order[:64],
                "expected": expected_type_order[:64],
            }
        )
    header_failures = []
    header_base = len(assembly.plan.script_strings) * 4
    header_base += sum(len(str(value).encode("latin-1")) + 1 for value in assembly.plan.script_strings if value is not None)
    header_base = (header_base + 3) & ~3
    for asset in xasset_list.assets:
        # Replay B4 independently: ScriptString pointer array, inline XStrings,
        # align4, then the eight-byte XAssetHeader records. The raw 0x10-byte
        # XAssetList root is physically present but outside every loader block.
        logical_pointer_cell = header_base + int(asset.index) * 8 + 4
        if logical_pointer_cell < 0 or logical_pointer_cell & 3 or logical_pointer_cell >= int(expected.block_sizes[4]):
            header_failures.append(
                {
                    "index": asset.index,
                    "record_physical": asset.record_offset,
                    "b4_pointer_cell": logical_pointer_cell,
                }
            )
    if header_failures:
        boundary_failures.append(
            {"reason": "XAssetHeader cells are not valid align4 B4 boundaries", "failures": header_failures[:32]}
        )

    placement = expected.graph_results.get("graph.placement", {})
    owned_nodes = list(placement.get("owned_nodes", ())) if isinstance(placement, Mapping) else []
    owner_by_child = {str(x["child_symbol"]): str(x["owner_symbol"]) for x in owned_nodes}
    top_index_by_symbol = {node.symbol: i for i, node in enumerate(top)}

    def top_ancestor(symbol: str) -> str | None:
        seen: set[str] = set()
        while symbol not in top_index_by_symbol:
            if symbol in seen or symbol not in owner_by_child:
                return None
            seen.add(symbol)
            symbol = owner_by_child[symbol]
        return symbol

    owned_failures = []
    for item in owned_nodes:
        child_symbol = str(item["child_symbol"])
        owner_symbol = str(item["owner_symbol"])
        child = row_by_symbol.get(child_symbol)
        owner = row_by_symbol.get(owner_symbol)
        ancestor = top_ancestor(child_symbol)
        if child is None or owner is None or ancestor is None:
            owned_failures.append({"reason": "owned placement cannot be resolved", "placement": _jsonable(item)})
            continue
        child_root = int(child["root_physical"])
        owner_root = int(owner["root_physical"])
        top_index = top_index_by_symbol[ancestor]
        interval_end = top_roots[top_index + 1] if top_index + 1 < len(top_roots) else len(expected.serialized_zone)
        if not owner_root < child_root < interval_end:
            owned_failures.append(
                {
                    "reason": "owned root lies outside its owner/top-level interval",
                    "owner": owner_symbol,
                    "child": child_symbol,
                    "top_ancestor": ancestor,
                    "owner_root": owner_root,
                    "child_root": child_root,
                    "interval_end": interval_end,
                    "site": item.get("site"),
                }
            )
    if owned_failures:
        boundary_failures.append({"reason": "inline-owned root failures", "failures": owned_failures[:32]})
    all_roots = [int(x["root_physical"]) for x in rows]
    if len(set(all_roots)) != len(all_roots):
        boundary_failures.append({"reason": "two asset identities share one physical root"})
    audit.require(
        "ASSET.ROOT_BOUNDARIES",
        "asset_inventory",
        not boundary_failures and len(owned_nodes) == len(active) - len(top),
        "XAssetHeader order and every top-level/owned root boundary match the ownership forest",
        {
            "top_level_roots": len(top_roots),
            "owned_roots": len(owned_nodes),
            "active_roots": len(active),
            "first_top_root": top_roots[0] if top_roots else None,
            "last_top_root": top_roots[-1] if top_roots else None,
            "failures": boundary_failures[:32],
        },
    )


def _audit_xassetlist_raw_contract(
    audit: Audit,
    zone: bytes,
    xasset_list: Any,
    assembly: Any,
    expected: Any,
) -> None:
    """Prove that the raw 0x10 XAssetList root consumes no B4 bytes."""

    raw_root = PS3_ZONE_HEADER_SIZE
    script_count = len(assembly.plan.script_strings)
    top = tuple(assembly.plan.top_level_assets())
    physical_script_array = raw_root + 0x10
    physical_strings_end = physical_script_array + script_count * 4
    for script in xasset_list.script_strings:
        if script.serialized_pointer == FOLLOWING:
            _value, physical_strings_end = _read_z(zone, physical_strings_end)
    logical_header_base = script_count * 4
    logical_header_base += sum(
        len(str(value).encode("latin-1")) + 1
        for value in assembly.plan.script_strings
        if value is not None
    )
    logical_header_base = (logical_header_base + 3) & ~3
    raw_words = tuple(_u32be(zone, raw_root + index * 4) for index in range(4))
    expected_raw_words = (
        script_count,
        FOLLOWING if script_count else NULL,
        len(top),
        FOLLOWING,
    )
    header_offsets = []
    header_failures = []
    for asset, node in zip(xasset_list.assets, top):
        logical_pointer = logical_header_base + int(asset.index) * 8 + 4
        header_offsets.append(logical_pointer)
        matches = [
            row
            for row in expected.relocation_rows
            if str(row.get("target_symbol", "")) == node.symbol + XASSET_HEADER_SUFFIX
        ]
        for row in matches:
            if int(row.get("target_block", -1)) != 4 or int(row.get("target_offset", -1)) != logical_pointer:
                header_failures.append(
                    {
                        "asset": asset.index,
                        "symbol": node.symbol,
                        "logical_pointer": logical_pointer,
                        "relocation": _jsonable(row),
                    }
                )
    physical_ok = (
        raw_words == expected_raw_words
        and int(xasset_list.asset_pool_offset) == physical_strings_end
        and int(xasset_list.asset_data_offset) == int(xasset_list.asset_pool_offset) + len(top) * 8
    )
    logical_ok = (
        logical_header_base >= script_count * 4
        and (not header_offsets or header_offsets[0] == logical_header_base + 4)
        and logical_header_base + len(top) * 8 <= int(expected.block_sizes[4])
        and not header_failures
    )
    # If the raw root were accidentally charged to B4, every header pointer
    # would be shifted by exactly 0x10.  Make that forbidden offset explicit.
    shifted_by_raw_root = [offset + 0x10 for offset in header_offsets]
    audit.require(
        "XASSETLIST.RAW_ROOT.OUTSIDE_B4",
        "asset_inventory",
        physical_ok and logical_ok,
        "raw 0x10-byte XAssetList root is physical-only; B4 begins at ScriptString[0]",
        {
            "raw_root_physical": raw_root,
            "raw_words": raw_words,
            "expected_raw_words": expected_raw_words,
            "physical_script_array": physical_script_array,
            "physical_asset_pool": xasset_list.asset_pool_offset,
            "physical_asset_data": xasset_list.asset_data_offset,
            "logical_b4_script_start": 0,
            "logical_header_base": logical_header_base,
            "first_header_pointer": header_offsets[0] if header_offsets else None,
            "forbidden_first_header_if_raw_root_were_b4": shifted_by_raw_root[0] if shifted_by_raw_root else None,
            "header_failures": header_failures[:32],
        },
    )


def _audit_loader_safe_topology(audit: Audit, assembly: Any, expected: Any) -> None:
    dependency_failures, header_failures = _topology_rows(assembly, expected)
    audit.require(
        "XASSET.ORDER.LOADER_SAFE",
        "asset_inventory",
        not dependency_failures,
        "stable top-level order loads every dependency/top-owner before its consumer",
        {
            "top_level_order": [node.symbol for node in assembly.plan.top_level_assets()],
            "failures": dependency_failures[:64],
        },
    )
    audit.require(
        "XASSET.ORDER.NO_FORWARD_HEADERS",
        "pointer",
        not header_failures,
        "every actual XAssetHeader relocation targets an earlier loaded B4 header cell",
        {
            "header_relocations": sum(
                str(row.get("target_symbol", "")).endswith(XASSET_HEADER_SUFFIX)
                for row in expected.relocation_rows
            ),
            "failures": header_failures[:64],
        },
    )


def _audit_default_normal_matrix(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    """Check every graph-result address whose loader block is structurally known."""

    failures: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []

    def expect_location(
        owner: str,
        label: str,
        value: Any,
        block: int,
        *,
        required: bool = True,
        alignment: int = 1,
    ) -> None:
        location = _location_tuple(value)
        passed = location is not None and location[0] == block and location[1] % alignment == 0
        if value is None and not required:
            return
        proof = {
            "owner": owner,
            "field": label,
            "location": None if location is None else {"block": location[0], "offset": location[1]},
            "expected_block": block,
            "alignment": alignment,
        }
        observations.append(proof)
        if not passed:
            failures.append(proof)

    for node in assembly.plan.serialized_assets():
        key = _node_result_key(node, expected.graph_results)
        row = expected.graph_results.get(key, {}) if key else {}
        owner = f"{node.__class__.__name__}:{node.symbol}"
        if not row:
            failures.append({"owner": owner, "reason": "missing graph result"})
            continue

        # Common default-normal members exposed by multiple families.
        for field in ("name_location", "buffer_location", "table_location"):
            if field in row:
                expect_location(owner, field, row[field], 4, required=row[field] is not None)
        for field in ("member_block_start", "member_block_end"):
            if field in row:
                expect_location(owner, field, row[field], 4)

        cls = node.__class__.__name__
        root_physical = int(row.get("root_physical", -1))
        if cls == "ExternalTechniqueSetNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            name, _ = _read_z(zone, int(row["name_physical"]), 1024)
            if name != str(row.get("serialized_name", "")) or _u32be(zone, root_physical) != FOLLOWING:
                failures.append({"owner": owner, "reason": "TechniqueSet root/name bytes", "name": name})
        elif cls == "ExternalMaterialNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            name, _ = _read_z(zone, int(row["name_physical"]), 1024)
            if name != str(row.get("serialized_name", "")) or _u32be(zone, root_physical) != FOLLOWING:
                failures.append({"owner": owner, "reason": "Material root/name bytes", "name": name})
        elif cls == "MaterialNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            for index, location in enumerate(row.get("state_object_locations", ())):
                expect_location(owner, f"state_object[{index}]", location, 0, alignment=4)
        elif cls == "ImageNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            expect_location(owner, "loaddef_location", row.get("loaddef_location"), 0, required=False, alignment=4)
        elif cls == "ExternalImageNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            name, _ = _read_z(zone, int(row["name_physical"]), 1024)
            if not name.startswith(",") or _u32be(zone, root_physical + 0x30) != FOLLOWING:
                failures.append({"owner": owner, "reason": "external GfxImage name/root marker", "name": name})
        elif cls == "ExternalFxNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            name, _ = _read_z(zone, int(row["name_physical"]), 1024)
            if not name.startswith(",") or _u32be(zone, root_physical) != FOLLOWING:
                failures.append({"owner": owner, "reason": "external FxEffectDef name/root marker", "name": name})
        elif cls == "PhysPresetNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
        elif cls == "XModelNode":
            for field in ("member_block_start", "member_block_end"):
                expect_location(owner, field, row.get(field), 4)
            expect_location(
                owner,
                "surface_root_location",
                row.get("surface_root_location"),
                4,
                required=int(row.get("surface_count", 0)) > 0,
                alignment=4,
            )
            expect_location(
                owner,
                "material_handle_location",
                row.get("material_handle_location"),
                4,
                required=int(row.get("surface_count", 0)) > 0,
                alignment=4,
            )
            for field in (
                "collision_location",
                "bone_info_location",
                "phys_geom_location",
            ):
                expect_location(owner, field, row.get(field), 4, required=False, alignment=4)
            expect_location(
                owner,
                "inline_phys_preset_root_location",
                row.get("inline_phys_preset_root_location"),
                0,
                required=False,
                alignment=4,
            )
        elif cls == "ComWorldNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
        elif cls == "LightDefNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            if row.get("attenuation_external"):
                expect_location(owner, "attenuation_external_root_location", row.get("attenuation_external_root_location"), 0, alignment=4)
                expect_location(owner, "attenuation_external_name_location", row.get("attenuation_external_name_location"), 4)
        elif cls == "RawFileNode":
            if row.get("name_symbol") is None:
                expect_location(owner, "name_location", row.get("name_location"), 4)
            expect_location(owner, "buffer_location", row.get("buffer_location"), 4)
        elif cls == "StringTableNode":
            expect_location(owner, "root_location", row.get("root_location"), 4, alignment=4)
        elif cls == "OwnedFxNode":
            expect_location(owner, "member_block_start", row.get("member_block_start"), 4)
            expect_location(owner, "member_block_end", row.get("member_block_end"), 4)
        elif cls == "ImpactFxNode":
            expect_location(owner, "name_location", row.get("name_location"), 4)
            expect_location(owner, "table_location", row.get("table_location"), 4, alignment=4)
        elif cls == "SoundNode":
            expect_location(owner, "member_block_start", row.get("member_block_start"), 4)
            expect_location(owner, "member_block_end", row.get("member_block_end"), 4)
        elif cls == "ClipMapNode":
            expect_location(owner, "mapents_insert_alias_location", row.get("mapents_insert_alias_location"), 4, alignment=4)
            for label, location in row.get("logical_allocations", {}).items():
                block = 0 if label == "mapEnts" or str(label).startswith("ownedPhys.") else 4
                expect_location(owner, f"logical_allocations.{label}", location, block)
            for owned in row.get("physpreset_bindings", {}).get("owned_rows", ()):
                expect_location(owner, f"ownedPhys.{owned.get('list_kind')}.{owned.get('index')}", owned.get("root_location"), 0, alignment=4)
        elif cls == "GfxWorldNode":
            entries = row.get("image_resource_stats", {}).get("entries", ())
            for image in entries:
                label = f"image[{image.get('label')}]"
                expect_location(owner, label + ".shell", image.get("shell_location"), 0, alignment=4)
                expect_location(owner, label + ".loaddef", image.get("loaddef_location"), 0, required=False, alignment=4)
                expect_location(owner, label + ".name", image.get("name_location"), 4)

    audit.require(
        "BLOCK.DEFAULT_NORMAL.MATRIX",
        "alignment",
        not failures,
        "all graph-proven XAsset members use PS3 default-normal B4; nested asset roots remain TEMP",
        {
            "locations_checked": len(observations),
            "failures": failures[:96],
        },
    )


def _audit_loaded_sound_temp_contract(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    failures: list[dict[str, Any]] = []
    checked = 0
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, SoundNode)):
        result = expected.graph_results.get("sound:" + node.symbol, {})
        source_payloads = Counter(
            (len(alias.sound_file.payload), _sha(alias.sound_file.payload))
            for alias in node.plan.aliases
            if alias.sound_file is not None and alias.sound_file.payload
        )
        for child in result.get("loaded_sound_rows", ()):
            checked += 1
            root_location = _location_tuple(child.get("root_location"))
            payload_location = _location_tuple(child.get("payload_location"))
            name_location = _location_tuple(child.get("name_location"))
            symbol_location = _location_tuple(child.get("symbol_location"))
            root_physical = int(child.get("root_physical", -1))
            payload_physical = int(child.get("payload_physical", -1))
            payload_bytes = int(child.get("payload_bytes", -1))
            in_bounds = (
                0 <= root_physical <= len(zone) - 0x1C
                and 0 <= payload_physical <= len(zone) - max(payload_bytes, 0)
            )
            actual_payload = zone[payload_physical : payload_physical + payload_bytes] if in_bounds else b""
            payload_identity = (len(actual_payload), _sha(actual_payload))
            passed = (
                in_bounds
                and root_location is not None
                and payload_location is not None
                and root_location[0] == 0
                and payload_location == (0, root_location[1] + 0x1C)
                and name_location is not None
                and name_location[0] == 4
                and symbol_location is not None
                and symbol_location[0] == 4
                and _u32be(zone, root_physical + 4) == payload_bytes
                and _u32be(zone, root_physical + 0x18) == (FOLLOWING if payload_bytes else NULL)
                and source_payloads[payload_identity] > 0
            )
            if not passed:
                failures.append(
                    {
                        "sound": node.plan.source.name,
                        "identity": child.get("identity"),
                        "root_location": root_location,
                        "payload_location": payload_location,
                        "name_location": name_location,
                        "symbol_location": symbol_location,
                        "root_physical": root_physical,
                        "payload_physical": payload_physical,
                        "payload_bytes": payload_bytes,
                        "payload_identity": payload_identity,
                    }
                )
            elif source_payloads[payload_identity]:
                source_payloads[payload_identity] -= 1
    audit.require(
        "SOUND.LOADEDSOUND.TEMP_STACK",
        "asset_struct",
        checked > 0 and not failures,
        "every nested LoadedSound payload begins at logical TEMP root+0x1C and matches source audio bytes",
        {"checked": checked, "failures": failures[:64]},
    )


def _audit_fx_xmodel_alias_shells(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    expected_names: Counter[str] = Counter()
    reported = 0
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, OwnedFxNode)):
        result = expected.graph_results.get("fx.owned:" + node.symbol, {})
        reported += int(result.get("xmodel_aliases", 0))
        for element in node.source.elements:
            for visual in element.visuals:
                if getattr(getattr(visual, "kind", None), "value", None) == "xmodel_alias":
                    expected_names["," + str(visual.name or "").lstrip(",")] += 1

    observed: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    shell_prefix = struct.pack(">I", FOLLOWING) + b"\0" * (PS3_FX_XMODEL_ALIAS_ROOT_SIZE - 4)
    for name, required_count in expected_names.items():
        needle = name.encode("latin-1") + b"\0"
        cursor = 0
        while True:
            name_physical = zone.find(needle, cursor)
            if name_physical < 0:
                break
            cursor = name_physical + 1
            root = name_physical - PS3_FX_XMODEL_ALIAS_ROOT_SIZE
            if root >= PS3_ZONE_HEADER_SIZE and zone[root:name_physical] == shell_prefix:
                observed[name] += 1
        if observed[name] != required_count:
            failures.append(
                {
                    "name": name,
                    "expected_shells": required_count,
                    "observed_0xCC_shells": observed[name],
                }
            )
    if reported != sum(expected_names.values()):
        failures.append(
            {
                "reason": "graph result/source XModel-alias cardinality mismatch",
                "reported": reported,
                "source": sum(expected_names.values()),
            }
        )
    audit.require(
        "FX.XMODEL_ALIAS.PS3_SHELL",
        "asset_struct",
        bool(expected_names) and not failures,
        "every inline FX XModel visual is a zeroed 0xCC PS3 shell followed by a comma-prefixed external name",
        {
            "expected": dict(expected_names),
            "observed": dict(observed),
            "failures": failures[:64],
        },
    )


def _audit_clipmap_insert_alias_contract(
    audit: Audit,
    zone: bytes,
    block_sizes: Sequence[int],
    assembly: Any,
    expected: Any,
) -> None:
    failures: list[dict[str, Any]] = []
    checked = 0
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, ClipMapNode)):
        checked += 1
        row = expected.graph_results.get("clipmap:" + node.symbol, {})
        root = int(row.get("root_physical", -1))
        source_root = int(node.clip.root_offset)
        source_zone = assembly.source.pc_document.zone
        source_mapents = decode_pc_pointer(_u32le(source_zone, source_root + 0xA4)).kind
        alias_location = _location_tuple(row.get("mapents_insert_alias_location"))
        mapents_location = _location_tuple(row.get("logical_allocations", {}).get("mapEnts"))
        mapents_physical = int(row.get("mapents_physical", -1))
        if not (
            source_mapents == "insert"
            and 0 <= root <= len(zone) - 0x11C
            and _u32be(zone, root + 0xA4) == INSERT
            and alias_location is not None
            and alias_location[0] == 4
            and alias_location[1] % 4 == 0
            and alias_location[1] < int(block_sizes[4])
            and mapents_location is not None
            and mapents_location[0] == 0
            and mapents_location[1] % 4 == 0
            and 0 <= mapents_physical <= len(zone) - 0x0C
            and _u32be(zone, mapents_physical + 4) == FOLLOWING
        ):
            failures.append(
                {
                    "reason": "MapEnts INSERT/TEMP contract",
                    "source_kind": source_mapents,
                    "destination_marker": None if root < 0 else _u32be(zone, root + 0xA4),
                    "alias_location": alias_location,
                    "mapents_location": mapents_location,
                    "mapents_physical": mapents_physical,
                }
            )

        dyn_bases = {
            "model": int(row.get("dyn_model_physical", -1)),
            "brush": int(row.get("dyn_brush_physical", -1)),
        }
        dyn_logical = {
            "model": _location_tuple(row.get("logical_allocations", {}).get("dynModels")),
            "brush": _location_tuple(row.get("logical_allocations", {}).get("dynBrushes")),
        }
        for owner in node.clip.dyn_owned_phys_presets:
            field = dyn_bases[owner.list_kind] + owner.index * DYNDEF_SIZE + 0x30
            expected_marker = INSERT if owner.pointer_kind == "insert" else FOLLOWING
            actual = _u32be(zone, field) if 0 <= field <= len(zone) - 4 else NULL
            if actual != expected_marker:
                failures.append(
                    {
                        "reason": "DynEnt owned PhysPreset marker differs from exact source kind",
                        "list": owner.list_kind,
                        "index": owner.index,
                        "source_kind": owner.pointer_kind,
                        "actual": actual,
                    }
                )

        # Packed Getaway aliases to an earlier DynEnt pointer field can be
        # reconstructed from the B4 DynEnt array itself, without writer symbols.
        for kind, entities in (("model", node.clip.dyn_models), ("brush", node.clip.dyn_brushes)):
            logical = dyn_logical[kind]
            if not entities or logical is None:
                continue
            for entity in entities:
                target_symbol = node.dynent_physpreset_alias_symbols_by_raw.get(entity.phys_preset_raw)
                if target_symbol is None:
                    continue
                try:
                    target_index = int(target_symbol.rsplit(".", 3)[-3])
                    target_kind = target_symbol.rsplit(".", 4)[-4]
                except (ValueError, IndexError):
                    failures.append({"reason": "unparseable DynEnt alias symbol", "symbol": target_symbol})
                    continue
                target_logical = dyn_logical.get(target_kind)
                field = dyn_bases[kind] + entity.index * DYNDEF_SIZE + 0x30
                actual = _u32be(zone, field)
                decoded = decode_ps3_pointer(actual, block_sizes)
                expected_offset = None if target_logical is None else target_logical[1] + target_index * DYNDEF_SIZE + 0x30
                if decoded.kind != "packed" or decoded.block != 4 or decoded.offset != expected_offset:
                    failures.append(
                        {
                            "reason": "DynEnt packed PhysPreset alias misses earlier B4 field",
                            "owner_kind": kind,
                            "owner_index": entity.index,
                            "target_symbol": target_symbol,
                            "decoded": _jsonable(decoded),
                            "expected_offset": expected_offset,
                        }
                    )
    audit.require(
        "CLIPMAP.INSERT_ALIAS.SEMANTICS",
        "pointer",
        checked == 1 and not failures,
        "ClipMap MapEnts/DynEnt markers and packed aliases preserve exact source INSERT/FOLLOWING semantics",
        {"checked": checked, "failures": failures[:64]},
    )


def _audit_expected_readback(
    audit: Audit,
    main_path: Path,
    candidate: StrictPs3Document,
    assembly: Any,
    expected: Any,
) -> Any:
    expected_zone = expected.retail_zone
    exact = candidate.zone == expected_zone
    mismatch = None
    if not exact:
        common = min(len(candidate.zone), len(expected_zone))
        mismatch = next((i for i in range(common) if candidate.zone[i] != expected_zone[i]), common)
    audit.require(
        "ZONE.SOURCE_DERIVED.EXACT",
        "readback",
        exact,
        "candidate decompressed zone exactly matches a fresh source-derived typed C10 serialization",
        {
            "candidate_bytes": len(candidate.zone),
            "expected_bytes": len(expected_zone),
            "candidate_sha256": candidate.zone_sha256,
            "expected_sha256": _sha(expected_zone),
            "first_mismatch": mismatch,
        },
    )
    report = inspect_main_fastfile(main_path, assembly.plan, expected)
    family_failures = [x for x in report.family_checks if not x.get("passed", False)]
    audit.require(
        "ZONE.PROJECT_READBACK",
        "readback",
        bool(report.passed),
        "project deep-readback passes for roots, families, relocations, counts and zone bytes",
        {
            "asset_count": report.asset_count,
            "script_string_count": report.script_string_count,
            "type_counts": report.type_counts,
            "checks": report.checks,
            "relocation_checks": report.relocation_checks,
            "pointer_leaks": report.pointer_leaks,
            "family_count": len(report.family_checks),
            "family_failures": family_failures[:32],
        },
    )
    return report


def _audit_container_and_header(
    audit: Audit,
    candidate: StrictPs3Document,
    assembly: Any,
    expected: Any,
) -> tuple[Any, Any]:
    zone = candidate.zone
    header = parse_zone_header(zone, "ps3")
    assets = parse_xasset_list(zone, "ps3")
    expected_types: dict[int, int] = {}
    for node in assembly.plan.top_level_assets():
        expected_types[int(node.type)] = expected_types.get(int(node.type), 0) + 1
    actual_types: dict[int, int] = {}
    for asset in assets.assets:
        actual_types[asset.type_id] = actual_types.get(asset.type_id, 0) + 1
    audit.require(
        "ZONE.HEADER",
        "container",
        len(header.block_sizes) == 7
        and header.zone_memory_size == len(expected.serialized_zone) - PS3_ZONE_HEADER_SIZE
        and tuple(header.block_sizes) == tuple(expected.block_sizes),
        "PS3 zone header memory size and all seven block high-water marks match source-derived replay",
        {
            "zone_memory_size": header.zone_memory_size,
            "expected_zone_memory_size": len(expected.serialized_zone) - PS3_ZONE_HEADER_SIZE,
            "block_sizes": header.block_sizes,
            "expected_block_sizes": expected.block_sizes,
        },
    )
    audit.require(
        "ZONE.XASSETLIST",
        "asset_inventory",
        len(assets.assets) == len(assembly.plan.top_level_assets())
        and len(assets.script_strings) == len(assembly.plan.script_strings)
        and actual_types == expected_types
        and all(a.serialized_pointer == FOLLOWING for a in assets.assets),
        "XAssetList cardinality, order family counts and root markers are exact",
        {
            "assets": len(assets.assets),
            "expected_assets": len(assembly.plan.top_level_assets()),
            "script_strings": len(assets.script_strings),
            "expected_script_strings": len(assembly.plan.script_strings),
            "type_counts": {PS3_ASSET_TYPES.get(k, hex(k)): v for k, v in actual_types.items()},
            "expected_type_counts": {PS3_ASSET_TYPES.get(k, hex(k)): v for k, v in expected_types.items()},
            "non_following_roots": [a.index for a in assets.assets if a.serialized_pointer != FOLLOWING][:32],
        },
    )
    if assembly.source.map_name.casefold() == "mp_getaway":
        audit.require(
            "GETAWAY.PROFILE",
            "container",
            len(assets.assets) == EXPECTED_GETAWAY_ASSETS
            and len(assets.script_strings) == EXPECTED_GETAWAY_SCRIPT_STRINGS
            and header.block_sizes[1] == EXPECTED_GETAWAY_B1
            and header.block_sizes[3] == EXPECTED_GETAWAY_B3
            and header.block_sizes[6] == EXPECTED_GETAWAY_B6,
            "Getaway invariant asset/string counts and independently audited B1/B3/B6 budgets remain exact",
            {
                "assets": len(assets.assets),
                "script_strings": len(assets.script_strings),
                "b1": header.block_sizes[1],
                "b3": header.block_sizes[3],
                "b6": header.block_sizes[6],
                "expected": {
                    "assets": EXPECTED_GETAWAY_ASSETS,
                    "script_strings": EXPECTED_GETAWAY_SCRIPT_STRINGS,
                    "b1": EXPECTED_GETAWAY_B1,
                    "b3": EXPECTED_GETAWAY_B3,
                    "b6": EXPECTED_GETAWAY_B6,
                },
            },
        )
    return header, assets


def _audit_relocations(
    audit: Audit,
    zone: bytes,
    block_sizes: Sequence[int],
    expected: Any,
) -> None:
    failures = []
    null_losses = []
    temporary_targets = []
    block_counts: dict[int, int] = {}
    for row in expected.relocation_rows:
        off = int(row["physical_field_offset"])
        expected_raw = int(row["serialized_pointer"])
        if off < PS3_ZONE_HEADER_SIZE or off + 4 > len(zone):
            failures.append({"offset": off, "reason": "field outside zone", "row": _jsonable(row)})
            continue
        actual = _u32be(zone, off)
        if actual == NULL:
            null_losses.append({"offset": off, "description": row.get("description"), "target": row.get("target_symbol")})
        decoded = decode_ps3_pointer(actual, block_sizes)
        if int(row.get("target_block", -1)) == 0 or decoded.block == 0:
            temporary_targets.append(
                {
                    "offset": off,
                    "actual": actual,
                    "decoded": _jsonable(decoded),
                    "target_symbol": row.get("target_symbol"),
                    "description": row.get("description"),
                }
            )
        if (
            actual != expected_raw
            or decoded.kind != "packed"
            or decoded.block != int(row["target_block"])
            or decoded.offset != int(row["target_offset"])
        ):
            failures.append(
                {
                    "offset": off,
                    "actual": actual,
                    "expected": expected_raw,
                    "decoded": _jsonable(decoded),
                    "row": _jsonable(row),
                }
            )
        elif decoded.block is not None:
            block_counts[decoded.block] = block_counts.get(decoded.block, 0) + 1
    audit.require(
        "POINTER.RELOCATIONS",
        "pointer",
        not failures and not null_losses,
        "every source-derived packed relocation is present, non-NULL, exact and within its declared PS3 block",
        {
            "checked": len(expected.relocation_rows),
            "target_block_counts": block_counts,
            "null_losses": null_losses[:32],
            "failures": failures[:32],
        },
    )
    audit.require(
        "POINTER.NO_TEMP_TARGETS",
        "pointer",
        not temporary_targets,
        "no serialized packed relocation targets reusable TEMP/block-0 storage",
        {
            "checked": len(expected.relocation_rows),
            "temporary_targets": temporary_targets[:64],
        },
    )


def _audit_runtime_block1(
    audit: Audit,
    header: Any,
    assembly: Any,
    expected: Any,
) -> None:
    """Reject compensating PS3 runtime-size errors hidden by the same B1 sum."""

    if assembly.source.map_name.casefold() != "mp_getaway":
        return
    gfx_nodes = [node for node in assembly.plan.serialized_assets() if isinstance(node, GfxWorldNode)]
    clip_nodes = [node for node in assembly.plan.serialized_assets() if isinstance(node, ClipMapNode)]
    failures: list[dict[str, Any]] = []
    proof: dict[str, Any] = {}
    if len(gfx_nodes) != 1 or len(clip_nodes) != 1:
        failures.append({"reason": "expected exactly one GfxWorld and one ClipMap", "gfx": len(gfx_nodes), "clip": len(clip_nodes)})
    else:
        gfx_row = expected.graph_results.get("gfxworld:" + gfx_nodes[0].symbol, {})
        clip_row = expected.graph_results.get("clipmap:" + clip_nodes[0].symbol, {})
        actual_gfx_allocations = tuple(dict(row) for row in gfx_row.get("runtime_contract_allocations", ()))
        gfx_end = int(gfx_row.get("runtime_bytes", -1))
        proof["gfx_allocations"] = _jsonable(actual_gfx_allocations)
        proof["gfx_end"] = gfx_end
        if actual_gfx_allocations != GETAWAY_GFX_B1_ALLOCATIONS:
            failures.append(
                {
                    "reason": "GfxWorld B1 per-allocation contract differs",
                    "actual": _jsonable(actual_gfx_allocations),
                    "expected": _jsonable(GETAWAY_GFX_B1_ALLOCATIONS),
                }
            )
        if gfx_end != EXPECTED_GETAWAY_GFX_B1_END:
            failures.append(
                {"reason": "GfxWorld B1 end differs", "actual": gfx_end, "expected": EXPECTED_GETAWAY_GFX_B1_END}
            )

        runtime_rows = []
        for raw in clip_row.get("runtime", ()):
            label, location = raw
            block = int(location.block) if hasattr(location, "block") else int(location["block"])
            offset = int(location.offset) if hasattr(location, "offset") else int(location["offset"])
            runtime_rows.append((str(label), block, offset))
        proof["clip_runtime_locations"] = runtime_rows
        expected_clip_locations = [(label, 1, offset) for label, offset, _, _ in EXPECTED_GETAWAY_CLIP_B1_ROWS]
        if runtime_rows != expected_clip_locations:
            failures.append(
                {
                    "reason": "ClipMap B1 allocation starts differ",
                    "actual": runtime_rows,
                    "expected": expected_clip_locations,
                }
            )
        cursor = EXPECTED_GETAWAY_GFX_B1_END
        clip_sizes = []
        for label, offset, count, stride in EXPECTED_GETAWAY_CLIP_B1_ROWS:
            size = count * stride
            clip_sizes.append({"label": label, "offset": offset, "count": count, "stride": stride, "bytes": size})
            if offset != cursor or offset & 3:
                failures.append(
                    {"reason": "frozen ClipMap B1 row is not contiguous/align4", "label": label, "offset": offset, "cursor": cursor}
                )
            cursor = offset + size
        proof["clip_allocations"] = clip_sizes
        proof["combined_end"] = cursor
        if cursor != EXPECTED_GETAWAY_B1 or int(header.block_sizes[1]) != cursor:
            failures.append(
                {
                    "reason": "combined GfxWorld+ClipMap B1 end differs",
                    "computed": cursor,
                    "header": int(header.block_sizes[1]),
                    "expected": EXPECTED_GETAWAY_B1,
                }
            )
    audit.require(
        "RUNTIME.B1.ALLOCATIONS",
        "alignment",
        not failures,
        "every frozen Getaway GfxWorld/ClipMap B1 allocation has the exact PS3 offset, alignment, stride and extent",
        {"proof": proof, "failures": failures},
    )


def _audit_xmodels(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    models = [n for n in assembly.plan.serialized_assets() if isinstance(n, XModelNode)]
    surface_failures = []
    mem_failures = []
    root_failures = []
    surface_count = 0
    total_vertices = 0
    total_triangles = 0
    for node in models:
        result = expected.graph_results.get("xmodel:" + node.symbol)
        if result is None:
            root_failures.append({"model": node.model.name, "reason": "missing graph result"})
            continue
        root = int(result["root_physical"])
        surfaces = int(result["surface_root_physical"])
        model = node.model
        if root < PS3_ZONE_HEADER_SIZE or root + PS3_XMODEL_ROOT_SIZE > len(zone):
            root_failures.append({"model": model.name, "root": root, "reason": "outside zone"})
            continue
        try:
            actual_name, _ = _read_z(zone, root + PS3_XMODEL_ROOT_SIZE, 512)
            root_ok = (
                _u32be(zone, root) == FOLLOWING
                and zone[root + 4] == model.num_bones
                and zone[root + 5] == model.num_root_bones
                and zone[root + 6] == model.surface_count
                and actual_name == model.name
                and _i32be(zone, root + 0x8C) == model.num_collision_surfaces
            )
        except Exception as exc:
            root_ok = False
            actual_name = f"<error: {exc}>"
        if not root_ok:
            root_failures.append({"model": model.name, "root": root, "serialized_name": actual_name})
        actual_mem = _u32be(zone, root + 0xBC)
        expected_mem = ps3_memusage(model.memory_usage, model.surface_count)
        if actual_mem != expected_mem:
            mem_failures.append(
                {
                    "model": model.name,
                    "source_asset_index": model.source_asset_index,
                    "root": root,
                    "pc_mem_usage": model.memory_usage,
                    "surface_count": model.surface_count,
                    "expected_ps3_mem_usage": expected_mem,
                    "actual_ps3_mem_usage": actual_mem,
                }
            )
        for index, surface in enumerate(model.surfaces):
            sr = surfaces + index * PS3_XSURFACE_ROOT_SIZE
            passed, proof = check_xsurface_contract(zone, sr, surface.part_bits)
            if not passed:
                proof.update({"model": model.name, "surface_index": index})
                surface_failures.append(proof)
            surface_count += 1
            total_vertices += surface.vertex_count
            total_triangles += surface.triangle_count
    audit.require(
        "C10.XSURFACE.PARTBITS",
        "c10_fix",
        not surface_failures
        and surface_count > 0
        and (
            assembly.source.map_name.casefold() != "mp_getaway"
            or (len(models) == 333 and surface_count == 1_314)
        ),
        "every PS3 XSurface keeps +0x38 reserved and stores partBits[4] at +0x3C..+0x48",
        {
            "models": len(models),
            "surfaces": surface_count,
            "expected_getaway": {"models": 333, "surfaces": 1_314},
            "failures": surface_failures[:64],
        },
    )
    audit.require(
        "C10.XMODEL.MEMUSAGE",
        "c10_fix",
        not mem_failures and len(models) > 0,
        "every XModel memUsage uses the retail PS3 structural formula PC + 0x14*numSurfs - 0x10",
        {"models": len(models), "failures": mem_failures[:64]},
    )
    audit.require(
        "XMODEL.ROOTS.STRUCTS",
        "asset_struct",
        not root_failures,
        "all XModel roots, names, bone/surface counts and compact collision counts match source semantics",
        {
            "models": len(models),
            "surfaces": surface_count,
            "vertices": total_vertices,
            "triangles": total_triangles,
            "expected_getaway": {"vertices": 561_815, "triangles": 543_202},
            "failures": root_failures[:64],
        },
    )
    if assembly.source.map_name.casefold() == "mp_getaway":
        audit.require(
            "XMODEL.GETAWAY.COUNTS",
            "asset_struct",
            len(models) == 333
            and surface_count == 1_314
            and total_vertices == 561_815
            and total_triangles == 543_202,
            "Getaway XModel inventory preserves all models, XSurfaces, vertices and triangles",
            {
                "models": len(models),
                "surfaces": surface_count,
                "vertices": total_vertices,
                "triangles": total_triangles,
            },
        )


def _audit_comworld(
    audit: Audit,
    zone: bytes,
    block_sizes: Sequence[int],
    assembly: Any,
    expected: Any,
) -> None:
    nodes = [n for n in assembly.plan.serialized_assets() if isinstance(n, ComWorldNode)]
    if len(nodes) != 1:
        audit.require("C10.COMWORLD.XSTRINGS", "c10_fix", False, "exactly one ComWorld node", {"nodes": len(nodes)})
        return
    node = nodes[0]
    row = expected.graph_results["comworld:" + node.symbol]
    root = int(row["root_physical"])
    source = node.source
    failures = []
    try:
        name, cursor = _read_z(zone, root + 0x10)
        count = _i32be(zone, root + 8)
        array = cursor
        nonnull = []
        packed = []
        following = []
        source_nonnull = []
        for index, light in enumerate(source.primary_lights):
            field = array + index * 0x44 + 0x40
            raw = _u32be(zone, field)
            source_raw = int(light.def_name_pointer) & 0xFFFFFFFF
            if source_raw:
                source_nonnull.append(index)
                if raw == NULL:
                    failures.append({"light": index, "field": field, "reason": "source non-NULL became NULL"})
            elif raw != NULL:
                failures.append({"light": index, "field": field, "reason": "source NULL became non-NULL", "raw": raw})
            if raw:
                nonnull.append(index)
            if raw == FOLLOWING:
                following.append(index)
            decoded = decode_ps3_pointer(raw, block_sizes)
            if decode_pc_pointer(source_raw).kind == "packed":
                if decoded.kind != "packed" or decoded.block != 4:
                    failures.append(
                        {"light": index, "field": field, "reason": "packed source XString is not a B4 packed target", "raw": raw, "decoded": _jsonable(decoded)}
                    )
                else:
                    packed.append((index, raw, decoded.offset))
        packed_words = {x[1] for x in packed}
        payload = array + count * 0x44
        inline_name, _ = _read_z(zone, payload, 128)
        if name != source.name:
            failures.append({"reason": "ComWorld name mismatch", "actual": name, "expected": source.name})
        if count != source.primary_light_count:
            failures.append({"reason": "primary light count mismatch", "actual": count, "expected": source.primary_light_count})
        if inline_name != LIGHTDEF_NAME:
            failures.append({"reason": "canonical inline light-def XString mismatch", "actual": inline_name, "expected": LIGHTDEF_NAME})
        if len(packed_words) != 1:
            failures.append({"reason": "packed light-def aliases do not share one canonical B4 target", "distinct": sorted(packed_words)})
        if source.name.casefold().endswith("mp_getaway.d3dbsp") and (
            count != 102
            or source_nonnull != list(range(2, 102))
            or nonnull != list(range(2, 102))
            or following != [2]
            or len(packed) != 99
        ):
            failures.append(
                {
                    "reason": "Getaway exact ComWorld alias cardinality/order",
                    "lights": count,
                    "source_nonnull": source_nonnull,
                    "output_nonnull": nonnull,
                    "following": following,
                    "packed": len(packed),
                }
            )
    except Exception as exc:
        failures.append({"reason": str(exc), "traceback": traceback.format_exc(limit=2)})
        name = None
        count = -1
        nonnull = packed = following = source_nonnull = []
    audit.require(
        "C10.COMWORLD.XSTRINGS",
        "c10_fix",
        not failures and len(source_nonnull) == len(nonnull),
        "all 102 ComWorld light records preserve source NULL/non-NULL semantics; 99 reused names are exact B4 packed XString aliases",
        {
            "root": root,
            "name": name,
            "lights": count,
            "source_nonnull": len(source_nonnull),
            "output_nonnull": len(nonnull),
            "packed_aliases": len(packed),
            "following_indices": following,
            "failures": failures[:64],
        },
    )


def _audit_lightdef(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    nodes = [n for n in assembly.plan.serialized_assets() if isinstance(n, LightDefNode)]
    failures = []
    proofs = []
    if len(nodes) != 1:
        failures.append({"reason": "expected one LightDef", "count": len(nodes)})
    for node in nodes:
        row = expected.graph_results["lightdef:" + node.symbol]
        root = int(row["root_physical"])
        try:
            name, shell = _read_z(zone, root + 0x10, 256)
            declared_shell = int(row.get("attenuation_external_root_physical", -1))
            logical_shell = row.get("attenuation_external_root_location")
            logical_shell_block = int(logical_shell.block) if hasattr(logical_shell, "block") else int((logical_shell or {}).get("block", -1))
            logical_shell_offset = int(logical_shell.offset) if hasattr(logical_shell, "offset") else int((logical_shell or {}).get("offset", -1))
            shell_name, end = _read_z(zone, shell + 0x34, 256)
            proof = {
                "root": root,
                "name": name,
                "attenuation_pointer": _u32be(zone, root + 4),
                "shell": shell,
                "declared_shell": declared_shell,
                "shell_logical": {"block": logical_shell_block, "offset": logical_shell_offset},
                "shell_prefix_sha256": _sha(zone[shell : shell + 0x30]),
                "shell_name_pointer": _u32be(zone, shell + 0x30),
                "shell_name": shell_name,
                "physical_end": end,
            }
            proofs.append(proof)
            if name != LIGHTDEF_NAME:
                failures.append({"reason": "LightDef name", **proof})
            if _u32be(zone, root + 4) != INSERT:
                failures.append({"reason": "LightDef attenuation must own inline shell via INSERT", **proof})
            if shell != declared_shell or logical_shell_block != 0 or logical_shell_offset % 4:
                failures.append({"reason": "external GfxImage root physical order/logical 4-byte alignment", **proof})
            if zone[shell : shell + 0x30] != b"\0" * 0x30:
                failures.append({"reason": "external GfxImage shell +0x00..+0x2F not zero", **proof})
            if _u32be(zone, shell + 0x30) != FOLLOWING:
                failures.append({"reason": "external GfxImage name marker is not FOLLOWING", **proof})
            if shell_name != ATTENUATION_IMAGE_NAME:
                failures.append({"reason": "attenuation image identity mismatch", **proof})
        except Exception as exc:
            failures.append({"node": node.symbol, "reason": str(exc)})
    audit.require(
        "C10.LIGHTDEF.IMAGE_SHELL",
        "c10_fix",
        not failures and len(proofs) == 1,
        "LightDef owns the exact retail 0x34 external GfxImage shell for ,falloff_linear",
        {"proofs": proofs, "failures": failures},
    )


def _audit_rawfile501(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    raw_nodes = [n for n in assembly.plan.top_level_assets() if isinstance(n, RawFileNode)]
    target = next((n for n in raw_nodes if getattr(n, "name", "") == RAWFILE501_GOOD_NAME), None)
    failures = []
    proof: dict[str, Any] = {"rawfile_count": len(raw_nodes)}
    if target is None:
        failures.append({"reason": "source-derived plan has no mp_getaway RawFile"})
    else:
        top_assets = tuple(assembly.plan.top_level_assets())
        top_index = top_assets.index(target)
        row = expected.graph_results["rawfile:" + target.symbol]
        root = int(row["root_physical"])
        try:
            name_raw = _u32be(zone, root)
            decoded_name = decode_ps3_pointer(name_raw, expected.block_sizes)
            length = _u32be(zone, root + 4)
            buffer = int(row["buffer_physical"])
            tables = [n for n in assembly.plan.serialized_assets() if isinstance(n, StringTableNode)]
            table = next((n for n in tables if len(n.values) > 112 and n.values[112] == RAWFILE501_GOOD_NAME), None)
            expected_symbol = table.cell_symbol(112) if table is not None else None
            relocation = next(
                (r for r in expected.relocation_rows if int(r["physical_field_offset"]) == root),
                None,
            )
            cell_physical = -1
            if table is not None:
                table_row = expected.graph_results["stringtable:" + table.symbol]
                cell_physical = int(table_row["payload_physical"])
                for value in table.values[:112]:
                    if value is not None:
                        cell_physical += len(value.encode("latin-1")) + 1
            cell_value, _ = _read_z(zone, cell_physical, 256) if cell_physical >= 0 else (None, -1)
            proof.update(
                {
                    "root": root,
                    "xasset_index": top_index,
                    "name_pointer": name_raw,
                    "name_decoded": _jsonable(decoded_name),
                    "relocation": _jsonable(relocation),
                    "expected_stringtable_cell_symbol": expected_symbol,
                    "stringtable_cell_index": 112,
                    "stringtable_cell_physical": cell_physical,
                    "stringtable_cell_value": cell_value,
                    "length": length,
                    "buffer": buffer,
                    "payload_sha256": _sha(zone[buffer : buffer + length]),
                    "source_payload_sha256": _sha(target.data),
                }
            )
            if table is None or cell_value != RAWFILE501_GOOD_NAME:
                failures.append({"reason": "StringTable cell 112 is not the exact mp_getaway XString owner"})
            if top_index != 501:
                failures.append({"reason": "mp_getaway RawFile is not destination XAsset #501", "actual": top_index})
            if (
                decoded_name.kind != "packed"
                or decoded_name.block != 4
                or relocation is None
                or relocation.get("target_symbol") != expected_symbol
                or int(relocation.get("target_block", -1)) != 4
                or decoded_name.offset != int(relocation.get("target_offset", -1))
                or int(relocation.get("serialized_pointer", -1)) != name_raw
            ):
                failures.append({"reason": "RawFile name is not the exact packed B4 alias to StringTable cell 112"})
            if _u32be(zone, root + 8) != FOLLOWING:
                failures.append({"reason": "RawFile buffer marker is not FOLLOWING"})
            if length != len(target.data) or zone[buffer : buffer + length] != target.data or zone[buffer + length] != 0:
                failures.append({"reason": "RawFile payload/terminator mismatch"})
        except Exception as exc:
            failures.append({"reason": str(exc)})
    bad_occurrences = zone.count(RAWFILE501_BAD_NAME.encode("latin-1") + b"\0")
    good_occurrences = zone.count(RAWFILE501_GOOD_NAME.encode("latin-1") + b"\0")
    proof.update({"bad_placeholder_occurrences": bad_occurrences, "mp_getaway_string_occurrences": good_occurrences})
    if bad_occurrences:
        failures.append({"reason": "legacy placeholder RawFile key remains", "occurrences": bad_occurrences})
    audit.require(
        "C10.RAWFILE501.IDENTITY",
        "c10_fix",
        not failures,
        "final RawFile uses the exact packed B4 alias to StringTable cell 112 (mp_getaway) and preserves its payload",
        {"proof": proof, "failures": failures},
    )


def _audit_stringtable_nested_alignment(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    """Replay StringTable's variable-name transition without writer metadata.

    Logical AlignStream padding is absent from the physical FastFile.  Derive
    the B4 pointer-array/cell offsets from the root, shape and XStrings, then
    compare every observable packed relocation against those offsets.
    """

    tables = [node for node in assembly.plan.serialized_assets() if isinstance(node, StringTableNode)]
    failures: list[dict[str, Any]] = []
    table_proofs = []
    expected_cell_locations: dict[str, tuple[int, int]] = {}
    for node in tables:
        row = expected.graph_results.get("stringtable:" + node.symbol, {})
        root = int(row.get("root_physical", -1))
        logical = _logical_location(row)
        proof: dict[str, Any] = {"name": node.name, "symbol": node.symbol, "root_physical": root, "root_logical": logical}
        try:
            if logical is None or logical[0] != 4 or logical[1] & 3:
                raise ValueError("StringTable root is not align4 in B4")
            if root < PS3_ZONE_HEADER_SIZE or root + 0x10 > len(zone):
                raise ValueError("StringTable root outside zone")
            name_physical = int(row["name_physical"])
            pointer_physical = int(row["pointer_array_physical"])
            payload_physical = int(row["payload_physical"])
            actual_name, physical_after_name = _read_z(zone, name_physical, 1 << 16)
            pointer_logical = (logical[1] + 0x10 + len(node.name.encode("latin-1")) + 1 + 3) & ~3
            payload_logical = pointer_logical + len(node.values) * 4
            proof.update(
                {
                    "serialized_name": actual_name,
                    "pointer_array_physical": pointer_physical,
                    "pointer_array_logical": {"block": 4, "offset": pointer_logical},
                    "payload_physical": payload_physical,
                    "payload_logical": {"block": 4, "offset": payload_logical},
                    "values": len(node.values),
                }
            )
            if (
                _u32be(zone, root) != FOLLOWING
                or _u32be(zone, root + 4) != int(node.columns)
                or _u32be(zone, root + 8) != int(node.rows)
                or _u32be(zone, root + 0x0C) != (FOLLOWING if node.values else NULL)
            ):
                raise ValueError("StringTable root words differ from its typed shape")
            if actual_name != node.name or name_physical != root + 0x10:
                raise ValueError("StringTable name identity/physical boundary differs")
            if pointer_physical != physical_after_name:
                raise ValueError("StringTable inserted physical padding before its logical-only alignment")
            if pointer_logical & 3:
                raise ValueError("StringTable pointer array logical start is not align4")
            if payload_physical != pointer_physical + len(node.values) * 4:
                raise ValueError("StringTable pointer-array physical extent differs")

            cell_physical = payload_physical
            cell_logical = payload_logical
            nonnull = 0
            for index, value in enumerate(node.values):
                raw = _u32be(zone, pointer_physical + index * 4)
                if value is None:
                    if raw != NULL:
                        failures.append(
                            {"table": node.name, "cell": index, "reason": "NULL source cell has non-NULL pointer", "raw": raw}
                        )
                    continue
                nonnull += 1
                if raw != FOLLOWING:
                    failures.append(
                        {"table": node.name, "cell": index, "reason": "non-NULL cell pointer is not FOLLOWING", "raw": raw}
                    )
                actual_value, next_physical = _read_z(zone, cell_physical, 1 << 20)
                if actual_value != value:
                    failures.append(
                        {"table": node.name, "cell": index, "reason": "cell XString differs", "actual": actual_value, "expected": value}
                    )
                expected_cell_locations[node.cell_symbol(index)] = (4, cell_logical)
                encoded_bytes = len(value.encode("latin-1")) + 1
                if next_physical != cell_physical + encoded_bytes:
                    failures.append({"table": node.name, "cell": index, "reason": "cell physical extent differs"})
                cell_physical = next_physical
                cell_logical += encoded_bytes
            proof.update(
                {
                    "nonnull_cells": nonnull,
                    "cell_payload_physical_end": cell_physical,
                    "cell_payload_logical_end": cell_logical,
                }
            )
        except Exception as exc:
            failures.append({"table": node.name, "reason": str(exc)})
        table_proofs.append(proof)

    alias_rows = []
    for relocation in expected.relocation_rows:
        target_symbol = str(relocation.get("target_symbol", ""))
        target = expected_cell_locations.get(target_symbol)
        if target is None:
            continue
        field = int(relocation["physical_field_offset"])
        raw = _u32be(zone, field) if 0 <= field <= len(zone) - 4 else NULL
        decoded = decode_ps3_pointer(raw, expected.block_sizes)
        passed = (
            int(relocation["target_block"]) == target[0]
            and int(relocation["target_offset"]) == target[1]
            and decoded.kind == "packed"
            and decoded.block == target[0]
            and decoded.offset == target[1]
        )
        alias = {
            "field": field,
            "target_symbol": target_symbol,
            "expected_block": target[0],
            "expected_offset": target[1],
            "actual": raw,
            "decoded": _jsonable(decoded),
            "description": relocation.get("description"),
            "passed": passed,
        }
        alias_rows.append(alias)
        if not passed:
            failures.append({"reason": "StringTable cell packed alias differs from independent logical replay", **alias})

    cell112_symbol = next(
        (
            node.cell_symbol(112)
            for node in tables
            if len(node.values) > 112 and node.values[112] == RAWFILE501_GOOD_NAME
        ),
        None,
    )
    cell112_aliases = [row for row in alias_rows if row["target_symbol"] == cell112_symbol]
    if cell112_symbol is None or not cell112_aliases or not all(row["passed"] for row in cell112_aliases):
        failures.append(
            {
                "reason": "mp_getaway StringTable cell112 has no exact packed consumer proof",
                "symbol": cell112_symbol,
                "aliases": cell112_aliases,
            }
        )
    audit.require(
        "ALIGN.NESTED.STRINGTABLE",
        "alignment",
        bool(tables) and not failures,
        "every StringTable pointer array is independently replayed as align4 B4 and all cell aliases hit exact XString offsets",
        {
            "tables": len(tables),
            "cell_symbols": len(expected_cell_locations),
            "packed_cell_aliases": len(alias_rows),
            "cell112_symbol": cell112_symbol,
            "cell112_aliases": cell112_aliases,
            "proofs": table_proofs,
            "failures": failures[:64],
        },
    )


def _reverse_nonnull_closure(
    audit: Audit,
    zone: bytes,
    assembly: Any,
    expected: Any,
) -> None:
    sites: list[dict[str, Any]] = []

    def site(family: str, owner: str, field: int, source_raw: int, description: str) -> None:
        if not source_raw:
            return
        actual = _u32be(zone, field) if 0 <= field <= len(zone) - 4 else NULL
        decoded = decode_ps3_pointer(actual, expected.block_sizes)
        output_kind_valid = actual in (FOLLOWING, INSERT) or decoded.kind == "packed"
        sites.append(
            {
                "family": family,
                "owner": owner,
                "field": field,
                "source_raw": int(source_raw) & 0xFFFFFFFF,
                "actual": actual,
                "actual_decoded": _jsonable(decoded),
                "description": description,
                "passed": actual != NULL and output_kind_valid,
            }
        )

    # XModel Material** and PhysPreset references.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, XModelNode)):
        row = expected.graph_results["xmodel:" + node.symbol]
        materials = int(row["material_handle_physical"])
        for i, surface in enumerate(node.model.surfaces):
            site("XModel", node.model.name, materials + i * 4, 1, f"material[{i}] {surface.material_name}")
        source_phys = int(getattr(node.model, "phys_preset_pointer", 0) or 0)
        site("XModel", node.model.name, int(row["root_physical"]) + 0xC4, source_phys, "physPreset")

    # ClipMap static models and all four DynEnt XAsset fields.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, ClipMapNode)):
        row = expected.graph_results["clipmap:" + node.symbol]
        for i, item in enumerate(node.clip.static_models):
            site("ClipMap", f"static[{i}]", int(row["static_model_physical"]) + i * STATIC_MODEL_SIZE + 4, item.xmodel_raw, "xmodel")
        for category, items, base_key in (
            ("model", node.clip.dyn_models, "dyn_model_physical"),
            ("brush", node.clip.dyn_brushes, "dyn_brush_physical"),
        ):
            base = int(row[base_key])
            for i, item in enumerate(items):
                for off, raw, desc in (
                    (0x20, item.xmodel_raw, "xmodel"),
                    (0x28, item.destroy_fx_raw, "destroyFx"),
                    (0x2C, item.destroy_pieces_raw, "destroyPieces"),
                    (0x30, item.phys_preset_raw, "physPreset"),
                ):
                    site("ClipMap", f"dyn{category}[{i}]", base + i * DYNDEF_SIZE + off, raw, desc)

    # GfxWorld material and static-XModel slots.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, GfxWorldNode)):
        row = expected.graph_results["gfxworld:" + node.symbol]
        for i in range(node.report.surface_count):
            site("GfxWorld", f"surface[{i}]", int(row["surface_physical"]) + i * PS3_SURFACE + 0x14, 1, "material")
        for i in range(node.report.static_model_count):
            site("GfxWorld", f"draw[{i}]", int(row["draw_physical"]) + i * PS3_DRAW + 0x20, 1, "xmodel")

    # Sound alias soundFile fields: this is the inverse check that caught 367
    # silently nulled values in the pre-C09 writer.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, SoundNode)):
        row = expected.graph_results["sound:" + node.symbol]
        root = int(row["root_physical"])
        _, heads = _read_z(zone, root + 0x0C, 1024)
        for i, alias in enumerate(node.plan.aliases):
            source_raw = 1 if alias.source.sound_file is not None else 0
            site("Sound", node.plan.source.name, heads + i * SOUND_ALIAS_SIZE + 0x10, source_raw, f"alias[{i}].soundFile")

    # FX visual/effect fields.  The root pointer proves table ownership for
    # multi-visual elements; single runners are checked at +0xBC directly.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, OwnedFxNode)):
        row = expected.graph_results["fx.owned:" + node.symbol]
        root = int(row["root_physical"])
        _, elements = _read_z(zone, root + 0x20, 1024)
        for element in node.source.elements:
            eroot = elements + element.index * 0xFC
            site(
                "FX",
                node.source.name,
                eroot + 0xBC,
                int(element.visual_pointer_raw),
                f"element[{element.index}].visual",
            )
            for off, ref, desc in (
                (0xD8, element.effect_on_impact, "impact"),
                (0xDC, element.effect_on_death, "death"),
                (0xE0, element.effect_emitted, "emitted"),
            ):
                source_nonnull = 0 if ref.pointer_kind == "null" else 1
                site("FX", node.source.name, eroot + off, source_nonnull, f"element[{element.index}].{desc}")

    # PhysPreset sound aliases.
    for node in (n for n in assembly.plan.serialized_assets() if isinstance(n, PhysPresetNode)):
        row = expected.graph_results["physpreset:" + node.symbol]
        site(
            "PhysPreset", node.preset.name, int(row["root_physical"]) + 0x1C,
            1 if node.preset.sound_alias_prefix is not None else 0, "soundAliasPrefix",
        )

    failures = [x for x in sites if not x["passed"]]
    counts: dict[str, int] = {}
    for row in sites:
        counts[row["family"]] = counts.get(row["family"], 0) + 1
    diagnostics = dict(assembly.diagnostics)
    semantic_ok = (
        int(diagnostics.get("clipmap_runtime_null_physpreset_targets", 0)) == 0
        and int(diagnostics.get("fx_runtime_null_visuals", 0)) == 0
        and int(diagnostics.get("sound", {}).get("runtime_null_soundfile_references", 0)) == 0
    )
    audit.require(
        "SOURCE.NONNULL.REVERSE_CLOSURE",
        "pointer",
        not failures and semantic_ok,
        "every enumerated non-NULL source reference has a non-NULL destination field (reverse closure)",
        {
            "checked": len(sites),
            "family_counts": counts,
            "failures": failures[:64],
            "semantic_diagnostics": {
                "clipmap_runtime_null_physpreset_targets": diagnostics.get("clipmap_runtime_null_physpreset_targets"),
                "fx_runtime_null_visuals": diagnostics.get("fx_runtime_null_visuals"),
                "sound_runtime_null_soundfiles": diagnostics.get("sound", {}).get("runtime_null_soundfile_references"),
            },
        },
    )


def _audit_delayed_fifo(
    audit: Audit,
    zone: bytes,
    header: Any,
    expected: Any,
    *,
    map_name: str,
) -> None:
    entries: list[dict[str, Any]] = []
    for key, value in expected.graph_results.items():
        if not isinstance(value, Mapping):
            continue
        stats = value.get("image_resource_stats")
        if isinstance(stats, Mapping):
            for raw in stats.get("entries", ()):
                row = dict(raw)
                row["owner"] = key
                entries.append(row)
        if key.startswith("image:") and int(value.get("resource_physical", -1)) >= 0:
            entries.append(
                {
                    "owner": key,
                    "name": value.get("name"),
                    "resource_physical": value.get("resource_physical"),
                    "declared_resource_bytes": value.get("declared_resource_bytes", value.get("resource_bytes", 0)),
                    "resource_sha256": value.get("sha256"),
                    "embedded": value.get("embedded", True),
                }
            )
    # Block 3 is not a generic resource block in the reconciled PS3 contract.  It
    # is the one global delayed-image FIFO, flushed only after the complete
    # ordinary XAsset stream.  In particular, self-contained block-6 resources
    # must never be allowed to make this check pass by being included in the
    # byte sum.  Restrict the audit to rows that explicitly participated in the
    # deferred FIFO and fail closed if such a row was not finalized into B3.
    deferred = [x for x in entries if bool(x.get("deferred", False))]
    fifo_rows = sorted(deferred, key=lambda x: int(x.get("fifo_index", -1)))
    ranges = []
    failures = []
    seen_fifo_indices: set[int] = set()
    previous_logical_end = 0
    for expected_index, row in enumerate(fifo_rows):
        fifo_index = int(row.get("fifo_index", -1))
        logical_block = int(row.get("resource_logical_block", -1))
        logical_offset = int(row.get("resource_logical_offset", -1))
        if fifo_index != expected_index or fifo_index in seen_fifo_indices:
            failures.append(
                {
                    "reason": "delayed resource FIFO indices are not unique and gap-free",
                    "expected_index": expected_index,
                    "actual_index": fifo_index,
                    "row": _jsonable(row),
                }
            )
        seen_fifo_indices.add(fifo_index)
        if logical_block != 3:
            failures.append({"reason": "deferred resource is not allocated in block 3", "row": _jsonable(row)})
        if logical_offset < 0 or logical_offset & 0x7F:
            failures.append({"reason": "deferred resource logical offset is not 0x80 aligned", "row": _jsonable(row)})
        if logical_offset != previous_logical_end:
            failures.append(
                {
                    "reason": "block-3 logical FIFO has an unexpected gap or overlap",
                    "expected_offset": previous_logical_end,
                    "actual_offset": logical_offset,
                    "row": _jsonable(row),
                }
            )
        if row.get("resource_physical") is None:
            failures.append({"reason": "deferred resource was never flushed to a physical range", "row": _jsonable(row)})
            continue
        start = int(row["resource_physical"])
        size = int(row["declared_resource_bytes"])
        end = start + size
        previous_logical_end = logical_offset + size
        if start < PS3_ZONE_HEADER_SIZE or end > len(expected.serialized_zone):
            failures.append({"reason": "range outside serialized zone", "row": _jsonable(row)})
            continue
        expected_sha = str(row.get("resource_sha256") or row.get("sha256") or "").upper()
        actual_sha = _sha(zone[start:end])
        if expected_sha and actual_sha != expected_sha:
            failures.append({"reason": "resource hash mismatch", "actual": actual_sha, "row": _jsonable(row)})
        ranges.append((start, end, row.get("name"), size))
    ranges.sort()
    for left, right in zip(ranges, ranges[1:]):
        if left[1] > right[0]:
            failures.append({"reason": "resource overlap", "left": left, "right": right})
    b3 = int(header.block_sizes[3])
    b3_sum = sum(size for _, _, _, size in ranges)
    fifo_exact = bool(ranges) and b3_sum == b3 and previous_logical_end == b3
    if fifo_exact:
        fifo_exact = ranges[-1][1] == len(expected.serialized_zone)
    if fifo_exact:
        fifo_exact = all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    getaway_exact = True
    if map_name.casefold() == "mp_getaway":
        getaway_exact = len(fifo_rows) == 25 and b3_sum == EXPECTED_GETAWAY_B3
    audit.require(
        "STREAM.DELAYED_FIFO",
        "stream",
        not failures and fifo_exact and getaway_exact,
        "all and only deferred images form the hash-exact 0x80-aligned B3 FIFO through zone-memory end",
        {
            "resource_entries": len(entries),
            "deferred_entries": len(fifo_rows),
            "embedded_ranges": len(ranges),
            "embedded_bytes": b3_sum,
            "block3_bytes": b3,
            "logical_fifo_end": previous_logical_end,
            "getaway_exact_25_resources": getaway_exact,
            "first_range": ranges[0] if ranges else None,
            "last_range": ranges[-1] if ranges else None,
            "serialized_zone_end": len(expected.serialized_zone),
            "failures": failures[:32],
        },
    )


def _scan_ps3_xmodels(zone: bytes, start: int = PS3_ZONE_HEADER_SIZE) -> list[dict[str, Any]]:
    """Structure-only PS3 XModel scan used as a retail oracle.

    It does not use the production writer or graph-results table.  Surface roots
    are exposed only when the retail model owns them inline.
    """

    rows = []
    marker = b"\xFF\xFF\xFF\xFF"
    cursor = max(0, start)
    limit = len(zone) - PS3_XMODEL_ROOT_SIZE - 2
    while cursor <= limit:
        root = zone.find(marker, cursor, limit + 1)
        if root < 0:
            break
        cursor = root + 1
        try:
            bones = zone[root + 4]
            roots = zone[root + 5]
            surfaces = zone[root + 6]
            if bones == 0 or roots == 0 or roots > bones or surfaces == 0:
                continue
            if _u32be(zone, root + 0x20) not in (FOLLOWING, INSERT):
                continue
            if _u32be(zone, root + 0x24) not in (FOLLOWING, INSERT):
                continue
            lods = _u16be(zone, root + 0xB4)
            collision_lod = _i16be(zone, root + 0xB6)
            if not 1 <= lods <= 4 or not -1 <= collision_lod <= 4:
                continue
            radius = _f32be(zone, root + 0x98)
            if not math.isfinite(radius) or not 0 <= radius < 1e7:
                continue
            valid = True
            for i in range(3):
                minimum = _f32be(zone, root + 0x9C + i * 4)
                maximum = _f32be(zone, root + 0xA8 + i * 4)
                if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
                    valid = False
                    break
            if not valid:
                continue
            for i in range(4):
                lod = root + 0x28 + i * 0x18
                distance = _f32be(zone, lod)
                count = _u16be(zone, lod + 4)
                first = _u16be(zone, lod + 6)
                if not math.isfinite(distance) or count > surfaces or first + count > surfaces:
                    valid = False
                    break
            if not valid:
                continue
            name, after_name = _read_z(zone, root + PS3_XMODEL_ROOT_SIZE, 256)
            if not name or len(name) > 128 or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in name):
                continue
            nonroot = bones - roots
            skeleton = (
                (0x08, bones * 2),
                (0x0C, nonroot),
                (0x10, nonroot * 8),
                (0x14, nonroot * 16),
                (0x18, bones),
                (0x1C, bones * 0x20),
            )
            surface_root = after_name
            for pointer_offset, size in skeleton:
                if _u32be(zone, root + pointer_offset) in (FOLLOWING, INSERT):
                    surface_root += size
            if surface_root + surfaces * PS3_XSURFACE_ROOT_SIZE > len(zone):
                continue
            rows.append(
                {
                    "root": root,
                    "name": name,
                    "bones": bones,
                    "surfaces": surfaces,
                    "surface_root": surface_root,
                    "mem_usage": _u32be(zone, root + 0xBC),
                }
            )
        except (ValueError, struct.error, UnicodeDecodeError):
            continue
    # A true root is unique by physical address; retain deterministic order.
    return sorted({x["root"]: x for x in rows}.values(), key=lambda x: x["root"])


def _retail_oracles(
    audit: Audit,
    shipment_main: StrictPs3Document,
    shipment_load: StrictPs3Document,
    assembly: Any,
) -> None:
    _assert_retail_frames(audit, "ORACLE.SHIPMENT.MAIN.ADLER", "Shipment main", shipment_main)
    _assert_retail_frames(audit, "ORACLE.SHIPMENT.LOAD.ADLER", "Shipment load", shipment_load)
    try:
        shipment_load_doc = read_load_document(Path(shipment_load.path))
        audit.require(
            "ORACLE.SHIPMENT.LOAD.STRUCTURE",
            "retail_oracle",
            shipment_load_doc.rawfile.name == "mp_shipment_load"
            and len(shipment_load_doc.materials) == 3
            and len(shipment_load_doc.technique_slots) == 26,
            "retail Shipment _load.ff confirms the typed load-zone family/order contract",
            {
                "rawfile": shipment_load_doc.rawfile.name,
                "materials": len(shipment_load_doc.materials),
                "technique_slots": len(shipment_load_doc.technique_slots),
                "block_sizes": shipment_load_doc.block_sizes,
            },
        )
    except Exception as exc:
        audit.require(
            "ORACLE.SHIPMENT.LOAD.STRUCTURE", "retail_oracle", False,
            "retail Shipment _load.ff typed graph parses", {"error": str(exc)},
        )
    try:
        header = parse_zone_header(shipment_main.zone, "ps3")
        assets = parse_xasset_list(shipment_main.zone, "ps3")
        audit.require(
            "ORACLE.SHIPMENT.HEADER",
            "retail_oracle",
            header.block_sizes[1] == EXPECTED_SHIPMENT_B1 and len(assets.assets) > 0,
            "retail Shipment parses and independently anchors PS3 B1 at 165,232 bytes",
            {"block_sizes": header.block_sizes, "assets": len(assets.assets)},
        )
    except Exception as exc:
        audit.require("ORACLE.SHIPMENT.HEADER", "retail_oracle", False, "retail Shipment zone parses", {"error": str(exc)})

    retail_models = _scan_ps3_xmodels(shipment_main.zone)
    retail_by_name = {x["name"].casefold(): x for x in retail_models}
    source_models = {x.model.name.casefold(): x.model for x in assembly.source.xmodels}
    mem_rows = []
    mem_bad = []
    part_rows = []
    part_bad = []
    missing_proven = []
    for oracle_name, oracle_pc_mem, oracle_surfaces, oracle_retail_mem in RETAIL_XMODEL_MEM_ORACLE:
        name = oracle_name.casefold()
        retail = retail_by_name.get(name)
        pc = source_models.get(name)
        if retail is None or pc is None:
            missing_proven.append(
                {
                    "name": oracle_name,
                    "pc_present": pc is not None,
                    "retail_present": retail is not None,
                }
            )
            continue
        identity_ok = (
            int(pc.memory_usage) == oracle_pc_mem
            and int(pc.surface_count) == oracle_surfaces
            and int(retail["surfaces"]) == oracle_surfaces
        )
        expected_mem = ps3_memusage(oracle_pc_mem, oracle_surfaces)
        mem = {
            "name": retail["name"],
            "pc_mem_usage": pc.memory_usage,
            "surfaces": pc.surface_count,
            "retail_mem_usage": retail["mem_usage"],
            "expected_mem_usage": expected_mem,
            "frozen_retail_mem_usage": oracle_retail_mem,
            "same_revision_identity": identity_ok,
            "passed": identity_ok and retail["mem_usage"] == expected_mem == oracle_retail_mem,
        }
        mem_rows.append(mem)
        if not mem["passed"]:
            mem_bad.append(mem)
        # The same frozen revision set is the only allowed layout sample.  This
        # prevents an equal-surface-count but different-geometry name match from
        # becoming a false memUsage/partBits oracle.
        if identity_ok:
            for i, surface in enumerate(pc.surfaces):
                ok, proof = check_xsurface_contract(
                    shipment_main.zone,
                    int(retail["surface_root"]) + i * PS3_XSURFACE_ROOT_SIZE,
                    surface.part_bits,
                )
                proof.update({"model": retail["name"], "surface": i})
                part_rows.append(proof)
                if not ok:
                    part_bad.append(proof)
    audit.require(
        "ORACLE.XMODEL.MEMUSAGE",
        "retail_oracle",
        len(mem_rows) == len(RETAIL_XMODEL_MEM_ORACLE) and not missing_proven and not mem_bad,
        "the frozen 12-model same-geometry Shipment sample confirms the PS3 memUsage formula",
        {
            "retail_models_scanned": len(retail_models),
            "proven_same_revision_models": len(mem_rows),
            "missing_proven_models": missing_proven,
            "formula_matches": sum(x["passed"] for x in mem_rows),
            "failures": mem_bad,
            "excluded_equal_count_name_matches": ["com_laptop_2_open"],
            "excluded_different_ownership_outlier": "com_junktire",
        },
    )
    # Do not require source partBits equality for every same-name asset revision;
    # the hard oracle is the reserved slot and shifted four-word location.
    reserved_bad = [x for x in part_rows if x.get("reserved_0x38") != 0]
    audit.require(
        "ORACLE.XSURFACE.LAYOUT",
        "retail_oracle",
        bool(part_rows) and not reserved_bad,
        "same-revision retail XSurfaces independently confirm reserved +0x38 and partBits beginning at +0x3C",
        {
            "surfaces_observed": len(part_rows),
            "reserved_slot_failures": reserved_bad[:32],
            "full_partbits_differences": part_bad[:16],
        },
    )


def _audit_load(audit: Audit, load_path: Path, candidate_load: StrictPs3Document) -> None:
    _assert_retail_frames(audit, "C10.LOAD.ADLER", "Version-10 load", candidate_load)
    try:
        doc = read_load_document(load_path)
        recomputed_blocks = compute_load_block_sizes(doc)
        typed_ok = (
            doc.rawfile.name == "mp_getaway_load"
            and len(doc.materials) == 3
            and len(doc.technique_slots) == 26
            and sum(len(material.textures) for material in doc.materials) == 2
            and len(doc.block_sizes) == 7
        )
        audit.require(
            "LOAD.STRUCTURE",
            "load_fastfile",
            typed_ok,
            "_load.ff typed TechniqueSet/Material/Image/RawFile graph parses exactly",
            {
                "rawfile": doc.rawfile.name,
                "rawfile_bytes": len(doc.rawfile.data),
                "materials": len(doc.materials),
                "images": sum(len(material.textures) for material in doc.materials),
                "technique_slots": len(doc.technique_slots),
                "block_sizes": doc.block_sizes,
                "recomputed_block_sizes": recomputed_blocks,
                "expected_getaway_block_sizes": EXPECTED_GETAWAY_LOAD_BLOCKS,
                "decompressed_capacity": doc.preferred_capacity,
            },
        )
        audit.require(
            "LOAD.BLOCKS.EXACT",
            "load_fastfile",
            tuple(doc.block_sizes) == tuple(recomputed_blocks)
            and tuple(recomputed_blocks) == EXPECTED_GETAWAY_LOAD_BLOCKS,
            "_load.ff header equals the independent typed allocator replay for Getaway",
            {
                "header_block_sizes": doc.block_sizes,
                "typed_replay": recomputed_blocks,
                "expected": EXPECTED_GETAWAY_LOAD_BLOCKS,
            },
        )
    except Exception as exc:
        audit.require("LOAD.STRUCTURE", "load_fastfile", False, "_load.ff typed graph parses", {"error": str(exc)})


def _render_markdown(report: Mapping[str, Any]) -> str:
    checks = report["checks"]
    failed = [x for x in checks if x["hard"] and not x["passed"]]
    lines = [
        "# Version 10 – independent zone audit",
        "",
        f"**Overall result:** {'PASS' if report['passed'] else 'FAIL'}",
        "",
        f"- Required checks: {sum(x['hard'] for x in checks)}",
        f"- Passed: {sum(x['passed'] for x in checks)}",
        f"- Failed: {len(failed)}",
        f"- Asset roots in inventory: {len(report.get('asset_inventory', []))}",
        "",
        "## Check matrix",
        "",
        "| ID | Category | Result | Description |",
        "|---|---|---:|---|",
    ]
    for row in checks:
        result = "PASS" if row["passed"] else ("FAIL" if row["hard"] else "WARN")
        summary = str(row["summary"]).replace("|", "\\|")
        lines.append(f"| `{row['check_id']}` | {row['category']} | {result} | {summary} |")
    lines.extend(["", "## C10-Fix-Gates", ""])
    for check_id in (
        "C10.XSURFACE.PARTBITS",
        "C10.XMODEL.MEMUSAGE",
        "C10.COMWORLD.XSTRINGS",
        "C10.LIGHTDEF.IMAGE_SHELL",
        "C10.RAWFILE501.IDENTITY",
        "C10.MAIN.ADLER",
        "C10.LOAD.ADLER",
    ):
        row = next((x for x in checks if x["check_id"] == check_id), None)
        if row:
            lines.append(f"- **{'PASS' if row['passed'] else 'FAIL'}** `{check_id}` – {row['summary']}")
    if failed:
        lines.extend(["", "## Required checks failed", ""])
        for row in failed:
            lines.append(f"### `{row['check_id']}`")
            lines.append("")
            lines.append(row["summary"])
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(row.get("details", {}), ensure_ascii=False, indent=2, sort_keys=True))
            lines.append("```")
            lines.append("")
    else:
        lines.extend(
            [
                "",
                "## Verification statement",
                "",
                "All known C10 structure fixes, all source-derived asset families, reverse NULL closure, "
                "block bounds, relocations, root alignments, image FIFO and both FastFile containers passed.",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent IW3 PS3 Version-10 binary audit")
    parser.add_argument("--main", required=True, type=Path, help="Candidate mp_getaway.ff")
    parser.add_argument("--load", required=True, type=Path, help="Candidate mp_getaway_load.ff")
    parser.add_argument("--pc-ff", required=True, type=Path, help="Original PC mp_getaway.ff")
    parser.add_argument("--iwd", action="append", required=True, type=Path, help="Original PC IWD; repeatable")
    parser.add_argument("--shipment-main", required=True, type=Path, help="Retail PS3 mp_shipment.ff")
    parser.add_argument("--shipment-load", required=True, type=Path, help="Retail PS3 mp_shipment_load.ff")
    parser.add_argument("--map", default="mp_getaway")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--gfxworld-resource-policy",
        choices=[x.value for x in GfxWorldResourcePolicy],
        default=GfxWorldResourcePolicy.FASTFILE_DELAYED.value,
    )
    parser.add_argument(
        "--material-image-resource-policy",
        choices=[x.value for x in ImageResourcePolicy],
        default=ImageResourcePolicy.FASTFILE_DELAYED.value,
    )
    parser.add_argument("--out-json", type=Path, default=Path("version10_audit.json"))
    parser.add_argument("--out-md", type=Path, default=Path("version10_audit.md"))
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    audit = Audit()
    started = {
        "main": str(args.main.resolve()),
        "load": str(args.load.resolve()),
        "pc_ff": str(args.pc_ff.resolve()),
        "iwd": [str(x.resolve()) for x in args.iwd],
        "shipment_main": str(args.shipment_main.resolve()),
        "shipment_load": str(args.shipment_load.resolve()),
        "map": args.map,
        "gfxworld_resource_policy": args.gfxworld_resource_policy,
        "material_image_resource_policy": args.material_image_resource_policy,
    }
    documents: dict[str, Any] = {}
    fatal: str | None = None
    try:
        for path in (args.main, args.load, args.pc_ff, args.shipment_main, args.shipment_load, *args.iwd):
            if not path.is_file():
                raise FileNotFoundError(path)
        _validate_pc_and_iwd(audit, args.pc_ff, args.iwd)

        main_doc = read_ps3_fastfile_strict(args.main)
        load_doc = read_ps3_fastfile_strict(args.load)
        shipment_main = read_ps3_fastfile_strict(args.shipment_main)
        shipment_load = read_ps3_fastfile_strict(args.shipment_load)
        documents = {
            "candidate_main": _frame_summary(main_doc),
            "candidate_load": _frame_summary(load_doc),
            "shipment_main": _frame_summary(shipment_main),
            "shipment_load": _frame_summary(shipment_load),
        }
        _assert_retail_frames(audit, "C10.MAIN.ADLER", "Version-10 main", main_doc)
        _audit_load(audit, args.load, load_doc)

        assembly = analyze_and_assemble(
            args.pc_ff,
            args.map,
            args.iwd,
            ffmpeg=args.ffmpeg,
            runtime_compatible=True,
            boot_isolation=True,
            gfxworld_resource_policy=args.gfxworld_resource_policy,
            material_image_resource_policy=args.material_image_resource_policy,
        )
        audit.require(
            "SOURCE.ASSEMBLY",
            "source",
            bool(assembly.plan.validate(assembly.source.asset_list).get("passed")),
            "PC/IWD source graph is completely classified before destination inspection",
            {
                "source_diagnostics": assembly.source.diagnostics,
                "assembly_diagnostics": assembly.diagnostics,
            },
        )
        # This is a fresh deterministic double serialization from the original
        # source, not a manifest emitted by the candidate build.
        expected = assembly.plan.build(assembly.source.asset_list)
        report = _audit_expected_readback(audit, args.main, main_doc, assembly, expected)
        header, xasset_list = _audit_container_and_header(audit, main_doc, assembly, expected)
        _build_inventory(audit, assembly, expected, main_doc.zone, report, xasset_list)
        _audit_xassetlist_raw_contract(audit, main_doc.zone, xasset_list, assembly, expected)
        _audit_loader_safe_topology(audit, assembly, expected)
        _audit_relocations(audit, main_doc.zone, header.block_sizes, expected)
        _audit_default_normal_matrix(audit, main_doc.zone, assembly, expected)
        _audit_runtime_block1(audit, header, assembly, expected)

        # Independent raw-byte gates for every C10 fix and every known NULL-loss family.
        _audit_xmodels(audit, main_doc.zone, assembly, expected)
        _audit_comworld(audit, main_doc.zone, header.block_sizes, assembly, expected)
        _audit_lightdef(audit, main_doc.zone, assembly, expected)
        _audit_loaded_sound_temp_contract(audit, main_doc.zone, assembly, expected)
        _audit_fx_xmodel_alias_shells(audit, main_doc.zone, assembly, expected)
        _audit_clipmap_insert_alias_contract(audit, main_doc.zone, header.block_sizes, assembly, expected)
        _audit_stringtable_nested_alignment(audit, main_doc.zone, assembly, expected)
        _audit_rawfile501(audit, main_doc.zone, assembly, expected)
        _reverse_nonnull_closure(audit, main_doc.zone, assembly, expected)
        _audit_delayed_fifo(audit, main_doc.zone, header, expected, map_name=args.map)
        _retail_oracles(audit, shipment_main, shipment_load, assembly)

        # Cross-reader agreement protects the strict framing implementation.
        normal = read_ps3_fastfile(args.main)
        audit.require(
            "CONTAINER.DUAL_READER",
            "container",
            normal.zone == main_doc.zone and normal.zone_sha256 == main_doc.zone_sha256,
            "regular project reader and strict Adler-aware reader produce identical zone bytes",
            {
                "project_zone_sha256": normal.zone_sha256,
                "strict_zone_sha256": main_doc.zone_sha256,
            },
        )
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
        audit.require(
            "AUDIT.FATAL",
            "audit",
            False,
            "audit completed without an unhandled exception",
            {"error": fatal, "traceback": traceback.format_exc()},
        )

    checks = [_jsonable(dataclasses.asdict(x)) for x in audit.checks]
    result = {
        "schema": TOOL_SCHEMA,
        "passed": audit.passed,
        "fatal_error": fatal,
        "inputs": started,
        "documents": documents,
        "summary": {
            "checks": len(checks),
            "hard_checks": sum(x["hard"] for x in checks),
            "passed_checks": sum(x["passed"] for x in checks),
            "failed_hard_checks": len(audit.failures),
            "asset_inventory_rows": len(audit.inventory),
        },
        "checks": checks,
        "failures": audit.failures,
        "asset_inventory": _jsonable(audit.inventory),
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.out_md.write_text(_render_markdown(result), encoding="utf-8")
    print(json.dumps(result["summary"] | {"passed": result["passed"], "json": str(args.out_json), "markdown": str(args.out_md)}, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(run())

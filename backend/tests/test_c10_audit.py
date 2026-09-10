from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cod4porter.graph import AssetType, Dependency  # noqa: E402
from cod4porter.zone_writer import PackedOffset  # noqa: E402

SPEC = importlib.util.spec_from_file_location("c10_audit", ROOT / "tools" / "c10_audit.py")
assert SPEC is not None and SPEC.loader is not None
c10 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = c10
SPEC.loader.exec_module(c10)


def _raw_deflate(data: bytes) -> bytes:
    obj = zlib.compressobj(level=9, wbits=-15)
    return obj.compress(data) + obj.flush()


def _fastfile(data: bytes, *, adler: bool) -> bytes:
    comp = _raw_deflate(data)
    suffix = struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF) if adler else b""
    payload = comp + suffix
    return c10.MAGIC + struct.pack(">I", c10.PS3_VERSION) + struct.pack(">H", len(payload)) + payload + b"\x00\x01"


def test_strict_frame_accepts_retail_adler_and_detects_missing_suffix() -> None:
    raw = bytes(range(251)) * 17
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.ff"
        bad = Path(td) / "bad.ff"
        good.write_bytes(_fastfile(raw, adler=True))
        bad.write_bytes(_fastfile(raw, adler=False))
        good_doc = c10.read_ps3_fastfile_strict(good)
        bad_doc = c10.read_ps3_fastfile_strict(bad)
    assert good_doc.zone == raw
    assert len(good_doc.frames) == 1
    assert good_doc.frames[0].adler_present
    assert good_doc.frames[0].adler_valid
    assert bad_doc.zone == raw
    assert not bad_doc.frames[0].adler_present
    assert not bad_doc.frames[0].adler_valid


def test_xsurface_reserved_slot_and_shifted_partbits_contract() -> None:
    expected = (0x80000000, 0x01020304, 0, 0xFFFFFFFF)
    root = 7
    zone = bytearray(root + c10.PS3_XSURFACE_ROOT_SIZE)
    for index, word in enumerate(expected):
        struct.pack_into(">I", zone, root + 0x3C + index * 4, word)
    passed, proof = c10.check_xsurface_contract(bytes(zone), root, expected)
    assert passed, proof

    # This is the Version-09 bug: partBits started in the reserved word.
    broken = bytearray(root + c10.PS3_XSURFACE_ROOT_SIZE)
    for index, word in enumerate(expected):
        struct.pack_into(">I", broken, root + 0x38 + index * 4, word)
    passed, proof = c10.check_xsurface_contract(bytes(broken), root, expected)
    assert not passed
    assert proof["reserved_0x38"] == expected[0]


def test_ps3_memusage_formula_is_data_independent() -> None:
    assert c10.ps3_memusage(0x1000, 1) == 0x1004
    assert c10.ps3_memusage(0x1000, 10) == 0x10B8
    try:
        c10.ps3_memusage(0, -1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative surface count was accepted")


def test_structure_only_ps3_xmodel_scanner_finds_shifted_surface() -> None:
    root = 0x24
    name = b"unit_test_model\0"
    bones = 1
    surface_count = 1
    surface_root = root + c10.PS3_XMODEL_ROOT_SIZE + len(name) + bones * 2 + bones + bones * 0x20
    zone = bytearray(surface_root + c10.PS3_XSURFACE_ROOT_SIZE + 32)
    struct.pack_into(">I", zone, root, c10.FOLLOWING)
    zone[root + 4 : root + 8] = bytes((bones, bones, surface_count, 0))
    for off in (0x08, 0x18, 0x1C, 0x20, 0x24):
        struct.pack_into(">I", zone, root + off, c10.FOLLOWING)
    for off in (0x0C, 0x10, 0x14):
        struct.pack_into(">I", zone, root + off, c10.NULL)
    for lod in range(4):
        pos = root + 0x28 + lod * 0x18
        struct.pack_into(">fHH", zone, pos, float(lod), 1 if lod == 0 else 0, 0)
    struct.pack_into(">f", zone, root + 0x98, 1.0)
    for axis in range(3):
        struct.pack_into(">f", zone, root + 0x9C + axis * 4, -1.0)
        struct.pack_into(">f", zone, root + 0xA8 + axis * 4, 1.0)
    struct.pack_into(">Hh", zone, root + 0xB4, 1, -1)
    struct.pack_into(">I", zone, root + 0xBC, c10.ps3_memusage(100, 1))
    zone[root + c10.PS3_XMODEL_ROOT_SIZE : root + c10.PS3_XMODEL_ROOT_SIZE + len(name)] = name
    part_bits = (1, 2, 3, 4)
    for index, word in enumerate(part_bits):
        struct.pack_into(">I", zone, surface_root + 0x3C + index * 4, word)

    rows = c10._scan_ps3_xmodels(bytes(zone), root)
    assert len(rows) == 1, rows
    assert rows[0]["name"] == "unit_test_model"
    assert rows[0]["surface_root"] == surface_root
    passed, proof = c10.check_xsurface_contract(bytes(zone), surface_root, part_bits)
    assert passed, proof


def _check(audit: c10.Audit, check_id: str) -> c10.Check:
    return next(row for row in audit.checks if row.check_id == check_id)


def test_relocation_gate_rejects_packed_temp_target_even_when_in_bounds() -> None:
    zone = bytearray(0x40)
    raw = PackedOffset(0, 4).encode()
    struct.pack_into(">I", zone, 0x24, raw)
    expected = SimpleNamespace(
        relocation_rows=(
            {
                "physical_field_offset": 0x24,
                "target_symbol": "temporary-root",
                "target_block": 0,
                "target_offset": 4,
                "serialized_pointer": raw,
                "description": "forbidden TEMP pointer",
            },
        )
    )
    audit = c10.Audit()
    c10._audit_relocations(audit, bytes(zone), (16, 0, 0, 0, 16, 0, 0), expected)
    assert _check(audit, "POINTER.RELOCATIONS").passed
    assert not _check(audit, "POINTER.NO_TEMP_TARGETS").passed


def test_loader_topology_proves_stable_dependency_order_and_rejects_forward_header() -> None:
    TechniqueNode = type("ExternalTechniqueSetNode", (), {})
    ModelNode = type("XModelNode", (), {})
    dependency = TechniqueNode()
    dependency.symbol = "tech"
    dependency.type = AssetType.TECHSET
    dependency.dependencies = ()
    consumer = ModelNode()
    consumer.symbol = "model"
    consumer.type = AssetType.XMODEL
    consumer.dependencies = (Dependency("tech", AssetType.TECHSET, "model technique"),)

    class Plan:
        script_strings = ()

        def __init__(self, top):
            self._top = tuple(top)

        def top_level_assets(self):
            return self._top

        def _source_top_level_assets(self):
            return (consumer, dependency)

        def serialized_assets(self):
            return (consumer, dependency)

        def active_ownership_edges(self):
            return ()

    good = SimpleNamespace(
        graph_results={
            "techset.external:tech": {"root_physical": 0x40},
            "xmodel:model": {"root_physical": 0x80},
        },
        relocation_rows=(
            {
                "physical_field_offset": 0x84,
                "target_symbol": "tech" + c10.XASSET_HEADER_SUFFIX,
                "target_block": 4,
                "target_offset": 4,
                "description": "model -> earlier tech",
            },
        ),
    )
    dependency_failures, header_failures = c10._topology_rows(SimpleNamespace(plan=Plan((dependency, consumer))), good)
    assert not dependency_failures
    assert not header_failures

    bad = SimpleNamespace(
        graph_results={
            "xmodel:model": {"root_physical": 0x40},
            "techset.external:tech": {"root_physical": 0x80},
        },
        relocation_rows=(
            {
                "physical_field_offset": 0x44,
                "target_symbol": "tech" + c10.XASSET_HEADER_SUFFIX,
                "target_block": 4,
                "target_offset": 12,
                "description": "model -> later tech",
            },
        ),
    )
    dependency_failures, header_failures = c10._topology_rows(SimpleNamespace(plan=Plan((consumer, dependency))), bad)
    assert dependency_failures
    assert header_failures


def test_loaded_sound_gate_requires_payload_at_root_plus_0x1c() -> None:
    node = object.__new__(c10.SoundNode)
    node.symbol = "sound:test"
    payload = b"ID3-test-payload"
    sound_file = SimpleNamespace(payload=payload)
    node.plan = SimpleNamespace(
        source=SimpleNamespace(name="test_alias_list"),
        aliases=(SimpleNamespace(sound_file=sound_file),),
    )
    root_physical = 0x30
    payload_physical = 0x70
    zone = bytearray(payload_physical + len(payload))
    struct.pack_into(">IIIII", zone, root_physical, c10.FOLLOWING, len(payload), 1, 2, 1)
    struct.pack_into(">II", zone, root_physical + 0x14, 44_100, c10.FOLLOWING)
    zone[payload_physical : payload_physical + len(payload)] = payload
    row = {
        "root_location": {"block": 0, "offset": 0x0C},
        "payload_location": {"block": 0, "offset": 0x28},
        "name_location": {"block": 4, "offset": 0x100},
        "symbol_location": {"block": 4, "offset": 0x80},
        "root_physical": root_physical,
        "payload_physical": payload_physical,
        "payload_bytes": len(payload),
        "identity": "loaded-test",
    }
    plan = SimpleNamespace(serialized_assets=lambda: (node,))
    expected = SimpleNamespace(graph_results={"sound:sound:test": {"loaded_sound_rows": (row,)}})
    audit = c10.Audit()
    c10._audit_loaded_sound_temp_contract(audit, bytes(zone), SimpleNamespace(plan=plan), expected)
    assert _check(audit, "SOUND.LOADEDSOUND.TEMP_STACK").passed

    broken = dict(row)
    broken["payload_location"] = {"block": 0, "offset": 0x2C}
    audit = c10.Audit()
    c10._audit_loaded_sound_temp_contract(
        audit,
        bytes(zone),
        SimpleNamespace(plan=plan),
        SimpleNamespace(graph_results={"sound:sound:test": {"loaded_sound_rows": (broken,)}}),
    )
    assert not _check(audit, "SOUND.LOADEDSOUND.TEMP_STACK").passed


def test_fx_xmodel_alias_gate_distinguishes_ps3_0xcc_from_pc_0xdc_shell() -> None:
    node = object.__new__(c10.OwnedFxNode)
    node.symbol = "fx:test"
    visual = SimpleNamespace(kind=SimpleNamespace(value="xmodel_alias"), name="vehicle/test")
    node.source = SimpleNamespace(elements=(SimpleNamespace(visuals=(visual,)),))
    plan = SimpleNamespace(serialized_assets=lambda: (node,))
    expected = SimpleNamespace(graph_results={"fx.owned:fx:test": {"xmodel_aliases": 1}})
    name = b",vehicle/test\0"
    ps3_zone = b"\0" * c10.PS3_ZONE_HEADER_SIZE + struct.pack(">I", c10.FOLLOWING) + b"\0" * (0xCC - 4) + name
    audit = c10.Audit()
    c10._audit_fx_xmodel_alias_shells(audit, ps3_zone, SimpleNamespace(plan=plan), expected)
    assert _check(audit, "FX.XMODEL_ALIAS.PS3_SHELL").passed

    pc_zone = b"\0" * c10.PS3_ZONE_HEADER_SIZE + struct.pack(">I", c10.FOLLOWING) + b"\0" * (0xDC - 4) + name
    audit = c10.Audit()
    c10._audit_fx_xmodel_alias_shells(audit, pc_zone, SimpleNamespace(plan=plan), expected)
    assert not _check(audit, "FX.XMODEL_ALIAS.PS3_SHELL").passed


if __name__ == "__main__":
    test_strict_frame_accepts_retail_adler_and_detects_missing_suffix()
    test_xsurface_reserved_slot_and_shifted_partbits_contract()
    test_ps3_memusage_formula_is_data_independent()
    test_structure_only_ps3_xmodel_scanner_finds_shifted_surface()
    test_relocation_gate_rejects_packed_temp_target_even_when_in_bounds()
    test_loader_topology_proves_stable_dependency_order_and_rejects_forward_header()
    test_loaded_sound_gate_requires_payload_at_root_plus_0x1c()
    test_fx_xmodel_alias_gate_distinguishes_ps3_0xcc_from_pc_0xdc_shell()
    print("test_c10_audit PASS")

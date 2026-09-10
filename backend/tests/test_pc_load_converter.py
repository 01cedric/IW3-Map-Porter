from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.backend.v4_ffio import (
    FOLLOWING,
    INSERT,
    MAGIC,
    NULL,
    encode_pc_packed,
    parse_zone_header,
    read_ps3_fastfile,
    require_retail_ps3_adler32,
)
from cod4porter.backend.v4_load import parse_load_zone
from cod4porter.pc_load_converter import convert_pc_load_to_ps3, parse_pc_load_source


PC_TYPES = (0x05, 0x05, 0x04, 0x04, 0x04, 0x1F)


def _z(value: str) -> bytes:
    return value.encode("latin-1") + b"\0"


def _technique(name: str) -> bytes:
    root = bytearray(0x94)
    struct.pack_into("<I", root, 0, FOLLOWING)
    return bytes(root) + _z(name)


def _material(name: str, image_name: str | None, state_pair: tuple[int, int] | None) -> bytes:
    root = bytearray(0x50)
    struct.pack_into("<I", root, 0, FOLLOWING)
    if image_name is None:
        struct.pack_into("<IIII", root, 0x40, NULL, NULL, NULL, NULL)
        return bytes(root) + _z(name)

    root[4:8] = bytes((0, 43 if name == "$victorybackdrop" else 4, 1, 1))
    root[0x18:0x18 + 34] = b"\xFF" * 34
    root[0x18 + 4] = 0
    root[0x3A] = 1
    root[0x3C] = 1
    root[0x3E] = 3
    struct.pack_into(
        "<IIII",
        root,
        0x40,
        encode_pc_packed(4, 12),
        FOLLOWING,
        NULL,
        FOLLOWING,
    )
    texture = struct.pack("<I4BI", 0xA0AB1041, 99, 112, 11, 0, FOLLOWING)
    image = bytearray(0x24)
    struct.pack_into("<i", image, 0, 3)
    struct.pack_into("<I", image, 4, INSERT)
    image[10] = 1
    image[0x1E] = 3
    struct.pack_into("<I", image, 0x20, FOLLOWING)
    loaddef = bytearray(0x10)
    loaddef[0] = 0 if name == "$victorybackdrop" else 1
    loaddef[1] = 0 if name == "$victorybackdrop" else 2
    struct.pack_into("<HHH", loaddef, 2, 8, 8, 1)
    struct.pack_into("<I", loaddef, 8, 0x31545844)
    struct.pack_into("<I", loaddef, 0x0C, 0)
    state = struct.pack("<II", *(state_pair or (0, 0)))
    return bytes(root) + _z(name) + texture + bytes(image) + _z(image_name) + bytes(loaddef) + state


def _pc_load_fastfile(map_name: str, shared_victory=False, briefing_name=None, owned_defeat=False) -> bytes:
    diagnostic = b"ERROR: synthetic linker diagnostic\n"
    payload = bytearray(struct.pack("<4I", 0, NULL, len(PC_TYPES), FOLLOWING))
    for type_id in PC_TYPES:
        payload += struct.pack("<II", type_id, FOLLOWING)
    payload += _technique(",sm2/2d")
    payload += _technique(",2d")
    payload += _material(",$victorybackdrop",None,None) if shared_victory else _material("$victorybackdrop", "mile_high_victory_screen", (1, 2))
    payload += _material('$defeatbackdrop','defeat',(1,2)) if owned_defeat else _material(",$defeatbackdrop", None, None)
    payload += _material("$levelbriefing", briefing_name or f"loadscreen_{map_name}", (3, 4))
    payload += struct.pack("<III", FOLLOWING, len(diagnostic), FOLLOWING)
    payload += _z(f"{map_name}_load") + diagnostic + b"\0"

    header = struct.pack(
        "<11I",
        len(payload),
        64,
        0xA4,
        0,
        0,
        0,
        len(payload),
        0,
        0,
        0,
        0,
    )
    zone = header + payload
    return MAGIC + struct.pack("<I", 5) + zlib.compress(zone, 9)


def _iwi(name: str) -> bytes:
    payload = bytes(range(32))
    size = 28 + len(payload)
    header = bytearray(b"IWi\x06" + bytes((0x0B, 0x02)))
    header += struct.pack("<HHH", 8, 8, 1)
    header += struct.pack("<4I", size, size, size, size)
    assert len(header) == 28
    return bytes(header) + payload


def _synthetic_test() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        pc = tmp / "source.ff"
        iwd = tmp / "source.iwd"
        output = tmp / "mp_fixture_load.ff"
        pc.write_bytes(_pc_load_fastfile("mp_fixture"))
        with zipfile.ZipFile(iwd, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("images/loadscreen_mp_fixture.iwi", _iwi("loadscreen_mp_fixture"))
            archive.writestr("sound/ignored.wav", b"not parsed by the load converter")
        source = parse_pc_load_source(pc)
        assert source.map_name == "mp_fixture"
        assert [tech.name for tech in source.technique_sets] == [",sm2/2d", ",2d"]
        report = convert_pc_load_to_ps3(pc, iwd, output)
        assert output.exists()
        assert report["source_technique_count"] == 2
        assert report["target_technique_count"] == 1
        assert report["target_material_count"] == 3
        assert report["levelbriefing_image_name"] == "loadscreen_mp_fixture"
        assert report["levelbriefing_dimensions"] == [8, 8, 1]
        assert report["levelbriefing_mip_count"] == 1
        assert report["levelbriefing_unpadded_bytes"] == 32
        assert report["levelbriefing_padding_bytes"] == 96
        assert report["rawfile_name"] == "mp_fixture_load"
        assert report["rawfile_bytes"] == 0
        assert report["source_diagnostic_bytes_omitted"] == 35
        assert report["sound_assets_emitted"] == 0
        assert report["iwd_output_created"] is False
        assert report["template_map_specific_names_reused"] is False
        assert report["standalone_build"] is True
        assert report["load_full_fidelity"] is False
        assert report["compatibility_fallbacks"] == ["neutral_victory_pixels"]
        assert list(tmp.glob("*.iwd")) == [iwd]

        ff = read_ps3_fastfile(output)
        assert ff.version == 1
        assert require_retail_ps3_adler32(ff) == report["retail_adler32_frames"]
        profile = parse_load_zone(ff.zone)
        assert profile.asset_type_order == ["techset", "material", "material", "material", "rawfile"]
        assert profile.rawfile_name == "mp_fixture_load" and profile.rawfile_bytes == 0
        assert profile.materials[2].textures[0]["image"]["name"] == "loadscreen_mp_fixture"
        assert b"mp_getaway_load\0" not in ff.zone
        assert b"loadscreen_mp_getaway\0" not in ff.zone
        header = parse_zone_header(ff.zone, "ps3")
        assert list(header.block_sizes) == report["block_sizes"]
        pc.write_bytes(_pc_load_fastfile('mp_fixture',shared_victory=True,briefing_name='custom_loading_picture'))
        with zipfile.ZipFile(iwd,'w') as archive:archive.writestr('images/custom_loading_picture.iwi',_iwi('custom_loading_picture'))
        shared=convert_pc_load_to_ps3(pc,iwd,output,overwrite=True)
        assert shared['victory_material_shared'] and shared['victory_resource_bytes']==0
        assert shared['source_levelbriefing_image_name']=='custom_loading_picture'
        profile=parse_load_zone(read_ps3_fastfile(output).zone)
        assert profile.materials[0].name==',$victorybackdrop' and not profile.materials[0].textures
        assert profile.materials[2].textures[0]['image']['name']=='loadscreen_mp_fixture'
        return {
            "source_techniques": report["source_technique_sets"],
            "target_techniques": report["target_technique_sets"],
            "block_sizes": report["block_sizes"],
            "adler_frames": report["retail_adler32_frames"],
        }


def _real_nuked_test() -> dict:
    inputs = ROOT.parent.parent / "inputs" / "CoDMapPorter"
    pc = Path(os.environ.get("IW3_PORTER_PC_NUKED_LOAD_FF", inputs / "mp_nuked_load.ff"))
    iwd = Path(os.environ.get("IW3_PORTER_PC_NUKED_IWD", inputs / "mp_nuked.iwd"))
    shipment = Path(os.environ.get("IW3_PORTER_PS3_SHIPMENT_LOAD_FF", inputs / "mp_shipment_load.ff"))
    if not all(path.exists() for path in (pc, iwd, shipment)):
        return {"skipped": True, "reason": "optional mp_nuked/Shipment integration inputs unavailable"}

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "mp_nuked_load.ff"
        report = convert_pc_load_to_ps3(
            pc,
            iwd,
            output,
            retail_load_reference=shipment,
        )
        assert report["source_technique_sets"] == [",sm2/2d", ",2d"]
        assert report["target_technique_sets"] == [",2d"]
        assert report["target_materials"] == [
            "$victorybackdrop",
            ",$defeatbackdrop",
            "$levelbriefing",
        ]
        assert report["levelbriefing_image_name"] == "loadscreen_mp_nuked"
        assert report["levelbriefing_dimensions"] == [1024, 1024, 1]
        assert report["levelbriefing_mip_count"] == 1
        assert report["levelbriefing_resource_bytes"] == 524288
        assert report["levelbriefing_resource_sha256"] == (
            "1E2541E44B479A99BFE03BEFA21C0B3AF1AC743A59128AA961115348F3B20EDD"
        )
        assert report["rawfile_name"] == "mp_nuked_load"
        assert report["rawfile_bytes"] == 0
        assert report["source_diagnostic_bytes_omitted"] == 9897
        assert report["block_sizes"] == [196, 0, 0, 0, 191, 0, 1223424]
        assert report["zone_memory_size"] == 1224253
        assert report["zone_bytes"] == 1245184
        assert report["retail_adler32_frames"] == 19
        assert report["full_resolution_preserved"]
        assert report["full_mip_chain_preserved"]
        assert report["load_full_fidelity"]
        assert report["standalone_build"] is False
        assert report["external_schema_audit"]["structure_exact_vs_builtin"]
        assert report["external_schema_audit"]["map_specific_image_payload_used_for_build"] is False
        assert report["retail_image_payload_bytes_reused"] == 699136
        assert report["map_specific_retail_image_payload_bytes_reused"] == 0
        assert report["sound_assets_emitted"] == 0
        assert report["gsc_rawfiles_emitted"] == 0
        assert report["iwd_output_created"] is False

        zone = read_ps3_fastfile(output).zone
        assert b"mp_shipment_load\0" not in zone
        assert b"loadscreen_mp_shipment\0" not in zone
        assert b"mp_getaway" not in zone
        assert not list(Path(directory).glob("*.iwd"))
        return {
            "output_sha256": report["output_file_sha256"],
            "resource_sha256": report["levelbriefing_resource_sha256"],
            "block_sizes": report["block_sizes"],
            "zone_memory_size": report["zone_memory_size"],
            "adler_frames": report["retail_adler32_frames"],
        }


def main() -> None:
    result = {
        "passed": True,
        "synthetic": _synthetic_test(),
        "mp_nuked": _real_nuked_test(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

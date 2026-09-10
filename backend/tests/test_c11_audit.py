from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("c11_audit", ROOT / "tools" / "c11_audit.py")
assert SPEC is not None and SPEC.loader is not None
c11 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = c11
SPEC.loader.exec_module(c11)
from cod4porter.backend.v4_gate import CRITERIA, REQUIRED_SOURCES


def _base_report() -> dict:
    evidence_documents = [{"evidence": [
        {"criterion": criterion, "passed": True, "source": source, "proof": {"unit": True}}
        for criterion, _label in CRITERIA
        for source in sorted(REQUIRED_SOURCES[criterion])
    ]}]
    full_gate = c11.evaluate_full_fidelity(*evidence_documents)
    return {
        "map": "mp_unit",
        "hardware_tested": False,
        "execution_mode": "owner-aware-visual-candidate",
        "runtime_compatible": True,
        "boot_isolation": False,
        "gfxworld_resource_policy": "fastfile-delayed",
        "material_image_resource_policy": "fastfile-delayed",
        "sound_policy": "omit",
        "main": {"asset_counts": {}, "asset_count": 1, "script_strings": 0, "block_sizes": [0, 0, 0, 128, 0, 0, 0]},
        "gfxworld_resource_validation": {"passed": True, "complete": True},
        "material_image_resource_validation": {"passed": True, "complete": True},
        "global_deferred_resource_validation": {"passed": True, "fifo_count": 2, "block": 3, "declared_bytes": 128, "block_bytes": 128, "physical_tail_exact": True, "failures": []},
        "image_fidelity_validation": {"passed": True, "source_iwd_images": 2, "destination_iwd_images": 2, "mismatches": []},
        "platform_state_validation": {"passed": True, "retail_exact_passed": True, "tier_counts": {"exact_signature": 1, "hardware_observed_family": 0, "synthetic_assignment": 0}},
        "independent_readback": {"passed": True},
        "roundtrip": {"passed": True},
        "closure": {
            "passed": True,
            "blockers": [],
            "unresolved_output_refs": 0,
            "source_pointer_leaks": 0,
            "deterministic_write": True,
            "neutral_image_fallbacks": 0,
            "unresolved_platform_states": 0,
            "source_fx_visual_refs": 2,
            "resolved_exact_fx_visual_refs": 2,
            "runtime_fx_visual_fallbacks": 0,
        },
        "assembly_diagnostics": {"fx_runtime_material_visuals": 0},
        "evidence_documents": evidence_documents,
        "full_fidelity_gate": full_gate,
        "full_fidelity_passed": True,
        "load": {
            "passed": True,
            "readback_passed": True,
            "mode": "fresh-pc-load-conversion",
            "strict_readback": True,
            "load_full_fidelity": True,
            "load_materials": 3,
            "template_map_specific_names_reused": False,
            "map_specific_retail_image_payload_bytes_reused": 0,
            "sound_assets_emitted": 0,
            "iwd_output_created": False,
        },
    }


def test_version11_report_gate_distinguishes_structural_and_fidelity_debt() -> None:
    report = _base_report()
    checks = c11.evaluate_report(report)
    assert all(row.passed for row in checks)

    report["closure"]["passed"] = False
    report["closure"]["blockers"] = [
        "3 Material image slots use synthetic neutral resources",
        "7 Material PlatformState plans use non-exact compatibility families",
        "2 FX Material visuals use the shared runtime default",
        "1 PhysPreset sound prefixes use an empty compatibility string",
        "FX packed visuals: source=2 output/resolved=0",
    ]
    report["closure"]["neutral_image_fallbacks"] = 3
    report["closure"]["unresolved_platform_states"] = 7
    report["closure"]["resolved_exact_fx_visual_refs"] = 0
    report["closure"]["runtime_fx_visual_fallbacks"] = 2
    report["assembly_diagnostics"]["fx_runtime_material_visuals"] = 2
    report["platform_state_validation"]["retail_exact_passed"] = False
    report["platform_state_validation"]["tier_counts"]["synthetic_assignment"] = 1
    report["full_fidelity_passed"] = False
    checks = c11.evaluate_report(report)
    failed = {row.check_id for row in checks if not row.passed}
    assert failed == {
        "GRAPH.CLOSURE",
        "SEMANTIC.NEUTRAL_IMAGES",
        "SEMANTIC.PLATFORM_STATES",
        "SEMANTIC.FX_MATERIAL_FALLBACKS",
        "SEMANTIC.FX_VISUAL_REFERENCES",
        "SEMANTIC.FX_VISUAL_FALLBACKS",
        "PLATFORM_STATE.EXACT",
        "PLATFORM_STATE.NO_SYNTHETIC",
        "FIDELITY.FULL_GATE",
    }
    assert all(row.fidelity_only for row in checks if not row.passed)

    report["closure"]["blockers"].append("destination relocation table is inconsistent")
    checks = c11.evaluate_report(report)
    structural = next(row for row in checks if row.check_id == "GRAPH.STRUCTURAL_CLOSURE")
    assert not structural.passed
    assert not structural.fidelity_only


def test_version11_report_gate_fails_closed_on_missing_evidence() -> None:
    checks = c11.evaluate_report({})
    assert checks
    assert not all(row.passed for row in checks)
    assert any(row.observed is None for row in checks if not row.passed)


def test_fx_count_deficit_requires_complete_fallback_accounting() -> None:
    report = _base_report()
    closure = report['closure']
    closure.update(passed=False, source_fx_visual_refs=19, resolved_exact_fx_visual_refs=17,
        runtime_fx_visual_fallbacks=2, blockers=['2 FX Material visuals use the shared runtime default',
                                             'FX packed visuals: source=19 output/resolved=17'])
    assert _check_by_id(report,'GRAPH.STRUCTURAL_CLOSURE').passed
    assert not _check_by_id(report,'SEMANTIC.FX_VISUAL_REFERENCES').passed
    assert not _check_by_id(report,'SEMANTIC.FX_VISUAL_FALLBACKS').passed
    assert not _check_by_id(report,'GRAPH.CLOSURE').passed
    for bad in (None,True,'2',-1,0,1,3):
        closure['runtime_fx_visual_fallbacks']=bad
        assert not _check_by_id(report,'GRAPH.STRUCTURAL_CLOSURE').passed, bad
    closure['runtime_fx_visual_fallbacks']=2
    closure['blockers'].append('FX packed visuals: source=20 output/resolved=17')
    assert not _check_by_id(report,'GRAPH.STRUCTURAL_CLOSURE').passed


def _check_by_id(report: dict, check_id: str):
    return next(row for row in c11.evaluate_report(report) if row.check_id == check_id)


def test_version11_fifo_rejects_wrong_block_nonintegers_and_header_mismatch() -> None:
    report = _base_report()
    assert _check_by_id(report, "RESOURCE.GLOBAL_FIFO.NONEMPTY").passed
    assert _check_by_id(report, "RESOURCE.GLOBAL_FIFO.BLOCK").passed
    assert _check_by_id(report, "RESOURCE.GLOBAL_FIFO.BYTE_CLOSURE").passed
    assert _check_by_id(report, "RESOURCE.GLOBAL_FIFO.MAIN_BLOCK3").passed

    mutations = (
        ("fifo_count", True, "RESOURCE.GLOBAL_FIFO.NONEMPTY"),
        ("block", True, "RESOURCE.GLOBAL_FIFO.BLOCK"),
        ("declared_bytes", -1, "RESOURCE.GLOBAL_FIFO.BYTE_CLOSURE"),
        ("block_bytes", -1, "RESOURCE.GLOBAL_FIFO.BYTE_CLOSURE"),
    )
    for field, value, check_id in mutations:
        mutated = deepcopy(report)
        mutated["global_deferred_resource_validation"][field] = value
        assert not _check_by_id(mutated, check_id).passed, (field, value, check_id)

    wrong_block = deepcopy(report)
    wrong_block["global_deferred_resource_validation"]["block"] = 6
    assert not _check_by_id(wrong_block, "RESOURCE.GLOBAL_FIFO.BLOCK").passed

    wrong_header = deepcopy(report)
    wrong_header["main"]["block_sizes"][3] = 127
    assert not _check_by_id(wrong_header, "RESOURCE.GLOBAL_FIFO.MAIN_BLOCK3").passed


def test_version11_closure_schema_rejects_inconsistent_pass_flag() -> None:
    report = _base_report()
    report["closure"]["passed"] = False
    report["closure"]["blockers"] = []
    check = _check_by_id(report, "GRAPH.CLOSURE_SCHEMA")
    assert not check.passed
    assert not check.fidelity_only


def _fake_saved_load():
    level = {
        "name": "loadscreen_mp_unit",
        "format": 0x86,
        "mip_count": 1,
        "width": 1024,
        "height": 1024,
        "depth": 1,
        "resource_size": 524288,
        "resource_sha256": "LEVEL",
    }
    victory = {
        "name": "mile_high_victory_screen",
        "format": 0x86,
        "mip_count": 11,
        "width": 1024,
        "height": 1024,
        "depth": 1,
        "resource_size": 699136,
        "resource_sha256": "VICTORY",
    }
    materials = [
        SimpleNamespace(name="$victorybackdrop", textures=[{"image": victory}]),
        SimpleNamespace(name=",$defeatbackdrop", textures=[]),
        SimpleNamespace(name="$levelbriefing", textures=[{"image": level}]),
    ]
    total = level["resource_size"] + victory["resource_size"]
    profile = SimpleNamespace(
        technique_name=",2d",
        materials=materials,
        rawfile_name="mp_unit_load",
        rawfile_bytes=0,
        resource_bytes=total,
        declared_resource_bytes=total,
        zone_memory_size=321,
        trailing_nonzero=0,
    )
    typed = SimpleNamespace(block_sizes=(196, 0, 0, 0, 191, 0, total))
    document = SimpleNamespace(zone_sha256="ZONE", zone=b"zone")
    report = {
        "levelbriefing_image_name": "loadscreen_mp_unit",
        "levelbriefing_dimensions": [1024, 1024, 1],
        "levelbriefing_mip_count": 1,
        "levelbriefing_ps3_format": 0x86,
        "levelbriefing_resource_sha256": "LEVEL",
        "levelbriefing_resource_bytes": 524288,
        "victory_image_name": "mile_high_victory_screen",
        "victory_dimensions": [1024, 1024, 1],
        "victory_mip_count": 11,
        "victory_resource_sha256": "VICTORY",
        "victory_resource_bytes": 699136,
        "block_sizes": [196, 0, 0, 0, 191, 0, total],
        "output_zone_sha256": "ZONE",
        "zone_bytes": 4,
        "zone_memory_size": 321,
    }
    return report, document, typed, profile


def _saved_load_check(load_report: dict, document, typed, profile, check_id: str):
    return next(
        row
        for row in c11._evaluate_saved_load("mp_unit", load_report, document, typed, profile)
        if row.check_id == check_id
    )


def test_version11_saved_load_checks_exact_identity_topology_and_geometry() -> None:
    load_report, document, typed, profile = _fake_saved_load()
    assert all(row.passed for row in c11._evaluate_saved_load("mp_unit", load_report, document, typed, profile))

    wrong_material = deepcopy(profile)
    wrong_material.materials[1].name = "$wrong"
    assert not _saved_load_check(load_report, document, typed, wrong_material, "LOAD.ACTUAL_MATERIALS").passed

    extra_texture = deepcopy(profile)
    extra_texture.materials[1].textures.append(deepcopy(extra_texture.materials[0].textures[0]))
    extra_texture.resource_bytes += 699136
    extra_texture.declared_resource_bytes += 699136
    assert not _saved_load_check(load_report, document, typed, extra_texture, "LOAD.ACTUAL_TEXTURE_CARDINALITY").passed
    assert not _saved_load_check(load_report, document, typed, extra_texture, "LOAD.ACTUAL_RESOURCE_TOTAL").passed

    wrong_level = deepcopy(profile)
    wrong_level.materials[2].textures[0]["image"]["name"] = "loadscreen_mp_other"
    wrong_level.materials[2].textures[0]["image"]["width"] = 512
    assert not _saved_load_check(load_report, document, typed, wrong_level, "LOAD.ACTUAL_LEVEL_NAME").passed
    assert not _saved_load_check(load_report, document, typed, wrong_level, "LOAD.ACTUAL_LEVEL_GEOMETRY").passed

    wrong_victory = deepcopy(profile)
    wrong_victory.materials[0].textures[0]["image"]["name"] = "wrong_victory"
    wrong_victory.materials[0].textures[0]["image"]["mip_count"] = 1
    assert not _saved_load_check(load_report, document, typed, wrong_victory, "LOAD.ACTUAL_VICTORY_NAME").passed
    assert not _saved_load_check(load_report, document, typed, wrong_victory, "LOAD.ACTUAL_VICTORY_MIPS").passed


def test_version11_output_iwd_scan_excludes_only_reported_source_recursively() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        report_path = root / "output" / "mp_unit.python-port-report.json"
        report_path.parent.mkdir(parents=True)
        source_iwd = report_path.parent / "inputs" / "nested" / "Source.IWD"
        source_iwd.parent.mkdir(parents=True)
        source_iwd.write_bytes(b"source")
        report = {"iwd_paths": [str(source_iwd)]}

        unexpected, errors = c11._unexpected_output_iwds(
            report_path,
            report,
            (report_path.parent,),
        )
        assert errors == []
        assert unexpected == []

        generated = report_path.parent / "generated" / "Unexpected.IwD"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated")
        unexpected, errors = c11._unexpected_output_iwds(
            report_path,
            report,
            (report_path.parent,),
        )
        assert errors == []
        assert unexpected == [str(generated)]


if __name__ == "__main__":
    test_fx_count_deficit_requires_complete_fallback_accounting()
    test_version11_report_gate_distinguishes_structural_and_fidelity_debt()
    test_version11_report_gate_fails_closed_on_missing_evidence()
    test_version11_fifo_rejects_wrong_block_nonintegers_and_header_mismatch()
    test_version11_closure_schema_rejects_inconsistent_pass_flag()
    test_version11_saved_load_checks_exact_identity_topology_and_geometry()
    test_version11_output_iwd_scan_excludes_only_reported_source_recursively()
    print("PASS: Version-11 audit is generic, fail-closed and separates structural/fidelity status")

"""Offline preflight contract.

The porter cannot see the target console, so the failures it cannot report are
the ones that come from names the zone expects the installation to resolve:
TechniqueSets are never converted, only re-referenced, and with the default sound
policy no sound asset is written at all.  The preflight extracts those references
and, given the installation's own zone files, says which ones are not there.
"""

from pathlib import Path
import json
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.basic import RawFileNode
from cod4porter.assets.external import ExternalMaterialNode, ExternalTechniqueSetNode
from cod4porter.backend.v4_ffio import build_ps3_fastfile
from cod4porter.main_ff import build_main_fastfile, save_main_fastfile

TOOL = ROOT / "tools" / "ps3_preflight.py"


def synthetic_support_zone(path: Path, names) -> None:
    zone = bytearray(b"\0" * 36)
    zone += struct.pack(">4I", 0, 0, 0, 0)
    for name in names:
        zone += b"\0" + name.encode("latin-1") + b"\0"
    zone += b"\0" * (0x10000 - len(zone))
    struct.pack_into(">I", zone, 0, len(zone) - 36)
    path.write_bytes(build_ps3_fastfile(bytes(zone)))


def run(*args) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)
    assert proc.stdout, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def main():
    nodes = [
        ExternalTechniqueSetNode("mc_l_sm_r0c0", "techset:a"),
        ExternalTechniqueSetNode("wc_custom_thing", "techset:b"),
        ExternalMaterialNode("default", "material:def"),
        RawFileNode("maps/mp/mp_x.gsc", b"main(){}", "raw:x"),
    ]
    build = build_main_fastfile(nodes, ["alpha", None])

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        main_ff = temp / "mp_x.ff"
        save_main_fastfile(main_ff, build)

        # Without support zones the structural checks stand on their own and pass.
        code, result = run(str(main_ff))
        assert code == 0, [c for c in result["checks"] if not c["passed"]]
        assert result["passed"] and result["checks_failed"] == 0
        required = set(result["ps3_native_dependencies"]["required"])
        assert {"mc_l_sm_r0c0", "wc_custom_thing", "default"} <= required, required
        assert result["ps3_native_dependencies"]["missing"] is None
        assert result["ps3_native_dependencies"]["check_status"] == "not_checked_no_support_zones"
        assert result["ps3_native_dependencies"]["native_linker_executed"] is False

        # An installation that lacks one referenced TechniqueSet fails closed and names it.
        incomplete = temp / "common_mp.ff"
        synthetic_support_zone(incomplete, ["mc_l_sm_r0c0", "default", "some_other_asset"])
        code, result = run(str(main_ff), "--support-zone", str(incomplete))
        assert code == 2
        assert result["ps3_native_dependencies"]["missing"] == ["wc_custom_thing"]
        assert [c["key"] for c in result["checks"] if not c["passed"]] == ["DEPENDENCIES.RESOLVED"]

        # A complete installation resolves every reference.
        complete = temp / "common_mp_full.ff"
        synthetic_support_zone(complete, ["MC_L_SM_R0C0", "WC_CUSTOM_THING", "DEFAULT"])
        code, result = run(str(main_ff), "--support-zone", str(complete))
        assert code == 0, [c for c in result["checks"] if not c["passed"]]
        assert result["ps3_native_dependencies"]["missing"] == []

        # Explicit model-only manifests must not disappear when report evidence
        # replaces the approximate string scan.
        report=temp/'report.json'
        report.write_text(json.dumps({'ps3_native_dependencies':{'external_xmodels':['prop_flag_opfor']}}))
        code,result=run(str(main_ff),'--report',str(report),'--support-zone',str(complete))
        assert code==2 and result['ps3_native_dependencies']['missing']==['prop_flag_opfor']

        # The allocation gate is enforced on the saved file too.
        code, result = run(str(main_ff), "--ps3-zone-budget", "16")
        assert code == 2
        assert any(c["key"].endswith(".ZONE_BUDGET") and not c["passed"] for c in result["checks"])

        zone_info = next(iter(result["zones"].values()))
        assert all(size % 2 == 0 for size in zone_info["block_sizes"]), zone_info["block_sizes"]

    print(json.dumps({
        "passed": True,
        "required_native_names": sorted(required),
        "detects_missing_dependency": True,
        "enforces_zone_budget": True,
    }, indent=2))


if __name__ == "__main__":
    main()

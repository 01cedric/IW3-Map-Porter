from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assembler import analyze_and_assemble
from cod4porter.assets.image import ImageResourcePolicy
from cod4porter.assets.gfxworld import GfxWorldResourcePolicy
from cod4porter.graph import AssetType
from cod4porter.platform_state import resolution_tier


def main() -> int:
    pc_value = os.environ.get("IW3_PORTER_PC_NUKED_FF")
    iwd_value = os.environ.get("IW3_PORTER_PC_NUKED_IWD")
    if not pc_value or not iwd_value:
        print(json.dumps({
            "passed": True,
            "skipped": True,
            "reason": "set IW3_PORTER_PC_NUKED_FF and IW3_PORTER_PC_NUKED_IWD for the real Version-11 integration test",
        }))
        return 77
    pc = Path(pc_value).expanduser().resolve()
    iwd = Path(iwd_value).expanduser().resolve()
    if not pc.is_file() or not iwd.is_file():
        raise FileNotFoundError(f"Nuked integration fixtures missing: {pc}, {iwd}")

    result = analyze_and_assemble(
        pc,
        "mp_nuked",
        (iwd,),
        runtime_compatible=True,
        boot_isolation=False,
        gfxworld_resource_policy=GfxWorldResourcePolicy.FASTFILE_DELAYED,
        material_image_resource_policy=ImageResourcePolicy.FASTFILE_DELAYED,
        sound_policy="omit",
    )
    validation = result.plan.validate(result.source.asset_list)
    diagnostics = result.diagnostics
    source = result.source
    modes = Counter(planned.platform_state_mode for planned in source.materials.planned_owned)
    tiers = Counter(resolution_tier(mode) for mode in modes.elements())

    assert len(source.asset_list.assets) == 550
    assert validation["source_assets"] == 550
    assert diagnostics["boot_isolation"] is False
    assert diagnostics["sound"]["policy"] == "omit"
    assert diagnostics["sound"]["source_xassets"] == 149
    assert all(node.type not in (AssetType.SOUND, AssetType.SNDCURVE, AssetType.LOADED_SOUND) for node in result.plan.top_level_assets())
    assert diagnostics["fx_source_packed_visuals"] == diagnostics["fx_exact_packed_visuals"]
    assert diagnostics["fx_runtime_material_visuals"] == 0
    assert diagnostics["fx_runtime_null_visuals"] == 0
    assert diagnostics["fx_material_replay_unresolved_raws"] == 0
    assert not source.materials.image_proof["unresolved"]
    assert len(source.iwd_images) == 450
    assert tiers["exact_signature"] == 8
    assert tiers["hardware_observed_family"] == 350
    assert tiers["synthetic_assignment"] == 0
    print(json.dumps({
        "passed": True,
        "source_assets": 550,
        "destination_assets": validation["destination_assets"],
        "support_nodes": validation["support_nodes"],
        "iwd_images": len(source.iwd_images),
        "neutral_image_fallbacks": source.materials.image_proof["neutral_fallback_count"],
        "fx_exact_visuals": diagnostics["fx_exact_packed_visuals"],
        "platform_state_tiers": dict(tiers),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

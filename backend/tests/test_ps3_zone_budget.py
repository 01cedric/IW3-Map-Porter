"""PS3 zone-allocation budget and mip-level downscaling contract.

Evidence anchors, measured 2026-09-03 on real Retail PS3 zones:
  * ``mp_shipment.ff``  (6512, 165232, 5504, 63333760, 17503405, 0, 6257552) = 87,271,965
  * ``mp_crash.ff``     (6256, 169664, 0, 98589696, 24202937, 0, 9305488)    = 132,274,041
  * the Version-11/12/13 Nuked builds declared 194,427,262 bytes, 1.47x the larger one.

The budget planner must reach the Retail envelope by dropping whole top mip
levels from the largest textures, never by omitting an image or re-encoding a
retained level.
"""

from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.image import RESOURCE_ALIGN, ImageAsset, ImageMip, PS3_DXT1
from cod4porter.ps3_budget import (
    DEFAULT_ZONE_BUDGET_BYTES,
    RETAIL_PS3_CRASH_BLOCK3_BYTES,
    RETAIL_PS3_CRASH_ZONE_BYTES,
    RETAIL_PS3_SHIPMENT_BLOCK3_BYTES,
    RETAIL_PS3_SHIPMENT_ZONE_BYTES,
    can_drop_top_mip,
    check_zone_allocation,
    downscale_image,
    drop_top_mip_level,
    plan_image_budget,
)


def dxt1_bytes(width: int, height: int) -> int:
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8


def make_image(name: str, size: int, faces: int = 1, levels: int | None = None) -> ImageAsset:
    """Build a complete, deterministic DXT1 mip chain exactly like from_iwi does."""
    if levels is None:
        levels = size.bit_length()
    buffer = bytearray()
    mips: list[ImageMip] = []
    for face in range(faces):
        for level in range(levels):
            width = max(1, size >> level)
            height = max(1, size >> level)
            payload = bytes(((face * 31 + level * 7 + i) & 0xFF) for i in range(dxt1_bytes(width, height)))
            offset = len(buffer)
            buffer += payload
            mips.append(
                ImageMip(face, level, width, height, offset, len(payload),
                         hashlib.sha256(payload).hexdigest().upper())
            )
    unpadded = len(buffer)
    padding = (-unpadded) % RESOURCE_ALIGN
    buffer += b"\0" * padding
    return ImageAsset(name, 5 if faces == 6 else 3, "dxt1", PS3_DXT1, 2, size, size, 1,
                      levels, faces, bytes(buffer), unpadded, padding, tuple(mips))


def assert_retained_levels_exact(source: ImageAsset, scaled: ImageAsset, dropped: int) -> None:
    """Every level the scaled image keeps must be the source level, byte for byte."""
    by_key = {(m.face, m.level): m for m in source.mips}
    assert scaled.level_count == source.level_count - dropped
    assert scaled.face_count == source.face_count
    assert len(scaled.mips) == scaled.face_count * scaled.level_count
    for mip in scaled.mips:
        origin = by_key[(mip.face, mip.level + dropped)]
        assert (mip.width, mip.height, mip.size) == (origin.width, origin.height, origin.size)
        assert mip.sha256 == origin.sha256
        payload = scaled.resource[mip.offset:mip.offset + mip.size]
        assert hashlib.sha256(payload).hexdigest().upper() == origin.sha256
        assert payload == source.resource[origin.offset:origin.offset + origin.size]
    assert (scaled.width, scaled.height) == (by_key[(0, dropped)].width, by_key[(0, dropped)].height)
    assert len(scaled.resource) % RESOURCE_ALIGN == 0
    assert scaled.padding == (-scaled.unpadded) % RESOURCE_ALIGN
    assert scaled.unpadded == sum(m.size for m in scaled.mips)


def main():
    # --- one level off a 2D texture ------------------------------------------
    base = make_image("big_2d", 1024)
    one = drop_top_mip_level(base)
    assert_retained_levels_exact(base, one, 1)
    assert (one.width, one.height) == (512, 512)
    # A DXT mip chain is ~4/3 of its base level, so one level removes ~75 %.
    assert len(one.resource) < len(base.resource) // 3

    # --- cubemaps drop the level on every face -------------------------------
    cube = make_image("sky_cube", 256, faces=6)
    scaled_cube = downscale_image(cube, 2)
    assert_retained_levels_exact(cube, scaled_cube, 2)
    assert (scaled_cube.width, scaled_cube.height) == (64, 64)
    assert scaled_cube.map_type == 5 and scaled_cube.face_count == 6

    # --- floors are respected -------------------------------------------------
    small = make_image("hud_icon", 32)
    assert not can_drop_top_mip(small, min_base_dimension=32)
    assert can_drop_top_mip(small, min_base_dimension=16)
    single = make_image("flat", 4, levels=1)
    assert not can_drop_top_mip(single, min_base_dimension=1)

    # --- budget planning ------------------------------------------------------
    images = {f"image:{i}": make_image(f"tex_{i:02d}", 1024) for i in range(24)}
    images.update({f"icon:{i}": make_image(f"icon_{i:02d}", 32) for i in range(8)})
    source_total = sum(len(x.resource) for x in images.values())
    fixed = 4_000_000
    budget = 12_000_000
    plan = plan_image_budget(images, fixed_bytes=fixed, budget_bytes=budget)
    assert plan.within_budget, plan.as_dict()
    assert plan.projected_zone_bytes <= budget
    assert plan.planned_image_bytes < source_total
    # Small HUD textures must survive untouched while large ones absorb the cut.
    assert all(plan.dropped_levels[f"icon:{i}"] == 0 for i in range(8))
    assert any(plan.dropped_levels[f"image:{i}"] > 0 for i in range(24))
    # Deterministic: same inputs, same plan.
    again = plan_image_budget(images, fixed_bytes=fixed, budget_bytes=budget)
    assert again.dropped_levels == plan.dropped_levels
    # Applying the plan reproduces the planned byte count exactly, losslessly.
    applied = 0
    for key, asset in images.items():
        scaled = downscale_image(asset, plan.dropped_levels[key])
        assert_retained_levels_exact(asset, scaled, plan.dropped_levels[key])
        applied += len(scaled.resource)
    assert applied == plan.planned_image_bytes
    assert fixed + applied == plan.projected_zone_bytes

    # Protected images (caller-supplied Retail evidence) are never rescaled.
    guarded = plan_image_budget(images, fixed_bytes=fixed, budget_bytes=budget,
                                protected=[f"image:{i}" for i in range(4)])
    assert guarded.within_budget, guarded.as_dict()
    assert all(guarded.dropped_levels[f"image:{i}"] == 0 for i in range(4))
    assert any(guarded.dropped_levels[f"image:{i}"] > 0 for i in range(4, 24))

    # A map whose non-image allocation alone blows the budget must be reported,
    # never silently shipped.
    impossible = plan_image_budget(images, fixed_bytes=200_000_000, budget_bytes=budget)
    assert not impossible.within_budget

    # An already-small map is left completely alone.
    untouched = plan_image_budget(images, fixed_bytes=fixed, budget_bytes=1 << 40)
    assert set(untouched.dropped_levels.values()) == {0}
    assert untouched.planned_image_bytes == source_total

    # --- allocation gate ------------------------------------------------------
    # Both measured Retail zones must sit inside the default budget by construction.
    shipment = check_zone_allocation((6512, 165_232, 5504, RETAIL_PS3_SHIPMENT_BLOCK3_BYTES, 17_503_405, 0, 6_257_552))
    assert shipment["declared_zone_bytes"] == RETAIL_PS3_SHIPMENT_ZONE_BYTES, shipment
    assert shipment["within_budget"]
    crash = check_zone_allocation((6256, 169_664, 0, RETAIL_PS3_CRASH_BLOCK3_BYTES, 24_202_937, 0, 9_305_488))
    assert crash["declared_zone_bytes"] == RETAIL_PS3_CRASH_ZONE_BYTES, crash
    assert crash["within_budget"]
    version13 = check_zone_allocation((936, 54_848, 0, 80_337_280, 33_696_598, 0, 80_343_552))
    assert not version13["within_budget"], version13
    assert version13["declared_zone_bytes"] == 194_433_214
    assert 1.4 < version13["ratio_vs_retail_reference"] < 1.5, version13
    assert DEFAULT_ZONE_BUDGET_BYTES == RETAIL_PS3_CRASH_ZONE_BYTES

    print(json.dumps({
        "passed": True,
        "retail_reference_zone_bytes": RETAIL_PS3_CRASH_ZONE_BYTES,
        "budget_bytes": DEFAULT_ZONE_BUDGET_BYTES,
        "planned_images": len(plan.rows),
        "scaled_images": plan.scaled_images,
        "source_image_bytes": plan.source_image_bytes,
        "planned_image_bytes": plan.planned_image_bytes,
        "version13_declared_zone_bytes": version13["declared_zone_bytes"],
        "version13_ratio_vs_retail": version13["ratio_vs_retail_reference"],
    }, indent=2))


if __name__ == "__main__":
    main()

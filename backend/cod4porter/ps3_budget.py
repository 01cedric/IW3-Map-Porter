"""PS3 zone-allocation budget and Retail-faithful image downscaling.

Evidence
--------
Two real Retail IW3 PS3 multiplayer main zones were measured for this module:
``mp_shipment.ff`` declares **87,271,965** bytes across its seven loader blocks
(63,333,760 of them the Block-3 delayed-image FIFO) and ``mp_crash.ff`` declares
**132,274,041** bytes (98,589,696 in Block 3).  The console therefore does load a
zone well above the small-map figure, and the budget follows the largest measured
one rather than the smallest.

A PS3 has 256 MiB of XDR main memory and CoD4 keeps only a fraction of it free
for a map zone.  Version 11/12/13 Nuked builds declared 194,427,262 bytes,
i.e. 1.47x the largest measured Retail zone.  ``DB_AllocXZoneMemory`` cannot satisfy
that request, and IW3 does not fail such a load cleanly: the loading screen
stops at whatever percentage the loader had reached, which is exactly the
reported symptom ("freezes at different points").

Retail console ports reach their budget by shipping *smaller* textures than the
PC build, not by omitting assets.  :func:`plan_image_budget` reproduces that
behaviour: it drops whole top mip levels from the largest images until the
projected zone allocation fits the budget.  Every image, every face and every
retained mip level stays byte-identical to its PC source, so the result is still
a complete, self-consistent texture set - only at console resolution.

Nothing in this module invents pixel data, drops an image, or splits a resource.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from .assets.image import RESOURCE_ALIGN, ImageAsset, ImageMip

# --- Retail oracle -----------------------------------------------------------
# Measured 2026-09-03 directly on real PS3 retail CoD4 zones:
#   mp_shipment.ff  blocks (6512, 165232, 5504, 63333760, 17503405, 0, 6257552)
#   mp_crash.ff     blocks (6256, 169664,    0, 98589696, 24202937, 0, 9305488)

#: Total declared block allocation of the Retail PS3 ``mp_shipment.ff`` (small map).
RETAIL_PS3_SHIPMENT_ZONE_BYTES = 87_271_965
#: Its Block-3 (delayed image FIFO) share.
RETAIL_PS3_SHIPMENT_BLOCK3_BYTES = 63_333_760
#: Total declared block allocation of the Retail PS3 ``mp_crash.ff`` (medium map).
RETAIL_PS3_CRASH_ZONE_BYTES = 132_274_041
#: Its Block-3 share - the largest measured Retail PS3 image allocation.
RETAIL_PS3_CRASH_BLOCK3_BYTES = 98_589_696

#: Default ceiling for a generated zone: the largest Retail PS3 zone that is known
#: to load on the console.  Anything above this is unproven rather than proven
#: impossible - raise it with ``--ps3-zone-budget`` once a larger Retail map has
#: been measured.
DEFAULT_ZONE_BUDGET_BYTES = RETAIL_PS3_CRASH_ZONE_BYTES

#: Never shrink a texture below this base edge length.
DEFAULT_MIN_BASE_DIMENSION = 32

# Measured texture-resolution convention of the Retail PS3 build.  Base-edge histograms
# of the two Retail map zones:
#   mp_shipment  4:3  32:1  64:34  128:16  256:271  512:120  1024:49
#   mp_crash          32:2  64:26  128:37  256:504  512:183  1024:56  2048:1
# The console build simply ships smaller textures than the PC build: the bulk sits at
# 256, the ceiling is 1024, and the single 2048 in mp_crash is one semantic-1 (sky)
# image.  Capping at the Retail ceiling is therefore the primary size mechanism; the
# zone budget only has to close whatever gap is left afterwards.
RETAIL_PS3_MAX_TEXTURE_EDGE = 1024
RETAIL_PS3_TYPICAL_TEXTURE_EDGE = 256


class ZoneBudgetExceeded(ValueError):
    """The projected zone allocation cannot be brought under the budget."""


# --- Image downscaling -------------------------------------------------------


def _faces(asset: ImageAsset) -> dict[int, list[ImageMip]]:
    faces: dict[int, list[ImageMip]] = {}
    for mip in asset.mips:
        faces.setdefault(int(mip.face), []).append(mip)
    for mips in faces.values():
        mips.sort(key=lambda m: int(m.level))
    return faces


def can_drop_top_mip(asset: ImageAsset, min_base_dimension: int = DEFAULT_MIN_BASE_DIMENSION) -> bool:
    """Report whether one complete top mip level can be removed losslessly.

    The image must expose a full ``face x level`` grid, keep at least two levels
    and stay at or above ``min_base_dimension`` afterwards.  Anything else is
    left untouched rather than reshaped on an assumption.
    """
    if asset.level_count < 2 or not asset.mips:
        return False
    faces = _faces(asset)
    if sorted(faces) != list(range(asset.face_count)):
        return False
    for mips in faces.values():
        if [int(m.level) for m in mips] != list(range(asset.level_count)):
            return False
    nxt = faces[0][1]
    # The floor guards the *base edge*, i.e. the larger dimension.  Applying it to both
    # edges froze long thin textures at PC resolution: mp_getaway's
    # airplane_contrail_scrolling_col is 2048x32, and requiring height >= 32 kept it at
    # 2048 - wider than any Retail PS3 texture - because its next level is only 16 tall.
    if max(int(nxt.width), int(nxt.height)) < min_base_dimension:
        return False
    return True


def drop_top_mip_level(asset: ImageAsset) -> ImageAsset:
    """Return ``asset`` without its level-0 mip, on every face.

    The retained level payloads are copied verbatim - their SHA-256 values are
    carried over unchanged, which lets the port report prove that no pixel was
    re-encoded.  The resource stays ``RESOURCE_ALIGN`` padded exactly like
    :func:`cod4porter.assets.image.from_iwi` produces it.
    """
    if not can_drop_top_mip(asset, min_base_dimension=1):
        raise ValueError(f"image {asset.name!r} has no removable complete top mip level")
    faces = _faces(asset)
    source = asset.resource
    buffer = bytearray()
    mips: list[ImageMip] = []
    for face in range(asset.face_count):
        for mip in faces[face][1:]:
            start = int(mip.offset)
            data = source[start:start + int(mip.size)]
            if len(data) != int(mip.size):
                raise ValueError(f"image {asset.name!r} mip payload is truncated")
            offset = len(buffer)
            buffer += data
            mips.append(
                ImageMip(face, int(mip.level) - 1, int(mip.width), int(mip.height), offset, int(mip.size), mip.sha256)
            )
    unpadded = len(buffer)
    padding = (-unpadded) % RESOURCE_ALIGN
    buffer += b"\0" * padding
    base = faces[0][1]
    return replace(
        asset,
        width=int(base.width),
        height=int(base.height),
        depth=max(1, asset.depth >> 1) if asset.depth > 1 else asset.depth,
        level_count=asset.level_count - 1,
        resource=bytes(buffer),
        unpadded=unpadded,
        padding=padding,
        mips=tuple(mips),
    )


def downscale_image(asset: ImageAsset, levels: int) -> ImageAsset:
    """Drop ``levels`` complete top mip levels."""
    if levels < 0:
        raise ValueError("cannot drop a negative number of mip levels")
    out = asset
    for _ in range(levels):
        out = drop_top_mip_level(out)
    return out


# --- Budget planning ---------------------------------------------------------


@dataclass(frozen=True)
class BudgetImageRow:
    key: str
    name: str
    dropped_levels: int
    source_bytes: int
    planned_bytes: int
    source_width: int
    source_height: int
    planned_width: int
    planned_height: int
    source_levels: int
    planned_levels: int


@dataclass(frozen=True)
class ZoneBudgetPlan:
    max_edge: int
    capped_images: int
    budget_bytes: int
    fixed_bytes: int
    source_image_bytes: int
    planned_image_bytes: int
    projected_zone_bytes: int
    within_budget: bool
    dropped_levels: Mapping[str, int]
    rows: tuple[BudgetImageRow, ...]
    min_base_dimension: int

    @property
    def saved_bytes(self) -> int:
        return self.source_image_bytes - self.planned_image_bytes

    @property
    def scaled_images(self) -> int:
        return sum(1 for row in self.rows if row.dropped_levels)

    def as_dict(self) -> dict:
        return {
            "retail_max_texture_edge": self.max_edge,
            "retail_typical_texture_edge": RETAIL_PS3_TYPICAL_TEXTURE_EDGE,
            "images_capped_to_retail_resolution": self.capped_images,
            "budget_bytes": self.budget_bytes,
            "retail_reference_zone_bytes": RETAIL_PS3_CRASH_ZONE_BYTES,
            "fixed_bytes": self.fixed_bytes,
            "source_image_bytes": self.source_image_bytes,
            "planned_image_bytes": self.planned_image_bytes,
            "saved_bytes": self.saved_bytes,
            "projected_zone_bytes": self.projected_zone_bytes,
            "within_budget": self.within_budget,
            "min_base_dimension": self.min_base_dimension,
            "images": len(self.rows),
            "scaled_images": self.scaled_images,
            "scaled": [
                {
                    "name": row.name,
                    "dropped_levels": row.dropped_levels,
                    "source": f"{row.source_width}x{row.source_height}/{row.source_levels}",
                    "planned": f"{row.planned_width}x{row.planned_height}/{row.planned_levels}",
                    "source_bytes": row.source_bytes,
                    "planned_bytes": row.planned_bytes,
                }
                for row in self.rows
                if row.dropped_levels
            ],
        }


def plan_image_budget(
    images: Mapping[str, ImageAsset],
    *,
    fixed_bytes: int,
    budget_bytes: int = DEFAULT_ZONE_BUDGET_BYTES,
    min_base_dimension: int = DEFAULT_MIN_BASE_DIMENSION,
    max_edge: int = RETAIL_PS3_MAX_TEXTURE_EDGE,
    protected: Iterable[str] = (),
) -> ZoneBudgetPlan:
    """Choose per-image mip drops so that the zone fits ``budget_bytes``.

    ``fixed_bytes`` is the declared block allocation the zone needs *without* the
    payloads in ``images`` - measure it with one layout pass that keeps the image
    shells but omits their resources.

    The rule is deterministic: repeatedly take the currently largest image that
    can still lose a complete top mip level and drop one, tie-broken by key.
    Largest-first converges quickly because one DXT level removes about 75 % of
    an image, and it protects small UI/HUD textures from being touched at all.
    """
    if budget_bytes <= 0:
        raise ValueError("zone budget must be positive")
    if fixed_bytes < 0:
        raise ValueError("fixed zone allocation cannot be negative")

    # Images the caller holds independent Retail evidence for (supplied cubemap
    # face hashes, for example) keep their exact source payload; scaling them
    # would silently invalidate that proof.
    frozen = {key for key in protected if key in images}
    current = dict(images)
    dropped: dict[str, int] = {key: 0 for key in current}
    image_bytes = sum(len(asset.resource) for asset in current.values())
    source_bytes = image_bytes
    capped = 0

    def total() -> int:
        return fixed_bytes + image_bytes

    # Phase 1 - Retail resolution convention.  Independent of the budget: a PC texture
    # simply does not ship at PC resolution on PS3.
    if max_edge:
        for key, asset in current.items():
            if key in frozen:
                continue
            while (max(current[key].width, current[key].height) > max_edge
                   and can_drop_top_mip(current[key], min_base_dimension=min_base_dimension)):
                before = len(current[key].resource)
                current[key] = drop_top_mip_level(current[key])
                image_bytes += len(current[key].resource) - before
                dropped[key] += 1
            if dropped[key]:
                capped += 1

    # Phase 2 - close whatever gap to the zone budget is still open.
    while total() > budget_bytes:
        candidates = [
            key for key, asset in current.items()
            if key not in frozen and can_drop_top_mip(asset, min_base_dimension=min_base_dimension)
        ]
        if not candidates:
            break
        key = min(candidates, key=lambda k: (-len(current[k].resource), k))
        before = len(current[key].resource)
        current[key] = drop_top_mip_level(current[key])
        image_bytes += len(current[key].resource) - before
        dropped[key] += 1

    rows = tuple(
        BudgetImageRow(
            key=key,
            name=images[key].name,
            dropped_levels=dropped[key],
            source_bytes=len(images[key].resource),
            planned_bytes=len(current[key].resource),
            source_width=images[key].width,
            source_height=images[key].height,
            planned_width=current[key].width,
            planned_height=current[key].height,
            source_levels=images[key].level_count,
            planned_levels=current[key].level_count,
        )
        for key in sorted(images)
    )
    return ZoneBudgetPlan(
        max_edge=max_edge,
        capped_images=capped,
        budget_bytes=budget_bytes,
        fixed_bytes=fixed_bytes,
        source_image_bytes=source_bytes,
        planned_image_bytes=image_bytes,
        projected_zone_bytes=total(),
        within_budget=total() <= budget_bytes,
        dropped_levels=dict(dropped),
        rows=rows,
        min_base_dimension=min_base_dimension,
    )


def check_zone_allocation(
    block_sizes: Sequence[int],
    *,
    budget_bytes: int = DEFAULT_ZONE_BUDGET_BYTES,
) -> dict:
    """Compare a finished zone against the Retail-derived allocation budget."""
    blocks = tuple(int(x) for x in block_sizes)
    declared = sum(blocks)
    return {
        "declared_zone_bytes": declared,
        "block_sizes": blocks,
        "budget_bytes": int(budget_bytes),
        "retail_reference_zone_bytes": RETAIL_PS3_CRASH_ZONE_BYTES,
        "retail_reference_block3_bytes": RETAIL_PS3_CRASH_BLOCK3_BYTES,
        "ratio_vs_retail_reference": round(declared / RETAIL_PS3_CRASH_ZONE_BYTES, 4),
        "within_budget": declared <= int(budget_bytes),
    }


def source_mip_signature(mips: Iterable) -> tuple:
    """Normalize IWI/destination mip rows into comparable tuples."""
    rows = []
    for mip in mips:
        rows.append(
            (
                int(getattr(mip, "face", 0)),
                int(getattr(mip, "level", 0)),
                int(getattr(mip, "width", 0)),
                int(getattr(mip, "height", 0)),
            )
        )
    return tuple(sorted(rows))

"""The budget projection must equal the allocation the writer actually declares.

The porter measures the zone with the IWD image payloads omitted, adds the
planned image bytes and gates on that number.  If the projection drifted from
the real layout the gate would either pass an oversized zone or reject a valid
one, so this test pins projection == measurement on the Retail image layout
(every resource in the Block-3 FIFO).
"""

from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.image import (
    PS3_DXT1,
    RESOURCE_ALIGN,
    ImageAsset,
    ImageMip,
    ImageNode,
    ImageResourcePolicy,
)
from cod4porter.graph import write_graph
from cod4porter.ps3_budget import check_zone_allocation, downscale_image, plan_image_budget


def make_image(name: str, size: int) -> ImageAsset:
    levels = size.bit_length()
    buffer = bytearray()
    mips: list[ImageMip] = []
    for level in range(levels):
        edge = max(1, size >> level)
        payload = bytes(((level * 7 + i) & 0xFF) for i in range(max(1, (edge + 3) // 4) ** 2 * 8))
        offset = len(buffer)
        buffer += payload
        mips.append(ImageMip(0, level, edge, edge, offset, len(payload),
                             hashlib.sha256(payload).hexdigest().upper()))
    unpadded = len(buffer)
    padding = (-unpadded) % RESOURCE_ALIGN
    buffer += b"\0" * padding
    return ImageAsset(name, 3, "dxt1", PS3_DXT1, 2, size, size, 1, levels, 1,
                      bytes(buffer), unpadded, padding, tuple(mips))


def measure(nodes) -> tuple[int, ...]:
    return write_graph(nodes, []).zone.block_sizes


def main():
    nodes = [
        ImageNode(make_image(f"tex_{i:02d}", 512 if i % 2 else 256), f"image:{i}",
                  ImageResourcePolicy.FASTFILE_DELAYED)
        for i in range(8)
    ]

    saved = [node.resource_policy for node in nodes]
    for node in nodes:
        node.resource_policy = ImageResourcePolicy.RETAIL_DELAYED
    baseline = measure(nodes)
    for node, policy in zip(nodes, saved):
        node.resource_policy = policy

    image_bytes = sum(len(node.image.resource) for node in nodes)
    full = measure(nodes)
    assert sum(full) == sum(baseline) + image_bytes, (baseline, full, image_bytes)
    assert full[3] == image_bytes, full

    budget = sum(baseline) + image_bytes // 3
    plan = plan_image_budget(
        {node.symbol: node.image for node in nodes},
        fixed_bytes=sum(baseline),
        budget_bytes=budget,
        min_base_dimension=16,
    )
    assert plan.within_budget, plan.as_dict()
    for node in nodes:
        levels = plan.dropped_levels[node.symbol]
        if levels:
            node.image = downscale_image(node.image, levels)

    after = measure(nodes)
    assert sum(after) == plan.projected_zone_bytes, (after, plan.projected_zone_bytes)
    assert sum(after) <= budget
    gate = check_zone_allocation(after, budget_bytes=budget)
    assert gate["within_budget"] and gate["declared_zone_bytes"] == sum(after)
    over = check_zone_allocation(after, budget_bytes=sum(after) - 1)
    assert not over["within_budget"]

    print(json.dumps({
        "passed": True,
        "baseline_blocks": list(baseline),
        "unbudgeted_blocks": list(full),
        "budgeted_blocks": list(after),
        "budget_bytes": budget,
        "projection_exact": True,
        "scaled_images": plan.scaled_images,
    }, indent=2))


if __name__ == "__main__":
    main()

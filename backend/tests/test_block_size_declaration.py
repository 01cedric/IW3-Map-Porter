"""A declared loader block size is the exact allocator high-water mark.

Measured 2026-09-03 on real Retail PS3 CoD4 zones:

    mp_shipment_load.ff  (196, 0, 0, 0, 198, 0, 1223424)
    mp_shipment.ff       (6512, 165232, 5504, 63333760, 17503405, 0, 6257552)
    mp_crash.ff          (6256, 169664, 0, 98589696, 24202937, 0, 9305488)

Block 4 is ODD in both main zones, so the declaration is never rounded to an even
boundary.  This test exists because an earlier fix inferred a 2-byte rounding rule
from the Fix89B mp_getaway_load baseline, whose 198 was inherited from the Shipment
header rather than produced by its own content.
"""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets import ExternalFxNode, RawFileNode, StringTableNode
from cod4porter.backend.v4_ffio import parse_zone_header
from cod4porter.graph import write_graph
from cod4porter.load_zone import compute_load_block_sizes, read_load_document
from cod4porter.zone_writer import (
    BLOCK_SIZE_GRANULARITY,
    RelocatingZoneWriter,
    VIRTUAL_BLOCK,
    declared_block_size,
)

RETAIL_MAIN_BLOCKS = (
    (6512, 165232, 5504, 63333760, 17503405, 0, 6257552),
    (6256, 169664, 0, 98589696, 24202937, 0, 9305488),
)


def main():
    assert BLOCK_SIZE_GRANULARITY == 1
    for used in (0, 1, 196, 197, 198, 17_503_405, 24_202_937):
        assert declared_block_size(used) == used, used
    try:
        declared_block_size(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative high-water mark accepted")

    # Retail main zones really do declare odd blocks; the writer must be able to.
    assert any(size % 2 for blocks in RETAIL_MAIN_BLOCKS for size in blocks)

    # An odd Block-4 high-water mark is declared as exactly that.
    w = RelocatingZoneWriter()
    w.push_block(VIRTUAL_BLOCK)
    w.write_in_block(b"\x01" * 197, "odd virtual payload")
    w.pop_block()
    result = w.build()
    assert result.block_sizes[VIRTUAL_BLOCK] == 197, result.block_sizes

    assets = [
        RawFileNode("maps/mp/odd_length_name.gsc", b"main(){}", "raw:odd"),
        StringTableNode("mp/odd.csv", 2, 1, ["mp_odd", "X"], "st:odd"),
        ExternalFxNode("misc/odd", "fx:odd"),
    ]
    graph = write_graph(assets, ["alpha", None, "beta"])
    header = parse_zone_header(graph.zone.zone_bytes, "ps3")
    assert header.block_sizes == graph.zone.block_sizes
    for relocation in graph.zone.relocations:
        assert relocation.target.offset < header.block_sizes[relocation.target.block]

    for name, expected in (
        ("mp_getaway_load_fix89b.ff", 197),
        ("mp_getaway_load_version09.ff", 197),
    ):
        document = read_load_document(ROOT / "reference" / name)
        assert compute_load_block_sizes(document)[4] == expected, name

    print(json.dumps({
        "passed": True,
        "granularity": BLOCK_SIZE_GRANULARITY,
        "graph_block_sizes": list(header.block_sizes),
        "retail_main_blocks_have_odd_sizes": True,
    }, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.gfxworld import (
    GfxImagePayload,
    GfxWorldNode,
    GfxWorldResourcePolicy,
    PS3_B8,
    _ImagePlan,
    pack_rsx_world_unit_vector,
)
from cod4porter.zone_writer import (
    DELAYED_IMAGE_BLOCK,
    FOLLOWING,
    INSERT_BLOCK,
    TEMP_BLOCK,
    VIRTUAL_BLOCK,
    RelocatingZoneWriter,
)


def _plan() -> _ImagePlan:
    source = GfxImagePayload(
        'reflectionProbe[0]', 0, '*reflection_probe0', 3,
        0, 0, 0, 1, 0, 2, 1, 1, 0,
        16, 8, 1, 50, 128, 0, 128, 'SOURCE',
    )
    payload = bytes(range(128))
    digest = hashlib.sha256(payload).hexdigest().upper()
    return _ImagePlan(source, PS3_B8, payload, 128, digest, 1)


def test_retail_rsx_111110_vector_codec() -> None:
    # Independent Version09 byte oracles: PC PackedUnitVec -> PS3 BE RSX word.
    cases = {
        bytes.fromhex('7ffe7f3f'): 0x001FF800,
        bytes.fromhex('007f7f3f'): 0x00000401,
        bytes.fromhex('aaa eed3f'.replace(' ', '')): 0x6ECBD95A,
        bytes.fromhex('22d67f3f'): 0x0015ED13,
        bytes.fromhex('7f9bfb3f'): 0x7CC71000,
        bytes.fromhex('7cfb633f'): 0xE3DF3FE8,
    }
    for source, expected in cases.items():
        assert pack_rsx_world_unit_vector(source) == expected


def test_global_delayed_fifo_is_block3_tail() -> None:
    node = object.__new__(GfxWorldNode)
    node._images = {'reflectionProbe[0]': _plan()}
    node.image_resource_policy = GfxWorldResourcePolicy.FASTFILE_DELAYED
    node._image_emit_stats = {}
    writer = RelocatingZoneWriter()
    writer.push_block(TEMP_BLOCK)
    shell = writer.physical_position
    node._write_image(writer, 'reflectionProbe[0]')
    normal_end = writer.physical_position
    writer.pop_block()
    result = writer.build()
    row = node._image_emit_stats['entries'][0]
    assert DELAYED_IMAGE_BLOCK == 3
    assert VIRTUAL_BLOCK == INSERT_BLOCK == 4
    assert result.block_sizes[3] == 128
    assert result.block_sizes[6] == 0
    assert row['resource_physical'] == normal_end
    assert row['resource_logical_block'] == 3
    assert row['resource_logical_offset'] == 0
    assert result.zone_bytes[normal_end:] == bytes(range(128))
    assert result.zone_bytes[shell + 0x2B] == 1
    assert struct.unpack_from('>I', result.zone_bytes, shell + 0x2C)[0] == FOLLOWING


def main() -> None:
    test_retail_rsx_111110_vector_codec()
    test_global_delayed_fifo_is_block3_tail()
    print({'passed': True, 'vector_oracles': 6, 'fifo_bytes': 128})


if __name__ == '__main__':
    main()

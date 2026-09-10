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
    _convert_image,
)
from cod4porter.zone_writer import FOLLOWING, TEMP_BLOCK, RelocatingZoneWriter


def make_plan(delay: int) -> _ImagePlan:
    src = GfxImagePayload(
        'lightmap[0].primary',
        0,
        '*lightmap0_primary',
        3,
        0,
        0,
        0,
        1,
        0,
        2,
        delay,
        1,
        0,
        1024,
        1024,
        1,
        50,
        128,
        0,
        128,
        'X',
    )
    data = bytes(range(128))
    return _ImagePlan(
        src,
        PS3_B8,
        data,
        len(data),
        hashlib.sha256(data).hexdigest().upper(),
        1,
    )


def emit(delay: int, policy: GfxWorldResourcePolicy):
    # Exercise the image primitive directly without needing a complete GfxWorld fixture.
    node = object.__new__(GfxWorldNode)
    node._images = {'lightmap[0].primary': make_plan(delay)}
    node.image_resource_policy = policy
    node._image_emit_stats = {}

    writer = RelocatingZoneWriter()
    writer.push_block(TEMP_BLOCK)
    start = writer.physical_position
    node._write_image(writer, 'lightmap[0].primary')
    writer.pop_block()
    result = writer.build()
    return start, result, node._image_emit_stats


def assert_shell(start: int, zone: bytes, *, expected_delay: int) -> None:
    assert struct.unpack_from('>I', zone, start + 0x20)[0] == 128
    assert struct.unpack_from('>I', zone, start + 0x2C)[0] == FOLLOWING
    assert zone[start + 0x2B] == expected_delay


def test_retail_delayed_forces_delayed_and_omits_pixels() -> None:
    for source_delay in (0, 1):
        start, result, stats = emit(source_delay, GfxWorldResourcePolicy.RETAIL_DELAYED)
        assert_shell(start, result.zone_bytes, expected_delay=1)
        assert result.block_sizes[6] == 0
        assert len(result.zone_bytes) == start + 0x34 + len('*lightmap0_primary') + 1
        assert stats['policy'] == 'retail-delayed'
        assert stats['images'] == 1
        assert stats['delayed_images'] == 1
        assert stats['embedded_images'] == 0
        assert stats['declared_resource_bytes'] == 128
        assert stats['embedded_resource_bytes'] == 0
        assert stats['omitted_resource_bytes'] == 128


def test_self_contained_forces_inline_pixels() -> None:
    for source_delay in (0, 1):
        start, result, stats = emit(source_delay, GfxWorldResourcePolicy.SELF_CONTAINED)
        assert_shell(start, result.zone_bytes, expected_delay=0)
        assert result.block_sizes[6] == 128
        assert len(result.zone_bytes) == start + 0x34 + len('*lightmap0_primary') + 1 + 128
        assert stats['embedded_images'] == 1
        assert stats['embedded_resource_bytes'] == 128
        assert stats['omitted_resource_bytes'] == 0


def test_source_policy_preserves_source_delay_flag() -> None:
    delayed_start, delayed, delayed_stats = emit(1, GfxWorldResourcePolicy.SOURCE)
    assert_shell(delayed_start, delayed.zone_bytes, expected_delay=1)
    assert delayed.block_sizes[6] == 0
    assert delayed_stats['omitted_resource_bytes'] == 128

    inline_start, inline, inline_stats = emit(0, GfxWorldResourcePolicy.SOURCE)
    assert_shell(inline_start, inline.zone_bytes, expected_delay=0)
    assert inline.block_sizes[6] == 128
    assert inline_stats['embedded_resource_bytes'] == 128


def test_zero_level_cubemap_is_normalized_and_face_aligned() -> None:
    # PC reflection probes encode levelCount=0 even though the payload is a complete
    # 64..1 ARGB cubemap mip chain. PS3 retail normalizes this to 7 levels and pads
    # each face chain independently to 0x80: 21,844 -> 21,888 bytes per face.
    face = sum(max(1, 64 >> mip) * max(1, 64 >> mip) * 4 for mip in range(7))
    raw = bytes(face * 6)
    cubemap = GfxImagePayload(
        'reflectionProbe[0]',
        0,
        '*reflection_probe0',
        5,
        0,
        0,
        0,
        1,
        0,
        1,
        1,
        0,
        4,
        64,
        64,
        1,
        21,
        len(raw),
        0,
        len(raw),
        'X',
    )
    plan = _convert_image(raw, cubemap)
    metadata_only = _convert_image(raw, cubemap, materialize=False)
    assert plan.level_count == 7
    assert plan.unpadded == 131064
    assert len(plan.resource) == 131328
    assert plan.declared_resource_size == 131328
    assert metadata_only.declared_resource_size == plan.declared_resource_size
    assert metadata_only.resource == b''
    assert not metadata_only.materialized
    assert metadata_only.source_sha256


def main() -> None:
    test_retail_delayed_forces_delayed_and_omits_pixels()
    test_self_contained_forces_inline_pixels()
    test_source_policy_preserves_source_delay_flag()
    test_zero_level_cubemap_is_normalized_and_face_aligned()
    print({'passed': True, 'policies': 3, 'cubemap_levels': 7, 'cubemap_resource': 131328})


if __name__ == '__main__':
    main()

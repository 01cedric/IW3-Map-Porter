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
    _new_image_emit_stats,
)
from cod4porter.zone_writer import DELAYED_IMAGE_BLOCK, FOLLOWING, TEMP_BLOCK, VERTEX_BLOCK, RelocatingZoneWriter


def _resource_less_sky(*, delay: int = 1, name: str = 'cobra_ft') -> GfxImagePayload:
    # mp_nuked's sky owns a PC LoadDef descriptor but deliberately carries no
    # pixels.  delayLoadPixels=1 is the evidence that this is a support/native
    # identity rather than a truncated inline texture.
    return GfxImagePayload(
        'skyImage', 0, name, 5,
        0, 0, 1, 2, 0, 3, delay, 1, 6,
        512, 512, 1, 0x31545844, 0, 0, 0,
        'E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855',
    )


def test_delayed_resource_less_sky_is_native_not_a_donor_payload() -> None:
    plan = _convert_image(b'', _resource_less_sky())
    assert plan.external_native
    assert plan.declared_resource_size == 0
    assert plan.resource == b''

    node = object.__new__(GfxWorldNode)
    node._images = {'skyImage': plan}
    node.image_resource_policy = GfxWorldResourcePolicy.FASTFILE_DELAYED
    node._image_emit_stats = {}
    writer = RelocatingZoneWriter()
    writer.push_block(TEMP_BLOCK)
    shell = writer.physical_position
    node._write_image(writer, 'skyImage')
    writer.pop_block()
    result = writer.build()

    # Retail nested external-image form: reserved shell body, no LoadDef/data,
    # and the exact comma-prefixed stock identity immediately after the shell.
    assert result.zone_bytes[shell:shell + 0x30] == bytes(0x30)
    assert struct.unpack_from('>I', result.zone_bytes, shell + 0x30)[0] == FOLLOWING
    assert result.zone_bytes[shell + 0x34:] == b',cobra_ft\0'
    assert result.block_sizes[DELAYED_IMAGE_BLOCK] == 0
    assert result.block_sizes[VERTEX_BLOCK] == 0
    assert b'sp_bonus_ft' not in result.zone_bytes

    stats = node._image_emit_stats
    assert stats['images'] == 1
    assert stats['external_native_images'] == 1
    assert stats['entries'] == []
    assert stats['external_entries'][0]['name'] == ',cobra_ft'
    assert stats['external_entries'][0]['loaddef_location'] is None


def test_non_delayed_descriptor_without_pixels_fails_closed() -> None:
    try:
        _convert_image(b'', _resource_less_sky(delay=0, name='custom_sky_ft'))
    except ValueError as error:
        assert 'bind the matching IWD/FF payload' in str(error)
    else:
        raise AssertionError('non-delayed zero-resource descriptor did not fail closed')


def test_resource_less_image_requires_exact_external_identity() -> None:
    try:
        _convert_image(b'', _resource_less_sky(name='packed:0x40001234'))
    except ValueError as error:
        assert 'no exact native image identity' in str(error)
    else:
        raise AssertionError('unresolved external identity did not fail closed')


def test_external_sky_and_deferred_pixels_share_complete_stats_and_fifo() -> None:
    external = _convert_image(b'', _resource_less_sky())
    payload = bytes(range(128))
    source = GfxImagePayload(
        'reflectionProbe[0]', 0, '*reflection_probe0', 3,
        0, 0, 0, 1, 0, 2, 1, 1, 0,
        16, 8, 1, 50, 128, 0, 128, 'SOURCE',
    )
    deferred = _ImagePlan(
        source, PS3_B8, payload, len(payload),
        hashlib.sha256(payload).hexdigest().upper(), 1,
    )
    node = object.__new__(GfxWorldNode)
    node._images = {'skyImage': external, 'reflectionProbe[0]': deferred}
    node.image_resource_policy = GfxWorldResourcePolicy.FASTFILE_DELAYED
    # This is the same eager initialization used by GfxWorldNode.write.
    node._image_emit_stats = _new_image_emit_stats(node.image_resource_policy)

    writer = RelocatingZoneWriter()
    writer.push_block(TEMP_BLOCK)
    node._write_image(writer, 'skyImage')
    node._write_image(writer, 'reflectionProbe[0]')
    normal_end = writer.physical_position
    writer.pop_block()
    result = writer.build()

    assert result.block_sizes[DELAYED_IMAGE_BLOCK] == 128
    assert result.zone_bytes[normal_end:] == payload
    stats = node._image_emit_stats
    assert stats['images'] == 2
    assert stats['external_native_images'] == 1
    assert stats['delayed_images'] == 1
    assert len(stats['external_entries']) == 1
    assert len(stats['entries']) == 1
    assert stats['entries'][0]['deferred']
    assert stats['entries'][0]['fifo_index'] == 0


def main() -> None:
    test_delayed_resource_less_sky_is_native_not_a_donor_payload()
    test_non_delayed_descriptor_without_pixels_fails_closed()
    test_resource_less_image_requires_exact_external_identity()
    test_external_sky_and_deferred_pixels_share_complete_stats_and_fifo()
    print({'passed': True, 'external_name': ',cobra_ft', 'donor_bytes': 0, 'deferred_bytes': 128})


if __name__ == '__main__':
    main()

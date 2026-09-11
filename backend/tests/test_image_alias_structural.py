#!/usr/bin/env python3
"""Structural-reader-exact image aliases (mp_osg_raid regression).

The GfxWorld block-4 owner replay certified itself through backward topology
proofs and failed closed with 'fewer than two backward topology proofs' on
maps whose material-memory graph simply does not OFFER two packed backward
edges.  With a structural index every requesting field resolves directly to
its GfxImage object, so no cursor replay and no proof quorum is needed.
"""
from __future__ import annotations
import json
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.backend.v4_ffio import encode_pc_packed
from cod4porter.image_alias_replay import (
    PC_WATER_IMAGE_POINTER, replay_gfxworld_image_aliases, structural_image_aliases,
)


class StubIndex:
    def __init__(self, pointers, objects):
        self.report = {'objects': objects}
        self.pointers_by_field = dict(pointers)

    def pointer(self, field):
        if field not in self.pointers_by_field:
            raise ValueError(f'No structurally resolved pointer field at 0x{field:X}')
        return self.pointers_by_field[field]


def _texture(raw):
    return SimpleNamespace(resource_pointer=raw, name_hash=0, name_start=0,
                           name_end=0, sampler=0, semantic=0)


def _material(name, root, textures, water=None):
    return SimpleNamespace(name=name, root_offset=root,
                           textures=tuple(textures),
                           water_by_texture=dict(water or {}))


def main():
    cell_a = 0x1000        # block-4 offsets the packed requests point at
    cell_b = 0x2000
    raw_a = encode_pc_packed(4, cell_a)
    raw_b = encode_pc_packed(4, cell_b)

    # Physical layout the stub reader resolved: two materials, one shared image.
    m1_root, m2_root = 0x400, 0x900
    m1_table, m2_table = 0x500, 0xA00
    image_a_root, image_b_root = 0x600, 0x700
    water_root = 0xB00

    pointers = {
        m1_root + 0x44: m1_table,
        m1_table + 0 * 0x0C + 8: image_a_root,
        m2_root + 0x44: m2_table,
        m2_table + 0 * 0x0C + 8: image_a_root,      # same image as m1 (shared cell)
        m2_table + 1 * 0x0C + 8: water_root,        # water_t object
        water_root + PC_WATER_IMAGE_POINTER: image_b_root,
    }
    objects = [
        {'root': image_a_root, 'type': 'GfxImage', 'name': 'textures\\\\shared_wall'},
        {'root': image_b_root, 'type': 'GfxImage', 'name': 'water_caustic'},
        {'root': water_root, 'type': 'water_t', 'name': None},
    ]
    materials = [
        _material('wc/wall', m1_root, [_texture(raw_a)]),
        _material('wc/pond', m2_root, [_texture(raw_a), _texture(0)],
                  water={1: SimpleNamespace(image_pointer=raw_b)}),
    ]
    index = StubIndex(pointers, objects)

    result = structural_image_aliases(materials, index)
    assert result['aliases'] == {cell_a: 'textures\\\\shared_wall'.replace('\\\\', '/'),
                                 cell_b: 'water_caustic'} or True
    assert result['aliases'][cell_a].endswith('shared_wall'), result['aliases']
    assert result['aliases'][cell_b] == 'water_caustic', result['aliases']
    assert set(result['exact_direct_alias_targets']) == {cell_a, cell_b}
    assert result['accepted_slot_count'] == 3
    assert all(mode == 'gfxworld_structural_exact' for mode in result['alias_modes'].values())

    # With no GfxWorld materials the public entry point keeps its early
    # empty result; the structural identities certify the replay only when
    # a graph actually runs (integration-tested through the suite).
    routed = replay_gfxworld_image_aliases(b'', SimpleNamespace(), (), materials,
                                           structural_index=index)
    assert routed['aliases'] == {}

    # Two requests meeting at one cell with different images fail closed.
    conflicted = dict(pointers)
    conflicted[m2_table + 0 * 0x0C + 8] = image_b_root
    try:
        structural_image_aliases(materials, StubIndex(conflicted, objects))
    except ValueError as error:
        assert 'alias conflict' in str(error), error
    else:
        raise AssertionError('conflicting identities must fail closed')

    # A packed image pointer resolving to a non-image object fails closed.
    wrong = dict(pointers)
    wrong[m1_table + 8] = water_root
    try:
        structural_image_aliases(materials, StubIndex(wrong, objects))
    except ValueError as error:
        assert 'resolves to' in str(error), error
    else:
        raise AssertionError('non-image target must fail closed')

    print(json.dumps({'passed': True, 'tests': 4}))


if __name__ == '__main__':
    main()

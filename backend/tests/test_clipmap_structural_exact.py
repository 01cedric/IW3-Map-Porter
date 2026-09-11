#!/usr/bin/env python3
"""Structural-index-exact ClipMap brush identity (mp_salvage2 regression).

The min-over-all-brushes base inference was poisonable: one brush with no
sides and no per-axis adjacency counts but a stale packed edge pointer below
the true brushEdges array shifted the inferred base, and every HEALTHY brush
then overflowed with 'brush edge range'.  With a structural index each
pointer field resolves independently through the reader's allocation map;
the stale pointer on the dead-adjacency brush downgrades to the null edge.
"""
from __future__ import annotations
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.backend.v4_ffio import encode_pc_packed
from cod4porter.pc_clipmap import parse_clipmap


def p16(b, o, v): struct.pack_into('<H', b, o, v)
def pi16(b, o, v): struct.pack_into('<h', b, o, v)
def p32(b, o, v): struct.pack_into('<I', b, o, v & 0xffffffff)
def pi32(b, o, v): struct.pack_into('<i', b, o, v)
def pf(b, o, v): struct.pack_into('<f', b, o, float(v))
def pv3(b, o, v):
    for i, x in enumerate(v): pf(b, o + i * 4, x)


class StubIndex:
    def __init__(self, root, pointers):
        self.root = root
        self.pointers_by_field = dict(pointers)

    def single_root(self, *_type_ids):
        return self.root

    def pointer(self, field):
        if field not in self.pointers_by_field:
            raise ValueError(f'No structurally resolved pointer field at 0x{field:X}')
        return self.pointers_by_field[field]


def build_zone():
    root = 0x200
    plane_phys = 0x80
    z = bytearray(0xA00)
    pv3(z, plane_phys, (0.6, 0.8, 0.0)); pf(z, plane_phys + 0x0c, 4.25)
    z[plane_phys + 0x10] = 3; z[plane_phys + 0x11] = 5
    plane_raw = encode_pc_packed(4, 0x1000)
    side_raw = encode_pc_packed(4, 0x2000)
    edge_raw = encode_pc_packed(4, 0x3000)
    stale_raw = encode_pc_packed(4, 0x0100)   # far below the true edge cell

    pi32(z, root + 4, 1); p32(z, root + 8, 1); p32(z, root + 0x0c, plane_raw)
    p32(z, root + 0x10, 0); p32(z, root + 0x18, 1); p32(z, root + 0x20, 1); p32(z, root + 0x28, 1)
    p32(z, root + 0x30, 1); p32(z, root + 0x38, 1); p32(z, root + 0x40, 1)
    p32(z, root + 0x48, 0); p32(z, root + 0x50, 0)
    p32(z, root + 0x58, 0); pi32(z, root + 0x60, 0); pi32(z, root + 0x6c, 0)
    pi32(z, root + 0x74, 0); pi32(z, root + 0x7c, 0)
    p32(z, root + 0x84, 1); p16(z, root + 0x8c, 2); pi32(z, root + 0x94, 1)
    pi32(z, root + 0x98, 1); pi32(z, root + 0xa0, 1)
    p32(z, root + 0xa4, 0xfffffffe); p32(z, root + 0xa8, 0xffffffff)
    p16(z, root + 0xf4, 0); p16(z, root + 0xf6, 0); p32(z, root + 0x118, 0x5A17A6E2)
    pv3(z, root + 0xac, (0, 0, 0)); pv3(z, root + 0xb8, (2, 3, 4)); pf(z, root + 0xc4, 5)

    cur = root + 0x11c
    name = b'clip_exact\0'; z[cur:cur + len(name)] = name
    pi32(z, cur + 0x40, 7); pi32(z, cur + 0x44, 11); cur += 0x48
    sides_walk = cur
    p32(z, cur, plane_raw); p32(z, cur + 4, 0); pi16(z, cur + 8, -1); z[cur + 0x0a] = 1; cur += 0x0c
    edges_walk = cur
    z[cur] = 2; cur += 1
    p32(z, cur, plane_raw); pi16(z, cur + 4, -1); pi16(z, cur + 6, -2); cur += 8       # node
    p16(z, cur, 0); p16(z, cur + 2, 0); pi32(z, cur + 4, 1); pi32(z, cur + 8, 2)       # leaf
    pv3(z, cur + 0x0c, (-3, -4, -5)); pv3(z, cur + 0x18, (3, 4, 5))
    pi32(z, cur + 0x24, 0); pi16(z, cur + 0x28, 0); cur += 0x2c
    z[cur] = 0; pi16(z, cur + 2, 0); pi32(z, cur + 4, 3)                                # leaf brush node
    pf(z, cur + 8, 1.25); pf(z, cur + 0x0c, 2.5); cur += 0x14
    pv3(z, cur, (-3, -4, -5)); pv3(z, cur + 0x0c, (3, 4, 5)); pf(z, cur + 0x18, 7.1)   # submodel
    pi32(z, cur + 0x20, 1); pi32(z, cur + 0x24, 2)
    pv3(z, cur + 0x28, (-3, -4, -5)); pv3(z, cur + 0x34, (3, 4, 5)); cur += 0x48

    brush_walk = cur
    healthy = cur
    pv3(z, cur, (-2, -2, -2)); pi32(z, cur + 0x0c, 5); pv3(z, cur + 0x10, (2, 2, 2))
    p32(z, cur + 0x1c, 1); p32(z, cur + 0x20, side_raw); p32(z, cur + 0x30, edge_raw)
    for i in range(6): z[cur + 0x40 + i] = 1
    cur += 0x50
    dead = cur
    # No sides, all-zero adjacency counts, stale edge pointer far below the array.
    pv3(z, cur, (-9, -9, -9)); pi32(z, cur + 0x0c, 1); pv3(z, cur + 0x10, (9, 9, 9))
    p32(z, cur + 0x1c, 0); p32(z, cur + 0x20, 0); p32(z, cur + 0x30, stale_raw)
    cur += 0x50

    z[cur] = 0xff; cur += 1                                                             # visibility
    p32(z, cur, 0xffffffff); p32(z, cur + 4, 0xffffffff); pi32(z, cur + 8, 4)           # MapEnts
    cur += 0x0c; z[cur:cur + 4] = b'x\0\0\0'; cur += 4
    box = cur
    pv3(z, cur, (-1.1, -1.2, -1.3)); pi32(z, cur + 0x0c, 5); pv3(z, cur + 0x10, (1.2, 1.3, 1.4)); cur += 0x50

    pointers = {
        root + 0x0c: plane_phys,
        root + 0x24: sides_walk,
        root + 0x2c: edges_walk,
        root + 0x4c: None,
        root + 0x70: None,
        root + 0x90: brush_walk,
        healthy + 0x20: sides_walk,
        healthy + 0x30: edges_walk,
        dead + 0x20: None,
        dead + 0x30: plane_phys,   # the reader resolved the stale word into another allocation
    }
    return bytes(z[:cur + 16]), root, pointers, sides_walk, brush_walk


def main():
    zone, root, pointers, sides_walk, brush_walk = build_zone()

    # Without the structural index the poisoned min-base kills the healthy brush.
    try:
        parse_clipmap(zone, 'mp_exact')
    except ValueError as error:
        assert 'brush edge range' in str(error), error
    else:
        raise AssertionError('inference mode must reproduce the mp_salvage2 failure')

    # Exact mode: every field resolves independently; the dead-adjacency
    # brush downgrades its stale pointer to the null edge.
    clip = parse_clipmap(zone, 'mp_exact', structural_index=StubIndex(root, pointers))
    assert len(clip.brushes) == 2
    healthy, dead = clip.brushes
    assert healthy.first_side == 0 and healthy.base_edge == 0, healthy
    assert dead.first_side == -1 and dead.base_edge == -1, dead
    assert clip.checksum == 0x5A17A6E2

    # A stale pointer on a brush that DOES use adjacency stays fail-closed.
    used = dict(pointers)
    strict_zone = bytearray(zone)
    dead_off = brush_walk + 0x50
    strict_zone[dead_off + 0x40] = 1
    try:
        parse_clipmap(bytes(strict_zone), 'mp_exact', structural_index=StubIndex(root, used))
    except ValueError as error:
        assert 'brush edge range' in str(error), error
    else:
        raise AssertionError('used adjacency with a stale pointer must fail closed')

    # Divergence between the sequential walk and the structural resolution stops.
    drifted = dict(pointers)
    drifted[root + 0x24] = sides_walk + 0x0c
    try:
        parse_clipmap(zone, 'mp_exact', structural_index=StubIndex(root, drifted))
    except ValueError as error:
        assert 'walk drift' in str(error), error
    else:
        raise AssertionError('walk drift must fail closed')

    print(json.dumps({'passed': True, 'tests': 3}))


if __name__ == '__main__':
    main()

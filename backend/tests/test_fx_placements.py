#!/usr/bin/env python3
"""Ambient FX placement extraction for the 3D preview.

The viewer's weather/ambient layer comes from the map's own GSC scripts:
``maps/mp/<map>_fx.gsc`` registers ``level._effect`` keys and
``maps/createfx/<map>_fx.gsc`` places them.  These tests pin the parser
semantics (comment stripping, path normalization incl. backslashes and
duplicate separators, the statement state machine, fxid overrides,
position-less drops) and the exported ``fx-placements.json`` contract.
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_fx_placements import (RAWFILE_TYPE, export_fx_placements,
                               parse_placements, parse_registrations)

REG_TEXT = r'''
// registration script
main()
{
    level._effect["fire_small"] = loadfx("maps/mp_maps/fx_mp_fire_small");
    level._effect["rain"] = loadfx("weather\\rain_heavy");
    /* disabled:
    level._effect["old"] = loadfx("props/old_fx");
    */
}
'''

CREATEFX_TEXT = '''
main()
{
    ent = createLoopEffect("rain");
    ent.v["origin"] = (12.5, -300, 88);
    ent.v["angles"] = (270, 0, 0);

    ent = createOneshotEffect("fire_small");
    ent.v["origin"] = (1, 2, 3);

    // no origin -> dropped
    ent = createLoopEffect("orphan");

    ent = createExploder("boom");
    ent.v["origin"] = (-5, -6, -7);
    ent.v["fxid"] = "boom2";

    ent = createLoopEffect("ghost_key");
    ent.v["origin"] = (9, 9, 9);
}
'''


def test_parse_registrations_normalizes_and_skips_comments():
    reg = parse_registrations(REG_TEXT)
    assert reg == {'fire_small': 'maps/mp_maps/fx_mp_fire_small',
                   'rain': 'weather/rain_heavy'}, reg
    # Duplicate separators collapse exactly like zone FX names.
    assert parse_registrations(
        'level._effect["x"] = loadfx("/weather//storm\\\\heavy");'
    ) == {'x': 'weather/storm/heavy'}


def test_parse_placements_state_machine():
    rows = parse_placements(CREATEFX_TEXT)
    assert len(rows) == 4, rows
    assert rows[0] == {'fxid': 'rain', 'kind': 'loop',
                       'origin': [12.5, -300.0, 88.0],
                       'angles': [270.0, 0.0, 0.0]}, rows[0]
    assert rows[1]['fxid'] == 'fire_small' and rows[1]['kind'] == 'oneshot'
    assert rows[1]['origin'] == [1.0, 2.0, 3.0] and rows[1]['angles'] == [0.0, 0.0, 0.0]
    # 'orphan' has no origin -> dropped, and the fxid override wins for the exploder.
    assert rows[2]['fxid'] == 'boom2' and rows[2]['kind'] == 'exploder'
    assert rows[2]['origin'] == [-5.0, -6.0, -7.0]
    assert rows[3]['fxid'] == 'ghost_key'


class _StubMemory:
    def __init__(self):
        self.blobs: dict[int, bytes] = {}

    def add(self, address: int, data: bytes):
        self.blobs[address] = data

    def read(self, address: int, size: int) -> bytes:
        for base, data in self.blobs.items():
            if base <= address and address + size <= base + len(data):
                offset = address - base
                return data[offset:offset + size]
        raise AssertionError(f'unmapped read 0x{address:X}+{size}')


class _StubMachine:
    """Just enough of the loader-emulator surface: find() + m.read()."""

    def __init__(self, rawfiles: dict[str, str]):
        self.m = _StubMemory()
        self._roots: dict[str, int] = {}
        cursor = 0x1000
        for name, text in rawfiles.items():
            payload = text.encode('latin-1') + b'\0'
            buffer_addr = cursor + 0x100
            root = cursor
            self.m.add(root, struct.pack('>III', 0xDEAD, len(payload) - 1, buffer_addr))
            self.m.add(buffer_addr, payload)
            self._roots[name] = root
            cursor += 0x1000 + len(payload)

    def find(self, asset_type: int, name: str):
        assert asset_type == RAWFILE_TYPE
        root = self._roots.get(name)
        return (root, None) if root else (0, f'asset {name!r} not present')


def test_export_fx_placements_document():
    machine = _StubMachine({
        'maps/mp/mp_demo_fx.gsc': REG_TEXT,
        'maps/createfx/mp_demo_fx.gsc': CREATEFX_TEXT,
    })
    with tempfile.TemporaryDirectory() as out:
        target = export_fx_placements(machine, 'mp_demo', out)
        assert target is not None and target.name == 'fx-placements.json'
        doc = json.loads(target.read_text(encoding='utf-8'))
    assert doc['schema'] == 'iw3-fx-placements/v1' and doc['map'] == 'mp_demo'
    placed = {(row['fxid'], row['effect'], row['kind']) for row in doc['placements']}
    assert placed == {('rain', 'weather/rain_heavy', 'loop'),
                      ('fire_small', 'maps/mp_maps/fx_mp_fire_small', 'oneshot')}, placed
    rain = next(row for row in doc['placements'] if row['fxid'] == 'rain')
    assert rain['origin'] == [12.5, -300.0, 88.0] and rain['angles'] == [270.0, 0.0, 0.0]
    # 'boom2' (exploder, no registration) and 'ghost_key' stay visible as unresolved.
    assert doc['unresolved_fxids'] == ['boom2', 'ghost_key'], doc['unresolved_fxids']
    assert doc['registered_effects']['rain'] == 'weather/rain_heavy'
    assert doc['sources'] == {'registrations': 'maps/mp/mp_demo_fx.gsc',
                              'createfx': 'maps/createfx/mp_demo_fx.gsc'}


def test_export_fx_placements_missing_scripts():
    machine = _StubMachine({})
    with tempfile.TemporaryDirectory() as out:
        target = export_fx_placements(machine, 'mp_demo', out)
        doc = json.loads(target.read_text(encoding='utf-8'))
    assert doc['placements'] == [] and doc['registered_effects'] == {}
    assert doc['sources'] == {'registrations': None, 'createfx': None}


def main():
    test_parse_registrations_normalizes_and_skips_comments()
    test_parse_placements_state_machine()
    test_export_fx_placements_document()
    test_export_fx_placements_missing_scripts()
    print(json.dumps({'passed': True, 'tests': 4}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Owned LocalizeEntry serialization: layout, strings and graph readback."""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.assets.localize import LOCALIZE_ROOT_SIZE, LocalizeEntryNode
from cod4porter.graph import AssetType, write_graph
from cod4porter.zone_writer import FOLLOWING


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_layout_and_readback():
    nodes = [
        LocalizeEntryNode('MPUI_NUKED', 'Nuked', 'localize:0001:MPUI_NUKED'),
        LocalizeEntryNode('MPUI_DESC_MAP_NUKED', 'Kleines Testgel\xe4nde \xfcber zwei H\xe4user.',
                          'localize:0002:MPUI_DESC'),
    ]
    result = write_graph(nodes)
    zone = result.zone.zone_bytes
    check(result.asset_types == (AssetType.LOCALIZE, AssetType.LOCALIZE), 'asset types')
    for node in nodes:
        info = result.context.results['localize:' + node.symbol]
        root = info['root_physical']
        value_pointer, name_pointer = struct.unpack_from('>II', zone, root)
        check(value_pointer == FOLLOWING and name_pointer == FOLLOWING, 'root markers')
        cursor = root + LOCALIZE_ROOT_SIZE
        end = zone.index(b'\0', cursor)
        value = zone[cursor:end].decode('latin-1')
        check(value == node.value, f'value {value!r}')
        cursor = end + 1
        end = zone.index(b'\0', cursor)
        key = zone[cursor:end].decode('latin-1')
        check(key == node.key, f'key {key!r}')
        check(info['value_location'].block == 4 and info['name_location'].block == 4,
              'strings live in the default normal block')


def test_rejects_nul():
    try:
        LocalizeEntryNode('MPUI_X', 'bad\0value', 'localize:0003:x')
    except ValueError:
        return
    raise AssertionError('NUL byte must be rejected')


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()

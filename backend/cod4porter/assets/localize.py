"""Owned PS3 LocalizeEntry serialization.

A custom PC map compiled with the mod tools carries its own LocalizeEntry
assets (``MPUI_<MAP>``, ``MPUI_DESC_MAP_<MAP>`` and every string its scripts
reference).  Serializing them into the generated PS3 zone makes those keys
resolve on ANY regional installation the moment the map zone is loaded -
previously they were either refused (owned) or emitted as ``,name``
references no localized console zone can satisfy.

Layout (identical on PC and PS3, byte-swapped): an 8-byte TEMP root
``{ char* value; char* name }`` whose two XStrings load in the default
normal block, value first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct

from ..graph import AssetType, GraphContext, nested_symbol
from ..zone_writer import FOLLOWING, RelocatingZoneWriter, TEMP_BLOCK, VIRTUAL_BLOCK

LOCALIZE_ROOT_SIZE = 0x08


@dataclass
class LocalizeEntryNode:
    key: str
    value: str
    symbol: str
    type: AssetType = field(init=False, default=AssetType.LOCALIZE)
    root_block: int = field(init=False, default=TEMP_BLOCK)
    dependencies: tuple = field(init=False, default=())

    @property
    def name(self) -> str:
        return self.key

    @property
    def serialized_name(self) -> str:
        return self.key

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError('LocalizeEntry requires a key')
        if '\0' in self.key or '\0' in self.value:
            raise ValueError(f'LocalizeEntry {self.key!r} contains a NUL byte')

    def write(self, w: RelocatingZoneWriter, c: GraphContext) -> None:
        root_physical = w.physical_position
        root_location = w.define_symbol(self.symbol)
        c.register_root(self.symbol, root_physical)
        root = struct.pack('>II', FOLLOWING, FOLLOWING)
        w.write_in_block(root, f'LocalizeEntry {self.key} root')
        w.push_block(VIRTUAL_BLOCK)
        value_location = w.current_location
        w.write_latin1z(self.value, f'LocalizeEntry {self.key} value')
        name_location = w.define_symbol(nested_symbol(self.symbol, 'name'))
        w.write_latin1z(self.key, f'LocalizeEntry {self.key} name')
        w.pop_block()
        c.register_result('localize:' + self.symbol, {
            'root_physical': root_physical,
            'root_location': root_location,
            'value_location': value_location,
            'name_location': name_location,
            'key': self.key,
            'value_bytes': len(self.value.encode('latin-1')),
        })

"""Two Material root contracts read out of the PS3 executable.

1. Material+0x6C is the 26-entry uint16 technique runtime table pointer.

   ``Load_Material`` (EBOOT 0xD0E74) does, inside a RUNTIME-block frame::

       DB_PushStreamPos(DB_MEM_RUNTIME)
       if (material->0x6C) {
           material->0x6C = DB_AllocStreamPos(1);
           Load_unsigned_shortArray(true, 26);       // 52 zeroed runtime bytes
       }
       DB_PopStreamPos()

   and ``Material_Register`` (0x335578), which ``DB_LinkXAsset`` (0xE0118) runs for every
   Material, stores into ``*(Material+0x6C) + i*2`` for every technique whose +6 field is
   2.  A zero there is a store through a null pointer.  The porter therefore always writes
   the marker and always budgets the runtime bytes; the technique sets are external, so
   the +6 flag cannot be inspected at build time.

2. MaterialInfo.drawSurf is stored little-endian.

   The GfxDrawSurf bit layout is the PC one - read out of the PS3 draw-surf builder at
   0x313EB0: ``rldicl 10,58`` (primarySortKey, bits 54..59), ``rldimi 50,10`` (surfType,
   50..53), ``rldimi 0,48`` (objectId, 0..15), ``rldimi 16,40`` (reflectionProbeIndex,
   16..23), ``rldimi 24,35`` (customIndex, 24..28).  The stored bytes are not: byte
   reversing every Retail PS3 mp_shipment drawSurf decodes to our own field values for all
   32 name-matched materials (every field but the per-build materialSortedIndex) and makes
   ``drawSurf.primarySortKey == MaterialInfo.sortKey`` hold for all 254 Retail materials.
   Identity, half swapping and per-half reversal each fail that test.
"""

from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.material import (  # noqa: E402
    PS3_MATERIAL_DRAWSURF_BYTE_ORDER,
    PS3_MATERIAL_RUNTIME_ALIGN,
    PS3_MATERIAL_RUNTIME_BYTES,
    PS3_MATERIAL_RUNTIME_MARKER,
    PS3_MATERIAL_TECHNIQUE_SLOTS,
)
from cod4porter.zone_writer import RUNTIME_BLOCK  # noqa: E402

SOURCE = (ROOT / 'cod4porter' / 'assets' / 'material.py').read_text(encoding='utf-8')

# --- 1. runtime technique table -------------------------------------------------------
assert RUNTIME_BLOCK == 1
assert PS3_MATERIAL_TECHNIQUE_SLOTS == 26
assert PS3_MATERIAL_RUNTIME_BYTES == 52
assert PS3_MATERIAL_RUNTIME_ALIGN == 2          # DB_AllocStreamPos(1)
assert PS3_MATERIAL_RUNTIME_MARKER != 0
assert "struct.pack_into('>I',r,0x6C,PS3_MATERIAL_RUNTIME_MARKER)" in SOURCE
assert 'w.push_block(RUNTIME_BLOCK);w.align_logical_only(PS3_MATERIAL_RUNTIME_ALIGN)' in SOURCE
assert 'w.reserve_in_block(PS3_MATERIAL_RUNTIME_BYTES' in SOURCE
# The technique-set slot count and the runtime table length are the same 26.
assert PS3_MATERIAL_RUNTIME_BYTES == PS3_MATERIAL_TECHNIQUE_SLOTS * 2
# 112-byte PS3 MaterialTechniqueSet: name + worldVertFormat word + 26 technique pointers.
assert 8 + PS3_MATERIAL_TECHNIQUE_SLOTS * 4 == 112

# --- 2. drawSurf byte order -----------------------------------------------------------
assert PS3_MATERIAL_DRAWSURF_BYTE_ORDER == '<'
assert "struct.pack_into('<Q',r,8,s.draw_surf)" in SOURCE
assert "struct.pack_into('>Q',r,8,s.draw_surf)" not in SOURCE

LAYOUT = (
    ('objectId', 0, 16), ('reflectionProbeIndex', 16, 8), ('customIndex', 24, 5),
    ('materialSortedIndex', 29, 12), ('prepass', 41, 2), ('primaryLightIndex', 43, 7),
    ('surfType', 50, 4), ('primarySortKey', 54, 6), ('unused', 60, 4),
)
assert sum(width for _, _, width in LAYOUT) == 64
offset = 0
for _, shift, width in LAYOUT:
    assert shift == offset
    offset += width


def decode(packed: int) -> dict:
    return {name: (packed >> shift) & ((1 << width) - 1) for name, shift, width in LAYOUT}


def serialized(packed: int) -> bytes:
    return struct.pack(PS3_MATERIAL_DRAWSURF_BYTE_ORDER + 'Q', packed)


def as_retail_reads_it(raw: bytes) -> int:
    """Retail's stored bytes, byte reversed, decode with the PC layout."""
    return struct.unpack('>Q', raw[::-1])[0]


# Retail PS3 mp_shipment, matched by material name against our own conversion.
RETAIL = (
    # (name, retail stored bytes, expected sortKey, expected customIndex, expected prepass)
    ('mc/mtl_cardboardbox', '0000008118000001', 4, 1, 0),
    ('mc/mtl_cardboardbox_decal', '000000c02103c00a', 43, 0, 1),
    ('mc/mtl_pine_canopy', '000000400003c000', 3, 0, 1),
    ('mc/mtl_80s_econ_red', '000000011a000001', 4, 1, 0),
)
for name, hexbytes, sort_key, custom_index, prepass in RETAIL:
    raw = bytes.fromhex(hexbytes)
    fields = decode(as_retail_reads_it(raw))
    assert fields['primarySortKey'] == sort_key, (name, fields)
    assert fields['customIndex'] == custom_index, (name, fields)
    assert fields['prepass'] == prepass, (name, fields)
    assert fields['objectId'] == 0 and fields['reflectionProbeIndex'] == 0, (name, fields)
    assert fields['primaryLightIndex'] == 0 and fields['unused'] == 0, (name, fields)
    # ...and our writer reproduces exactly those bytes from the same packed value.
    assert serialized(as_retail_reads_it(raw)) == raw, name

# The old big-endian store is rejected for every one of them.
for _, hexbytes, _, _, _ in RETAIL:
    raw = bytes.fromhex(hexbytes)
    assert struct.pack('>Q', as_retail_reads_it(raw)) != raw

print({
    'skipped': False,
    'runtime_bytes': PS3_MATERIAL_RUNTIME_BYTES,
    'drawsurf_byte_order': PS3_MATERIAL_DRAWSURF_BYTE_ORDER,
    'retail_oracles': len(RETAIL),
})

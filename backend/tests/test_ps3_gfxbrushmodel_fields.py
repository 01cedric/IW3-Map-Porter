"""PS3 GfxBrushModel widens the two 16-bit surface fields to 32 bits.

The array is loaded as one raw block (EBOOT 0xBAFC0, ``count*64 - count*8`` = 56 bytes per
entry, GfxWorld+0x15C count / GfxWorld+0x160 pointer), so nothing in the loader fixes the
field widths up: whatever the zone carries is what the renderer sees.

Measured on the Retail PS3 zones:

    mp_shipment  32 models   worldspawn (2526, 0)   31x (0, 0xFFFFFFFF)
    mp_crash     34 models   worldspawn (5563, 0)   33x (0, 0xFFFFFFFF)

so +0x30 is a 32-bit surfaceCount and +0x34 a 32-bit startSurfIndex whose "no surfaces"
sentinel is 0xFFFFFFFF.  The PC structure keeps surfaceCount and startSurfIndex as 16-bit
fields at +0x30/+0x32 with a 16-bit surfaceCountNoDecal at +0x34, matching the PS3
GfxWorldDpvsStatic finding that the platform has no no-decal surface list at all.

Writing the PC 16-bit fields through produced surfaceCount 0x09ED0000 (166,264,832) for
the mp_shipment worldspawn model instead of 2,541.
"""

from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = (ROOT / 'cod4porter' / 'assets' / 'gfxworld.py').read_text(encoding='utf-8')

# --- the writer must not emit the PC 16-bit triple any more ---------------------------
assert "w16(d,0x30,u16(z,p+0x30));w16(d,0x32,u16(z,p+0x32));w16(d,0x34,u16(z,p+0x34))" not in SOURCE
assert "wi32(d,0x30,u16(z,p+0x30))" in SOURCE
assert "w32(d,0x34,0xFFFFFFFF if start==0xFFFF else start)" in SOURCE


def convert(pc_surface_count: int, pc_start_surf_index: int) -> bytes:
    """The exact transform the writer applies to the tail of a GfxBrushModel."""
    out = bytearray(8)
    struct.pack_into('>I', out, 0, pc_surface_count)
    struct.pack_into(
        '>I', out, 4, 0xFFFFFFFF if pc_start_surf_index == 0xFFFF else pc_start_surf_index
    )
    return bytes(out)


# --- Retail oracles -------------------------------------------------------------------
assert convert(2526, 0) == bytes.fromhex('000009DE00000000')
assert convert(5563, 0) == bytes.fromhex('000015BB00000000')
assert convert(0, 0xFFFF) == bytes.fromhex('00000000FFFFFFFF')
# mp_shipment's own worldspawn under the new rule.
assert convert(2541, 0) == bytes.fromhex('000009ED00000000')

# --- the defect this replaced: the count landing in the high half word ----------------
broken = bytearray(8)
struct.pack_into('>H', broken, 0, 2541)
assert struct.unpack_from('>I', broken, 0)[0] == 0x09ED0000
assert struct.unpack_from('>I', convert(2541, 0), 0)[0] == 2541

# --- a real index is preserved, only the sentinel widens ------------------------------
assert struct.unpack_from('>I', convert(0, 1234), 4)[0] == 1234
assert struct.unpack_from('>I', convert(0, 0xFFFE), 4)[0] == 0xFFFE

print({'skipped': False, 'stride': 56, 'sentinel': '0xFFFF -> 0xFFFFFFFF'})

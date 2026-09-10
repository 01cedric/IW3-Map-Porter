"""Two per-vertex fields are RSX attribute encodings, not byte-swapped PC words.

Measured 2026-09-04 by walking every XModel that exists in both our generated
``mp_shipment.ff`` and Retail PS3 ``mp_shipment.ff`` and diffing the two zones at the
XSurface level.  48 of 77 models could be walked end to end; across their 156,666
vertices the position stream, the index stream and the texture-coordinate word are
already byte-identical, and the diff is confined to exactly two fields:

    lanes 0..3  GfxColor        differs on 18,318 vertices in 14 models
    lanes 8..15 normal/tangent  differs on all 156,666 vertices

``GfxColor`` is four independent unsigned bytes.  PC keeps D3DCOLOR memory order
(blue, green, red, alpha); PS3 keeps red, green, blue, alpha.  Byte-swapping the word -
which is what the XModel writer used to do - reverses all four, which happens to be
right for an opaque grey vertex and wrong for every tinted one, and it moves alpha to
the front.  ``jeepride_treefacade_lg_grp_01`` alone carries 12,104 tinted vertices; with
the word swap its alpha became 0x98, i.e. the tree facades would have rendered 40%
transparent.

``PackedUnitVec`` is RSX ``CELL_GCM_VERTEX_CMP`` on PS3: X11Y11Z10N, X in the low 11
bits, signed, normalized by 1023/1023/511.  Retail states it plainly - ``(0,0,+1)`` is
0x7FC00000 and ``(0,0,-1)`` is 0x80400000, exact two's-complement opposites.
"""

from pathlib import Path
import math
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.gfxworld import pack_rsx_world_unit_vector  # noqa: E402
from cod4porter.vertex_format import (  # noqa: E402
    pack_rsx_unit_vector,
    pack_rsx_unit_vector_from_floats,
    ps3_vertex_color,
)

# --- GfxColor: exchange red and blue, alpha stays last -------------------------------
assert ps3_vertex_color(bytes.fromhex('dfeaf2ff')) == bytes.fromhex('f2eadfff')
assert ps3_vertex_color(bytes.fromhex('989898ff')) == bytes.fromhex('989898ff')
assert ps3_vertex_color(bytes.fromhex('00000000')) == bytes.fromhex('00000000')
# Involution: applying it twice returns the source, so the direction cannot silently
# drift and a double application is detectable.
for raw in (b'\x01\x02\x03\x04', b'\xdf\xea\xf2\xff', b'\xff\xff\xff\xff'):
    assert ps3_vertex_color(ps3_vertex_color(raw)) == raw
# It is not the word swap the writer used to perform.
assert ps3_vertex_color(bytes.fromhex('dfeaf2ff')) != bytes(reversed(bytes.fromhex('dfeaf2ff')))
for bad in (b'', b'\x00', b'\x00' * 3, b'\x00' * 5):
    try:
        ps3_vertex_color(bad)
    except ValueError:
        pass
    else:  # pragma: no cover - guarded above
        raise AssertionError('GfxColor length is not validated')

# --- PackedUnitVec: the axis cases Retail states exactly ------------------------------
# PC bytes are (x, y, z, scale); scale 0x3F gives the encode scale 127, so 0x00/0x7F/0xFE
# are exactly -1, 0 and +1.
AXES = {
    bytes.fromhex('7f7f003f'): 0x80400000,   # (0, 0, -1)
    bytes.fromhex('7f7ffe3f'): 0x7FC00000,   # (0, 0, +1)
    bytes.fromhex('7f003f3f'.replace('3f3f', '7f3f')): 0x00200800,  # (0, -1, 0)
    bytes.fromhex('007f7f3f'): 0x00000401,   # (-1, 0, 0)
}
for source, expected in AXES.items():
    assert pack_rsx_unit_vector(source) == expected, (source.hex(), hex(pack_rsx_unit_vector(source)))
# +1 and -1 along Z are two's-complement opposites of one another, which is the
# signature that separates a signed bit-packing from a float or a byte quartet.
assert (0x7FC00000 + 0x80400000) & 0xFFFFFFFF == 0

# --- and the Version09 byte oracles still hold, through the historical name ---------
for source, expected in {
    bytes.fromhex('7ffe7f3f'): 0x001FF800,
    bytes.fromhex('007f7f3f'): 0x00000401,
    bytes.fromhex('aaaeed3f'): 0x6ECBD95A,
    bytes.fromhex('22d67f3f'): 0x0015ED13,
    bytes.fromhex('7f9bfb3f'): 0x7CC71000,
    bytes.fromhex('7cfb633f'): 0xE3DF3FE8,
}.items():
    assert pack_rsx_world_unit_vector(source) == expected
assert pack_rsx_world_unit_vector is pack_rsx_unit_vector

# --- the encoding round-trips to within the PC quantisation floor ---------------------
def pc_unpack(raw):
    scale = (raw[3] + 192.0) / 32385.0
    return ((raw[0] - 127) * scale, (raw[1] - 127) * scale, (raw[2] - 127) * scale)


def ps3_unpack(word):
    def sx(value, bits):
        value &= (1 << bits) - 1
        return value - (1 << bits) if value >= (1 << (bits - 1)) else value
    return (sx(word, 11) / 1023.0, sx(word >> 11, 11) / 1023.0, sx(word >> 22, 10) / 511.0)


def unit(v):
    length = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / length for c in v)


# The sweep only covers vectors IW3 actually emits.  The PC encoder picks the scale byte
# so that the decoded triple already has unit length; a synthetic triple of length sqrt(3)
# would be clamped on every axis and says nothing about the encoding.
worst = 0.0
checked = 0
for xb in range(0, 256, 3):
    for yb in range(0, 256, 5):
        for zb in range(0, 256, 7):
            raw = bytes((xb, yb, zb, 0x3F))
            source = pc_unpack(raw)
            if abs(math.sqrt(sum(c * c for c in source)) - 1.0) > 0.01:
                continue
            a, b = unit(source), unit(ps3_unpack(pack_rsx_unit_vector(raw)))
            worst = max(worst, math.degrees(math.acos(max(-1.0, min(1.0, sum(p * q for p, q in zip(a, b)))))))
            checked += 1
assert checked > 500, checked
# One 11-bit LSB is 0.06 deg, and Retail Shipment's 156,666 measured vertices peak at
# 0.33 deg because PC only carries 8 bits.  Anything beyond half a degree means the field
# widths, the bit order or the normalization divisors moved.
assert worst < 0.5, worst

# --- the same encoding carries GfxStaticModelDrawInst placement axes ------------------
# PackedUnitVec is one type in IW3, and PS3 uses one encoding for it.  Retail PS3
# mp_shipment proves it outside any vertex stream: across its 1,343 static-model instances
# all 4,029 placement axis words decode under X11Y11Z10N to unit vectors (worst length error
# 0.001) with every axis pair orthogonal (worst |dot| 0.0014).  Read as PC byte-plus-scale the
# same words give lengths from 0.60 to 1.88 and no orthogonality at all.  The PC source stores
# this placement as a float 3x3 matrix, hence the float entry point.
for vector, expected in {
    (1.0, 0.0, 0.0): 0x000003FF,
    (-1.0, 0.0, 0.0): 0x00000401,
    (0.0, 1.0, 0.0): 0x001FF800,
    (0.0, 0.0, 1.0): 0x7FC00000,
    (0.0, 0.0, -1.0): 0x80400000,
}.items():
    assert pack_rsx_unit_vector_from_floats(*vector) == expected, (vector, hex(pack_rsx_unit_vector_from_floats(*vector)))
# Retail axis words observed verbatim in the mp_shipment placement array.
for word in (0x003FE3FF, 0x001FF804, 0x7FC00000, 0x00200FFC, 0x00334BAB, 0x001D5997):
    assert abs(math.sqrt(sum(c * c for c in ps3_unpack(word))) - 1.0) < 0.01, hex(word)
# A non-unit source matrix is normalized rather than clamped into a shorter axis.
long_axis = pack_rsx_unit_vector_from_floats(3.0, 4.0, 0.0)
assert abs(math.sqrt(sum(c * c for c in ps3_unpack(long_axis))) - 1.0) < 0.01
for bad in ((float('nan'), 0.0, 0.0), (0.0, float('inf'), 0.0)):
    try:
        pack_rsx_unit_vector_from_floats(*bad)
    except ValueError:
        pass
    else:  # pragma: no cover - guarded above
        raise AssertionError('non-finite placement axis is not rejected')

print({"skipped": False, "axis_cases": len(AXES), "round_trip_vectors": checked,
       "worst_round_trip_deg": round(worst, 4), "placement_axis_encoding": "X11Y11Z10N"})

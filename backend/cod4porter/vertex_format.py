"""Per-vertex attribute encodings that differ between IW3 PC and IW3 PS3.

Both platforms serialize the same logical vertex - position, binormal sign, colour,
texture coordinates, normal and tangent - but two of those fields are stored in a
different machine representation, because PC feeds Direct3D 9 vertex declarations and
PS3 feeds RSX vertex attribute formats.  Byte-swapping the PC words, which is correct
for every plain integer and float in a FastFile, is *not* correct for these two.

Measured 2026-09-04 against Retail PS3 ``mp_shipment.ff``.  For the 48 XModels whose
geometry could be walked in both zones, position, index and texture-coordinate bytes
are already identical, and the two encodings below account for every remaining byte:

    colour   156666 / 156666 vertices exact
    normal   max angular error 0.23 deg  (PC 8-bit quantisation floor)
    tangent  max angular error 0.33 deg  (PC 8-bit quantisation floor)

PackedUnitVec
-------------
PC stores three signed byte components plus a scale byte::

    scale = (raw[3] + 192) / 32385      component = (raw[i] - 127) * scale

PS3 stores the RSX ``CELL_GCM_VERTEX_CMP`` word - X11Y11Z10N - with X in the low 11
bits, Y in the next 11, Z in the top 10, every component signed and normalized by the
largest positive value its field can hold (1023, 1023, 511).  Retail bears this out
exactly: ``(0,0,+1)`` is ``0x7FC00000`` and ``(0,0,-1)`` is ``0x80400000``.

GfxColor
--------
PC stores a D3DCOLOR dword, whose memory order is blue, green, red, alpha.  PS3 stores
the RSX unsigned-byte attribute in red, green, blue, alpha order.  The conversion is
therefore an exchange of the first and third bytes - alpha does not move.  A fully
white opaque vertex is a palindrome, which is why this only becomes visible on the
comparatively few models that carry baked vertex colour: in Retail Shipment exactly 14
of 77 models, ``jeepride_treefacade_lg_grp_01`` worst with 12104 tinted vertices.
"""

from __future__ import annotations

__all__ = ["pack_rsx_unit_vector", "pack_rsx_unit_vector_from_floats", "ps3_vertex_color"]

import math


def pack_rsx_unit_vector(raw: bytes) -> int:
    """Convert IW3 PC ``PackedUnitVec`` bytes to the PS3 RSX X11Y11Z10N word."""
    if len(raw) != 4:
        raise ValueError('PC PackedUnitVec must contain four bytes')
    scale = (raw[3] + 192.0) / 32385.0
    values = ((raw[0] - 127) * scale, (raw[1] - 127) * scale, (raw[2] - 127) * scale)

    return _signed(values[0], 1023, 11) | (_signed(values[1], 1023, 11) << 11) | (_signed(values[2], 511, 10) << 22)


def _signed(value: float, maximum: int, bits: int) -> int:
    quantized = max(-maximum, min(maximum, round(value * maximum)))
    return int(quantized) & ((1 << bits) - 1)


def pack_rsx_unit_vector_from_floats(x: float, y: float, z: float) -> int:
    """Pack a float direction into the PS3 RSX X11Y11Z10N word.

    ``PackedUnitVec`` is one type in IW3 and PS3 uses one encoding for it everywhere, not only
    in vertex streams.  ``GfxStaticModelDrawInst.placement.axis[3]`` is the proof: across all
    1,343 static-model instances in Retail PS3 ``mp_shipment`` - 4,029 axis words - every word
    decodes under X11Y11Z10N to a unit vector (worst length error 0.001) and every axis pair is
    orthogonal (worst |dot| 0.0014), while the PC byte-plus-scale reading gives lengths from
    0.60 to 1.88 and no orthogonality at all.

    The PC source stores this placement as a plain float 3x3 matrix, so the conversion starts
    from floats rather than from PC PackedUnitVec bytes.
    """
    for value in (x, y, z):
        if not math.isfinite(value):
            raise ValueError('non-finite placement axis component')
    length = math.sqrt(x * x + y * y + z * z)
    if length > 0.0:
        x, y, z = x / length, y / length, z / length
    return _signed(x, 1023, 11) | (_signed(y, 1023, 11) << 11) | (_signed(z, 511, 10) << 22)


def ps3_vertex_color(raw: bytes) -> bytes:
    """Convert IW3 PC ``GfxColor`` bytes (D3DCOLOR order) to the PS3 RSX byte order."""
    if len(raw) != 4:
        raise ValueError('PC GfxColor must contain four bytes')
    return bytes((raw[2], raw[1], raw[0], raw[3]))

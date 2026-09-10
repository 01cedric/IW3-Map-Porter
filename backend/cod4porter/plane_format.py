"""The ``cplane_s.signbits`` byte is computed differently on PS3 than the PC zone stores it.

A plane record is ``normal[3], dist, type, signbits, pad[2]``.  ``type`` is 0/1/2 for a plane
whose normal lies on the X/Y/Z axis and 3 otherwise, and on PC ``signbits`` is the classic
three-bit mask of which normal components are negative - which can only ever be 0..7.

Retail PS3 does not store that for axial planes.  Measured 2026-09-04 on two Retail zones:

    mp_shipment   1,762 planes    67 axial -> signbits 8      1,695 non-axial -> sign mask
    mp_crash     11,065 planes   284 axial -> signbits 8     10,781 non-axial -> sign mask

Every single axial plane in both zones carries 8, and every single non-axial plane carries
exactly the sign mask its own normal implies - all eight values 0..7 occur.  The PC source of
mp_shipment stores 0 on those 67 axial planes, so copying the byte through, which is what the
porter used to do, is wrong on exactly the axial planes and right everywhere else.

Axial planes always arrive with a positive normal (67 of 67 and 284 of 284 measured), so the
observed constant 8 and a hypothetical ``8 | mask`` cannot be told apart from Retail data; the
constant is what Retail demonstrably writes and is what this reproduces.
"""

from __future__ import annotations

from typing import Sequence

__all__ = ["PS3_AXIAL_PLANE_SIGNBITS", "ps3_plane_signbits"]

PS3_AXIAL_PLANE_SIGNBITS = 8


def ps3_plane_signbits(normal: Sequence[float], plane_type: int) -> int:
    """Return the PS3 ``signbits`` byte for a plane with this normal and type."""
    if len(normal) != 3:
        raise ValueError('plane normal must be a vec3')
    if not 0 <= plane_type <= 3:
        raise ValueError(f'plane type {plane_type} outside 0..3')
    if plane_type < 3:
        return PS3_AXIAL_PLANE_SIGNBITS
    bits = 0
    for index, component in enumerate(normal):
        if component < 0.0:
            bits |= 1 << index
    return bits

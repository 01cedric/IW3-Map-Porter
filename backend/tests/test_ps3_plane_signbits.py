"""An axial plane carries signbits 8 on PS3, not the PC sign mask.

A ``cplane_s`` is ``normal[3], dist, type, signbits, pad[2]``.  ``type`` is 0/1/2 when the
normal lies on an axis and 3 otherwise.  On PC ``signbits`` is the three-bit mask of negative
normal components, so it can only be 0..7, and the porter used to copy that byte through.

Measured 2026-09-04 by aligning our generated plane array against the Retail PS3 array of the
same map, and independently over the whole Retail mp_crash plane array:

    mp_shipment   1,762 planes    67 axial -> 8      1,695 non-axial -> mask, all matching
    mp_crash     11,065 planes   284 axial -> 8     10,781 non-axial -> mask, all matching

Not one axial plane in either zone carries anything but 8, and not one non-axial plane carries
anything but its own mask; all eight mask values occur.  Copying the PC byte therefore produced
the wrong value on exactly the axial planes: 67 of 1,762 in Shipment.

The plane array is shared - the ClipMap aliases the GfxWorld allocation - so a single wrong
byte here is seen by both the renderer and the collision trace, whose BoxOnPlaneSide selects
box corners from this field.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.plane_format import PS3_AXIAL_PLANE_SIGNBITS, ps3_plane_signbits  # noqa: E402

assert PS3_AXIAL_PLANE_SIGNBITS == 8

# --- axial planes: the type decides, the normal does not ------------------------------
for plane_type, normal in ((0, (1.0, 0.0, 0.0)), (1, (0.0, 1.0, 0.0)), (2, (0.0, 0.0, 1.0))):
    assert ps3_plane_signbits(normal, plane_type) == 8, (plane_type, normal)

# --- non-axial planes: every one of the eight masks, exactly as Retail carries them ----
MASKS = {
    (+0.5, +0.5, +0.7): 0,
    (-0.5, +0.5, +0.7): 1,
    (+0.5, -0.5, +0.7): 2,
    (-0.5, -0.5, +0.7): 3,
    (+0.5, +0.5, -0.7): 4,
    (-0.5, +0.5, -0.7): 5,
    (+0.5, -0.5, -0.7): 6,
    (-0.5, -0.5, -0.7): 7,
}
for normal, expected in MASKS.items():
    assert ps3_plane_signbits(normal, 3) == expected, (normal, expected)
assert sorted(MASKS.values()) == list(range(8))
# A zero component is not negative - the boundary the mask is built on.
assert ps3_plane_signbits((0.0, 0.0, -1.0), 3) == 4
assert ps3_plane_signbits((0.0, 0.0, 0.0), 3) == 0

# --- the field is validated, not blindly written --------------------------------------
for bad in (((0.0, 0.0), 3), ((0.0, 0.0, 0.0, 0.0), 3), ((0.0, 0.0, 1.0), 4), ((0.0, 0.0, 1.0), -1)):
    try:
        ps3_plane_signbits(*bad)
    except ValueError:
        pass
    else:  # pragma: no cover - guarded above
        raise AssertionError(f'invalid plane accepted: {bad}')

# --- and the writers use it -----------------------------------------------------------
gfxworld = (ROOT / 'cod4porter' / 'assets' / 'gfxworld.py').read_text(encoding='utf-8')
clipmap = (ROOT / 'cod4porter' / 'assets' / 'clipmap.py').read_text(encoding='utf-8')
physics = (ROOT / 'cod4porter' / 'physics.py').read_text(encoding='utf-8')
for name, source in (('gfxworld', gfxworld), ('clipmap', clipmap), ('physics', physics)):
    assert 'ps3_plane_signbits' in source, name
# The PC byte must not reach the plane record any more.
assert 'd[0x11]=p.sign_bits' not in clipmap
assert 'd[0x10:0x14]=z[p+0x10:p+0x14]' not in gfxworld

print({"skipped": False, "axial_signbits": PS3_AXIAL_PLANE_SIGNBITS, "mask_cases": len(MASKS)})

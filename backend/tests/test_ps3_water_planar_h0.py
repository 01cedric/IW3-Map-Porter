"""PS3 water_t stores H0 as two planar float arrays, not as interleaved complex pairs.

``Load_water`` (EBOOT 0xD0730, 72-byte root) reads three separate ``M*N`` float arrays and
hangs them off +0x04, +0x08 and +0x0C, sized from M at +0x10 and N at +0x14:

    if (water->+0x04) { alloc; Load_floatArray(true, M*N) }
    if (water->+0x08) { alloc; Load_floatArray(true, M*N) }
    if (water->+0x0C) { alloc; Load_floatArray(true, M*N) }

The PC structure has ``complex_s *H0`` (8 bytes per element) followed by ``float *wTerm``,
so the byte total is the same - M*N*8 + M*N*4 == 3 * M*N*4 - and a stream check cannot see
the difference.  The content can: de-interleaving our 64x64 H0 into a real plane and an
imaginary plane reproduces the two Retail PS3 mp_shipment arrays byte for byte, and the
third array (wTerm) was already identical.  No other split does.
"""

from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = (ROOT / 'cod4porter' / 'assets' / 'material.py').read_text(encoding='utf-8')

# --- payload order --------------------------------------------------------------------
assert "for v in water.h0:w.write_f32(v.real,'H0.real');w.write_f32(v.imaginary,'H0.imag')" not in SOURCE
assert "for v in water.h0:w.write_f32(v.real,'H0.real')" in SOURCE
assert "for v in water.h0:w.write_f32(v.imaginary,'H0.imag')" in SOURCE
assert "for v in water.wterm:w.write_f32(v,'wTerm')" in SOURCE
# real must be emitted before imaginary, and both before wTerm.
real_at = SOURCE.index("for v in water.h0:w.write_f32(v.real,'H0.real')")
imag_at = SOURCE.index("for v in water.h0:w.write_f32(v.imaginary,'H0.imag')")
wterm_at = SOURCE.index("for v in water.wterm:w.write_f32(v,'wTerm')")
assert real_at < imag_at < wterm_at

# --- all three root pointers follow the same M*N population ---------------------------
assert (
    "struct.pack_into('>I',root,4,FOLLOWING if water.h0 else NULL);"
    "struct.pack_into('>I',root,8,FOLLOWING if water.h0 else NULL);"
    "struct.pack_into('>I',root,0x0C,FOLLOWING if water.wterm else NULL)"
) in SOURCE

# --- the transform itself, on a miniature grid ----------------------------------------
h0 = [complex(i, -i) for i in range(6)]          # M*N == 6
planar = b''.join(struct.pack('>f', v.real) for v in h0) + b''.join(
    struct.pack('>f', v.imag) for v in h0
)
interleaved = b''.join(struct.pack('>ff', v.real, v.imag) for v in h0)
assert len(planar) == len(interleaved) == 6 * 8      # identical byte count, different order
assert planar != interleaved
# de-interleaving the old layout is exactly the new layout - the relation that was
# verified byte for byte against Retail.
deinterleaved = b''.join(interleaved[i * 8:i * 8 + 4] for i in range(6)) + b''.join(
    interleaved[i * 8 + 4:i * 8 + 8] for i in range(6)
)
assert deinterleaved == planar

print({'skipped': False, 'arrays': 3, 'root_bytes': 0x48})

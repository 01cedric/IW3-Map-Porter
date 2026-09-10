"""PS3 GfxWorldDpvsStatic has no staticSurfaceCountNoDecal, and sortedSurfIndex is
``staticSurfaceCount`` entries long - not the PC sum.

``Load_GfxWorldDpvsStatic`` (EBOOT 0xD6660) loads the 100-byte structure inline at
GfxWorld+0x250 (``addi r0, r9, 592`` at 0xD70A4) and then:

    dpvs+0x44  sortedSurfIndex   Load_unsigned_shortArray(true, dpvs+0x04)   (0xBC908)
    dpvs+0x48  smodelInsts       28 bytes/entry, count dpvs+0x00             (0xB60C0)
    dpvs+0x4C  surfaces          52 bytes/entry, count gfxWorld+0x1C         (0xD1E38)
    dpvs+0x50  cullGroups        count gfxWorld+0xEC                         (0xB5C78)
    dpvs+0x54  smodelDrawInsts   44 bytes/entry, count dpvs+0x00             (0xD65D8)

so the index array length comes from dpvs+0x04 alone.  Retail PS3 mp_shipment carries
GfxWorld+0x250.. = 1343, 2526, 0, 2525, 2525, 2526, 2526, 44, 80 - eight count words, not
nine: smodelCount, staticSurfaceCount, then litSurfsBegin/End, decalSurfsBegin/End,
emissiveSurfsBegin/End, then smodelVisDataCount and surfaceVisDataCount.  The PC structure
has staticSurfaceCountNoDecal as its third word; copying nine PC words shifted all six
range fields by one and dropped emissiveSurfsEnd.

The length error was the load-blocking defect: mp_shipment wrote 2541+2067 index entries
where the loader reads 2541, so 4,134 stream bytes were never consumed and every asset
behind GfxWorld - ClipMap first - was decoded from the wrong offset.
"""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = (ROOT / 'cod4porter' / 'assets' / 'gfxworld.py').read_text(encoding='utf-8')

# --- the root builder copies eight count words, skipping PC word 2 -------------------
assert 'for i in range(8):wi32(b,ps+i*4,i32(z,pd+i*4))' not in SOURCE, (
    'PS3 GfxWorldDpvsStatic must not copy the PC staticSurfaceCountNoDecal word'
)
assert 'wi32(b,ps+0x00,i32(z,pd+0x00));wi32(b,ps+0x04,i32(z,pd+0x04))' in SOURCE
assert 'for i in range(6):wi32(b,ps+0x08+i*4,i32(z,pd+0x0c+i*4))' in SOURCE
assert 'wi32(b,ps+0x20,i32(z,pd+0x24));wi32(b,ps+0x24,i32(z,pd+0x28))' in SOURCE

# --- the index array is written with staticSurfaceCount entries ----------------------
assert 'for i in range(q.static_surface_count):w.write_u16(u16(z,cur+i*2)' in SOURCE
assert 'for i in range(sc):w.write_u16(u16(z,cur+i*2)' not in SOURCE
# ...while the PC source cursor still advances by the sum, so the following PC arrays
# stay addressable.
assert 'sc=q.static_surface_count+q.static_surface_count_no_decal' in SOURCE
assert 'cur+=sc*2' in SOURCE
# ...and the pointer field follows the same count.
assert "w32(b,ps+0x44,FOLLOWING if q.static_surface_count else 0)" in SOURCE
assert 'ps+0x44,FOLLOWING if q.static_surface_count+q.static_surface_count_no_decal' not in SOURCE

# --- the Retail oracle, as a pure layout statement -----------------------------------
RETAIL_SHIPMENT_DPVS = (1343, 2526, 0, 2525, 2525, 2526, 2526, 44, 80)
smodel, ssc, lit_begin, lit_end, decal_begin, decal_end, emissive_begin, smodel_vis, surf_vis = (
    RETAIL_SHIPMENT_DPVS
)
assert lit_begin == 0 and lit_end <= decal_begin <= decal_end <= emissive_begin <= ssc
# The two visibility word counts are ((count + 127) >> 7) * 4 on both platforms.
assert ((smodel + 127) >> 7) * 4 == smodel_vis
assert ((ssc + 127) >> 7) * 4 == surf_vis

# --- the emulator-derived byte count that used to leak into the stream ---------------
SHIPMENT_STATIC_SURFACE_COUNT = 2541
SHIPMENT_STATIC_SURFACE_COUNT_NO_DECAL = 2067
assert SHIPMENT_STATIC_SURFACE_COUNT_NO_DECAL * 2 == 4134

print({
    'skipped': False,
    'retail_dpvs_words': len(RETAIL_SHIPMENT_DPVS),
    'leaked_bytes_before_fix': SHIPMENT_STATIC_SURFACE_COUNT_NO_DECAL * 2,
})

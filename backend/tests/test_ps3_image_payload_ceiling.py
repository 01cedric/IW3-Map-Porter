"""No Retail PS3 image payload exceeds 4 MiB, so no generated one may either.

Measured 2026-09-04 across 2843 GfxImages in five Retail PS3 zones - mp_crash,
mp_shipment, common_mp, ui_mp, code_post_gfx_mp - the largest single image payload
anywhere is exactly 4 MiB (``*lightmap0_secondary`` in mp_crash, 512x2048 ARGB8), and
no uncompressed GfxWorld image is wider than 1024:

    mp_crash     *lightmap0_primary   1024x2048 B8     2 MiB
                 *lightmap0_secondary  512x2048 ARGB8  4 MiB
    mp_shipment  *lightmap0_primary   1024x1024 B8     1 MiB
                 *lightmap0_secondary  512x1024 ARGB8  2 MiB

A PC map brings PC-sized lightmaps.  mp_getaway declares four pages with 2048x2048
primaries and 1024x2048 secondaries - three payloads of 8 MiB each and 39.8 MB of
lightmap where Retail's largest map has 6.3 MB.  On hardware the loading bar stopped
at ~16%, which is where GfxWorld ends, and the console froze outright.

This test locks the ceiling and, just as importantly, locks that an image already
inside it is left byte-for-byte alone.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.gfxworld import (  # noqa: E402
    PS3_MAX_IMAGE_PAYLOAD_BYTES,
    PS3_MAX_UNCOMPRESSED_WIDTH,
    _apply_ps3_image_caps,
    _halve_box,
    _oversized_uncompressed,
)
from cod4porter.pc_gfxworld import GfxImagePayload  # noqa: E402

ARGB8, B8, DXT1 = 21, 50, 0x31545844


def image(pc_format, width, height, *, map_type=3, levels=1):
    bpp = 4 if pc_format == ARGB8 else 1 if pc_format == B8 else 0
    payload = width * height * bpp if bpp else width * height // 2
    return GfxImagePayload("lightmap[0].primary", 0, "*lightmap0_primary", map_type, 0, 0, 0,
                           0, 0, 0, 1, levels, 0, width, height, 1, pc_format, payload, 0, 0, "")


# --- the box filter is an average, not a decimation --------------------------------
assert list(_halve_box(bytes([0, 10, 20, 30, 40, 50, 60, 70]), 4, 2, 1)) == [25, 45]
assert list(_halve_box(bytes(range(1, 17)), 2, 2, 4)) == [7, 8, 9, 10]

# --- the ceiling reproduces the Retail dimensions exactly ---------------------------
cases = {
    # PC lightmaps from mp_getaway -> the Retail mp_shipment geometry
    (B8, 2048, 2048): (1024, 1024, 1 << 20),
    (ARGB8, 1024, 2048): (512, 1024, 2 << 20),
    # already Retail-sized (mp_crash): untouched
    (B8, 1024, 2048): (1024, 2048, 2 << 20),
    (ARGB8, 512, 2048): (512, 2048, 4 << 20),
    # $outdoor and other small pages: untouched
    (B8, 512, 512): (512, 512, 512 * 512),
}
for (fmt, w, h), (ew, eh, ebytes) in cases.items():
    img = image(fmt, w, h)
    bpp = 4 if fmt == ARGB8 else 1
    source = bytes(w * h * bpp)
    reduced, data, steps = _apply_ps3_image_caps(img, source, materialize=True)
    assert (reduced.width, reduced.height) == (ew, eh), ((fmt, w, h), reduced.width, reduced.height)
    assert reduced.resource_bytes == ebytes, ((fmt, w, h), reduced.resource_bytes)
    assert reduced.resource_bytes <= PS3_MAX_IMAGE_PAYLOAD_BYTES
    assert reduced.width <= PS3_MAX_UNCOMPRESSED_WIDTH or fmt not in (ARGB8, B8)
    if steps == 0:
        # An image inside the ceiling must come back completely untouched - same
        # object contents and the identical source buffer, not a re-encoded copy.
        assert reduced is img and data is source, (fmt, w, h)
    else:
        assert len(data) == reduced.resource_bytes

# --- what the ceiling must NOT touch ------------------------------------------------
# Compressed images carry their own mip chain and are capped by the texture budget.
assert not _oversized_uncompressed(image(DXT1, 2048, 2048))
# Cubemaps (reflection probes) are six faces of one small page.
assert not _oversized_uncompressed(image(ARGB8, 64, 64, map_type=5, levels=7))
# A mipped uncompressed image is not a lightmap page and is left to the budget stage.
assert not _oversized_uncompressed(image(ARGB8, 2048, 2048, levels=8))

# --- the metadata-only path agrees with the materialized one ------------------------
img = image(ARGB8, 1024, 2048)
a, _, steps_a = _apply_ps3_image_caps(img, bytes(1024 * 2048 * 4), materialize=True)
b, empty, steps_b = _apply_ps3_image_caps(img, bytes(1024 * 2048 * 4), materialize=False)
assert (a.width, a.height, a.resource_bytes) == (b.width, b.height, b.resource_bytes)
assert steps_a == steps_b == 1 and empty == b""

print({"skipped": False, "ceiling_bytes": PS3_MAX_IMAGE_PAYLOAD_BYTES,
       "max_uncompressed_width": PS3_MAX_UNCOMPRESSED_WIDTH, "cases": len(cases)})

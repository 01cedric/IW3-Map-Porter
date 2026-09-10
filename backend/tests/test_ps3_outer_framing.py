from __future__ import annotations

from pathlib import Path
import struct
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.basic import ExternalFxNode, RawFileNode, StringTableNode
from cod4porter.backend.v4_ffio import (
    PS3_RAW_BLOCK,
    read_ps3_fastfile,
    read_ps3_fastfile_bytes,
    require_retail_ps3_adler32,
)
from cod4porter.load_zone import write_load_fastfile
from cod4porter.main_ff import build_main_fastfile


LEGACY_LOAD = ROOT / "reference" / "mp_getaway_load_fix89b.ff"


def independently_verify_every_compressed_frame(data: bytes) -> int:
    """Verify framing without using the production FastFile parser."""
    assert data[:8] == b"IWffu100"
    assert struct.unpack_from(">I", data, 8)[0] == 1
    cursor = 12
    compressed = 0
    while True:
        assert cursor + 2 <= len(data)
        stored, = struct.unpack_from(">H", data, cursor)
        cursor += 2
        if stored == 1:
            break
        if stored == 0:
            assert cursor + PS3_RAW_BLOCK <= len(data)
            cursor += PS3_RAW_BLOCK
            continue
        assert cursor + stored <= len(data)
        payload = data[cursor:cursor + stored]
        cursor += stored
        decoder = zlib.decompressobj(wbits=-15)
        raw = decoder.decompress(payload) + decoder.flush()
        assert decoder.eof
        assert not decoder.unconsumed_tail
        assert len(decoder.unused_data) == 4
        stored_adler, = struct.unpack(">I", decoder.unused_data)
        assert stored_adler == (zlib.adler32(raw) & 0xFFFFFFFF)
        compressed += 1
    return compressed


def corrupt_first_adler(data: bytes) -> bytes:
    damaged = bytearray(data)
    cursor = 12
    while True:
        stored, = struct.unpack_from(">H", damaged, cursor)
        cursor += 2
        if stored == 1:
            raise AssertionError("test FastFile contains no compressed frame")
        if stored == 0:
            cursor += PS3_RAW_BLOCK
            continue
        damaged[cursor + stored - 1] ^= 0x01
        return bytes(damaged)


assets = [
    ExternalFxNode("misc/test", "fx.test"),
    RawFileNode("maps/mp/mp_test.gsc", b"main(){}", "raw.test"),
    StringTableNode("mp/mapsTable.csv", 2, 1, ["mp_test", "CUSTOM"], "st.test"),
]
main = build_main_fastfile(assets, ["a", None, "b"])
main_doc = read_ps3_fastfile_bytes(main.fastfile)
main_frames = independently_verify_every_compressed_frame(main.fastfile)
assert main_frames > 0
assert main_frames == require_retail_ps3_adler32(main_doc)
assert all(not block.compressed or block.stored_bytes == block.deflate_bytes + 4 for block in main_doc.blocks)

with tempfile.TemporaryDirectory() as td:
    output = Path(td) / "mp_getaway_load.ff"
    result = write_load_fastfile(LEGACY_LOAD, output)
    load_bytes = output.read_bytes()
    load_doc = read_ps3_fastfile(output)
    load_frames = independently_verify_every_compressed_frame(load_bytes)
    assert load_frames > 0
    assert load_frames == result["retail_adler32_frames"]
    assert load_frames == require_retail_ps3_adler32(load_doc)

# Legacy Candidate-style frames remain readable and are reported distinctly.
legacy_doc = read_ps3_fastfile(LEGACY_LOAD)
assert any(block.compressed for block in legacy_doc.blocks)
assert all(block.adler32 is None and not block.adler32_verified for block in legacy_doc.blocks if block.compressed)
try:
    require_retail_ps3_adler32(legacy_doc)
except ValueError as exc:
    assert "no Retail Adler-32" in str(exc)
else:
    raise AssertionError("legacy framing incorrectly passed the Retail output gate")

# A present but corrupted checksum must never be mistaken for a legacy frame.
try:
    read_ps3_fastfile_bytes(corrupt_first_adler(main.fastfile))
except ValueError as exc:
    assert "Adler-32 mismatch" in str(exc)
else:
    raise AssertionError("corrupted Retail Adler-32 was accepted")

print({
    "passed": True,
    "main_retail_adler32_frames": main_frames,
    "load_retail_adler32_frames": load_frames,
    "legacy_frames_detected": sum(1 for block in legacy_doc.blocks if block.compressed and block.adler32 is None),
    "corruption_rejected": True,
})

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import io
import struct
import zlib
from typing import Iterable

MAGIC = b"IWffu100"
PC_VERSION = 5
PS3_VERSION = 1
PC_HEADER_SIZE = 12
PS3_HEADER_SIZE = 12
PC_ZONE_HEADER_SIZE = 0x2C
PS3_ZONE_HEADER_SIZE = 0x24
PC_BLOCK_COUNT = 9
PS3_BLOCK_COUNT = 7
FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE
NULL = 0x00000000
PS3_RAW_BLOCK = 0x10000
PS3_END_MARKER = 1
PS3_RAW_MARKER = 0
PS3_ADLER32_SIZE = 4

PC_ASSET_TYPES = {
    0x00:"xmodelpieces",0x01:"physpreset",0x02:"xanimparts",0x03:"xmodel",0x04:"material",
    0x05:"techset",0x06:"image",0x07:"sound",0x08:"sndcurve",0x09:"loaded_sound",
    0x0A:"clipmap",0x0B:"clipmap_pvs",0x0C:"comworld",0x0D:"gameworld_sp",0x0E:"gameworld_mp",
    0x0F:"map_ents",0x10:"gfxworld",0x11:"lightdef",0x12:"ui_map",0x13:"font",0x14:"menulist",
    0x15:"menu",0x16:"localize",0x17:"weapon",0x18:"snddriverglobals",0x19:"fx",0x1A:"impactfx",
    0x1B:"aitype",0x1C:"mptype",0x1D:"character",0x1E:"xmodelalias",0x1F:"rawfile",0x20:"stringtable",
}
PS3_ASSET_TYPES = {
    0x00:"xmodelpieces",0x01:"physpreset",0x02:"xanim",0x03:"xmodel",0x04:"material",
    0x05:"pixelshader",0x06:"vertexshader",0x07:"techset",0x08:"image",0x09:"sound",
    0x0A:"sndcurve",0x0B:"loaded_sound",0x0C:"col_map_sp",0x0D:"col_map_mp",0x0E:"com_map",
    0x0F:"game_map_sp",0x10:"game_map_mp",0x11:"map_ents",0x12:"gfx_map",0x13:"lightdef",
    0x14:"ui_map",0x15:"font",0x16:"menufile",0x17:"menu",0x18:"localize",0x19:"weapon",
    0x1A:"snddriverglobals",0x1B:"fx",0x1C:"impactfx",0x1D:"aitype",0x1E:"mptype",0x1F:"character",
    0x20:"xmodelalias",0x21:"rawfile",0x22:"stringtable",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()

@dataclass(frozen=True)
class PackedPointer:
    raw: int
    kind: str
    block: int | None = None
    offset: int | None = None

@dataclass(frozen=True)
class ZoneHeader:
    platform: str
    zone_memory_size: int
    external_memory_size: int
    block_sizes: tuple[int, ...]
    serialized_size: int

@dataclass(frozen=True)
class ScriptString:
    index: int
    serialized_pointer: int
    value: str | None

@dataclass(frozen=True)
class XAsset:
    index: int
    record_offset: int
    type_id: int
    type_name: str
    serialized_pointer: int

@dataclass(frozen=True)
class XAssetList:
    platform: str
    script_string_pointer: int
    asset_pointer: int
    script_strings: tuple[ScriptString, ...]
    assets: tuple[XAsset, ...]
    asset_pool_offset: int
    asset_data_offset: int
    structural_index: object | None = None

@dataclass(frozen=True)
class Ps3CompressionBlock:
    index: int
    header_offset: int
    stored_bytes: int
    raw_bytes: int
    compressed: bool
    deflate_bytes: int = 0
    adler32: int | None = None
    adler32_verified: bool = False

@dataclass(frozen=True)
class FastFileDocument:
    platform: str
    path: str
    version: int
    file_sha256: str
    zone_sha256: str
    zone: bytes
    trailer: bytes = b""
    blocks: tuple[Ps3CompressionBlock, ...] = ()
    end_marker_offset: int | None = None


def decode_pc_pointer(raw: int) -> PackedPointer:
    raw &= 0xFFFFFFFF
    if raw == NULL: return PackedPointer(raw, "null")
    if raw == FOLLOWING: return PackedPointer(raw, "following")
    if raw == INSERT: return PackedPointer(raw, "insert")
    encoded = (raw - 1) & 0xFFFFFFFF
    return PackedPointer(raw, "packed", encoded >> 28, encoded & 0x0FFFFFFF)


def encode_pc_packed(block: int, offset: int) -> int:
    if not 0 <= block < 16: raise ValueError("PC block must be 0..15")
    if not 0 <= offset <= 0x0FFFFFFF: raise ValueError("PC packed offset exceeds 28 bits")
    return ((block << 28) | offset) + 1


def decode_ps3_pointer(raw: int, block_sizes: Iterable[int] | None = None) -> PackedPointer:
    raw &= 0xFFFFFFFF
    if raw == NULL: return PackedPointer(raw, "null")
    if raw == FOLLOWING: return PackedPointer(raw, "following")
    if raw == INSERT: return PackedPointer(raw, "insert")
    encoded = (raw - 1) & 0xFFFFFFFF
    block = encoded >> 29
    offset = encoded & 0x1FFFFFFF
    if block_sizes is not None:
        sizes = tuple(block_sizes)
        if block >= len(sizes) or sizes[block] <= 0 or offset >= sizes[block]:
            return PackedPointer(raw, "invalid_packed", block, offset)
    return PackedPointer(raw, "packed", block, offset)


def encode_ps3_packed(block: int, offset: int, block_sizes: Iterable[int] | None = None) -> int:
    if not 0 <= block < PS3_BLOCK_COUNT: raise ValueError("PS3 block must be 0..6")
    if not 0 <= offset <= 0x1FFFFFFF: raise ValueError("PS3 packed offset exceeds 29 bits")
    if block_sizes is not None:
        sizes = tuple(block_sizes)
        if offset >= sizes[block]: raise ValueError("PS3 packed offset outside declared block")
    return ((block << 29) | offset) + 1


def read_pc_fastfile(path: str | Path) -> FastFileDocument:
    path = Path(path)
    file = path.read_bytes()
    if len(file) <= PC_HEADER_SIZE or file[:8] != MAGIC:
        raise ValueError("invalid IW3 PC FastFile header")
    version, = struct.unpack_from("<I", file, 8)
    if version != PC_VERSION: raise ValueError(f"expected PC FastFile version {PC_VERSION}, got {version}")
    try:
        zone = zlib.decompress(file[PC_HEADER_SIZE:])
    except zlib.error as exc:
        raise ValueError("PC FastFile zlib payload cannot be decompressed") from exc
    return FastFileDocument("pc", str(path.resolve()), version, sha256(file), sha256(zone), zone)


def _decompress_ps3_frame(payload: bytes, index: int) -> tuple[bytes, int, int | None, bool]:
    """Decode one PS3 compressed frame, accepting legacy and Retail framing.

    Retail IW3 stores ``raw-DEFLATE || Adler32_BE(expanded_bytes)`` inside the size declared by
    the two-byte frame header.  Earlier porter outputs ended at the DEFLATE end-of-stream marker.
    Those legacy frames remain readable, but a present Retail checksum is always verified and
    malformed/unknown trailing bytes fail closed.
    """
    decoder = zlib.decompressobj(wbits=-15)
    try:
        raw = decoder.decompress(payload) + decoder.flush()
    except zlib.error as exc:
        raise ValueError(f"raw-DEFLATE block {index} cannot be decompressed") from exc
    if not decoder.eof:
        raise ValueError(f"raw-DEFLATE block {index} has no end-of-stream marker")
    if decoder.unconsumed_tail:
        raise ValueError(f"raw-DEFLATE block {index} has unconsumed compressed bytes")
    trailing = decoder.unused_data
    deflate_bytes = len(payload) - len(trailing)
    if not trailing:
        return raw, deflate_bytes, None, False
    if len(trailing) != PS3_ADLER32_SIZE:
        raise ValueError(
            f"raw-DEFLATE block {index} has {len(trailing)} unsupported trailing bytes; "
            "expected a 4-byte Retail Adler-32"
        )
    stored_adler, = struct.unpack(">I", trailing)
    calculated_adler = zlib.adler32(raw) & 0xFFFFFFFF
    if stored_adler != calculated_adler:
        raise ValueError(
            f"raw-DEFLATE block {index} Adler-32 mismatch: "
            f"stored 0x{stored_adler:08X}, calculated 0x{calculated_adler:08X}"
        )
    return raw, deflate_bytes, stored_adler, True


def read_ps3_fastfile_bytes(file: bytes, path: str = "<memory>") -> FastFileDocument:
    if len(file) < PS3_HEADER_SIZE + 2 or file[:8] != MAGIC:
        raise ValueError("invalid IW3 PS3 FastFile header")
    version, = struct.unpack_from(">I", file, 8)
    if version != PS3_VERSION: raise ValueError(f"expected PS3 FastFile version {PS3_VERSION}, got {version}")
    cursor = PS3_HEADER_SIZE
    out = bytearray()
    blocks: list[Ps3CompressionBlock] = []
    end_marker_offset = None
    index = 0
    while cursor <= len(file) - 2:
        header_offset = cursor
        stored, = struct.unpack_from(">H", file, cursor)
        cursor += 2
        if stored == PS3_END_MARKER:
            end_marker_offset = header_offset
            break
        if stored == PS3_RAW_MARKER:
            if cursor + PS3_RAW_BLOCK > len(file): raise ValueError(f"truncated raw block {index}")
            raw = file[cursor:cursor+PS3_RAW_BLOCK]
            cursor += PS3_RAW_BLOCK
            out += raw
            blocks.append(Ps3CompressionBlock(index, header_offset, PS3_RAW_BLOCK, PS3_RAW_BLOCK, False))
        else:
            if cursor + stored > len(file): raise ValueError(f"truncated compressed block {index}")
            payload = file[cursor:cursor+stored]
            cursor += stored
            raw, deflate_bytes, adler32, adler32_verified = _decompress_ps3_frame(payload, index)
            if not 1 <= len(raw) <= PS3_RAW_BLOCK: raise ValueError(f"invalid expanded block size {len(raw)}")
            out += raw
            blocks.append(Ps3CompressionBlock(
                index, header_offset, stored, len(raw), True,
                deflate_bytes, adler32, adler32_verified,
            ))
        index += 1
    if end_marker_offset is None: raise ValueError("PS3 FastFile has no 0x0001 end marker")
    zone = bytes(out)
    trailer = file[cursor:]
    return FastFileDocument("ps3", path, version, sha256(file), sha256(zone), zone, trailer, tuple(blocks), end_marker_offset)


def read_ps3_fastfile(path: str | Path) -> FastFileDocument:
    path = Path(path)
    return read_ps3_fastfile_bytes(path.read_bytes(), str(path.resolve()))


def require_retail_ps3_adler32(doc: FastFileDocument) -> int:
    """Require a verified Retail Adler-32 on every compressed PS3 frame.

    Uncompressed 64-KiB marker frames do not carry this trailer.  The return value is the number
    of compressed frames verified, which lets callers include the proof in build reports.
    """
    if doc.platform != "ps3":
        raise ValueError("Retail PS3 frame validation requires a PS3 FastFile document")
    legacy = [block.index for block in doc.blocks if block.compressed and not block.adler32_verified]
    if legacy:
        preview = ", ".join(str(index) for index in legacy[:8])
        suffix = "..." if len(legacy) > 8 else ""
        raise ValueError(
            f"{len(legacy)} compressed PS3 frame(s) have no Retail Adler-32: {preview}{suffix}"
        )
    return sum(1 for block in doc.blocks if block.compressed)


def _compress_raw_deflate(data: bytes, level: int = 9) -> bytes:
    c = zlib.compressobj(level=level, wbits=-15)
    return c.compress(data) + c.flush()


def build_ps3_fastfile(zone: bytes, trailer: bytes = b"", preferred_raw_block_size: int = PS3_RAW_BLOCK) -> bytes:
    if not 4096 <= preferred_raw_block_size <= PS3_RAW_BLOCK: raise ValueError("block size must be 4KiB..64KiB")
    out = bytearray(MAGIC + struct.pack(">I", PS3_VERSION))
    offset = 0
    while offset < len(zone):
        requested = min(preferred_raw_block_size, len(zone)-offset)
        length = requested
        while length >= 1:
            chunk = zone[offset:offset+length]
            comp = _compress_raw_deflate(chunk)
            payload = comp + struct.pack(">I", zlib.adler32(chunk) & 0xFFFFFFFF)
            if len(payload) <= 0xFFFF and len(payload) < len(chunk):
                out += struct.pack(">H", len(payload)) + payload
                offset += length
                break
            if length == PS3_RAW_BLOCK:
                out += b"\x00\x00" + chunk
                offset += length
                break
            if len(payload) <= 0xFFFF:
                out += struct.pack(">H", len(payload)) + payload
                offset += length
                break
            length = max(1, length - 1024)
        else:
            raise RuntimeError("unable to encode PS3 FastFile block")
    out += b"\x00\x01" + trailer
    return bytes(out)


def save_ps3_fastfile(path: str | Path, zone: bytes, trailer: bytes = b"", preferred_raw_block_size: int = PS3_RAW_BLOCK) -> None:
    Path(path).write_bytes(build_ps3_fastfile(zone, trailer, preferred_raw_block_size))


def parse_zone_header(zone: bytes, platform: str) -> ZoneHeader:
    if platform == "pc":
        if len(zone) < PC_ZONE_HEADER_SIZE: raise ValueError("PC zone too short for header")
        words = struct.unpack_from("<11I", zone, 0)
        return ZoneHeader("pc", words[0], words[1], tuple(words[2:]), PC_ZONE_HEADER_SIZE)
    if platform == "ps3":
        if len(zone) < PS3_ZONE_HEADER_SIZE: raise ValueError("PS3 zone too short for header")
        words = struct.unpack_from(">9I", zone, 0)
        return ZoneHeader("ps3", words[0], words[1], tuple(words[2:]), PS3_ZONE_HEADER_SIZE)
    raise ValueError("platform must be pc or ps3")


def _read_cstring(zone: bytes, cursor: int, max_len: int = 1_048_576) -> tuple[str, int]:
    end_limit = min(len(zone), cursor + max_len)
    end = zone.find(b"\x00", cursor, end_limit)
    if end < 0: raise ValueError(f"unterminated string at 0x{cursor:X}")
    return zone[cursor:end].decode("latin-1"), end+1


def parse_xasset_list(zone: bytes, platform: str) -> XAssetList:
    header = parse_zone_header(zone, platform)
    root = header.serialized_size
    if root + 16 > len(zone): raise ValueError("zone too short for XAssetList root")
    endian = "<" if platform == "pc" else ">"
    script_count, script_ptr, asset_count, asset_ptr = struct.unpack_from(endian+"4I", zone, root)
    if script_count > 1_000_000 or asset_count > 1_000_000: raise ValueError("implausible XAssetList counts")
    if script_count and script_ptr != FOLLOWING: raise ValueError(f"script string array not inline: 0x{script_ptr:08X}")
    if asset_count and asset_ptr != FOLLOWING: raise ValueError(f"asset array not inline: 0x{asset_ptr:08X}")
    cursor = root + 16
    ptr_bytes = script_count * 4
    if cursor + ptr_bytes > len(zone): raise ValueError("script string pointer array exceeds zone")
    pointers = struct.unpack_from(endian+f"{script_count}I", zone, cursor) if script_count else ()
    cursor += ptr_bytes
    strings: list[ScriptString] = []
    for i, raw in enumerate(pointers):
        value = None
        if raw == FOLLOWING:
            value, cursor = _read_cstring(zone, cursor)
        strings.append(ScriptString(i, raw, value))
    asset_pool_offset = cursor
    asset_bytes = asset_count * 8
    if asset_pool_offset + asset_bytes > len(zone): raise ValueError("XAsset pool exceeds zone")
    types = PC_ASSET_TYPES if platform == "pc" else PS3_ASSET_TYPES
    max_type = max(types)
    assets: list[XAsset] = []
    for i in range(asset_count):
        record_offset = asset_pool_offset + i*8
        type_id, raw = struct.unpack_from(endian+"II", zone, record_offset)
        if type_id > max_type: raise ValueError(f"asset #{i} unsupported type 0x{type_id:X} at 0x{record_offset:X}")
        if raw == 0: raise ValueError(f"asset #{i} has null root pointer")
        assets.append(XAsset(i, record_offset, type_id, types.get(type_id, f"unknown_0x{type_id:02X}"), raw))
    return XAssetList(platform, script_ptr, asset_ptr, tuple(strings), tuple(assets), asset_pool_offset, asset_pool_offset+asset_bytes)


def summarize_fastfile(path: str | Path, platform: str | None = None) -> dict:
    if platform is None:
        data = Path(path).read_bytes()
        if len(data) < 12 or data[:8] != MAGIC: raise ValueError("not an IW3 FastFile")
        le = struct.unpack_from("<I", data, 8)[0]
        be = struct.unpack_from(">I", data, 8)[0]
        if le == PC_VERSION: platform = "pc"
        elif be == PS3_VERSION: platform = "ps3"
        else: raise ValueError("cannot infer FastFile platform/version")
    doc = read_pc_fastfile(path) if platform == "pc" else read_ps3_fastfile(path)
    header = parse_zone_header(doc.zone, platform)
    assets = parse_xasset_list(doc.zone, platform)
    counts: dict[str,int] = {}
    for a in assets.assets: counts[a.type_name] = counts.get(a.type_name,0)+1
    return {
        "platform": platform,
        "path": doc.path,
        "version": doc.version,
        "file_bytes": Path(path).stat().st_size,
        "zone_bytes": len(doc.zone),
        "file_sha256": doc.file_sha256,
        "zone_sha256": doc.zone_sha256,
        "compression_blocks": len(doc.blocks),
        "compressed_blocks": sum(1 for b in doc.blocks if b.compressed),
        "retail_adler32_blocks": sum(1 for b in doc.blocks if b.adler32_verified),
        "legacy_deflate_blocks": sum(1 for b in doc.blocks if b.compressed and b.adler32 is None),
        "trailer_bytes": len(doc.trailer),
        "zone_header": asdict(header),
        "script_strings": len(assets.script_strings),
        "xassets": len(assets.assets),
        "asset_counts": counts,
        "asset_pool_offset": assets.asset_pool_offset,
        "asset_data_offset": assets.asset_data_offset,
    }

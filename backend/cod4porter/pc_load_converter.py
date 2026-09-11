from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Iterable
import zipfile

from .assets.image import ImageAsset, ImageMip, PS3_DXT1, build_image_root, from_iwi
from .backend.v4_ffio import (
    FOLLOWING,
    INSERT,
    NULL,
    PC_ZONE_HEADER_SIZE,
    build_ps3_fastfile,
    decode_pc_pointer,
    encode_pc_packed,
    parse_xasset_list,
    parse_zone_header,
    read_pc_fastfile,
    read_ps3_fastfile,
    read_ps3_fastfile_bytes,
    require_retail_ps3_adler32,
    sha256,
)
from .backend.v4_iwd import IwiImage, normalize_iwi_name, parse_iwi
from .backend.v4_load import parse_load_zone
from .load_zone import (
    LoadDocument,
    LoadImage,
    LoadMaterial,
    LoadRawFile,
    LoadTexture,
    build_load_zone,
    compute_load_block_sizes,
    read_load_document,
)
from .material import MaterialRecord
from .pc_image import PcImageRecord, parse_image_at
from .pc_material import parse_material_at
from .pc_technique import ROOT as PC_TECHNIQUE_ROOT
from .pc_technique import TechniqueSet, parse_at as parse_technique_at


PC_LOAD_TYPES = (0x05, 0x05, 0x04, 0x04, 0x04, 0x1F)
PC_LOAD_TECHNIQUES = (",sm2/2d", ",2d")
LOAD_MATERIALS = ("$victorybackdrop", ",$defeatbackdrop", "$levelbriefing")
PC_RAWFILE_ROOT = 0x0C
PC_FORMATS = {
    0x31545844: "dxt1",
    0x33545844: "dxt3",
    0x35545844: "dxt5",
}
MAP_NAME_RE = re.compile(r"^mp_[a-z0-9_]+$")


def _u32le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_latin1z(data: bytes, cursor: int, label: str) -> tuple[str, int]:
    if cursor < 0 or cursor >= len(data):
        raise ValueError(f"{label} starts outside the PC zone")
    end = data.find(b"\0", cursor)
    if end < 0:
        raise ValueError(f"{label} is not NUL-terminated")
    value = data[cursor:end].decode("latin-1")
    if not value:
        raise ValueError(f"{label} is empty")
    return value, end + 1


def _is_linker_diagnostic(data: bytes) -> bool:
    if not data:
        return True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(
        line.startswith(("ERROR:", "WARNING:", "INFO:")) for line in lines
    )


@dataclass(frozen=True)
class PcLoadSource:
    path: str
    file_sha256: str
    zone_sha256: str
    map_name: str
    rawfile_name: str
    rawfile_data: bytes
    external_memory_size: int
    block_sizes: tuple[int, ...]
    zone_bytes: int
    technique_sets: tuple[TechniqueSet, TechniqueSet]
    materials: tuple[MaterialRecord, MaterialRecord, MaterialRecord]
    victory_image: PcImageRecord | None
    levelbriefing_image: PcImageRecord
    defeat_image: PcImageRecord | None = None

    @property
    def levelbriefing_name(self) -> str:
        return f"loadscreen_{self.map_name}"


@dataclass(frozen=True)
class IwdImageSource:
    archive_path: str
    entry_name: str
    content_sha256: str
    image: IwiImage


@dataclass(frozen=True)
class Ps3LoadUiProfile:
    """Small audited PS3 UI ABI profile; it deliberately contains no image payload."""

    name: str
    audit_source: str
    audit_file_sha256: str
    audit_zone_sha256: str
    preferred_capacity: int
    reference_block_sizes: tuple[int, ...]
    technique_state: bytes
    technique_slots: tuple[int, ...]
    material_roots: tuple[bytes, bytes, bytes]
    texture_headers: tuple[bytes, bytes]
    material_states: tuple[tuple[bytes, ...], tuple[bytes, ...], tuple[bytes, ...]]

    @property
    def structural_bytes(self) -> int:
        return (
            len(self.technique_state)
            + len(self.technique_slots) * 4
            + sum(len(root) for root in self.material_roots)
            + sum(len(header) for header in self.texture_headers)
            + sum(len(state) for states in self.material_states for state in states)
        )


BUILTIN_PS3_LOAD_UI_V1 = Ps3LoadUiProfile(
    name="iw3-ps3-retail-load-ui-v1",
    audit_source="retail PS3 mp_shipment_load.ff (structure/scalars only)",
    audit_file_sha256="54D2ABB742BFD29A88BFB9460C4841C7C57111B5F2113467C330A432341989C4",
    audit_zone_sha256="1660C023BD5958DDDF55B1A8E62D39109BC80D5B730D1258C8966E0A005814B9",
    preferred_capacity=0x130000,
    reference_block_sizes=(196, 0, 0, 0, 198, 0, 1223424),
    technique_state=b"\0\0\0\0",
    technique_slots=(0,) * 26,
    material_roots=(
        bytes.fromhex(
            "FFFFFFFF002B010100000000000000000000000000000000FFFFFFFF00FFFFFFFF"
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF01000100030000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000080000005FFFFFFFF00000000FFFFFFFF"
        ),
        bytes.fromhex(
            "FFFFFFFF00000000000000000000000000000000000000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
        ),
        bytes.fromhex(
            "FFFFFFFF0004010100000000000000000000000000000000FFFFFFFF00FFFFFFFF"
            "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF01000100030000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000080000005FFFFFFFF00000000FFFFFFFF"
        ),
    ),
    texture_headers=(
        bytes.fromhex("A0AB104163700A00"),
        bytes.fromhex("A0AB10416370E200"),
    ),
    material_states=(
        (bytes.fromhex("FFFFFFFF19289165E00E0002"),),
        (),
        (bytes.fromhex("FFFFFFFF18128812E00E0002"),),
    ),
)

if BUILTIN_PS3_LOAD_UI_V1.structural_bytes >= 4096:
    raise AssertionError("built-in PS3 load UI profile unexpectedly exceeds 4 KiB")


def _validate_pc_load_image(
    image: PcImageRecord,
    expected_name: str,
    label: str,
) -> None:
    if image.name.casefold() != expected_name.casefold():
        raise ValueError(f"{label} image is {image.name!r}, expected {expected_name!r}")
    if image.map_type != 3:
        raise ValueError(f"{label} image mapType {image.map_type} is not a 2D texture")
    if image.name_pointer != FOLLOWING:
        raise ValueError(f"{label} image does not own its source name")
    if decode_pc_pointer(image.loaddef_pointer).kind != "insert":
        raise ValueError(f"{label} image does not own an INSERT loadDef")
    if image.loaddef is None:
        raise ValueError(f"{label} image has no PC loadDef metadata")
    if image.loaddef.resource:
        raise ValueError(f"{label} image unexpectedly embeds PC resource bytes")
    if image.loaddef.pc_format not in PC_FORMATS:
        raise ValueError(
            f"{label} image format 0x{image.loaddef.pc_format:08X} is unsupported"
        )
    # ``noPicmip`` only says whether the PC build may downscale this texture through the
    # picmip setting.  It is a per-image authoring choice, not a load-zone contract:
    # Retail Shipment ships both loadscreen images with 1, while mp_getaway's
    # loadscreen_mp_getaway carries 0.  The PS3 image root does not encode the flag at
    # all, so both values convert identically and only the fixed category/delay policy
    # is a real requirement here.
    if image.no_picmip not in (0, 1):
        raise ValueError(f"{label} image has a non-boolean PC noPicmip value {image.no_picmip}")
    if image.category != 3 or image.delay_load_pixels != 0:
        raise ValueError(
            f"{label} image has an unmeasured PC load-zone policy "
            f"(category {image.category}, delayLoadPixels {image.delay_load_pixels})"
        )


def parse_pc_load_source(
    path: str | Path,
    map_name: str | None = None,
) -> PcLoadSource:
    """Parse the complete, small IW3 PC ``*_load.ff`` LoadStream.

    This intentionally does not scan for plausible roots.  The measured load-zone family has
    six source-order XAssets and every root is serialized inline, so a sequential replay gives
    stronger ownership evidence and fails closed if the source shape changes.
    """

    source_path = Path(path)
    ff = read_pc_fastfile(source_path)
    zone = ff.zone
    header = parse_zone_header(zone, "pc")
    assets = parse_xasset_list(zone, "pc")
    if assets.script_strings:
        raise ValueError("PC load zone must not contain script strings")
    got_types = tuple(asset.type_id for asset in assets.assets)
    if got_types != PC_LOAD_TYPES:
        raise ValueError(f"unexpected PC load XAsset order {got_types!r}")
    if any(asset.serialized_pointer != FOLLOWING for asset in assets.assets):
        raise ValueError("all PC load top-level roots must be FOLLOWING")
    if header.zone_memory_size != len(zone) - PC_ZONE_HEADER_SIZE:
        raise ValueError("PC load zone memory size does not cover the exact serialized payload")

    cursor = assets.asset_data_offset
    techniques: list[TechniqueSet] = []
    for asset_index in range(2):
        root = cursor
        parsed = parse_technique_at(zone, root)
        if parsed is None:
            raise ValueError(f"PC TechniqueSet #{asset_index} is structurally invalid")
        name, cursor = _read_latin1z(
            zone, root + PC_TECHNIQUE_ROOT, f"PC TechniqueSet #{asset_index} name"
        )
        if name != parsed.name:
            raise AssertionError("TechniqueSet parser/name replay differs")
        techniques.append(replace(parsed, source_asset_index=asset_index))

    technique_names = tuple(tech.name for tech in techniques)
    if technique_names != PC_LOAD_TECHNIQUES:
        raise ValueError(
            f"PC load TechniqueSets are {technique_names!r}, expected {PC_LOAD_TECHNIQUES!r}"
        )
    for tech in techniques:
        if (
            tech.world_vertex_format != 0
            or tech.has_been_uploaded
            or tech.remapped_pointer != NULL
            or any(tech.technique_pointers)
        ):
            raise ValueError(f"PC load TechniqueSet {tech.name!r} is not the measured null-slot shell")

    materials: list[MaterialRecord] = []
    for asset_index in range(2, 5):
        root = cursor
        if _u32le(zone, root) != FOLLOWING:
            raise ValueError(f"PC Material XAsset #{asset_index} does not own its name")
        material, cursor = parse_material_at(zone, root)
        materials.append(material)

    material_names = tuple(material.name for material in materials)
    shared_victory=material_names[0]==',$victorybackdrop'
    shared_defeat=material_names[1]==',$defeatbackdrop'
    if (tuple(n.lstrip(',') for n in material_names)!=('$victorybackdrop','$defeatbackdrop','$levelbriefing')
            or material_names[2]!='$levelbriefing'):
        raise ValueError(
            f"PC load materials are {material_names!r}, expected {LOAD_MATERIALS!r}"
        )
    expected_technique_alias = encode_pc_packed(4, 1 * 8 + 4)
    if tuple(material.technique_set_pointer for material in materials) != (
        NULL if shared_victory else expected_technique_alias,
        NULL if shared_defeat else expected_technique_alias,
        expected_technique_alias,
    ):
        raise ValueError("PC load materials do not bind both active materials to source ,2d")
    if tuple((m.texture_count, m.constant_count, m.declared_state_bits_count) for m in materials) != (
        (0, 0, 0) if shared_victory else (1, 0, 1),
        (0, 0, 0) if shared_defeat else (1, 0, 1),
        (1, 0, 1),
    ):
        raise ValueError("PC load material table counts differ from the measured family")

    victory_texture = None if shared_victory else materials[0].textures[0]
    defeat_texture = None if shared_defeat else materials[1].textures[0]
    briefing_texture = materials[2].textures[0]
    if (victory_texture is not None and victory_texture.inline_image_root is None) or briefing_texture.inline_image_root is None:
        raise ValueError("PC load material images must be physically inline")
    victory = parse_image_at(zone, victory_texture.inline_image_root) if victory_texture is not None else None
    briefing = parse_image_at(zone, briefing_texture.inline_image_root)
    if defeat_texture is not None and defeat_texture.inline_image_root is None:
        raise ValueError('PC defeat backdrop image must be physically inline')
    defeat = parse_image_at(zone,defeat_texture.inline_image_root) if defeat_texture is not None else None

    if cursor + PC_RAWFILE_ROOT > len(zone):
        raise ValueError("PC load RawFile root is truncated")
    name_pointer, raw_length, buffer_pointer = struct.unpack_from("<III", zone, cursor)
    if name_pointer != FOLLOWING or buffer_pointer != FOLLOWING:
        raise ValueError("PC load RawFile name and buffer must be FOLLOWING")
    cursor += PC_RAWFILE_ROOT
    raw_name, cursor = _read_latin1z(zone, cursor, "PC load RawFile name")
    if raw_length > len(zone) - cursor - 1:
        raise ValueError("PC load RawFile payload is truncated")
    raw_data = bytes(zone[cursor : cursor + raw_length])
    cursor += raw_length
    if cursor >= len(zone) or zone[cursor] != 0:
        raise ValueError("PC load RawFile terminator is missing")
    cursor += 1
    if cursor != len(zone):
        raise ValueError(f"PC load parser left {len(zone) - cursor} unowned byte(s)")

    if not raw_name.endswith("_load"):
        raise ValueError(f"PC load RawFile {raw_name!r} has no _load marker suffix")
    derived_map = raw_name[:-5]
    if not MAP_NAME_RE.fullmatch(derived_map):
        raise ValueError(f"PC load RawFile implies invalid map name {derived_map!r}")
    if map_name is not None and derived_map.casefold() != map_name.casefold():
        raise ValueError(
            f"requested map {map_name!r} does not match PC load marker {raw_name!r}"
        )
    if not _is_linker_diagnostic(raw_data):
        raise ValueError(
            "PC load marker contains non-diagnostic data; refusing to discard a possible runtime RawFile"
        )

    # $levelbriefing owns the image identity; custom maps may use an arbitrary
    # IWI basename (Raid uses bo2raid_loadscreen). The output name is canonical.
    expected_briefing = briefing.name
    if victory is not None:_validate_pc_load_image(victory, "mile_high_victory_screen", "victory")
    _validate_pc_load_image(briefing, expected_briefing, "levelbriefing")
    if defeat is not None:
        _validate_pc_load_image(defeat,defeat.name,'defeat')
        if defeat_texture.inline_image_name!=defeat.name:
            raise ValueError('defeat material/image ownership names disagree')
    if victory is not None and victory_texture.inline_image_name != victory.name:
        raise ValueError("victory material/image ownership names disagree")
    if briefing_texture.inline_image_name != briefing.name:
        raise ValueError("levelbriefing material/image ownership names disagree")

    return PcLoadSource(
        str(source_path.resolve()),
        ff.file_sha256,
        ff.zone_sha256,
        derived_map,
        raw_name,
        raw_data,
        header.external_memory_size,
        header.block_sizes,
        len(zone),
        (techniques[0], techniques[1]),
        (materials[0], materials[1], materials[2]),
        victory,
        briefing,
        defeat,
    )


def _coerce_iwd_paths(paths: str | Path | Iterable[str | Path]) -> tuple[Path, ...]:
    if isinstance(paths, (str, Path)):
        result = (Path(paths),)
    else:
        result = tuple(Path(path) for path in paths)
    if not result:
        raise ValueError("at least one source IWD is required")
    return result


class MissingIwdImage(ValueError):
    pass


def find_iwd_image(
    paths: str | Path | Iterable[str | Path],
    image_name: str,
) -> IwdImageSource:
    """Resolve one exact IWI without treating unrelated IWD audio as output assets."""

    wanted = normalize_iwi_name(image_name).casefold()
    matches: list[IwdImageSource] = []
    for archive in _coerce_iwd_paths(paths):
        with zipfile.ZipFile(archive, "r") as zf:
            for entry in zf.infolist():
                if entry.is_dir() or not entry.filename.lower().endswith(".iwi"):
                    continue
                if normalize_iwi_name(entry.filename).casefold() != wanted:
                    continue
                data = zf.read(entry)
                matches.append(
                    IwdImageSource(
                        str(archive.resolve()),
                        entry.filename.replace("\\", "/"),
                        hashlib.sha256(data).hexdigest().upper(),
                        parse_iwi(entry.filename, data),
                    )
                )
    if not matches:
        raise MissingIwdImage(f"IWD set has no exact images/{image_name}.iwi")
    hashes = {match.content_sha256 for match in matches}
    if len(hashes) != 1:
        owners = ", ".join(match.archive_path for match in matches)
        raise ValueError(f"conflicting IWI payloads for {image_name!r}: {owners}")
    return matches[0]


def _image_asset_from_iwd(source: PcLoadSource, iwd: IwdImageSource,
                          notes: list | None = None) -> ImageAsset:
    pc = source.levelbriefing_image.loaddef
    if pc is None:
        raise AssertionError("validated PC levelbriefing has no loadDef")
    image = iwd.image
    if image.name.casefold() != source.levelbriefing_image.name.casefold():
        raise ValueError("resolved IWI canonical name does not match the PC load image")
    if (image.width, image.height, image.depth) != (pc.width, pc.height, pc.depth):
        raise ValueError(
            "IWI dimensions do not match PC load metadata: "
            f"{image.width}x{image.height}x{image.depth} vs {pc.width}x{pc.height}x{pc.depth}"
        )
    if image.format_name != PC_FORMATS[pc.pc_format]:
        # The IWI is the pixel authority: on PC the engine streams the IWI at
        # runtime and the loadDef format byte is a metadata stub, and custom
        # loading screens routinely ship e.g. DXT5 pixels behind a DXT1 stub
        # (mp_osg_raid).  The PS3 load zone serializes the IWI's own format
        # and sizes (from_iwi below), so accepting the mismatch stays
        # byte-consistent; it is recorded as a fidelity note, never guessed.
        if notes is not None:
            notes.append(
                f"loadscreen_format_from_iwi:{image.format_name}"
                f"_over_pc_declared_{PC_FORMATS[pc.pc_format]}")
    # An IW3 loadDef may declare levelCount 0, which means "the full chain implied by the
    # dimensions" rather than "no mip levels".  mp_getaway's 1024x1024 loadscreen does
    # exactly that while its IWI carries all 11 levels.  Derive the expected count in
    # that case instead of rejecting a valid PC load zone.
    expected_levels = pc.level_count
    if expected_levels == 0:
        expected_levels = 1 if pc.flags & 0x02 else max(pc.width, pc.height, pc.depth).bit_length()
    if image.level_count != expected_levels:
        raise ValueError(
            f"IWI mip count {image.level_count} does not match the PC load chain "
            f"{expected_levels} (declared levelCount {pc.level_count})"
        )
    # Flags also carry upload/picmip policy. Equality of the entire byte is not
    # a pixel-layout contract: dimensions, format and the actual mip count were
    # checked above. Both inputs must still describe a 2D, single-face image.
    if image.is_cubemap or pc.flags & 0x04 or image.depth != 1:
        raise ValueError("Loading image must be a single-face 2D image; cubemaps are unsupported")
    converted = from_iwi(image, semantic=source.levelbriefing_image.semantic)
    return replace(converted, name=source.levelbriefing_name)


def _neutral_victory_image(source: PcLoadSource) -> ImageAsset:
    """Create owned BC1 pixels from PC metadata, with no Retail payload dependency.

    The PC load shell declares this stock image in external memory and therefore supplies no
    bytes.  A complete neutral mip chain keeps the measured dimensions, depth, format and PS3
    allocation topology without copying Shipment/Getaway pixels.
    """

    record = source.victory_image
    loaddef = record.loaddef
    if loaddef is None:
        raise AssertionError("validated PC victory image has no loadDef")
    if loaddef.pc_format != 0x31545844 or loaddef.depth != 1:
        raise ValueError("built-in PS3 load profile supports only the measured 2D BC1 victory shell")
    if loaddef.flags & 0x02:
        levels = 1
    else:
        levels = max(loaddef.width, loaddef.height).bit_length()
    block = b"\xFF\xFF\xFF\xFF\0\0\0\0"
    resource = bytearray()
    mips: list[ImageMip] = []
    for level in range(levels):
        width = max(1, loaddef.width >> level)
        height = max(1, loaddef.height >> level)
        size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
        data = block * (size // len(block))
        offset = len(resource)
        resource += data
        mips.append(
            ImageMip(
                0,
                level,
                width,
                height,
                offset,
                size,
                hashlib.sha256(data).hexdigest().upper(),
            )
        )
    unpadded = len(resource)
    padding = (-unpadded) % 0x80
    resource += b"\0" * padding
    return ImageAsset(
        record.name,
        3,
        "dxt1",
        PS3_DXT1,
        record.semantic,
        loaddef.width,
        loaddef.height,
        loaddef.depth,
        levels,
        1,
        bytes(resource),
        unpadded,
        padding,
        tuple(mips),
    )


def _audit_external_retail_reference(
    path: str | Path,
    source: PcLoadSource,
    built_in: Ps3LoadUiProfile,
) -> tuple[dict, ImageAsset]:
    """Optionally re-audit the built-in scalar profile against a complete Retail file."""

    reference_path = Path(path)
    reference_ff = read_ps3_fastfile(reference_path)
    adler_frames = require_retail_ps3_adler32(reference_ff)
    parsed = parse_load_zone(reference_ff.zone)
    doc = read_load_document(reference_path)
    if doc.trailer or doc.external_memory_size != 0:
        raise ValueError("PS3 load reference has an unsupported trailer/external-memory policy")
    if tuple(material.name for material in doc.materials) != LOAD_MATERIALS:
        raise ValueError("PS3 load reference materials differ from the retail load family")
    if tuple(len(material.textures) for material in doc.materials) != (1, 0, 1):
        raise ValueError("PS3 load reference texture topology differs from retail")
    if doc.rawfile.data or not doc.rawfile.name.endswith("_load"):
        raise ValueError("PS3 load reference RawFile is not an empty _load marker")
    if parsed.trailing_nonzero:
        raise ValueError("PS3 load reference capacity tail contains nonzero bytes")

    exact_structure = (
        doc.preferred_capacity == built_in.preferred_capacity
        and doc.block_sizes == built_in.reference_block_sizes
        and doc.technique_name == ",2d"
        and doc.technique_state == built_in.technique_state
        and doc.technique_slots == built_in.technique_slots
        and tuple(material.root_prefix for material in doc.materials) == built_in.material_roots
        and (
            doc.materials[0].textures[0].header8,
            doc.materials[2].textures[0].header8,
        )
        == built_in.texture_headers
        and tuple(material.states for material in doc.materials) == built_in.material_states
        and all(not material.constants for material in doc.materials)
    )
    if not exact_structure:
        raise ValueError("external PS3 load reference differs from the built-in audited UI profile")
    victory = parsed.materials[0].textures[0]["image"]
    source_victory = source.victory_image.loaddef
    if source_victory is None:
        raise AssertionError("validated PC victory image has no loadDef")
    if victory["name"].casefold() != source.victory_image.name.casefold():
        raise ValueError("external PS3 reference victory name differs from the PC source")
    if (victory["width"], victory["height"], victory["depth"]) != (
        source_victory.width,
        source_victory.height,
        source_victory.depth,
    ):
        raise ValueError("external PS3 reference victory dimensions differ from the PC source")
    retail_image = doc.materials[0].textures[0].image
    descriptor = retail_image.descriptor
    if retail_image.map_type != 3 or descriptor[0] != PS3_DXT1:
        raise ValueError("external PS3 reference victory image is not the measured 2D BC1 asset")
    mip_count = descriptor[1]
    width, height, depth = struct.unpack_from(">HHH", descriptor, 8)
    unpadded = 0
    for level in range(mip_count):
        mip_width = max(1, width >> level)
        mip_height = max(1, height >> level)
        unpadded += max(1, (mip_width + 3) // 4) * max(1, (mip_height + 3) // 4) * 8
    if unpadded > len(retail_image.resource):
        raise ValueError("external PS3 reference victory resource is shorter than its mip chain")
    padding = len(retail_image.resource) - unpadded
    if padding >= 0x80 or any(retail_image.resource[unpadded:]):
        raise ValueError("external PS3 reference victory padding is not canonical zero alignment")
    victory_asset = ImageAsset(
        source.victory_image.name,
        3,
        "dxt1",
        PS3_DXT1,
        descriptor[0x18],
        width,
        height,
        depth,
        mip_count,
        1,
        bytes(retail_image.resource),
        unpadded,
        padding,
        (),
    )
    rebuilt = build_image_root(victory_asset)
    if rebuilt[4:0x2C] != descriptor:
        raise ValueError("typed rebuild of the external stock victory descriptor is not exact")
    report = {
        "performed": True,
        "path": str(reference_path.resolve()),
        "file_sha256": reference_ff.file_sha256,
        "zone_sha256": reference_ff.zone_sha256,
        "retail_adler32_frames": adler_frames,
        "structure_exact_vs_builtin": True,
        "rawfile_name": doc.rawfile.name,
        "levelbriefing_name": parsed.materials[2].textures[0]["image"]["name"],
        "image_payload_used_for_build": "stock mile_high_victory_screen only",
        "stock_victory_payload_used_for_build": True,
        "stock_victory_identity": {
            "name_matches_pc": True,
            "dimensions_match_pc": True,
            "format": "dxt1",
            "mip_count": mip_count,
            "resource_bytes": len(retail_image.resource),
            "resource_sha256": sha256(retail_image.resource),
        },
        "map_specific_image_payload_used_for_build": False,
        "map_specific_names_used_for_build": False,
    }
    return report, victory_asset


def _load_image(image: ImageAsset, name: str) -> LoadImage:
    root = build_image_root(image)
    return LoadImage(
        struct.unpack_from(">I", root, 0)[0],
        bytes(root[4:0x2C]),
        name,
        bytes(image.resource),
    )


def _fresh_target_document(
    source: PcLoadSource,
    built_in: Ps3LoadUiProfile,
    victory: ImageAsset | None,
    levelbriefing: ImageAsset,
    defeat: ImageAsset | None = None,
) -> LoadDocument:
    materials = (
        LoadMaterial(
            bytes(built_in.material_roots[0 if victory is not None else 1]),
            source.materials[0].name,
            (
                LoadTexture(
                    bytes(built_in.texture_headers[0]),
                    _load_image(victory, source.victory_image.name),
                ),
            ) if victory is not None else (),
            (),
            tuple(bytes(value) for value in built_in.material_states[0]) if victory is not None else (),
        ),
        LoadMaterial(
            bytes(built_in.material_roots[0 if defeat is not None else 1]),
            source.materials[1].name if defeat is not None else ','+source.materials[1].name.lstrip(','),
            (LoadTexture(bytes(built_in.texture_headers[0]),_load_image(defeat,source.defeat_image.name)),) if defeat is not None else (),
            (),
            tuple(bytes(value) for value in built_in.material_states[0]) if defeat is not None else (),
        ),
        LoadMaterial(
            bytes(built_in.material_roots[2]),
            source.materials[2].name,
            (
                LoadTexture(
                    bytes(built_in.texture_headers[1]),
                    _load_image(levelbriefing, source.levelbriefing_name),
                ),
            ),
            (),
            tuple(bytes(value) for value in built_in.material_states[2]),
        ),
    )
    return LoadDocument(
        0,
        tuple(built_in.reference_block_sizes),
        built_in.preferred_capacity,
        b"",
        bytes(built_in.technique_state),
        tuple(built_in.technique_slots),
        ",2d",
        materials,
        LoadRawFile(f"{source.map_name}_load", b""),
    )


def convert_pc_load_to_ps3(
    pc_load_ff: str | Path,
    iwd_paths: str | Path | Iterable[str | Path],
    output: str | Path,
    *,
    retail_load_reference: str | Path | None = None,
    map_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Build a fresh PS3-v1 ``*_load.ff`` from its PC source and exact IWI.

    No complete Retail map is required for compatibility mode.  The built-in profile contains
    fewer than 4 KiB of audited UI structure/scalars and no image payload.  A supplied Retail
    reference re-audits that profile and contributes only the source-name-identical stock victory
    image for full-fidelity mode; map-specific reference names/resources never participate.
    """

    source = parse_pc_load_source(pc_load_ff, map_name)
    output_path = Path(output)
    expected_filename = f"{source.map_name}_load.ff"
    if output_path.name.casefold() != expected_filename.casefold():
        raise ValueError(
            f"PS3 load output must be named {expected_filename!r}, got {output_path.name!r}"
        )
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing output {output_path}")

    iwd = find_iwd_image(iwd_paths, source.levelbriefing_image.name)
    image_notes: list[str] = []
    image = _image_asset_from_iwd(source, iwd, image_notes)
    defeat_image=None
    defeat_missing=False
    if source.defeat_image is not None:
        defeat_source=replace(source,levelbriefing_image=source.defeat_image)
        try:defeat_iwd=find_iwd_image(iwd_paths,source.defeat_image.name)
        except MissingIwdImage:defeat_missing=True
        else:defeat_image=replace(_image_asset_from_iwd(defeat_source,defeat_iwd,image_notes),name=source.defeat_image.name)
    if source.victory_image is None:
        external_audit=None;victory_image=None
        victory_provenance='shared $victorybackdrop material; no image payload owned by the source'
        load_full_fidelity=True;compatibility_fallbacks=[]
    elif retail_load_reference is not None:
        external_audit, victory_image = _audit_external_retail_reference(
            retail_load_reference, source, BUILTIN_PS3_LOAD_UI_V1
        )
        victory_provenance = "exact stock-identical PS3 mile_high_victory_screen from audited reference"
        load_full_fidelity = True
        compatibility_fallbacks: list[str] = []
    else:
        external_audit = None
        victory_image = _neutral_victory_image(source)
        victory_provenance = "fresh neutral BC1 compatibility fallback from PC dimensions/format"
        load_full_fidelity = False
        compatibility_fallbacks = ["neutral_victory_pixels"]
    if defeat_missing:
        load_full_fidelity=False
        compatibility_fallbacks.append('native_shared_defeatbackdrop_missing_source_pixels')
    compatibility_fallbacks.extend(image_notes)
    target_doc = _fresh_target_document(
        source,
        BUILTIN_PS3_LOAD_UI_V1,
        victory_image,
        image,
        defeat_image,
    )
    target_blocks = compute_load_block_sizes(target_doc)
    zone = build_load_zone(target_doc)
    fastfile = build_ps3_fastfile(zone)

    memory_doc = read_ps3_fastfile_bytes(fastfile)
    output_adler_frames = require_retail_ps3_adler32(memory_doc)
    profile = parse_load_zone(memory_doc.zone)
    header = parse_zone_header(memory_doc.zone, "ps3")
    if header.block_sizes != target_blocks:
        raise AssertionError("PS3 load header differs from allocator replay")
    if profile.technique_name != ",2d" or profile.rawfile_name != f"{source.map_name}_load":
        raise AssertionError("PS3 load readback names differ from the fresh document")
    if profile.rawfile_bytes != 0:
        raise AssertionError("PS3 load RawFile marker is not empty")
    if profile.asset_type_order != ["techset", "material", "material", "material", "rawfile"]:
        raise AssertionError("PS3 load readback XAsset order changed")
    level = profile.materials[2].textures[0]["image"]
    if defeat_image is not None:
        defeat_readback=profile.materials[1].textures[0]['image']
        if defeat_readback['name']!=defeat_image.name or defeat_readback['resource_sha256']!=sha256(defeat_image.resource):
            raise AssertionError('PS3 load readback defeat image differs from selected source')
    if level["name"] != source.levelbriefing_name:
        raise AssertionError("PS3 load readback levelbriefing name changed")
    if level["resource_sha256"] != sha256(image.resource):
        raise AssertionError("PS3 load readback levelbriefing resource changed")
    if (level["width"], level["height"], level["depth"], level["mip_count"]) != (
        image.width,
        image.height,
        image.depth,
        image.level_count,
    ):
        raise AssertionError("PS3 load readback dimensions/mips changed")
    if profile.trailing_nonzero:
        raise AssertionError("PS3 load capacity tail is not zero-filled")
    victory = profile.materials[0].textures[0]["image"] if victory_image is not None else None
    if victory is not None and victory["name"] != source.victory_image.name:
        raise AssertionError("PS3 load readback victory name changed")
    if victory is not None and victory["resource_sha256"] != sha256(victory_image.resource):
        raise AssertionError("PS3 load readback victory resource changed")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            handle.write(fastfile)
            temp_path = Path(handle.name)
        disk_doc = read_load_document(temp_path)
        disk_ff = read_ps3_fastfile(temp_path)
        require_retail_ps3_adler32(disk_ff)
        if disk_doc.rawfile.name != target_doc.rawfile.name or disk_doc.rawfile.data:
            raise AssertionError("disk readback RawFile differs")
        if output_path.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing output {output_path}")
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    final_ff = read_ps3_fastfile(output_path)
    final_doc = read_load_document(output_path)
    final_frames = require_retail_ps3_adler32(final_ff)
    if final_ff.zone != zone or final_doc.rawfile != target_doc.rawfile:
        raise AssertionError("committed PS3 load FastFile differs from validated bytes")

    reference_level_name = external_audit["levelbriefing_name"] if external_audit else None
    reference_specific_tokens = (
        {
            external_audit["rawfile_name"].casefold(),
            external_audit["levelbriefing_name"].casefold(),
        }
        if external_audit
        else set()
    ) - {target_doc.rawfile.name.casefold(), source.levelbriefing_name.casefold()}
    leaked_reference_names = sorted(
        token
        for token in reference_specific_tokens
        if (token.encode("latin-1") + b"\0") in zone.lower()
    )
    if leaked_reference_names:
        raise AssertionError(f"map-specific reference names leaked: {leaked_reference_names!r}")

    source_pc = source.levelbriefing_image.loaddef
    if source_pc is None:
        raise AssertionError("validated PC levelbriefing has no loadDef")
    report = {
        "passed": True,
        "output": str(output_path.resolve()),
        "output_filename": output_path.name,
        "map_name": source.map_name,
        "source_file_sha256": source.file_sha256,
        "source_zone_sha256": source.zone_sha256,
        "output_file_sha256": hashlib.sha256(fastfile).hexdigest().upper(),
        "output_zone_sha256": sha256(zone),
        "source_technique_sets": [tech.name for tech in source.technique_sets],
        "source_technique_count": len(source.technique_sets),
        "target_technique_sets": [profile.technique_name],
        "target_technique_count": 1,
        "target_materials": [material.name for material in profile.materials],
        "target_material_count": len(profile.materials),
        "target_asset_type_order": profile.asset_type_order,
        "ps3_version": final_ff.version,
        "zone_bytes": len(zone),
        "zone_memory_size": header.zone_memory_size,
        "block_sizes": list(header.block_sizes),
        "trailing_capacity": profile.trailing_capacity,
        "trailing_nonzero": profile.trailing_nonzero,
        "retail_adler32_frames": final_frames,
        "reference_retail_adler32_frames": (
            external_audit["retail_adler32_frames"] if external_audit else None
        ),
        "readback_passed": True,
        "standalone_capable": True,
        "standalone_build": external_audit is None,
        "retail_fastfile_required": False,
        "retail_reference_required_for_full_fidelity": True,
        "load_full_fidelity": load_full_fidelity,
        "full_fidelity_blockers": (
            (["PC _load.ff contains no victory pixels; no audited PS3 stock reference was supplied"]
             if 'neutral_victory_pixels' in compatibility_fallbacks else [])+
            (["PC defeat image pixels are missing; used native shared $defeatbackdrop"] if defeat_missing else [])
        ),
        "compatibility_fallbacks": compatibility_fallbacks,
        "defeat_source_image_name": source.defeat_image.name if source.defeat_image else None,
        "defeat_material_shared": defeat_image is None,
        "defeat_resource_sha256": sha256(defeat_image.resource) if defeat_image else None,
        "built_in_profile": {
            "name": BUILTIN_PS3_LOAD_UI_V1.name,
            "audit_source": BUILTIN_PS3_LOAD_UI_V1.audit_source,
            "audit_file_sha256": BUILTIN_PS3_LOAD_UI_V1.audit_file_sha256,
            "audit_zone_sha256": BUILTIN_PS3_LOAD_UI_V1.audit_zone_sha256,
            "structural_bytes": BUILTIN_PS3_LOAD_UI_V1.structural_bytes,
            "contains_image_payload": False,
            "contains_map_specific_names": False,
        },
        "external_schema_audit": external_audit,
        "levelbriefing_image_name": source.levelbriefing_name,
        "source_levelbriefing_image_name": source.levelbriefing_image.name,
        "levelbriefing_iwd": iwd.archive_path,
        "levelbriefing_iwd_entry": iwd.entry_name,
        "levelbriefing_iwi_sha256": iwd.content_sha256,
        "levelbriefing_resource_sha256": sha256(image.resource),
        "levelbriefing_resource_bytes": len(image.resource),
        "levelbriefing_unpadded_bytes": image.unpadded,
        "levelbriefing_padding_bytes": image.padding,
        "levelbriefing_format": image.source_format,
        "levelbriefing_ps3_format": image.ps3_format,
        "levelbriefing_dimensions": [image.width, image.height, image.depth],
        "levelbriefing_mip_count": image.level_count,
        "full_resolution_preserved": (image.width, image.height, image.depth)
        == (source_pc.width, source_pc.height, source_pc.depth),
        "full_mip_chain_preserved": image.level_count == source_pc.level_count,
        "victory_image_name": source.victory_image.name if source.victory_image else None,
        "victory_material_shared": source.victory_image is None,
        "victory_resource_provenance": victory_provenance,
        "victory_resource_sha256": sha256(victory_image.resource) if victory_image else None,
        "victory_resource_bytes": len(victory_image.resource) if victory_image else 0,
        "victory_unpadded_bytes": victory_image.unpadded if victory_image else 0,
        "victory_padding_bytes": victory_image.padding if victory_image else 0,
        "victory_dimensions": [victory_image.width, victory_image.height, victory_image.depth] if victory_image else None,
        "victory_mip_count": victory_image.level_count if victory_image else 0,
        "retail_image_payload_bytes_reused": (
            len(victory_image.resource) if external_audit else 0
        ),
        "map_specific_retail_image_payload_bytes_reused": 0,
        "rawfile_name": target_doc.rawfile.name,
        "rawfile_bytes": len(target_doc.rawfile.data),
        "source_diagnostic_bytes_omitted": len(source.rawfile_data),
        "load_rawfile_role": "empty_zone_marker",
        "template_levelbriefing_name": reference_level_name,
        "template_map_specific_names_reused": False,
        "template_map_specific_names_leaked": leaked_reference_names,
        "fresh_name_checks": {
            "rawfile_exact": profile.rawfile_name == f"{source.map_name}_load",
            "levelbriefing_exact": level["name"] == source.levelbriefing_name,
            "victory_exact_source_name": victory["name"] == source.victory_image.name if victory else profile.materials[0].name==source.materials[0].name,
            "external_map_names_absent": not leaked_reference_names,
        },
        "fresh_resource_checks": {
            "levelbriefing_exact_iwd": level["resource_sha256"] == sha256(image.resource),
            "victory_exact_selected_source": victory["resource_sha256"] == sha256(victory_image.resource) if victory else None,
            "victory_stock_reference_exact": bool(external_audit),
            "neutral_victory_fallback": victory_image is not None and external_audit is None,
            "map_specific_retail_payload_bytes_reused": 0,
        },
        "sound_assets_emitted": 0,
        "gsc_rawfiles_emitted": 0,
        "iwd_output_created": False,
        "output_iwd": None,
    }
    if output_adler_frames != final_frames:
        raise AssertionError("in-memory and committed PS3 frame counts differ")
    return report


__all__ = [
    "BUILTIN_PS3_LOAD_UI_V1",
    "IwdImageSource",
    "PcLoadSource",
    "Ps3LoadUiProfile",
    "convert_pc_load_to_ps3",
    "find_iwd_image",
    "parse_pc_load_source",
]

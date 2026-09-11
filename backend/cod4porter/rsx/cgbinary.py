"""Sony CgBinaryProgram container reading and writing for RSX shader assets.

Layout (all fields big-endian), as consumed by the vendored IW4Studio decoder
and the PS3 Cg runtime:

``CgBinaryProgram`` header, 0x20 bytes::

    +0x00 profile            0x1B5B vertex / 0x1B5C fragment (sce *_typeb)
    +0x04 binaryFormatRevision
    +0x08 totalSize
    +0x0C parameterCount
    +0x10 parameterArray     -> CgBinaryParameter[parameterCount], 0x30 each
    +0x14 program            -> CgBinaryVertexProgram / CgBinaryFragmentProgram
    +0x18 ucodeSize
    +0x1C ucode              (>= 0x40, 16-aligned)

``CgBinaryParameter``, 0x30 bytes::

    +0x00 type   +0x04 resource   +0x08 variability   +0x0C resourceIndex
    +0x10 name   +0x14 defaultValue   +0x18 embeddedConstants
    +0x1C semantic   +0x20 direction   +0x24 paramno
    +0x28 isReferenced   +0x2C isShared

``CgBinaryEmbeddedConstant``: u32 count, then count u32 ucode-relative byte
offsets of 16-byte inline-constant payloads (the runtime patch table).

``CgBinaryVertexProgram``, 0x18: instructionCount, instructionSlot,
registerCount, attributeInputMask, attributeOutputMask, userClipMask.

``CgBinaryFragmentProgram``: instructionCount, attributeInputMask,
partialTexType, texCoordsInputMask u16, texCoords2D u16, texCoordsCentroid
u16, registerCount u8, outputFromH0 u8, depthReplace u8, pixelFog u8.
The engine reads the four control bytes at descriptor +0x12..0x15 to build
NV4097_SET_SHADER_CONTROL, and the twelve bytes at +0x0C..0x17 are the exact
"program bytes" a PS3 MaterialPixelShader root carries at +0x0C.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

from . import nv40

PROFILE_VERTEX = 0x1B5B
PROFILE_FRAGMENT = 0x1B5C
ACCEPTED_PROFILES = frozenset((0x1807, 0x1B59, 0x1B5B, 0x1B5C, 0x1B5D, 0x1B5E))

BINARY_FORMAT_REVISION = 0x00000006

CG_TYPE_FLOAT4 = 1048          # CG_FLOAT4
CG_RESOURCE_C = 0x0882         # CG_C (constant register file)
CG_RESOURCE_UNDEFINED = 3256   # CG_UNDEFINED
CG_VARIABILITY_UNIFORM = 0x1006
CG_VARIABILITY_CONSTANT = 0x1007
CG_DIRECTION_IN = 0x1001       # CG_IN
CG_ERROR_NONE = 0

HEADER_SIZE = 0x20
PARAMETER_SIZE = 0x30
VERTEX_DESCRIPTOR_SIZE = 0x18
FRAGMENT_DESCRIPTOR_SIZE = 0x18


class CgBinaryError(ValueError):
    """The blob is not a valid Cg binary program for this domain."""


@dataclass(frozen=True)
class CgParameter:
    cg_type: int
    resource: int
    variability: int
    resource_index: int
    name: str
    default_value: tuple | None
    embedded_offsets: tuple[int, ...]   # ucode-relative patch sites
    semantic: str = ''
    direction: int = CG_DIRECTION_IN
    paramno: int = 0
    is_referenced: bool = True
    is_shared: bool = False


@dataclass
class CgProgram:
    profile: int
    parameters: tuple[CgParameter, ...]
    descriptor: dict
    ucode: bytes
    ucode_offset: int
    total_size: int
    raw: bytes

    @property
    def is_fragment(self) -> bool:
        return self.profile in (0x1B5C, 0x1B5E, 0x1807)


def _pad16(value: int) -> int:
    return (value + 15) & ~15


def build_vertex_binary(translation, *, embedded_constants=None) -> bytes:
    """Build the CgBinaryProgram blob for a translated vertex program.

    ``embedded_constants`` maps c-register -> float4 defaults the Cg runtime
    uploads at load time (D3D9 ``def`` constants).  Engine-set registers stay
    out of the table: the technique arguments carry them by register index.
    """
    ucode = translation.ucode
    parameters = []
    for register, value in sorted((embedded_constants or {}).items()):
        parameters.append(CgParameter(
            CG_TYPE_FLOAT4, CG_RESOURCE_C, CG_VARIABILITY_CONSTANT, register,
            f'c{register}', tuple(value), ()))
    descriptor = {
        'instruction_count': len(translation.instructions),
        'instruction_slot': 0,
        'register_count': translation.temp_count,
        'attribute_input_mask': translation.attribute_input_mask,
        'attribute_output_mask': translation.attribute_output_mask,
        'user_clip_mask': 0,
    }
    return _assemble(PROFILE_VERTEX, parameters, descriptor, ucode)


def build_fragment_binary(translation, *, register_defaults=None) -> tuple[bytes, dict]:
    """Build the fragment CgBinaryProgram blob.

    Returns ``(blob, parameter_index_by_register)`` where the mapping gives
    each D3D9 ``c#`` its CgBinaryParameter ordinal — the value a technique
    argument's ``dest`` must carry so the engine patches the right sites.
    """
    ucode = translation.ucode
    patch_offsets = translation.constant_patch_offsets()
    parameters = []
    parameter_index = {}
    for ordinal, register in enumerate(sorted(patch_offsets)):
        parameter_index[register] = ordinal
        default = (register_defaults or {}).get(
            register, translation.const_defaults.get(register, (0.0, 0.0, 0.0, 0.0)))
        parameters.append(CgParameter(
            CG_TYPE_FLOAT4, CG_RESOURCE_UNDEFINED, CG_VARIABILITY_UNIFORM, register,
            f'c{register}', tuple(default), tuple(patch_offsets[register]),
            paramno=ordinal))
    tex_input_mask = 0
    tex_2d_mask = 0
    for attr in translation.input_attrs:
        if nv40.FRAG_ATTR_TEX0 <= attr <= nv40.FRAG_ATTR_TEX0 + 9:
            tex_input_mask |= 1 << (attr - nv40.FRAG_ATTR_TEX0)
    centroid_mask = 0
    for attr in translation.center_weight_attrs:
        if nv40.FRAG_ATTR_TEX0 <= attr <= nv40.FRAG_ATTR_TEX0 + 9:
            centroid_mask |= 1 << (attr - nv40.FRAG_ATTR_TEX0)
    attribute_mask = 0
    for attr in translation.input_attrs:
        attribute_mask |= 1 << attr
    descriptor = {
        'instruction_count': len(translation.instructions),
        'attribute_input_mask': attribute_mask,
        'partial_tex_type': 0,
        'tex_coords_input_mask': tex_input_mask,
        'tex_coords_2d': tex_2d_mask,
        'tex_coords_centroid': centroid_mask,
        'register_count': max(2, translation.register_count),
        'output_from_h0': 0,          # FP32 exports from R0
        'depth_replace': 1 if translation.depth_export else 0,
        'pixel_fog': 1 if translation.uses_kill else 0,
    }
    return _assemble(PROFILE_FRAGMENT, parameters, descriptor, ucode), parameter_index


def _assemble(profile: int, parameters, descriptor: dict, ucode: bytes) -> bytes:
    header = bytearray(HEADER_SIZE)
    descriptor_offset = HEADER_SIZE
    descriptor_size = VERTEX_DESCRIPTOR_SIZE if profile == PROFILE_VERTEX else FRAGMENT_DESCRIPTOR_SIZE
    parameter_offset = descriptor_offset + descriptor_size
    cursor = parameter_offset + PARAMETER_SIZE * len(parameters)

    strings: list[tuple[int, bytes]] = []
    def intern(text: str) -> int:
        nonlocal cursor
        data = text.encode('ascii') + b'\0'
        offset = cursor
        strings.append((offset, data))
        cursor += len(data)
        return offset

    name_offsets = [intern(p.name) for p in parameters]
    semantic_offsets = [intern(p.semantic) if p.semantic else 0 for p in parameters]

    cursor = (cursor + 3) & ~3
    default_offsets = []
    defaults_blob = []
    for parameter in parameters:
        if parameter.default_value is None:
            default_offsets.append(0)
            continue
        default_offsets.append(cursor)
        defaults_blob.append((cursor, struct.pack('>4f', *parameter.default_value)))
        cursor += 16

    embedded_offsets = []
    embedded_blob = []
    for parameter in parameters:
        if not parameter.embedded_offsets:
            embedded_offsets.append(0)
            continue
        embedded_offsets.append(cursor)
        payload = struct.pack(f'>I{len(parameter.embedded_offsets)}I',
                              len(parameter.embedded_offsets), *parameter.embedded_offsets)
        embedded_blob.append((cursor, payload))
        cursor += len(payload)

    ucode_offset = _pad16(cursor)
    if ucode_offset < 0x40:
        ucode_offset = 0x40
    total_size = ucode_offset + len(ucode)

    struct.pack_into('>8I', header, 0, profile, BINARY_FORMAT_REVISION, total_size,
                     len(parameters), parameter_offset if parameters else 0,
                     descriptor_offset, len(ucode), ucode_offset)

    blob = bytearray(total_size)
    blob[:HEADER_SIZE] = header
    if profile == PROFILE_VERTEX:
        struct.pack_into('>6I', blob, descriptor_offset,
                         descriptor['instruction_count'], descriptor['instruction_slot'],
                         descriptor['register_count'], descriptor['attribute_input_mask'],
                         descriptor['attribute_output_mask'], descriptor['user_clip_mask'])
    else:
        struct.pack_into('>3I3H4B2x', blob, descriptor_offset,
                         descriptor['instruction_count'], descriptor['attribute_input_mask'],
                         descriptor['partial_tex_type'], descriptor['tex_coords_input_mask'],
                         descriptor['tex_coords_2d'], descriptor['tex_coords_centroid'],
                         descriptor['register_count'], descriptor['output_from_h0'],
                         descriptor['depth_replace'], descriptor['pixel_fog'])
    for index, parameter in enumerate(parameters):
        struct.pack_into('>12I', blob, parameter_offset + index * PARAMETER_SIZE,
                         parameter.cg_type, parameter.resource, parameter.variability,
                         parameter.resource_index, name_offsets[index],
                         default_offsets[index], embedded_offsets[index],
                         semantic_offsets[index], parameter.direction,
                         parameter.paramno & 0xFFFFFFFF,
                         1 if parameter.is_referenced else 0,
                         1 if parameter.is_shared else 0)
    for offset, data in strings:
        blob[offset:offset + len(data)] = data
    for offset, data in defaults_blob:
        blob[offset:offset + len(data)] = data
    for offset, data in embedded_blob:
        blob[offset:offset + len(data)] = data
    blob[ucode_offset:ucode_offset + len(ucode)] = ucode
    return bytes(blob)


def parse_cg_binary(blob: bytes) -> CgProgram:
    if len(blob) < HEADER_SIZE:
        raise CgBinaryError(f'blob of {len(blob)} bytes has no Cg header')
    profile, revision, total_size, parameter_count, parameter_offset, \
        descriptor_offset, ucode_size, ucode_offset = struct.unpack_from('>8I', blob, 0)
    if profile not in ACCEPTED_PROFILES:
        raise CgBinaryError(f'unknown Cg profile 0x{profile:04X}')
    if total_size > len(blob):
        raise CgBinaryError(f'declared size 0x{total_size:X} exceeds blob 0x{len(blob):X}')
    if ucode_offset < 0x40 or ucode_offset % 16 or ucode_offset + ucode_size > len(blob):
        raise CgBinaryError(f'invalid ucode window +0x{ucode_offset:X}/0x{ucode_size:X}')
    if parameter_count > 0xFFFF or parameter_offset + parameter_count * PARAMETER_SIZE > len(blob):
        raise CgBinaryError('invalid parameter table window')

    def cstr(offset: int) -> str:
        if offset == 0:
            return ''
        end = blob.find(b'\0', offset)
        if end < 0:
            raise CgBinaryError(f'unterminated string at +0x{offset:X}')
        return blob[offset:end].decode('latin-1')

    parameters = []
    for index in range(parameter_count):
        base = parameter_offset + index * PARAMETER_SIZE
        (cg_type, resource, variability, resource_index, name_offset,
         default_offset, embedded_offset, semantic_offset, direction,
         paramno, referenced, shared) = struct.unpack_from('>12I', blob, base)
        default = None
        if default_offset:
            if default_offset + 16 > len(blob):
                raise CgBinaryError(f'parameter {index} default outside blob')
            default = struct.unpack_from('>4f', blob, default_offset)
        offsets: tuple[int, ...] = ()
        if embedded_offset:
            if embedded_offset + 4 > len(blob):
                raise CgBinaryError(f'parameter {index} embedded table outside blob')
            count = struct.unpack_from('>I', blob, embedded_offset)[0]
            if count > 0xFFFF or embedded_offset + 4 + count * 4 > len(blob):
                raise CgBinaryError(f'parameter {index} embedded table invalid')
            offsets = struct.unpack_from(f'>{count}I', blob, embedded_offset + 4)
            for patch in offsets:
                if patch + 16 > ucode_size:
                    raise CgBinaryError(f'parameter {index} patch +0x{patch:X} outside ucode')
        parameters.append(CgParameter(
            cg_type, resource, variability, resource_index, cstr(name_offset),
            default, offsets, cstr(semantic_offset), direction, paramno,
            bool(referenced), bool(shared)))

    if profile in (PROFILE_VERTEX, 0x1B5D, 0x1B59):
        values = struct.unpack_from('>6I', blob, descriptor_offset)
        descriptor = dict(zip(('instruction_count', 'instruction_slot', 'register_count',
                               'attribute_input_mask', 'attribute_output_mask',
                               'user_clip_mask'), values))
    else:
        values = struct.unpack_from('>3I3H4B', blob, descriptor_offset)
        descriptor = dict(zip(('instruction_count', 'attribute_input_mask',
                               'partial_tex_type', 'tex_coords_input_mask',
                               'tex_coords_2d', 'tex_coords_centroid', 'register_count',
                               'output_from_h0', 'depth_replace', 'pixel_fog'), values))
    return CgProgram(profile, tuple(parameters), descriptor,
                     blob[ucode_offset:ucode_offset + ucode_size], ucode_offset,
                     total_size, bytes(blob))


def fragment_control_bytes(blob: bytes) -> bytes:
    """The 12 descriptor bytes (+0x0C..0x17) a PS3 pixel-shader root stores."""
    descriptor_offset = struct.unpack_from('>I', blob, 0x14)[0]
    if descriptor_offset + 0x18 > len(blob):
        raise CgBinaryError('fragment descriptor outside blob')
    return bytes(blob[descriptor_offset + 0x0C:descriptor_offset + 0x18])

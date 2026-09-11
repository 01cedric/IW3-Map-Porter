"""Full structural parse of IW3 PC MaterialTechniqueSets.

The PC structural reader (:mod:`cod4porter.pc_structure`) already resolves
every pointer field in the zone; this module reads the technique graph through
those resolved fields into typed records the compiler can consume.  Layouts
follow the vendored OpenAssetTools schema exactly (MaterialTechnique 0x1C
header view / passes 0x14, MaterialVertexDeclaration 0x64 with 16 routing
pairs, shader roots 0x10 with dword program sizes).
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

# MaterialShaderArgumentType (identical PC and PS3)
ARG_MATERIAL_VERTEX_CONST = 0
ARG_LITERAL_VERTEX_CONST = 1
ARG_MATERIAL_PIXEL_SAMPLER = 2
ARG_CODE_VERTEX_CONST = 3
ARG_CODE_PIXEL_SAMPLER = 4
ARG_CODE_PIXEL_CONST = 5
ARG_MATERIAL_PIXEL_CONST = 6
ARG_LITERAL_PIXEL_CONST = 7

PIXEL_CONST_TYPES = frozenset((ARG_CODE_PIXEL_CONST, ARG_MATERIAL_PIXEL_CONST,
                               ARG_LITERAL_PIXEL_CONST))
VERTEX_CONST_TYPES = frozenset((ARG_MATERIAL_VERTEX_CONST, ARG_LITERAL_VERTEX_CONST,
                                ARG_CODE_VERTEX_CONST))

PC_TECHNIQUE_SLOTS = 34
PC_PASS_SIZE = 20
PC_DECL_SIZE = 100
PC_DECL_ROUTING = 16
PC_SHADER_ROOT = 16

# PC MaterialStreamDestination -> D3D9 usage pairs (position, normal, colors,
# texcoords) and, in the same order, the RSX vertex attribute the PS3 build
# binds them to.  Sources are the identical enum on both platforms.
PC_STREAM_DEST_TO_RSX_ATTR = {
    0: 0,    # position   -> ATTR0
    1: 2,    # normal     -> ATTR2 (normal)
    2: 3,    # color[0]   -> ATTR3
    3: 4,    # color[1]   -> ATTR4
    **{4 + n: 8 + n for n in range(8)},  # texcoord[n] -> ATTR8+n
}

STREAM_SOURCE_NAMES = {
    0: 'position', 1: 'color', 2: 'texcoord0', 3: 'normal', 4: 'tangent',
    5: 'texcoord1', 6: 'texcoord2', 7: 'normalTransform0', 8: 'normalTransform1',
}


class PcTechsetParseError(ValueError):
    pass


@dataclass(frozen=True)
class PcShaderRecord:
    kind: str                # 'vertex' | 'pixel'
    name: str
    bytecode: bytes          # little-endian D3D9 token stream
    load_for_renderer: int
    root_offset: int


@dataclass(frozen=True)
class PcDeclRecord:
    stream_count: int
    has_optional_source: bool
    routing: tuple[tuple[int, int], ...]   # (source, pc_dest) pairs
    root_offset: int


@dataclass(frozen=True)
class PcArgRecord:
    type: int
    dest: int
    raw: int                       # little-endian union dword
    literal: tuple | None          # float4 for types 1/7

    @property
    def code_const(self) -> tuple[int, int, int]:
        """(index, firstRow, rowCount) for code-constant arguments."""
        return (self.raw & 0xFFFF, (self.raw >> 16) & 0xFF, (self.raw >> 24) & 0xFF)


@dataclass(frozen=True)
class PcPassRecord:
    decl: PcDeclRecord | None
    vertex_shader: PcShaderRecord | None
    pixel_shader: PcShaderRecord | None
    per_prim_arg_count: int
    per_obj_arg_count: int
    stable_arg_count: int
    custom_sampler_flags: int
    args: tuple[PcArgRecord, ...]


@dataclass(frozen=True)
class PcTechniqueRecord:
    name: str
    flags: int
    passes: tuple[PcPassRecord, ...]
    root_offset: int


@dataclass(frozen=True)
class PcTechniqueSetFull:
    name: str
    world_vertex_format: int
    techniques: tuple[PcTechniqueRecord | None, ...]   # 34 PC slots
    source_asset_index: int


class _Parser:
    def __init__(self, zone: bytes, pointer):
        self.zone = zone
        self.pointer = pointer     # field physical offset -> target/string/None
        self.techniques: dict[int, PcTechniqueRecord] = {}
        self.shaders: dict[int, PcShaderRecord] = {}
        self.decls: dict[int, PcDeclRecord] = {}

    def _target(self, field: int, what: str) -> int | None:
        value = self.pointer(field)
        if value is None:
            return None
        if not isinstance(value, int):
            raise PcTechsetParseError(f'{what} pointer at 0x{field:X} resolved to {value!r}')
        return value

    def _string(self, field: int, what: str) -> str:
        value = self.pointer(field)
        if not isinstance(value, str) or not value:
            raise PcTechsetParseError(f'{what} name at 0x{field:X} resolved to {value!r}')
        return value

    def u16(self, offset: int) -> int:
        return struct.unpack_from('<H', self.zone, offset)[0]

    def u32(self, offset: int) -> int:
        return struct.unpack_from('<I', self.zone, offset)[0]

    def technique(self, offset: int) -> PcTechniqueRecord:
        cached = self.techniques.get(offset)
        if cached is not None:
            return cached
        name = self._string(offset, 'MaterialTechnique')
        flags = self.u16(offset + 4)
        pass_count = self.u16(offset + 6)
        if not 1 <= pass_count <= 16:
            raise PcTechsetParseError(f'technique {name!r} pass count {pass_count}')
        passes = tuple(self.material_pass(offset + 8 + index * PC_PASS_SIZE, name, index)
                       for index in range(pass_count))
        record = PcTechniqueRecord(name, flags, passes, offset)
        self.techniques[offset] = record
        return record

    def material_pass(self, offset: int, technique: str, index: int) -> PcPassRecord:
        decl_offset = self._target(offset + 0, f'{technique} pass {index} vertexDecl')
        vertex_offset = self._target(offset + 4, f'{technique} pass {index} vertexShader')
        pixel_offset = self._target(offset + 8, f'{technique} pass {index} pixelShader')
        per_prim = self.zone[offset + 12]
        per_obj = self.zone[offset + 13]
        stable = self.zone[offset + 14]
        sampler_flags = self.zone[offset + 15]
        argument_count = per_prim + per_obj + stable
        args_offset = self._target(offset + 16, f'{technique} pass {index} args')
        if argument_count and args_offset is None:
            raise PcTechsetParseError(f'{technique} pass {index}: {argument_count} args but null table')
        args = tuple(self.argument(args_offset + i * 8) for i in range(argument_count)) if args_offset is not None else ()
        return PcPassRecord(
            self.decl(decl_offset) if decl_offset is not None else None,
            self.shader(vertex_offset, 'vertex') if vertex_offset is not None else None,
            self.shader(pixel_offset, 'pixel') if pixel_offset is not None else None,
            per_prim, per_obj, stable, sampler_flags, args)

    def argument(self, offset: int) -> PcArgRecord:
        argument_type = self.u16(offset)
        dest = self.u16(offset + 2)
        raw = self.u32(offset + 4)
        literal = None
        if argument_type in (ARG_LITERAL_VERTEX_CONST, ARG_LITERAL_PIXEL_CONST):
            literal_offset = self._target(offset + 4, 'literal constant')
            if literal_offset is None:
                raise PcTechsetParseError(f'literal argument at 0x{offset:X} has null value')
            literal = struct.unpack_from('<4f', self.zone, literal_offset)
        elif argument_type > 7:
            raise PcTechsetParseError(f'argument type {argument_type} at 0x{offset:X}')
        return PcArgRecord(argument_type, dest, raw, literal)

    def decl(self, offset: int) -> PcDeclRecord:
        cached = self.decls.get(offset)
        if cached is not None:
            return cached
        stream_count = self.zone[offset]
        has_optional = bool(self.zone[offset + 1])
        if stream_count > PC_DECL_ROUTING:
            raise PcTechsetParseError(f'vertex declaration at 0x{offset:X} stream count {stream_count}')
        routing = tuple((self.zone[offset + 4 + i * 2], self.zone[offset + 4 + i * 2 + 1])
                        for i in range(stream_count))
        record = PcDeclRecord(stream_count, has_optional, routing, offset)
        self.decls[offset] = record
        return record

    def shader(self, offset: int, kind: str) -> PcShaderRecord:
        cached = self.shaders.get(offset)
        if cached is not None:
            if cached.kind != kind:
                raise PcTechsetParseError(f'shader at 0x{offset:X} used as {kind} and {cached.kind}')
            return cached
        name = self._string(offset, f'{kind} shader')
        program_offset = self._target(offset + 8, f'{kind} shader program')
        if program_offset is None:
            raise PcTechsetParseError(f'{kind} shader {name!r} has no program')
        words = self.u16(offset + 12)
        load_for_renderer = self.u16(offset + 14)
        if words < 2:
            raise PcTechsetParseError(f'{kind} shader {name!r} program of {words} dwords')
        bytecode = bytes(self.zone[program_offset:program_offset + words * 4])
        if len(bytecode) != words * 4:
            raise PcTechsetParseError(f'{kind} shader {name!r} program exceeds zone')
        stage = struct.unpack_from('<I', bytecode, 0)[0] >> 16
        expected = 0xFFFE if kind == 'vertex' else 0xFFFF
        if stage != expected:
            raise PcTechsetParseError(
                f'{kind} shader {name!r} version stage 0x{stage:04X} != 0x{expected:04X}')
        record = PcShaderRecord(kind, name, bytecode, load_for_renderer, offset)
        self.shaders[offset] = record
        return record


def parse_full_techniqueset(zone: bytes, pointer, techset, *, name: str | None = None,
                            source_asset_index: int | None = None) -> PcTechniqueSetFull:
    """Parse one PC techset completely.

    ``pointer`` is ``StructuralIndex.pointer`` (or an equivalent callable that
    returns the resolved target offset, string value, or None for a pointer
    field's physical offset).  ``techset`` is the light
    :class:`cod4porter.pc_technique.TechniqueSet` row.
    """
    parser = _Parser(zone, pointer)
    techniques: list[PcTechniqueRecord | None] = []
    for slot in range(PC_TECHNIQUE_SLOTS):
        field = techset.root_offset + 12 + slot * 4
        target = parser._target(field, f'technique slot {slot}')
        techniques.append(parser.technique(target) if target is not None else None)
    return PcTechniqueSetFull(
        name if name is not None else techset.name,
        techset.world_vertex_format,
        tuple(techniques),
        techset.source_asset_index if source_asset_index is None else source_asset_index)

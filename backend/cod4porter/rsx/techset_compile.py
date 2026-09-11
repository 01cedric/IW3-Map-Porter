"""Compile a parsed PC MaterialTechniqueSet into PS3-ready technique data.

This is the general PC-to-RSX path: every shader is translated to RSX
microcode, wrapped in a CgBinaryProgram, and every pass's declaration and
argument table is rebuilt for the PS3 register model.  The result carries
both the serializable payloads and the decoded programs, so the same object
feeds the zone writer and the software preview executor.

Technique slots: PC CoD4 has 34 technique types, the PS3 build 26.  The
difference is exactly the hardware-instancing family the console renderer
does not have (LIT_INSTANCED* 0x0E..0x14 and DEBUG_BUMPMAP_INSTANCED 0x21);
removing those from the PC list yields the 26-slot PS3 order.  The
calibration tool verifies this against retail PS3 zones on installations
that have them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import struct

from . import cgbinary, d3d9, nv40
from .techset_parse import (
    ARG_CODE_PIXEL_CONST, ARG_CODE_PIXEL_SAMPLER, ARG_CODE_VERTEX_CONST,
    ARG_LITERAL_PIXEL_CONST, ARG_LITERAL_VERTEX_CONST, ARG_MATERIAL_PIXEL_SAMPLER,
    PC_STREAM_DEST_TO_RSX_ATTR, PIXEL_CONST_TYPES, VERTEX_CONST_TYPES,
    PcArgRecord, PcPassRecord, PcTechniqueRecord, PcTechniqueSetFull,
)
from .translate import (
    RsxTranslationError, compile_pixel_shader, compile_vertex_shader,
)

PS3_TECHNIQUE_SLOTS = 26
# PC technique-type index serving each PS3 slot.
PS3_SLOT_TO_PC = tuple(list(range(0x0E)) + list(range(0x15, 0x21)))
assert len(PS3_SLOT_TO_PC) == PS3_TECHNIQUE_SLOTS

PS3_DECL_ROUTING = 13

# A single RSX shader's CgBinary blob is written verbatim and its u32 length
# sits in the shader root; the console streams exactly that many bytes.  Real
# IW3 fragment/vertex programs are a few KiB (the test pipeline emits ~320 B).
# A blob far past any plausible shader is a runaway translation: its length
# field would make the loader over-read the stream and then dereference the
# bytes that follow as the next structural pointer - the mp_wmd_night
# `unmapped address 0x3f03` fault.  Refuse to serialize it; the techset then
# fails closed onto substitution/omission instead of freezing the console.
MAX_SHADER_BLOB_BYTES = 256 * 1024


class TechsetCompileError(ValueError):
    """A techset (or one of its techniques) cannot be compiled; the message
    names the technique, pass and construct."""

    def __init__(self, message: str, *, technique: str = '', detail: str = ''):
        super().__init__(message)
        self.technique = technique
        self.detail = detail


@dataclass(frozen=True)
class CompiledShader:
    kind: str                       # 'vertex' | 'pixel'
    name: str                       # PS3 asset name (content-derived)
    source_name: str                # PC shader name
    blob: bytes                     # CgBinaryProgram
    translation: object             # VertexTranslation | FragmentTranslation
    parameter_index: dict           # pixel: c# -> parameter ordinal
    content_key: str

    @property
    def control_bytes(self) -> bytes:
        if self.kind != 'pixel':
            raise ValueError('only pixel shaders carry root control bytes')
        return cgbinary.fragment_control_bytes(self.blob)


@dataclass(frozen=True)
class CompiledDecl:
    stream_count: int
    has_optional_source: bool
    routing: tuple[tuple[int, int], ...]     # (source, rsx_attr)
    content_key: str

    def serialized(self) -> bytes:
        out = bytearray(2 + PS3_DECL_ROUTING * 2)
        out[0] = self.stream_count
        out[1] = 1 if self.has_optional_source else 0
        for index, (source, dest) in enumerate(self.routing):
            out[2 + index * 2] = source
            out[3 + index * 2] = dest
        return bytes(out)


@dataclass(frozen=True)
class CompiledArg:
    type: int
    dest: int
    raw_be: int                     # big-endian union dword (non-literals)
    literal: tuple | None


@dataclass(frozen=True)
class CompiledPass:
    decl: CompiledDecl | None
    vertex_shader: CompiledShader
    pixel_shader: CompiledShader
    per_prim_arg_count: int
    per_obj_arg_count: int
    stable_arg_count: int
    custom_sampler_flags: int
    args: tuple[CompiledArg, ...]
    dropped_args: tuple[str, ...]


@dataclass(frozen=True)
class CompiledTechnique:
    name: str
    flags: int
    passes: tuple[CompiledPass, ...]
    pc_slot: int
    content_key: str


@dataclass
class CompiledTechniqueSet:
    name: str
    world_vertex_format: int
    slots: tuple[CompiledTechnique | None, ...]      # 26 PS3 slots
    dropped_pc_slots: tuple[int, ...]
    notes: tuple[str, ...]
    source_asset_index: int

    def serialized_size_estimate(self) -> int:
        total = 0x70 + len(self.name) + 1
        seen: set[str] = set()
        for technique in self.slots:
            if technique is None or technique.content_key in seen:
                continue
            seen.add(technique.content_key)
            total += 8 + len(technique.name) + 1 + len(technique.passes) * 0x18
            for pass_ in technique.passes:
                total += 2 + PS3_DECL_ROUTING * 2
                total += len(pass_.args) * 8
                total += sum(16 for argument in pass_.args if argument.literal is not None)
                for shader in (pass_.vertex_shader, pass_.pixel_shader):
                    if shader.content_key in seen:
                        continue
                    seen.add(shader.content_key)
                    root = 0x0C if shader.kind == 'vertex' else 0x18
                    total += root + len(shader.name) + 1 + len(shader.blob) + 16
        return total

    def unique_shaders(self) -> dict:
        out: dict[str, CompiledShader] = {}
        for technique in self.slots:
            if technique is None:
                continue
            for pass_ in technique.passes:
                for shader in (pass_.vertex_shader, pass_.pixel_shader):
                    out.setdefault(shader.content_key, shader)
        return out


def _shader_asset_name(kind: str, source_name: str, blob: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(blob).hexdigest()[:12]
    prefix = 'vs' if kind == 'vertex' else 'ps'
    stem = ''.join(c if c.isalnum() or c in '_' else '_' for c in source_name)[:40]
    return f'iw3c_{prefix}_{stem}_{digest}', digest


def _guard_blob_size(kind: str, source_name: str, blob: bytes) -> None:
    if len(blob) > MAX_SHADER_BLOB_BYTES:
        raise TechsetCompileError(
            f'{kind} shader {source_name!r} compiled to {len(blob)} bytes, past the '
            f'{MAX_SHADER_BLOB_BYTES}-byte ceiling for a single RSX program; refusing to '
            f'serialize a length field that would make the loader over-read the stream',
            detail=f'{kind}_blob_{len(blob)}')


def _compile_vertex(record) -> CompiledShader:
    program = d3d9.disassemble(record.bytecode)
    translation = compile_vertex_shader(program)
    defaults = {definition.register_number: definition.value for definition in program.defs}
    blob = cgbinary.build_vertex_binary(translation, embedded_constants=defaults)
    _guard_blob_size('vertex', record.name, blob)
    name, digest = _shader_asset_name('vertex', record.name, blob)
    return CompiledShader('vertex', name, record.name, blob, translation, {},
                          f'vs:{digest}:{len(blob)}')


def _compile_pixel(record) -> CompiledShader:
    program = d3d9.disassemble(record.bytecode)
    translation = compile_pixel_shader(program)
    blob, parameter_index = cgbinary.build_fragment_binary(translation)
    _guard_blob_size('pixel', record.name, blob)
    name, digest = _shader_asset_name('pixel', record.name, blob)
    return CompiledShader('pixel', name, record.name, blob, translation, parameter_index,
                          f'ps:{digest}:{len(blob)}')


def _compile_decl(record) -> CompiledDecl:
    routing = []
    for source, pc_dest in record.routing:
        rsx_dest = PC_STREAM_DEST_TO_RSX_ATTR.get(pc_dest)
        if rsx_dest is None:
            raise TechsetCompileError(f'vertex declaration routes to unknown PC destination {pc_dest}')
        if source > 8:
            raise TechsetCompileError(f'vertex declaration reads unknown source stream {source}')
        routing.append((source, rsx_dest))
    if len(routing) > PS3_DECL_ROUTING:
        raise TechsetCompileError(f'vertex declaration has {len(routing)} routes; PS3 stores {PS3_DECL_ROUTING}')
    key = hashlib.sha256(bytes(v for pair in routing for v in pair) +
                         bytes((record.stream_count, record.has_optional_source))).hexdigest()[:12]
    return CompiledDecl(record.stream_count, record.has_optional_source, tuple(routing), key)


def _translate_args(pc_pass: PcPassRecord, pixel: CompiledShader,
                    vertex: CompiledShader) -> tuple[tuple[CompiledArg, ...], tuple[int, int, int], tuple[str, ...]]:
    slices = (
        pc_pass.args[:pc_pass.per_prim_arg_count],
        pc_pass.args[pc_pass.per_prim_arg_count:pc_pass.per_prim_arg_count + pc_pass.per_obj_arg_count],
        pc_pass.args[pc_pass.per_prim_arg_count + pc_pass.per_obj_arg_count:],
    )
    vertex_reads = set(vertex.translation.const_reads)
    dropped: list[str] = []
    out_slices: list[list[CompiledArg]] = []
    for slice_args in slices:
        out: list[CompiledArg] = []
        for argument in slice_args:
            if argument.type in PIXEL_CONST_TYPES:
                if (argument.type == ARG_CODE_PIXEL_CONST
                        and max(1, argument.code_const[2]) > 1):
                    raise TechsetCompileError(
                        f'pixel code constant c{argument.dest} spans '
                        f'{argument.code_const[2]} rows; the PS3 parameter patch '
                        'model is proven for single rows only')
                parameter = pixel.parameter_index.get(argument.dest)
                if parameter is None:
                    dropped.append(
                        f'type{argument.type} pixel constant c{argument.dest} is never read by the translated program')
                    continue
                out.append(CompiledArg(argument.type, parameter,
                                       _union_be(argument), argument.literal))
                continue
            if argument.type in VERTEX_CONST_TYPES:
                rows = 1
                if argument.type == ARG_CODE_VERTEX_CONST:
                    rows = max(1, argument.code_const[2])
                if argument.dest + rows - 1 > 467:
                    raise TechsetCompileError(
                        f'vertex constant argument c{argument.dest} (+{rows} rows) exceeds the RSX file')
                if not any(argument.dest + row in vertex_reads for row in range(rows)):
                    dropped.append(
                        f'type{argument.type} vertex constant c{argument.dest} is never read by the translated program')
                    continue
                out.append(CompiledArg(argument.type, argument.dest,
                                       _union_be(argument), argument.literal))
                continue
            if argument.type in (ARG_MATERIAL_PIXEL_SAMPLER, ARG_CODE_PIXEL_SAMPLER):
                if argument.dest > 15:
                    raise TechsetCompileError(f'sampler argument s{argument.dest}')
                out.append(CompiledArg(argument.type, argument.dest,
                                       _union_be(argument), None))
                continue
            raise TechsetCompileError(f'argument type {argument.type} is outside the IW3 domain')
        out_slices.append(out)
    counts = tuple(len(chunk) for chunk in out_slices)
    return tuple(x for chunk in out_slices for x in chunk), counts, tuple(dropped)


def _union_be(argument: PcArgRecord) -> int:
    if argument.type in (ARG_LITERAL_VERTEX_CONST, ARG_LITERAL_PIXEL_CONST):
        return 0  # pointer written by the serializer
    if argument.type in (ARG_CODE_VERTEX_CONST, 5):
        index, first_row, row_count = argument.code_const
        return ((index & 0xFFFF) << 16) | ((first_row & 0xFF) << 8) | (row_count & 0xFF)
    return argument.raw & 0xFFFFFFFF


def _compile_pass(pc_pass: PcPassRecord, technique_name: str, index: int,
                  shader_cache: dict) -> CompiledPass:
    if pc_pass.vertex_shader is None or pc_pass.pixel_shader is None:
        raise TechsetCompileError(
            f'technique {technique_name!r} pass {index} has no shader pair',
            technique=technique_name)
    try:
        vertex = shader_cache.get(('v', pc_pass.vertex_shader.root_offset))
        if vertex is None:
            vertex = _compile_vertex(pc_pass.vertex_shader)
            shader_cache[('v', pc_pass.vertex_shader.root_offset)] = vertex
        pixel = shader_cache.get(('p', pc_pass.pixel_shader.root_offset))
        if pixel is None:
            pixel = _compile_pixel(pc_pass.pixel_shader)
            shader_cache[('p', pc_pass.pixel_shader.root_offset)] = pixel
    except (RsxTranslationError, d3d9.D3d9Error) as error:
        raise TechsetCompileError(
            f'technique {technique_name!r} pass {index}: {error}',
            technique=technique_name, detail=str(error)) from error
    decl = _compile_decl(pc_pass.decl) if pc_pass.decl is not None else None
    args, counts, dropped = _translate_args(pc_pass, pixel, vertex)
    return CompiledPass(decl, vertex, pixel, counts[0], counts[1], counts[2],
                        pc_pass.custom_sampler_flags, args, dropped)


def compile_techniqueset(full: PcTechniqueSetFull) -> CompiledTechniqueSet:
    shader_cache: dict = {}
    technique_cache: dict[int, CompiledTechnique] = {}
    notes: list[str] = []
    slots: list[CompiledTechnique | None] = []
    for ps3_slot, pc_slot in enumerate(PS3_SLOT_TO_PC):
        record = full.techniques[pc_slot]
        if record is None:
            slots.append(None)
            continue
        cached = technique_cache.get(record.root_offset)
        if cached is None:
            passes = tuple(_compile_pass(p, record.name, i, shader_cache)
                           for i, p in enumerate(record.passes))
            key_material = record.name.encode('latin-1') + struct.pack('<HH', record.flags, len(passes))
            for pass_ in passes:
                key_material += pass_.vertex_shader.content_key.encode()
                key_material += pass_.pixel_shader.content_key.encode()
                # Fold declaration and argument tables in too: two techniques
                # sharing name/flags/shaders but differing in args or decl
                # must never merge into one emitted technique.
                key_material += (pass_.decl.content_key.encode()
                                 if pass_.decl is not None else b'-')
                key_material += struct.pack('<BBBB', pass_.per_prim_arg_count,
                                            pass_.per_obj_arg_count,
                                            pass_.stable_arg_count,
                                            pass_.custom_sampler_flags & 0xFF)
                for argument in pass_.args:
                    key_material += struct.pack('<HHI', argument.type, argument.dest,
                                                argument.raw_be & 0xFFFFFFFF)
                    if argument.literal is not None:
                        key_material += struct.pack('<4f', *argument.literal)
            cached = CompiledTechnique(
                record.name, record.flags, passes, pc_slot,
                hashlib.sha256(key_material).hexdigest()[:12])
            technique_cache[record.root_offset] = cached
            for pass_index, pass_ in enumerate(passes):
                for message in pass_.dropped_args:
                    notes.append(f'{record.name} pass {pass_index}: dropped {message}')
        slots.append(cached)
    dropped_pc = tuple(slot for slot in range(len(full.techniques))
                       if slot not in PS3_SLOT_TO_PC and full.techniques[slot] is not None)
    for slot in dropped_pc:
        notes.append(f'PC technique slot 0x{slot:X} ({full.techniques[slot].name}) has no PS3 technique type')
    return CompiledTechniqueSet(full.name.lstrip(','), full.world_vertex_format,
                                tuple(slots), dropped_pc, tuple(notes),
                                full.source_asset_index)

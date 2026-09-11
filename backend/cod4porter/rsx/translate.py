"""D3D9 SM2/SM3 -> RSX (NV40) shader translation.

The translator maps IW3 PC shader programs onto RSX microcode while keeping
the register identities the IW3 material system depends on:

* vertex float constants keep their ``c#`` index (RSX has 468 slots, D3D9
  used at most 256, so the identity mapping always fits);
* samplers keep their ``s#`` index as the RSX texture unit;
* pixel float constants become inline fragment constants, one 16-byte
  payload per reading instruction, and the translator reports every payload
  offset per ``c#`` so the container writer can emit the runtime patch table
  the engine uses to bind material/code/literal values.

Anything outside the proven domain (flow control, predication, SM1-era
modifiers, address-register indexing) fails closed with the exact construct
named, so unsupported techniques are reported instead of miscompiled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import d3d9
from .d3d9 import D3d9Program, Instruction, SourceParam
from . import nv40
from .nv40 import (
    FragInstr, FragSource, VertInstr, VertSource,
    FRAG_REG_CONST, FRAG_REG_INPUT, FRAG_REG_TEMP,
    VERT_REG_CONST, VERT_REG_INPUT, VERT_REG_TEMP,
)

SWZ_XYZW = (0, 1, 2, 3)


class RsxTranslationError(ValueError):
    """A shader construct outside the supported translation domain."""

    def __init__(self, message: str, blockers: tuple[str, ...] = ()):  # noqa: D107
        super().__init__(message)
        self.blockers = blockers or (message,)


# --- semantics --------------------------------------------------------------

# D3D9 vertex declaration usage -> RSX vertex input attribute.
VERTEX_USAGE_TO_ATTR = {
    (d3d9.USAGE_POSITION, 0): nv40.VERT_ATTR_POSITION,
    (d3d9.USAGE_BLENDWEIGHT, 0): nv40.VERT_ATTR_WEIGHT,
    (d3d9.USAGE_NORMAL, 0): nv40.VERT_ATTR_NORMAL,
    (d3d9.USAGE_COLOR, 0): nv40.VERT_ATTR_COL0,
    (d3d9.USAGE_COLOR, 1): nv40.VERT_ATTR_COL1,
    (d3d9.USAGE_PSIZE, 0): nv40.VERT_ATTR_PSIZE,
    (d3d9.USAGE_DEPTH, 0): nv40.VERT_ATTR_FOG,
    (d3d9.USAGE_FOG, 0): nv40.VERT_ATTR_FOG,
    (d3d9.USAGE_TANGENT, 0): nv40.VERT_ATTR_ATTR7,
    **{(d3d9.USAGE_TEXCOORD, index): nv40.VERT_ATTR_TEX0 + index for index in range(8)},
}

# D3D9 output usage -> RSX vertex result register.
VERTEX_USAGE_TO_RESULT = {
    (d3d9.USAGE_POSITION, 0): nv40.VERT_RESULT_POSITION,
    (d3d9.USAGE_COLOR, 0): nv40.VERT_RESULT_COL0,
    (d3d9.USAGE_COLOR, 1): nv40.VERT_RESULT_COL1,
    (d3d9.USAGE_FOG, 0): nv40.VERT_RESULT_FOG,
    (d3d9.USAGE_PSIZE, 0): nv40.VERT_RESULT_PSIZE,
    **{(d3d9.USAGE_TEXCOORD, index): nv40.VERT_RESULT_TEX0 + index for index in range(9)},
}

# D3D9 pixel input usage -> RSX fragment input attribute.
PIXEL_USAGE_TO_ATTR = {
    (d3d9.USAGE_COLOR, 0): nv40.FRAG_ATTR_COL0,
    (d3d9.USAGE_COLOR, 1): nv40.FRAG_ATTR_COL1,
    (d3d9.USAGE_FOG, 0): nv40.FRAG_ATTR_FOGC,
    **{(d3d9.USAGE_TEXCOORD, index): nv40.FRAG_ATTR_TEX0 + index for index in range(10)},
}

# RSX vertex result -> the fragment attribute the rasterizer feeds from it.
RESULT_FEEDS_ATTR = {
    nv40.VERT_RESULT_COL0: nv40.FRAG_ATTR_COL0,
    nv40.VERT_RESULT_COL1: nv40.FRAG_ATTR_COL1,
    nv40.VERT_RESULT_FOG: nv40.FRAG_ATTR_FOGC,
    **{nv40.VERT_RESULT_TEX0 + index: nv40.FRAG_ATTR_TEX0 + index for index in range(9)},
}

# RSX vertex result -> CELL_GCM_ATTRIB_OUTPUT_MASK_* bit.  This is the
# cellGcmCgGetAttribOutputMask / NV4097_SET_VERTEX_ATTRIB_OUTPUT_MASK layout:
# FRONTDIFFUSE=1<<0, FRONTSPECULAR=1<<1, BACKDIFFUSE=1<<2, BACKSPECULAR=1<<3,
# FOG=1<<4, POINTSIZE=1<<5, UC0..5=1<<6..11, TEX8=1<<12, TEX9=1<<13,
# TEX0..7=1<<14..21.  The position result has no bit.
RESULT_TO_GCM_OUTPUT_BIT = {
    nv40.VERT_RESULT_COL0: 1 << 0,
    nv40.VERT_RESULT_COL1: 1 << 1,
    nv40.VERT_RESULT_BFC0: 1 << 2,
    nv40.VERT_RESULT_BFC1: 1 << 3,
    nv40.VERT_RESULT_FOG: 1 << 4,
    nv40.VERT_RESULT_PSIZE: 1 << 5,
    **{nv40.VERT_RESULT_TEX0 + index: 1 << (14 + index) for index in range(8)},
    nv40.VERT_RESULT_TEX0 + 8: 1 << 12,
}

_SUPPORTED_SRCMODS = {
    d3d9.SRCMOD_NONE: (False, False),
    d3d9.SRCMOD_NEG: (True, False),
    d3d9.SRCMOD_ABS: (False, True),
    d3d9.SRCMOD_ABSNEG: (True, True),
}


def _swizzled(base: tuple[int, int, int, int], swizzle: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    return tuple(base[c] for c in swizzle)


@dataclass
class _TempAllocator:
    limit: int
    used: set = field(default_factory=set)

    def reserve(self, index: int) -> None:
        self.used.add(index)

    def scratch(self) -> int:
        for index in range(self.limit):
            if index not in self.used:
                self.used.add(index)
                return index
        raise RsxTranslationError(f'out of scratch registers (limit {self.limit})')

    @property
    def count(self) -> int:
        return max(self.used) + 1 if self.used else 0


# --- vertex -----------------------------------------------------------------

@dataclass
class VertexTranslation:
    instructions: list
    input_attrs: tuple[int, ...]
    attr_by_usage: dict
    results: tuple[int, ...]
    const_reads: tuple[int, ...]
    temp_count: int
    notes: tuple[str, ...]

    @property
    def ucode(self) -> bytes:
        return nv40.encode_vertex_program(self.instructions)

    @property
    def attribute_input_mask(self) -> int:
        mask = 0
        for attr in self.input_attrs:
            mask |= 1 << attr
        return mask

    @property
    def attribute_output_mask(self) -> int:
        mask = 0
        for result in self.results:
            mask |= RESULT_TO_GCM_OUTPUT_BIT.get(result, 0)
        return mask


class _VertexBuilder:
    def __init__(self, program: D3d9Program):
        self.program = program
        self.out: list[VertInstr] = []
        self.notes: list[str] = []
        self.const_reads: set[int] = set()
        self.results: set[int] = set()
        self.input_attrs: set[int] = set()
        self.attr_by_usage: dict[tuple[int, int], int] = {}
        self.temps = _TempAllocator(32)
        self.input_map: dict[int, int] = {}
        self.output_map: dict[int, int] = {}
        for instruction in program.instructions:
            for operand in list(instruction.sources) + ([instruction.dest] if instruction.dest else []):
                if operand is not None and operand.register_type == d3d9.REG_TEMP:
                    self.temps.reserve(operand.register_number)
        for declaration in program.declarations:
            dest = declaration.dest
            if dest.register_type == d3d9.REG_INPUT:
                key = (declaration.usage, declaration.usage_index)
                attr = VERTEX_USAGE_TO_ATTR.get(key)
                if attr is None:
                    raise RsxTranslationError(
                        f'vertex input usage {d3d9.USAGE_NAMES.get(declaration.usage, declaration.usage)}'
                        f'{declaration.usage_index} has no RSX attribute mapping')
                self.input_map[dest.register_number] = attr
                self.attr_by_usage[key] = attr
            elif dest.register_type == d3d9.REG_TEXCRDOUT and self.program.major >= 3:
                key = (declaration.usage, declaration.usage_index)
                result = VERTEX_USAGE_TO_RESULT.get(key)
                if result is None:
                    raise RsxTranslationError(
                        f'vertex output usage {d3d9.USAGE_NAMES.get(declaration.usage, declaration.usage)}'
                        f'{declaration.usage_index} has no RSX result mapping')
                self.output_map[dest.register_number] = result

    # -- operand handling --

    def _source(self, param: SourceParam) -> tuple[VertSource, bool, int | None, int | None]:
        """Returns (source, abs, const_index, input_attr)."""
        if param.relative:
            raise RsxTranslationError('vertex address-register (a0) indexing is unsupported')
        if param.modifier not in _SUPPORTED_SRCMODS:
            raise RsxTranslationError(
                f'source modifier {d3d9.SRCMOD_NAMES.get(param.modifier, param.modifier)!r} is SM1-only')
        negate, absolute = _SUPPORTED_SRCMODS[param.modifier]
        rt = param.register_type
        if rt == d3d9.REG_TEMP:
            return VertSource(VERT_REG_TEMP, param.register_number, param.swizzle, negate), absolute, None, None
        if rt == d3d9.REG_CONST:
            if param.register_number > 467:
                raise RsxTranslationError(f'vertex constant c{param.register_number} exceeds the RSX register file')
            self.const_reads.add(param.register_number)
            return VertSource(VERT_REG_CONST, 0, param.swizzle, negate), absolute, param.register_number, None
        if rt == d3d9.REG_INPUT:
            attr = self.input_map.get(param.register_number)
            if attr is None:
                raise RsxTranslationError(f'vertex input v{param.register_number} was never declared')
            self.input_attrs.add(attr)
            return VertSource(VERT_REG_INPUT, 0, param.swizzle, negate), absolute, None, attr
        raise RsxTranslationError(
            f'vertex source register file {d3d9.REGISTER_NAMES.get(rt, rt)} is unsupported')

    def _dest(self, instruction: Instruction) -> tuple[int | None, int | None, tuple[bool, bool, bool, bool], bool]:
        dest = instruction.dest
        mask = tuple(bool(dest.write_mask >> c & 1) for c in range(4))
        saturate = dest.saturate
        rt = dest.register_type
        if rt == d3d9.REG_TEMP:
            return dest.register_number, None, mask, saturate
        if rt == d3d9.REG_RASTOUT:
            result = {d3d9.RASTOUT_POSITION: nv40.VERT_RESULT_POSITION,
                      d3d9.RASTOUT_FOG: nv40.VERT_RESULT_FOG,
                      d3d9.RASTOUT_POINT_SIZE: nv40.VERT_RESULT_PSIZE}.get(dest.register_number)
            if result is None:
                raise RsxTranslationError(f'unknown rasterizer output {dest.register_number}')
            return None, result, mask, saturate
        if rt == d3d9.REG_ATTROUT:
            if dest.register_number > 1:
                raise RsxTranslationError(f'vertex color output oD{dest.register_number}')
            return None, nv40.VERT_RESULT_COL0 + dest.register_number, mask, saturate
        if rt == d3d9.REG_TEXCRDOUT:
            if self.program.major >= 3:
                result = self.output_map.get(dest.register_number)
                if result is None:
                    raise RsxTranslationError(f'vertex output o{dest.register_number} was never declared')
            else:
                if dest.register_number > 7:
                    raise RsxTranslationError(f'vertex texture output oT{dest.register_number}')
                result = nv40.VERT_RESULT_TEX0 + dest.register_number
            return None, result, mask, saturate
        raise RsxTranslationError(
            f'vertex destination register file {d3d9.REGISTER_NAMES.get(rt, rt)} is unsupported')

    # -- emission --

    def _flush_shared_operands(self, prepared) -> tuple[list, int, int]:
        """Enforce one constant register and one input attribute per instruction."""
        const_index: int | None = None
        input_attr: int | None = None
        rewritten = []
        for source, absolute, const, attr in prepared:
            if const is not None:
                if const_index is None or const == const_index:
                    const_index = const
                else:
                    scratch = self.temps.scratch()
                    self._emit_vec(nv40.VEC_OP_MOV,
                                   [(VertSource(VERT_REG_CONST, 0, SWZ_XYZW, False), False, const, None)],
                                   temp=scratch, mask=(True,) * 4)
                    source = VertSource(VERT_REG_TEMP, scratch, source.swizzle, source.negate)
                    const = None
            if attr is not None:
                if input_attr is None or attr == input_attr:
                    input_attr = attr
                else:
                    scratch = self.temps.scratch()
                    self._emit_vec(nv40.VEC_OP_MOV,
                                   [(VertSource(VERT_REG_INPUT, 0, SWZ_XYZW, False), False, None, attr)],
                                   temp=scratch, mask=(True,) * 4)
                    source = VertSource(VERT_REG_TEMP, scratch, source.swizzle, source.negate)
                    attr = None
            rewritten.append((source, absolute, const, attr))
        return rewritten, (0 if const_index is None else const_index), (0 if input_attr is None else input_attr)

    def _emit_vec(self, opcode, prepared, *, temp=None, result=None,
                  mask=(True,) * 4, saturate=False, slots=None):
        prepared, const_src, input_attr = self._flush_shared_operands(prepared)
        slots = slots if slots is not None else nv40.vec_source_slots(opcode)
        if len(prepared) != len(slots):
            raise RsxTranslationError(f'vector opcode {opcode:#x} slot mismatch')
        sources = [VertSource(), VertSource(), VertSource()]
        abs_flags = [False, False, False]
        for slot, (source, absolute, _, _) in zip(slots, prepared):
            sources[slot] = source
            abs_flags[slot] = absolute
        if result is not None:
            self.results.add(result)
        self.out.append(VertInstr(
            vec_opcode=opcode,
            sources=tuple(sources),
            source_abs=tuple(abs_flags),
            input_attr=input_attr,
            const_src=const_src,
            vec_dest_temp=temp if temp is not None else nv40.VEC_DEST_NONE,
            vec_write_mask=mask,
            vec_result=result is not None,
            result=result if result is not None else nv40.VERT_RESULT_NONE,
            saturate=saturate,
        ))

    def _emit_sca(self, opcode, prepared_source, *, temp=None, result=None,
                  mask=(True,) * 4, saturate=False):
        prepared, const_src, input_attr = self._flush_shared_operands([prepared_source])
        source, absolute, _, _ = prepared[0]
        if result is not None:
            self.results.add(result)
        self.out.append(VertInstr(
            sca_opcode=opcode,
            sources=(VertSource(), VertSource(), source),
            source_abs=(False, False, absolute),
            input_attr=input_attr,
            const_src=const_src,
            sca_dest_temp=temp if temp is not None else nv40.SCA_DEST_NONE,
            sca_write_mask=mask,
            sca_result=result is not None,
            result=result if result is not None else nv40.VERT_RESULT_NONE,
            vec_write_mask=(False,) * 4,
            saturate=saturate,
        ))

    # -- instruction lowering --

    def translate(self) -> VertexTranslation:
        program = self.program
        if program.has_flow_control():
            flow = sorted({i.name for i in program.instructions
                           if i.opcode in (d3d9.OP_CALL, d3d9.OP_CALLNZ, d3d9.OP_LOOP,
                                           d3d9.OP_RET, d3d9.OP_ENDLOOP, d3d9.OP_LABEL,
                                           d3d9.OP_REP, d3d9.OP_ENDREP, d3d9.OP_IF,
                                           d3d9.OP_IFC, d3d9.OP_ELSE, d3d9.OP_ENDIF,
                                           d3d9.OP_BREAK, d3d9.OP_BREAKC, d3d9.OP_BREAKP)})
            raise RsxTranslationError(f'vertex flow control ({", ".join(flow)}) is unsupported')
        for instruction in program.instructions:
            self._lower(instruction)
        if not self.out:
            raise RsxTranslationError('vertex program produced no instructions')
        if len(self.out) > 512:
            raise RsxTranslationError(
                f'vertex program needs {len(self.out)} RSX slots; the hardware stores 512')
        if nv40.VERT_RESULT_POSITION not in self.results:
            raise RsxTranslationError('vertex program never writes the position result')
        self.out[-1] = self._with_end(self.out[-1])
        return VertexTranslation(
            self.out,
            tuple(sorted(self.input_attrs)),
            dict(self.attr_by_usage),
            tuple(sorted(self.results)),
            tuple(sorted(self.const_reads)),
            self.temps.count,
            tuple(self.notes),
        )

    @staticmethod
    def _with_end(instruction: VertInstr) -> VertInstr:
        from dataclasses import replace
        return replace(instruction, end=True)

    def _lower(self, ins: Instruction) -> None:
        op = ins.opcode
        VEC = {
            d3d9.OP_MOV: nv40.VEC_OP_MOV, d3d9.OP_ADD: nv40.VEC_OP_ADD,
            d3d9.OP_MUL: nv40.VEC_OP_MUL, d3d9.OP_MAD: nv40.VEC_OP_MAD,
            d3d9.OP_DP3: nv40.VEC_OP_DP3, d3d9.OP_DP4: nv40.VEC_OP_DP4,
            d3d9.OP_MIN: nv40.VEC_OP_MIN, d3d9.OP_MAX: nv40.VEC_OP_MAX,
            d3d9.OP_SLT: nv40.VEC_OP_SLT, d3d9.OP_SGE: nv40.VEC_OP_SGE,
            d3d9.OP_FRC: nv40.VEC_OP_FRC, d3d9.OP_DST: nv40.VEC_OP_DST,
            d3d9.OP_SGN: nv40.VEC_OP_SSG,
        }
        SCA = {
            d3d9.OP_RCP: nv40.SCA_OP_RCP, d3d9.OP_RSQ: nv40.SCA_OP_RSQ,
            d3d9.OP_EXP: nv40.SCA_OP_EX2, d3d9.OP_EXPP: nv40.SCA_OP_EX2,
            d3d9.OP_LIT: nv40.SCA_OP_LIT,
        }
        if op == d3d9.OP_NOP:
            return
        if op in VEC:
            temp, result, mask, saturate = self._dest(ins)
            prepared = [self._source(s) for s in ins.sources]
            if op == d3d9.OP_SGN:
                prepared = prepared[:1]  # sgn scratch operands carry no data
            self._emit_vec(VEC[op], prepared, temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op in SCA:
            temp, result, mask, saturate = self._dest(ins)
            self._emit_sca(SCA[op], self._source(ins.sources[0]), temp=temp,
                           result=result, mask=mask, saturate=saturate)
            return
        if op in (d3d9.OP_LOG, d3d9.OP_LOGP):
            # D3D9 log = log2(|src|); RSX LG2 is undefined for negative input,
            # so the absolute modifier carries the spec semantics (negate is
            # irrelevant under |x| and is cleared).
            temp, result, mask, saturate = self._dest(ins)
            source = self._source(ins.sources[0])
            source = (VertSource(source[0].kind, source[0].index, source[0].swizzle, False),
                      True, source[2], source[3])
            self._emit_sca(nv40.SCA_OP_LG2, source, temp=temp,
                           result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_SUB:
            temp, result, mask, saturate = self._dest(ins)
            a = self._source(ins.sources[0])
            b = self._source(ins.sources[1])
            b = (VertSource(b[0].kind, b[0].index, b[0].swizzle, not b[0].negate), b[1], b[2], b[3])
            self._emit_vec(nv40.VEC_OP_ADD, [a, b], temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_ABS:
            temp, result, mask, saturate = self._dest(ins)
            source, _, const, attr = self._source(ins.sources[0])
            source = VertSource(source.kind, source.index, source.swizzle, False)
            self._emit_vec(nv40.VEC_OP_MOV, [(source, True, const, attr)],
                           temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_LRP:
            # d = s0*s1 + (1-s0)*s2  ->  t = s1 - s2 ; d = t*s0 + s2
            temp, result, mask, saturate = self._dest(ins)
            s0, s1, s2 = (self._source(s) for s in ins.sources)
            scratch = self.temps.scratch()
            neg_s2 = (VertSource(s2[0].kind, s2[0].index, s2[0].swizzle, not s2[0].negate), s2[1], s2[2], s2[3])
            self._emit_vec(nv40.VEC_OP_ADD, [s1, neg_s2], temp=scratch, mask=(True,) * 4)
            t = (VertSource(VERT_REG_TEMP, scratch, SWZ_XYZW, False), False, None, None)
            self._emit_vec(nv40.VEC_OP_MAD, [t, s0, s2], temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_NRM:
            temp, result, mask, saturate = self._dest(ins)
            source = self._source(ins.sources[0])
            length = self.temps.scratch()
            self._emit_vec(nv40.VEC_OP_DP3, [source, source], temp=length, mask=(True, False, False, False))
            self._emit_sca(nv40.SCA_OP_RSQ,
                           (VertSource(VERT_REG_TEMP, length, (0, 0, 0, 0), False), False, None, None),
                           temp=length, mask=(True, False, False, False))
            inv = (VertSource(VERT_REG_TEMP, length, (0, 0, 0, 0), False), False, None, None)
            self._emit_vec(nv40.VEC_OP_MUL, [source, inv], temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_CRS:
            # (a x b) = a.yzx*b.zxy - a.zxy*b.yzx
            temp, result, mask, saturate = self._dest(ins)
            a = self._source(ins.sources[0])
            b = self._source(ins.sources[1])
            scratch = self.temps.scratch()
            a_yzx = (VertSource(a[0].kind, a[0].index, _swizzled(a[0].swizzle, (1, 2, 0, 3)), a[0].negate), a[1], a[2], a[3])
            b_zxy = (VertSource(b[0].kind, b[0].index, _swizzled(b[0].swizzle, (2, 0, 1, 3)), b[0].negate), b[1], b[2], b[3])
            self._emit_vec(nv40.VEC_OP_MUL, [a_yzx, b_zxy], temp=scratch, mask=(True,) * 4)
            a_zxy = (VertSource(a[0].kind, a[0].index, _swizzled(a[0].swizzle, (2, 0, 1, 3)), not a[0].negate), a[1], a[2], a[3])
            b_yzx = (VertSource(b[0].kind, b[0].index, _swizzled(b[0].swizzle, (1, 2, 0, 3)), b[0].negate), b[1], b[2], b[3])
            t = (VertSource(VERT_REG_TEMP, scratch, SWZ_XYZW, False), False, None, None)
            self._emit_vec(nv40.VEC_OP_MAD, [a_zxy, b_yzx, t], temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_POW:
            # |s0|^s1 = ex2(s1 * lg2(|s0|))
            temp, result, mask, saturate = self._dest(ins)
            s0 = self._source(ins.sources[0])
            s1 = self._source(ins.sources[1])
            s0 = (VertSource(s0[0].kind, s0[0].index, s0[0].swizzle, False), True, s0[2], s0[3])
            scratch = self.temps.scratch()
            self._emit_sca(nv40.SCA_OP_LG2, s0, temp=scratch, mask=(True, False, False, False))
            t = (VertSource(VERT_REG_TEMP, scratch, (0, 0, 0, 0), False), False, None, None)
            self._emit_vec(nv40.VEC_OP_MUL, [t, s1], temp=scratch, mask=(True, False, False, False))
            t = (VertSource(VERT_REG_TEMP, scratch, (0, 0, 0, 0), False), False, None, None)
            self._emit_sca(nv40.SCA_OP_EX2, t, temp=temp, result=result, mask=mask, saturate=saturate)
            return
        if op == d3d9.OP_SINCOS:
            temp, result, mask, saturate = self._dest(ins)
            if result is not None:
                raise RsxTranslationError('sincos to an output register is unsupported')
            source = self._source(ins.sources[0])
            if mask[2] or mask[3]:
                raise RsxTranslationError('sincos with a z/w write mask is undefined')
            if (mask[0] and mask[1] and source[0].kind == VERT_REG_TEMP
                    and source[0].index == temp):
                # D3D9 sincos reads its source once.  Lowered as two scalar
                # ops, an in-place form (dest register == source register)
                # would feed SIN the COS result, so the angle moves to a
                # scratch first.
                scratch = self.temps.scratch()
                self._emit_vec(nv40.VEC_OP_MOV, [source], temp=scratch,
                               mask=(True, False, False, False))
                source = (VertSource(VERT_REG_TEMP, scratch, (0, 0, 0, 0), False),
                          False, None, None)
            if mask[0]:  # x = cos
                self._emit_sca(nv40.SCA_OP_COS, source, temp=temp,
                               mask=(True, False, False, False), saturate=saturate)
            if mask[1]:  # y = sin
                self._emit_sca(nv40.SCA_OP_SIN, source, temp=temp,
                               mask=(False, True, False, False), saturate=saturate)
            return
        if op in (d3d9.OP_M4X4, d3d9.OP_M4X3, d3d9.OP_M3X4, d3d9.OP_M3X3, d3d9.OP_M3X2):
            rows = {d3d9.OP_M4X4: 4, d3d9.OP_M4X3: 3, d3d9.OP_M3X4: 4,
                    d3d9.OP_M3X3: 3, d3d9.OP_M3X2: 2}[op]
            dot = nv40.VEC_OP_DP4 if op in (d3d9.OP_M4X4, d3d9.OP_M4X3) else nv40.VEC_OP_DP3
            temp, result, mask, saturate = self._dest(ins)
            base = ins.sources[1]
            if base.register_type != d3d9.REG_CONST:
                raise RsxTranslationError('matrix operand outside the constant file is unsupported')
            for row in range(rows):
                row_mask = tuple(component == row for component in range(4))
                if not mask[row]:
                    continue
                row_source = SourceParam(base.register_type, base.register_number + row,
                                         base.swizzle, base.modifier)
                self._emit_vec(dot, [self._source(ins.sources[0]), self._source(row_source)],
                               temp=temp, result=result, mask=row_mask, saturate=saturate)
            return
        if op == d3d9.OP_MOVA:
            raise RsxTranslationError('vertex address register (mova) is unsupported')
        raise RsxTranslationError(f'vertex opcode {ins.name!r} is unsupported')


def compile_vertex_shader(data_or_program) -> VertexTranslation:
    program = data_or_program if isinstance(data_or_program, D3d9Program) else d3d9.disassemble(bytes(data_or_program))
    if program.kind != 'vertex':
        raise RsxTranslationError(f'expected a vertex program, got {program.version}')
    return _VertexBuilder(program).translate()


# --- fragment ---------------------------------------------------------------

@dataclass
class FragmentTranslation:
    instructions: list
    input_attrs: tuple[int, ...]
    tex_units: tuple[int, ...]
    const_registers: tuple[int, ...]        # D3D9 c# order = parameter order
    const_defaults: dict
    register_count: int
    uses_kill: bool
    depth_export: bool
    center_weight_attrs: tuple[int, ...]
    notes: tuple[str, ...]

    @property
    def ucode(self) -> bytes:
        return nv40.encode_fragment_program(self.instructions)

    def constant_patch_offsets(self) -> dict:
        """c# -> ucode-relative byte offsets of every inline payload reading it."""
        offsets: dict[int, list[int]] = {reg: [] for reg in self.const_registers}
        pc = 0
        for instruction in self.instructions:
            if instruction.inline_constant is not None:
                register = getattr(instruction, '_const_register', None)
                register = self._register_of(instruction) if register is None else register
                offsets[register].append(pc + 16)
            pc += instruction.byte_count
        return offsets

    def _register_of(self, instruction) -> int:
        return self._const_by_id[id(instruction)]

    _const_by_id: dict = field(default_factory=dict, repr=False)


class _FragmentBuilder:
    def __init__(self, program: D3d9Program):
        self.program = program
        self.rows: list[dict] = []   # staged instructions with const register tags
        self.notes: list[str] = []
        self.temps = _TempAllocator(48)
        self.input_map: dict[int, int] = {}
        self.const_defaults = {d.register_number: d.value for d in program.defs}
        self.const_registers: list[int] = []
        self.tex_units: set[int] = set()
        self.input_attrs: set[int] = set()
        self.uses_kill = False
        self.depth_export = False
        self.center_attrs: set[int] = set()
        self.sampler_types = {d.dest.register_number: d.sampler_type
                              for d in program.sampler_declarations()}
        self.color_out: dict[int, int] = {}
        self.depth_temp: int | None = None
        for instruction in program.instructions:
            for operand in list(instruction.sources) + ([instruction.dest] if instruction.dest else []):
                if operand is not None and operand.register_type == d3d9.REG_TEMP:
                    self.temps.reserve(operand.register_number)
        # Hardware export registers must never be handed out as scratch:
        # the final export moves write R0/R2/R3/R4 (and R1.z for depth) in
        # sequence, and a scratch accumulator living in one of them would be
        # clobbered before its own export.
        _export_map = {0: 0, 1: 2, 2: 3, 3: 4}
        for instruction in program.instructions:
            dest = instruction.dest
            if dest is None:
                continue
            if dest.register_type == d3d9.REG_COLOROUT and dest.register_number in _export_map:
                self.temps.reserve(_export_map[dest.register_number])
            elif dest.register_type == d3d9.REG_DEPTHOUT:
                self.temps.reserve(1)
        for declaration in program.declarations:
            dest = declaration.dest
            if dest.register_type == d3d9.REG_INPUT:
                if program.major >= 3:
                    key = (declaration.usage, declaration.usage_index)
                    attr = PIXEL_USAGE_TO_ATTR.get(key)
                    if attr is None:
                        raise RsxTranslationError(
                            f'pixel input usage {d3d9.USAGE_NAMES.get(declaration.usage, declaration.usage)}'
                            f'{declaration.usage_index} has no RSX attribute mapping')
                else:
                    # ps_2_0 declares its two color inputs as bare `dcl v#`.
                    if dest.register_number > 1:
                        raise RsxTranslationError(f'ps_2_0 color input v{dest.register_number}')
                    attr = nv40.FRAG_ATTR_COL0 + dest.register_number
                self.input_map[('v', dest.register_number)] = attr
                if dest.modifier & d3d9.DSTMOD_CENTROID:
                    self.center_attrs.add(attr)
            elif dest.register_type == d3d9.REG_ADDR:  # SM2 t#
                if dest.register_number > 7:
                    raise RsxTranslationError(f'pixel texcoord t{dest.register_number}')
                self.input_map[('t', dest.register_number)] = nv40.FRAG_ATTR_TEX0 + dest.register_number

    def _const_register(self, register: int) -> int:
        if register not in self.const_registers:
            self.const_registers.append(register)
        return register

    def _source(self, param: SourceParam):
        """Returns dict(source=FragSource, const=c#|None, attr=input|None)."""
        if param.relative:
            raise RsxTranslationError('pixel relative addressing is unsupported')
        if param.modifier not in _SUPPORTED_SRCMODS:
            raise RsxTranslationError(
                f'source modifier {d3d9.SRCMOD_NAMES.get(param.modifier, param.modifier)!r} is SM1-only')
        negate, absolute = _SUPPORTED_SRCMODS[param.modifier]
        rt = param.register_type
        if rt == d3d9.REG_TEMP:
            return {'source': FragSource(FRAG_REG_TEMP, param.register_number, param.swizzle, negate, absolute),
                    'const': None, 'attr': None}
        if rt == d3d9.REG_CONST:
            register = self._const_register(param.register_number)
            return {'source': FragSource(FRAG_REG_CONST, 0, param.swizzle, negate, absolute),
                    'const': register, 'attr': None}
        if rt in (d3d9.REG_INPUT, d3d9.REG_ADDR):
            group = 'v' if rt == d3d9.REG_INPUT else 't'
            attr = self.input_map.get((group, param.register_number))
            if attr is None:
                raise RsxTranslationError(f'pixel input {group}{param.register_number} was never declared')
            self.input_attrs.add(attr)
            return {'source': FragSource(FRAG_REG_INPUT, 0, param.swizzle, negate, absolute),
                    'const': None, 'attr': attr}
        if rt == d3d9.REG_MISCTYPE:
            raise RsxTranslationError('vPos/vFace inputs are unsupported')
        raise RsxTranslationError(
            f'pixel source register file {d3d9.REGISTER_NAMES.get(rt, rt)} is unsupported')

    def _dest(self, ins: Instruction):
        dest = ins.dest
        mask = tuple(bool(dest.write_mask >> c & 1) for c in range(4))
        rt = dest.register_type
        if rt == d3d9.REG_TEMP:
            return dest.register_number, mask, dest.saturate
        if rt == d3d9.REG_COLOROUT:
            if dest.register_number > 3:
                raise RsxTranslationError(f'color output oC{dest.register_number}')
            register = self.color_out.get(dest.register_number)
            if register is None:
                register = self.temps.scratch()
                self.color_out[dest.register_number] = register
            return register, mask, dest.saturate
        if rt == d3d9.REG_DEPTHOUT:
            if self.depth_temp is None:
                self.depth_temp = self.temps.scratch()
            self.depth_export = True
            return self.depth_temp, (True, False, False, False), dest.saturate
        raise RsxTranslationError(
            f'pixel destination register file {d3d9.REGISTER_NAMES.get(rt, rt)} is unsupported')

    # -- staging --

    def _stage(self, opcode, dest_reg, mask, saturate, prepared, *,
               tex_unit=0, no_dest=False, cond=None):
        """Split shared-resource conflicts, then stage one instruction row."""
        const_register = None
        input_attr = None
        sources = []
        for entry in prepared:
            source, const, attr = entry['source'], entry['const'], entry['attr']
            if const is not None:
                if const_register is None or const == const_register:
                    const_register = const
                else:
                    scratch = self.temps.scratch()
                    self._stage(nv40.FRAG_OP_MOV, scratch, (True,) * 4, False,
                                [{'source': FragSource(FRAG_REG_CONST, 0, SWZ_XYZW, False), 'const': const, 'attr': None}])
                    source = FragSource(FRAG_REG_TEMP, scratch, source.swizzle, source.negate, source.absolute)
                    const = None
            if attr is not None:
                if input_attr is None or attr == input_attr:
                    input_attr = attr
                else:
                    scratch = self.temps.scratch()
                    self._stage(nv40.FRAG_OP_MOV, scratch, (True,) * 4, False,
                                [{'source': FragSource(FRAG_REG_INPUT, 0, SWZ_XYZW, False), 'const': None, 'attr': attr}])
                    source = FragSource(FRAG_REG_TEMP, scratch, source.swizzle, source.negate, source.absolute)
                    attr = None
            sources.append(source)
        expected = nv40.fragment_operand_count(opcode)
        if len(sources) != expected:
            raise RsxTranslationError(f'fragment opcode {opcode:#x} takes {expected} operands, staged {len(sources)}')
        self.rows.append({
            'opcode': opcode, 'dest': dest_reg, 'mask': mask, 'saturate': saturate,
            'sources': tuple(sources), 'const': const_register,
            'attr': input_attr or 0, 'tex_unit': tex_unit, 'no_dest': no_dest,
            'cond': cond,
        })

    # -- lowering --

    def translate(self) -> FragmentTranslation:
        program = self.program
        if program.has_flow_control():
            raise RsxTranslationError('pixel flow control is unsupported')
        for instruction in program.instructions:
            self._lower(instruction)
        if 0 not in self.color_out:
            raise RsxTranslationError('pixel program never writes oC0')
        # Final export moves: scratch accumulators -> hardware color registers.
        export_map = {0: 0, 1: 2, 2: 3, 3: 4}
        for output_index in sorted(self.color_out):
            register = self.color_out[output_index]
            self._stage(nv40.FRAG_OP_MOV, export_map[output_index], (True,) * 4, False,
                        [{'source': FragSource(FRAG_REG_TEMP, register, SWZ_XYZW, False), 'const': None, 'attr': None}])
        if self.depth_temp is not None:
            self._stage(nv40.FRAG_OP_MOV, 1, (False, False, True, False), False,
                        [{'source': FragSource(FRAG_REG_TEMP, self.depth_temp, (0, 0, 0, 0), False), 'const': None, 'attr': None}])
        instructions = self._finalize()
        register_count = max(2, self.temps.count, 5 if len(self.color_out) > 1 else 0)
        translation = FragmentTranslation(
            instructions,
            tuple(sorted(self.input_attrs)),
            tuple(sorted(self.tex_units)),
            tuple(self.const_registers),
            dict(self.const_defaults),
            register_count,
            self.uses_kill,
            self.depth_export,
            tuple(sorted(self.center_attrs)),
            tuple(self.notes),
        )
        translation._const_by_id = {
            id(instruction): row['const']
            for instruction, row in zip(instructions, self.rows)
            if row['const'] is not None
        }
        return translation

    def _finalize(self):
        instructions = []
        for index, row in enumerate(self.rows):
            constant = None
            if row['const'] is not None:
                constant = tuple(self.const_defaults.get(row['const'], (0.0, 0.0, 0.0, 0.0)))
            cond = row['cond'] or {}
            instructions.append(FragInstr(
                opcode=row['opcode'],
                dest_reg=row['dest'],
                write_mask=row['mask'],
                no_dest=row['no_dest'],
                saturate=row['saturate'],
                end=index == len(self.rows) - 1,
                input_attr=row['attr'],
                tex_unit=row['tex_unit'],
                sources=row['sources'],
                inline_constant=constant,
                cond_test=cond.get('test', nv40.COND_TRUE),
                cond_swizzle=cond.get('swizzle', SWZ_XYZW),
                cond_write=cond.get('write', False),
            ))
        return instructions

    def _sampler(self, param: SourceParam) -> int:
        if param.register_type != d3d9.REG_SAMPLER:
            raise RsxTranslationError('texture instruction without a sampler operand')
        unit = param.register_number
        if unit > 15:
            raise RsxTranslationError(f'sampler s{unit}')
        self.tex_units.add(unit)
        return unit

    def _lower(self, ins: Instruction) -> None:
        op = ins.opcode
        DIRECT = {
            d3d9.OP_MOV: nv40.FRAG_OP_MOV, d3d9.OP_ADD: nv40.FRAG_OP_ADD,
            d3d9.OP_MUL: nv40.FRAG_OP_MUL, d3d9.OP_MAD: nv40.FRAG_OP_MAD,
            d3d9.OP_DP3: nv40.FRAG_OP_DP3, d3d9.OP_DP4: nv40.FRAG_OP_DP4,
            d3d9.OP_MIN: nv40.FRAG_OP_MIN, d3d9.OP_MAX: nv40.FRAG_OP_MAX,
            d3d9.OP_FRC: nv40.FRAG_OP_FRC, d3d9.OP_LRP: nv40.FRAG_OP_LRP,
            d3d9.OP_SLT: nv40.FRAG_OP_SLT, d3d9.OP_SGE: nv40.FRAG_OP_SGE,
            d3d9.OP_RCP: nv40.FRAG_OP_RCP, d3d9.OP_RSQ: nv40.FRAG_OP_RSQ,
            d3d9.OP_EXP: nv40.FRAG_OP_EX2, d3d9.OP_EXPP: nv40.FRAG_OP_EX2,
            d3d9.OP_DSX: nv40.FRAG_OP_DDX, d3d9.OP_DSY: nv40.FRAG_OP_DDY,
            d3d9.OP_LIT: nv40.FRAG_OP_LIT, d3d9.OP_DST: nv40.FRAG_OP_DST,
        }
        if op == d3d9.OP_NOP:
            return
        if op in DIRECT:
            dest, mask, saturate = self._dest(ins)
            self._stage(DIRECT[op], dest, mask, saturate, [self._source(s) for s in ins.sources])
            return
        if op in (d3d9.OP_LOG, d3d9.OP_LOGP):
            # D3D9 log = log2(|src|); RSX LG2 is undefined for negative input,
            # so the absolute modifier carries the spec semantics (negate is
            # irrelevant under |x| and is cleared).
            dest, mask, saturate = self._dest(ins)
            entry = self._source(ins.sources[0])
            entry['source'] = FragSource(entry['source'].kind, entry['source'].index,
                                         entry['source'].swizzle, False, True)
            self._stage(nv40.FRAG_OP_LG2, dest, mask, saturate, [entry])
            return
        if op == d3d9.OP_SUB:
            dest, mask, saturate = self._dest(ins)
            a = self._source(ins.sources[0])
            b = self._source(ins.sources[1])
            b['source'] = FragSource(b['source'].kind, b['source'].index, b['source'].swizzle,
                                     not b['source'].negate, b['source'].absolute)
            self._stage(nv40.FRAG_OP_ADD, dest, mask, saturate, [a, b])
            return
        if op == d3d9.OP_ABS:
            dest, mask, saturate = self._dest(ins)
            entry = self._source(ins.sources[0])
            entry['source'] = FragSource(entry['source'].kind, entry['source'].index,
                                         entry['source'].swizzle, False, True)
            self._stage(nv40.FRAG_OP_MOV, dest, mask, saturate, [entry])
            return
        if op == d3d9.OP_NRM:
            dest, mask, saturate = self._dest(ins)
            entry = self._source(ins.sources[0])
            length = self.temps.scratch()
            self._stage(nv40.FRAG_OP_DP3, length, (True,) * 4, False, [entry, dict(entry)])
            self._stage(nv40.FRAG_OP_RSQ, length, (True,) * 4, False,
                        [{'source': FragSource(FRAG_REG_TEMP, length, (0, 0, 0, 0), False), 'const': None, 'attr': None}])
            self._stage(nv40.FRAG_OP_MUL, dest, mask, saturate,
                        [dict(entry), {'source': FragSource(FRAG_REG_TEMP, length, (0, 0, 0, 0), False), 'const': None, 'attr': None}])
            return
        if op == d3d9.OP_POW:
            dest, mask, saturate = self._dest(ins)
            s0 = self._source(ins.sources[0])
            s0['source'] = FragSource(s0['source'].kind, s0['source'].index, s0['source'].swizzle, False, True)
            s1 = self._source(ins.sources[1])
            scratch = self.temps.scratch()
            self._stage(nv40.FRAG_OP_LG2, scratch, (True,) * 4, False, [s0])
            self._stage(nv40.FRAG_OP_MUL, scratch, (True,) * 4, False,
                        [{'source': FragSource(FRAG_REG_TEMP, scratch, (0, 0, 0, 0), False), 'const': None, 'attr': None}, s1])
            self._stage(nv40.FRAG_OP_EX2, dest, mask, saturate,
                        [{'source': FragSource(FRAG_REG_TEMP, scratch, (0, 0, 0, 0), False), 'const': None, 'attr': None}])
            return
        if op == d3d9.OP_DP2ADD:
            dest, mask, saturate = self._dest(ins)
            s0, s1, s2 = (self._source(s) for s in ins.sources)
            scratch = self.temps.scratch()
            self._stage(nv40.FRAG_OP_DP2, scratch, (True,) * 4, False, [s0, s1])
            self._stage(nv40.FRAG_OP_ADD, dest, mask, saturate,
                        [{'source': FragSource(FRAG_REG_TEMP, scratch, (0, 0, 0, 0), False), 'const': None, 'attr': None}, s2])
            return
        if op == d3d9.OP_CMP:
            # d = s0 >= 0 ? s1 : s2, selected through condition codes.  The
            # earlier sge+lrp lowering multiplied the REJECTED operand by 0 and
            # poisoned the result when it was non-finite (rcp+cmp divide
            # guards: 0*inf = NaN).  CC select never touches the rejected side;
            # s2 is the unconditional default so a NaN condition (every CC test
            # false) selects s2 exactly like D3D9's failed s0 >= 0.
            dest, mask, saturate = self._dest(ins)
            s0, s1, s2 = (self._source(s) for s in ins.sources)
            scratch = self.temps.scratch()
            self._stage(nv40.FRAG_OP_MOV, scratch, (True,) * 4, False, [s0],
                        cond={'write': True})
            self._stage(nv40.FRAG_OP_MOV, scratch, (True,) * 4, False, [s2])
            self._stage(nv40.FRAG_OP_MOV, scratch, (True,) * 4, False, [s1],
                        cond={'test': nv40.COND_GE})
            self._stage(nv40.FRAG_OP_MOV, dest, mask, saturate,
                        [{'source': FragSource(FRAG_REG_TEMP, scratch, SWZ_XYZW, False),
                          'const': None, 'attr': None}])
            return
        if op == d3d9.OP_SINCOS:
            dest, mask, saturate = self._dest(ins)
            entry = self._source(ins.sources[0])
            if mask[2] or mask[3]:
                raise RsxTranslationError('sincos with a z/w write mask is undefined')
            if (mask[0] and mask[1] and entry['source'].kind == FRAG_REG_TEMP
                    and entry['source'].index == dest):
                # D3D9 sincos reads its source once.  Lowered as two scalar
                # ops, an in-place form (dest register == source register)
                # would feed SIN the COS result, so the angle moves to a
                # scratch first.
                scratch = self.temps.scratch()
                self._stage(nv40.FRAG_OP_MOV, scratch, (True, False, False, False),
                            False, [dict(entry)])
                entry = {'source': FragSource(FRAG_REG_TEMP, scratch, (0, 0, 0, 0), False),
                         'const': None, 'attr': None}
            if mask[0]:
                self._stage(nv40.FRAG_OP_COS, dest, (True, False, False, False), saturate, [dict(entry)])
            if mask[1]:
                self._stage(nv40.FRAG_OP_SIN, dest, (False, True, False, False), saturate, [dict(entry)])
            return
        if op == d3d9.OP_TEXKILL:
            # kill when any component of the register is < 0
            self.uses_kill = True
            register = ins.dest
            source = FragSource(FRAG_REG_TEMP, register.register_number, SWZ_XYZW, False)
            if register.register_type == d3d9.REG_INPUT or register.register_type == d3d9.REG_ADDR:
                entry = self._source(SourceParam(register.register_type, register.register_number,
                                                 SWZ_XYZW, d3d9.SRCMOD_NONE))
            elif register.register_type == d3d9.REG_TEMP:
                entry = {'source': source, 'const': None, 'attr': None}
            else:
                raise RsxTranslationError('texkill register file unsupported')
            # Duplicate z into w so all four condition components are written;
            # the kill then fires exactly when any of x/y/z is negative.
            entry['source'] = FragSource(entry['source'].kind, entry['source'].index,
                                         (0, 1, 2, 2), entry['source'].negate,
                                         entry['source'].absolute)
            self._stage(nv40.FRAG_OP_MOV, 0, (True, True, True, True), False, [entry],
                        no_dest=True, cond={'write': True})
            self._stage(nv40.FRAG_OP_KIL, 0, (False,) * 4, False, [],
                        no_dest=True, cond={'test': nv40.COND_LT})
            return
        if op == d3d9.OP_TEXLD:
            dest, mask, saturate = self._dest(ins)
            coordinate = self._source(ins.sources[0])
            unit = self._sampler(ins.sources[1])
            opcode = {'': nv40.FRAG_OP_TEX, 'p': nv40.FRAG_OP_TXP, 'b': nv40.FRAG_OP_TXB}[ins.texld_variant]
            if opcode == nv40.FRAG_OP_TXB:
                bias = dict(coordinate)
                bias['source'] = FragSource(bias['source'].kind, bias['source'].index,
                                            (3, 3, 3, 3), bias['source'].negate, bias['source'].absolute)
                self._stage(opcode, dest, mask, saturate, [coordinate, bias], tex_unit=unit)
            else:
                self._stage(opcode, dest, mask, saturate, [coordinate], tex_unit=unit)
            return
        if op == d3d9.OP_TEXLDL:
            dest, mask, saturate = self._dest(ins)
            coordinate = self._source(ins.sources[0])
            unit = self._sampler(ins.sources[1])
            lod = dict(coordinate)
            lod['source'] = FragSource(lod['source'].kind, lod['source'].index,
                                       (3, 3, 3, 3), lod['source'].negate, lod['source'].absolute)
            self._stage(nv40.FRAG_OP_TXL, dest, mask, saturate, [coordinate, lod], tex_unit=unit)
            return
        if op == d3d9.OP_TEXLDD:
            dest, mask, saturate = self._dest(ins)
            coordinate = self._source(ins.sources[0])
            unit = self._sampler(ins.sources[1])
            ddx = self._source(ins.sources[2])
            ddy = self._source(ins.sources[3])
            self._stage(nv40.FRAG_OP_TXD, dest, mask, saturate, [coordinate, ddx, ddy], tex_unit=unit)
            return
        raise RsxTranslationError(f'pixel opcode {ins.name!r} is unsupported')

    def _materialize_zero(self):
        register = self._const_register_synthetic()
        return {'source': FragSource(FRAG_REG_CONST, 0, SWZ_XYZW, False), 'const': register, 'attr': None}

    _ZERO_REGISTER = None

    def _const_register_synthetic(self) -> int:
        """A private all-zero constant that no engine argument ever patches."""
        if self._ZERO_REGISTER is None:
            register = 224
            while register in self.const_defaults or register in self.const_registers:
                register += 1
            self._ZERO_REGISTER = register
            self.const_defaults[register] = (0.0, 0.0, 0.0, 0.0)
            self._const_register(register)
        return self._ZERO_REGISTER


def compile_pixel_shader(data_or_program) -> FragmentTranslation:
    program = data_or_program if isinstance(data_or_program, D3d9Program) else d3d9.disassemble(bytes(data_or_program))
    if program.kind != 'pixel':
        raise RsxTranslationError(f'expected a pixel program, got {program.version}')
    return _FragmentBuilder(program).translate()

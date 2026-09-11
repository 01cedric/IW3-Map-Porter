"""RSX (NV40-class) vertex and fragment microcode encoding and decoding.

Every bit position here mirrors the vendored IW4Studio decoder
(``RsxVertexProgramIr.cs`` / ``RsxFragmentInstructions.cs``), which decodes
real PS3 CoD shader uploads.  The encoder and decoder in this module are exact
inverses of each other; ``tests/test_rsx_nv40.py`` proves the round trip and
re-checks the decode positions against the C# property definitions.

Fragment programs store each 32-bit word with its 16-bit halves swapped
(``fragment_word``); vertex programs store plain big-endian words.  The
transform is its own inverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import struct

# --- shared -----------------------------------------------------------------

SWIZZLE_XYZW = (0, 1, 2, 3)

COND_FALSE = 0
COND_LT = 1
COND_EQ = 2
COND_LE = 3
COND_GT = 4
COND_NE = 5
COND_GE = 6
COND_TRUE = 7


def swizzle_from(text: str) -> tuple[int, int, int, int]:
    if len(text) == 1:
        text = text * 4
    while len(text) < 4:
        text += text[-1]
    return tuple('xyzw'.index(c) for c in text)


def fragment_word(value: int) -> int:
    """The RSX fragment byte-lane transform (its own inverse)."""
    value &= 0xFFFFFFFF
    return (((value & 0x000000FF) << 16) | ((value & 0x0000FF00) << 16) |
            ((value & 0x00FF0000) >> 16) | ((value & 0xFF000000) >> 16))


class RsxEncodeError(ValueError):
    """A program shape the RSX encoders cannot represent."""


# --- fragment ---------------------------------------------------------------

FRAG_OP_NOP = 0x00
FRAG_OP_MOV = 0x01
FRAG_OP_MUL = 0x02
FRAG_OP_ADD = 0x03
FRAG_OP_MAD = 0x04
FRAG_OP_DP3 = 0x05
FRAG_OP_DP4 = 0x06
FRAG_OP_DST = 0x07
FRAG_OP_MIN = 0x08
FRAG_OP_MAX = 0x09
FRAG_OP_SLT = 0x0A
FRAG_OP_SGE = 0x0B
FRAG_OP_SLE = 0x0C
FRAG_OP_SGT = 0x0D
FRAG_OP_SNE = 0x0E
FRAG_OP_SEQ = 0x0F
FRAG_OP_FRC = 0x10
FRAG_OP_FLR = 0x11
FRAG_OP_KIL = 0x12
FRAG_OP_PK4 = 0x13
FRAG_OP_UP4 = 0x14
FRAG_OP_DDX = 0x15
FRAG_OP_DDY = 0x16
FRAG_OP_TEX = 0x17
FRAG_OP_TXP = 0x18
FRAG_OP_TXD = 0x19
FRAG_OP_RCP = 0x1A
FRAG_OP_RSQ = 0x1B
FRAG_OP_EX2 = 0x1C
FRAG_OP_LG2 = 0x1D
FRAG_OP_LIT = 0x1E
FRAG_OP_LRP = 0x1F
FRAG_OP_STR = 0x20
FRAG_OP_SFL = 0x21
FRAG_OP_COS = 0x22
FRAG_OP_SIN = 0x23
FRAG_OP_PK2H = 0x24
FRAG_OP_UP2H = 0x25
FRAG_OP_POW = 0x26
FRAG_OP_PKB = 0x27
FRAG_OP_UPB = 0x28
FRAG_OP_PK16 = 0x29
FRAG_OP_UP16 = 0x2A
FRAG_OP_BEM = 0x2B
FRAG_OP_PKG = 0x2C
FRAG_OP_UPG = 0x2D
FRAG_OP_DP2A = 0x2E
FRAG_OP_TXL = 0x2F
FRAG_OP_TXB = 0x31
FRAG_OP_TEXBEM = 0x33
FRAG_OP_TXPBEM = 0x34
FRAG_OP_BEMLUM = 0x35
FRAG_OP_REFL = 0x36
FRAG_OP_TIMESWTEX = 0x37
FRAG_OP_DP2 = 0x38
FRAG_OP_NRM = 0x39
FRAG_OP_DIV = 0x3A
FRAG_OP_DIVSQ = 0x3B
FRAG_OP_LIF = 0x3C
FRAG_OP_FENCT = 0x3D
FRAG_OP_FENCB = 0x3E
FRAG_OP_BRK = 0x40
FRAG_OP_CAL = 0x41
FRAG_OP_IFE = 0x42
FRAG_OP_LOOP = 0x43
FRAG_OP_REP = 0x44
FRAG_OP_RET = 0x45

FRAG_REG_TEMP = 0
FRAG_REG_INPUT = 1
FRAG_REG_CONST = 2

# rasterizer inputs (dst bits 13..16)
FRAG_ATTR_WPOS = 0
FRAG_ATTR_COL0 = 1
FRAG_ATTR_COL1 = 2
FRAG_ATTR_FOGC = 3
FRAG_ATTR_TEX0 = 4  # ..TEX9 = 13
FRAG_ATTR_SSA = 14

FRAG_PREC_FP32 = 0
FRAG_PREC_FP16 = 1
FRAG_PREC_FX12 = 2

_FRAG_OPERAND_COUNTS = {}
for _ops, _count in (
    ((FRAG_OP_NOP, FRAG_OP_KIL, FRAG_OP_STR, FRAG_OP_SFL, 0x30, 0x32,
      FRAG_OP_FENCT, FRAG_OP_FENCB, 0x3F, FRAG_OP_BRK, FRAG_OP_CAL,
      FRAG_OP_IFE, FRAG_OP_LOOP, FRAG_OP_REP, FRAG_OP_RET), 0),
    ((FRAG_OP_MOV, FRAG_OP_FRC, FRAG_OP_FLR, FRAG_OP_PK4, FRAG_OP_UP4,
      FRAG_OP_DDX, FRAG_OP_DDY, FRAG_OP_TEX, FRAG_OP_TXP, FRAG_OP_RCP,
      FRAG_OP_RSQ, FRAG_OP_EX2, FRAG_OP_LG2, FRAG_OP_LIT, FRAG_OP_COS,
      FRAG_OP_SIN, FRAG_OP_PK2H, FRAG_OP_UP2H, FRAG_OP_PKB, FRAG_OP_UPB,
      FRAG_OP_PK16, FRAG_OP_UP16, FRAG_OP_PKG, FRAG_OP_UPG, FRAG_OP_NRM,
      FRAG_OP_LIF), 1),
    ((FRAG_OP_MUL, FRAG_OP_ADD, FRAG_OP_DP3, FRAG_OP_DP4, FRAG_OP_DST,
      FRAG_OP_MIN, FRAG_OP_MAX, FRAG_OP_SLT, FRAG_OP_SGE, FRAG_OP_SLE,
      FRAG_OP_SGT, FRAG_OP_SNE, FRAG_OP_SEQ, FRAG_OP_POW, FRAG_OP_TXL,
      FRAG_OP_TXB, FRAG_OP_REFL, FRAG_OP_DP2, FRAG_OP_DIV, FRAG_OP_DIVSQ), 2),
    ((FRAG_OP_MAD, FRAG_OP_LRP, FRAG_OP_BEM, FRAG_OP_TEXBEM, FRAG_OP_TXPBEM,
      FRAG_OP_DP2A, FRAG_OP_TXD), 3),
):
    for _op in _ops:
        _FRAG_OPERAND_COUNTS[_op] = _count

FRAG_TEXTURE_OPS = frozenset((FRAG_OP_TEX, FRAG_OP_TXP, FRAG_OP_TXD,
                              FRAG_OP_TXL, FRAG_OP_TXB, FRAG_OP_TEXBEM,
                              FRAG_OP_TXPBEM))


def fragment_operand_count(opcode: int) -> int:
    return _FRAG_OPERAND_COUNTS.get(opcode, 3)


@dataclass(frozen=True)
class FragSource:
    kind: int = FRAG_REG_TEMP
    index: int = 0
    swizzle: tuple[int, int, int, int] = SWIZZLE_XYZW
    negate: bool = False
    absolute: bool = False
    fp16: bool = False

    def encode(self, slot: int) -> int:
        if not 0 <= self.kind <= 3:
            raise RsxEncodeError(f'fragment source kind {self.kind}')
        if not 0 <= self.index <= 0x3F:
            raise RsxEncodeError(f'fragment source register {self.index}')
        word = self.kind & 3
        word |= (self.index & 0x3F) << 2
        if self.fp16:
            word |= 1 << 8
        for component, selector in enumerate(self.swizzle):
            word |= (selector & 3) << (9 + 2 * component)
        if self.negate:
            word |= 1 << 17
        if self.absolute:
            word |= 1 << 29 if slot == 0 else 1 << 18
        return word

    @staticmethod
    def decode(word: int, slot: int) -> 'FragSource':
        return FragSource(
            kind=word & 3,
            index=(word >> 2) & 0x3F,
            swizzle=tuple((word >> (9 + 2 * c)) & 3 for c in range(4)),
            negate=bool((word >> 17) & 1),
            absolute=bool((word >> 29) & 1) if slot == 0 else bool((word >> 18) & 1),
            fp16=bool((word >> 8) & 1),
        )


@dataclass(frozen=True)
class FragInstr:
    opcode: int
    dest_reg: int = 0
    dest_fp16: bool = False
    write_mask: tuple[bool, bool, bool, bool] = (True, True, True, True)
    no_dest: bool = False
    saturate: bool = False
    end: bool = False
    input_attr: int = 0
    tex_unit: int = 0
    sources: tuple[FragSource, ...] = ()
    inline_constant: tuple[float, float, float, float] | None = None
    scale: int = 0
    cond_test: int = COND_TRUE
    cond_swizzle: tuple[int, int, int, int] = SWIZZLE_XYZW
    cond_write: bool = False
    cond_write_reg1: bool = False
    cond_read_reg1: bool = False
    dest_precision: int = FRAG_PREC_FP32
    src_precisions: tuple[int, int, int] = (FRAG_PREC_FP32,) * 3
    expanded_texture: bool = False
    perspective_correction: bool = True
    indexed_input: bool = False

    @property
    def byte_count(self) -> int:
        return 32 if self.inline_constant is not None else 16

    def _sources3(self) -> tuple[FragSource, FragSource, FragSource]:
        sources = list(self.sources)
        while len(sources) < 3:
            sources.append(FragSource())
        return tuple(sources[:3])

    def encode(self) -> tuple[int, int, int, int]:
        if not 0 <= self.opcode <= 0x7F:
            raise RsxEncodeError(f'fragment opcode {self.opcode:#x}')
        if not 0 <= self.dest_reg <= 0x3F:
            raise RsxEncodeError(f'fragment destination register {self.dest_reg}')
        if not 0 <= self.tex_unit <= 0xF:
            raise RsxEncodeError(f'fragment texture unit {self.tex_unit}')
        if not 0 <= self.input_attr <= 0xF:
            raise RsxEncodeError(f'fragment input attribute {self.input_attr}')
        used = fragment_operand_count(self.opcode)
        if len(self.sources) != used:
            raise RsxEncodeError(
                f'fragment opcode {self.opcode:#x} takes {used} sources, got {len(self.sources)}')
        uses_constant = any(s.kind == FRAG_REG_CONST for s in self.sources)
        if uses_constant != (self.inline_constant is not None):
            raise RsxEncodeError('inline-constant payload does not match constant operands')

        s0, s1, s2 = self._sources3()
        dst = 0
        if self.end:
            dst |= 1
        dst |= (self.dest_reg & 0x3F) << 1
        if self.dest_fp16:
            dst |= 1 << 7
        if self.cond_write:
            dst |= 1 << 8
        for component, on in enumerate(self.write_mask):
            if on:
                dst |= 1 << (9 + component)
        dst |= (self.input_attr & 0xF) << 13
        dst |= (self.tex_unit & 0xF) << 17
        if self.expanded_texture:
            dst |= 1 << 21
        dst |= (self.dest_precision & 3) << 22
        dst |= (self.opcode & 0x3F) << 24
        if self.no_dest:
            dst |= 1 << 30
        if self.saturate:
            dst |= 1 << 31

        w0 = s0.encode(0)
        w0 |= (self.cond_test & 7) << 18
        for component, selector in enumerate(self.cond_swizzle):
            w0 |= (selector & 3) << (21 + 2 * component)
        if self.cond_write_reg1:
            w0 |= 1 << 30
        if self.cond_read_reg1:
            w0 |= 1 << 31

        w1 = s1.encode(1)
        for slot, precision in enumerate(self.src_precisions):
            w1 |= (precision & 7) << (19 + slot * 3)
        w1 |= (self.scale & 7) << 28
        if self.opcode & 0x40:
            w1 |= 1 << 31

        w2 = s2.encode(2)
        if self.indexed_input:
            w2 |= 1 << 30
        if self.perspective_correction:
            w2 |= 1 << 31
        return dst, w0, w1, w2

    @staticmethod
    def decode(dst: int, w0: int, w1: int, w2: int,
               constant: tuple[float, float, float, float] | None = None) -> 'FragInstr':
        opcode = ((dst >> 24) & 0x3F) | ((w1 >> 25) & 0x40)
        used = fragment_operand_count(opcode)
        sources = tuple(FragSource.decode(w, slot) for slot, w in enumerate((w0, w1, w2)))[:used]
        return FragInstr(
            opcode=opcode,
            dest_reg=(dst >> 1) & 0x3F,
            dest_fp16=bool((dst >> 7) & 1),
            write_mask=tuple(bool((dst >> (9 + c)) & 1) for c in range(4)),
            no_dest=bool((dst >> 30) & 1),
            saturate=bool((dst >> 31) & 1),
            end=bool(dst & 1),
            input_attr=(dst >> 13) & 0xF,
            tex_unit=(dst >> 17) & 0xF,
            sources=sources,
            inline_constant=constant,
            scale=(w1 >> 28) & 7,
            cond_test=(w0 >> 18) & 7,
            cond_swizzle=tuple((w0 >> (21 + 2 * c)) & 3 for c in range(4)),
            cond_write=bool((dst >> 8) & 1),
            cond_write_reg1=bool((w0 >> 30) & 1),
            cond_read_reg1=bool((w0 >> 31) & 1),
            dest_precision=(dst >> 22) & 3,
            src_precisions=tuple((w1 >> (19 + s * 3)) & 7 for s in range(3)),
            expanded_texture=bool((dst >> 21) & 1),
            perspective_correction=bool((w2 >> 31) & 1),
            indexed_input=bool((w2 >> 30) & 1),
        )


def encode_fragment_program(instructions: list[FragInstr]) -> bytes:
    """Encode instructions into the stored fragment upload byte image."""
    if not instructions:
        raise RsxEncodeError('fragment program has no instructions')
    for i, instruction in enumerate(instructions):
        expected_end = i == len(instructions) - 1
        if instruction.end != expected_end:
            raise RsxEncodeError(f'fragment end flag mismatch at instruction {i}')
    out = bytearray()
    for instruction in instructions:
        for word in instruction.encode():
            out += struct.pack('>I', fragment_word(word))
        if instruction.inline_constant is not None:
            for value in instruction.inline_constant:
                bits = struct.unpack('>I', struct.pack('>f', value))[0]
                out += struct.pack('>I', fragment_word(bits))
    return bytes(out)


def decode_fragment_program(data: bytes, offset: int = 0) -> list[FragInstr]:
    """Decode a stored fragment upload (mirror of the IW4Studio decoder)."""
    out: list[FragInstr] = []
    pc = offset
    while pc + 16 <= len(data):
        dst, w0, w1, w2 = (fragment_word(struct.unpack_from('>I', data, pc + i * 4)[0]) for i in range(4))
        opcode = ((dst >> 24) & 0x3F) | ((w1 >> 25) & 0x40)
        control_flow = FRAG_OP_BRK <= opcode <= FRAG_OP_RET
        can_const = not control_flow and opcode not in (FRAG_OP_FENCT, FRAG_OP_FENCB)
        has_const = can_const and any(
            (w & 3) == FRAG_REG_CONST for w in (w0, w1, w2))
        constant = None
        byte_count = 16
        if has_const:
            byte_count = 32
            if pc + 32 <= len(data):
                bits = tuple(fragment_word(struct.unpack_from('>I', data, pc + 16 + i * 4)[0]) for i in range(4))
                constant = struct.unpack('>4f', struct.pack('>4I', *bits))
        out.append(FragInstr.decode(dst, w0, w1, w2, constant))
        pc += byte_count
        if dst & 1:
            break
    return out


def fragment_constant_slot_offsets(instructions: list[FragInstr]) -> list[int]:
    """Byte offset of each instruction's inline-constant payload, program-relative."""
    offsets = []
    pc = 0
    for instruction in instructions:
        if instruction.inline_constant is not None:
            offsets.append(pc + 16)
        pc += instruction.byte_count
    return offsets


# --- vertex -----------------------------------------------------------------

VEC_OP_NOP = 0x00
VEC_OP_MOV = 0x01
VEC_OP_MUL = 0x02
VEC_OP_ADD = 0x03
VEC_OP_MAD = 0x04
VEC_OP_DP3 = 0x05
VEC_OP_DPH = 0x06
VEC_OP_DP4 = 0x07
VEC_OP_DST = 0x08
VEC_OP_MIN = 0x09
VEC_OP_MAX = 0x0A
VEC_OP_SLT = 0x0B
VEC_OP_SGE = 0x0C
VEC_OP_ARL = 0x0D
VEC_OP_FRC = 0x0E
VEC_OP_FLR = 0x0F
VEC_OP_SEQ = 0x10
VEC_OP_SFL = 0x11
VEC_OP_SGT = 0x12
VEC_OP_SLE = 0x13
VEC_OP_SNE = 0x14
VEC_OP_STR = 0x15
VEC_OP_SSG = 0x16
VEC_OP_TXL = 0x19

SCA_OP_NOP = 0x00
SCA_OP_MOV = 0x01
SCA_OP_RCP = 0x02
SCA_OP_RCC = 0x03
SCA_OP_RSQ = 0x04
SCA_OP_EXP = 0x05
SCA_OP_LOG = 0x06
SCA_OP_LIT = 0x07
SCA_OP_BRA = 0x08
SCA_OP_BRI = 0x09
SCA_OP_CAL = 0x0A
SCA_OP_CLI = 0x0B
SCA_OP_RET = 0x0C
SCA_OP_LG2 = 0x0D
SCA_OP_EX2 = 0x0E
SCA_OP_SIN = 0x0F
SCA_OP_COS = 0x10
SCA_OP_BRB = 0x11
SCA_OP_CLB = 0x12
SCA_OP_PSH = 0x13
SCA_OP_POP = 0x14

VERT_REG_TEMP = 1
VERT_REG_INPUT = 2
VERT_REG_CONST = 3

VERT_ATTR_POSITION = 0
VERT_ATTR_WEIGHT = 1
VERT_ATTR_NORMAL = 2
VERT_ATTR_COL0 = 3
VERT_ATTR_COL1 = 4
VERT_ATTR_FOG = 5
VERT_ATTR_PSIZE = 6
VERT_ATTR_ATTR7 = 7
VERT_ATTR_TEX0 = 8  # ..TEX7 = 15

VERT_RESULT_POSITION = 0
VERT_RESULT_COL0 = 1
VERT_RESULT_COL1 = 2
VERT_RESULT_BFC0 = 3
VERT_RESULT_BFC1 = 4
VERT_RESULT_FOG = 5
VERT_RESULT_PSIZE = 6
VERT_RESULT_TEX0 = 7  # ..TEX8 = 15
VERT_RESULT_NONE = 31

VEC_DEST_NONE = 0x3F
SCA_DEST_NONE = 0x1F

_VEC_SOURCE_MASKS = {
    VEC_OP_NOP: (), VEC_OP_SFL: (), VEC_OP_STR: (),
    VEC_OP_MOV: (0,), VEC_OP_ARL: (0,), VEC_OP_FRC: (0,), VEC_OP_FLR: (0,),
    VEC_OP_SSG: (0,), VEC_OP_TXL: (0,),
    VEC_OP_ADD: (0, 2), VEC_OP_MAD: (0, 1, 2),
}


def vec_source_slots(opcode: int) -> tuple[int, ...]:
    """Source slots read by a vector opcode (matches the IW4Studio table)."""
    if opcode in (0x17, 0x18):
        return ()
    return _VEC_SOURCE_MASKS.get(opcode, (0, 1))


def sca_reads_source2(opcode: int) -> bool:
    return not (opcode == 0 or 0x08 <= opcode <= 0x0C or 0x11 <= opcode <= 0x14)


@dataclass(frozen=True)
class VertSource:
    kind: int = VERT_REG_TEMP
    index: int = 0
    swizzle: tuple[int, int, int, int] = SWIZZLE_XYZW
    negate: bool = False

    def encode(self) -> int:
        if not 0 <= self.kind <= 3:
            raise RsxEncodeError(f'vertex source kind {self.kind}')
        if not 0 <= self.index <= 0x3F:
            raise RsxEncodeError(f'vertex source temp {self.index}')
        word = self.kind & 3
        word |= (self.index & 0x3F) << 2
        for component, selector in enumerate(self.swizzle):
            word |= (selector & 3) << (14 - 2 * component)
        if self.negate:
            word |= 1 << 16
        return word

    @staticmethod
    def decode(word: int) -> 'VertSource':
        return VertSource(
            kind=word & 3,
            index=(word >> 2) & 0x3F,
            swizzle=tuple((word >> (14 - 2 * c)) & 3 for c in range(4)),
            negate=bool((word >> 16) & 1),
        )


_NO_MASK = (False, False, False, False)


@dataclass(frozen=True)
class VertInstr:
    vec_opcode: int = VEC_OP_NOP
    sca_opcode: int = SCA_OP_NOP
    sources: tuple[VertSource, VertSource, VertSource] = (VertSource(), VertSource(), VertSource())
    source_abs: tuple[bool, bool, bool] = (False, False, False)
    input_attr: int = 0
    const_src: int = 0
    index_const: bool = False
    vec_dest_temp: int = VEC_DEST_NONE
    vec_write_mask: tuple[bool, bool, bool, bool] = _NO_MASK
    vec_result: bool = False
    sca_dest_temp: int = SCA_DEST_NONE
    sca_write_mask: tuple[bool, bool, bool, bool] = _NO_MASK
    sca_result: bool = False
    result: int = VERT_RESULT_NONE
    saturate: bool = False
    cond_update: bool = False
    cond_test: bool = False
    cond_condition: int = COND_TRUE
    cond_swizzle: tuple[int, int, int, int] = SWIZZLE_XYZW
    cond_register: int = 0
    end: bool = False

    def encode(self) -> tuple[int, int, int, int]:
        if not 0 <= self.vec_opcode <= 0x1F or not 0 <= self.sca_opcode <= 0x1F:
            raise RsxEncodeError('vertex opcode out of range')
        if not 0 <= self.const_src <= 0x3FF:
            raise RsxEncodeError(f'vertex constant source {self.const_src}')
        if not 0 <= self.input_attr <= 0xF:
            raise RsxEncodeError(f'vertex input attribute {self.input_attr}')
        if not 0 <= self.vec_dest_temp <= 0x3F or not 0 <= self.sca_dest_temp <= 0x1F:
            raise RsxEncodeError('vertex destination temp out of range')
        if not 0 <= self.result <= 0x1F:
            raise RsxEncodeError(f'vertex result register {self.result}')

        w0 = 0
        for component, selector in enumerate(self.cond_swizzle):
            w0 |= (selector & 3) << (8 - 2 * component)
        w0 |= (self.cond_condition & 7) << 10
        if self.cond_test:
            w0 |= 1 << 13
        if self.cond_update:
            w0 |= 0x20004000
        dest_temp = self.vec_dest_temp
        w0 |= (dest_temp & 0x3F) << 15
        if self.source_abs[0]:
            w0 |= 1 << 21
        if self.source_abs[1]:
            w0 |= 1 << 22
        if self.source_abs[2]:
            w0 |= 1 << 23
        w0 |= (self.cond_register & 1) << 25
        if self.saturate:
            w0 |= 1 << 26
        if self.vec_result:
            w0 |= 1 << 30

        s0 = self.sources[0].encode()
        s1 = self.sources[1].encode()
        s2 = self.sources[2].encode()

        w1 = (self.sca_opcode & 0x1F) << 27
        w1 |= (self.vec_opcode & 0x1F) << 22
        w1 |= (self.const_src & 0x3FF) << 12
        w1 |= (self.input_attr & 0xF) << 8
        w1 |= (s0 >> 9) & 0xFF

        w2 = (s0 & 0x1FF) << 23
        w2 |= (s1 & 0x1FFFF) << 6
        w2 |= (s2 >> 11) & 0x3F

        w3 = (s2 & 0x7FF) << 21
        for component, on in enumerate(self.sca_write_mask):
            if on:
                w3 |= 1 << (20 - component)
        for component, on in enumerate(self.vec_write_mask):
            if on:
                w3 |= 1 << (16 - component)
        if self.sca_result:
            w3 |= 1 << 12
        w3 |= (self.sca_dest_temp & 0x1F) << 7
        w3 |= (self.result & 0x1F) << 2
        if self.index_const:
            w3 |= 2
        if self.end:
            w3 |= 1
        return w0, w1, w2, w3

    @staticmethod
    def decode(w0: int, w1: int, w2: int, w3: int) -> 'VertInstr':
        s0 = ((w1 & 0xFF) << 9) | ((w2 >> 23) & 0x1FF)
        s1 = (w2 >> 6) & 0x1FFFF
        s2 = ((w2 & 0x3F) << 11) | ((w3 >> 21) & 0x7FF)
        return VertInstr(
            vec_opcode=(w1 >> 22) & 0x1F,
            sca_opcode=(w1 >> 27) & 0x1F,
            sources=tuple(VertSource.decode(s) for s in (s0, s1, s2)),
            source_abs=tuple(bool((w0 >> b) & 1) for b in (21, 22, 23)),
            input_attr=(w1 >> 8) & 0xF,
            const_src=(w1 >> 12) & 0x3FF,
            index_const=bool(w3 & 2),
            vec_dest_temp=(w0 >> 15) & 0x3F,
            vec_write_mask=tuple(bool((w3 >> (16 - c)) & 1) for c in range(4)),
            vec_result=bool((w0 >> 30) & 1),
            sca_dest_temp=(w3 >> 7) & 0x1F,
            sca_write_mask=tuple(bool((w3 >> (20 - c)) & 1) for c in range(4)),
            sca_result=bool((w3 >> 12) & 1),
            result=(w3 >> 2) & 0x1F,
            saturate=bool((w0 >> 26) & 1),
            cond_update=(w0 & 0x20004000) == 0x20004000,
            cond_test=bool((w0 >> 13) & 1),
            cond_condition=(w0 >> 10) & 7,
            cond_swizzle=tuple((w0 >> (8 - 2 * c)) & 3 for c in range(4)),
            cond_register=(w0 >> 25) & 1,
            end=bool(w3 & 1),
        )


def encode_vertex_program(instructions: list[VertInstr]) -> bytes:
    if not instructions:
        raise RsxEncodeError('vertex program has no instructions')
    for i, instruction in enumerate(instructions):
        expected_end = i == len(instructions) - 1
        if instruction.end != expected_end:
            raise RsxEncodeError(f'vertex end flag mismatch at instruction {i}')
    out = bytearray()
    for instruction in instructions:
        out += struct.pack('>4I', *instruction.encode())
    return bytes(out)


def decode_vertex_program(data: bytes, offset: int = 0, limit: int = 512) -> list[VertInstr]:
    out: list[VertInstr] = []
    pc = offset
    while len(out) < limit and pc + 16 <= len(data):
        words = struct.unpack_from('>4I', data, pc)
        out.append(VertInstr.decode(*words))
        pc += 16
        if words[3] & 1:
            break
    return out

"""Direct3D 9 shader-model 2/3 bytecode disassembly.

IW3 PC TechniqueSets store their vertex programs as ``vs_2_0``/``vs_3_0`` and
their pixel programs as ``ps_2_0``/``ps_3_0`` token streams (the ``sm2/``
technique prefix selects the 2.0 family).  This module decodes those streams
exactly: every token is consumed, every reserved bit is checked, and any
construct outside the documented SM2/SM3 grammar raises :class:`D3d9Error`
naming the instruction index and token offset.  Nothing here guesses.

Token references: the D3D9 shader token stream documentation
(``D3DSIO_*``, destination/source parameter tokens) and d3d9types.h.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct

# --- opcodes -----------------------------------------------------------------

OP_NOP = 0x00
OP_MOV = 0x01
OP_ADD = 0x02
OP_SUB = 0x03
OP_MAD = 0x04
OP_MUL = 0x05
OP_RCP = 0x06
OP_RSQ = 0x07
OP_DP3 = 0x08
OP_DP4 = 0x09
OP_MIN = 0x0A
OP_MAX = 0x0B
OP_SLT = 0x0C
OP_SGE = 0x0D
OP_EXP = 0x0E
OP_LOG = 0x0F
OP_LIT = 0x10
OP_DST = 0x11
OP_LRP = 0x12
OP_FRC = 0x13
OP_M4X4 = 0x14
OP_M4X3 = 0x15
OP_M3X4 = 0x16
OP_M3X3 = 0x17
OP_M3X2 = 0x18
OP_CALL = 0x19
OP_CALLNZ = 0x1A
OP_LOOP = 0x1B
OP_RET = 0x1C
OP_ENDLOOP = 0x1D
OP_LABEL = 0x1E
OP_DCL = 0x1F
OP_POW = 0x20
OP_CRS = 0x21
OP_SGN = 0x22
OP_ABS = 0x23
OP_NRM = 0x24
OP_SINCOS = 0x25
OP_REP = 0x26
OP_ENDREP = 0x27
OP_IF = 0x28
OP_IFC = 0x29
OP_ELSE = 0x2A
OP_ENDIF = 0x2B
OP_BREAK = 0x2C
OP_BREAKC = 0x2D
OP_MOVA = 0x2E
OP_DEFB = 0x2F
OP_DEFI = 0x30
OP_TEXKILL = 0x41
OP_TEXLD = 0x42
OP_EXPP = 0x4E
OP_LOGP = 0x4F
OP_DEF = 0x51
OP_CMP = 0x58
OP_DP2ADD = 0x5A
OP_DSX = 0x5B
OP_DSY = 0x5C
OP_TEXLDD = 0x5D
OP_SETP = 0x5E
OP_TEXLDL = 0x5F
OP_BREAKP = 0x60
OP_PHASE = 0xFFFD
OP_COMMENT = 0xFFFE
OP_END = 0xFFFF

OPCODE_NAMES = {
    OP_NOP: 'nop', OP_MOV: 'mov', OP_ADD: 'add', OP_SUB: 'sub', OP_MAD: 'mad',
    OP_MUL: 'mul', OP_RCP: 'rcp', OP_RSQ: 'rsq', OP_DP3: 'dp3', OP_DP4: 'dp4',
    OP_MIN: 'min', OP_MAX: 'max', OP_SLT: 'slt', OP_SGE: 'sge', OP_EXP: 'exp',
    OP_LOG: 'log', OP_LIT: 'lit', OP_DST: 'dst', OP_LRP: 'lrp', OP_FRC: 'frc',
    OP_M4X4: 'm4x4', OP_M4X3: 'm4x3', OP_M3X4: 'm3x4', OP_M3X3: 'm3x3',
    OP_M3X2: 'm3x2', OP_CALL: 'call', OP_CALLNZ: 'callnz', OP_LOOP: 'loop',
    OP_RET: 'ret', OP_ENDLOOP: 'endloop', OP_LABEL: 'label', OP_DCL: 'dcl',
    OP_POW: 'pow', OP_CRS: 'crs', OP_SGN: 'sgn', OP_ABS: 'abs', OP_NRM: 'nrm',
    OP_SINCOS: 'sincos', OP_REP: 'rep', OP_ENDREP: 'endrep', OP_IF: 'if',
    OP_IFC: 'ifc', OP_ELSE: 'else', OP_ENDIF: 'endif', OP_BREAK: 'break',
    OP_BREAKC: 'breakc', OP_MOVA: 'mova', OP_DEFB: 'defb', OP_DEFI: 'defi',
    OP_TEXKILL: 'texkill', OP_TEXLD: 'texld', OP_EXPP: 'expp', OP_LOGP: 'logp',
    OP_DEF: 'def', OP_CMP: 'cmp', OP_DP2ADD: 'dp2add', OP_DSX: 'dsx',
    OP_DSY: 'dsy', OP_TEXLDD: 'texldd', OP_SETP: 'setp', OP_TEXLDL: 'texldl',
    OP_BREAKP: 'breakp',
}

# --- register files ----------------------------------------------------------

REG_TEMP = 0        # r#
REG_INPUT = 1       # v#
REG_CONST = 2       # c#
REG_ADDR = 3        # a0 (vertex) / t# (SM2 pixel)
REG_RASTOUT = 4     # oPos(0) oFog(1) oPts(2)
REG_ATTROUT = 5     # oD#
REG_TEXCRDOUT = 6   # vs oT# / SM3 o#
REG_CONSTINT = 7    # i#
REG_COLOROUT = 8    # oC#
REG_DEPTHOUT = 9    # oDepth
REG_SAMPLER = 10    # s#
REG_CONST2 = 11
REG_CONST3 = 12
REG_CONST4 = 13
REG_CONSTBOOL = 14  # b#
REG_LOOP = 15       # aL
REG_TEMPFLOAT16 = 16
REG_MISCTYPE = 17   # vPos(0) vFace(1)
REG_LABEL = 18
REG_PREDICATE = 19  # p0

REGISTER_NAMES = {
    REG_TEMP: 'r', REG_INPUT: 'v', REG_CONST: 'c', REG_ADDR: 'a/t',
    REG_RASTOUT: 'oRast', REG_ATTROUT: 'oD', REG_TEXCRDOUT: 'oT/o',
    REG_CONSTINT: 'i', REG_COLOROUT: 'oC', REG_DEPTHOUT: 'oDepth',
    REG_SAMPLER: 's', REG_CONSTBOOL: 'b', REG_LOOP: 'aL',
    REG_MISCTYPE: 'vMisc', REG_LABEL: 'label', REG_PREDICATE: 'p',
}

# --- dcl usages (vertex input/output semantics) ------------------------------

USAGE_POSITION = 0
USAGE_BLENDWEIGHT = 1
USAGE_BLENDINDICES = 2
USAGE_NORMAL = 3
USAGE_PSIZE = 4
USAGE_TEXCOORD = 5
USAGE_TANGENT = 6
USAGE_BINORMAL = 7
USAGE_TESSFACTOR = 8
USAGE_POSITIONT = 9
USAGE_COLOR = 10
USAGE_FOG = 11
USAGE_DEPTH = 12
USAGE_SAMPLE = 13

USAGE_NAMES = {
    USAGE_POSITION: 'position', USAGE_BLENDWEIGHT: 'blendweight',
    USAGE_BLENDINDICES: 'blendindices', USAGE_NORMAL: 'normal',
    USAGE_PSIZE: 'psize', USAGE_TEXCOORD: 'texcoord', USAGE_TANGENT: 'tangent',
    USAGE_BINORMAL: 'binormal', USAGE_TESSFACTOR: 'tessfactor',
    USAGE_POSITIONT: 'positiont', USAGE_COLOR: 'color', USAGE_FOG: 'fog',
    USAGE_DEPTH: 'depth', USAGE_SAMPLE: 'sample',
}

# sampler texture types from the DCL token (bits 27..30)
SAMPLER_UNKNOWN = 0
SAMPLER_2D = 2
SAMPLER_CUBE = 3
SAMPLER_VOLUME = 4

# source modifiers
SRCMOD_NONE = 0
SRCMOD_NEG = 1
SRCMOD_BIAS = 2
SRCMOD_BIASNEG = 3
SRCMOD_SIGN = 4
SRCMOD_SIGNNEG = 5
SRCMOD_COMP = 6
SRCMOD_X2 = 7
SRCMOD_X2NEG = 8
SRCMOD_DZ = 9
SRCMOD_DW = 10
SRCMOD_ABS = 11
SRCMOD_ABSNEG = 12
SRCMOD_NOT = 13

SRCMOD_NAMES = {
    SRCMOD_NONE: '', SRCMOD_NEG: 'neg', SRCMOD_BIAS: 'bias',
    SRCMOD_BIASNEG: 'biasneg', SRCMOD_SIGN: 'sign', SRCMOD_SIGNNEG: 'signneg',
    SRCMOD_COMP: 'comp', SRCMOD_X2: 'x2', SRCMOD_X2NEG: 'x2neg',
    SRCMOD_DZ: 'dz', SRCMOD_DW: 'dw', SRCMOD_ABS: 'abs',
    SRCMOD_ABSNEG: 'absneg', SRCMOD_NOT: 'not',
}

# destination result modifiers (bit flags)
DSTMOD_SATURATE = 0x1
DSTMOD_PARTIAL_PRECISION = 0x2
DSTMOD_CENTROID = 0x4

RASTOUT_POSITION = 0
RASTOUT_FOG = 1
RASTOUT_POINT_SIZE = 2

MISC_POSITION = 0  # vPos
MISC_FACE = 1      # vFace

# operand counts per opcode: (destinations, sources)
_SHAPES = {
    OP_NOP: (0, 0), OP_MOV: (1, 1), OP_ADD: (1, 2), OP_SUB: (1, 2),
    OP_MAD: (1, 3), OP_MUL: (1, 2), OP_RCP: (1, 1), OP_RSQ: (1, 1),
    OP_DP3: (1, 2), OP_DP4: (1, 2), OP_MIN: (1, 2), OP_MAX: (1, 2),
    OP_SLT: (1, 2), OP_SGE: (1, 2), OP_EXP: (1, 1), OP_LOG: (1, 1),
    OP_LIT: (1, 1), OP_DST: (1, 2), OP_LRP: (1, 3), OP_FRC: (1, 1),
    OP_M4X4: (1, 2), OP_M4X3: (1, 2), OP_M3X4: (1, 2), OP_M3X3: (1, 2),
    OP_M3X2: (1, 2), OP_CALL: (0, 1), OP_CALLNZ: (0, 2), OP_LOOP: (0, 2),
    OP_RET: (0, 0), OP_ENDLOOP: (0, 0), OP_LABEL: (0, 1), OP_POW: (1, 2),
    OP_CRS: (1, 2), OP_SGN: (1, 3), OP_ABS: (1, 1), OP_NRM: (1, 1),
    OP_SINCOS: (1, 3), OP_REP: (0, 1), OP_ENDREP: (0, 0), OP_IF: (0, 1),
    OP_IFC: (0, 2), OP_ELSE: (0, 0), OP_ENDIF: (0, 0), OP_BREAK: (0, 0),
    OP_BREAKC: (0, 2), OP_MOVA: (1, 1), OP_TEXKILL: (1, 0), OP_TEXLD: (1, 2),
    OP_EXPP: (1, 1), OP_LOGP: (1, 1), OP_CMP: (1, 3), OP_DP2ADD: (1, 3),
    OP_DSX: (1, 1), OP_DSY: (1, 1), OP_TEXLDD: (1, 4), OP_SETP: (1, 2),
    OP_TEXLDL: (1, 2), OP_BREAKP: (0, 1),
}

# SM3 drops the trailing zero sources of sincos; both spellings are legal.
_ALT_SHAPES = {OP_SINCOS: (1, 1)}

_COMPARISONS = {1: 'gt', 2: 'eq', 3: 'ge', 4: 'lt', 5: 'ne', 6: 'le'}


class D3d9Error(ValueError):
    """A construct outside the supported/valid D3D9 SM2/SM3 grammar."""


@dataclass(frozen=True)
class SourceParam:
    register_type: int
    register_number: int
    swizzle: tuple[int, int, int, int]  # component selectors 0..3
    modifier: int
    relative: bool = False
    relative_source: 'SourceParam | None' = None

    @property
    def negated(self) -> bool:
        return self.modifier in (SRCMOD_NEG, SRCMOD_BIASNEG, SRCMOD_SIGNNEG,
                                 SRCMOD_X2NEG, SRCMOD_ABSNEG)

    def describe(self) -> str:
        swz = ''.join('xyzw'[c] for c in self.swizzle)
        mod = SRCMOD_NAMES.get(self.modifier, f'mod{self.modifier}')
        base = f'{REGISTER_NAMES.get(self.register_type, self.register_type)}{self.register_number}'
        if self.relative:
            base += '[a0]'
        return f'{mod}({base}).{swz}' if mod else f'{base}.{swz}'


@dataclass(frozen=True)
class DestParam:
    register_type: int
    register_number: int
    write_mask: int  # bit0=x bit1=y bit2=z bit3=w
    modifier: int    # DSTMOD_* flags
    relative: bool = False

    @property
    def saturate(self) -> bool:
        return bool(self.modifier & DSTMOD_SATURATE)

    def describe(self) -> str:
        mask = ''.join(c for i, c in enumerate('xyzw') if self.write_mask >> i & 1)
        return f'{REGISTER_NAMES.get(self.register_type, self.register_type)}{self.register_number}.{mask}'


@dataclass(frozen=True)
class Declaration:
    dest: DestParam
    usage: int | None = None        # vertex semantics
    usage_index: int = 0
    sampler_type: int = SAMPLER_UNKNOWN

    def describe(self) -> str:
        if self.dest.register_type == REG_SAMPLER:
            kind = {SAMPLER_2D: '2d', SAMPLER_CUBE: 'cube', SAMPLER_VOLUME: 'volume'}.get(self.sampler_type, '?')
            return f'dcl_{kind} s{self.dest.register_number}'
        usage = USAGE_NAMES.get(self.usage, f'usage{self.usage}')
        return f'dcl_{usage}{self.usage_index} {self.dest.describe()}'


@dataclass(frozen=True)
class Instruction:
    index: int
    token_offset: int
    opcode: int
    specific: int              # opcode-specific control bits 16..23
    dest: DestParam | None
    sources: tuple[SourceParam, ...]
    predicated: bool = False
    coissue: bool = False

    @property
    def name(self) -> str:
        return OPCODE_NAMES.get(self.opcode, f'op{self.opcode:02X}')

    @property
    def comparison(self) -> str | None:
        if self.opcode in (OP_IFC, OP_BREAKC, OP_SETP):
            return _COMPARISONS.get(self.specific & 7)
        return None

    @property
    def texld_variant(self) -> str:
        # ps_2_0+: bits 16/17 select project/bias forms of texld.
        if self.opcode != OP_TEXLD:
            return ''
        if self.specific & 0x1:
            return 'p'
        if self.specific & 0x2:
            return 'b'
        return ''


@dataclass(frozen=True)
class ConstantDef:
    register_number: int
    value: tuple[float, float, float, float]


@dataclass(frozen=True)
class IntConstantDef:
    register_number: int
    value: tuple[int, int, int, int]


@dataclass(frozen=True)
class BoolConstantDef:
    register_number: int
    value: bool


@dataclass
class D3d9Program:
    kind: str                        # 'vertex' | 'pixel'
    major: int
    minor: int
    declarations: tuple[Declaration, ...]
    defs: tuple[ConstantDef, ...]
    int_defs: tuple[IntConstantDef, ...]
    bool_defs: tuple[BoolConstantDef, ...]
    instructions: tuple[Instruction, ...]
    token_count: int
    comment_dwords: int = 0
    _dcl_by_reg: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._dcl_by_reg = {
            (d.dest.register_type, d.dest.register_number): d
            for d in self.declarations
        }

    @property
    def version(self) -> str:
        return f'{"vs" if self.kind == "vertex" else "ps"}_{self.major}_{self.minor}'

    def declaration_for(self, register_type: int, register_number: int) -> Declaration | None:
        return self._dcl_by_reg.get((register_type, register_number))

    def input_declarations(self) -> tuple[Declaration, ...]:
        return tuple(d for d in self.declarations if d.dest.register_type == REG_INPUT)

    def sampler_declarations(self) -> tuple[Declaration, ...]:
        return tuple(d for d in self.declarations if d.dest.register_type == REG_SAMPLER)

    def output_declarations(self) -> tuple[Declaration, ...]:
        return tuple(d for d in self.declarations if d.dest.register_type == REG_TEXCRDOUT)

    def has_flow_control(self) -> bool:
        flow = {OP_CALL, OP_CALLNZ, OP_LOOP, OP_RET, OP_ENDLOOP, OP_LABEL,
                OP_REP, OP_ENDREP, OP_IF, OP_IFC, OP_ELSE, OP_ENDIF,
                OP_BREAK, OP_BREAKC, OP_BREAKP}
        return any(i.opcode in flow for i in self.instructions)


def _register(token: int) -> tuple[int, int]:
    register_type = ((token >> 28) & 0x7) | ((token >> 8) & 0x18)
    register_number = token & 0x7FF
    return register_type, register_number


def _decode_source(token: int) -> SourceParam:
    register_type, number = _register(token)
    swizzle = tuple((token >> (16 + 2 * c)) & 3 for c in range(4))
    modifier = (token >> 24) & 0xF
    relative = bool(token & 0x2000)
    return SourceParam(register_type, number, swizzle, modifier, relative)


def _decode_dest(token: int, *, offset: int) -> DestParam:
    register_type, number = _register(token)
    write_mask = (token >> 16) & 0xF
    modifier = (token >> 20) & 0xF
    shift = (token >> 24) & 0xF
    if shift:
        raise D3d9Error(f'destination shift scale {shift} at token 0x{offset:X} is SM1-only')
    relative = bool(token & 0x2000)
    return DestParam(register_type, number, write_mask, modifier, relative)


def disassemble(data: bytes) -> D3d9Program:
    """Decode a complete D3D9 SM2/SM3 token stream (little-endian dwords)."""
    if len(data) < 8 or len(data) % 4:
        raise D3d9Error(f'shader byte length {len(data)} is not a valid token stream')
    tokens = struct.unpack(f'<{len(data) // 4}I', data)
    version = tokens[0]
    stage = version >> 16
    if stage == 0xFFFE:
        kind = 'vertex'
    elif stage == 0xFFFF:
        kind = 'pixel'
    else:
        raise D3d9Error(f'unknown shader stage 0x{stage:04X}')
    major = (version >> 8) & 0xFF
    minor = version & 0xFF
    if major not in (2, 3):
        raise D3d9Error(f'{kind} shader model {major}.{minor} is outside the supported SM2/SM3 domain')

    declarations: list[Declaration] = []
    defs: list[ConstantDef] = []
    int_defs: list[IntConstantDef] = []
    bool_defs: list[BoolConstantDef] = []
    instructions: list[Instruction] = []
    comment_dwords = 0

    i = 1
    index = 0
    ended = False
    while i < len(tokens):
        offset = i
        token = tokens[i]
        opcode = token & 0xFFFF
        if opcode == OP_END:
            if token != 0x0000FFFF:
                raise D3d9Error(f'malformed end token 0x{token:08X} at 0x{offset:X}')
            ended = True
            i += 1
            break
        if opcode == OP_COMMENT:
            length = (token >> 16) & 0x7FFF
            if i + 1 + length > len(tokens):
                raise D3d9Error(f'comment at token 0x{offset:X} exceeds stream')
            comment_dwords += length + 1
            i += 1 + length
            continue
        if opcode == OP_PHASE:
            raise D3d9Error('ps_1_4 phase token is outside the SM2/SM3 domain')
        length = (token >> 24) & 0xF
        predicated = bool(token & 0x10000000)
        coissue = bool(token & 0x40000000)
        if token & 0x80000000:
            raise D3d9Error(f'instruction token high bit set at 0x{offset:X}')
        specific = (token >> 16) & 0xFF
        i += 1
        operand_tokens = tokens[i:i + length]
        if len(operand_tokens) != length:
            raise D3d9Error(f'{OPCODE_NAMES.get(opcode, hex(opcode))} at token 0x{offset:X} exceeds stream')
        i += length

        if opcode == OP_DCL:
            if length != 2:
                raise D3d9Error(f'dcl at 0x{offset:X} has {length} operand tokens')
            info, dest_token = operand_tokens
            if not dest_token & 0x80000000:
                raise D3d9Error(f'dcl destination missing parameter bit at 0x{offset:X}')
            dest = _decode_dest(dest_token, offset=offset)
            if dest.register_type == REG_SAMPLER:
                declarations.append(Declaration(dest, sampler_type=(info >> 27) & 0xF))
            else:
                declarations.append(Declaration(dest, usage=info & 0x1F, usage_index=(info >> 16) & 0xF))
            continue
        if opcode == OP_DEF:
            if length != 5:
                raise D3d9Error(f'def at 0x{offset:X} has {length} operand tokens')
            dest = _decode_dest(operand_tokens[0], offset=offset)
            if dest.register_type != REG_CONST:
                raise D3d9Error(f'def targets non-float-constant register at 0x{offset:X}')
            value = struct.unpack('<4f', struct.pack('<4I', *operand_tokens[1:5]))
            defs.append(ConstantDef(dest.register_number, value))
            continue
        if opcode == OP_DEFI:
            if length != 5:
                raise D3d9Error(f'defi at 0x{offset:X} has {length} operand tokens')
            dest = _decode_dest(operand_tokens[0], offset=offset)
            if dest.register_type != REG_CONSTINT:
                raise D3d9Error(f'defi targets non-integer-constant register at 0x{offset:X}')
            value = struct.unpack('<4i', struct.pack('<4I', *operand_tokens[1:5]))
            int_defs.append(IntConstantDef(dest.register_number, value))
            continue
        if opcode == OP_DEFB:
            if length != 2:
                raise D3d9Error(f'defb at 0x{offset:X} has {length} operand tokens')
            dest = _decode_dest(operand_tokens[0], offset=offset)
            if dest.register_type != REG_CONSTBOOL:
                raise D3d9Error(f'defb targets non-bool-constant register at 0x{offset:X}')
            bool_defs.append(BoolConstantDef(dest.register_number, bool(operand_tokens[1])))
            continue

        shape = _SHAPES.get(opcode)
        if shape is None:
            raise D3d9Error(
                f'unsupported D3D9 opcode 0x{opcode:02X} at token 0x{offset:X} '
                f'(outside the SM2/SM3 IW3 shader domain)')
        dest_count, source_count = shape
        alt = _ALT_SHAPES.get(opcode)
        expected = dest_count + source_count
        if alt is not None and length == alt[0] + alt[1]:
            dest_count, source_count = alt
            expected = length
        # Relative addressing in SM3 appends one extra address token per
        # relatively-addressed operand; account for them while walking.
        cursor = 0
        dest = None
        if dest_count:
            if cursor >= len(operand_tokens):
                raise D3d9Error(f'{OPCODE_NAMES[opcode]} at 0x{offset:X} is missing its destination')
            dtok = operand_tokens[cursor]
            if not dtok & 0x80000000:
                raise D3d9Error(f'destination missing parameter bit at 0x{offset:X}')
            dest = _decode_dest(dtok, offset=offset)
            cursor += 1
            if dest.relative:
                raise D3d9Error(f'relative destination addressing at 0x{offset:X} is unsupported')
        sources: list[SourceParam] = []
        while cursor < len(operand_tokens):
            stok = operand_tokens[cursor]
            if not stok & 0x80000000:
                raise D3d9Error(f'source missing parameter bit at 0x{offset:X}')
            source = _decode_source(stok)
            cursor += 1
            if source.relative:
                if major >= 3:
                    if cursor >= len(operand_tokens):
                        raise D3d9Error(f'missing relative-address token at 0x{offset:X}')
                    atok = operand_tokens[cursor]
                    if not atok & 0x80000000:
                        raise D3d9Error(f'relative-address token missing parameter bit at 0x{offset:X}')
                    source = SourceParam(
                        source.register_type, source.register_number,
                        source.swizzle, source.modifier, True,
                        _decode_source(atok))
                    cursor += 1
                else:
                    source = SourceParam(
                        source.register_type, source.register_number,
                        source.swizzle, source.modifier, True,
                        SourceParam(REG_ADDR, 0, (0, 0, 0, 0), SRCMOD_NONE))
            sources.append(source)
        if len(sources) != source_count and not (alt and len(sources) == alt[1]):
            raise D3d9Error(
                f'{OPCODE_NAMES[opcode]} at 0x{offset:X} decoded {len(sources)} sources, expected {source_count}')
        if predicated:
            raise D3d9Error(f'predicated {OPCODE_NAMES[opcode]} at 0x{offset:X} is unsupported')
        instructions.append(Instruction(index, offset, opcode, specific, dest, tuple(sources), predicated, coissue))
        index += 1

    if not ended:
        raise D3d9Error('token stream has no end token')
    if i != len(tokens):
        raise D3d9Error(f'{len(tokens) - i} tokens after end token')
    return D3d9Program(kind, major, minor, tuple(declarations), tuple(defs),
                       tuple(int_defs), tuple(bool_defs), tuple(instructions), len(tokens),
                       comment_dwords)


# --- assembler (test fixtures and self-checks) -------------------------------

def _encode_dest(dest: DestParam) -> int:
    token = 0x80000000
    token |= dest.register_number & 0x7FF
    token |= (dest.register_type & 0x7) << 28
    token |= (dest.register_type & 0x18) << 8
    token |= (dest.write_mask & 0xF) << 16
    token |= (dest.modifier & 0xF) << 20
    return token


def _encode_source(source: SourceParam) -> int:
    token = 0x80000000
    token |= source.register_number & 0x7FF
    token |= (source.register_type & 0x7) << 28
    token |= (source.register_type & 0x18) << 8
    for component, selector in enumerate(source.swizzle):
        token |= (selector & 3) << (16 + 2 * component)
    token |= (source.modifier & 0xF) << 24
    if source.relative:
        token |= 0x2000
    return token


def assemble(kind: str, major: int, minor: int, body) -> bytes:
    """Encode a token stream from ``(opcode, specific, dest, sources)`` rows.

    Rows may also be ``('dcl', usage, usage_index, dest)``,
    ``('dcl_sampler', sampler_type, dest)``, ``('def', n, (x,y,z,w))``,
    ``('defi', n, (x,y,z,w))`` and ``('defb', n, bool)``.
    This assembler exists for regression fixtures; the disassembler above is
    the production surface and both must round-trip.
    """
    tokens: list[int] = [((0xFFFE if kind == 'vertex' else 0xFFFF) << 16) | (major << 8) | minor]
    for row in body:
        if row[0] == 'dcl':
            _, usage, usage_index, dest = row
            tokens.append(OP_DCL | (2 << 24))
            tokens.append(0x80000000 | (usage & 0x1F) | ((usage_index & 0xF) << 16))
            tokens.append(_encode_dest(dest))
            continue
        if row[0] == 'dcl_sampler':
            _, sampler_type, dest = row
            tokens.append(OP_DCL | (2 << 24))
            tokens.append(0x80000000 | ((sampler_type & 0xF) << 27))
            tokens.append(_encode_dest(dest))
            continue
        if row[0] == 'def':
            _, register_number, value = row
            tokens.append(OP_DEF | (5 << 24))
            tokens.append(_encode_dest(DestParam(REG_CONST, register_number, 0xF, 0)))
            tokens.extend(struct.unpack('<4I', struct.pack('<4f', *value)))
            continue
        if row[0] == 'defi':
            _, register_number, value = row
            tokens.append(OP_DEFI | (5 << 24))
            tokens.append(_encode_dest(DestParam(REG_CONSTINT, register_number, 0xF, 0)))
            tokens.extend(struct.unpack('<4I', struct.pack('<4i', *value)))
            continue
        if row[0] == 'defb':
            _, register_number, value = row
            tokens.append(OP_DEFB | (2 << 24))
            tokens.append(_encode_dest(DestParam(REG_CONSTBOOL, register_number, 0xF, 0)))
            tokens.append(1 if value else 0)
            continue
        opcode, specific, dest, sources = row
        operand_tokens: list[int] = []
        if dest is not None:
            operand_tokens.append(_encode_dest(dest))
        for source in sources:
            operand_tokens.append(_encode_source(source))
            if source.relative and major >= 3:
                operand_tokens.append(_encode_source(source.relative_source or SourceParam(REG_ADDR, 0, (0, 0, 0, 0), SRCMOD_NONE)))
        tokens.append((opcode & 0xFFFF) | ((specific & 0xFF) << 16) | (len(operand_tokens) << 24))
        tokens.extend(operand_tokens)
    tokens.append(0x0000FFFF)
    return struct.pack(f'<{len(tokens)}I', *tokens)


def src(register_type: int, register_number: int, swizzle: str = 'xyzw', modifier: int = SRCMOD_NONE) -> SourceParam:
    if len(swizzle) == 1:
        swizzle = swizzle * 4
    while len(swizzle) < 4:
        swizzle += swizzle[-1]
    return SourceParam(register_type, register_number, tuple('xyzw'.index(c) for c in swizzle), modifier)


def dst(register_type: int, register_number: int, mask: str = 'xyzw', modifier: int = 0) -> DestParam:
    write_mask = 0
    for c in mask:
        write_mask |= 1 << 'xyzw'.index(c)
    return DestParam(register_type, register_number, write_mask, modifier)

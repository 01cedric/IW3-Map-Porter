"""Deterministic software execution of D3D9 and RSX shader programs.

Two independent interpreters with one purpose each:

* the D3D9 interpreter executes the exact PC token stream semantics;
* the RSX interpreter executes decoded NV40 instructions the way the
  IW4Studio field decode describes them.

Running both over the same inputs is the translator's differential proof, and
the RSX side is also the preview executor: given linked technique data it
shades material previews without a console.

Both interpreters are straight-line only, matching the supported translation
domain, and raise on anything else.
"""

from __future__ import annotations

import math

from . import d3d9, nv40

Vec4 = list  # [x, y, z, w] floats


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _swizzle(vector, swizzle) -> Vec4:
    return [vector[c] for c in swizzle]


def _rcp(x: float) -> float:
    if x == 0.0:
        return math.inf
    return 1.0 / x


def _rsq(x: float) -> float:
    x = abs(x)
    if x == 0.0:
        return math.inf
    return 1.0 / math.sqrt(x)


def _ex2(x: float) -> float:
    try:
        return math.pow(2.0, x)
    except OverflowError:
        return math.inf


def _lg2_abs(x: float) -> float:
    """D3D9 log/logp/pow semantics: log2(|x|), -INF at zero."""
    x = abs(x)
    if x == 0.0:
        return -math.inf
    return math.log2(x)


def _lg2_raw(x: float) -> float:
    """RSX LG2 hardware semantics: undefined (NaN) for negative input.

    Modelling the raw behavior keeps the differential honest: a translation
    that forgets the |x| modifier on LG2 now disagrees with the D3D9 oracle
    instead of both sides silently sharing the absolute.
    """
    if x < 0.0:
        return math.nan
    if x == 0.0:
        return -math.inf
    return math.log2(x)


def _lit(source: Vec4) -> Vec4:
    diffuse = max(source[0], 0.0)
    specular = 0.0
    if source[0] > 0.0:
        power = max(min(source[3], 127.9961), -127.9961)
        specular = math.pow(max(source[1], 0.0), power) if source[1] > 0.0 else 0.0
    return [1.0, diffuse, specular, 1.0]


def _dst(a: Vec4, b: Vec4) -> Vec4:
    return [1.0, a[1] * b[1], a[2], b[3]]


class ShaderKill(Exception):
    """The fragment was discarded by a kill instruction."""


def default_sampler(unit: int, coordinate: Vec4, kind: str) -> Vec4:
    """Neutral checker so executions are defined without bound textures."""
    u = coordinate[0] - math.floor(coordinate[0])
    v = coordinate[1] - math.floor(coordinate[1])
    checker = 1.0 if (int(u * 8) + int(v * 8)) % 2 == 0 else 0.25
    return [checker, checker, checker, 1.0]


# --- D3D9 -------------------------------------------------------------------

class D3d9Machine:
    def __init__(self, program: d3d9.D3d9Program, constants=None, sampler=None):
        self.program = program
        self.temps = {}
        self.constants = dict(constants or {})
        for definition in program.defs:
            self.constants.setdefault(definition.register_number, list(definition.value))
        self.inputs = {}
        self.outputs = {}
        self.sampler = sampler or default_sampler
        self.killed = False

    def set_input(self, register_type: int, number: int, value) -> None:
        self.inputs[(register_type, number)] = list(value)

    def _read(self, param: d3d9.SourceParam) -> Vec4:
        rt, number = param.register_type, param.register_number
        if param.relative:
            raise ValueError('relative addressing is outside the interpreter domain')
        if rt == d3d9.REG_TEMP:
            base = self.temps.get(number)
            if base is None:
                raise ValueError(f'read of undefined r{number}')
        elif rt == d3d9.REG_CONST:
            base = self.constants.get(number, [0.0, 0.0, 0.0, 0.0])
        elif rt in (d3d9.REG_INPUT, d3d9.REG_ADDR):
            base = self.inputs.get((rt, number))
            if base is None:
                raise ValueError(f'read of unbound input {rt}:{number}')
        else:
            raise ValueError(f'interpreter cannot read register file {rt}')
        value = _swizzle(base, param.swizzle)
        modifier = param.modifier
        if modifier in (d3d9.SRCMOD_ABS, d3d9.SRCMOD_ABSNEG):
            value = [abs(component) for component in value]
        if modifier in (d3d9.SRCMOD_NEG, d3d9.SRCMOD_ABSNEG):
            value = [-component for component in value]
        elif modifier not in (d3d9.SRCMOD_NONE, d3d9.SRCMOD_ABS):
            raise ValueError(f'interpreter does not model modifier {modifier}')
        return value

    def _write(self, dest: d3d9.DestParam, value: Vec4) -> None:
        if dest.saturate:
            value = [_clamp01(component) for component in value]
        key = None
        if dest.register_type == d3d9.REG_TEMP:
            store = self.temps.setdefault(dest.register_number, [0.0, 0.0, 0.0, 0.0])
        else:
            key = (dest.register_type, dest.register_number)
            store = self.outputs.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        for component in range(4):
            if dest.write_mask >> component & 1:
                store[component] = float(value[component])

    def run(self) -> dict:
        program = self.program
        if program.has_flow_control():
            raise ValueError('flow control is outside the interpreter domain')
        for ins in program.instructions:
            self._execute(ins)
        return self.outputs

    def _execute(self, ins: d3d9.Instruction) -> None:
        op = ins.opcode
        read = self._read
        if op == d3d9.OP_NOP:
            return
        simple = {
            d3d9.OP_MOV: lambda a: a,
            d3d9.OP_ADD: lambda a, b: [x + y for x, y in zip(a, b)],
            d3d9.OP_SUB: lambda a, b: [x - y for x, y in zip(a, b)],
            d3d9.OP_MUL: lambda a, b: [x * y for x, y in zip(a, b)],
            d3d9.OP_MAD: lambda a, b, c: [x * y + z for x, y, z in zip(a, b, c)],
            d3d9.OP_MIN: lambda a, b: [min(x, y) for x, y in zip(a, b)],
            d3d9.OP_MAX: lambda a, b: [max(x, y) for x, y in zip(a, b)],
            d3d9.OP_SLT: lambda a, b: [1.0 if x < y else 0.0 for x, y in zip(a, b)],
            d3d9.OP_SGE: lambda a, b: [1.0 if x >= y else 0.0 for x, y in zip(a, b)],
            d3d9.OP_FRC: lambda a: [x - math.floor(x) for x in a],
            d3d9.OP_LRP: lambda a, b, c: [x * (y - z) + z for x, y, z in zip(a, b, c)],
            d3d9.OP_CMP: lambda a, b, c: [y if x >= 0.0 else z for x, y, z in zip(a, b, c)],
            d3d9.OP_ABS: lambda a: [abs(x) for x in a],
            d3d9.OP_SGN: lambda a, *_: [0.0 if x == 0.0 else math.copysign(1.0, x) for x in a],
            d3d9.OP_DST: _dst,
            d3d9.OP_LIT: _lit,
        }
        if op in simple:
            sources = ins.sources[:1] if op == d3d9.OP_SGN else ins.sources
            values = [read(s) for s in sources]
            self._write(ins.dest, simple[op](*values))
            return
        if op in (d3d9.OP_DP3, d3d9.OP_DP4, d3d9.OP_DP2ADD):
            a = read(ins.sources[0]); b = read(ins.sources[1])
            if op == d3d9.OP_DP3:
                value = sum(x * y for x, y in zip(a[:3], b[:3]))
            elif op == d3d9.OP_DP4:
                value = sum(x * y for x, y in zip(a, b))
            else:
                value = a[0] * b[0] + a[1] * b[1] + read(ins.sources[2])[0]
            self._write(ins.dest, [value] * 4)
            return
        if op in (d3d9.OP_RCP, d3d9.OP_RSQ, d3d9.OP_EXP, d3d9.OP_EXPP, d3d9.OP_LOG, d3d9.OP_LOGP):
            scalar = read(ins.sources[0])[0]
            fn = {d3d9.OP_RCP: _rcp, d3d9.OP_RSQ: _rsq, d3d9.OP_EXP: _ex2,
                  d3d9.OP_EXPP: _ex2, d3d9.OP_LOG: _lg2_abs, d3d9.OP_LOGP: _lg2_abs}[op]
            self._write(ins.dest, [fn(scalar)] * 4)
            return
        if op == d3d9.OP_POW:
            base = abs(read(ins.sources[0])[0])
            exponent = read(ins.sources[1])[0]
            value = _ex2(exponent * _lg2_abs(base)) if base != 0.0 else 0.0
            self._write(ins.dest, [value] * 4)
            return
        if op == d3d9.OP_NRM:
            a = read(ins.sources[0])
            inverse = _rsq(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
            self._write(ins.dest, [component * inverse for component in a])
            return
        if op == d3d9.OP_CRS:
            a = read(ins.sources[0]); b = read(ins.sources[1])
            self._write(ins.dest, [a[1] * b[2] - a[2] * b[1],
                                   a[2] * b[0] - a[0] * b[2],
                                   a[0] * b[1] - a[1] * b[0], 0.0])
            return
        if op == d3d9.OP_SINCOS:
            angle = read(ins.sources[0])[0]
            self._write(ins.dest, [math.cos(angle), math.sin(angle), 0.0, 0.0])
            return
        if op in (d3d9.OP_M4X4, d3d9.OP_M4X3, d3d9.OP_M3X4, d3d9.OP_M3X3, d3d9.OP_M3X2):
            rows = {d3d9.OP_M4X4: 4, d3d9.OP_M4X3: 3, d3d9.OP_M3X4: 4,
                    d3d9.OP_M3X3: 3, d3d9.OP_M3X2: 2}[op]
            dot3 = op in (d3d9.OP_M3X4, d3d9.OP_M3X3, d3d9.OP_M3X2)
            a = read(ins.sources[0])
            base = ins.sources[1]
            out = [0.0, 0.0, 0.0, 0.0]
            for row in range(rows):
                row_param = d3d9.SourceParam(base.register_type, base.register_number + row,
                                             base.swizzle, base.modifier)
                b = self._read(row_param)
                out[row] = (sum(x * y for x, y in zip(a[:3], b[:3])) if dot3
                            else sum(x * y for x, y in zip(a, b)))
            self._write(ins.dest, out)
            return
        if op == d3d9.OP_TEXKILL:
            register = ins.dest
            value = self._read(d3d9.SourceParam(register.register_type, register.register_number,
                                                (0, 1, 2, 3), d3d9.SRCMOD_NONE))
            if value[0] < 0.0 or value[1] < 0.0 or value[2] < 0.0:
                self.killed = True
                raise ShaderKill()
            return
        if op in (d3d9.OP_TEXLD, d3d9.OP_TEXLDL, d3d9.OP_TEXLDD):
            coordinate = read(ins.sources[0])
            sampler = ins.sources[1]
            if sampler.register_type != d3d9.REG_SAMPLER:
                raise ValueError('texture instruction without sampler')
            if ins.opcode == d3d9.OP_TEXLD and ins.texld_variant == 'p':
                w = coordinate[3]
                coordinate = [component / w if w else math.inf for component in coordinate]
            kind = {d3d9.SAMPLER_2D: '2d', d3d9.SAMPLER_CUBE: 'cube',
                    d3d9.SAMPLER_VOLUME: 'volume'}.get(
                        self.program.declaration_for(d3d9.REG_SAMPLER, sampler.register_number).sampler_type
                        if self.program.declaration_for(d3d9.REG_SAMPLER, sampler.register_number) else d3d9.SAMPLER_2D,
                        '2d')
            self._write(ins.dest, self.sampler(sampler.register_number, coordinate, kind))
            return
        if op in (d3d9.OP_DSX, d3d9.OP_DSY):
            self._write(ins.dest, [0.0, 0.0, 0.0, 0.0])
            return
        raise ValueError(f'interpreter does not model d3d9 opcode {ins.name!r}')


def run_d3d9(program: d3d9.D3d9Program, *, inputs=None, constants=None, sampler=None):
    machine = D3d9Machine(program, constants, sampler)
    for key, value in (inputs or {}).items():
        machine.set_input(key[0], key[1], value)
    try:
        outputs = machine.run()
    except ShaderKill:
        return {'killed': True}
    return {'outputs': outputs, 'killed': False}


# --- RSX --------------------------------------------------------------------

class RsxVertexMachine:
    def __init__(self, instructions, *, attributes=None, constants=None):
        self.instructions = instructions
        self.attributes = {k: list(v) for k, v in (attributes or {}).items()}
        self.constants = {k: list(v) for k, v in (constants or {}).items()}
        self.temps = {}
        self.results = {}

    def _read(self, source: nv40.VertSource, instruction: nv40.VertInstr, absolute: bool) -> Vec4:
        if source.kind == nv40.VERT_REG_TEMP:
            base = self.temps.get(source.index)
            if base is None:
                raise ValueError(f'read of undefined vertex temp R{source.index}')
        elif source.kind == nv40.VERT_REG_INPUT:
            base = self.attributes.get(instruction.input_attr)
            if base is None:
                raise ValueError(f'read of unbound vertex attribute {instruction.input_attr}')
        elif source.kind == nv40.VERT_REG_CONST:
            base = self.constants.get(instruction.const_src, [0.0, 0.0, 0.0, 0.0])
        else:
            raise ValueError(f'vertex source kind {source.kind}')
        value = _swizzle(base, source.swizzle)
        if absolute:
            value = [abs(component) for component in value]
        if source.negate:
            value = [-component for component in value]
        return value

    def _write(self, instruction, value: Vec4, mask, *, scalar: bool) -> None:
        if instruction.saturate:
            value = [_clamp01(component) for component in value]
        if scalar:
            to_result = instruction.sca_result
            temp = instruction.sca_dest_temp
            temp_none = nv40.SCA_DEST_NONE
        else:
            to_result = instruction.vec_result
            temp = instruction.vec_dest_temp
            temp_none = nv40.VEC_DEST_NONE
        if to_result:
            store = self.results.setdefault(instruction.result, [0.0, 0.0, 0.0, 0.0])
        elif temp != temp_none:
            store = self.temps.setdefault(temp, [0.0, 0.0, 0.0, 0.0])
        else:
            return
        for component in range(4):
            if mask[component]:
                store[component] = float(value[component])

    def run(self) -> dict:
        for instruction in self.instructions:
            self._execute(instruction)
        return self.results

    def _execute(self, ins: nv40.VertInstr) -> None:
        if ins.cond_test or ins.cond_update:
            raise ValueError('vertex condition codes are outside the interpreter domain')
        sources = ins.sources
        if ins.vec_opcode != nv40.VEC_OP_NOP:
            slots = nv40.vec_source_slots(ins.vec_opcode)
            values = [self._read(sources[slot], ins, ins.source_abs[slot]) for slot in slots]
            vec = {
                nv40.VEC_OP_MOV: lambda a: a,
                nv40.VEC_OP_MUL: lambda a, b: [x * y for x, y in zip(a, b)],
                nv40.VEC_OP_ADD: lambda a, b: [x + y for x, y in zip(a, b)],
                nv40.VEC_OP_MAD: lambda a, b, c: [x * y + z for x, y, z in zip(a, b, c)],
                nv40.VEC_OP_MIN: lambda a, b: [min(x, y) for x, y in zip(a, b)],
                nv40.VEC_OP_MAX: lambda a, b: [max(x, y) for x, y in zip(a, b)],
                nv40.VEC_OP_SLT: lambda a, b: [1.0 if x < y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_SGE: lambda a, b: [1.0 if x >= y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_SGT: lambda a, b: [1.0 if x > y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_SLE: lambda a, b: [1.0 if x <= y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_SEQ: lambda a, b: [1.0 if x == y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_SNE: lambda a, b: [1.0 if x != y else 0.0 for x, y in zip(a, b)],
                nv40.VEC_OP_FRC: lambda a: [x - math.floor(x) for x in a],
                nv40.VEC_OP_FLR: lambda a: [math.floor(x) for x in a],
                nv40.VEC_OP_SSG: lambda a: [0.0 if x == 0.0 else math.copysign(1.0, x) for x in a],
                nv40.VEC_OP_DST: _dst,
            }
            if ins.vec_opcode in vec:
                result = vec[ins.vec_opcode](*values)
            elif ins.vec_opcode == nv40.VEC_OP_DP3:
                result = [sum(x * y for x, y in zip(values[0][:3], values[1][:3]))] * 4
            elif ins.vec_opcode == nv40.VEC_OP_DP4:
                result = [sum(x * y for x, y in zip(values[0], values[1]))] * 4
            elif ins.vec_opcode == nv40.VEC_OP_DPH:
                result = [sum(x * y for x, y in zip(values[0][:3], values[1][:3])) + values[1][3]] * 4
            else:
                raise ValueError(f'vertex vector opcode {ins.vec_opcode:#x}')
            self._write(ins, result, ins.vec_write_mask, scalar=False)
        if ins.sca_opcode != nv40.SCA_OP_NOP:
            if not nv40.sca_reads_source2(ins.sca_opcode):
                raise ValueError(f'vertex scalar control flow {ins.sca_opcode:#x}')
            source = self._read(sources[2], ins, ins.source_abs[2])
            scalar = source[0]
            fn = {
                nv40.SCA_OP_MOV: lambda x: x,
                nv40.SCA_OP_RCP: _rcp,
                nv40.SCA_OP_RCC: lambda x: max(min(_rcp(x), 1.884467e19), 5.42101e-20) if x > 0
                    else min(max(_rcp(x), -1.884467e19), -5.42101e-20),
                nv40.SCA_OP_RSQ: _rsq,
                nv40.SCA_OP_EXP: _ex2, nv40.SCA_OP_EX2: _ex2,
                nv40.SCA_OP_LOG: _lg2_raw, nv40.SCA_OP_LG2: _lg2_raw,
                nv40.SCA_OP_SIN: math.sin, nv40.SCA_OP_COS: math.cos,
            }.get(ins.sca_opcode)
            if fn is None and ins.sca_opcode != nv40.SCA_OP_LIT:
                raise ValueError(f'vertex scalar opcode {ins.sca_opcode:#x}')
            result = _lit(source) if ins.sca_opcode == nv40.SCA_OP_LIT else [fn(scalar)] * 4
            self._write(ins, result, ins.sca_write_mask, scalar=True)


class RsxFragmentMachine:
    def __init__(self, instructions, *, attributes=None, sampler=None, sampler_kinds=None,
                 constant_overrides=None):
        self.instructions = instructions
        self.attributes = {k: list(v) for k, v in (attributes or {}).items()}
        self.sampler = sampler or default_sampler
        self.sampler_kinds = dict(sampler_kinds or {})
        self.temps = {}
        self.temps_half = {}
        self.condition = {0: [0.0, 0.0, 0.0, 0.0], 1: [0.0, 0.0, 0.0, 0.0]}
        self.killed = False
        # instruction index -> replacement float4 for its inline constant
        self.constant_overrides = dict(constant_overrides or {})

    def _bank(self, fp16: bool):
        return self.temps_half if fp16 else self.temps

    def _read(self, source: nv40.FragSource, instruction, index: int) -> Vec4:
        if source.kind == nv40.FRAG_REG_TEMP:
            base = self._bank(source.fp16).get(source.index)
            if base is None:
                raise ValueError(f'read of undefined fragment temp {source.index} (fp16={source.fp16})')
        elif source.kind == nv40.FRAG_REG_INPUT:
            base = self.attributes.get(instruction.input_attr)
            if base is None:
                raise ValueError(f'read of unbound fragment attribute {instruction.input_attr}')
        elif source.kind == nv40.FRAG_REG_CONST:
            base = self.constant_overrides.get(index)
            if base is None:
                base = instruction.inline_constant
            if base is None:
                raise ValueError('constant operand without inline payload')
            base = list(base)
        else:
            raise ValueError(f'fragment source kind {source.kind}')
        value = _swizzle(base, source.swizzle)
        if source.absolute:
            value = [abs(component) for component in value]
        if source.negate:
            value = [-component for component in value]
        return value

    def _condition_passes(self, instruction) -> list:
        register = self.condition[1 if instruction.cond_read_reg1 else 0]
        swizzled = _swizzle(register, instruction.cond_swizzle)
        test = instruction.cond_test
        out = []
        for component in swizzled:
            out.append({
                nv40.COND_FALSE: False,
                nv40.COND_LT: component < 0.0,
                nv40.COND_EQ: component == 0.0,
                nv40.COND_LE: component <= 0.0,
                nv40.COND_GT: component > 0.0,
                nv40.COND_NE: component != 0.0,
                nv40.COND_GE: component >= 0.0,
                nv40.COND_TRUE: True,
            }[test])
        return out

    _SCALE = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 5: 0.5, 6: 0.25, 7: 0.125}

    def _write(self, instruction, value: Vec4) -> None:
        scale = self._SCALE.get(instruction.scale)
        if scale is None:
            raise ValueError(f'reserved fragment result scale {instruction.scale}')
        if scale != 1.0:
            value = [component * scale for component in value]
        if instruction.saturate:
            value = [_clamp01(component) for component in value]
        passes = self._condition_passes(instruction)
        if instruction.cond_write:
            register = self.condition[1 if instruction.cond_write_reg1 else 0]
            for component in range(4):
                if instruction.write_mask[component]:
                    register[component] = float(value[component])
        if instruction.no_dest:
            return
        store = self._bank(instruction.dest_fp16).setdefault(instruction.dest_reg, [0.0, 0.0, 0.0, 0.0])
        for component in range(4):
            if instruction.write_mask[component] and passes[component]:
                store[component] = float(value[component])

    def run(self) -> dict:
        try:
            for index, instruction in enumerate(self.instructions):
                self._execute(instruction, index)
        except ShaderKill:
            return {'killed': True}
        return {'killed': False, 'temps': self.temps, 'temps_half': self.temps_half,
                'color0': self.temps.get(0, self.temps_half.get(0))}

    def _execute(self, ins: nv40.FragInstr, index: int) -> None:
        op = ins.opcode
        read = lambda slot: self._read(ins.sources[slot], ins, index)
        if op == nv40.FRAG_OP_NOP:
            return
        if op == nv40.FRAG_OP_KIL:
            if any(self._condition_passes(ins)):
                self.killed = True
                raise ShaderKill()
            return
        count = nv40.fragment_operand_count(op)
        values = [read(slot) for slot in range(count)]
        simple = {
            nv40.FRAG_OP_MOV: lambda a: a,
            nv40.FRAG_OP_MUL: lambda a, b: [x * y for x, y in zip(a, b)],
            nv40.FRAG_OP_ADD: lambda a, b: [x + y for x, y in zip(a, b)],
            nv40.FRAG_OP_MAD: lambda a, b, c: [x * y + z for x, y, z in zip(a, b, c)],
            nv40.FRAG_OP_MIN: lambda a, b: [min(x, y) for x, y in zip(a, b)],
            nv40.FRAG_OP_MAX: lambda a, b: [max(x, y) for x, y in zip(a, b)],
            nv40.FRAG_OP_SLT: lambda a, b: [1.0 if x < y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_SGE: lambda a, b: [1.0 if x >= y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_SLE: lambda a, b: [1.0 if x <= y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_SGT: lambda a, b: [1.0 if x > y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_SNE: lambda a, b: [1.0 if x != y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_SEQ: lambda a, b: [1.0 if x == y else 0.0 for x, y in zip(a, b)],
            nv40.FRAG_OP_FRC: lambda a: [x - math.floor(x) for x in a],
            nv40.FRAG_OP_FLR: lambda a: [math.floor(x) for x in a],
            nv40.FRAG_OP_LRP: lambda a, b, c: [x * y + (1.0 - x) * z for x, y, z in zip(a, b, c)],
            nv40.FRAG_OP_LIT: _lit,
            nv40.FRAG_OP_DST: _dst,
            nv40.FRAG_OP_DDX: lambda a: [0.0, 0.0, 0.0, 0.0],
            nv40.FRAG_OP_DDY: lambda a: [0.0, 0.0, 0.0, 0.0],
        }
        if op in simple:
            self._write(ins, simple[op](*values))
            return
        if op in (nv40.FRAG_OP_DP2, nv40.FRAG_OP_DP3, nv40.FRAG_OP_DP4):
            width = {nv40.FRAG_OP_DP2: 2, nv40.FRAG_OP_DP3: 3, nv40.FRAG_OP_DP4: 4}[op]
            value = sum(x * y for x, y in zip(values[0][:width], values[1][:width]))
            self._write(ins, [value] * 4)
            return
        if op in (nv40.FRAG_OP_RCP, nv40.FRAG_OP_RSQ, nv40.FRAG_OP_EX2, nv40.FRAG_OP_LG2,
                  nv40.FRAG_OP_SIN, nv40.FRAG_OP_COS):
            scalar = values[0][0]
            fn = {nv40.FRAG_OP_RCP: _rcp, nv40.FRAG_OP_RSQ: _rsq, nv40.FRAG_OP_EX2: _ex2,
                  nv40.FRAG_OP_LG2: _lg2_raw, nv40.FRAG_OP_SIN: math.sin, nv40.FRAG_OP_COS: math.cos}[op]
            self._write(ins, [fn(scalar)] * 4)
            return
        if op == nv40.FRAG_OP_POW:
            base = abs(values[0][0])
            value = _ex2(values[1][0] * _lg2_abs(base)) if base != 0.0 else 0.0
            self._write(ins, [value] * 4)
            return
        if op == nv40.FRAG_OP_NRM:
            a = values[0]
            inverse = _rsq(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
            self._write(ins, [a[0] * inverse, a[1] * inverse, a[2] * inverse, 0.0])
            return
        if op in (nv40.FRAG_OP_TEX, nv40.FRAG_OP_TXP, nv40.FRAG_OP_TXL, nv40.FRAG_OP_TXB,
                  nv40.FRAG_OP_TXD):
            coordinate = values[0]
            if op == nv40.FRAG_OP_TXP:
                w = coordinate[3]
                coordinate = [component / w if w else math.inf for component in coordinate]
            kind = self.sampler_kinds.get(ins.tex_unit, '2d')
            self._write(ins, self.sampler(ins.tex_unit, coordinate, kind))
            return
        raise ValueError(f'fragment opcode {op:#x} is outside the interpreter domain')


def run_rsx_vertex(instructions, *, attributes=None, constants=None) -> dict:
    return RsxVertexMachine(instructions, attributes=attributes, constants=constants).run()


def run_rsx_fragment(instructions, *, attributes=None, sampler=None, sampler_kinds=None,
                     constant_overrides=None) -> dict:
    machine = RsxFragmentMachine(instructions, attributes=attributes, sampler=sampler,
                                 sampler_kinds=sampler_kinds,
                                 constant_overrides=constant_overrides)
    return machine.run()

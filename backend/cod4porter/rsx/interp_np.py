"""Vectorized RSX fragment-program execution over pixel grids.

The scalar interpreter in :mod:`cod4porter.rsx.interp` proves per-pixel
semantics; this module runs the same instruction semantics over whole numpy
grids so complete material previews can be shaded in one pass.  Registers are
``(H, W, 4)`` float32 arrays, textures are sampled bilinearly from RGBA
arrays, condition codes and the RSX result-scale field are honored, and KIL
clears the alpha of killed pixels.

Anything outside the modeled opcode set raises :class:`RsxExecError` with the
opcode named, so callers fall back per material instead of shading wrongly.
"""

from __future__ import annotations

import numpy as np

from . import nv40

_COND_OPS = {
    nv40.COND_FALSE: lambda c: np.zeros(c.shape, dtype=bool),
    nv40.COND_LT: lambda c: c < 0.0,
    nv40.COND_EQ: lambda c: c == 0.0,
    nv40.COND_LE: lambda c: c <= 0.0,
    nv40.COND_GT: lambda c: c > 0.0,
    nv40.COND_NE: lambda c: c != 0.0,
    nv40.COND_GE: lambda c: c >= 0.0,
    nv40.COND_TRUE: lambda c: np.ones(c.shape, dtype=bool),
}

_SCALE = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 5: 0.5, 6: 0.25, 7: 0.125}


class RsxExecError(ValueError):
    pass


class GridSampler:
    """Bilinear RGBA sampler over a numpy image (values 0..1), wrap addressing."""

    def __init__(self, rgba: np.ndarray):
        if rgba.ndim != 3 or rgba.shape[2] != 4:
            raise RsxExecError('sampler image must be HxWx4')
        self.rgba = np.asarray(rgba, dtype=np.float32)

    def sample(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        height, width = self.rgba.shape[:2]
        fu = (u % 1.0) * width - 0.5
        fv = (v % 1.0) * height - 0.5
        x0 = np.floor(fu).astype(np.int64)
        y0 = np.floor(fv).astype(np.int64)
        tx = (fu - x0)[..., None]
        ty = (fv - y0)[..., None]
        x0m, x1m = x0 % width, (x0 + 1) % width
        y0m, y1m = y0 % height, (y0 + 1) % height
        c00 = self.rgba[y0m, x0m]
        c10 = self.rgba[y0m, x1m]
        c01 = self.rgba[y1m, x0m]
        c11 = self.rgba[y1m, x1m]
        top = c00 * (1 - tx) + c10 * tx
        bottom = c01 * (1 - tx) + c11 * tx
        return top * (1 - ty) + bottom * ty


def solid_sampler(color) -> GridSampler:
    return GridSampler(np.full((1, 1, 4), color, dtype=np.float32))


class FragmentGridMachine:
    def __init__(self, instructions, shape, *, attributes, samplers,
                 constant_values=None):
        """``attributes``: attr index -> (H,W,4) array or 4-vector.
        ``samplers``: tex unit -> GridSampler.
        ``constant_values``: instruction index -> float4 replacing its inline
        payload (engine-patched constants)."""
        self.instructions = instructions
        self.shape = tuple(shape)
        self.attributes = {}
        for key, value in attributes.items():
            array = np.asarray(value, dtype=np.float32)
            if array.shape == (4,):
                array = np.broadcast_to(array, (*self.shape, 4)).copy()
            self.attributes[key] = array
        self.samplers = dict(samplers)
        self.constant_values = dict(constant_values or {})
        self.temps = {}
        self.temps_half = {}
        self.condition = [np.zeros((*self.shape, 4), dtype=np.float32) for _ in range(2)]
        self.killed = np.zeros(self.shape, dtype=bool)

    def _bank(self, fp16):
        return self.temps_half if fp16 else self.temps

    def _read(self, source: nv40.FragSource, instruction, index):
        if source.kind == nv40.FRAG_REG_TEMP:
            base = self._bank(source.fp16).get(source.index)
            if base is None:
                base = np.zeros((*self.shape, 4), dtype=np.float32)
        elif source.kind == nv40.FRAG_REG_INPUT:
            base = self.attributes.get(instruction.input_attr)
            if base is None:
                base = np.zeros((*self.shape, 4), dtype=np.float32)
        elif source.kind == nv40.FRAG_REG_CONST:
            payload = self.constant_values.get(index, instruction.inline_constant)
            if payload is None:
                raise RsxExecError('constant operand without payload')
            base = np.broadcast_to(np.asarray(payload, dtype=np.float32),
                                   (*self.shape, 4))
        else:
            raise RsxExecError(f'source kind {source.kind}')
        value = base[..., list(source.swizzle)]
        if source.absolute:
            value = np.abs(value)
        if source.negate:
            value = -value
        return value

    def _cond_mask(self, instruction):
        register = self.condition[1 if instruction.cond_read_reg1 else 0]
        swizzled = register[..., list(instruction.cond_swizzle)]
        return _COND_OPS[instruction.cond_test](swizzled)

    def _write(self, instruction, value):
        value = np.asarray(value, dtype=np.float32)
        if value.shape != (*self.shape, 4):
            value = np.broadcast_to(value, (*self.shape, 4)).copy()
        scale = _SCALE.get(instruction.scale)
        if scale is None:
            raise RsxExecError(f'result scale {instruction.scale}')
        if scale != 1.0:
            value = value * scale
        if instruction.saturate:
            value = np.clip(value, 0.0, 1.0)
        passes = self._cond_mask(instruction)
        if instruction.cond_write:
            target = self.condition[1 if instruction.cond_write_reg1 else 0]
            for component in range(4):
                if instruction.write_mask[component]:
                    target[..., component] = value[..., component]
        if instruction.no_dest:
            return
        bank = self._bank(instruction.dest_fp16)
        store = bank.get(instruction.dest_reg)
        if store is None:
            store = np.zeros((*self.shape, 4), dtype=np.float32)
            bank[instruction.dest_reg] = store
        for component in range(4):
            if instruction.write_mask[component]:
                mask = passes[..., component]
                store[..., component][mask] = value[..., component][mask]

    def run(self):
        for index, instruction in enumerate(self.instructions):
            self._execute(instruction, index)
        color = self.temps.get(0)
        if color is None:
            color = self.temps_half.get(0)
        if color is None:
            raise RsxExecError('program wrote no color output register')
        color = color.copy()
        color[..., 3][self.killed] = 0.0
        return color

    def _execute(self, ins: nv40.FragInstr, index: int):
        op = ins.opcode
        if op == nv40.FRAG_OP_NOP or op in (nv40.FRAG_OP_FENCT, nv40.FRAG_OP_FENCB):
            return
        if op == nv40.FRAG_OP_KIL:
            self.killed |= self._cond_mask(ins).any(axis=-1)
            return
        count = nv40.fragment_operand_count(op)
        values = [self._read(ins.sources[slot], ins, index) for slot in range(count)]
        E = 1e-38

        def dot(width):
            product = values[0][..., :width] * values[1][..., :width]
            return np.repeat(product.sum(axis=-1, keepdims=True), 4, axis=-1)

        if op == nv40.FRAG_OP_MOV:
            result = values[0]
        elif op == nv40.FRAG_OP_MUL:
            result = values[0] * values[1]
        elif op == nv40.FRAG_OP_ADD:
            result = values[0] + values[1]
        elif op == nv40.FRAG_OP_MAD:
            result = values[0] * values[1] + values[2]
        elif op == nv40.FRAG_OP_DP2:
            result = dot(2)
        elif op == nv40.FRAG_OP_DP3:
            result = dot(3)
        elif op == nv40.FRAG_OP_DP4:
            result = dot(4)
        elif op == nv40.FRAG_OP_DP2A:
            result = np.repeat((values[0][..., :2] * values[1][..., :2]).sum(axis=-1, keepdims=True),
                               4, axis=-1) + values[2]
        elif op == nv40.FRAG_OP_MIN:
            result = np.minimum(values[0], values[1])
        elif op == nv40.FRAG_OP_MAX:
            result = np.maximum(values[0], values[1])
        elif op in (nv40.FRAG_OP_SLT, nv40.FRAG_OP_SGE, nv40.FRAG_OP_SLE,
                    nv40.FRAG_OP_SGT, nv40.FRAG_OP_SNE, nv40.FRAG_OP_SEQ):
            comparison = {
                nv40.FRAG_OP_SLT: np.less, nv40.FRAG_OP_SGE: np.greater_equal,
                nv40.FRAG_OP_SLE: np.less_equal, nv40.FRAG_OP_SGT: np.greater,
                nv40.FRAG_OP_SNE: np.not_equal, nv40.FRAG_OP_SEQ: np.equal,
            }[op]
            result = comparison(values[0], values[1]).astype(np.float32)
        elif op == nv40.FRAG_OP_FRC:
            result = values[0] - np.floor(values[0])
        elif op == nv40.FRAG_OP_FLR:
            result = np.floor(values[0])
        elif op == nv40.FRAG_OP_LRP:
            result = values[0] * values[1] + (1.0 - values[0]) * values[2]
        elif op == nv40.FRAG_OP_RCP:
            result = np.repeat(1.0 / np.where(np.abs(values[0][..., :1]) < E,
                                              np.copysign(E, values[0][..., :1]),
                                              values[0][..., :1]), 4, axis=-1)
        elif op == nv40.FRAG_OP_RSQ:
            result = np.repeat(1.0 / np.sqrt(np.maximum(np.abs(values[0][..., :1]), E)), 4, axis=-1)
        elif op == nv40.FRAG_OP_EX2:
            result = np.repeat(np.exp2(np.clip(values[0][..., :1], -126, 128)), 4, axis=-1)
        elif op == nv40.FRAG_OP_LG2:
            result = np.repeat(np.log2(np.maximum(np.abs(values[0][..., :1]), E)), 4, axis=-1)
        elif op == nv40.FRAG_OP_SIN:
            result = np.repeat(np.sin(values[0][..., :1]), 4, axis=-1)
        elif op == nv40.FRAG_OP_COS:
            result = np.repeat(np.cos(values[0][..., :1]), 4, axis=-1)
        elif op == nv40.FRAG_OP_POW:
            base = np.maximum(np.abs(values[0][..., :1]), E)
            result = np.repeat(np.exp2(values[1][..., :1] * np.log2(base)), 4, axis=-1)
        elif op == nv40.FRAG_OP_NRM:
            length = np.sqrt(np.maximum((values[0][..., :3] ** 2).sum(axis=-1, keepdims=True), E))
            result = np.concatenate([values[0][..., :3] / length,
                                     np.zeros((*self.shape, 1), dtype=np.float32)], axis=-1)
        elif op == nv40.FRAG_OP_LIT:
            x = np.maximum(values[0][..., 0:1], 0.0)
            y = np.maximum(values[0][..., 1:2], 0.0)
            w = np.clip(values[0][..., 3:4], -127.9961, 127.9961)
            specular = np.where(values[0][..., 0:1] > 0.0,
                                np.power(np.maximum(y, E), w), 0.0)
            ones = np.ones_like(x)
            result = np.concatenate([ones, x, specular, ones], axis=-1)
        elif op == nv40.FRAG_OP_DST:
            ones = np.ones((*self.shape, 1), dtype=np.float32)
            result = np.concatenate([ones, values[0][..., 1:2] * values[1][..., 1:2],
                                     values[0][..., 2:3], values[1][..., 3:4]], axis=-1)
        elif op in (nv40.FRAG_OP_DDX, nv40.FRAG_OP_DDY):
            result = np.zeros((*self.shape, 4), dtype=np.float32)
        elif op in (nv40.FRAG_OP_TEX, nv40.FRAG_OP_TXP, nv40.FRAG_OP_TXL, nv40.FRAG_OP_TXB):
            coordinate = values[0]
            if op == nv40.FRAG_OP_TXP:
                w = coordinate[..., 3:4]
                w = np.where(np.abs(w) < E, np.copysign(E, w), w)
                coordinate = coordinate / w
            sampler = self.samplers.get(ins.tex_unit)
            if sampler is None:
                sampler = solid_sampler((1.0, 1.0, 1.0, 1.0))
                self.samplers[ins.tex_unit] = sampler
            result = sampler.sample(coordinate[..., 0], coordinate[..., 1])
        else:
            raise RsxExecError(f'fragment opcode 0x{op:02X} is outside the preview executor')
        self._write(ins, result)


def execute_fragment_grid(instructions, shape, *, attributes, samplers,
                          constant_values=None):
    machine = FragmentGridMachine(instructions, shape, attributes=attributes,
                                  samplers=samplers, constant_values=constant_values)
    return machine.run()

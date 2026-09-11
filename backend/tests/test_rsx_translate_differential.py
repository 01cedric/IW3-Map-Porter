#!/usr/bin/env python3
"""Differential proof of the D3D9->RSX translator.

Every supported construct is executed twice over identical random inputs:
once through the exact D3D9 interpreter, once through encode->decode->RSX
interpretation of the translated program.  The outputs must agree.
"""
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.rsx import d3d9, interp, nv40, translate
from cod4porter.rsx.d3d9 import assemble, dst, src
from cod4porter.rsx.translate import compile_pixel_shader, compile_vertex_shader

R = d3d9.REG_TEMP
V = d3d9.REG_INPUT
C = d3d9.REG_CONST
T = d3d9.REG_ADDR
S = d3d9.REG_SAMPLER
OPOS = d3d9.REG_RASTOUT
OD = d3d9.REG_ATTROUT
OT = d3d9.REG_TEXCRDOUT
OC = d3d9.REG_COLOROUT

RESULT_OF = {
    (OPOS, 0): nv40.VERT_RESULT_POSITION,
    (OPOS, 1): nv40.VERT_RESULT_FOG,
    (OPOS, 2): nv40.VERT_RESULT_PSIZE,
    (OD, 0): nv40.VERT_RESULT_COL0,
    (OD, 1): nv40.VERT_RESULT_COL1,
    **{(OT, index): nv40.VERT_RESULT_TEX0 + index for index in range(8)},
}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def close(a, b, tolerance=2e-4):
    if math.isinf(a) and math.isinf(b) and (a > 0) == (b > 0):
        return True
    if math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def compare_vec(label, a, b):
    for component in range(4):
        check(close(a[component], b[component]),
              f'{label}[{"xyzw"[component]}]: d3d9={a[component]!r} rsx={b[component]!r}')


def run_vertex_case(name, body, *, draws=20, seed=7, compare=None):
    blob = assemble('vertex', 2, 0, body)
    program = d3d9.disassemble(blob)
    translation = compile_vertex_shader(program)
    decoded = nv40.decode_vertex_program(translation.ucode)
    check(decoded == translation.instructions, f'{name}: vertex encode/decode drift')
    rng = random.Random(seed)
    declared_inputs = [d.dest.register_number for d in program.input_declarations()]
    input_attr = {d.dest.register_number: translation.attr_by_usage[(d.usage, d.usage_index)]
                  for d in program.input_declarations()}
    const_registers = sorted({p.register_number
                              for i in program.instructions for p in i.sources
                              if p.register_type == C} |
                             {p.register_number + row
                              for i in program.instructions
                              if i.opcode in (d3d9.OP_M4X4, d3d9.OP_M4X3, d3d9.OP_M3X4,
                                              d3d9.OP_M3X3, d3d9.OP_M3X2)
                              for p in [i.sources[1]]
                              for row in range(4)})
    for draw in range(draws):
        constants = {register: [rng.uniform(-2, 2) for _ in range(4)] for register in const_registers}
        for definition in program.defs:
            constants[definition.register_number] = list(definition.value)
        inputs = {(V, number): [rng.uniform(-2, 2) for _ in range(4)] for number in declared_inputs}
        d3d_result = interp.run_d3d9(program, inputs=inputs, constants=constants)
        check(not d3d_result['killed'], f'{name}: vertex kill?')
        attributes = {input_attr[number]: value for (_, number), value in inputs.items()}
        rsx_results = interp.run_rsx_vertex(decoded, attributes=attributes, constants=constants)
        outputs = d3d_result['outputs']
        keys = compare if compare is not None else list(outputs)
        for key in keys:
            expected = outputs[key]
            result_register = RESULT_OF[key]
            actual = rsx_results.get(result_register)
            check(actual is not None, f'{name}: RSX never wrote result {result_register} ({key})')
            written = [component for component in range(4)
                       if any(i.dest is not None and
                              i.dest.register_type == key[0] and
                              i.dest.register_number == key[1] and
                              i.dest.write_mask >> component & 1
                              for i in program.instructions)]
            for component in written:
                check(close(expected[component], actual[component]),
                      f'{name} draw {draw} {key}[{"xyzw"[component]}]: '
                      f'd3d9={expected[component]!r} rsx={actual[component]!r}')


def run_pixel_case(name, body, *, major=2, draws=20, seed=11, expect_kill=None):
    blob = assemble('pixel', major, 0, body)
    program = d3d9.disassemble(blob)
    translation = compile_pixel_shader(program)
    decoded = nv40.decode_fragment_program(translation.ucode)
    check(decoded == translation.instructions, f'{name}: fragment encode/decode drift')
    rng = random.Random(seed)
    input_keys = []
    attr_of = {}
    for declaration in program.declarations:
        dest = declaration.dest
        if dest.register_type == V:
            key = (V, dest.register_number)
            if major >= 3:
                attr_of[key] = translate.PIXEL_USAGE_TO_ATTR[(declaration.usage, declaration.usage_index)]
            else:
                attr_of[key] = nv40.FRAG_ATTR_COL0 + dest.register_number
            input_keys.append(key)
        elif dest.register_type == T:
            key = (T, dest.register_number)
            attr_of[key] = nv40.FRAG_ATTR_TEX0 + dest.register_number
            input_keys.append(key)
    kills = 0
    for draw in range(draws):
        inputs = {key: [rng.uniform(-1.5, 1.5) for _ in range(4)] for key in input_keys}
        d3d_result = interp.run_d3d9(program, inputs=inputs)
        attributes = {attr_of[key]: value for key, value in inputs.items()}
        rsx_result = interp.run_rsx_fragment(decoded, attributes=attributes)
        check(d3d_result['killed'] == rsx_result['killed'],
              f'{name} draw {draw}: kill mismatch d3d9={d3d_result["killed"]} rsx={rsx_result["killed"]}')
        if d3d_result['killed']:
            kills += 1
            continue
        color = d3d_result['outputs'].get((OC, 0))
        check(color is not None, f'{name}: d3d9 never wrote oC0')
        compare_vec(f'{name} draw {draw} oC0', color, rsx_result['color0'])
        depth = d3d_result['outputs'].get((d3d9.REG_DEPTHOUT, 0))
        if depth is not None:
            r1 = rsx_result['temps'].get(1)
            check(r1 is not None, f'{name}: depth export missing')
            check(close(depth[0], r1[2]), f'{name}: depth {depth[0]} != R1.z {r1[2]}')
    if expect_kill == 'some':
        check(0 < kills < draws, f'{name}: expected mixed kills, got {kills}/{draws}')


def test_vertex_arithmetic():
    run_vertex_case('mad-chain', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_NORMAL, 0, dst(V, 1)),
        (d3d9.OP_MAD, 0, dst(R, 0), (src(V, 0), src(C, 4), src(C, 5))),
        (d3d9.OP_ADD, 0, dst(R, 0, 'xy'), (src(R, 0), src(V, 1, 'zzzw'))),
        (d3d9.OP_MUL, 0, dst(R, 1), (src(R, 0, 'wzyx'), src(C, 6, 'yyxx', d3d9.SRCMOD_NEG))),
        (d3d9.OP_DP4, 0, dst(OPOS, 0, 'x'), (src(R, 1), src(C, 0))),
        (d3d9.OP_DP4, 0, dst(OPOS, 0, 'y'), (src(R, 1), src(C, 1))),
        (d3d9.OP_DP3, 0, dst(OPOS, 0, 'z'), (src(R, 0), src(C, 2))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0, 'w'), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0), (src(R, 1),)),
    ])


def test_vertex_minmax_setops():
    run_vertex_case('setops', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_MIN, 0, dst(R, 0), (src(V, 0), src(C, 0))),
        (d3d9.OP_MAX, 0, dst(R, 1), (src(V, 0), src(C, 1))),
        (d3d9.OP_SLT, 0, dst(R, 2), (src(R, 0), src(R, 1))),
        (d3d9.OP_SGE, 0, dst(R, 3), (src(R, 0, 'yxwz'), src(R, 1))),
        (d3d9.OP_FRC, 0, dst(R, 4), (src(V, 0),)),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 2),)),
        (d3d9.OP_MOV, 0, dst(OT, 0), (src(R, 3),)),
        (d3d9.OP_MOV, 0, dst(OT, 1), (src(R, 4),)),
    ])


def test_vertex_sub_abs_sgn():
    run_vertex_case('sub-abs-sgn', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_SUB, 0, dst(R, 0), (src(V, 0), src(C, 2, 'xyzw', d3d9.SRCMOD_NEG))),
        (d3d9.OP_ABS, 0, dst(R, 1), (src(R, 0, 'wzyx'),)),
        (d3d9.OP_SGN, 0, dst(R, 2), (src(R, 0), src(R, 6), src(R, 7))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 1),)),
        (d3d9.OP_MOV, 0, dst(OT, 2), (src(R, 2),)),
    ])


def test_vertex_scalar_ops():
    run_vertex_case('scalar', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_RCP, 0, dst(R, 0, 'x'), (src(V, 0, 'w'),)),
        (d3d9.OP_RSQ, 0, dst(R, 0, 'y'), (src(V, 0, 'x'),)),
        (d3d9.OP_EXP, 0, dst(R, 0, 'z'), (src(V, 0, 'y'),)),
        (d3d9.OP_LOG, 0, dst(R, 0, 'w'), (src(V, 0, 'z'),)),
        (d3d9.OP_LIT, 0, dst(R, 1), (src(V, 0),)),
        (d3d9.OP_DST, 0, dst(R, 2), (src(V, 0, 'xyzw', d3d9.SRCMOD_ABS), src(C, 3))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0), (src(R, 1),)),
        (d3d9.OP_MOV, 0, dst(OT, 1), (src(R, 2),)),
    ])


def test_vertex_lrp_nrm_crs_pow():
    run_vertex_case('composite', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_NORMAL, 0, dst(V, 1)),
        (d3d9.OP_LRP, 0, dst(R, 0), (src(V, 0, 'x'), src(C, 0), src(C, 1))),
        (d3d9.OP_NRM, 0, dst(R, 1, 'xyz'), (src(V, 1),)),
        (d3d9.OP_CRS, 0, dst(R, 2, 'xyz'), (src(V, 0), src(V, 1))),
        (d3d9.OP_POW, 0, dst(R, 3, 'x'), (src(V, 0, 'x'), src(C, 2, 'y'))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0, 'xyz'), (src(R, 1),)),
        (d3d9.OP_MOV, 0, dst(OT, 1, 'xyz'), (src(R, 2),)),
        (d3d9.OP_MOV, 0, dst(OT, 2, 'x'), (src(R, 3, 'x'),)),
    ])


def test_vertex_sincos_and_matrix():
    run_vertex_case('sincos-matrix', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_SINCOS, 0, dst(R, 0, 'xy'),
         (src(V, 0, 'x'), src(C, 30), src(C, 31))),
        (d3d9.OP_M4X4, 0, dst(R, 1), (src(V, 0), src(C, 8))),
        (d3d9.OP_M3X3, 0, dst(R, 2, 'xyz'), (src(V, 0), src(C, 12))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 1),)),
        (d3d9.OP_MOV, 0, dst(OT, 0, 'xy'), (src(R, 0, 'xyxx'),)),
        (d3d9.OP_MOV, 0, dst(OT, 1, 'xyz'), (src(R, 2),)),
    ])


def test_vertex_outputs_and_saturate():
    run_vertex_case('outputs', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_COLOR, 0, dst(V, 3)),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(V, 0),)),
        (d3d9.OP_MOV, 0, dst(OD, 0, 'xyzw', d3d9.DSTMOD_SATURATE), (src(V, 3),)),
        (d3d9.OP_ADD, 0, dst(OD, 1), (src(V, 3), src(C, 7))),
        (d3d9.OP_MUL, 0, dst(OPOS, 1, 'x'), (src(V, 0, 'z'), src(C, 9, 'x'))),
        (d3d9.OP_MOV, 0, dst(OPOS, 2, 'x'), (src(C, 9, 'w'),)),
        (d3d9.OP_MOV, 0, dst(OT, 5), (src(V, 0, 'zwxy'),)),
    ])


def test_vertex_shared_operand_splitting():
    # two different constants and two different inputs in single instructions
    run_vertex_case('operand-split', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_NORMAL, 0, dst(V, 1)),
        ('dcl', d3d9.USAGE_TEXCOORD, 0, dst(V, 2)),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(V, 0), src(V, 1))),
        (d3d9.OP_MAD, 0, dst(R, 1), (src(C, 0), src(C, 1), src(C, 2))),
        (d3d9.OP_MAD, 0, dst(R, 2), (src(V, 2), src(C, 3), src(V, 1, 'wzyx'))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0), (src(R, 1),)),
        (d3d9.OP_MOV, 0, dst(OT, 1), (src(R, 2),)),
    ])


def test_vertex_def_constants():
    run_vertex_case('defs', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('def', 50, (0.25, -1.5, 3.0, 0.0)),
        (d3d9.OP_MAD, 0, dst(R, 0), (src(V, 0), src(C, 50, 'x'), src(C, 50, 'y'))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(R, 0),)),
    ])


def test_pixel_arithmetic_and_texture():
    run_pixel_case('ps2-basic', [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl', 0, 0, dst(V, 0)),
        ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 0)),
        ('def', 0, (0.6, 0.3, 0.9, 1.0)),
        (d3d9.OP_TEXLD, 0, dst(R, 0), (src(T, 0), src(S, 0))),
        (d3d9.OP_MUL, 0, dst(R, 0), (src(R, 0), src(V, 0))),
        (d3d9.OP_MAD, 0, dst(R, 0, 'xyz'), (src(R, 0), src(C, 0), src(C, 0, 'wwww'))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])


def test_pixel_multi_const_and_inputs():
    run_pixel_case('ps2-split', [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl', 0, 0, dst(T, 1)),
        ('def', 1, (0.2, 0.4, 0.6, 0.8)),
        ('def', 2, (-0.5, 1.5, 2.5, -3.5)),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(T, 0), src(T, 1))),
        (d3d9.OP_MAD, 0, dst(R, 1), (src(C, 1), src(C, 2), src(R, 0))),
        (d3d9.OP_LRP, 0, dst(R, 2), (src(T, 0, 'x'), src(R, 1), src(C, 1))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 2),)),
    ])


def test_pixel_cmp_pow_nrm_dp2add():
    run_pixel_case('ps2-composite', [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl', 0, 0, dst(T, 1)),
        ('def', 0, (0.5, 2.0, -1.0, 0.25)),
        (d3d9.OP_CMP, 0, dst(R, 0), (src(T, 0), src(T, 1), src(C, 0))),
        (d3d9.OP_POW, 0, dst(R, 1, 'x'), (src(T, 0, 'y'), src(C, 0, 'y'))),
        (d3d9.OP_NRM, 0, dst(R, 2, 'xyz'), (src(T, 1),)),
        (d3d9.OP_DP2ADD, 0, dst(R, 3, 'x'), (src(T, 0), src(T, 1), src(C, 0, 'w'))),
        (d3d9.OP_MUL, 0, dst(R, 0, 'w'), (src(R, 1, 'x'), src(R, 3, 'x'))),
        (d3d9.OP_ADD, 0, dst(R, 0, 'xyz'), (src(R, 0), src(R, 2))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])


def test_pixel_scalar_and_sincos():
    run_pixel_case('ps2-scalar', [
        ('dcl', 0, 0, dst(T, 0)),
        (d3d9.OP_RCP, 0, dst(R, 0, 'x'), (src(T, 0, 'w'),)),
        (d3d9.OP_RSQ, 0, dst(R, 0, 'y'), (src(T, 0, 'x'),)),
        (d3d9.OP_EXP, 0, dst(R, 0, 'z'), (src(T, 0, 'y'),)),
        (d3d9.OP_LOG, 0, dst(R, 0, 'w'), (src(T, 0, 'z'),)),
        (d3d9.OP_SINCOS, 0, dst(R, 1, 'xy'), (src(T, 0, 'x'), src(C, 30), src(C, 31))),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(R, 0), src(R, 1, 'xyxy'))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])


def test_pixel_texkill():
    run_pixel_case('ps2-texkill', [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 0)),
        (d3d9.OP_TEXKILL, 0, dst(T, 0), ()),
        (d3d9.OP_TEXLD, 0, dst(R, 0), (src(T, 0), src(S, 0))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ], expect_kill='some')


def test_pixel_texldp_and_depth():
    run_pixel_case('ps3-proj-depth', [
        ('dcl', d3d9.USAGE_TEXCOORD, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_COLOR, 0, dst(V, 1)),
        ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 3)),
        (d3d9.OP_TEXLD, 0x1, dst(R, 0), (src(V, 0), src(S, 3))),
        (d3d9.OP_MUL, 0, dst(R, 0), (src(R, 0), src(V, 1))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_DEPTHOUT, 0, 'x'), (src(R, 0, 'w'),)),
    ], major=3)


def test_pixel_saturate_and_masks():
    run_pixel_case('ps2-sat', [
        ('dcl', 0, 0, dst(T, 0)),
        (d3d9.OP_ADD, 0, dst(R, 0, 'xyzw', d3d9.DSTMOD_SATURATE), (src(T, 0), src(T, 0, 'wzyx'))),
        (d3d9.OP_MUL, 0, dst(R, 0, 'yw'), (src(R, 0), src(T, 0, 'x'))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])


def test_pixel_sub_abs_minmax_setops():
    run_pixel_case('ps2-setops', [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl', 0, 0, dst(T, 1)),
        (d3d9.OP_SUB, 0, dst(R, 0), (src(T, 0), src(T, 1))),
        (d3d9.OP_ABS, 0, dst(R, 1), (src(R, 0, 'zwxy'),)),
        (d3d9.OP_MIN, 0, dst(R, 2), (src(R, 0), src(R, 1))),
        (d3d9.OP_MAX, 0, dst(R, 3), (src(R, 0), src(T, 1, 'xxzz', d3d9.SRCMOD_NEG))),
        (d3d9.OP_SLT, 0, dst(R, 2, 'xy'), (src(R, 2), src(R, 3))),
        (d3d9.OP_SGE, 0, dst(R, 2, 'zw'), (src(R, 2), src(R, 3))),
        (d3d9.OP_FRC, 0, dst(R, 4), (src(R, 3),)),
        (d3d9.OP_DP3, 0, dst(R, 5, 'x'), (src(R, 2), src(R, 4))),
        (d3d9.OP_DP4, 0, dst(R, 5, 'y'), (src(R, 2), src(R, 4))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 5, 'xyxy'),)),
    ])


def test_pixel_dsx_dsy():
    run_pixel_case('ps3-gradients', [
        ('dcl', d3d9.USAGE_TEXCOORD, 0, dst(V, 0)),
        (d3d9.OP_DSX, 0, dst(R, 0), (src(V, 0),)),
        (d3d9.OP_DSY, 0, dst(R, 1), (src(V, 0),)),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(R, 0), src(R, 1))),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(R, 0), src(V, 0))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ], major=3)


def test_mrt_export_ordering():
    # Audit finding 2: accumulators must never live in an export register
    # that an earlier export move overwrites.  Write oC2 first, then oC0/oC1,
    # with r0/r1 occupied so scratch allocation starts at the export range.
    blob = assemble('pixel', 3, 0, [
        ('dcl', d3d9.USAGE_TEXCOORD, 0, dst(V, 0)),
        (d3d9.OP_MOV, 0, dst(R, 0), (src(V, 0),)),
        (d3d9.OP_MOV, 0, dst(R, 1), (src(V, 0, 'wzyx'),)),
        (d3d9.OP_ADD, 0, dst(OC, 2), (src(R, 0), src(R, 1))),
        (d3d9.OP_MUL, 0, dst(OC, 0), (src(R, 0), src(R, 1))),
        (d3d9.OP_SUB, 0, dst(OC, 1), (src(R, 0), src(R, 1))),
    ])
    program = d3d9.disassemble(blob)
    translation = compile_pixel_shader(program)
    decoded = nv40.decode_fragment_program(translation.ucode)
    check(decoded == translation.instructions, 'mrt encode/decode drift')
    rng = random.Random(21)
    for draw in range(20):
        value = [rng.uniform(-2, 2) for _ in range(4)]
        d3d_result = interp.run_d3d9(program, inputs={(V, 0): value})
        rsx_result = interp.run_rsx_fragment(decoded,
                                             attributes={nv40.FRAG_ATTR_TEX0: value})
        for oc_index, register in ((0, 0), (1, 2), (2, 3)):
            expected = d3d_result['outputs'][(OC, oc_index)]
            actual = rsx_result['temps'].get(register)
            check(actual is not None, f'draw {draw}: R{register} unwritten')
            compare_vec(f'draw {draw} oC{oc_index}->R{register}', expected, actual)


def test_scalar_interpreter_applies_result_scale():
    # Audit finding 3: decoded retail microcode carries result scales.
    from cod4porter.rsx.nv40 import FragInstr, FragSource
    for scale, factor in ((1, 2.0), (2, 4.0), (5, 0.5)):
        instructions = [
            FragInstr(opcode=nv40.FRAG_OP_MOV, dest_reg=0, end=True, scale=scale,
                      sources=(FragSource(kind=nv40.FRAG_REG_INPUT),), input_attr=4),
        ]
        image = nv40.encode_fragment_program(instructions)
        decoded = nv40.decode_fragment_program(image)
        result = interp.run_rsx_fragment(decoded,
                                         attributes={nv40.FRAG_ATTR_TEX0: [0.5, -1.0, 2.0, 0.25]})
        for component, base in enumerate([0.5, -1.0, 2.0, 0.25]):
            check(abs(result['color0'][component] - base * factor) < 1e-6,
                  f'scale {scale} component {component}')
    import numpy as np
    from cod4porter.rsx.interp_np import execute_fragment_grid
    instructions = [
        FragInstr(opcode=nv40.FRAG_OP_MOV, dest_reg=0, end=True, scale=1,
                  sources=(FragSource(kind=nv40.FRAG_REG_INPUT),), input_attr=4),
    ]
    decoded = nv40.decode_fragment_program(nv40.encode_fragment_program(instructions))
    grid = execute_fragment_grid(decoded, (1, 1),
                                 attributes={nv40.FRAG_ATTR_TEX0: (0.5, -1.0, 2.0, 0.25)},
                                 samplers={})
    scalar = interp.run_rsx_fragment(decoded,
                                     attributes={nv40.FRAG_ATTR_TEX0: [0.5, -1.0, 2.0, 0.25]})
    for component in range(4):
        check(abs(float(grid[0, 0, component]) - scalar['color0'][component]) < 1e-6,
              'grid/scalar scale agreement')


def test_sincos_in_place_reads_the_angle_once():
    # Audit: sincos lowered as COS-then-SIN clobbered its own source when
    # dest register == source register (legal D3D9); SIN then read cos(x).
    body = [
        ('dcl', 0, 0, dst(T, 0)),
        (d3d9.OP_MOV, 0, dst(R, 0), (src(T, 0),)),
        (d3d9.OP_SINCOS, 0, dst(R, 0, 'xy'), (src(R, 0, 'x'), src(C, 30), src(C, 31))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ]
    run_pixel_case('sincos-in-place', body)
    run_vertex_case('sincos-in-place', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_MOV, 0, dst(R, 0), (src(V, 0),)),
        (d3d9.OP_SINCOS, 0, dst(R, 0, 'xy'), (src(R, 0, 'x'), src(C, 30), src(C, 31))),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(V, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0, 'xy'), (src(R, 0, 'xyxx'),)),
    ])


def test_cmp_never_multiplies_the_rejected_operand():
    # Audit: sge+lrp lowering computed 0*inf = NaN when the rejected operand
    # was non-finite - exactly the rcp+cmp divide-guard idiom.  The CC select
    # must return the selected side untouched, and a NaN condition selects s2
    # like D3D9's failed s0 >= 0.
    blob = assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        ('def', 0, (0.25, 0.5, 0.75, 1.0)),
        (d3d9.OP_RCP, 0, dst(R, 0), (src(T, 0, 'x'),)),
        (d3d9.OP_MUL, 0, dst(R, 2), (src(R, 0), src(T, 0, 'z'))),
        (d3d9.OP_CMP, 0, dst(R, 1), (src(T, 0, 'y'), src(C, 0), src(R, 0))),
        (d3d9.OP_CMP, 0, dst(R, 3), (src(R, 2), src(C, 0), src(R, 1))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 3),)),
    ])
    program = d3d9.disassemble(blob)
    translation = compile_pixel_shader(program)
    decoded = nv40.decode_fragment_program(translation.ucode)
    check(decoded == translation.instructions, 'cmp encode/decode drift')
    cases = [
        [0.0, 1.0, 0.0, 0.0],    # rcp -> inf rejected; inf*0 = NaN condition -> s2
        [0.0, -1.0, 1.0, 0.0],   # inf selected on the inner cmp
        [2.0, 1.0, 0.5, 0.0],    # finite everywhere
        [-0.5, -2.0, 1.0, 0.0],  # negative conditions
    ]
    for index, value in enumerate(cases):
        d3d_result = interp.run_d3d9(program, inputs={(T, 0): value})
        rsx_result = interp.run_rsx_fragment(decoded,
                                             attributes={nv40.FRAG_ATTR_TEX0: value})
        compare_vec(f'cmp case {index} oC0',
                    d3d_result['outputs'][(OC, 0)], rsx_result['color0'])


def test_log_matches_d3d9_absolute_semantics():
    # Audit: LG2 was emitted without |x| while both interpreters modelled
    # log2(|x|) - a shared blind spot.  The RSX interpreter is now raw
    # (NaN for negative input), so these draws fail unless the translator
    # carries the absolute modifier.
    run_pixel_case('log-abs', [
        ('dcl', 0, 0, dst(T, 0)),
        (d3d9.OP_LOG, 0, dst(R, 0), (src(T, 0, 'x'),)),
        (d3d9.OP_LOG, 0, dst(R, 1), (src(T, 0, 'y', d3d9.SRCMOD_NEG),)),
        (d3d9.OP_MAX, 0, dst(R, 0), (src(R, 0), src(R, 1))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])
    run_vertex_case('log-abs', [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_LOG, 0, dst(R, 0, 'x'), (src(V, 0, 'x'),)),
        (d3d9.OP_LOG, 0, dst(R, 0, 'y'), (src(V, 0, 'y', d3d9.SRCMOD_NEG),)),
        (d3d9.OP_MOV, 0, dst(OPOS, 0), (src(V, 0),)),
        (d3d9.OP_MOV, 0, dst(OT, 0, 'xy'), (src(R, 0, 'xyxx'),)),
    ])


def test_fragment_constant_patch_offsets():
    blob = assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        ('def', 3, (1.0, 2.0, 3.0, 4.0)),
        (d3d9.OP_MUL, 0, dst(R, 0), (src(T, 0), src(C, 3))),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(R, 0), src(C, 5))),
        (d3d9.OP_MAD, 0, dst(R, 0), (src(R, 0), src(C, 3, 'x'), src(R, 0))),
        (d3d9.OP_MOV, 0, dst(OC, 0), (src(R, 0),)),
    ])
    translation = compile_pixel_shader(blob)
    offsets = translation.constant_patch_offsets()
    check(set(offsets) == {3, 5}, f'const registers {sorted(offsets)}')
    check(len(offsets[3]) == 2 and len(offsets[5]) == 1, f'patch counts {offsets}')
    ucode = translation.ucode
    for register, sites in offsets.items():
        expected = translation.const_defaults.get(register, (0.0, 0.0, 0.0, 0.0))
        for site in sites:
            import struct as _s
            words = [nv40.fragment_word(_s.unpack_from(">I", ucode, site + 4 * i)[0]) for i in range(4)]
            values = _s.unpack('>4f', _s.pack('>4I', *words))
            for a, b in zip(values, expected):
                check(close(a, b), f'baked default for c{register} at {site}: {values} != {expected}')


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()

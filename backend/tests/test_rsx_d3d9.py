#!/usr/bin/env python3
"""D3D9 SM2/SM3 disassembler grammar, round trip and fail-closed checks."""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.rsx import d3d9
from cod4porter.rsx.d3d9 import (
    D3d9Error, DestParam, SourceParam, assemble, disassemble, dst, src,
    OP_ADD, OP_DCL, OP_DEF, OP_DP3, OP_MAD, OP_MOV, OP_MUL, OP_NRM, OP_RSQ,
    OP_SINCOS, OP_TEXLD, OP_TEXKILL,
    REG_ADDR, REG_COLOROUT, REG_CONST, REG_INPUT, REG_RASTOUT, REG_SAMPLER,
    REG_TEMP, REG_TEXCRDOUT,
    SAMPLER_2D, SRCMOD_ABS, SRCMOD_NEG,
    USAGE_NORMAL, USAGE_POSITION, USAGE_TEXCOORD,
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_error(data, needle):
    try:
        disassemble(data)
    except D3d9Error as error:
        check(needle in str(error), f'error {error} does not mention {needle!r}')
    else:
        raise AssertionError(f'expected D3d9Error mentioning {needle!r}')


def _vertex_fixture():
    return assemble('vertex', 2, 0, [
        ('dcl', USAGE_POSITION, 0, dst(REG_INPUT, 0)),
        ('dcl', USAGE_NORMAL, 0, dst(REG_INPUT, 1)),
        ('dcl', USAGE_TEXCOORD, 0, dst(REG_INPUT, 2)),
        ('def', 40, (0.5, 1.5, -2.0, 0.0)),
        (OP_DP3, 0, dst(REG_TEMP, 0, 'x'), (src(REG_INPUT, 1), src(REG_INPUT, 1))),
        (OP_RSQ, 0, dst(REG_TEMP, 0, 'x'), (src(REG_TEMP, 0, 'x'),)),
        (OP_MUL, 0, dst(REG_TEMP, 1), (src(REG_INPUT, 1), src(REG_TEMP, 0, 'x'))),
        (OP_MAD, 0, dst(REG_TEMP, 2), (src(REG_INPUT, 0), src(REG_CONST, 40, 'x'), src(REG_CONST, 41, modifier=SRCMOD_NEG))),
        (OP_DP4, 0, dst(REG_RASTOUT, 0, 'x'), (src(REG_TEMP, 2), src(REG_CONST, 0))),
        (OP_DP4, 0, dst(REG_RASTOUT, 0, 'y'), (src(REG_TEMP, 2), src(REG_CONST, 1))),
        (OP_DP4, 0, dst(REG_RASTOUT, 0, 'z'), (src(REG_TEMP, 2), src(REG_CONST, 2))),
        (OP_DP4, 0, dst(REG_RASTOUT, 0, 'w'), (src(REG_TEMP, 2), src(REG_CONST, 3))),
        (OP_MOV, 0, dst(REG_TEXCRDOUT, 0, 'xy'), (src(REG_INPUT, 2, 'xyxx'),)),
    ])


OP_DP4 = d3d9.OP_DP4


def test_vertex_disassembly():
    program = disassemble(_vertex_fixture())
    check(program.kind == 'vertex' and program.version == 'vs_2_0', program.version)
    check(len(program.declarations) == 3, 'dcl count')
    check(program.declarations[0].usage == USAGE_POSITION, 'dcl usage')
    check(len(program.defs) == 1 and program.defs[0].register_number == 40, 'def')
    check(abs(program.defs[0].value[1] - 1.5) < 1e-6, 'def value')
    check(len(program.instructions) == 9, f"{len(program.instructions)} instructions")
    check(program.instructions[0].name == 'dp3', 'first op')
    check(program.instructions[3].sources[2].modifier == SRCMOD_NEG, 'neg modifier')
    check(not program.has_flow_control(), 'flow control')
    check(program.instructions[-1].dest.register_type == REG_TEXCRDOUT, 'oT dest')


def test_pixel_disassembly():
    blob = assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(REG_ADDR, 0)),        # dcl t0 (SM2 texcoord)
        ('dcl_sampler', SAMPLER_2D, dst(REG_SAMPLER, 0)),
        ('def', 0, (1.0, 0.0, 0.0, 0.5)),
        (OP_TEXLD, 0, dst(REG_TEMP, 0), (src(REG_ADDR, 0), src(REG_SAMPLER, 0))),
        (OP_MUL, 0, dst(REG_TEMP, 0), (src(REG_TEMP, 0), src(REG_CONST, 0))),
        (OP_MOV, 0, dst(REG_COLOROUT, 0), (src(REG_TEMP, 0),)),
    ])
    program = disassemble(blob)
    check(program.kind == 'pixel' and program.version == 'ps_2_0', program.version)
    samplers = program.sampler_declarations()
    check(len(samplers) == 1 and samplers[0].sampler_type == SAMPLER_2D, 'sampler dcl')
    check(program.instructions[0].opcode == OP_TEXLD, 'texld')
    check(program.instructions[0].texld_variant == '', 'plain texld')
    check(program.instructions[-1].dest.register_type == REG_COLOROUT, 'oC0')


def test_texldp_variant_and_sm3():
    blob = assemble('pixel', 3, 0, [
        ('dcl', USAGE_TEXCOORD, 0, dst(REG_INPUT, 0)),
        ('dcl_sampler', SAMPLER_2D, dst(REG_SAMPLER, 2)),
        (OP_TEXLD, 0x1, dst(REG_TEMP, 1), (src(REG_INPUT, 0), src(REG_SAMPLER, 2))),
        (OP_TEXKILL, 0, dst(REG_TEMP, 1), ()),
        (OP_MOV, 0, dst(REG_COLOROUT, 0), (src(REG_TEMP, 1),)),
    ])
    program = disassemble(blob)
    check(program.version == 'ps_3_0', program.version)
    check(program.instructions[0].texld_variant == 'p', 'texldp')
    check(program.instructions[1].opcode == OP_TEXKILL, 'texkill')


def test_sincos_both_shapes():
    long_form = assemble('vertex', 2, 0, [
        (OP_SINCOS, 0, dst(REG_TEMP, 0, 'xy'),
         (src(REG_TEMP, 1, 'w'), src(REG_CONST, 10), src(REG_CONST, 11))),
        (OP_MOV, 0, dst(REG_RASTOUT, 0), (src(REG_TEMP, 0),)),
    ])
    short_form = assemble('vertex', 3, 0, [
        (OP_SINCOS, 0, dst(REG_TEMP, 0, 'xy'), (src(REG_TEMP, 1, 'w'),)),
        (OP_MOV, 0, dst(REG_RASTOUT, 0), (src(REG_TEMP, 0),)),
    ])
    check(len(disassemble(long_form).instructions[0].sources) == 3, 'sincos long')
    check(len(disassemble(short_form).instructions[0].sources) == 1, 'sincos short')


def test_comment_blocks_are_skipped():
    body = _vertex_fixture()
    tokens = list(struct.unpack(f'<{len(body) // 4}I', body))
    comment = [0xFFFE | (2 << 16), 0x11111111, 0x22222222]
    tokens[1:1] = comment
    program = disassemble(struct.pack(f'<{len(tokens)}I', *tokens))
    check(program.comment_dwords == 3, 'comment dwords')
    check(len(program.instructions) == 9, "instructions after comment")


def test_fail_closed():
    expect_error(b'\x00\x01', 'token stream')
    expect_error(struct.pack('<2I', 0xFFFD0300, 0x0000FFFF), 'stage')
    expect_error(struct.pack('<2I', 0xFFFE0100, 0x0000FFFF), 'shader model 1.0')
    # truncated instruction
    expect_error(struct.pack('<3I', 0xFFFE0200, OP_MOV | (2 << 24), 0x0000FFFF), 'exceeds stream')
    # missing end token
    expect_error(struct.pack('<2I', 0xFFFE0200, d3d9.OP_NOP), 'no end token')
    # unknown opcode fails with its number
    expect_error(assemble('vertex', 2, 0, [(0x3F, 0, dst(REG_TEMP, 0), ())]), '0x3F')
    # trailing tokens after end
    blob = _vertex_fixture() + struct.pack('<I', 0)
    expect_error(blob, 'after end token')


def test_relative_addressing_sm3():
    blob = assemble('vertex', 3, 0, [
        (OP_MOV, 0, dst(REG_TEMP, 0),
         (SourceParam(REG_CONST, 8, (0, 1, 2, 3), 0, True,
                      SourceParam(REG_ADDR, 0, (0, 0, 0, 0), 0)),)),
        (OP_MOV, 0, dst(REG_RASTOUT, 0), (src(REG_TEMP, 0),)),
    ])
    program = disassemble(blob)
    source = program.instructions[0].sources[0]
    check(source.relative and source.relative_source is not None, 'relative source')
    check(source.relative_source.register_type == REG_ADDR, 'address register')


def test_abs_modifier_and_masks():
    blob = assemble('pixel', 3, 0, [
        (OP_ADD, 0, dst(REG_TEMP, 3, 'xz'),
         (src(REG_TEMP, 1, 'yyyy', SRCMOD_ABS), src(REG_TEMP, 2))),
        (OP_MOV, 0, dst(REG_COLOROUT, 0), (src(REG_TEMP, 3),)),
    ])
    program = disassemble(blob)
    add = program.instructions[0]
    check(add.dest.write_mask == 0b0101, 'write mask xz')
    check(add.sources[0].modifier == SRCMOD_ABS, 'abs modifier')
    check(add.sources[0].swizzle == (1, 1, 1, 1), 'yyyy swizzle')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()

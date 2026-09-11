#!/usr/bin/env python3
"""RSX NV40 encoder/decoder round trip and vendored-decoder field agreement."""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.rsx import nv40
from cod4porter.rsx.nv40 import (
    FragInstr, FragSource, VertInstr, VertSource,
    FRAG_OP_MAD, FRAG_OP_MOV, FRAG_OP_TEX, FRAG_OP_KIL, FRAG_OP_RET,
    FRAG_REG_CONST, FRAG_REG_INPUT, FRAG_REG_TEMP,
    VEC_OP_DP4, VEC_OP_MAD, VEC_OP_MOV, SCA_OP_RSQ,
    VERT_REG_CONST, VERT_REG_INPUT, VERT_REG_TEMP,
    decode_fragment_program, decode_vertex_program,
    encode_fragment_program, encode_vertex_program, fragment_word,
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_fragment_word_involution():
    rng = random.Random(1)
    for _ in range(1000):
        value = rng.getrandbits(32)
        check(fragment_word(fragment_word(value)) == value, 'fragment_word not an involution')
    # Exact vendored transform: low halfword <->
    check(fragment_word(0xAABBCCDD) == 0xCCDDAABB, 'fragment_word lane order')


def _random_frag_source(rng, kinds):
    return FragSource(
        kind=rng.choice(kinds),
        index=rng.randrange(0x40),
        swizzle=tuple(rng.randrange(4) for _ in range(4)),
        negate=rng.random() < 0.5,
        absolute=rng.random() < 0.5,
        fp16=rng.random() < 0.3,
    )


def test_fragment_roundtrip_random():
    rng = random.Random(2)
    opcodes = [op for op, n in nv40._FRAG_OPERAND_COUNTS.items() if op < 0x40]
    for _ in range(600):
        opcode = rng.choice(opcodes)
        count = nv40.fragment_operand_count(opcode)
        constant = None
        sources = []
        for slot in range(count):
            kinds = [FRAG_REG_TEMP, FRAG_REG_INPUT]
            if constant is None:
                kinds.append(FRAG_REG_CONST)
            source = _random_frag_source(rng, kinds)
            if source.kind == FRAG_REG_CONST:
                constant = tuple(float(rng.randrange(-8, 8)) for _ in range(4))
            sources.append(source)
        instruction = FragInstr(
            opcode=opcode,
            dest_reg=rng.randrange(0x40),
            dest_fp16=rng.random() < 0.5,
            write_mask=tuple(rng.random() < 0.7 for _ in range(4)),
            no_dest=rng.random() < 0.2,
            saturate=rng.random() < 0.3,
            end=True,
            input_attr=rng.randrange(16),
            tex_unit=rng.randrange(16),
            sources=tuple(sources),
            inline_constant=constant,
            scale=rng.choice((0, 1, 2, 3, 5, 6, 7)),
            cond_test=rng.randrange(8),
            cond_swizzle=tuple(rng.randrange(4) for _ in range(4)),
            cond_write=rng.random() < 0.2,
            cond_write_reg1=rng.random() < 0.2,
            cond_read_reg1=rng.random() < 0.2,
            dest_precision=rng.randrange(3),
            src_precisions=tuple(rng.randrange(3) for _ in range(3)),
            expanded_texture=rng.random() < 0.2,
            perspective_correction=rng.random() < 0.8,
            indexed_input=False,
        )
        image = encode_fragment_program([instruction])
        check(len(image) == instruction.byte_count, 'fragment image length')
        decoded = decode_fragment_program(image)
        check(len(decoded) == 1, 'fragment decode count')
        check(decoded[0] == instruction, f'fragment mismatch:\n{instruction}\n{decoded[0]}')


def test_fragment_stream_shapes():
    program = [
        FragInstr(opcode=FRAG_OP_TEX, dest_reg=2,
                  sources=(FragSource(kind=FRAG_REG_INPUT),), input_attr=4, tex_unit=1),
        FragInstr(opcode=FRAG_OP_MAD, dest_reg=0,
                  sources=(FragSource(index=2), FragSource(kind=FRAG_REG_CONST),
                           FragSource(index=2)),
                  inline_constant=(0.5, 0.25, -1.0, 2.0)),
        FragInstr(opcode=FRAG_OP_MOV, dest_reg=0, end=True,
                  sources=(FragSource(index=0),)),
    ]
    image = encode_fragment_program(program)
    check(len(image) == 16 + 32 + 16, 'stream length with inline constant')
    offsets = nv40.fragment_constant_slot_offsets(program)
    check(offsets == [32], f'constant slot offsets {offsets}')
    decoded = decode_fragment_program(image)
    check(decoded == program, 'fragment stream mismatch')


def test_fragment_vendored_bit_positions():
    """Match the exact C# RsxFragmentInstruction property arithmetic."""
    instruction = FragInstr(
        opcode=FRAG_OP_TEX, dest_reg=5, dest_fp16=True,
        write_mask=(True, False, True, False), end=True,
        input_attr=9, tex_unit=3,
        sources=(FragSource(kind=FRAG_REG_INPUT, swizzle=(3, 2, 1, 0), negate=True),),
        saturate=True)
    dst, w0, w1, w2 = instruction.encode()
    check(dst & 1 == 1, 'End')
    check((dst >> 1) & 0x3F == 5, 'DestRegister')
    check((dst >> 7) & 1 == 1, 'DestFp16')
    check((dst >> 9) & 0xF == 0b0101, 'WriteMask X|Z')
    check((dst >> 13) & 0xF == 9, 'SourceAttribute')
    check((dst >> 17) & 0xF == 3, 'TextureUnit')
    check((dst >> 24) & 0x3F == FRAG_OP_TEX, 'opcode low bits')
    check(dst >> 31 == 1, 'Saturate')
    check(w0 & 3 == FRAG_REG_INPUT, 'source kind')
    check((w0 >> 9) & 3 == 3 and (w0 >> 11) & 3 == 2, 'source swizzle x/y')
    check((w0 >> 17) & 1 == 1, 'source negate')
    check((w1 >> 25) & 0x40 == 0, 'opcode bit6 clear')


def test_vertex_roundtrip_random():
    rng = random.Random(3)
    for _ in range(600):
        vec_opcode = rng.choice((VEC_OP_MOV, VEC_OP_MAD, VEC_OP_DP4, nv40.VEC_OP_ADD,
                                 nv40.VEC_OP_MUL, nv40.VEC_OP_FRC, nv40.VEC_OP_MIN,
                                 nv40.VEC_OP_SGE, nv40.VEC_OP_ARL, nv40.VEC_OP_NOP))
        sca_opcode = rng.choice((nv40.SCA_OP_NOP, nv40.SCA_OP_RCP, SCA_OP_RSQ,
                                 nv40.SCA_OP_EX2, nv40.SCA_OP_LG2, nv40.SCA_OP_SIN))
        uses_const = rng.random() < 0.4
        uses_input = rng.random() < 0.5
        def random_source():
            kinds = [VERT_REG_TEMP]
            if uses_const:
                kinds.append(VERT_REG_CONST)
            if uses_input:
                kinds.append(VERT_REG_INPUT)
            return VertSource(
                kind=rng.choice(kinds),
                index=rng.randrange(0x20),
                swizzle=tuple(rng.randrange(4) for _ in range(4)),
                negate=rng.random() < 0.5,
            )
        to_result = rng.random() < 0.5
        instruction = VertInstr(
            vec_opcode=vec_opcode,
            sca_opcode=sca_opcode,
            sources=(random_source(), random_source(), random_source()),
            source_abs=tuple(rng.random() < 0.3 for _ in range(3)),
            input_attr=rng.randrange(16) if uses_input else 0,
            const_src=rng.randrange(468) if uses_const else 0,
            vec_dest_temp=nv40.VEC_DEST_NONE if to_result else rng.randrange(32),
            vec_write_mask=tuple(rng.random() < 0.7 for _ in range(4)),
            vec_result=to_result,
            sca_dest_temp=rng.randrange(32) if sca_opcode and rng.random() < 0.5 else nv40.SCA_DEST_NONE,
            sca_write_mask=tuple(rng.random() < 0.4 for _ in range(4)) if sca_opcode else (False,) * 4,
            sca_result=False,
            result=rng.randrange(16) if to_result else nv40.VERT_RESULT_NONE,
            saturate=rng.random() < 0.3,
            cond_update=rng.random() < 0.2,
            cond_test=rng.random() < 0.2,
            cond_condition=rng.randrange(8),
            cond_swizzle=tuple(rng.randrange(4) for _ in range(4)),
            cond_register=rng.randrange(2),
            end=True,
        )
        image = encode_vertex_program([instruction])
        check(len(image) == 16, 'vertex image length')
        decoded = decode_vertex_program(image)
        check(len(decoded) == 1, 'vertex decode count')
        check(decoded[0] == instruction, f'vertex mismatch:\n{instruction}\n{decoded[0]}')


def test_vertex_vendored_bit_positions():
    """Match the exact C# RsxVertexInstruction property arithmetic."""
    instruction = VertInstr(
        vec_opcode=VEC_OP_DP4, sca_opcode=SCA_OP_RSQ,
        sources=(VertSource(kind=VERT_REG_INPUT, index=0, swizzle=(0, 1, 2, 3)),
                 VertSource(kind=VERT_REG_CONST, index=0),
                 VertSource(kind=VERT_REG_TEMP, index=7, negate=True)),
        input_attr=8, const_src=467,
        vec_dest_temp=nv40.VEC_DEST_NONE, vec_write_mask=(True, True, True, True),
        vec_result=True, result=nv40.VERT_RESULT_TEX0,
        sca_dest_temp=3, sca_write_mask=(False, False, False, True),
        end=True)
    w0, w1, w2, w3 = instruction.encode()
    check((w1 >> 22) & 0x1F == VEC_OP_DP4, 'VecOpcode')
    check((w1 >> 27) & 0x1F == SCA_OP_RSQ, 'ScaOpcode')
    check((w1 >> 12) & 0x3FF == 467, 'ConstSource')
    check((w1 >> 8) & 0xF == 8, 'InputAttribute')
    check((w0 >> 30) & 1 == 1, 'VecResult')
    check((w0 >> 15) & 0x3F == 0x3F, 'VecDestTemp none')
    check((w3 >> 13) & 0xF == 0xF, 'VectorWriteMask xyzw -> 0b1111')
    check((w3 >> 17) & 0xF == 0b0001, 'ScalarWriteMask w-only -> W bit')
    check((w3 >> 2) & 0x1F == nv40.VERT_RESULT_TEX0, 'Result')
    check((w3 >> 7) & 0x1F == 3, 'ScaDestTemp')
    check(w3 & 1 == 1, 'End')
    source0 = ((w1 & 0xFF) << 9) | ((w2 >> 23) & 0x1FF)
    check(source0 & 3 == VERT_REG_INPUT, 'Source0 kind')
    source2 = ((w2 & 0x3F) << 11) | ((w3 >> 21) & 0x7FF)
    check(source2 & 3 == VERT_REG_TEMP, 'Source2 kind')
    check((source2 >> 2) & 0x3F == 7, 'Source2 temp index')
    check((source2 >> 16) & 1 == 1, 'Source2 negate')


def test_multi_instruction_end_flags():
    rows = [
        VertInstr(vec_opcode=VEC_OP_MOV,
                  sources=(VertSource(kind=VERT_REG_INPUT), VertSource(), VertSource()),
                  input_attr=0, vec_dest_temp=0, vec_write_mask=(True,) * 4),
        VertInstr(vec_opcode=VEC_OP_MOV,
                  sources=(VertSource(kind=VERT_REG_TEMP), VertSource(), VertSource()),
                  vec_result=True, result=0, vec_write_mask=(True,) * 4,
                  end=True),
    ]
    image = encode_vertex_program(rows)
    check(len(image) == 32, 'two-instruction image')
    decoded = decode_vertex_program(image)
    check(decoded == rows, 'vertex multi round trip')
    try:
        encode_vertex_program(list(reversed(rows)))
    except nv40.RsxEncodeError:
        pass
    else:
        raise AssertionError('end-flag misplacement must fail closed')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()

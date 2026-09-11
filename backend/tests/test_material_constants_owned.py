#!/usr/bin/env python3
"""Material-constant contracts for owned (compiled/donor) PS3 TechniqueSets.

Regression for the 22.2.0 mp_gulag failure: ``bind_material_constants``
validated every binding against the retail name-keyed contract catalog, so a
freshly compiled techset such as ``wc_tools`` raised ``unknown target
contract``.  Owned techsets carry their complete argument tables, so their
contract is measured from the artifact itself.
"""
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.material_constants import bind_material_constants, owned_contract
from cod4porter.technique_binding import TechniqueBinding, TechniqueEvidence
from cod4porter.rsx import d3d9
from cod4porter.rsx.d3d9 import assemble, dst, src
from cod4porter.rsx.techset_compile import compile_techniqueset
from cod4porter.rsx.techset_parse import (
    ARG_CODE_VERTEX_CONST, ARG_MATERIAL_PIXEL_CONST, ARG_MATERIAL_PIXEL_SAMPLER,
    PcArgRecord, PcDeclRecord, PcPassRecord, PcShaderRecord, PcTechniqueRecord,
    PcTechniqueSetFull,
)

V, R, C, T, S = (d3d9.REG_INPUT, d3d9.REG_TEMP, d3d9.REG_CONST,
                 d3d9.REG_ADDR, d3d9.REG_SAMPLER)

READ_HASH = 0x11223344      # material pixel const c0: read by the program
UNREAD_HASH = 0x55667788    # material pixel const c9: dropped by the compiler


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def _vertex_bytecode():
    return assemble('vertex', 2, 0, [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        (d3d9.OP_M4X4, 0, dst(R, 0), (src(V, 0), src(C, 0))),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_RASTOUT, 0), (src(R, 0),)),
    ])


def _pixel_bytecode(*, read_consts):
    ops = [('dcl', 0, 0, dst(T, 0)),
           ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 0)),
           (d3d9.OP_TEXLD, 0, dst(R, 0), (src(T, 0), src(S, 0)))]
    if read_consts:
        ops.append((d3d9.OP_MUL, 0, dst(R, 0), (src(R, 0), src(C, 0))))
    ops.append((d3d9.OP_MOV, 0, dst(d3d9.REG_COLOROUT, 0), (src(R, 0),)))
    return assemble('pixel', 2, 0, ops)


def _compiled(name, *, material_consts):
    vertex = PcShaderRecord('vertex', name + '_vs', _vertex_bytecode(), 0, 0x2000)
    pixel = PcShaderRecord('pixel', name + '_ps',
                           _pixel_bytecode(read_consts=material_consts), 0, 0x3000)
    args = [PcArgRecord(ARG_CODE_VERTEX_CONST, 0, (4 << 24) | 0, None),
            PcArgRecord(ARG_MATERIAL_PIXEL_SAMPLER, 0, 0xDEADBEEF, None)]
    if material_consts:
        args.append(PcArgRecord(ARG_MATERIAL_PIXEL_CONST, 0, READ_HASH, None))
        args.append(PcArgRecord(ARG_MATERIAL_PIXEL_CONST, 9, UNREAD_HASH, None))
    decl = PcDeclRecord(1, False, ((0, 0),), 0x1000)
    technique = PcTechniqueRecord('lit', 2, (
        PcPassRecord(decl, vertex, pixel, 0, 0, len(args), 0, tuple(args)),), 0x4000)
    slots = [None] * 34
    slots[4] = technique
    slots[7] = technique  # shared object: contract must not double-count
    return compile_techniqueset(PcTechniqueSetFull(name, 2, tuple(slots), 55))


def _binding(compiled, evidence=TechniqueEvidence.COMPILED_RSX):
    return TechniqueBinding(compiled.name, compiled.name, False, evidence, True,
                            'compiled from the map\'s own PC shaders.',
                            source_asset_index=7, compiled=compiled)


SAMPLER_HASH = 0xDEADBEEF   # the fixture techset's material pixel sampler


@dataclass(frozen=True)
class _Texture:
    name_hash: int


@dataclass(frozen=True)
class _Material:
    name: str
    constants: tuple
    textures: tuple


def _material(name, *hashes, textures=(SAMPLER_HASH,)):
    return _Material(name,
                     tuple(struct.pack('<I', h) + b'\x00' * 28 for h in hashes),
                     tuple(_Texture(h) for h in textures))


def test_owned_contract_measures_only_surviving_args():
    compiled = _compiled('wc_l_custom_t0c0', material_consts=True)
    constants, samplers = owned_contract(compiled)
    check(constants == frozenset({READ_HASH}),
          f'contract {sorted(hex(h) for h in constants)}: the unread c9 constant was '
          'dropped by the compiler and must not be required')
    check(samplers == frozenset({SAMPLER_HASH}),
          f'sampler contract {sorted(hex(h) for h in samplers)}')


def test_owned_binding_with_satisfied_contract_is_kept():
    compiled = _compiled('wc_l_custom_t0c0', material_consts=True)
    binding = _binding(compiled)
    bound = bind_material_constants(_material('wc/custom_wall', READ_HASH, 0x0BADF00D), binding)
    check(bound is binding, 'satisfied owned binding must return unchanged')


def test_owned_binding_without_material_consts_binds_bare_material():
    # The mp_gulag regression: wc/caulk_shadow (zero constants) bound to the
    # compiled wc_tools techset raised 'unknown target contract'.
    compiled = _compiled('wc_tools', material_consts=False)
    binding = _binding(compiled)
    bound = bind_material_constants(_material('wc/caulk_shadow'), binding)
    check(bound is binding, 'constant-free owned techset must bind a bare material')


def test_owned_binding_missing_constant_fails_closed_with_exact_hash():
    compiled = _compiled('wc_l_custom_t0c0', material_consts=True)
    binding = _binding(compiled)
    try:
        bind_material_constants(_material('wc/custom_wall', 0x0BADF00D), binding)
    except ValueError as error:
        text = str(error)
        check('0x11223344' in text, f'missing hash must be named: {text}')
        check('owned' in text, f'owned techset failure must say so: {text}')
        check('unknown target contract' not in text,
              f'owned techsets have a measured contract: {text}')
    else:
        raise AssertionError('missing owned constant must fail closed')


def test_donor_evidence_uses_the_owned_contract_too():
    compiled = _compiled('mc_l_sm_r0c0s0', material_consts=True)
    binding = _binding(compiled, TechniqueEvidence.RETAIL_DONOR)
    check(binding.owns_ps3_techset, 'donor binding with payload owns its techset')
    bound = bind_material_constants(_material('mc/metal_wall', READ_HASH), binding)
    check(bound is binding, 'satisfied donor binding must return unchanged')
    try:
        bind_material_constants(_material('mc/metal_bare'), binding)
    except ValueError as error:
        check('0x11223344' in str(error), f'donor contract must name the hash: {error}')
    else:
        raise AssertionError('donor transplant missing a constant must fail closed')


def test_owned_binding_missing_sampler_fails_closed_with_exact_hash():
    # The engine's texture-table scan has no end check either: a donor graph
    # sampling a texture the material does not carry must be refused.
    compiled = _compiled('wc_l_sm_t0c0n0s0', material_consts=True)
    binding = _binding(compiled, TechniqueEvidence.RETAIL_DONOR)
    try:
        bind_material_constants(
            _material('wc/donor_wall', READ_HASH, textures=(0x0BADF00D,)), binding)
    except ValueError as error:
        text = str(error)
        check('missing texture samplers' in text and '0xdeadbeef' in text,
              f'missing sampler hash must be named: {text}')
    else:
        raise AssertionError('missing owned sampler must fail closed')


def test_non_owned_bindings_keep_the_retail_catalog_gate():
    binding = TechniqueBinding('wc_never_measured_xyz', 'wc_never_measured_xyz', False,
                               TechniqueEvidence.PS3_AVAILABLE_EXACT, True, 'test.')
    try:
        bind_material_constants(_material('wc/anything'), binding)
    except ValueError as error:
        check('unknown target contract' in str(error),
              f'non-owned bindings stay catalog-gated: {error}')
    else:
        raise AssertionError('catalog-less native binding must still fail closed')


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()

"""Require the selected PS3 shader graph's named material constants."""
from dataclasses import replace
import json
from pathlib import Path
import struct

from .technique_binding import ps3_candidates, _resolve, TechniqueEvidence

_PATH = Path(__file__).parent / 'reference' / 'ps3_material_constant_contracts.json'
CONTRACTS = {name.casefold(): frozenset(int(h, 16) for h in hashes)
             for name, hashes in json.loads(_PATH.read_text())['techniques'].items()}

MATERIAL_CONST_ARG_TYPES = (0, 6)  # material vertex / material pixel constant nameHash
MATERIAL_SAMPLER_ARG_TYPE = 2      # material pixel sampler nameHash


def owned_contract(compiled) -> tuple[frozenset, frozenset]:
    """(constant, sampler) nameHashes an owned (compiled or donor) graph reads.

    A CompiledTechniqueSet carries its complete argument tables, so its
    contract is measured from the artifact itself instead of the retail
    catalog: every type-0/6 MaterialShaderArgument names one constant and
    every type-2 argument one texture the engine scans for (both lookups run
    without an end check, so a missing hash is a loader hang, not a default).
    """
    constants = set()
    samplers = set()
    seen = set()
    for technique in compiled.slots:
        if technique is None or id(technique) in seen:
            continue
        seen.add(id(technique))
        for shader_pass in technique.passes:
            for argument in shader_pass.args:
                if argument.type in MATERIAL_CONST_ARG_TYPES:
                    constants.add(argument.raw_be & 0xFFFFFFFF)
                elif argument.type == MATERIAL_SAMPLER_ARG_TYPE:
                    samplers.add(argument.raw_be & 0xFFFFFFFF)
    return frozenset(constants), frozenset(samplers)


def bind_material_constants(material, binding):
    """Keep source constants unchanged; choose only a graph they can satisfy.

    Material_Register calls 0x344EE8, which scans for a type-6 argument's
    hash without an end check. A missing envMapParms after adding an s0
    channel therefore causes an out-of-bounds search during zone loading.
    Unknown target contracts cannot prove a safe binding and are rejected.

    Bindings that own their PS3 TechniqueSet (compiled from the map's own
    PC shaders, or transplanted from a retail donor) are not in the retail
    name-keyed catalog; their contract is read from the owned artifact's
    argument tables, so the same fail-closed check applies to them.
    """
    present = frozenset(struct.unpack_from('<I', c)[0] for c in material.constants)
    if getattr(binding, 'owns_ps3_techset', False):
        required_consts, required_samplers = owned_contract(binding.compiled)
        textures = frozenset(int(t.name_hash) & 0xFFFFFFFF for t in material.textures)
        problems = []
        missing_owned = required_consts - present
        if missing_owned:
            problems.append('missing constants '
                            + ', '.join(f'0x{h:08x}' for h in sorted(missing_owned)))
        missing_samplers = required_samplers - textures
        if missing_samplers:
            problems.append('missing texture samplers '
                            + ', '.join(f'0x{h:08x}' for h in sorted(missing_samplers)))
        if problems:
            raise ValueError(
                f"Material '{material.name}' cannot safely bind its owned PS3 TechniqueSet "
                f"'{binding.candidate_name}': " + '; '.join(problems))
        return binding
    # The first availability match can introduce a constant absent from the
    # source (for example zfeather when replacing effect_add_eyeoffset). Keep
    # the source family's sibling candidates: restarting only from the chosen
    # replacement loses valid alternatives such as effect_add_nofog.
    source = binding.source_name.lstrip(',')
    if source.lower().startswith('sm2/'):
        source = source[4:]
    candidates = dict.fromkeys((binding.candidate_name,
                               *ps3_candidates(source),
                               *ps3_candidates(binding.candidate_name)))
    for name in candidates:
        available = _resolve(name)
        if available is None:
            continue
        target = available[0]
        required = CONTRACTS.get(target.casefold())
        if required is None or not required.issubset(present):
            continue
        if target == binding.candidate_name:
            return binding
        return replace(binding, candidate_name=target,
                       evidence=TechniqueEvidence.PS3_FAMILY_SUBSTITUTION,
                       substitution=f'{binding.source_name} -> {target}',
                       reason=binding.reason + ' Selected a graph whose material constants '
                       'are present; no synthetic shader constants were added.')
    required = CONTRACTS.get(binding.candidate_name.casefold())
    missing = 'unknown target contract' if required is None else ', '.join(
        f'0x{h:08x}' for h in sorted(required - present))
    raise ValueError(f"Material '{material.name}' cannot safely bind PS3 TechniqueSet "
                     f"'{binding.candidate_name}': missing constants {missing}")

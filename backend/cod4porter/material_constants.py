"""Require the selected PS3 shader graph's named material constants."""
from dataclasses import replace
import json
from pathlib import Path
import struct

from .technique_binding import ps3_candidates, _resolve, TechniqueEvidence

_PATH = Path(__file__).parent / 'reference' / 'ps3_material_constant_contracts.json'
CONTRACTS = {name.casefold(): frozenset(int(h, 16) for h in hashes)
             for name, hashes in json.loads(_PATH.read_text())['techniques'].items()}


def bind_material_constants(material, binding):
    """Keep source constants unchanged; choose only a graph they can satisfy.

    Material_Register calls 0x344EE8, which scans for a type-6 argument's
    hash without an end check. A missing envMapParms after adding an s0
    channel therefore causes an out-of-bounds search during zone loading.
    Unknown target contracts cannot prove a safe binding and are rejected.
    """
    present = frozenset(struct.unpack_from('<I', c)[0] for c in material.constants)
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

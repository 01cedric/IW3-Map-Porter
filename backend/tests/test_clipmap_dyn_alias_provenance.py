from __future__ import annotations

from types import SimpleNamespace

from cod4porter.backend.v4_ffio import encode_pc_packed
from cod4porter.pc_clipmap import DynEntOwnedPhysPreset,resolve_dynent_owned_phys_aliases
from cod4porter.physics import PhysPreset


def preset(name):return PhysPreset(name,1,1.0,0.1,0.2,1.0,1.0,0.0,0.0,False,None)


target=0x198AE20
owner=DynEntOwnedPhysPreset(
    'model',0,0,'following',preset(',default'),0x4DA3952,0x4DA3987,
    target,(
        'BrushSide owner base',
        'BrushEdge owner base',
        'RootLeafBrush owner base',
        'Border owner base',
        'DynEnt model LoadStream base',
    ),
)
clip=SimpleNamespace(dyn_owned_phys_presets=(owner,))
raw=encode_pc_packed(4,target)
assert resolve_dynent_owned_phys_aliases(clip,[raw,raw])=={raw:owner}

# One target/one relative owner equation remains ambiguous without the independent typed replay.
unproven=DynEntOwnedPhysPreset('model',0,0,'following',preset(',default'),0,0)
try:resolve_dynent_owned_phys_aliases(SimpleNamespace(dyn_owned_phys_presets=(unproven,)),[raw])
except ValueError as exc:assert 'basis is not unique' in str(exc)
else:raise AssertionError('single-target relative basis was accepted without provenance')

# Provenance is exact.  A nearby packed cell must not be assigned by name or nearest address.
try:resolve_dynent_owned_phys_aliases(clip,[encode_pc_packed(4,target+4)])
except ValueError as exc:assert 'basis is not unique' in str(exc)
else:raise AssertionError('non-owner packed target was accepted')

duplicate=DynEntOwnedPhysPreset(
    'model',1,1,'following',preset(',other'),0,0,target,('independent typed replay',),
)
try:resolve_dynent_owned_phys_aliases(SimpleNamespace(dyn_owned_phys_presets=(owner,duplicate)),[raw])
except ValueError as exc:assert 'provenance is ambiguous' in str(exc)
else:raise AssertionError('duplicate exact pointer owners were accepted')

print({'passed':True,'target':hex(target),'owner':f'{owner.list_kind}[{owner.index}]'})

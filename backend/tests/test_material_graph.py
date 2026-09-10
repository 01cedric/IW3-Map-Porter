from cod4porter.material import *
from cod4porter.material_planning import MaterialUniverse,PlannedMaterial
from cod4porter.technique_binding import TechniqueBinding,TechniqueEvidence
from cod4porter.material_graph import build_material_graph

def rec(name):return MaterialRecord(1,name,0,0,0,0,0,0,0,bytes([255]*34),0,0,'None',0,0,0,0,0,(),(),(),{})
s=MaterialStateConversion(bytes([255]*26),(),0,0,0);b=TechniqueBinding('2d','2d',False,TechniqueEvidence.CANONICAL_STOCK,True,'x',5);p=PlannedMaterial(rec('foo'),s,bytes(0x38),b,(),10)
u=MaterialUniverse((),(),(),(),(p.source,),frozenset(),{'passed':True,'bindings':(),'unresolved':[]},(p,),(),{'packed':0,'unresolved':[]})
g=build_material_graph(u,{})
assert len(g.technique_nodes)==1 and len(g.material_nodes)==1 and g.material_symbol_by_name['foo']

# Runtime-compatible FX and other visual fallbacks require a stable shared default even after
# exact XModel material closure reaches zero.
g_runtime=build_material_graph(u,{},include_runtime_default_material=True)
assert 'default' in g_runtime.material_symbol_by_name
# ...and it is the engine's own default Material, '$default' (EBOOT default-name table
# 0x68094C).  A Material called plain 'default' does not exist in any PS3 zone: the emulated
# database resolved ',default' to a clone of '$default' on every map.
assert any(getattr(n,'name',None)=='$default' for n in g_runtime.material_nodes)
assert not any(getattr(n,'name',None)=='default' for n in g_runtime.material_nodes)

# Conflicting same semantic name must never silently merge.
p2=PlannedMaterial(MaterialRecord(**{**p.source.__dict__,'root_offset':2,'sort_key':1}),s,bytes(0x38),b,(),None)
u2=MaterialUniverse((),(),(),(),(p.source,p2.source),frozenset(),{'passed':True,'bindings':(),'unresolved':[]},(p,p2),(),{'packed':0,'unresolved':[]})
try:build_material_graph(u2,{});raise AssertionError('must fail')
except ValueError:pass
print('test_material_graph PASS')

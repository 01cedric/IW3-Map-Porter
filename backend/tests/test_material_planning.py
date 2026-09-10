import struct
from cod4porter.backend.v4_ffio import XAsset,XAssetList,encode_pc_packed,FOLLOWING
from cod4porter.material import MaterialRecord,MaterialTexture
from cod4porter.material_planning import *

def alist(types,ptrs=None):
    ptrs=ptrs or [FOLLOWING]*len(types);pool=0x100
    assets=tuple(XAsset(i,pool+i*8,int(t),str(t),int(ptrs[i])) for i,t in enumerate(types))
    return XAssetList('pc',0,0,(),assets,pool,0)
def alias(a,idx):
    base=a.asset_pool_offset-(0x2c+0x10);return encode_pc_packed(4,base+4+idx*8)
def mat(name,root,techptr,textures=(),entry=None,states=(),sc=0,sp=0):
    return MaterialRecord(root,name,0,0,0,0,0,0,0,bytes([255]*34) if entry is None else entry,sc,sp,'None' if sc==0 else 'Inline',len(textures),0,0,0,techptr,tuple(textures),(),tuple(states),{})

# Packed state table only resolves from one unique prior exact signature/content.
a=alist([5,4,6]);tech=alias(a,0)
e=bytearray([255]*34);e[0]=0
m1=mat('a',100,tech,entry=bytes(e),states=((1,2),),sc=1,sp=FOLLOWING)
m2=mat('b',200,tech,entry=bytes(e),states=(),sc=1,sp=encode_pc_packed(4,0x777))
r=resolve_state_tables((m1,m2));assert r[1].state_bits==((1,2),) and r[1].state_bits_resolution_kind=='PackedResolvedBySignature'

# Image stage 2a: source index is learned from signature A, then a different signature B to same XAsset resolves by header.
tA=MaterialTexture(1,2,3,4,5,alias(a,2),'images/foo',123)
tA2=MaterialTexture(1,2,3,4,5,alias(a,2),None,None)
tB=MaterialTexture(9,8,7,6,5,alias(a,2),None,None)
mi=mat('inline',10,tech,(tA,));md=mat('donor',20,tech,(tA2,));mt=mat('target',30,tech,(tB,))
proof=resolve_image_identities((mi,md,mt),a);assert proof['passed'];b={(x.material_root,x.texture_index):x for x in proof['bindings']};assert b[(30,0)].image_name=='images/foo' and b[(30,0)].mode=='direct_xasset_header'

# Image InsertPointer root alias from the Image XAsset's own packed header.
a2=alist([5,4,6],[FOLLOWING,FOLLOWING,encode_pc_packed(4,0x4567)]);tech2=alias(a2,0)
tA=MaterialTexture(1,2,3,4,5,encode_pc_packed(4,0x4567),'img/bar',1)
tA2=MaterialTexture(1,2,3,4,5,encode_pc_packed(4,0x4567),None,None)
tB=MaterialTexture(7,7,7,7,7,encode_pc_packed(4,0x4567),None,None)
p=resolve_image_identities((mat('i',1,tech2,(tA,)),mat('d',2,tech2,(tA2,)),mat('t',3,tech2,(tB,))),a2);assert p['passed'];bb={(x.material_root,x.texture_index):x for x in p['bindings']};assert bb[(3,0)].mode=='insert_pointer_alias' and bb[(3,0)].image_name=='img/bar'

# CP20: a causally reconstructed block-4 MaterialTextureDef::u.image cell may disambiguate
# an otherwise non-unique full TextureDef signature, but only when the selected identity is one
# of that signature's independently observed inline names.
packed=encode_pc_packed(4,0x999)
ta=MaterialTexture(11,12,13,14,5,FOLLOWING,'img/replay_a',1)
tb=MaterialTexture(11,12,13,14,5,FOLLOWING,'img/replay_b',2)
tt=MaterialTexture(11,12,13,14,5,packed,None,None)
proof=resolve_image_identities(
    (mat('ia',10,tech,(ta,)),mat('ib',20,tech,(tb,)),mat('target',30,tech,(tt,))),a,
    block4_cell_aliases={0x999:'img/replay_b'},
)
assert proof['passed'] and proof['neutral_fallback_count']==0
rb={(x.material_root,x.texture_index):x for x in proof['bindings']}[(30,0)]
assert rb.mode=='xmodel_block4_cell_replay' and rb.image_name=='img/replay_b'
assert proof['block4_cell_replay_binding_count']==1
assert proof['block4_cell_replay_ambiguous_signature_slots']==1

# An exact Image XAssetHeader alias must outrank an ambiguous TextureDef signature.
# The target signature is shared by two inline images, but its pointer names the source Image
# asset whose identity was independently learned as images/foo.
amb1=MaterialTexture(0x5151,20,21,7,2,FOLLOWING,'img/amb_a',1)
amb2=MaterialTexture(0x5151,20,21,7,2,FOLLOWING,'img/amb_b',2)
amb_target=MaterialTexture(0x5151,20,21,7,2,alias(a,2),None,None)
proof_ptr=resolve_image_identities(
    (mi,mat('amb_a',41,tech,(amb1,)),mat('amb_b',42,tech,(amb2,)),mat('amb_target',43,tech,(amb_target,))),a,
    allow_runtime_neutral=False,
)
assert proof_ptr['passed']
ptr_binding={(x.material_root,x.texture_index):x for x in proof_ptr['bindings']}[(43,0)]
assert ptr_binding.mode=='direct_xasset_header' and ptr_binding.image_name=='images/foo'

# Fail-closed unknown-pointer diagnostics must not reference an undefined temporary.
unknown=MaterialTexture(0xDEAD,9,10,11,12,encode_pc_packed(3,0x999),None,None)
proof_unknown=resolve_image_identities((mat('unknown',44,tech,(unknown,)),),a,allow_runtime_neutral=False)
assert not proof_unknown['passed'] and len(proof_unknown['unresolved'])==1
u=proof_unknown['unresolved'][0]
assert u['pointer_kind']=='packed' and u['block']==3 and u['offset']==0x999

try:
    resolve_image_identities(
        (mat('ia',10,tech,(ta,)),mat('ib',20,tech,(tb,)),mat('target',30,tech,(tt,))),a,
        block4_cell_aliases={0x999:'img/not_observed'},
    )
    raise AssertionError('unsupported replay identity was accepted')
except ValueError as exc:
    assert 'is not supported' in str(exc)
# A first-root hint constrains the beginning, not every root in a material run.
decl=alist([5,4,4]);zone=bytearray(0x300);cursor=0x100;first=cursor
for name in (b'first\0',b'second\0'):
    struct.pack_into('<I',zone,cursor,FOLLOWING)
    zone[cursor+0x18:cursor+0x3a]=bytes([255]*34)
    struct.pack_into('<I',zone,cursor+0x40,alias(decl,0))
    zone[cursor+0x50:cursor+0x50+len(name)]=name
    cursor+=0x50+len(name)
run=top_level_materials(bytes(zone),decl,first_root_hint=first)
assert [m.material.name for m in run]==['first','second']
print('test_material_planning PASS')

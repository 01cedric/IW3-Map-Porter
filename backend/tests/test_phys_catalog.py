import struct
from types import SimpleNamespace
from cod4porter.backend.v4_ffio import XAsset,XAssetList,FOLLOWING,encode_pc_packed
from cod4porter.physics import *

def al(ptr=FOLLOWING):
    pool=0x80;return XAssetList('pc',0,0,(),(XAsset(0,pool,1,'physpreset',ptr),),pool,0x20)
def put(z,r,name='phys/test'):
    struct.pack_into('<I',z,r,FOLLOWING);struct.pack_into('<i',z,r+4,1)
    for o,v in zip((8,12,16,20,24,32,36),(1.,.2,.3,1.,1.,.5,.6)):struct.pack_into('<f',z,r+o,v)
    z[r+0x28]=1;z[r+0x2c:r+0x2c+len(name)+1]=name.encode()+b'\0'
z=bytearray(0x200);put(z,0x40);p=top_level_phys_presets(bytes(z),al(),(),());assert len(p)==1 and p[0].name=='phys/test' and p[0].source_asset_index==0
# Direct XAssetHeader alias binds XModel packed preset.
a=al();base=a.asset_pool_offset-(0x2c+0x10);raw=encode_pc_packed(4,base+4);m=SimpleNamespace(name='m',phys_preset_pointer=raw,inline_phys_preset=None);x=SimpleNamespace(model=m);r=resolve_xmodel_phys_presets(a,p,[x]);assert m.resolved_phys_preset_asset_index==0 and r['resolved']==1

# Packed sndAliasPrefix resolves only from exact raw-XString evidence.
pr=PhysPreset('phys/snd',1,1.,.2,.3,1.,1.,.5,.6,True,None)
raws=encode_pc_packed(4,0x1234);pr.serialized_sound_alias_prefix_pointer=raws;pr.sound_alias_prefix_identity_proven=False
rr=resolve_phys_preset_sound_prefixes((pr,),(),{raws:'weap_impact'})
assert pr.sound_alias_prefix=='weap_impact' and rr['resolved_from_global_xstring']==1
try:
    pr2=PhysPreset('phys/bad',1,1.,.2,.3,1.,1.,.5,.6,True,None);pr2.serialized_sound_alias_prefix_pointer=encode_pc_packed(4,0x5678);pr2.sound_alias_prefix_identity_proven=False
    resolve_phys_preset_sound_prefixes((pr2,),(),{})
    raise AssertionError('missing exact packed XString evidence should block')
except ValueError:
    pass
rr=resolve_phys_preset_sound_prefixes((pr2,),(),{},runtime_compatible=True)
assert pr2.sound_alias_prefix=='' and rr['runtime_empty_fallbacks']==1
assert rr['runtime_null_fallbacks']==0 and len(rr['exact_unresolved'])==1
assert pr2.sound_alias_prefix_identity_proven is False
print('test_phys_catalog PASS')

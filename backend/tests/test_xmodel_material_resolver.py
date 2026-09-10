from cod4porter.pc_xmodel import *
from cod4porter.xmodel import *
from cod4porter.backend.v4_ffio import XAssetList,XAsset,ScriptString,encode_pc_packed

# fabricate XAsset pool physical offset. aliasBase = 0x1000 -> asset #2 header = 0x1014
assets=tuple(XAsset(i,0,4 if i==2 else 3,'material' if i==2 else 'xmodel',0xffffffff) for i in range(4))
al=XAssetList('pc',0xffffffff,0xffffffff,(),assets,0x1000+0x2c+0x10,0)
raw=encode_pc_packed(4,0x1000+4+2*8)
s=XSurface(0,False,1,1,0,[0,0,0,0],0,0,[0,0,0,0],f'packed:0x{raw:08X}',0,0)
m=XModel('m',1,1,0,1,-1,0,1.0,[0,0,0],[1,1,1],0,0,False,[XModelLod(0,1,0,[0,0,0,0])]+[XModelLod(1,0,0,[0,0,0,0]) for _ in range(3)],[s],{},0)
x=PcXModelIntermediate(m,0,0,[raw],{}, {},[None],[None],[None],[None],0)
res=resolve_xmodel_materials([x],al,{2:'mc/mtl_exact'})
assert not res['unresolved'] and s.material_name=='mc/mtl_exact' and res['direct_xasset_aliases']==1
# The serialized PC stream can end the script-string section at an unaligned
# physical offset.  The loader aligns only its logical VIRTUAL cursor before the
# XAsset array, so packed XAssetHeader aliases use the aligned logical base.
unaligned=XAssetList('pc',0xffffffff,0xffffffff,(),assets,0x1001+0x2c+0x10,0)
aligned_raw=encode_pc_packed(4,0x1004+4+2*8)
assert xasset_header_alias_index(unaligned,aligned_raw,4)==2


def fixture_model(name,specs,index):
    pointers=[];surfaces=[]
    for spec in specs:
        if isinstance(spec,str):
            pointer=0xffffffff;material_name=spec
        else:
            pointer=int(spec);material_name=f'packed:0x{pointer:08X}'
        pointers.append(pointer)
        surfaces.append(XSurface(0,False,1,1,0,[0,0,0,0],0,0,[0,0,0,0],material_name,0,0))
    count=len(surfaces)
    lods=[XModelLod(0,count,0,[0,0,0,0])]+[XModelLod(1,0,0,[0,0,0,0]) for _ in range(3)]
    model=XModel(name,1,1,0,1,-1,0,1.0,[0,0,0],[1,1,1],0,0,False,lods,surfaces,{},0)
    return PcXModelIntermediate(
        model,index*0x1000,index*0x1000+0x100,pointers,{}, {},
        [None]*count,[None]*count,[None]*count,[None]*count,index*0x1000+0x100+count*4,
    )


# A packed MaterialHandle cell can be owned by an all-inline XModel, which has no same-model
# packed pointer from which to infer its array base.  Reproduce the mp_nuked shape generically:
# two causal anchors bound the owner array, while seven uses in two different consumer models
# agree on the shared "red" variant.  Repeated slots in only one model must not count as this
# cross-consumer evidence.
lower=encode_pc_packed(4,0x1000);target=encode_pc_packed(4,0x2004);upper=encode_pc_packed(4,0x3000)
lower_owner=fixture_model('lower_owner',['mc/lower_anchor',lower],0)
unanchored_owner=fixture_model('unanchored_owner',[
    'mc/rus_metal_raised_vert','mc/us_art_color_red','mc/jun_art_metal_black',
    'mc/jun_art_metal_white','mc/mtl_fxanim_gp_chain_decal',
],1)
upper_owner=fixture_model('upper_owner',['mc/upper_anchor',upper],2)
stove_slots=[lower]*13
for slot in (1,5,9,12):stove_slots[slot]=target
fridge_slots=[lower]*6
for slot in (0,2,4):fridge_slots[slot]=target
stove=fixture_model('p_us_stove_red',stove_slots,3)
fridge=fixture_model('mp_nuked_refrigerator_red',fridge_slots,4)
consensus=resolve_xmodel_materials(
    [lower_owner,unanchored_owner,upper_owner,stove,fridge],al,{},
    allow_semantic_sibling=False,runtime_fallback_material=None,
)
target_offset=decode_pc_pointer(target).offset
assert not consensus['unresolved'] and not consensus['exact_unresolved']
assert consensus['runtime_fallbacks']==0 and consensus['semantic_aliases']==0
assert consensus['aliases'][target_offset][0]=='mc/us_art_color_red'
assert consensus['aliases'][target_offset][2]=='block4 interval replay'
assert consensus['block4_replay_aliases']==1
assert all(stove.model.surfaces[i].material_name=='mc/us_art_color_red' for i in (1,5,9,12))
assert all(fridge.model.surfaces[i].material_name=='mc/us_art_color_red' for i in (0,2,4))
group=next(row for row in consensus['block4_replay']['rows'] if target_offset in row['targets'])
assignment=group['assignment'][0]
assert assignment['model']=='unanchored_owner' and assignment['surface']==1
assert assignment['score_parts']['consumer_model_count']==2
assert assignment['score_parts']['cross_consumer_tokens']==['red']
assert assignment['score_parts']['cross_consumer_material_overlap']==1
assert group['margin']>=consensus['block4_replay']['margin_threshold']
print({'passed':True,'raw':hex(raw),'resolved':s.material_name,
       'consumer_consensus_target':hex(target_offset),'consumer_consensus_alias':consensus['aliases'][target_offset]})

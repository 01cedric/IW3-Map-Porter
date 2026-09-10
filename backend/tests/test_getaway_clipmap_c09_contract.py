from __future__ import annotations

from pathlib import Path
import os
from types import SimpleNamespace
import struct

from cod4porter.assembler import (
    PC,
    _clip_raw_map,
    _resolve_clipmap_physpreset_symbols,
)
from cod4porter.assets.clipmap import ClipMapNode, ROOT_SIZE
from cod4porter.backend.v4_ffio import (
    FOLLOWING,
    decode_pc_pointer,
    decode_ps3_pointer,
    parse_xasset_list,
    read_pc_fastfile,
)
from cod4porter.graph import GraphContext, header_symbol, nested_symbol
from cod4porter.pc_clipmap import parse_clipmap
from cod4porter.pc_material import attach_xmodel_inline_materials_and_tails
from cod4porter.pc_xmodel import parse_all_xmodels
from cod4porter.physics import top_level_phys_presets, resolve_xmodel_phys_presets
from cod4porter.zone_writer import LARGE_BLOCK, TEMP_BLOCK, RelocatingZoneWriter


def _getaway_pc_fixture() -> Path | None:
    repo=Path(__file__).resolve().parents[1]
    explicit=os.environ.get('IW3_GETAWAY_PC_FF')
    candidates=([Path(explicit)] if explicit else []) + [
        repo/'reference/original_pc/mp_getaway_original_pc.ff',
    ]
    return next((path for path in candidates if path.is_file()),None)


fixture=_getaway_pc_fixture()
if fixture is None:
    print({'passed':True,'skipped':'real mp_getaway PC fixture unavailable'})
    raise SystemExit(0)

document=read_pc_fastfile(fixture)
asset_list=parse_xasset_list(document.zone,'pc')
clip=parse_clipmap(document.zone,'mp_getaway')
xmodels=parse_all_xmodels(document.zone,asset_list)
attach_xmodel_inline_materials_and_tails(document.zone,xmodels)

inline_phys_roots={
    int(getattr(x.model.inline_phys_preset,'root_offset',-1))
    for x in xmodels
    if x.model.inline_phys_preset is not None
    and getattr(x.model.inline_phys_preset,'root_offset',-1)>=0
}
phys_presets=top_level_phys_presets(document.zone,asset_list,xmodels,inline_phys_roots)
phys_resolution=resolve_xmodel_phys_presets(
    asset_list,phys_presets,xmodels,runtime_compatible=True,
)
core=SimpleNamespace(
    clipmap=clip,
    phys_resolution=phys_resolution,
    xmodels=tuple(xmodels),
    asset_list=asset_list,
)

# Source-loadstream oracle: the parser must continue beyond both DynEnt arrays and consume the
# three real inline PhysPreset roots/XStrings rather than stopping at the definition array.
assert len(clip.dyn_models)==235 and len(clip.dyn_brushes)==0
assert clip.known_end==0x451CD12
assert [
    (owner.list_kind,owner.index,owner.pointer_kind,owner.preset.name,owner.source_root,owner.source_end)
    for owner in clip.dyn_owned_phys_presets
]==[
    ('model',102,'following',',cinderblock',0x451CC6B,0x451CCA4),
    ('model',107,'following',',default',0x451CCA4,0x451CCD9),
    ('model',150,'following',',woodencross',0x451CCD9,0x451CD12),
]

phys_symbols={p.source_asset_index:f'physpreset:{p.source_asset_index}' for p in phys_presets}
xmodel_symbols={x.model.source_asset_index:f'xmodel:{x.model.source_asset_index}' for x in xmodels}
clip_symbol='clipmap:mp_getaway'
phys_raw,top_level_phys,xmodel_phys_aliases,dynent_phys_aliases=(
    _resolve_clipmap_physpreset_symbols(
        core,clip_symbol,phys_symbols,xmodel_symbols,
    )
)
assert len(phys_raw)==235 and all(phys_raw)
assert sum(phys_raw.count(raw) for raw in top_level_phys)==166
assert sum(phys_raw.count(raw) for raw in xmodel_phys_aliases)==30
assert sum(phys_raw.count(raw) for raw in dynent_phys_aliases)==36
assert sum(decode_pc_pointer(raw).kind in ('following','insert') for raw in phys_raw)==3
assert xmodel_phys_aliases[0x4013787D]==nested_symbol(xmodel_symbols[145],'insert.physPreset')
assert phys_raw.count(0x4013787D)==29
assert xmodel_phys_aliases[0x40314F8D]==nested_symbol(xmodel_symbols[299],'insert.physPreset')
assert phys_raw.count(0x40314F8D)==1
assert dynent_phys_aliases=={
    0x40EFFE21:nested_symbol(clip_symbol,'dynEnt.model.107.physPreset.field'),
    0x40F00E41:nested_symbol(clip_symbol,'dynEnt.model.150.physPreset.field'),
}
assert phys_raw.count(0x40EFFE21)==32 and phys_raw.count(0x40F00E41)==4

# Exercise the actual ClipMap writer with the real Getaway graph. External XAsset headers and
# XModel INSERT cells are represented by typed symbols allocated before the ClipMap, just as they
# are in the complete zone. This validates output fields, shared aliases and logical alignment.
xmodel_words=[m.xmodel_raw for m in clip.static_models]
fx_words=[]
pieces_words=[]
for entity in clip.dyn_models+clip.dyn_brushes:
    xmodel_words.append(entity.xmodel_raw)
    fx_words.append(entity.destroy_fx_raw)
    pieces_words.append(entity.destroy_pieces_raw)
assert not any(pieces_words)
xmodel_by_raw=_clip_raw_map(core,PC['xmodel'],xmodel_symbols,xmodel_words,'ClipMap XModel')
fx_symbols={a.index:f'fx:{a.index}' for a in asset_list.assets if a.type_id==PC['fx']}
fx_by_raw=_clip_raw_map(core,PC['fx'],fx_symbols,fx_words,'ClipMap FX') if any(fx_words) else {}
shared_name='gfxworld:mp_getaway::name'
shared_planes='gfxworld:mp_getaway::planes'
node=ClipMapNode(
    clip,
    clip_symbol,
    xmodel_symbols_by_raw=xmodel_by_raw,
    physpreset_symbols_by_raw=top_level_phys,
    xmodel_physpreset_alias_symbols_by_raw=xmodel_phys_aliases,
    dynent_physpreset_alias_symbols_by_raw=dynent_phys_aliases,
    fx_symbols_by_raw=fx_by_raw,
    shared_name_symbol=shared_name,
    shared_planes_symbol=shared_planes,
)
external_symbols=set(xmodel_by_raw.values())|set(top_level_phys.values())|set(fx_by_raw.values())
nodes={node.symbol:node,**{symbol:object() for symbol in external_symbols}}
context=GraphContext(nodes=nodes,top_level_symbols=tuple(sorted(external_symbols)))
writer=RelocatingZoneWriter()
writer.push_block(LARGE_BLOCK)
writer.define_symbol(shared_name)
writer.write_latin1z('mp_getaway','shared GfxWorld name fixture')
writer.align_logical_only(4)
writer.define_symbol(shared_planes)
writer.reserve_in_block(len(clip.planes)*0x14,'shared GfxWorld planes fixture')
for symbol in sorted(external_symbols):
    writer.align_logical_only(4)
    writer.define_symbol(header_symbol(symbol))
    writer.reserve_in_block(4,'typed external XAssetHeader fixture')
for symbol in sorted(set(xmodel_phys_aliases.values())):
    writer.align_logical_only(4)
    writer.define_symbol(symbol)
    writer.reserve_in_block(4,'XModel INSERT PhysPreset cell fixture')
writer.pop_block()
writer.push_block(TEMP_BLOCK)
writer.align_logical_only(4)
node.write(writer,context)
writer.pop_block()
output=writer.build()
result=context.results['clipmap:'+clip_symbol]

assert result['shared_name_symbol']==shared_name
assert result['shared_planes_symbol']==shared_planes
assert result['name_physical']==-1 and result['plane_physical']==-1
assert result['static_model_physical']==result['root_physical']+ROOT_SIZE
assert output.block_sizes[1]==235*(0x20+0x0C+0x14)==15040

logical=result['logical_allocations']
for label,alignment in (
    ('nodes',4),('leafBrushNodes',4),('vertices',4),('borders',4),
    ('brushes',16),('boxBrush',16),('dynModels',4),
):
    assert logical[label]['block']==LARGE_BLOCK
    assert logical[label]['offset']%alignment==0,(label,logical[label])
for label in ('mapEnts','ownedPhys.model.102','ownedPhys.model.107','ownedPhys.model.150'):
    assert logical[label]['block']==TEMP_BLOCK
    assert logical[label]['offset']%4==0,(label,logical[label])
alias=result['mapents_insert_alias_location']
assert alias.block==LARGE_BLOCK and alias.offset%4==0
assert logical['entityString']['block']==LARGE_BLOCK
assert logical['entityString']['offset']==alias.offset+4
assert struct.unpack_from('>I',output.zone_bytes,result['root_physical']+0xA4)[0]==0xFFFFFFFE
assert struct.unpack_from('>I',output.zone_bytes,result['root_physical']+0xA8)[0]==FOLLOWING

bindings=result['physpreset_bindings']
assert bindings['bindings']==235
assert bindings['owned_fields']==3
assert bindings['owned_roots']==3
assert bindings['owned_names']==[',cinderblock',',default',',woodencross']
assert bindings['dynent_alias_uses']==36
assert bindings['xmodel_alias_uses']==30
assert bindings['top_level_uses']==166
assert bindings['source_nonnull']==235 and bindings['destination_null']==0
owned_rows=bindings['owned_rows']
assert owned_rows[1]['root_physical']-owned_rows[0]['root_physical']==0x39
assert owned_rows[2]['root_physical']-owned_rows[1]['root_physical']==0x35

zone=output.zone_bytes
dyn_root=result['dyn_model_physical']
destination_words=[struct.unpack_from('>I',zone,dyn_root+i*0x60+0x30)[0] for i in range(235)]
assert destination_words.count(0)==0
assert destination_words.count(FOLLOWING)==3
assert sum(decode_ps3_pointer(raw,output.block_sizes).kind=='packed' for raw in destination_words)==232

print({
    'passed':True,
    'source_nonnull':len(phys_raw),
    'top_level_uses':bindings['top_level_uses'],
    'xmodel_alias_uses':bindings['xmodel_alias_uses'],
    'dynent_alias_uses':bindings['dynent_alias_uses'],
    'owned_roots':bindings['owned_names'],
    'runtime_block1':output.block_sizes[1],
    'logical_allocations':logical,
})

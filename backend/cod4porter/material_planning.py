from __future__ import annotations
from dataclasses import dataclass,replace
from collections import defaultdict
from typing import Iterable,Mapping,Sequence

from .backend.v4_ffio import XAssetList,decode_pc_pointer
from .material import MaterialRecord,MaterialTexture,MaterialStateConversion,convert_state
from .pc_material import scan_materials,parse_material_at
from .pc_xmodel import xasset_header_alias_index
from .pc_technique import TechniqueSet
from .technique_binding import TechniqueBinding
from .material_constants import bind_material_constants
from .platform_state import resolve_exact as resolve_platform_state, resolve_runtime_compatible as resolve_platform_state_runtime

PC_MATERIAL=0x04; PC_TECHSET=0x05; PC_IMAGE=0x06

@dataclass(frozen=True)
class TopLevelMaterial:
    source_asset_index:int
    material:MaterialRecord

@dataclass(frozen=True)
class ImageBinding:
    material_root:int
    texture_index:int
    image_name:str
    mode:str
    source_image_asset_index:int|None

@dataclass(frozen=True)
class PlannedMaterial:
    source:MaterialRecord
    state:MaterialStateConversion
    platform_state:bytes
    technique:TechniqueBinding
    image_names:tuple[str,...]
    source_asset_index:int|None=None
    platform_state_mode:str='exact'


@dataclass(frozen=True)
class MaterialUniverse:
    top_level:tuple[TopLevelMaterial,...]
    xmodel_inline:tuple[MaterialRecord,...]
    gfxworld_inline:tuple[MaterialRecord,...]
    fx_inline:tuple[MaterialRecord,...]
    all_unique:tuple[MaterialRecord,...]
    excluded_nested_roots:frozenset[int]
    image_proof:dict
    planned_owned:tuple[PlannedMaterial,...]
    external_shared:tuple[tuple[int|None,MaterialRecord],...]
    xmodel_material_resolution:dict
    # Materials whose planning failed and that the graph rebinds to ,$default
    # (runtime-compatible mode only): ({'name','root','source_asset_index','reason'}, ...)
    quarantined_materials:tuple=()

def _state_signature(states:Sequence[tuple[int,int]])->tuple[tuple[int,int],...]:
    return tuple((int(a)&0xffffffff,int(b)&0xffffffff) for a,b in states)


def resolve_state_tables(targets:Sequence[MaterialRecord],donors:Sequence[MaterialRecord]|None=None)->tuple[MaterialRecord,...]:
    '''Resolve only uniquely-identifiable packed PC Material state tables. No neutral bytes are invented.'''
    donors=tuple(donors or targets)
    exact=[d for d in donors if d.declared_state_bits_count>0 and len(d.state_bits)==d.declared_state_bits_count and not d.state_bits_resolution_kind.startswith('PackedUnresolved')]
    out=[]
    for m in sorted(targets,key=lambda x:x.root_offset):
        if m.declared_state_bits_count==0:
            out.append(replace(m,state_bits_resolution_kind='None'));continue
        if len(m.state_bits)==m.declared_state_bits_count:
            out.append(replace(m,state_bits_resolution_kind='PackedResolved' if m.state_bits_resolution_kind=='PackedUnresolved' else m.state_bits_resolution_kind));continue
        p=decode_pc_pointer(m.serialized_state_bits_pointer)
        if p.kind!='packed':
            out.append(replace(m,state_bits_resolution_kind='InvalidInlineStateTable'));continue
        cand=[d for d in exact if d.root_offset<m.root_offset and d.declared_state_bits_count==m.declared_state_bits_count and d.state_bits_entry==m.state_bits_entry and d.state_flags==m.state_flags and d.camera_region==m.camera_region]
        unique={_state_signature(d.state_bits) for d in cand}
        if len(unique)==1:
            out.append(replace(m,state_bits=next(iter(unique)),state_bits_resolution_kind='PackedResolvedBySignature'))
        else:
            out.append(replace(m,state_bits_resolution_kind='PackedUnresolvedAmbiguous'))
    # preserve caller order
    by={m.root_offset:m for m in out}
    return tuple(by[m.root_offset] for m in targets)


def top_level_materials(
    zone:bytes,asset_list:XAssetList,excluded_roots:Iterable[int]=(),
    expected_names_by_asset_index:Mapping[int,str]|None=None,*,
    first_root_hint:int|None=None,
)->tuple[TopLevelMaterial,...]:
    if asset_list.structural_index is not None:
        return tuple(TopLevelMaterial(row['index'],parse_material_at(zone,row['root'])[0])
            for row in asset_list.structural_index.rows(PC_MATERIAL))
    assets=sorted((a for a in asset_list.assets if a.type_id==PC_MATERIAL),key=lambda a:a.index)
    if not assets:return ()
    if any(decode_pc_pointer(a.serialized_pointer).kind not in ('following','insert') for a in assets):
        raise ValueError('Top-level PC Material XAssets must be inline owners')
    excluded=set(excluded_roots)
    expected=None
    if expected_names_by_asset_index and all(expected_names_by_asset_index.get(a.index) for a in assets):
        expected={expected_names_by_asset_index[a.index] for a in assets}
    candidates=[m for m in scan_materials(zone,expected) if m.root_offset not in excluded]
    starts=candidates
    if first_root_hint is not None:
        hinted=[m for m in candidates if m.root_offset==first_root_hint]
        if not hinted:
            raise ValueError(f'top-level Material root hint 0x{first_root_hint:X} is not a valid Material root')
        starts=hinted
    roots={m.root_offset for m in candidates};matches=[];seen=set()
    for candidate in starts:
        try:
            cursor=candidate.root_offset;run=[]
            for _ in assets:
                if cursor not in roots:raise ValueError('run left candidate roots')
                m,cursor=parse_material_at(zone,cursor)
                if not m.name:raise ValueError('empty material name')
                run.append(m)
            key=tuple(x.root_offset for x in run)
            if key not in seen:seen.add(key);matches.append(tuple(run))
        except (ValueError,OverflowError,IndexError):
            pass
    unfiltered=len(matches)
    if expected_names_by_asset_index and len(matches)>1:
        filtered=[]
        for run in matches:
            if all(not expected_names_by_asset_index.get(a.index) or run[i].name.casefold()==expected_names_by_asset_index[a.index].casefold() for i,a in enumerate(assets)):
                filtered.append(run)
        matches=filtered
    # A genuine top-level Material must reference a declared TechniqueSet XAssetHeader.
    # Apply this exact pointer proof even when there is only one declared Material: with
    # a one-asset run every structurally plausible nested byte window would otherwise be
    # a complete run.  On Getaway this collapses 15 non-nested structural candidates to
    # the unique compass_map_mp_getaway root.
    compatible=[]
    for run in matches:
        if all(xasset_header_alias_index(asset_list,m.technique_set_pointer,PC_TECHSET) is not None for m in run):compatible.append(run)
    if compatible:matches=compatible
    if len(matches)!=1:
        sample='; '.join('0x%X:%s'%(r[0].root_offset,'|'.join(x.name for x in r[:4])) for r in matches[:10])
        raise ValueError(f'top-level Material run ambiguous: {unfiltered} complete runs -> {len(matches)} after exact filters; {sample}')
    selected=resolve_state_tables(matches[0],candidates)
    return tuple(TopLevelMaterial(a.index,m) for a,m in zip(assets,selected))


def texture_signature(t:MaterialTexture)->tuple[int,int,int,int,int]:
    return (t.name_hash,t.name_start,t.name_end,t.sampler,t.semantic)


def serialized_image_pointer(m:MaterialRecord,idx:int)->int:
    water=m.water_by_texture.get(idx)
    return water.image_pointer if water is not None else m.textures[idx].resource_pointer


def _inserted_image_aliases(asset_list:XAssetList)->dict[int,int]:
    out={}
    for a in asset_list.assets:
        if a.type_id!=PC_IMAGE:continue
        p=decode_pc_pointer(a.serialized_pointer)
        if p.kind!='packed' or p.block!=4:continue
        if xasset_header_alias_index(asset_list,a.serialized_pointer,PC_IMAGE) is not None:continue
        old=out.get(p.offset)
        if old is not None and old!=a.index:raise ValueError(f'GfxImage InsertPointer alias block4+0x{p.offset:X} claimed by Image XAssets #{old} and #{a.index}')
        out[p.offset]=a.index
    return out


def resolve_image_identities(
    materials:Sequence[MaterialRecord],asset_list:XAssetList,*,allow_runtime_neutral:bool=False,
    block4_cell_aliases:Mapping[int,str]|None=None,
    block4_exact_cell_aliases:Iterable[int]=(),
    block4_cell_alias_modes:Mapping[int,str]|None=None,
    exact_slot_names:Mapping[tuple[int,int],str]|None=None,
)->dict:
    '''Resolve every MaterialTextureDef image from exact evidence only.

    Modes: inline, exact full TextureDef signature, direct Image XAssetHeader alias,
    or Image-root DB_InsertPointer alias derived from the Image XAsset's own serialized pointer.
    '''
    sig_names=defaultdict(set)
    for m in materials:
        for t in m.textures:
            if t.inline_image_name:sig_names[texture_signature(t)].add(t.inline_image_name.replace('\\','/').lstrip('/'))
    inserted=_inserted_image_aliases(asset_list)
    replay_aliases={
        int(offset):str(name).replace('\\','/').lstrip('/')
        for offset,name in dict(block4_cell_aliases or {}).items()
    }
    exact_replay_aliases={int(offset) for offset in block4_exact_cell_aliases}
    replay_modes={int(offset):str(mode) for offset,mode in dict(block4_cell_alias_modes or {}).items()}
    exact_slots={(int(root),int(index)):str(name).replace('\\','/').lstrip('/') for (root,index),name in dict(exact_slot_names or {}).items()}
    if not exact_replay_aliases.issubset(replay_aliases):
        raise ValueError('exact GfxImage replay targets must be present in block4_cell_aliases')
    if not set(replay_modes).issubset(replay_aliases):
        raise ValueError('GfxImage replay modes must refer to present block4_cell_aliases')
    if any(offset<0 for offset in replay_aliases):
        raise ValueError('GfxImage block-4 replay aliases must use non-negative offsets')
    if any(not name for name in replay_aliases.values()):
        raise ValueError('GfxImage block-4 replay aliases must use non-empty image identities')
    if any(not name for name in exact_slots.values()):
        raise ValueError('exact GfxImage slot identities must be non-empty')
    names_by_asset=defaultdict(set)
    def observe(ai:int,name:str):names_by_asset[ai].add(name.replace('\\','/').lstrip('/'))
    # First establish semantic identity for source Image asset indices using signatures that are already unanimous.
    for m in materials:
        for i,t in enumerate(m.textures):
            names=sig_names.get(texture_signature(t),set())
            if len(names)!=1:continue
            name=next(iter(names));raw=serialized_image_pointer(m,i)
            ai=xasset_header_alias_index(asset_list,raw,PC_IMAGE)
            if ai is not None:observe(ai,name);continue
            p=decode_pc_pointer(raw)
            if p.kind=='packed' and p.block==4 and p.offset in inserted:observe(inserted[p.offset],name)
    conflicts={ai:sorted(v,key=str.casefold) for ai,v in names_by_asset.items() if len(v)>1}
    if conflicts:raise ValueError('conflicting GfxImage semantic identities: '+repr(conflicts))
    exact_by_asset={ai:next(iter(v)) for ai,v in names_by_asset.items() if len(v)==1}
    bindings=[];unresolved=[];replay_targets_used=set();replay_unique_slots=0;replay_ambiguous_slots=0
    for m in materials:
        for i,t in enumerate(m.textures):
            if t.inline_image_name:
                bindings.append(ImageBinding(m.root_offset,i,t.inline_image_name,'inline',None));continue
            exact_slot=exact_slots.get((int(m.root_offset),i))
            if exact_slot is not None:
                bindings.append(ImageBinding(m.root_offset,i,exact_slot,'top_level_b4_xstring_replay',None));continue
            names=sig_names.get(texture_signature(t),set())
            raw=serialized_image_pointer(m,i);p=decode_pc_pointer(raw)
            if p.kind=='packed' and p.block==4 and p.offset in replay_aliases:
                replay_name=replay_aliases[p.offset]
                # The causal replay is only accepted when this use independently contains the
                # recovered identity in its complete TextureDef-signature candidate set.  This
                # keeps the new path fail-closed and prevents address arithmetic from inventing
                # an image name when source semantics disagree.
                if replay_name not in names and p.offset not in exact_replay_aliases:
                    raise ValueError(
                        f"GfxImage replay alias block4+0x{p.offset:X}='{replay_name}' is not "
                        f"supported by Material '{m.name}' texture[{i}] candidates {sorted(names)}"
                    )
                bindings.append(ImageBinding(
                    m.root_offset,i,replay_name,replay_modes.get(int(p.offset),'xmodel_block4_cell_replay'),None,
                ))
                replay_targets_used.add(int(p.offset))
                if len(names)==1:replay_unique_slots+=1
                elif len(names)>1:replay_ambiguous_slots+=1
                continue
            # Exact pointer identity outranks a non-unique semantic signature.  The pointer names
            # one already-proven source Image XAsset/DB_InsertPointer cell, while a TextureDef
            # signature is intentionally shared by many color/normal/specular images.  Reversing
            # this order silently turns exact references into neutral fallbacks.
            ai=xasset_header_alias_index(asset_list,raw,PC_IMAGE)
            if ai is not None and ai in exact_by_asset:
                bindings.append(ImageBinding(m.root_offset,i,exact_by_asset[ai],'direct_xasset_header',ai));continue
            if p.kind=='packed' and p.block==4 and p.offset in inserted:
                ai=inserted[p.offset]
                if ai in exact_by_asset:
                    bindings.append(ImageBinding(m.root_offset,i,exact_by_asset[ai],'insert_pointer_alias',ai));continue
            if len(names)>1:
                if allow_runtime_neutral:
                    neutral=f'__cp11_neutral_semantic_{int(t.semantic)&0xff:02x}'
                    bindings.append(ImageBinding(m.root_offset,i,neutral,'runtime_neutral_semantic',None));continue
                unresolved.append({'material':m.name,'root':m.root_offset,'texture':i,'reason':'TextureDef signature ambiguous','candidates':sorted(names)});continue
            if len(names)==1:
                bindings.append(ImageBinding(m.root_offset,i,next(iter(names)),'texturedef_signature',None));continue
            if allow_runtime_neutral:
                # Never invent an external semantic name.  The graph builder recognizes this
                # reserved identity and emits a tiny owned PS3 BC1 image keyed by texture semantic.
                neutral=f'__cp11_neutral_semantic_{int(t.semantic)&0xff:02x}'
                bindings.append(ImageBinding(m.root_offset,i,neutral,'runtime_neutral_semantic',None))
            else:
                unresolved.append({'material':m.name,'root':m.root_offset,'texture':i,'raw':raw,'pointer_kind':p.kind,'block':p.block,'offset':p.offset,'direct_image_asset_index':ai})
    neutral_count=sum(b.mode=='runtime_neutral_semantic' for b in bindings)
    mode_counts=defaultdict(int)
    for binding in bindings:mode_counts[binding.mode]+=1
    return {
        'passed':not unresolved,'bindings':tuple(bindings),'unresolved':unresolved,
        'neutral_fallback_count':neutral_count,'insert_aliases':inserted,
        'exact_name_by_source_asset_index':exact_by_asset,
        'block4_cell_aliases':replay_aliases,
        'block4_cell_alias_target_count':len(replay_aliases),
        'block4_cell_alias_targets_used':len(replay_targets_used),
        'block4_exact_cell_alias_target_count':len(exact_replay_aliases),
        'block4_cell_alias_unused_targets':sorted(set(replay_aliases)-replay_targets_used),
        'block4_cell_replay_binding_count':mode_counts.get('xmodel_block4_cell_replay',0),
        'block4_cell_replay_unique_signature_slots':replay_unique_slots,
        'block4_cell_replay_ambiguous_signature_slots':replay_ambiguous_slots,
        'binding_mode_counts':dict(sorted(mode_counts.items())),
    }


def plan_material(material:MaterialRecord,source_asset_index:int|None,asset_list:XAssetList,techniques_by_asset_index:Mapping[int,TechniqueSet],bindings_by_source_asset_index:Mapping[int,TechniqueBinding],image_binding_by_slot:Mapping[tuple[int,int],ImageBinding],*,runtime_compatible:bool=False)->PlannedMaterial:
    tech_index=xasset_header_alias_index(asset_list,material.technique_set_pointer,PC_TECHSET)
    if tech_index is None:raise ValueError(f"Material '{material.name}' TechniqueSet is not a direct top-level TechniqueSet XAssetHeader alias")
    technique=techniques_by_asset_index.get(tech_index);binding=bindings_by_source_asset_index.get(tech_index)
    if technique is None or binding is None or not binding.can_bind_native:raise ValueError(f"Material '{material.name}' TechniqueSet XAsset #{tech_index} has no native PS3 binding")
    binding=bind_material_constants(material,binding)
    state=convert_state(material,technique.technique_pointers)
    if runtime_compatible:
        platform,platform_mode=resolve_platform_state_runtime(material,state)
    else:
        platform=resolve_platform_state(material,state);platform_mode='exact'
    names=[]
    for i in range(len(material.textures)):
        b=image_binding_by_slot.get((material.root_offset,i))
        if b is None:raise ValueError(f"Material '{material.name}' texture[{i}] has no exact GfxImage identity")
        names.append(b.image_name)
    return PlannedMaterial(material,state,platform,binding,tuple(names),source_asset_index,platform_mode)

def plan_materials(
    materials:Sequence[tuple[int|None,MaterialRecord]],asset_list:XAssetList,
    techniques:Sequence[TechniqueSet],technique_bindings:Sequence[TechniqueBinding],
    all_image_donors:Sequence[MaterialRecord]|None=None,*,allow_runtime_neutral:bool=False,
    resolved_image_proof:Mapping[str,object]|None=None,quarantine:list|None=None,
)->tuple[PlannedMaterial,...]:
    """``quarantine`` (a list collector) turns per-material planning failures
    into rows instead of aborting the conversion: the caller rebinds the
    collected materials to the engine's ,$default (the hardware-proven CP12-A
    closure) so ANY material-planning error class - present or future - costs
    that one surface, never the map.  With ``quarantine=None`` (the full-port
    default) every failure still stops the conversion exactly as before."""
    donors=tuple(all_image_donors or (m for _,m in materials))
    image=dict(resolved_image_proof) if resolved_image_proof is not None else resolve_image_identities(
        donors,asset_list,allow_runtime_neutral=allow_runtime_neutral)
    if not image['passed']:
        raise ValueError(f"unresolved packed GfxImages ({len(image['unresolved'])}): {image['unresolved'][:4]}")
    byslot={(b.material_root,b.texture_index):b for b in image['bindings']}
    tech={x.source_asset_index:x for x in techniques};bind={x.source_asset_index:x for x in technique_bindings}
    planned=[]
    for ai,m in materials:
        try:
            planned.append(plan_material(m,ai,asset_list,tech,bind,byslot,runtime_compatible=allow_runtime_neutral))
        except ValueError as error:
            if quarantine is None:raise
            quarantine.append({'name':m.name,'root':int(m.root_offset),
                               'source_asset_index':ai,'reason':str(error)})
    return tuple(planned)

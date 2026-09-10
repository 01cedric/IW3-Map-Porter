from __future__ import annotations
from dataclasses import dataclass,field,replace
from pathlib import Path
from typing import Mapping,Sequence

from .backend.v4_ffio import FastFileDocument,XAssetList,read_pc_fastfile,parse_xasset_list
from .backend.v4_iwd import IwdImageEntry,IwdSoundEntry,inspect_iwds
from .pc_technique import ROOT as PC_TECHNIQUE_ROOT_SIZE,TechniqueSet,top_level_technique_sets
from .technique_binding import TechniqueBinding,bind_all
from .pc_xmodel import PcXModelIntermediate,parse_all_xmodels,resolve_skeleton_and_surface_reuse,resolve_xmodel_materials
from .pc_material import attach_xmodel_inline_materials_and_tails,parse_material_at
from .pc_fx import OwnedFxGraph,FxReference,top_level_fx
from .pc_clipmap import ClipMap,parse_clipmap
from .pc_gfxworld import GfxWorldReport,parse_gfxworld
from .material import MaterialRecord
from .material_planning import TopLevelMaterial,PlannedMaterial,MaterialUniverse,top_level_materials,resolve_state_tables,resolve_image_identities,plan_materials
from .physics import PhysPreset,top_level_phys_presets,recover_packed_phys_names,resolve_xmodel_phys_presets,resolve_phys_preset_sound_prefixes
from .pc_sound import SoundCatalog,SoundFileLayout,locate_and_parse_sound,associate_top_level_sound_children
from .pc_image import PcImageCatalog,locate_top_level_images
from .image_alias_replay import (
    replay_gfxworld_image_aliases,
    replay_top_level_material_image_xstrings,
    replay_xmodel_image_aliases,
)

@dataclass(frozen=True)
class CoreSourcePlan:
    pc_document:FastFileDocument
    asset_list:XAssetList
    map_name:str
    techniques:tuple[TechniqueSet,...]
    technique_bindings:tuple[TechniqueBinding,...]
    xmodels:tuple[PcXModelIntermediate,...]
    fx_references:tuple[FxReference,...]
    fx_graphs:tuple[OwnedFxGraph,...]
    clipmap:ClipMap
    gfxworld:GfxWorldReport
    materials:MaterialUniverse
    image_catalog:PcImageCatalog
    phys_presets:tuple[PhysPreset,...]
    phys_resolution:Mapping[str,object]
    sound_catalog:SoundCatalog
    phys_sound_resolution:Mapping[str,object]
    iwd_images:Mapping[str,IwdImageEntry]
    iwd_sounds:tuple[IwdSoundEntry,...]
    diagnostics:Mapping[str,object]=field(default_factory=dict)


def _unique_materials(rows:Sequence[MaterialRecord])->tuple[MaterialRecord,...]:
    by={}
    for m in rows:
        old=by.get(m.root_offset)
        if old is not None and old!=m:raise ValueError(f'Material root 0x{m.root_offset:X} reparsed inconsistently')
        by[m.root_offset]=m
    return tuple(by[k] for k in sorted(by))


def _replace_resolved(groups:Sequence[Sequence[MaterialRecord]])->tuple[tuple[MaterialRecord,...],...]:
    merged=_unique_materials(tuple(m for g in groups for m in g))
    resolved=resolve_state_tables(merged,merged);by={m.root_offset:m for m in resolved}
    return tuple(tuple(by[m.root_offset] for m in g) for g in groups)


def build_material_universe(zone:bytes,asset_list:XAssetList,techniques:Sequence[TechniqueSet],bindings:Sequence[TechniqueBinding],xmodels:Sequence[PcXModelIntermediate],xmodel_inline:Sequence[MaterialRecord],gfxworld:GfxWorldReport,fx_graphs:Sequence[OwnedFxGraph],*,runtime_compatible:bool=False,clipmap:ClipMap|None=None)->MaterialUniverse:
    gfx_mats=tuple(parse_material_at(zone,r)[0] for r in gfxworld.full.inline_material_roots)
    fx_roots=sorted({r for fx in fx_graphs for r in fx.inline_material_roots})
    fx_mats=tuple(parse_material_at(zone,r)[0] for r in fx_roots)
    nested_roots={m.root_offset for m in xmodel_inline}|{m.root_offset for m in gfx_mats}|{m.root_offset for m in fx_mats}
    material_assets=sorted((a for a in asset_list.assets if a.type_id==0x04),key=lambda a:a.index)
    first_material_hint=None
    if material_assets:
        previous_index=material_assets[0].index-1
        previous_technique=next((t for t in techniques if t.source_asset_index==previous_index),None)
        if previous_technique is not None:
            # The decompressed zone is the physical Load_XAsset replay stream.  When a Material
            # run immediately follows a TechniqueSet, its first root begins exactly after the
            # fixed PC TechniqueSet shell and FOLLOWING name.  This causal boundary rejects
            # structurally plausible Material-shaped byte windows inside shaders/models.
            first_material_hint=(
                previous_technique.root_offset+PC_TECHNIQUE_ROOT_SIZE+
                len(previous_technique.name.encode('latin-1'))+1
            )
    top=top_level_materials(zone,asset_list,nested_roots,first_root_hint=first_material_hint)
    topm=tuple(x.material for x in top)
    topm,xm,gfxm,fxm=_replace_resolved((topm,tuple(xmodel_inline),gfx_mats,fx_mats))
    top=tuple(TopLevelMaterial(t.source_asset_index,m) for t,m in zip(top,topm))
    all_unique=_unique_materials(topm+xm+gfxm+fxm)
    names_by_asset={x.source_asset_index:x.material.name for x in top}
    xres=resolve_xmodel_materials(xmodels,asset_list,names_by_asset,allow_semantic_sibling=False,runtime_fallback_material=',default' if runtime_compatible else None)
    if xres['unresolved']:
        raise ValueError(f"XModel Material closure unresolved ({len(xres['unresolved'])}): {xres['unresolved'][:6]}")
    image_replay=replay_xmodel_image_aliases(xmodels,tuple(xm),all_unique,xres,pc_zone=zone)
    from .pc_xmodel import apply_replayed_material_handles
    apply_replayed_material_handles(xmodels, xres, image_replay['handle_bases'])
    gfx_image_replay=replay_gfxworld_image_aliases(zone,gfxworld,tuple(gfxm),all_unique)
    top_image_replay=(
        replay_top_level_material_image_xstrings(zone,asset_list,clipmap,techniques,top)
        if clipmap is not None else
        {'slot_names':{},'passed':True,'reason':'ClipMap bridge not supplied','diagnostics':{}}
    )
    merged_aliases=dict(image_replay['aliases'])
    alias_modes={
        int(target):str(mode)
        for target,mode in image_replay.get('alias_modes',{}).items()
    }
    for target in merged_aliases:
        alias_modes.setdefault(int(target),'xmodel_block4_cell_replay')
    for target,name in gfx_image_replay['aliases'].items():
        old=merged_aliases.get(int(target))
        if old is not None and old!=name:
            raise ValueError(f'global GfxImage replay target b4+0x{int(target):X} conflicts: {old!r}/{name!r}')
        merged_aliases[int(target)]=name
        alias_modes[int(target)]=str(
            gfx_image_replay.get('alias_modes',{}).get(int(target),'gfxworld_block4_owner_replay')
        )
    exact_aliases=set(image_replay['exact_direct_alias_targets'])|set(gfx_image_replay['exact_direct_alias_targets'])
    image=resolve_image_identities(
        all_unique,asset_list,allow_runtime_neutral=runtime_compatible,
        block4_cell_aliases=merged_aliases,
        block4_exact_cell_aliases=exact_aliases,
        block4_cell_alias_modes=alias_modes,
        exact_slot_names=top_image_replay['slot_names'],
    )
    image['xmodel_block4_cell_replay']=image_replay
    image['gfxworld_block4_owner_replay']=gfx_image_replay
    image['top_level_b4_xstring_replay']=top_image_replay
    if not image['passed']:
        raise ValueError(f"GfxImage closure unresolved ({len(image['unresolved'])}): {image['unresolved'][:6]}")
    # Shared comma Materials are native/support-zone references and do not need local PS3 platformState.
    material_rows=[];external=[]
    source_index_by_root={x.material.root_offset:x.source_asset_index for x in top}
    for m in all_unique:
        ai=source_index_by_root.get(m.root_offset)
        if m.name.startswith(','):external.append((ai,m))
        else:material_rows.append((ai,m))
    planned=plan_materials(
        material_rows,asset_list,techniques,bindings,all_unique,
        allow_runtime_neutral=runtime_compatible,resolved_image_proof=image,
    )
    return MaterialUniverse(top,tuple(xm),tuple(gfxm),tuple(fxm),all_unique,frozenset(nested_roots),image,planned,tuple(external),xres)


def analyze_core_source(
    pc_ff:str|Path,map_name:str,iwd_paths:Sequence[str|Path]=(),*,texture_library_directory:str|Path|None=None,
    runtime_compatible:bool=False,sound_policy:str='omit',check_portal_policy:str|None=None,
)->CoreSourcePlan:
    if sound_policy not in ('omit','map-owned'):
        raise ValueError(f'unsupported sound policy {sound_policy!r}')
    doc=read_pc_fastfile(pc_ff);al=parse_xasset_list(doc.zone,'pc')
    clip=parse_clipmap(doc.zone,map_name)
    clip_counts={'planes':len(clip.planes),'submodels':len(clip.submodels),'static_models':len(clip.static_models),'dynamic_models':len(clip.dyn_models),'dynamic_brushes':len(clip.dyn_brushes)}
    gfx=parse_gfxworld(doc.zone,map_name,clip_counts)
    if check_portal_policy=='source' and gfx.full.portal_count:
        from .gfxworld_portals import resolve_portals
        resolve_portals(doc.zone,gfx)
    techniques=tuple(top_level_technique_sets(doc.zone,al));bindings=tuple(bind_all(techniques))
    xmodels=parse_all_xmodels(doc.zone,al);x_inline=attach_xmodel_inline_materials_and_tails(doc.zone,xmodels);reuse=resolve_skeleton_and_surface_reuse(doc.zone,xmodels)
    fx_refs=tuple(top_level_fx(doc.zone,al,excluded_ranges=((gfx.root_offset,gfx.full.physical_end),)));fx=tuple(x.graph for x in fx_refs if x.source_owned and x.graph is not None)
    images,sounds=inspect_iwds(iwd_paths,include_sounds=sound_policy!='omit') if iwd_paths else ({},[])
    materials=build_material_universe(doc.zone,al,techniques,bindings,xmodels,x_inline,gfx,fx,runtime_compatible=runtime_compatible,clipmap=clip)
    texture_library_files=[];library_selected=0
    if texture_library_directory:
        from .texture_library import library_archives, supplement_images
        texture_library_files=library_archives(texture_library_directory)
        needed={str(name).lstrip(',').casefold() for planned in materials.planned_owned for name in planned.image_names}
        needed.update(str(name).lstrip(',').casefold() for name in materials.image_proof['exact_name_by_source_asset_index'].values())
        library_selected=supplement_images(images,texture_library_files,needed)
    image_catalog=locate_top_level_images(
        doc.zone,al,expected_names_by_asset_index=materials.image_proof['exact_name_by_source_asset_index'],
        iwd_names=(e.image.name for e in images.values()))
    inline_phys_roots={
        int(getattr(x.model.inline_phys_preset,'root_offset',-1))
        for x in xmodels if x.model.inline_phys_preset is not None and getattr(x.model.inline_phys_preset,'root_offset',-1)>=0
    }
    phys_presets=tuple(top_level_phys_presets(doc.zone,al,xmodels,inline_phys_roots))
    recovered_phys_names=recover_packed_phys_names(phys_presets,xmodels,runtime_compatible=runtime_compatible)
    phys_resolution=resolve_xmodel_phys_presets(al,phys_presets,xmodels,runtime_compatible=runtime_compatible)
    if sound_policy=='omit':
        # Sound payloads are deliberately outside the custom-map contract.  Do not walk the
        # source Sound graph at all: this both enforces the policy and prevents PC audio layouts
        # from becoming a prerequisite for an otherwise valid world conversion.
        sound_catalog=SoundCatalog(
            (),SoundFileLayout.LEGACY_0C,al.asset_data_offset,al.asset_data_offset,
            None,0,0,0,0,0,
        )
    else:
        sound_catalog=locate_and_parse_sound(doc.zone,al,map_name)
        sound_catalog=associate_top_level_sound_children(doc.zone,al,sound_catalog)
    packed_xstrings=dict(sound_catalog.packed_string_evidence)
    phys_sound_resolution=resolve_phys_preset_sound_prefixes(
        phys_presets,xmodels,packed_xstrings,
        runtime_compatible=runtime_compatible or sound_policy=='omit',
    )
    type_counts={}
    type_runs=[]
    for asset in al.assets:
        type_counts[asset.type_name]=type_counts.get(asset.type_name,0)+1
        if type_runs and type_runs[-1]['type']==asset.type_name:type_runs[-1]['count']+=1
        else:type_runs.append({'type':asset.type_name,'count':1})
    exact_xmodel_unresolved=tuple(materials.xmodel_material_resolution.get('exact_unresolved',()))
    exact_xmodel_unresolved_targets=sorted({
        (int(row.get('block',-1)),int(row.get('offset',-1)))
        for row in exact_xmodel_unresolved
    })
    diag={
        'script_strings':len(al.script_strings),'xassets':len(al.assets),'asset_type_counts':type_counts,'asset_type_runs':type_runs,'techniques':len(techniques),'xmodels':len(xmodels),
        'fx_xassets':len(fx_refs),'owned_fx_graphs':len(fx),'shared_fx':sum(not x.source_owned for x in fx_refs),'materials_total':len(materials.all_unique),'materials_top_level':len(materials.top_level),
        'materials_xmodel_inline':len(materials.xmodel_inline),'materials_gfxworld_inline':len(materials.gfxworld_inline),'materials_fx_inline':len(materials.fx_inline),
        'planned_owned_materials':len(materials.planned_owned),'shared_materials':len(materials.external_shared),
        'phys_presets':len(phys_presets),'phys_preset_names_recovered':recovered_phys_names,'phys_preset_name_runtime_fallbacks':sum(bool(getattr(p,'name_runtime_fallback',False)) for p in list(phys_presets)+[x.model.inline_phys_preset for x in xmodels if x.model.inline_phys_preset is not None]),
        'xmodel_phys_references':phys_resolution['references'],'xmodel_phys_resolved':phys_resolution['resolved'],'xmodel_phys_exact_resolved':phys_resolution.get('exact_resolved',phys_resolution['resolved']),'xmodel_phys_runtime_null_fallbacks':phys_resolution.get('runtime_null_fallbacks',0),
        'sound_policy':sound_policy,'sound_xassets_omitted':sum(a.type_id in (0x07,0x08,0x09) for a in al.assets) if sound_policy=='omit' else 0,
        'sound_lists':len(sound_catalog.lists),'sound_packed_refs':sound_catalog.packed_reference_count,'sound_resolved_packed_refs':sound_catalog.resolved_packed_reference_count,
        'sound_packed_xstring_evidence':len(sound_catalog.packed_string_evidence),'phys_sound_prefixes_resolved':phys_sound_resolution['resolved_from_global_xstring'],'phys_sound_prefix_runtime_null_fallbacks':phys_sound_resolution.get('runtime_null_fallbacks',0),
        'phys_sound_prefix_runtime_empty_fallbacks':phys_sound_resolution.get('runtime_empty_fallbacks',0),
        'phys_sound_prefix_exact_unresolved':phys_sound_resolution.get('exact_unresolved',()),
        'xmodel_packed_materials':materials.xmodel_material_resolution['packed'],
        'xmodel_unresolved_materials':len(materials.xmodel_material_resolution['unresolved']),
        'xmodel_material_runtime_fallbacks':materials.xmodel_material_resolution.get('runtime_fallbacks',0),
        'xmodel_material_exact_unresolved':len(materials.xmodel_material_resolution.get('exact_unresolved',())),
        'xmodel_material_direct_aliases':materials.xmodel_material_resolution.get('direct_xasset_aliases',0),
        'xmodel_material_reliable_bases':materials.xmodel_material_resolution.get('reliable_bases',0),
        'xmodel_material_causal_base_candidates':materials.xmodel_material_resolution.get('causal_base_diagnostics',{}).get('candidate_count',0),
        'xmodel_material_causal_rejections':materials.xmodel_material_resolution.get('causal_base_diagnostics',{}).get('causal_rejections',0),
        'xmodel_material_block4_replay_aliases':materials.xmodel_material_resolution.get('block4_replay_aliases',0),
        'xmodel_material_block4_replay_groups':materials.xmodel_material_resolution.get('block4_replay',{}).get('groups',0),
        'xmodel_material_block4_replay_remaining_targets':materials.xmodel_material_resolution.get('block4_replay',{}).get('remaining_targets',0),
        'xmodel_material_lod_aliases':materials.xmodel_material_resolution.get('lod_aliases',0),
        'xmodel_material_lod_conflicts':materials.xmodel_material_resolution.get('lod_conflicts',0),
        'xmodel_material_resolved_aliases':materials.xmodel_material_resolution.get('resolved_aliases',0),
        'xmodel_material_ambiguous_targets':len(materials.xmodel_material_resolution.get('ambiguous',{})),
        'xmodel_material_exact_unresolved_targets':len(exact_xmodel_unresolved_targets),
        'xmodel_material_exact_unresolved_sample':list(exact_xmodel_unresolved[:12]),
        'xmodel_material_ambiguous_sample':[
            {'target_offset':int(target),'evidence':value}
            for target,value in list(sorted(materials.xmodel_material_resolution.get('ambiguous',{}).items()))[:12]
        ],
        'images_exact_bindings':len(materials.image_proof['bindings']),'images_unresolved':len(materials.image_proof['unresolved']),'neutral_image_fallbacks':materials.image_proof.get('neutral_fallback_count',0),
        'image_block4_replay_alias_targets':materials.image_proof.get('block4_cell_alias_target_count',0),
        'image_block4_replay_targets_used':materials.image_proof.get('block4_cell_alias_targets_used',0),
        'image_block4_replay_bindings':materials.image_proof.get('block4_cell_replay_binding_count',0),
        'image_block4_replay_ambiguous_slots_resolved':materials.image_proof.get('block4_cell_replay_ambiguous_signature_slots',0),
        'image_block4_replay_unique_slots_confirmed':materials.image_proof.get('block4_cell_replay_unique_signature_slots',0),
        'image_block4_secondary_models':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('accepted_model_count',0),
        'image_block4_secondary_targets':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('accepted_target_count',0),
        'image_block4_secondary_slots':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('accepted_slot_count',0),
        'image_block4_secondary_candidate_models':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('candidate_model_count',0),
        'image_block4_secondary_candidate_bases':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('candidate_base_count',0),
        'image_block4_secondary_single_target_rejections':materials.image_proof.get('xmodel_block4_cell_replay',{}).get('unanchored_multitarget_replay',{}).get('single_target_candidates_rejected',0),
        'image_binding_mode_counts':materials.image_proof.get('binding_mode_counts',{}),
        'top_level_images':len([a for a in al.assets if a.type_id==0x06]),'top_level_inline_images':image_catalog.inline_asset_count,'top_level_image_names_exact':len(image_catalog.exact_name_by_asset_index),
        'texture_library':{'archives':[str(p) for p in texture_library_files],'selected_images':library_selected},
        'iwd_images':len(images),'iwd_sounds':len(sounds),'xmodel_reuse':reuse,
    }
    return CoreSourcePlan(doc,al,map_name,techniques,bindings,tuple(xmodels),fx_refs,fx,clip,gfx,materials,image_catalog,phys_presets,phys_resolution,sound_catalog,phys_sound_resolution,images,tuple(sounds),diag)

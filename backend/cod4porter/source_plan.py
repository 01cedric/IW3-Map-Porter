from __future__ import annotations
from dataclasses import dataclass,field,replace
from pathlib import Path
from typing import Mapping,Sequence

from .backend.v4_ffio import FastFileDocument,XAssetList,read_pc_fastfile,parse_xasset_list
from .backend.v4_iwd import IwdImageEntry,IwdSoundEntry,inspect_iwds
from .pc_technique import ROOT as PC_TECHNIQUE_ROOT_SIZE,TechniqueSet,top_level_technique_sets
from .technique_binding import TechniqueBinding,TechniqueEvidence,bind_all,bind_technique
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
    fx_omission:object|None=None


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
    fx_roots=sorted({r for fx in fx_graphs for r in fx.inline_material_roots}|set(getattr(asset_list.structural_index,'additional_material_roots',())))
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
    gfx_image_replay=replay_gfxworld_image_aliases(
        zone,gfxworld,tuple(gfxm),all_unique,
        structural_index=getattr(asset_list,'structural_index',None))
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
    quarantine=[] if runtime_compatible else None
    planned=plan_materials(
        material_rows,asset_list,techniques,bindings,all_unique,
        allow_runtime_neutral=runtime_compatible,resolved_image_proof=image,
        quarantine=quarantine,
    )
    return MaterialUniverse(top,tuple(xm),tuple(gfxm),tuple(fxm),all_unique,frozenset(nested_roots),image,planned,tuple(external),xres,tuple(quarantine or ()))


def _upgrade_bindings(zone,index,techniques,bindings,*,policy='auto',donor=None):
    """PC-to-RSX shader compilation for TechniqueSets the console cannot resolve.

    Every binding the availability ladder left UNSUPPORTED is parsed completely
    (techniques, passes, declarations, shader bytecode, argument tables) and
    translated to an owned PS3 TechniqueSet with RSX microcode.  A techset the
    compiler cannot prove stays UNSUPPORTED with the exact blocking construct
    appended, and the FX-omission policy then applies to it as before.
    """
    from .rsx.techset_parse import PcTechsetParseError,parse_full_techniqueset
    from .rsx.techset_compile import TechsetCompileError,compile_techniqueset
    from .rsx.d3d9 import D3d9Error
    from .rsx.donor import DonorCatalogError
    from .rsx.translate import RsxTranslationError
    upgraded=[];report={'compiled':[],'failed':[],'donor_transplants':[]}
    for technique,binding in zip(techniques,bindings):
        prefer_upgrade=(policy=='prefer' and binding.evidence is TechniqueEvidence.PS3_FAMILY_SUBSTITUTION)
        if binding.can_bind_native and not prefer_upgrade:
            upgraded.append(binding);continue
        if getattr(technique,'shared',False):
            # A ',name' reference carries no PC technique payload in this zone;
            # "compiling" its empty shell would fabricate a techset that renders
            # nothing while reporting success.  It stays on its current path
            # (UNSUPPORTED -> the omission policy, or the kept substitution).
            upgraded.append(binding)
            report['failed'].append({
                'name':binding.candidate_name,'source_asset_index':binding.source_asset_index,
                'kept_family_substitution':prefer_upgrade,
                'error':'shared ,reference carries no PC technique payload to compile',
            })
            continue
        source_name=technique.ps3_reference_candidate
        if donor is not None and source_name in donor:
            try:
                compiled=donor.techniqueset(source_name,source_asset_index=binding.source_asset_index)
            except DonorCatalogError as error:
                # A stale or corrupt donor entry must not abort the whole port;
                # the compile path below still applies to this techset.
                report['failed'].append({
                    'name':binding.candidate_name,'source_asset_index':binding.source_asset_index,
                    'kept_family_substitution':False,'donor_error':True,
                    'error':f'donor transplant failed: {error}',
                })
            else:
                upgraded.append(replace(
                    binding,candidate_name=compiled.name,
                    evidence=TechniqueEvidence.RETAIL_DONOR,can_bind_native=True,
                    reason='Transplanted byte-faithfully from a retail PS3 zone via the donor catalog.',
                    compiled=compiled))
                report['donor_transplants'].append({'name':compiled.name,
                    'source_asset_index':binding.source_asset_index})
                continue
        try:
            full=parse_full_techniqueset(zone,index.pointer,technique)
            compiled=compile_techniqueset(full)
            if not any(slot is not None for slot in compiled.slots):
                raise TechsetCompileError(
                    'no PC technique maps into the 26 PS3 slots; an empty owned '
                    'techset would render nothing')
            upgraded.append(replace(
                binding,candidate_name=compiled.name,
                evidence=TechniqueEvidence.COMPILED_RSX,can_bind_native=True,
                reason=('No native PS3 TechniqueSet exists; the PC shader graph was compiled '
                        'to RSX microcode and the generated zone owns the TechniqueSet.'),
                compiled=compiled))
            report['compiled'].append({
                'name':compiled.name,'source_asset_index':binding.source_asset_index,
                'estimate_bytes':compiled.serialized_size_estimate(),
                'dropped_pc_slots':list(compiled.dropped_pc_slots),
                'notes':list(compiled.notes)[:20],
            })
        except (PcTechsetParseError,TechsetCompileError,D3d9Error,RsxTranslationError) as error:
            if prefer_upgrade:
                # The family substitution stays in place; record why fidelity
                # could not be raised further.
                upgraded.append(binding)
            else:
                upgraded.append(replace(
                    binding,reason=binding.reason+f' PC-to-RSX compilation also failed: {error}'))
            report['failed'].append({
                'name':binding.candidate_name,'source_asset_index':binding.source_asset_index,
                'kept_family_substitution':prefer_upgrade,
                'error':str(error),
            })
    return tuple(upgraded),report


def analyze_core_source(
    pc_ff:str|Path,map_name:str,iwd_paths:Sequence[str|Path]=(),*,texture_library_directory:str|Path|None=None,
    runtime_compatible:bool=False,sound_policy:str='omit',check_portal_policy:str|None=None,structure_report_path:str|Path|None=None,unsupported_fx_policy:str='omit',
    shader_compilation:str='auto',donor_catalog:str|Path|None=None,
)->CoreSourcePlan:
    if sound_policy not in ('omit','map-owned'):
        raise ValueError(f'unsupported sound policy {sound_policy!r}')
    doc=read_pc_fastfile(pc_ff);al=parse_xasset_list(doc.zone,'pc')
    from .pc_structure import read_structure
    index=read_structure(doc.zone,report_path=structure_report_path)
    al=replace(al,structural_index=index)
    clip=parse_clipmap(doc.zone,map_name,structural_index=index)
    clip_counts={'planes':len(clip.planes),'submodels':len(clip.submodels),'static_models':len(clip.static_models),'dynamic_models':len(clip.dyn_models),'dynamic_brushes':len(clip.dyn_brushes)}
    gfx=parse_gfxworld(doc.zone,map_name,clip_counts,structural_index=index)
    if check_portal_policy=='source' and gfx.full.portal_count:
        from .gfxworld_portals import resolve_portals
        resolve_portals(doc.zone,gfx)
    if shader_compilation not in ('auto','prefer','off'):
        raise ValueError(f'unsupported shader_compilation policy {shader_compilation!r}')
    techniques=tuple(top_level_technique_sets(doc.zone,al));all_bindings=tuple(bind_technique(t) for t in techniques)
    compile_report={'policy':shader_compilation,'compiled':[],'failed':[],'donor_transplants':[]}
    donor=None
    if donor_catalog:
        from .rsx.donor import load_donor_catalog
        donor=load_donor_catalog(donor_catalog)
        compile_report['donor_catalog']={'path':str(donor_catalog),'techsets':len(donor.names())}
    if shader_compilation in ('auto','prefer'):
        all_bindings,compiled_rows=_upgrade_bindings(doc.zone,index,techniques,all_bindings,
                                                     policy=shader_compilation,donor=donor)
        compile_report.update(compiled_rows)
    from .fx_omission import plan_omissions
    from .pc_fx import resolve_fx_runner_bindings
    # Probe every owner's packed runner/impact references against source
    # topology BEFORE planning omissions: an owner whose references cannot be
    # proven routes into the same per-effect omission policy as an
    # unsupported shader, instead of stopping the whole conversion.
    fx_refs=tuple(top_level_fx(doc.zone,replace(al,structural_index=index.with_all_roots()),excluded_ranges=((gfx.root_offset,gfx.full.physical_end),)))
    runner_probe=resolve_fx_runner_bindings(map_name,fx_refs,zone=doc.zone,technique_sets=techniques,on_unprovable='collect')
    unresolvable={int(row['root_offset']):row['reason'] for row in runner_probe.failed_owners}
    omission=plan_omissions(index,techniques,all_bindings,unsupported_fx_policy,unresolvable_fx=unresolvable)
    bindings=tuple(b for t,b in zip(techniques,all_bindings) if t.root_offset not in omission.roots)
    if any(not b.can_bind_native for b in bindings):raise ValueError('Unsupported shader survived FX omission')
    index.omitted_roots=omission.roots
    index.additional_material_roots=omission.report.get('retained_shared_material_roots',[])
    xmodels=parse_all_xmodels(doc.zone,al);x_inline=attach_xmodel_inline_materials_and_tails(doc.zone,xmodels);reuse=resolve_skeleton_and_surface_reuse(doc.zone,xmodels)
    fx=tuple(x.graph for x in fx_refs if x.source_owned and x.graph is not None and x.root_offset not in omission.roots)
    images,sounds=inspect_iwds(iwd_paths,include_sounds=sound_policy!='omit') if iwd_paths else ({},[])
    images={k:v for k,v in images.items() if v.image.name.replace('\\','/').lstrip(',').casefold() not in omission.image_names}
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
        'pc_structure':index.summary(),'unsupported_fx':omission.report,
        'shader_compilation':compile_report,
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
    return CoreSourcePlan(doc,al,map_name,techniques,bindings,tuple(xmodels),fx_refs,fx,clip,gfx,materials,image_catalog,phys_presets,phys_resolution,sound_catalog,phys_sound_resolution,images,tuple(sounds),diag,omission)

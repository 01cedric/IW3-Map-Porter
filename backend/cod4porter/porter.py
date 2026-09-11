from __future__ import annotations

from dataclasses import dataclass,asdict
from pathlib import Path
from typing import Sequence,Mapping,Any
import json,hashlib

from .assembler import analyze_and_assemble,AssemblyResult
from .main_ff import save_main_fastfile,MainBuildResult
from .readback import inspect_main_fastfile,verify_build_roundtrip
from .load_zone import write_load_fastfile,read_load_document
from .pc_load_converter import convert_pc_load_to_ps3
from .graph import AssetType
from .zone_writer import declared_block_size
from .assets.image import ExternalImageNode,ImageNode,ImageResourcePolicy
from .assets.gfxworld import GfxWorldResourcePolicy
from .assets.material import MaterialNode
from .assets.external import ExternalMaterialNode,ExternalTechniqueSetNode,ExternalXModelNode
from .assets.fx import OwnedFxNode,ImpactFxNode
from .assets.basic import RawFileNode,StringTableNode,LightDefNode
from .assets.sound import SoundNode
from .material_graph import norm,balanced_resource_policies
from .ps3_budget import (
    DEFAULT_MIN_BASE_DIMENSION,
    DEFAULT_ZONE_BUDGET_BYTES,
    RETAIL_PS3_MAX_TEXTURE_EDGE,
    RETAIL_PS3_SHIPMENT_ZONE_BYTES,
    ZoneBudgetExceeded,
    check_zone_allocation,
    downscale_image,
    plan_image_budget,
)
from .source_assets import top_level_rawfiles,top_level_stringtables,top_level_lightdefs
from .pc_fx import parse_impact_fx
from .platform_state import (
    build_signature as platform_signature,
    exact_signature_count,
    catalog_sha256 as platform_catalog_sha256,
    matches_exact as platform_matches_exact,
    runtime_resolution_audit,
)
from .backend.v4_gate import evidence,evaluate_full_fidelity,evidence_from_conversion_report,evidence_from_load_readback
from .backend.v4_iwd import cubemap_face_fingerprints

@dataclass(frozen=True)
class ConversionClosure:
    source_xassets:int
    classified_source_xassets:int
    source_sound_xassets:int
    emitted_or_external_sound_xassets:int
    source_iwd_audio_payloads:int
    bound_iwd_audio_payloads:int
    source_iwd_images:int
    emitted_owned_iwd_images:int
    source_phys_sound_refs:int
    resolved_phys_sound_refs:int
    source_material_semantics:int
    output_material_semantics:int
    source_water_materials:int
    output_water_materials:int
    unresolved_material_state_slots:int
    unresolved_platform_states:int
    packed_image_refs:int
    resolved_packed_image_refs:int
    neutral_image_fallbacks:int
    water_fallbacks:int
    cubemap_source_count:int
    cubemap_output_count:int
    unproven_cubemaps:int
    source_techsets:int
    exact_techsets:int
    unsupported_techsets:int
    owned_fx:int
    exact_owned_fx:int
    shared_fx:int
    exact_shared_fx:int
    source_fx_visual_refs:int
    resolved_exact_fx_visual_refs:int
    runtime_fx_visual_fallbacks:int
    impactfx:int
    exact_impactfx:int
    lightdefs:int
    exact_lightdefs:int
    rawfiles:int
    exact_rawfiles:int
    stringtables:int
    exact_stringtables:int
    xmodel_source_vertices:int
    xmodel_output_vertices:int
    xmodel_source_triangles:int
    xmodel_output_triangles:int
    gfx_source_surfaces:int
    gfx_output_surfaces:int
    gfx_source_vertices:int
    gfx_output_vertices:int
    clip_source_brushes:int
    clip_output_brushes:int
    clip_source_triangles:int
    clip_output_triangles:int
    unresolved_output_refs:int
    source_pointer_leaks:int
    independent_readback_passed:bool
    deterministic_write:bool
    passed:bool
    blockers:tuple[str,...]

    def fidelity_section(self)->dict:
        return {
          'SourceXAssetCount':self.source_xassets,'ClassifiedSourceXAssetCount':self.classified_source_xassets,
          'SourceSoundXAssetCount':self.source_sound_xassets,'EmittedSoundXAssetCount':self.emitted_or_external_sound_xassets,
          'SourceCustomAudioPayloadCount':self.source_iwd_audio_payloads,'BoundCustomAudioPayloadCount':self.bound_iwd_audio_payloads,
          'SourceIwdImageCount':self.source_iwd_images,'EmittedOwnedIwdImageCount':self.emitted_owned_iwd_images,
          'SourcePhysPresetSoundAliasReferenceCount':self.source_phys_sound_refs,'ResolvedPhysPresetSoundAliasReferenceCount':self.resolved_phys_sound_refs,
          'SourceMaterialSemanticCount':self.source_material_semantics,'OutputMaterialSemanticCount':self.output_material_semantics,
          'SourceWaterMaterialCount':self.source_water_materials,'OutputWaterMaterialCount':self.output_water_materials,
          'UnresolvedMaterialStateSlotCount':self.unresolved_material_state_slots,'UnresolvedPlatformStateCount':self.unresolved_platform_states,
          'PackedImageReferenceCount':self.packed_image_refs,'ResolvedPackedImageReferenceCount':self.resolved_packed_image_refs,
          'NeutralImageFallbackCount':self.neutral_image_fallbacks,'WaterFallbackCount':self.water_fallbacks,
          'SourceCubemapCount':self.cubemap_source_count,'OutputCubemapCount':self.cubemap_output_count,'UnprovenCubemapCount':self.unproven_cubemaps,
          'SourceTechniqueSetCount':self.source_techsets,'ExactTechniqueSetCount':self.exact_techsets,'UnsupportedCustomTechniqueSetCount':self.unsupported_techsets,
          'OwnedFxCount':self.owned_fx,'ExactOwnedFxCount':self.exact_owned_fx,'SharedFxCount':self.shared_fx,'ExactSharedFxCount':self.exact_shared_fx,
          'SourceFxVisualReferenceCount':self.source_fx_visual_refs,'ResolvedExactFxVisualReferenceCount':self.resolved_exact_fx_visual_refs,'RuntimeFxVisualFallbackCount':self.runtime_fx_visual_fallbacks,
          'ImpactFxReferenceCount':self.impactfx,'ResolvedImpactFxReferenceCount':self.exact_impactfx,
          'LightDefAttenuationReferenceCount':self.lightdefs,'ResolvedLightDefAttenuationReferenceCount':self.exact_lightdefs,
          'RequiredRawFileCount':self.rawfiles,'ExactRawFileCount':self.exact_rawfiles,'RequiredStringTableCount':self.stringtables,'ExactStringTableCount':self.exact_stringtables,
          'XModelSourceVertexCount':self.xmodel_source_vertices,'XModelOutputVertexCount':self.xmodel_output_vertices,
          'XModelSourceTriangleCount':self.xmodel_source_triangles,'XModelOutputTriangleCount':self.xmodel_output_triangles,
          'GfxWorldSourceSurfaceCount':self.gfx_source_surfaces,'GfxWorldOutputSurfaceCount':self.gfx_output_surfaces,
          'GfxWorldSourceVertexCount':self.gfx_source_vertices,'GfxWorldOutputVertexCount':self.gfx_output_vertices,
          'ClipMapSourceBrushCount':self.clip_source_brushes,'ClipMapOutputBrushCount':self.clip_output_brushes,
          'ClipMapSourceTriangleCount':self.clip_source_triangles,'ClipMapOutputTriangleCount':self.clip_output_triangles,
          'UnresolvedOutputXAssetReferenceCount':self.unresolved_output_refs,'PackedSourcePointerLeakCount':self.source_pointer_leaks,
          'IndependentPs3ReadbackPassed':self.independent_readback_passed,'DeterministicWritePassed':self.deterministic_write,
          'ConversionClosurePassed':self.passed,
        }


def _results_of(build:MainBuildResult,prefix:str)->list[Mapping[str,Any]]:
    return [v for k,v in build.graph_results.items() if k.startswith(prefix)]

def _source_material_semantics(a:AssemblyResult)->set[str]:
    return {norm(m.name).casefold() for m in a.source.materials.all_unique}

def _output_material_semantics(a:AssemblyResult)->set[str]:
    return {norm(name).casefold() for name in a.material_graph.material_symbol_by_name}

def _synthetic_material_helper_names(a:AssemblyResult)->set[str]:
    # Runtime-compatible conversion may introduce a native shared `,default` Material for
    # unresolved FX visuals even after XModel Material closure reaches zero.  Derive helper
    # identity from the source/output semantic sets instead of coupling it to one consumer.
    source=_source_material_semantics(a);output=_output_material_semantics(a)
    return (output-source)&{'default'}

def _semantic_material_count(a:AssemblyResult)->int:
    return len(_source_material_semantics(a))

def _water_semantics(a:AssemblyResult)->set[str]:
    return {norm(m.name).casefold() for m in a.source.materials.all_unique if m.contains_water}

def _owned_iwd_image_names(a:AssemblyResult)->set[str]:
    names=set()
    for n in a.plan.assets:
        if isinstance(n,ImageNode):names.add(norm(n.image.name).casefold())
    return names

def _iwd_image_names(a:AssemblyResult)->set[str]:
    return {norm(e.image.name).casefold() for e in a.source.iwd_images.values()}

def _native_dependency_manifest(a:AssemblyResult)->dict:
    """List every asset the generated zone expects the PS3 installation to provide.

    A ``,name`` root is not converted data: at load time IW3 resolves it through
    DB_FindXAssetHeader against the already-installed support/common zones.  If the
    target installation does not contain that name, the reference resolves to a
    default or fails - which shows up as a loading-screen stall or missing visuals,
    not as a porter error.  TechniqueSets are the whole family in this category:
    the porter never converts a PC shader graph, it only re-references one by name.
    """
    from .technique_binding import audited_retail_names
    audited={x.casefold() for x in audited_retail_names()}
    techsets=[]
    bindings = {p.technique.candidate_name.casefold(): p.technique
                for p in a.source.materials.planned_owned}
    for node in sorted((n for n in a.plan.assets if isinstance(n, ExternalTechniqueSetNode)),
                       key=lambda n:n.name.casefold()):
        name=node.name.lstrip(',')
        binding=bindings.get(name.casefold())
        techsets.append({
            'source_name':binding.source_name if binding else None,'required_ps3_name':name,
            'evidence':binding.evidence.value if binding else 'serialized_external',
            'source_was_shared':bool(binding.source_was_shared) if binding else None,
            'in_bundled_retail_catalog':name.casefold() in audited,
        })
    unique_techsets={x['required_ps3_name'].casefold():x for x in techsets}
    from .assets.techset import OwnedTechniqueSetNode
    owned_techsets=[]
    for node in sorted((n for n in a.plan.assets if isinstance(n,OwnedTechniqueSetNode)),
                       key=lambda n:n.name.casefold()):
        compiled=node.compiled
        binding=bindings.get(node.name.casefold())
        # Without a planned-material binding the artifact itself says how it
        # was produced: donor transplants carry their retail provenance note.
        fallback_evidence=('ps3_retail_donor_transplant'
                          if any(str(note).startswith('transplanted from retail')
                                 for note in compiled.notes)
                          else 'compiled_pc_rsx_translation')
        owned_techsets.append({
            'name':node.name,
            'source_name':binding.source_name if binding else None,
            'evidence':binding.evidence.value if binding else fallback_evidence,
            'owned_techniques':sum(1 for t in compiled.slots if t is not None),
            'estimate_bytes':compiled.serialized_size_estimate(),
            'dropped_pc_slots':list(compiled.dropped_pc_slots),
            'notes':list(compiled.notes)[:20],
        })
    # Derive this from the nodes that were actually serialized, not from the PC source
    # inventory: --unresolved-material-policy default replaces a Material the console
    # cannot own with a ,default reference, and reporting the original name would
    # describe a dependency the zone no longer has.
    materials=sorted({
        norm(getattr(n,'name','')).lstrip(',') for n in a.plan.assets
        if isinstance(n,ExternalMaterialNode)
    } - {''})
    images=sorted({
        n.name.lstrip(',') for n in a.plan.assets
        if isinstance(n,ExternalImageNode)
    })
    proven=sum(1 for x in unique_techsets.values() if x['in_bundled_retail_catalog'])
    return {
        'contract':'these names are resolved by the target PS3 installation, not written into the zone',
        'techsets':{
            'unique_required':len(unique_techsets),
            'with_bundled_retail_evidence':proven,
            'without_direct_evidence':len(unique_techsets)-proven,
            'entries':sorted(unique_techsets.values(),key=lambda x:x['required_ps3_name'].casefold()),
            'owned_in_zone':len(owned_techsets),
            'owned_entries':owned_techsets,
        },
        'external_materials':materials,
        'external_images':images,
        'external_xmodels':sorted({n.name.lstrip(',') for n in a.plan.assets if isinstance(n,ExternalXModelNode)}),
        'sound_policy_note':(
            'sound-policy omit: no map-specific Sound/SndCurve/LoadedSound XAsset is written; '
            'every sound the map scripts reference must already exist in the PS3 installation'
        ),
    }


def build_conversion_closure(a:AssemblyResult,b:MainBuildResult,readback:Mapping[str,Any]|None=None)->ConversionClosure:
    core=a.source;al=core.asset_list;block=[]
    disp=a.plan.validate(al)
    source_sound=sum(x.type_id==0x07 for x in al.assets);sound_disps=sum(x.source_type_id==0x07 for x in a.plan.dispositions)
    sb=a.sound_binding
    src_phys_refs=int(core.diagnostics.get('xmodel_phys_references',0));resolved_phys_refs=int(core.diagnostics.get('xmodel_phys_resolved',0))
    phys_sound_unresolved=len(core.phys_sound_resolution.get('unresolved',()))
    if phys_sound_unresolved:block.append(f'{phys_sound_unresolved} PhysPreset sound-prefix refs unresolved')

    image_bindings=core.materials.image_proof['bindings']
    packed_image_refs=sum(getattr(x,'mode','') not in ('inline','texturedef_signature') for x in image_bindings)
    resolved_packed=packed_image_refs
    image_unresolved=len(core.materials.image_proof['unresolved'])
    if image_unresolved:block.append(f'{image_unresolved} packed image refs unresolved')
    delayed_image_bytes=sum(int(row.get('omitted_resource_bytes',0) or 0) for row in _results_of(b,'image:'))
    if delayed_image_bytes:
        block.append(f'{delayed_image_bytes} owned Material/Image resource bytes are retail-delayed without a generated PS3 stream container')
    gfx_omitted_bytes=sum(
        int((row.get('image_resource_stats') or {}).get('omitted_resource_bytes',0) or 0)
        for row in _results_of(b,'gfxworld:')
    )
    if gfx_omitted_bytes:
        block.append(f'{gfx_omitted_bytes} GfxWorld image bytes are omitted without a generated PS3 stream container')
    bad_states=sum(bool(p.state.invalid_pc_state_indices or p.state.neutralized_out_of_range) for p in core.materials.planned_owned)
    malformed_platform=sum(len(p.platform_state)!=0x38 for p in core.materials.planned_owned)
    bad_platform=sum(getattr(p,'platform_state_mode','exact')!='exact' for p in core.materials.planned_owned)
    neutral_images=int(core.materials.image_proof.get('neutral_fallback_count',0))
    runtime_fx_materials=int(a.diagnostics.get('fx_runtime_material_visuals',0) or 0)
    runtime_fx_nulls=int(a.diagnostics.get('fx_runtime_null_visuals',0) or 0)
    runtime_fx_fallbacks=runtime_fx_materials+runtime_fx_nulls
    if bad_states:block.append(f'{bad_states} Material plans retain invalid/neutralized state slots')
    if malformed_platform:block.append(f'{malformed_platform} Material PlatformState plans have invalid payload size')
    if bad_platform:block.append(f'{bad_platform} Material PlatformState plans use non-exact compatibility families')
    if neutral_images:block.append(f'{neutral_images} Material image slots use synthetic neutral resources')
    if runtime_fx_materials:block.append(f'{runtime_fx_materials} FX Material visuals use the shared runtime default')
    if runtime_fx_nulls:block.append(f'{runtime_fx_nulls} FX visuals use a runtime NULL compatibility target')
    runtime_phys_prefixes=int(core.diagnostics.get('phys_sound_prefix_runtime_empty_fallbacks',0) or 0)
    if runtime_phys_prefixes:block.append(f'{runtime_phys_prefixes} PhysPreset sound prefixes use an empty compatibility string')

    iwd_names=_iwd_image_names(a);owned_image_names=_owned_iwd_image_names(a);missing_iwd=sorted(iwd_names-owned_image_names)
    if missing_iwd:block.append(f'{len(missing_iwd)} IWD images are not emitted as owned destination Images: {missing_iwd[:5]}')
    source_material_names=_source_material_semantics(a);output_material_names=_output_material_semantics(a)
    synthetic_material_helper_names=_synthetic_material_helper_names(a)
    source_material_sem=len(source_material_names);output_material_sem=len(output_material_names)
    effective_output_material_names=output_material_names-synthetic_material_helper_names
    if source_material_names!=effective_output_material_names:
        missing_materials=sorted(source_material_names-effective_output_material_names)
        extra_materials=sorted(effective_output_material_names-source_material_names)
        block.append(
            f'Material semantic closure: source={source_material_sem} output={output_material_sem} '
            f'helpers={len(synthetic_material_helper_names)} missing={missing_materials[:5]} extra={extra_materials[:5]}'
        )
    water_names=_water_semantics(a);output_water={norm(n.source.name).casefold() for n in a.plan.assets if isinstance(n,MaterialNode) and n.source.contains_water}
    if water_names!=output_water:block.append(f'Water Material closure: source={sorted(water_names)} output={sorted(output_water)}')

    source_cubes={norm(e.image.name).casefold() for e in core.iwd_images.values() if e.image.is_cubemap}
    output_cubes={norm(n.image.name).casefold() for n in a.plan.assets if isinstance(n,ImageNode) and n.image.is_cubemap}
    # Writer preserves all six source faces deterministically, but retail face-order proof is separate.
    unproven_cubes=len(source_cubes) if source_cubes else 0
    if source_cubes!=output_cubes:block.append(f'Cubemap conversion closure: source={sorted(source_cubes)} output={sorted(output_cubes)}')

    omitted_indices=core.fx_omission.asset_indices if core.fx_omission is not None else set()
    retained_fx=[x for x in core.fx_references if x.source_asset_index not in omitted_indices]
    owned_fx=sum(x.source_owned for x in retained_fx);shared_fx=len(retained_fx)-owned_fx
    exact_fx=len(_results_of(b,'fx.owned:'));exact_shared=len(_results_of(b,'fx.external:'))
    source_fx_visuals=int(a.diagnostics.get('fx_source_packed_visuals',0) or 0)
    exact_fx_visuals=int(a.diagnostics.get('fx_exact_packed_visuals',0) or 0)
    impact_src=sum(x.type_id==0x1A for x in al.assets);impact_out=len(_results_of(b,'impactfx:'))
    light_src=sum(x.type_id==0x11 for x in al.assets);light_out=len(_results_of(b,'lightdef:'))
    raw_src=sum(x.type_id==0x1F for x in al.assets);raw_out=len(_results_of(b,'rawfile:'))
    st_src=sum(x.type_id==0x20 for x in al.assets);st_out=len(_results_of(b,'stringtable:'))
    source_tech=len(core.technique_bindings);exact_tech=sum(x.source_type_id==0x05 and x.source_index not in omitted_indices for x in a.plan.dispositions)

    xm_src_v=sum(s.vertex_count for x in core.xmodels for s in x.model.surfaces);xm_src_t=sum(s.triangle_count for x in core.xmodels for s in x.model.surfaces)
    xm_rows=_results_of(b,'xmodel:');xm_out_v=sum(int(x['vertex_count']) for x in xm_rows);xm_out_t=sum(int(x['triangle_count']) for x in xm_rows)
    gfx_rows=_results_of(b,'gfxworld:');gfx_out_s=sum(int(x['surface_count']) for x in gfx_rows);gfx_out_v=sum(int(x['vertex_count']) for x in gfx_rows)
    clip_rows=_results_of(b,'clipmap:');clip_out_b=sum(int(x['brushes']) for x in clip_rows);clip_out_t=sum(int(x['triangles']) for x in clip_rows)

    if sb.unresolved_owned:
        block.append(f'{len(sb.unresolved_owned)} owned Sound references unresolved: {list(sb.unresolved_owned[:5])}')
    pairs=[
      ('source XAsset classification',len(al.assets),disp['source_assets']),('Sound source classification',source_sound,sound_disps),
      # IWD archives may legally contain unused audio files. Runtime closure is all referenced owned
      # Sound payloads resolved, not all packaging-surplus members consumed. Inventory/used counts
      # remain visible in FidelityClosure without manufacturing a false blocker.
      ('IWD image ownership',len(iwd_names),len(owned_image_names & iwd_names)),
      ('XModel PhysPreset refs',src_phys_refs,resolved_phys_refs),('TechniqueSet classification',source_tech,exact_tech),
      ('owned FX',owned_fx,exact_fx),('shared FX',shared_fx,exact_shared),('FX packed visuals',source_fx_visuals,exact_fx_visuals),('ImpactFx',impact_src,impact_out),('LightDef',light_src,light_out),('RawFile',raw_src,raw_out),('StringTable',st_src,st_out),
      ('XModel vertices',xm_src_v,xm_out_v),('XModel triangles',xm_src_t,xm_out_t),('GfxWorld surfaces',core.gfxworld.surface_count,gfx_out_s),('GfxWorld vertices',core.gfxworld.vertex_count,gfx_out_v),('ClipMap brushes',len(core.clipmap.brushes),clip_out_b),('ClipMap triangles',len(core.clipmap.triangle_indices)//3,clip_out_t),
    ]
    for label,x,y in pairs:
        if x!=y:block.append(f'{label}: source={x} output/resolved={y}')
    rb_pass=True;leaks=0;unresolved_out=0
    if readback is not None:
        rb_pass=bool(readback.get('passed',False));leaks=int(readback.get('pointer_leaks',0) or 0)
        unresolved_out=0 if rb_pass else 1
        if not rb_pass:block.append('independent main FastFile readback failed')
        if leaks:block.append(f'{leaks} destination relocation/pointer readback failures')
    deterministic=bool(b.deterministic)
    if not deterministic:block.append('main FastFile write is not deterministic')
    return ConversionClosure(
      len(al.assets),disp['source_assets'],source_sound,sound_disps,sb.staged_payload_count,sb.staged_payload_count,
      len(iwd_names),len(owned_image_names&iwd_names),src_phys_refs,resolved_phys_refs,source_material_sem,output_material_sem,
      len(water_names),len(output_water),bad_states,bad_platform,packed_image_refs,resolved_packed,neutral_images,0,
      len(source_cubes),len(output_cubes),unproven_cubes,source_tech,exact_tech,0,owned_fx,exact_fx,shared_fx,exact_shared,source_fx_visuals,exact_fx_visuals,runtime_fx_fallbacks,
      impact_src,impact_out,light_src,light_out,raw_src,raw_out,st_src,st_out,
      xm_src_v,xm_out_v,xm_src_t,xm_out_t,core.gfxworld.surface_count,gfx_out_s,core.gfxworld.vertex_count,gfx_out_v,
      len(core.clipmap.brushes),clip_out_b,len(core.clipmap.triangle_indices)//3,clip_out_t,unresolved_out,leaks,rb_pass,deterministic,not block,tuple(block))


def _pc_source_evidence(a:AssemblyResult)->dict:
    c=a.source;al=c.asset_list;ev=[]
    ev.append(evidence('original_pc_ff_parsed',True,'pc_source_deep',{'file_sha256':c.pc_document.file_sha256,'zone_sha256':c.pc_document.zone_sha256,'zone_bytes':len(c.pc_document.zone)}))
    ev.append(evidence('iwd_inventory_exact',bool(c.iwd_images or c.iwd_sounds),'pc_source_deep',{'images':len(c.iwd_images),'audio':len(c.iwd_sounds),'archives':sorted({e.archive_path for e in c.iwd_images.values()}|{e.archive_path for e in c.iwd_sounds})}))
    ev.append(evidence('source_xasset_profile_exact',True,'pc_source_deep',{'xassets':len(al.assets),'script_strings':len(al.script_strings),'type_counts':{n:sum(x.type_name==n for x in al.assets) for n in sorted({x.type_name for x in al.assets})}}))
    try:
        val=a.plan.validate(al);closure_ok=val['source_assets']==len(al.assets)
        validation_error=None
    except Exception as exc:
        closure_ok=False;val={};validation_error=str(exc)
    ev.append(evidence('source_xasset_closure',closure_ok,'pc_source_deep',{'source_assets':len(al.assets),'classified':val.get('source_assets'),'validation_error':validation_error}))
    source_sound=sum(x.type_id==0x07 for x in al.assets);sc=c.sound_catalog
    # Phase-dependent/external XStrings are not Sound graph nodes. Every structural packed
    # Sound reference must resolve; external/shared strings remain explicit evidence instead of
    # being miscounted as a broken SndCurve/LoadedSound/SpeakerMap edge.
    structural_packed=max(0,sc.packed_reference_count-sc.external_or_phase_dependent_strings)
    sound_policy=str(c.diagnostics.get('sound_policy','omit'))
    sound_ok=(
        len(sc.lists)==0 and structural_packed==0
        if sound_policy=='omit'
        else len(sc.lists)==source_sound and structural_packed==sc.resolved_packed_reference_count
    )
    ev.append(evidence('sound_graph_closure',sound_ok,'pc_source_deep',{
        'declared':source_sound,'parsed_lists':len(sc.lists),'packed_refs':sc.packed_reference_count,
        'structural_packed_refs':structural_packed,'resolved_packed_refs':sc.resolved_packed_reference_count,
        'external_or_phase_dependent_xstrings':sc.external_or_phase_dependent_strings,
        'policy':sound_policy,'intentionally_omitted_xassets':source_sound if sound_policy=='omit' else 0}))
    ev.append(evidence('sound_iwd_payload_closure',True,'pc_source_deep',{
        'audio_inventory':len(c.iwd_sounds),'referenced_unique_payloads':a.sound_binding.staged_payload_count,
        'unreferenced_archive_members':list(a.sound_binding.unreferenced_staged),'parsed_sound_lists':len(sc.lists)}))
    phys_un=len(c.phys_sound_resolution.get('unresolved',()))
    ev.append(evidence('physpreset_sound_closure',phys_un==0,'pc_source_deep',{'references':int(c.diagnostics.get('xmodel_phys_references',0)),'unresolved_sound_prefixes':phys_un}))
    bad_states=sum(bool(p.state.invalid_pc_state_indices or p.state.neutralized_out_of_range) for p in c.materials.planned_owned)
    ev.append(evidence('material_state_slots_exact',bad_states==0,'pc_source_deep',{'planned_materials':len(c.materials.planned_owned),'bad_materials':bad_states}))
    img=c.materials.image_proof;ev.append(evidence('packed_image_identity_exact',not img['unresolved'],'pc_source_deep',{'bindings':len(img['bindings']),'unresolved':len(img['unresolved']),'insert_aliases':len(img.get('insert_aliases',{}))}))
    cubes=[e.image for e in c.iwd_images.values() if e.image.is_cubemap]
    ev.append(evidence('cubemap_exact',all(len(cubemap_face_fingerprints(x,True))==6 for x in cubes),'pc_source_deep',{'cubemap_count':len(cubes),'source_face_hashes':{x.name:cubemap_face_fingerprints(x,True) for x in cubes}}))
    owned=sum(x.source_owned for x in c.fx_references);ev.append(evidence('fx_owned_exact',owned==len(c.fx_graphs),'pc_source_deep',{'owned_declared':owned,'owned_graphs':len(c.fx_graphs)}))
    # Reporting must consume the typed objects already proven by assemble_core_source rather than
    # launching a second heuristic whole-zone scan with weaker/no source-order hints.
    impact_nodes=[n for n in a.plan.assets if isinstance(n,ImpactFxNode)]
    impact_decl=sum(x.type_id==0x1A for x in al.assets)
    ev.append(evidence('impactfx_exact',len(impact_nodes)==impact_decl,'pc_source_deep',{'declared':impact_decl,'parsed':len(impact_nodes)}))
    light_nodes=[n for n in a.plan.assets if isinstance(n,LightDefNode)]
    light_decl=sum(x.type_id==0x11 for x in al.assets)
    ev.append(evidence('lightdef_attenuation_exact',len(light_nodes)==light_decl,'pc_source_deep',{'declared':light_decl,'parsed':len(light_nodes),'with_attenuation':sum(n.attenuation_image_symbol is not None for n in light_nodes)}))
    raw_nodes=[n for n in a.plan.assets if isinstance(n,RawFileNode)]
    raw_decl=sum(x.type_id==0x1F for x in al.assets)
    ev.append(evidence('rawfiles_exact',len(raw_nodes)==raw_decl,'pc_source_deep',{'declared':raw_decl,'parsed':len(raw_nodes),'names':[n.name for n in raw_nodes]}))
    table_nodes=[n for n in a.plan.assets if isinstance(n,StringTableNode)]
    table_decl=sum(x.type_id==0x20 for x in al.assets)
    ev.append(evidence('stringtable_exact',len(table_nodes)==table_decl,'pc_source_deep',{'declared':table_decl,'parsed':len(table_nodes),'shapes':[(n.columns,n.rows) for n in table_nodes]}))
    return {'source':'pc_source_deep','evidence':ev}


def _conversion_report_doc(closure:ConversionClosure)->dict:
    return {'FidelityClosure':closure.fidelity_section()}

def _ps3_readback_evidence(rb:Mapping[str,Any],closure:ConversionClosure)->dict:
    fam=rb.get('family_checks') or [];by_type={}
    for r in fam:by_type.setdefault(r.get('type'),[]).append(r)
    ok=lambda t: bool(by_type.get(t)) and all(x.get('passed') for x in by_type[t])
    ev=[evidence('deterministic_write',closure.deterministic_write,'ps3_readback',{'writer_deterministic':closure.deterministic_write,'zone_sha256':rb.get('zone_sha256')}),
        evidence('output_xasset_closure',bool(rb.get('passed')),'ps3_readback',{'asset_count':rb.get('asset_count'),'checks':rb.get('checks')}),
        evidence('pointer_leaks_zero',closure.source_pointer_leaks==0,'ps3_readback',{'pointer_leaks':closure.source_pointer_leaks,'relocations':rb.get('relocation_checks')})]
    if by_type.get('XMODEL'):ev.append(evidence('xmodel_exact',ok('XMODEL'),'ps3_readback',{'roots':len(by_type['XMODEL']),'failed':[x['symbol'] for x in by_type['XMODEL'] if not x['passed']]}))
    if by_type.get('GFXWORLD'):ev.append(evidence('gfxworld_exact',ok('GFXWORLD'),'ps3_readback',{'roots':len(by_type['GFXWORLD'])}))
    if by_type.get('CLIPMAP_MP'):ev.append(evidence('clipmap_exact',ok('CLIPMAP_MP'),'ps3_readback',{'roots':len(by_type['CLIPMAP_MP'])}))
    if by_type.get('MATERIAL'):
        ev.append(evidence('platform_state_exact',ok('MATERIAL'),'ps3_readback',{'material_roots':len(by_type['MATERIAL'])}))
        ev.append(evidence('water_exact',ok('MATERIAL') and closure.water_fallbacks==0,'ps3_readback',{'water_materials':closure.output_water_materials}))
        ev.append(evidence('neutral_images_zero',closure.neutral_image_fallbacks==0,'ps3_readback',{'neutral_image_fallbacks':closure.neutral_image_fallbacks}))
    if by_type.get('TECHSET'):ev.append(evidence('technique_sets_exact',ok('TECHSET'),'ps3_readback',{'techset_roots':len(by_type['TECHSET'])}))
    if by_type.get('RAWFILE'):ev.append(evidence('rawfiles_exact',ok('RAWFILE'),'ps3_readback',{'roots':len(by_type['RAWFILE'])}))
    if by_type.get('STRINGTABLE'):ev.append(evidence('stringtable_exact',ok('STRINGTABLE'),'ps3_readback',{'roots':len(by_type['STRINGTABLE'])}))
    if by_type.get('SOUND'):ev.append(evidence('sound_graph_closure',ok('SOUND'),'ps3_readback',{'roots':len(by_type['SOUND'])}))
    if by_type.get('FX'):ev.append(evidence('fx_owned_exact',ok('FX'),'ps3_readback',{'roots':len(by_type['FX'])}))
    if by_type.get('IMPACTFX'):ev.append(evidence('impactfx_exact',ok('IMPACTFX'),'ps3_readback',{'roots':len(by_type['IMPACTFX'])}))
    if by_type.get('LIGHTDEF'):ev.append(evidence('lightdef_attenuation_exact',ok('LIGHTDEF'),'ps3_readback',{'roots':len(by_type['LIGHTDEF'])}))
    return {'source':'ps3_readback','evidence':ev}


def _bundled_retail_evidence(a:AssemblyResult)->dict:
    # PlatformState templates are byte-exact observations extracted from the hardware-working Fix89B baseline.
    # Revalidate both signature membership and the exact 0x38 output payload here instead of trusting the planner.
    rows=[]
    for p in a.source.materials.planned_owned:
        sig=platform_signature(p.source,p.state)
        rows.append({'material':p.source.name,'signature':sig,'matched':platform_matches_exact(sig,p.platform_state),'payload_sha256':hashlib.sha256(p.platform_state).hexdigest().upper() if len(p.platform_state)==0x38 else None})
    platform_ok=all(r['matched'] for r in rows)
    ev=[evidence('platform_state_exact',platform_ok,'retail_observation',{'hardware_baseline':'Fix89B','catalog_signatures':exact_signature_count(),'catalog_sha256':platform_catalog_sha256(),'required_signatures':len(rows),'resolved_signatures':sum(r['matched'] for r in rows),'unresolved_materials':[r['material'] for r in rows if not r['matched']],'catalog_file':'platformstate_fix89b.json'})]
    waters=[m for m in a.source.materials.planned_owned if m.source.contains_water]
    # FIX89B hardware observation proved the PS3-only +0x0C water pointer is FOLLOWING. Payload equality itself is validated in conversion+readback.
    ev.append(evidence('water_exact',True,'retail_observation',{'hardware_baseline':'Fix89B','hardware_main_sha256':'0F94852CA8A3882D049B95CCD6C46DA7E6088F64D3E319A1D2E042041953ADF2','ps3_root_size':0x48,'pc_root_size':0x44,'extra_pointer_offset':0x0c,'extra_pointer_value':'0xFFFFFFFF','required_water_materials':len(waters)}))
    # Cubemap target face order requires six PS3-retail face fingerprints. Do not manufacture it here.
    return {'source':'retail_observation','evidence':ev}


def _platform_state_validation(a:AssemblyResult)->dict:
    """Expose exact/family/synthetic PlatformState evidence without conflating the tiers."""
    rows=[]
    for planned in a.source.materials.planned_owned:
        row=runtime_resolution_audit(planned.source,planned.state)
        serialized_hash=hashlib.sha256(planned.platform_state).hexdigest().upper() if len(planned.platform_state)==0x38 else None
        rows.append(dict(
            row,
            material=planned.source.name,
            planner_mode=planned.platform_state_mode,
            serialized_payload_sha256=serialized_hash,
            serialized_payload_match=(
                row['mode']==planned.platform_state_mode
                and serialized_hash==row['payload_sha256']
            ),
        ))
    mode_counts={mode:sum(row['mode']==mode for row in rows) for mode in sorted({row['mode'] for row in rows})}
    tier_counts={tier:sum(row['tier']==tier for row in rows) for tier in ('exact_signature','hardware_observed_family','synthetic_assignment')}
    return {
        'passed':all(row['serialized_payload_match'] for row in rows),
        'retail_exact_passed':bool(rows) and tier_counts['exact_signature']==len(rows),
        'material_count':len(rows),
        'mode_counts':mode_counts,
        'tier_counts':tier_counts,
        'catalog_signatures':exact_signature_count(),
        'catalog_sha256':platform_catalog_sha256(),
        'synthetic_materials':[row['material'] for row in rows if row['tier']=='synthetic_assignment'],
        'rows':rows,
    }


def _cubemap_retail_doc(a:AssemblyResult,target_face_hashes:Mapping[str,Sequence[str]]|None)->dict:
    ev=[];target_face_hashes=target_face_hashes or {}
    for e in a.source.iwd_images.values():
        if not e.image.is_cubemap:continue
        target=target_face_hashes.get(norm(e.image.name).casefold()) or target_face_hashes.get(e.image.name)
        src=cubemap_face_fingerprints(e.image,True)
        passed=False;mapping={}
        if target is not None and len(target)==6:
            used=set();passed=True
            for si,h in enumerate(src):
                cand=[i for i,x in enumerate(target) if str(x).upper()==h and i not in used]
                if len(cand)!=1:passed=False;break
                mapping[si]=cand[0];used.add(cand[0])
            passed=passed and len(used)==6
        ev.append(evidence('cubemap_exact',passed,'retail_observation',{'image':e.image.name,'source_face_hashes':src,'target_face_hashes':list(target or []),'mapping':mapping,'reason':None if passed else 'six unique PS3-retail face hashes were not supplied/matched'}))
    if not ev:
        ev.append(evidence('cubemap_exact',True,'retail_observation',{'cubemap_count':0,'reason':'source contains no cubemap'}))
    return {'source':'retail_observation','evidence':ev}



def _known_profile_comparison(a:AssemblyResult,closure:ConversionClosure)->dict|None:
    if a.source.map_name.casefold()!='mp_getaway':return None
    path=Path(__file__).resolve().parent.parent/'reference'/'mp_getaway_known_profile.json'
    if not path.exists():return None
    spec=json.loads(path.read_text(encoding='utf-8'))['source'];c=a.source;d=c.diagnostics
    actual={
      'xassets':len(c.asset_list.assets),'script_strings':len(c.asset_list.script_strings),'iwd_images':len(c.iwd_images),'iwd_audio':len(c.iwd_sounds),
      'rawfiles':closure.rawfiles,'sound_lists':closure.source_sound_xassets,'xmodels':len(c.xmodels),
      'xsurfaces':sum(x.model.surface_count for x in c.xmodels),'xmodel_vertices':closure.xmodel_source_vertices,'xmodel_triangles':closure.xmodel_source_triangles,
      'materials_unique':len(c.materials.all_unique),'gfxworld_surfaces':c.gfxworld.surface_count,'gfxworld_vertices':c.gfxworld.vertex_count,'gfxworld_indices':c.gfxworld.index_count,'gfxworld_cells':c.gfxworld.cell_count,
      'clipmap_planes':len(c.clipmap.planes),'clipmap_static_models':len(c.clipmap.static_models),'clipmap_brushes':len(c.clipmap.brushes),'clipmap_triangles':len(c.clipmap.triangle_indices)//3,
    }
    rows={k:{'expected':v,'actual':actual.get(k),'matched':actual.get(k)==v} for k,v in spec.items()}
    return {'binding':False,'passed':all(r['matched'] for r in rows.values()),'profile_file':str(path.resolve()),'rows':rows}

def port_map(
    pc_ff:str|Path,map_name:str,iwd_paths:Sequence[str|Path],output_dir:str|Path,*,
    texture_library_directory:str|Path|None=None,load_baseline:str|Path|None=None,ffmpeg:str='ffmpeg',
    pc_load_ff:str|Path|None=None,retail_load_reference:str|Path|None=None,
    cubemap_target_face_hashes:Mapping[str,Sequence[str]]|None=None,
    runtime_compatible:bool=False,boot_isolation:bool|None=None,
    gfxworld_resource_policy:GfxWorldResourcePolicy|str=GfxWorldResourcePolicy.FASTFILE_DELAYED,
    material_image_resource_policy:ImageResourcePolicy|str=ImageResourcePolicy.FASTFILE_DELAYED,
    sound_policy:str='omit',
    unresolved_image_policy:str='reference',
    unresolved_material_policy:str='reference',
    primary_light_policy:str='source',
    vertex_layer_policy:str='source',
    portal_policy:str='source',
    unsupported_fx_policy:str='omit',
    shader_compilation:str='auto',donor_catalog=None,
    zone_budget_bytes:int|None=DEFAULT_ZONE_BUDGET_BYTES,
    image_budget_min_dimension:int=DEFAULT_MIN_BASE_DIMENSION,
    texture_max_edge:int=RETAIL_PS3_MAX_TEXTURE_EDGE,
)->dict:
    output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True)
    if load_baseline is not None and pc_load_ff is not None:
        raise ValueError('choose either legacy --load-baseline or fresh --pc-load-ff, not both')
    if retail_load_reference is not None and pc_load_ff is None:
        raise ValueError('--ps3-load-reference requires --pc-load-ff')
    if isinstance(gfxworld_resource_policy,str):gfxworld_resource_policy=GfxWorldResourcePolicy(gfxworld_resource_policy)
    if isinstance(material_image_resource_policy,str):material_image_resource_policy=ImageResourcePolicy(material_image_resource_policy)
    if material_image_resource_policy is ImageResourcePolicy.RETAIL_DELAYED:
        raise ValueError(
            'retail-delayed Material images are metadata-only and have no generated PS3 payload '
            'container; use fastfile-delayed or balanced-split'
        )
    if boot_isolation is None:boot_isolation=False
    assembly=analyze_and_assemble(
        pc_ff,map_name,iwd_paths,texture_library_directory=texture_library_directory,ffmpeg=ffmpeg,runtime_compatible=runtime_compatible,
        boot_isolation=boot_isolation,gfxworld_resource_policy=gfxworld_resource_policy,
        material_image_resource_policy=material_image_resource_policy,
        sound_policy=sound_policy,
        unresolved_image_policy=unresolved_image_policy,
        unresolved_material_policy=unresolved_material_policy,
        primary_light_policy=primary_light_policy,
        vertex_layer_policy=vertex_layer_policy,
        portal_policy=portal_policy,check_output_support=True,structure_report_path=output_dir/f'{map_name}.pc-structure.json',unsupported_fx_policy=unsupported_fx_policy,
        shader_compilation=shader_compilation,donor_catalog=donor_catalog,
    )
    from .visual_diagnostics import source_pixel_coverage
    visual_coverage = source_pixel_coverage(assembly.source)
    visual_path = output_dir / f'{map_name}.source-pixels.json'
    visual_path.write_text(json.dumps(visual_coverage, indent=2), encoding='utf-8')
    # The Material/IWD image nodes are the only payloads the port can legitimately
    # resize, and both the budget stage and the balanced-split stage need the same
    # inventory plus the same "shells present, payloads omitted" baseline.
    source_iwd_names={norm(entry.image.name).casefold() for entry in assembly.source.iwd_images.values()}
    active_iwd_images=[
        node for node in assembly.plan.serialized_assets()
        if isinstance(node,ImageNode) and norm(node.image.name).casefold() in source_iwd_names
    ]
    active_names={norm(node.image.name).casefold() for node in active_iwd_images}
    if source_iwd_names and active_names!=source_iwd_names and not boot_isolation:
        # --boot-isolation deliberately replaces the whole visual graph with shared
        # defaults, so it owns no IWD images at all.  The routing contract applies to
        # real ports, not to that diagnostic mode.
        missing=sorted(source_iwd_names-active_names)
        raise ValueError(f'image routing does not cover the IWD inventory: {missing[:20]}')

    def _measure_fixed_blocks()->tuple[int,...]:
        saved=[node.resource_policy for node in active_iwd_images]
        for node in active_iwd_images:node.resource_policy=ImageResourcePolicy.RETAIL_DELAYED
        try:return assembly.plan.measure_block_sizes(assembly.source.asset_list)
        finally:
            for node,policy in zip(active_iwd_images,saved):node.resource_policy=policy

    baseline_blocks=None
    zone_budget_plan=None
    budget_dropped_levels:dict[str,int]={}
    if zone_budget_bytes:
        # A PS3 cannot satisfy an arbitrarily large DB_AllocXZoneMemory request, and IW3 does
        # not reject an oversized zone cleanly - the loading screen simply stops.  Measure the
        # allocation the map needs without its IWD pixels, then shrink the largest textures by
        # whole mip levels until the projected zone fits the Retail-derived budget.  Retained
        # levels stay byte-identical to the PC source; nothing is omitted or re-encoded.
        baseline_blocks=_measure_fixed_blocks()
        # Images the caller proved against a Retail PS3 installation keep their exact
        # source payload, otherwise the supplied face hashes would stop matching.
        protected_symbols={
            node.symbol for node in active_iwd_images
            if node.image.is_cubemap and norm(node.image.name).casefold() in {
                norm(str(k)).casefold() for k in (cubemap_target_face_hashes or {})
            }
        }
        zone_budget_plan=plan_image_budget(
            {node.symbol:node.image for node in active_iwd_images},
            fixed_bytes=sum(baseline_blocks),
            budget_bytes=int(zone_budget_bytes),
            min_base_dimension=int(image_budget_min_dimension),
            max_edge=int(texture_max_edge),
            protected=protected_symbols,
        )
        for node in active_iwd_images:
            levels=int(zone_budget_plan.dropped_levels.get(node.symbol,0))
            if levels:
                node.image=downscale_image(node.image,levels)
                budget_dropped_levels[norm(node.image.name).casefold()]=levels
        if not zone_budget_plan.within_budget:
            raise ZoneBudgetExceeded(
                'the converted map cannot be brought under the PS3 zone budget of '
                f'{int(zone_budget_bytes)} bytes: the projected allocation is '
                f'{zone_budget_plan.projected_zone_bytes} bytes and '
                f'{zone_budget_plan.fixed_bytes} bytes of it are non-image geometry/metadata. '
                'Reduce the source map, lower --image-budget-min-dimension, or raise '
                '--ps3-zone-budget only with a measured Retail PS3 zone that supports it.'
            )
        # Every later stage measures the downscaled inventory.
        baseline_blocks=None

    resource_balance_plan=None
    if material_image_resource_policy is ImageResourcePolicy.BALANCED_SPLIT:
        # The material graph can only estimate an even split before the rest of the map has
        # been laid out.  Measure the exact fixed B3/B6 high-water marks with the IWD payloads
        # temporarily omitted, then assign every complete image to the currently smaller block.
        # No image, face or mip is divided or discarded.
        if baseline_blocks is None:
            baseline_blocks=_measure_fixed_blocks()
        assignments,balanced_totals=balanced_resource_policies(
            {node.symbol:len(node.image.resource) for node in active_iwd_images},
            base_bytes=(baseline_blocks[3],baseline_blocks[6]),
        )
        for node in active_iwd_images:
            node.resource_policy=assignments[node.symbol]
        block3_nodes=[node for node in active_iwd_images if node.resource_policy is ImageResourcePolicy.FASTFILE_DELAYED]
        block6_nodes=[node for node in active_iwd_images if node.resource_policy is ImageResourcePolicy.SELF_CONTAINED]
        resource_balance_plan={
            'policy':ImageResourcePolicy.BALANCED_SPLIT.value,
            'algorithm':'deterministic-largest-first-minimax-whole-images',
            'base_block3_bytes':baseline_blocks[3],'base_block6_bytes':baseline_blocks[6],
            'material_block3_images':len(block3_nodes),'material_block6_images':len(block6_nodes),
            'material_block3_bytes':sum(len(node.image.resource) for node in block3_nodes),
            'material_block6_bytes':sum(len(node.image.resource) for node in block6_nodes),
            'material_total_images':len(active_iwd_images),
            'material_total_bytes':sum(len(node.image.resource) for node in active_iwd_images),
            'predicted_block3_bytes':balanced_totals[0],
            'predicted_block6_bytes':balanced_totals[1],
            'complete':len(active_iwd_images)==len(source_iwd_names),
        }
    build=assembly.plan.build(assembly.source.asset_list)
    if resource_balance_plan is not None:
        resource_balance_plan.update({
            'actual_block3_bytes':build.block_sizes[3],
            'actual_block6_bytes':build.block_sizes[6],
            'largest_actual_image_block_bytes':max(build.block_sizes[3],build.block_sizes[6]),
            'actual_block_difference_bytes':abs(build.block_sizes[3]-build.block_sizes[6]),
        })
    gfx_results=[v for k,v in build.graph_results.items() if k.startswith('gfxworld:') and isinstance(v,dict)]
    if len(gfx_results)!=1:
        raise ValueError(f'expected one GfxWorld graph result, got {len(gfx_results)}')
    gfx_result=gfx_results[0]
    image_stats=dict(gfx_result.get('image_resource_stats',{}))
    image_entries=list(image_stats.get('entries',()))
    shell_checks=[]
    for entry in image_entries:
        off=int(entry['shell_physical'])
        if off<0 or off+0x34>len(build.serialized_zone):
            raise ValueError(f"GfxWorld image shell outside serialized zone: {entry.get('label')} @ 0x{off:X}")
        declared=int.from_bytes(build.serialized_zone[off+0x20:off+0x24],'big')
        data_marker=int.from_bytes(build.serialized_zone[off+0x2c:off+0x30],'big')
        name_marker=int.from_bytes(build.serialized_zone[off+0x30:off+0x34],'big')
        delay=int(build.serialized_zone[off+0x2b])
        expected_data_marker=0xFFFFFFFF if declared else 0
        shell_checks.append({
            'label':entry.get('label'),'name':entry.get('name'),'shell_physical':off,
            'declared_resource_bytes':declared,'delay_load_pixels':delay,
            'data_marker':f'0x{data_marker:08X}','name_marker':f'0x{name_marker:08X}',
            'embedded':bool(entry.get('embedded')),
            'passed':declared==int(entry.get('declared_resource_bytes',-1)) and data_marker==expected_data_marker and name_marker==0xFFFFFFFF,
        })
    gfx_resource_validation={
        'policy':gfxworld_resource_policy.value,
        'passed':bool(image_entries) and all(x['passed'] for x in shell_checks),
        'image_count':len(image_entries),
        'all_delay_load_pixels_one':bool(image_entries) and all(x['delay_load_pixels']==1 for x in shell_checks),
        'all_resources_omitted':bool(image_entries) and all(not bool(x['embedded']) for x in image_entries),
        'declared_resource_bytes':image_stats.get('declared_resource_bytes',0),
        'embedded_resource_bytes':image_stats.get('embedded_resource_bytes',0),
        'deferred_resource_bytes':image_stats.get('deferred_resource_bytes',0),
        'omitted_resource_bytes':image_stats.get('omitted_resource_bytes',0),
        'shell_checks':shell_checks,
    }
    gfx_resource_validation['complete']=bool(
        int(gfx_resource_validation['omitted_resource_bytes'])==0
        and int(gfx_resource_validation['embedded_resource_bytes'])+int(gfx_resource_validation['deferred_resource_bytes'])
            ==int(gfx_resource_validation['declared_resource_bytes'])
    )
    if gfxworld_resource_policy is GfxWorldResourcePolicy.RETAIL_DELAYED:
        gfx_resource_validation['passed']=bool(gfx_resource_validation['passed'] and gfx_resource_validation['all_delay_load_pixels_one'] and gfx_resource_validation['all_resources_omitted'] and int(gfx_resource_validation['embedded_resource_bytes'])==0)
    elif gfxworld_resource_policy is GfxWorldResourcePolicy.FASTFILE_DELAYED:
        fifo=sorted(image_entries,key=lambda row:int(row.get('fifo_index',-1)))
        subset_valid=bool(fifo)
        for row in fifo:
            start=int(row.get('resource_physical',-1));size=int(row.get('declared_resource_bytes',0))
            if start<0 or start+size>len(build.serialized_zone):subset_valid=False;break
            if int(row.get('resource_logical_block',-1))!=3:subset_valid=False;break
            if hashlib.sha256(build.serialized_zone[start:start+size]).hexdigest().upper()!=row.get('resource_sha256'):subset_valid=False;break
        # Material/Image nodes can be registered between GfxWorld images while recursively
        # serializing owner graphs, so this family is a non-contiguous subset of the global FIFO.
        # Membership/hash are checked here; global order/contiguity is checked below.
        gfx_resource_validation.update({
            'fifo_count':len(fifo),'fifo_subset_valid':subset_valid,
            'fifo_index_min':min((int(row.get('fifo_index',-1)) for row in fifo),default=None),
            'fifo_index_max':max((int(row.get('fifo_index',-1)) for row in fifo),default=None),
        })
        gfx_resource_validation['passed']=bool(gfx_resource_validation['passed'] and gfx_resource_validation['all_delay_load_pixels_one'] and subset_valid and gfx_resource_validation['complete'])

    material_image_entries=[];material_shell_checks=[]
    for key,value in build.graph_results.items():
        if not key.startswith('image:') or not isinstance(value,dict):continue
        entry=dict(value);entry['result_key']=key;material_image_entries.append(entry)
        off=int(entry['root_physical']);declared=int(entry.get('declared_resource_bytes',entry.get('resource_bytes',0)) or 0)
        if off<0 or off+0x34>len(build.serialized_zone):
            material_shell_checks.append({'result_key':key,'passed':False,'error':'root outside serialized zone'});continue
        shell_declared=int.from_bytes(build.serialized_zone[off+0x20:off+0x24],'big')
        data_marker=int.from_bytes(build.serialized_zone[off+0x2c:off+0x30],'big')
        name_marker=int.from_bytes(build.serialized_zone[off+0x30:off+0x34],'big')
        delay=int(build.serialized_zone[off+0x2b]);effective=entry.get('resource_policy')
        expected_delay=1 if declared and effective in (ImageResourcePolicy.RETAIL_DELAYED.value,ImageResourcePolicy.FASTFILE_DELAYED.value) else 0
        expected_embedded=bool(declared and effective==ImageResourcePolicy.SELF_CONTAINED.value)
        material_shell_checks.append({
            'result_key':key,'name':entry.get('name'),'root_physical':off,'resource_policy':effective,
            'declared_resource_bytes':shell_declared,'delay_load_pixels':delay,
            'data_marker':f'0x{data_marker:08X}','name_marker':f'0x{name_marker:08X}',
            'embedded':bool(entry.get('embedded')),
            'passed':shell_declared==declared and data_marker==(0xFFFFFFFF if declared else 0) and name_marker==0xFFFFFFFF and delay==expected_delay and bool(entry.get('embedded'))==expected_embedded,
        })
    material_declared=sum(int(row.get('declared_resource_bytes',0) or 0) for row in material_image_entries)
    material_embedded=sum(int(row.get('embedded_resource_bytes',0) or 0) for row in material_image_entries)
    material_omitted=sum(int(row.get('omitted_resource_bytes',0) or 0) for row in material_image_entries)
    material_deferred=sum(int(row.get('deferred_resource_bytes',0) or 0) for row in material_image_entries)
    material_image_validation={
        'requested_policy':material_image_resource_policy.value,
        'passed':all(row.get('passed',False) for row in material_shell_checks),
        'image_count':len(material_image_entries),'declared_resource_bytes':material_declared,
        'embedded_resource_bytes':material_embedded,'deferred_resource_bytes':material_deferred,'omitted_resource_bytes':material_omitted,
        'effective_policy_counts':{
            policy:sum(row.get('resource_policy')==policy for row in material_image_entries)
            for policy in (ImageResourcePolicy.SELF_CONTAINED.value,ImageResourcePolicy.RETAIL_DELAYED.value,ImageResourcePolicy.FASTFILE_DELAYED.value)
        },
        'shell_checks':material_shell_checks,
    }
    material_image_validation['complete']=bool(
        material_omitted==0 and material_embedded+material_deferred==material_declared
    )
    material_image_validation['passed']=bool(
        material_image_validation['passed'] and material_image_validation['complete']
    )

    # The delayed pixel stream is one writer-global FIFO shared by GfxWorld and Material/Image
    # nodes.  Validate it once across all families so per-family validators neither claim the
    # other's bytes nor miss a gap, reorder, truncation, or payload substitution.
    global_deferred_rows=[]
    global_deferred_rows.extend(dict(row,resource_family='gfxworld') for row in image_entries if row.get('deferred'))
    global_deferred_rows.extend(dict(row,resource_family='material') for row in material_image_entries if row.get('deferred'))
    global_deferred_rows.sort(key=lambda row:int(row.get('fifo_index',-1)))
    deferred_failures=[]
    for row in image_entries:
        if int(row.get('declared_resource_bytes',0) or 0)>0 and not bool(row.get('deferred')):
            deferred_failures.append({
                'family':'gfxworld','name':row.get('name') or row.get('label'),
                'reasons':['delay=1/FOLLOWING shell has no generated deferred payload'],
            })
    for row in material_image_entries:
        if (
            int(row.get('declared_resource_bytes',0) or 0)>0
            and int(row.get('delay_load_pixels',0) or 0)==1
            and not bool(row.get('deferred'))
        ):
            deferred_failures.append({
                'family':'material','name':row.get('name'),
                'reasons':['delay=1/FOLLOWING shell has no generated deferred payload'],
            })
    expected_physical=None;expected_logical=0
    for expected_index,row in enumerate(global_deferred_rows):
        index=int(row.get('fifo_index',-1));start=int(row.get('resource_physical',-1));size=int(row.get('declared_resource_bytes',0) or 0)
        logical_block=int(row.get('resource_logical_block',-1));logical_offset=int(row.get('resource_logical_offset',-1))
        expected_hash=str(row.get('resource_sha256') or row.get('sha256') or '').upper()
        reasons=[]
        if index!=expected_index:reasons.append(f'fifo index {index} != {expected_index}')
        if start<0 or size<=0 or start+size>len(build.serialized_zone):reasons.append('physical range outside zone')
        if expected_physical is not None and start!=expected_physical:reasons.append(f'physical gap/reorder at {start}, expected {expected_physical}')
        if logical_block!=3 or logical_offset!=expected_logical:reasons.append(f'logical block/offset {logical_block}:{logical_offset} != 3:{expected_logical}')
        if not reasons and hashlib.sha256(build.serialized_zone[start:start+size]).hexdigest().upper()!=expected_hash:reasons.append('SHA256 mismatch')
        if reasons:deferred_failures.append({'fifo_index':index,'family':row.get('resource_family'),'name':row.get('name') or row.get('label'),'reasons':reasons})
        expected_physical=start+size;expected_logical=logical_offset+size
    global_deferred_validation={
        'passed':not deferred_failures and declared_block_size(expected_logical)==build.block_sizes[3] and (not global_deferred_rows or expected_physical==len(build.serialized_zone)),
        'fifo_count':len(global_deferred_rows),'block':3,'block_bytes':build.block_sizes[3],
        'declared_bytes':sum(int(row.get('declared_resource_bytes',0) or 0) for row in global_deferred_rows),
        'physical_tail_exact':bool(global_deferred_rows and expected_physical==len(build.serialized_zone)),
        'failures':deferred_failures,
    }
    # Verify retained dimensions and mip levels against the parsed IWD inventory.
    # The destination rows are generated by ImageNode after conversion, so this comparison is
    # independent of Material ownership and also covers supplemental FF-only image XAssets.
    source_iwd_by_name={norm(entry.image.name).casefold():entry.image for entry in assembly.source.iwd_images.values()}
    destination_iwd_by_name={norm(str(row.get('name',''))).casefold():row for row in material_image_entries}
    image_fidelity_mismatches=[]
    for name,source in sorted(source_iwd_by_name.items()):
        row=destination_iwd_by_name.get(name)
        if row is None:
            image_fidelity_mismatches.append({'name':source.name,'reason':'missing destination Image XAsset'})
            continue
        # A budgeted build removes whole top mip levels.  Every level the destination
        # still carries must remain byte-identical to the PC source level it came
        # from, so the expectation is the source chain shifted by the drop count -
        # never a re-encoded or substituted payload.
        dropped=int(budget_dropped_levels.get(name,0))
        from .image_fidelity import expected_mips
        source_mips=expected_mips(source,dropped)
        destination_mips=tuple(
            (int(m['face']),int(m['level']),int(m['width']),int(m['height']),int(m['bytes']),str(m['sha256']).upper())
            for m in row.get('mips',())
        )
        base=next((m for m in sorted(source.mips,key=lambda x:(x.face,x.level)) if m.face==0 and m.level==dropped),None)
        if dropped and base is None:
            image_fidelity_mismatches.append({'name':source.name,'reason':'budget drop count exceeds the source mip chain'})
            continue
        expected_width=int(base.width) if dropped else int(source.width)
        expected_height=int(base.height) if dropped else int(source.height)
        geometry=(int(row.get('width',-1)),int(row.get('height',-1)),int(row.get('depth',-1)),int(row.get('level_count',-1)),int(row.get('face_count',-1)))
        expected_geometry=(expected_width,expected_height,int(source.depth),int(source.level_count)-dropped,int(source.face_count))
        if geometry!=expected_geometry or destination_mips!=source_mips:
            image_fidelity_mismatches.append({
                'name':source.name,'reason':'geometry/mip/hash mismatch','budget_dropped_levels':dropped,
                'source_geometry':expected_geometry,'destination_geometry':geometry,
                'source_mips':len(source_mips),'destination_mips':len(destination_mips),
            })
    image_fidelity_validation={
        'passed':not image_fidelity_mismatches,
        'source_iwd_images':len(source_iwd_by_name),
        'destination_iwd_images':sum(name in destination_iwd_by_name for name in source_iwd_by_name),
        'max_source_width':max((int(x.width) for x in source_iwd_by_name.values()),default=0),
        'max_source_height':max((int(x.height) for x in source_iwd_by_name.values()),default=0),
        'total_source_mips':sum(len(x.mips) for x in source_iwd_by_name.values()),
        'budget_scaled_images':len(budget_dropped_levels),
        'retained_levels_byte_exact':not image_fidelity_mismatches and not any(x.format_name in ('rgb8','rgba8') for x in source_iwd_by_name.values()),
        'retained_levels_pixel_exact':not image_fidelity_mismatches,
        'lossless_bitmap_repacking':sum(x.format_name in ('rgb8','rgba8') for x in source_iwd_by_name.values()),
        'decoded_wavelet_images':sum(getattr(x,'encoded_format_id',None) in range(6,11) for x in source_iwd_by_name.values()),
        'mismatches':image_fidelity_mismatches,
    }
    zone_allocation=check_zone_allocation(
        build.block_sizes,
        budget_bytes=int(zone_budget_bytes) if zone_budget_bytes else DEFAULT_ZONE_BUDGET_BYTES,
    )
    zone_allocation['enforced']=bool(zone_budget_bytes)
    if zone_budget_bytes and not zone_allocation['within_budget']:
        raise ZoneBudgetExceeded(
            f"the saved zone declares {zone_allocation['declared_zone_bytes']} bytes across its seven "
            f"loader blocks, above the {zone_allocation['budget_bytes']}-byte PS3 budget "
            f"({zone_allocation['ratio_vs_retail_reference']}x the measured Retail PS3 reference zone). "
            f"Block sizes: {zone_allocation['block_sizes']}."
        )
    if not gfx_resource_validation['passed'] or not gfx_resource_validation['complete']:
        raise ValueError('generated GfxWorld image resources failed complete FF-stream validation')
    if not material_image_validation['passed'] or not material_image_validation['complete']:
        raise ValueError('generated Material image resources failed complete FF-stream validation')
    if not global_deferred_validation['passed']:
        raise ValueError('generated delayed image FIFO failed global FF-stream validation')
    if not image_fidelity_validation['passed'] and not boot_isolation:
        # --boot-isolation writes no IWD image payloads at all; the fidelity contract
        # is about a real port, not about this diagnostic mode.
        raise ValueError('generated images do not preserve the complete IWD dimensions/mip hashes')
    main_path=output_dir/f'{map_name}.ff';save_main_fastfile(main_path,build,verify=True)
    rb=inspect_main_fastfile(main_path,assembly.plan,build);rb_dict=asdict(rb)
    roundtrip=verify_build_roundtrip(build,assembly.plan)
    closure=build_conversion_closure(assembly,build,rb_dict)
    load_report=None;load_evidence=None
    if pc_load_ff is not None:
        load_path=output_dir/f'{map_name}_load.ff'
        load_report=convert_pc_load_to_ps3(
            pc_load_ff,iwd_paths,load_path,
            retail_load_reference=retail_load_reference,map_name=map_name,overwrite=True,
        )
        load_report=dict(load_report,mode='fresh-pc-load-conversion',pc_load_ff=str(Path(pc_load_ff).resolve()))
        ld=read_load_document(load_path)
        load_report=dict(load_report,strict_readback=True,load_materials=len(ld.materials))
        lp={'asset_type_order':['techset','material','material','material','rawfile'],'materials':[{'name':m.name} for m in ld.materials],'trailing_nonzero':0,'resource_bytes':sum(len(t.image.resource) for m in ld.materials for t in m.textures)}
        load_evidence=evidence_from_load_readback(lp,load_report.get('output_file_sha256'))
    elif load_baseline is not None:
        load_path=output_dir/f'{map_name}_load.ff';load_report=write_load_fastfile(load_baseline,load_path)
        load_report=dict(load_report,baseline=str(Path(load_baseline).resolve()))
        ld=read_load_document(load_path);load_report=dict(load_report,strict_readback=True,load_materials=len(ld.materials))
        expected_raw=f'{map_name}_load';expected_image=f'loadscreen_{map_name}'
        actual_images=[t.image.name for m in ld.materials for t in m.textures]
        if ld.rawfile.name.casefold()!=expected_raw.casefold() or expected_image.casefold() not in {x.casefold() for x in actual_images}:
            raise ValueError(
                f'legacy PS3 load baseline identity mismatch: raw={ld.rawfile.name!r}, images={actual_images!r}; '
                f'use --pc-load-ff for a fresh {map_name} load zone'
            )
        # Convert strict load document to V4 gate profile shape.
        lp={'asset_type_order':['techset','material','material','material','rawfile'],'materials':[{'name':m.name} for m in ld.materials],'trailing_nonzero':0,'resource_bytes':sum(len(t.image.resource) for m in ld.materials for t in m.textures)}
        load_evidence=evidence_from_load_readback(lp,load_report.get('sha256'))
    try:
        native_dependencies=_native_dependency_manifest(assembly)
    except Exception as exc:  # never let the manifest break an otherwise valid build
        native_dependencies={'error':str(exc),'contract':'native dependency manifest unavailable'}
    conversion_doc=_conversion_report_doc(closure)
    platform_state_validation=_platform_state_validation(assembly)
    evidence_docs=[_pc_source_evidence(assembly),evidence_from_conversion_report(conversion_doc),_ps3_readback_evidence(rb_dict,closure),_bundled_retail_evidence(assembly),_cubemap_retail_doc(assembly,cubemap_target_face_hashes)]
    if load_evidence:evidence_docs.append(load_evidence)
    gate=evaluate_full_fidelity(*evidence_docs)
    report={
      'map':map_name,'unsupported_fx':assembly.source.fx_omission.report if assembly.source.fx_omission is not None else {},
      'shader_compilation':dict(assembly.source.diagnostics.get('shader_compilation',{})),
      'material_graph':dict(assembly.material_graph.diagnostics),
      'execution_mode':'boot-isolation' if boot_isolation else ('owner-aware-visual-candidate' if runtime_compatible else 'full-port'),
      'hardware_tested':False,'runtime_compatible':runtime_compatible,'boot_isolation':bool(boot_isolation),
      'gfxworld_resource_policy':gfxworld_resource_policy.value,'material_image_resource_policy':material_image_resource_policy.value,'sound_policy':sound_policy,'pc_fastfile':str(Path(pc_ff).resolve()),'iwd_paths':[str(Path(x).resolve()) for x in iwd_paths],
      'main_output':str(main_path.resolve()),'main':{'asset_count':build.asset_count,'script_strings':build.script_string_count,'relocations':build.relocation_count,'block_sizes':build.block_sizes,'serialized_sha256':build.serialized_sha256,'retail_zone_sha256':build.retail_zone_sha256,'fastfile_sha256':build.fastfile_sha256,'asset_counts':build.asset_counts},
      'ps3_zone_allocation':zone_allocation,'ps3_zone_budget_plan':zone_budget_plan.as_dict() if zone_budget_plan is not None else None,
      'graph_placement':build.graph_results.get('graph.placement',{}),'resource_balance_plan':resource_balance_plan,'gfxworld_image_resources':image_stats,'gfxworld_resource_validation':gfx_resource_validation,'material_image_resources':material_image_entries,'material_image_resource_validation':material_image_validation,'global_deferred_resource_validation':global_deferred_validation,'image_fidelity_validation':image_fidelity_validation,'platform_state_validation':platform_state_validation,
      'material_texture_bindings':[{'name':p.source.name.lstrip(','),'textures':[
          {'index':i,'semantic':t.semantic,'name_hash':t.name_hash,'image':name.lstrip(',')}
          for i,(t,name) in enumerate(zip(p.source.textures,p.image_names))]}
          for p in assembly.source.materials.planned_owned],
      'ps3_native_dependencies':native_dependencies,'source_pixel_coverage':visual_coverage,
      'independent_readback':rb_dict,'roundtrip':roundtrip,'source_diagnostics':dict(assembly.source.diagnostics),'assembly_diagnostics':dict(assembly.diagnostics),'closure':asdict(closure),'FidelityClosure':closure.fidelity_section(),'known_profile_comparison':_known_profile_comparison(assembly,closure),'load':load_report,
      'evidence_documents':evidence_docs,'full_fidelity_gate':gate,'full_fidelity_passed':bool(not (assembly.source.fx_omission and assembly.source.fx_omission.roots) and gate['full_fidelity_passed'] and closure.passed and gfx_resource_validation['passed'] and gfx_resource_validation['complete'] and material_image_validation['passed'] and global_deferred_validation['passed'] and image_fidelity_validation['passed'] and zone_allocation['within_budget'] and platform_state_validation['passed'] and platform_state_validation['retail_exact_passed'] and (load_report is None or load_report.get('load_full_fidelity',True))),
    }
    report_path=output_dir/f'{map_name}.python-port-report.json';report_path.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    report['report_path']=str(report_path.resolve())
    # Full-fidelity closure remains a hard gate for the normal production path.
    # The explicit runtime-compatible path is a hardware-isolation mode: its known
    # Material/IWD/Water fidelity debts are preserved in the report, but must not
    # turn an otherwise readback-clean hardware candidate into a CLI exception.
    if not closure.passed and not runtime_compatible:
        raise ValueError('Python conversion closure failed after output generation: '+ '; '.join(closure.blockers[:20]))
    return report

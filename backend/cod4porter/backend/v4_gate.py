from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict
from pathlib import Path
import json

CRITERIA=[
 ('original_pc_ff_parsed','Original PC FastFile byte-parse'),
 ('iwd_inventory_exact','Original IWD/IWI/audio inventory'),
 ('source_xasset_profile_exact','Source script-string/XAsset cardinality'),
 ('source_xasset_closure','All source XAssets classified/reachable'),
 ('sound_graph_closure','Sound/SndCurve/LoadedSound/SpeakerMap closure'),
 ('sound_iwd_payload_closure','All custom IWD audio uniquely bound'),
 ('physpreset_sound_closure','PhysPreset sndAliasPrefix closure'),
 ('material_state_slots_exact','No unresolved/neutralized Material state slots'),
 ('platform_state_exact','Exact PS3 Material PlatformState coverage'),
 ('packed_image_identity_exact','All packed/unnamed GfxImage identities proven'),
 ('neutral_images_zero','No semantic-neutral image fallback'),
 ('water_exact','Native PC->PS3 water_t conversion proven'),
 ('cubemap_exact','Cubemap 6/6 face ordering proven'),
 ('technique_sets_exact','All TechniqueSets resolved to valid PS3 references'),
 ('fx_owned_exact','Owned FX graphs exact or proven native/shared'),
 ('impactfx_exact','ImpactFX references closed'),
 ('lightdef_attenuation_exact','LightDef attenuation image identity closed'),
 ('rawfiles_exact','Required RawFiles exact'),
 ('stringtable_exact','StringTables exact; no generated compatibility substitute'),
 ('xmodel_exact','XModel/XSurface streams exact'),
 ('gfxworld_exact','GfxWorld topology/material references exact'),
 ('clipmap_exact','ClipMap/PVS/BSP/collision exact'),
 ('load_zone_exact','_load.ff strict retail-profile readback'),
 ('output_xasset_closure','Destination XAsset graph fully closed'),
 ('pointer_leaks_zero','No source packed-pointer leaks in destination'),
 ('deterministic_write','Second write byte-deterministic'),
]

# A trusted source is not automatically sufficient.  Full-fidelity criteria that span
# source ownership and destination serialization require independent evidence from every
# listed provenance.  This prevents one writer/report from certifying its own assumptions.
REQUIRED_SOURCES={
 'original_pc_ff_parsed':{'pc_source_deep'},
 'iwd_inventory_exact':{'pc_source_deep'},
 'source_xasset_profile_exact':{'pc_source_deep'},
 'source_xasset_closure':{'pc_source_deep','conversion_audit'},
 'sound_graph_closure':{'pc_source_deep','conversion_audit'},
 'sound_iwd_payload_closure':{'pc_source_deep','conversion_audit'},
 'physpreset_sound_closure':{'pc_source_deep','conversion_audit'},
 'material_state_slots_exact':{'pc_source_deep','conversion_audit'},
 'platform_state_exact':{'retail_observation','conversion_audit'},
 'packed_image_identity_exact':{'pc_source_deep','conversion_audit'},
 'neutral_images_zero':{'conversion_audit'},
 'water_exact':{'retail_observation','conversion_audit'},
 'cubemap_exact':{'pc_source_deep','retail_observation','conversion_audit'},
 'technique_sets_exact':{'conversion_audit'},
 'fx_owned_exact':{'pc_source_deep','conversion_audit'},
 'impactfx_exact':{'pc_source_deep','conversion_audit'},
 'lightdef_attenuation_exact':{'pc_source_deep','conversion_audit'},
 'rawfiles_exact':{'pc_source_deep','conversion_audit'},
 'stringtable_exact':{'pc_source_deep','conversion_audit'},
 'xmodel_exact':{'conversion_audit'},
 'gfxworld_exact':{'conversion_audit'},
 'clipmap_exact':{'conversion_audit'},
 'load_zone_exact':{'ps3_readback'},
 'output_xasset_closure':{'conversion_audit'},
 'pointer_leaks_zero':{'conversion_audit'},
 'deterministic_write':{'ps3_readback'},
}

TRUSTED_SOURCES={
 'original_pc_ff_parsed':{'pc_source_deep'},
 'iwd_inventory_exact':{'pc_source_deep'},
 'source_xasset_profile_exact':{'pc_source_deep'},
 'source_xasset_closure':{'pc_source_deep','conversion_audit'},
 'sound_graph_closure':{'pc_source_deep','conversion_audit'},
 'sound_iwd_payload_closure':{'pc_source_deep','conversion_audit'},
 'physpreset_sound_closure':{'pc_source_deep','conversion_audit'},
 'material_state_slots_exact':{'pc_source_deep','conversion_audit'},
 'platform_state_exact':{'retail_observation','conversion_audit','ps3_readback'},
 'packed_image_identity_exact':{'pc_source_deep','conversion_audit'},
 'neutral_images_zero':{'conversion_audit','ps3_readback'},
 'water_exact':{'retail_observation','conversion_audit','ps3_readback'},
 'cubemap_exact':{'retail_observation','pc_source_deep','conversion_audit'},
 'technique_sets_exact':{'conversion_audit','ps3_readback'},
 'fx_owned_exact':{'pc_source_deep','conversion_audit'},
 'impactfx_exact':{'pc_source_deep','conversion_audit'},
 'lightdef_attenuation_exact':{'pc_source_deep','conversion_audit'},
 'rawfiles_exact':{'pc_source_deep','conversion_audit','ps3_readback'},
 'stringtable_exact':{'pc_source_deep','conversion_audit','ps3_readback'},
 'xmodel_exact':{'conversion_audit','ps3_readback'},
 'gfxworld_exact':{'conversion_audit','ps3_readback'},
 'clipmap_exact':{'conversion_audit','ps3_readback'},
 'load_zone_exact':{'ps3_readback'},
 'output_xasset_closure':{'conversion_audit','ps3_readback'},
 'pointer_leaks_zero':{'conversion_audit','ps3_readback'},
 'deterministic_write':{'ps3_readback'},
}


def evidence(criterion:str,passed:bool,source:str,proof:dict|None=None,detail:str='')->dict:
    if criterion not in dict(CRITERIA): raise ValueError(f'unknown criterion {criterion}')
    return {'criterion':criterion,'passed':bool(passed),'source':source,'proof':proof or {},'detail':detail}


def _flatten(docs):
    out=[]
    for d in docs:
        if not d:continue
        if isinstance(d,list): out.extend(_flatten(d)); continue
        if isinstance(d,dict) and 'evidence' in d: out.extend(d['evidence'])
        elif isinstance(d,dict) and 'criterion' in d: out.append(d)
    return out


def evaluate_full_fidelity(*docs)->dict:
    ev=_flatten(docs); by=defaultdict(list)
    rejected=[]
    for e in ev:
        c=e.get('criterion'); src=e.get('source')
        if c not in TRUSTED_SOURCES:
            rejected.append({'evidence':e,'reason':'unknown criterion'}); continue
        if src not in TRUSTED_SOURCES[c]:
            rejected.append({'evidence':e,'reason':f'untrusted source {src!r} for {c}'}); continue
        # A pass must carry a non-empty machine proof. Bare true/manual flags never count.
        if e.get('passed') and not e.get('proof'):
            rejected.append({'evidence':e,'reason':'passing evidence has no machine-readable proof'}); continue
        by[c].append(e)
    rows=[]
    for c,label in CRITERIA:
        accepted=by.get(c,[]); passing=[e for e in accepted if e.get('passed')]
        failing=[e for e in accepted if not e.get('passed')]
        # Any trusted explicit failing proof blocks even if another source says pass.
        # In addition, every independent provenance listed in REQUIRED_SOURCES must have
        # at least one passing machine proof.
        required_sources=REQUIRED_SOURCES[c]
        passing_sources={e.get('source') for e in passing}
        missing_sources=sorted(required_sources-passing_sources)
        ok=bool(passing) and not failing and not missing_sources
        rows.append({'criterion':c,'label':label,'passed':ok,'passing_evidence':passing,'failing_evidence':failing,
                     'required_sources':sorted(required_sources),'missing_sources':missing_sources,
                     'missing':not accepted or bool(missing_sources)})
    return {'full_fidelity_passed':all(r['passed'] for r in rows),'passed_count':sum(r['passed'] for r in rows),'required_count':len(rows),
            'rows':rows,'rejected_evidence':rejected,'missing':[r['criterion'] for r in rows if r['missing']],
            'failed':[r['criterion'] for r in rows if not r['passed']]}


def evidence_from_pc_source(report:dict,expected:dict|None=None)->dict:
    ev=[]
    ev.append(evidence('original_pc_ff_parsed',bool(report.get('source_byte_evidence')),'pc_source_deep',
                       {'fastfile_sha256':report.get('fastfile',{}).get('file_sha256'),'zone_sha256':report.get('fastfile',{}).get('zone_sha256')}))
    iwd=report.get('iwd',{}); paths=iwd.get('paths',[])
    expected=expected or {}
    iwd_ok=bool(paths)
    if 'iwi' in expected:iwd_ok=iwd_ok and iwd.get('images')==expected['iwi']
    if 'audio' in expected:iwd_ok=iwd_ok and iwd.get('audio')==expected['audio']
    ev.append(evidence('iwd_inventory_exact',iwd_ok,'pc_source_deep',{'paths':paths,'images':iwd.get('images'),'audio':iwd.get('audio'),'image_formats':iwd.get('image_formats')}))
    profile_ok=True
    if 'script_strings' in expected: profile_ok &= report.get('script_strings')==expected['script_strings']
    if 'xassets' in expected: profile_ok &= report.get('xassets')==expected['xassets']
    ev.append(evidence('source_xasset_profile_exact',profile_ok,'pc_source_deep',{'script_strings':report.get('script_strings'),'xassets':report.get('xassets'),'asset_counts':report.get('asset_counts')}))

    fam=report.get('asset_family_scan') or {}
    if fam:
        raw=fam.get('rawfiles',{}); declared=raw.get('declared'); candidates=raw.get('candidates')
        if declared is not None and candidates is not None:
            ev.append(evidence('rawfiles_exact',candidates==declared,'pc_source_deep',{'declared':declared,'structural_candidates':candidates,'records':raw.get('records',[])}))
        st=fam.get('stringtables',{}); declared=st.get('declared'); candidates=st.get('candidates')
        if declared is not None and candidates is not None:
            ev.append(evidence('stringtable_exact',candidates==declared,'pc_source_deep',{'declared':declared,'structural_candidates':candidates,'records':st.get('records',[])}))
        imp=fam.get('impactfx',{})
        if 'exact' in imp:
            ev.append(evidence('impactfx_exact',bool(imp.get('exact')),'pc_source_deep',{'declared':imp.get('declared'),'candidates':imp.get('candidates'),'records':imp.get('records',[])}))
        fx=fam.get('fx',{})
        if 'exact' in fx:
            ev.append(evidence('fx_owned_exact',bool(fx.get('exact')),'pc_source_deep',{'declared':fx.get('declared'),'candidates':fx.get('candidates'),'alias_shell_candidates':fx.get('alias_shell_candidates'),'owned_graph_candidates':fx.get('owned_graph_candidates'),'records':fx.get('records',[])}))
        phys=fam.get('physpreset',{})
        if phys.get('exact') and not phys.get('packed_sound_requests'):
            ev.append(evidence('physpreset_sound_closure',True,'pc_source_deep',{'declared':phys.get('declared'),'candidates':phys.get('candidates'),'packed_sound_requests':[]}))
        ld=fam.get('lightdef',{})
        if 'exact' in ld:
            records=ld.get('records',[])
            unresolved_packed=0
            for r in records:
                rawp=r.get('image_pointer',0)
                # PC packed attenuation references need a separate exact identity proof.
                if isinstance(rawp,int) and rawp not in (0,0xFFFFFFFF,0xFFFFFFFE):
                    unresolved_packed+=1
            ok=bool(ld.get('exact')) and unresolved_packed==0
            ev.append(evidence('lightdef_attenuation_exact',ok,'pc_source_deep',{'declared':ld.get('declared'),'candidates':ld.get('candidates'),'unresolved_packed_attenuation':unresolved_packed,'records':records}))
    return {'source':'pc_source_deep','evidence':ev}


def evidence_from_load_readback(profile:dict,fastfile_sha256:str|None=None)->dict:
    mats=profile.get('materials',[])
    names=[m.get('name') if isinstance(m,dict) else getattr(m,'name',None) for m in mats]
    ok=profile.get('asset_type_order')==['techset','material','material','material','rawfile'] and names==['$victorybackdrop',',$defeatbackdrop','$levelbriefing'] and profile.get('trailing_nonzero',0)==0
    return {'source':'ps3_readback','evidence':[evidence('load_zone_exact',ok,'ps3_readback',{'fastfile_sha256':fastfile_sha256,'asset_order':profile.get('asset_type_order'),'materials':names,'resource_bytes':profile.get('resource_bytes'),'trailing_nonzero':profile.get('trailing_nonzero')})]}


def evidence_from_conversion_report(report:dict)->dict:
    """Translate an explicit FIX114 FidelityClosure section. It may only *report* machine counts; no free-form pass flags."""
    fc=report.get('FidelityClosure') or report.get('fidelityClosure') or {}
    ev=[]
    mappings={
      'source_xasset_closure':('SourceXAssetCount','ClassifiedSourceXAssetCount'),
      'sound_graph_closure':('SourceSoundXAssetCount','EmittedSoundXAssetCount'),
      'sound_iwd_payload_closure':('SourceCustomAudioPayloadCount','BoundCustomAudioPayloadCount'),
      'physpreset_sound_closure':('SourcePhysPresetSoundAliasReferenceCount','ResolvedPhysPresetSoundAliasReferenceCount'),
      'packed_image_identity_exact':('PackedImageReferenceCount','ResolvedPackedImageReferenceCount'),
      'fx_owned_exact':('OwnedFxCount','ExactOwnedFxCount'),
      'impactfx_exact':('ImpactFxReferenceCount','ResolvedImpactFxReferenceCount'),
      'lightdef_attenuation_exact':('LightDefAttenuationReferenceCount','ResolvedLightDefAttenuationReferenceCount'),
      'rawfiles_exact':('RequiredRawFileCount','ExactRawFileCount'),
      'stringtable_exact':('RequiredStringTableCount','ExactStringTableCount'),
    }
    for crit,(src,dst) in mappings.items():
        if src in fc and dst in fc:
            ev.append(evidence(crit,fc[src]==fc[dst],'conversion_audit',{'source_count':fc[src],'output_count':fc[dst],'fields':[src,dst]}))
    if all(key in fc for key in ('SourceFxVisualReferenceCount','ResolvedExactFxVisualReferenceCount','RuntimeFxVisualFallbackCount')):
        ev.append(evidence(
            'fx_owned_exact',
            fc['SourceFxVisualReferenceCount']==fc['ResolvedExactFxVisualReferenceCount'] and fc['RuntimeFxVisualFallbackCount']==0,
            'conversion_audit',
            {
                'source_visual_references':fc['SourceFxVisualReferenceCount'],
                'resolved_exact_visual_references':fc['ResolvedExactFxVisualReferenceCount'],
                'runtime_visual_fallbacks':fc['RuntimeFxVisualFallbackCount'],
            },
        ))
    zeros={
      'material_state_slots_exact':'UnresolvedMaterialStateSlotCount',
      'neutral_images_zero':'NeutralImageFallbackCount',
      'pointer_leaks_zero':'PackedSourcePointerLeakCount',
      'platform_state_exact':'UnresolvedPlatformStateCount',
      'water_exact':'WaterFallbackCount',
      'cubemap_exact':'UnprovenCubemapCount',
      'technique_sets_exact':'UnsupportedCustomTechniqueSetCount',
      'output_xasset_closure':'UnresolvedOutputXAssetReferenceCount',
    }
    for crit,key in zeros.items():
        if key in fc: ev.append(evidence(crit,fc[key]==0,'conversion_audit',{key:fc[key]}))
    closures={
      'xmodel_exact':('XModelSourceVertexCount','XModelOutputVertexCount','XModelSourceTriangleCount','XModelOutputTriangleCount'),
      'gfxworld_exact':('GfxWorldSourceSurfaceCount','GfxWorldOutputSurfaceCount','GfxWorldSourceVertexCount','GfxWorldOutputVertexCount'),
      'clipmap_exact':('ClipMapSourceBrushCount','ClipMapOutputBrushCount','ClipMapSourceTriangleCount','ClipMapOutputTriangleCount'),
    }
    for crit,keys in closures.items():
        if all(k in fc for k in keys):
            ok=fc[keys[0]]==fc[keys[1]] and fc[keys[2]]==fc[keys[3]]
            ev.append(evidence(crit,ok,'conversion_audit',{k:fc[k] for k in keys}))
    return {'source':'conversion_audit','evidence':ev}

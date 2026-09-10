from cod4porter.backend.v4_gate import evidence, evidence_from_conversion_report, evaluate_full_fidelity

fc={
 'SourceXAssetCount':10,'ClassifiedSourceXAssetCount':10,
 'SourceSoundXAssetCount':2,'EmittedSoundXAssetCount':2,
 'SourceCustomAudioPayloadCount':3,'BoundCustomAudioPayloadCount':3,
 'SourcePhysPresetSoundAliasReferenceCount':4,'ResolvedPhysPresetSoundAliasReferenceCount':4,
 'PackedImageReferenceCount':5,'ResolvedPackedImageReferenceCount':5,
 'OwnedFxCount':6,'ExactOwnedFxCount':6,
 'ImpactFxReferenceCount':0,'ResolvedImpactFxReferenceCount':0,
 'LightDefAttenuationReferenceCount':1,'ResolvedLightDefAttenuationReferenceCount':1,
 'RequiredRawFileCount':7,'ExactRawFileCount':7,
 'RequiredStringTableCount':1,'ExactStringTableCount':1,
 'UnresolvedMaterialStateSlotCount':0,'NeutralImageFallbackCount':0,'PackedSourcePointerLeakCount':0,
 'UnresolvedPlatformStateCount':0,'WaterFallbackCount':0,'UnprovenCubemapCount':0,
 'UnsupportedCustomTechniqueSetCount':0,'UnresolvedOutputXAssetReferenceCount':0,
 'XModelSourceVertexCount':100,'XModelOutputVertexCount':100,'XModelSourceTriangleCount':50,'XModelOutputTriangleCount':50,
 'GfxWorldSourceSurfaceCount':20,'GfxWorldOutputSurfaceCount':20,'GfxWorldSourceVertexCount':200,'GfxWorldOutputVertexCount':200,
 'ClipMapSourceBrushCount':30,'ClipMapOutputBrushCount':30,'ClipMapSourceTriangleCount':40,'ClipMapOutputTriangleCount':40,
}
conv=evidence_from_conversion_report({'FidelityClosure':fc})
assert len(conv['evidence'])==21, len(conv['evidence'])
assert all(e['passed'] and e['proof'] for e in conv['evidence'])

def ev(c,src,**proof): return evidence(c,True,src,proof or {'machine':True})
source={'source':'pc_source_deep','evidence':[
 ev('original_pc_ff_parsed','pc_source_deep',file_sha256='A'*64,zone_sha256='B'*64),
 ev('iwd_inventory_exact','pc_source_deep',images=1,audio=3),
 ev('source_xasset_profile_exact','pc_source_deep',xassets=10,script_strings=2),
 ev('source_xasset_closure','pc_source_deep',source=10,classified=10),
 ev('sound_graph_closure','pc_source_deep',declared=2,parsed=2),
 ev('sound_iwd_payload_closure','pc_source_deep',audio=3,boundable=3),
 ev('physpreset_sound_closure','pc_source_deep',references=4,unresolved=0),
 ev('material_state_slots_exact','pc_source_deep',bad_materials=0),
 ev('packed_image_identity_exact','pc_source_deep',requests=5,unresolved=0),
 ev('cubemap_exact','pc_source_deep',cubemaps=1,source_faces=6),
 ev('fx_owned_exact','pc_source_deep',owned=6,parsed=6),
 ev('impactfx_exact','pc_source_deep',declared=0,parsed=0),
 ev('lightdef_attenuation_exact','pc_source_deep',declared=1,resolved=1),
 ev('rawfiles_exact','pc_source_deep',declared=7,parsed=7),
 ev('stringtable_exact','pc_source_deep',declared=1,parsed=1),
]}
retail={'source':'retail_observation','evidence':[
 ev('platform_state_exact','retail_observation',catalog_sha256='C'*64,required=8,resolved=8),
 ev('water_exact','retail_observation',pc_root_size=0x44,ps3_root_size=0x48,extra_pointer=0xffffffff),
 ev('cubemap_exact','retail_observation',mapping={0:0,1:1,2:2,3:3,4:4,5:5},target_faces=6),
]}
ps3={'source':'ps3_readback','evidence':[
 ev('load_zone_exact','ps3_readback',asset_count=5,trailing_nonzero=0),
 ev('deterministic_write','ps3_readback',write1='D'*64,write2='D'*64),
]}
full=evaluate_full_fidelity(source,conv,retail,ps3)
assert full['full_fidelity_passed'], full['failed']
assert full['passed_count']==26 and full['required_count']==26

# A source cubemap plus conversion claim is deliberately insufficient without an independent retail 6-face proof.
retail_without_cube={'source':'retail_observation','evidence':retail['evidence'][:2]}
no_cube=evaluate_full_fidelity(source,conv,retail_without_cube,ps3)
assert not no_cube['full_fidelity_passed']
assert 'cubemap_exact' in no_cube['failed']
row=next(r for r in no_cube['rows'] if r['criterion']=='cubemap_exact')
assert row['missing_sources']==['retail_observation']

# Bare/manual PASS must never certify a criterion.
bare=evaluate_full_fidelity(evidence('water_exact',True,'retail_observation',{}))
assert not bare['full_fidelity_passed'] and bare['rejected_evidence']

# A trusted negative proof dominates a positive proof.
negative=evaluate_full_fidelity(source,conv,retail,ps3,evidence('water_exact',False,'retail_observation',{'mismatch':True}))
assert not negative['full_fidelity_passed'] and 'water_exact' in negative['failed']
print('test_full_fidelity_gate PASS')

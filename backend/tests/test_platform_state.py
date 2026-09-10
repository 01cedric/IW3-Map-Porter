from cod4porter.material import *
from cod4porter.platform_state import *

def rec(**kw):
    d=dict(root_offset=0,name='x',game_flags=0,sort_key=0,atlas_rows=0,atlas_cols=0,draw_surf=0,surface_type_bits=0,hash_index=0,state_bits_entry=bytes([255]*34),declared_state_bits_count=0,serialized_state_bits_pointer=0,state_bits_resolution_kind='None',texture_count=0,constant_count=0,state_flags=0,camera_region=0,technique_set_pointer=0,textures=(),constants=(),state_bits=(),water_by_texture={})
    d.update(kw);return MaterialRecord(**d)
r=rec()
s=MaterialStateConversion(bytes(26),(),0,0,0)
assert exact_signature_count()==94
assert len(catalog_sha256())==64
# Known catalog signature 0|0|0|0|0|0|0|zeros|0
sig=build_signature(r,s);assert contains_signature(sig)
b=resolve_exact(r,s);assert len(b)==0x38 and b==bytes(0x38)
assert matches_exact(sig,b) and not matches_exact(sig,bytes([1])+b[1:])
# Surface/impact classification and hash-table placement are deliberately excluded from the
# narrower structural family projection.  The target is still labelled observed, never exact.
rstruct=rec(hash_index=123,surface_type_bits=456)
bs,mode=resolve_runtime_compatible(rstruct,s)
assert bs==b and mode=='observed_structural_family'
audit=runtime_resolution_audit(rstruct,s)
assert audit['tier']=='hardware_observed_family'
assert audit['evidence_scope']=='structural_signature_without_hash_or_surface'
assert audit['catalog_observations']>=1
assert audit['payload_hardware_observed'] and not audit['exact_signature_match']

# A sortKey with one unanimous hardware payload can use that payload, but remains a family match.
rfamily=rec(sort_key=4,state_flags=201,surface_type_bits=42)
bf,mode=resolve_runtime_compatible(rfamily,s)
assert len(bf)==0x38 and mode=='observed_sortkey_family'
assert resolution_tier(mode)=='hardware_observed_family'

# A target with an unseen sortKey can use an otherwise identical, conflict-free observed render
# state family. Sort order/hash/surface classification remain separate Material fields, and the
# result is explicitly family evidence rather than an exact target signature.
tex=MaterialTexture(0,0,0,0,0,0)
entry=bytes.fromhex('FFFFFFFF00FFFF01010101010101020202FFFFFFFF03FFFFFF00')
textures=(tex,tex,tex)
states=((1,2),(3,4),(5,6),(7,8))
rcross=rec(sort_key=11,state_flags=49,textures=textures,constants=(bytes(32),bytes(32)),state_bits=states)
scross=MaterialStateConversion(entry,states,0,0,0)
bcross,mode=resolve_runtime_compatible(rcross,scross)
assert bcross==bytes(0x38) and mode=='observed_render_state_family'
across=runtime_resolution_audit(rcross,scross)
assert across['tier']=='hardware_observed_family'
assert across['evidence_scope']=='render_state_signature_without_sort_hash_or_surface'
assert across['catalog_observations']==1 and not across['exact_signature_match']

# SortKey 12 has two observed payloads.  Neutral shapes may reuse the neutral family; the known
# non-neutral 1-texture/1-constant/3-state shape must fall through when its full signature is new.
r12=rec(sort_key=12,state_flags=201)
b12,mode=resolve_runtime_compatible(r12,s)
assert b12==bytes(0x38) and mode=='observed_sortkey12_neutral_family'
r12mixed=rec(sort_key=12,state_flags=201,textures=(tex,),constants=(bytes(32),),state_bits=((1,2),(3,4),(5,6)))
s12mixed=MaterialStateConversion(bytes(26),r12mixed.state_bits,0,0,0)
b12mixed,mode=resolve_runtime_compatible(r12mixed,s12mixed)
assert b12mixed==bytes(0x38) and mode=='synthetic_neutral_runtime'
assert runtime_resolution_audit(r12mixed,s12mixed)['tier']=='synthetic_assignment'

r2=rec(sort_key=123)
assert try_resolve(r2,s) is None
try:resolve_exact(r2,s);raise AssertionError('must fail closed')
except ValueError:pass
br,mode=resolve_runtime_compatible(r2,s)
assert br==bytes(0x38) and mode=='synthetic_neutral_runtime'
assert resolution_tier(mode)=='synthetic_assignment'
try:resolution_tier('made_up');raise AssertionError('unknown mode must fail closed')
except ValueError:pass
print('test_platform_state PASS')

import struct
from cod4porter.backend.v4_ffio import XAsset,XAssetList,FOLLOWING,INSERT,encode_pc_packed
from cod4porter.pc_fx import parse_impact_fx,IMPACT_REFERENCE_COUNT

pool=0x100
assets=(XAsset(0,pool,0x19,'fx',FOLLOWING),XAsset(1,pool+8,0x1a,'impactfx',FOLLOWING))
al=XAssetList('pc',0,0,(),assets,pool,0x20)
alias=encode_pc_packed(4,pool-(0x2c+0x10)+4)
for marker in (FOLLOWING,INSERT):
    # Root intentionally unaligned; repeated marker bytes test overlapping search.
    root=0x103;name=b'impact_test\0';table=root+8+len(name)
    z=bytearray(table+IMPACT_REFERENCE_COUNT*4+17)
    z[0x20:0x80]=b'\xff'*0x60
    struct.pack_into('<II',z,root,FOLLOWING,marker);z[root+8:table]=name
    struct.pack_into('<I',z,table,alias)
    rows=parse_impact_fx(bytes(z),al)
    assert len(rows)==1 and rows[0].root_offset==root
    assert rows[0].name=='impact_test' and rows[0].fx_source_asset_indices[0]==0
print('test_impactfx_scan PASS')

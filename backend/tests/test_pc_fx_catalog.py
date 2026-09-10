import struct
from cod4porter.backend.v4_ffio import XAsset,XAssetList,FOLLOWING
from cod4porter.pc_fx import top_level_fx,PC_FX_TYPE

def al(n):
    pool=0x100;assets=tuple(XAsset(i,pool+i*8,PC_FX_TYPE,'fx',FOLLOWING) for i in range(n));return XAssetList('pc',0,0,(),assets,pool,0x20)
z=bytearray(0x300)
# Name-only shell at 0x20.
struct.pack_into('<I',z,0x20,FOLLOWING);z[0x40:0x49]=b'fx/alias\0'
# Owned one-element FX at 0x100.
struct.pack_into('<I',z,0x100,FOLLOWING);struct.pack_into('<I',z,0x110,1);struct.pack_into('<I',z,0x11c,FOLLOWING);z[0x120:0x129]=b'fx/owned\0';e=0x129;z[e+0xb0]=8;z[e+0xb1]=1;struct.pack_into('<I',z,e+0xbc,FOLLOWING);z[e+0xfc:e+0xfc+4]=b'tex\0'
r=top_level_fx(bytes(z),al(2));assert [x.name for x in r]==['fx/alias','fx/owned'];assert not r[0].source_owned and r[1].source_owned and r[1].graph is not None
# A plausible name-only shell inside proven world geometry must not become an FX.
z.extend(bytes(0x100));struct.pack_into('<I',z,0x300,FOLLOWING);z[0x320:0x325]=b'66ii\0'
r=top_level_fx(bytes(z),al(2),excluded_ranges=((0x300,0x400),))
assert [x.name for x in r]==['fx/alias','fx/owned']
print('test_pc_fx_catalog PASS')

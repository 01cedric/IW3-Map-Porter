import struct
from cod4porter.pc_technique import *
from cod4porter.backend.v4_ffio import FOLLOWING,XAsset,XAssetList
z=bytearray(0x300);r=0x100
struct.pack_into('<I',z,r,FOLLOWING);z[r+4]=2;z[r+5]=1;struct.pack_into('<I',z,r+8,0)
for i in range(34):struct.pack_into('<I',z,r+12+i*4,0)
z[r+0x94:r+0x94+9]=b',sm2/test\0'
rec=scan_technique_sets(bytes(z));assert len(rec)==1 and rec[0].ps3_reference_candidate=='test'
al=XAssetList('pc',0,0,(),(XAsset(7,0,0x05,'techset',FOLLOWING),),0,0)
top=top_level_technique_sets(bytes(z),al);assert top[0].source_asset_index==7
print({'passed':True,'name':top[0].name,'ps3':top[0].ps3_reference_candidate})

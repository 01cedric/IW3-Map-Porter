from cod4porter.pc_image import *
from cod4porter.backend.v4_ffio import XAssetList,XAsset,FOLLOWING,encode_pc_packed
import struct

def image_bytes(name,resource=b'ABCDEFGH',fmt=0x31545844):
    r=bytearray(0x24);struct.pack_into('<i',r,0,3);struct.pack_into('<I',r,4,FOLLOWING);r[10]=0;r[11]=2;r[31]=0;struct.pack_into('<I',r,0x20,FOLLOWING)
    ld=bytearray(0x10);ld[0]=1;struct.pack_into('<HHH',ld,2,4,4,1);struct.pack_into('<I',ld,8,fmt);struct.pack_into('<I',ld,0x0c,len(resource))
    return bytes(r)+name.encode()+b'\0'+bytes(ld)+resource

z=bytearray(4096);r0=1000;b0=image_bytes('img/a');z[r0:r0+len(b0)]=b0;r1=r0+len(b0);b1=image_bytes('img/b');z[r1:r1+len(b1)]=b1
assets=(XAsset(0,0,6,'image',FOLLOWING),XAsset(1,8,6,'image',FOLLOWING),XAsset(2,16,6,'image',encode_pc_packed(4,123)))
al=XAssetList('pc',0,0,(),assets,0,900)
cat=locate_top_level_images(bytes(z),al,expected_names_by_asset_index={0:'img/a',1:'img/b',2:'img/nested'},iwd_names=['img/a','img/b'])
assert cat.inline_asset_count==2 and cat.packed_asset_count==1
assert cat.records_by_asset_index[0].root_offset==r0 and cat.records_by_asset_index[1].root_offset==r1
assert cat.exact_name_by_asset_index[2]=='img/nested'
a=image_asset_from_pc(cat.records_by_asset_index[0]);assert a.ps3_format==0x86 and a.unpadded==8 and len(a.resource)==128
print('PASS test_pc_image')

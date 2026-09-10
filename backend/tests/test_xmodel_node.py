import struct
from dataclasses import dataclass,field
from cod4porter.xmodel import *
from cod4porter.graph import AssetType,write_graph,GraphContext
from cod4porter.zone_writer import TEMP_BLOCK,FOLLOWING,RelocatingZoneWriter

@dataclass
class DummyMaterial:
    name:str;symbol:str
    type:AssetType=field(init=False,default=AssetType.MATERIAL);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w,c):
        p=w.physical_position;w.define_symbol(self.symbol);c.register_root(self.symbol,p);root=bytearray(0x80);struct.pack_into('>I',root,0,FOLLOWING);w.write_in_block(root,'dummy material');w.write_latin1z(self.name,'mat name')

# one vertex and one tri + skeleton/bone data
pc=bytearray(0x400)
# skeleton sections laid out at fixed offsets
payloads={};cursor=0
for sec,data in [
 ('boneNames',struct.pack('<H',5)),('parentList',b''),('quats',b''),('trans',b''),('partClassification',b'\x01'),('baseMat',struct.pack('<8I',*range(8)) )]:
    off=cursor;pc[off:off+len(data)]=data;payloads[sec]=(off,len(data));cursor+=len(data)
vertex=0x100
for i in range(8):struct.pack_into('<I',pc,vertex+i*4,0x100+i)
tri=0x140;struct.pack_into('<HHH',pc,tri,0,0,0)
bone=0x180
for i in range(10):struct.pack_into('<I',pc,bone+i*4,i)
s=XSurface(0,False,1,1,0,[0,0,0,0],0,0,[1,2,3,4],'testmat',vertex,tri)
lods=[XModelLod(0.0,1,0,[1,2,3,4])]+[XModelLod(1000.0,0,0,[0,0,0,0]) for _ in range(3)]
m=XModel('model_test',1,1,0,1,-1,0,32.0,[-1,-1,-1],[1,1,1],123,0,False,lods,[s],payloads,bone,source_asset_index=5)
mat=DummyMaterial('testmat','mat:testmat')
node=XModelNode(bytes(pc),m,{'testmat':'mat:testmat'},'xmodel:model_test')
r=write_graph([mat,node])
res=r.context.results['xmodel:xmodel:model_test']
assert res['vertex_count']==1 and res['triangle_count']==1 and res['surface_count']==1
assert len(r.zone.zone_bytes)>0
from cod4porter.physics import PhysPreset
m.inline_phys_preset=PhysPreset('inline_prefix',1,1.,.1,.2,1.,1.,0.,0.,False,None)
fixed=write_graph([mat,node]);z=fixed.zone.zone_bytes
root=fixed.context.results['xmodel:xmodel:model_test']['inline_phys_preset_root_physical']
assert struct.unpack_from('>I',z,root+0x1c)[0]==FOLLOWING
assert z[root+0x2c:root+0x2c+15]==b'inline_prefix\0\0'
print({'passed':True,'zone_bytes':len(r.zone.zone_bytes),'geometry_sha256':res['geometry_sha256']})

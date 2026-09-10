"""Portal vertices must be consumed before reading the next cell's AABB."""
import struct
from cod4porter.pc_gfxworld import parse_gfxworld,PC_ROOT
from cod4porter.gfxworld_aabb import resolve_aabb_reuse

root=0x20;z=bytearray(root+PC_ROOT)
def put(offset,value):struct.pack_into('<I',z,offset,value)
for offset,value in {8:1,0xc:1,0x18:1,0x30:1,0xf0:2,0x150:1}.items():put(root+offset,value)
for offset in (0xf4,0xf8,0x104,0x12c,0x154,0x34,0x294):put(root+offset,0xffffffff)
z+=b'mp_portals\0'+bytes(20)+bytes(2)
cells=len(z);z+=bytes(2*0x38)
put(cells+0x20,1);put(cells+0x24,0xffffffff)
put(cells+0x38+0x18,1);put(cells+0x38+0x1c,0xffffffff)
portal=len(z);z+=bytes(0x44)
put(portal+0x24,0xffffffff);z[portal+0x28]=4
# +0x2c is hullAxis, not the vertex-count field.
z+=struct.pack('<12f',1,2,3,4,5,6,7,8,9,10,11,12)
aabb=len(z);z+=bytes(0x2c)
z+=bytes(2)+bytes(0x38)+bytes(0x2c)+bytes(0x30)
report=parse_gfxworld(bytes(z),'mp_portals',dict(planes=1,submodels=1,static_models=0,dynamic_models=0,dynamic_brushes=0))
assert report.full.portal_count==1 and report.full.portal_vertex_count==4
assert report.full.physical_end==len(z)
assert report.full.aabb_count==1
plan=resolve_aabb_reuse(bytes(z),report)
assert plan.bindings_by_record[aabb].cell_index==1
assert plan.null_pointer_count==1
print('test_pc_portal_layout PASS')

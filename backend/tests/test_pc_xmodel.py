import struct
from cod4porter.pc_xmodel import *
# synthesize a minimal PC XModel matching scanner invariants
z=bytearray(0x800);r=0x100
struct.pack_into('<I',z,r,0xffffffff);z[r+4:r+8]=bytes([1,1,1,0])
for o in (0x08,0x18,0x1c,0x20,0x24):struct.pack_into('<I',z,r+o,0xffffffff)
for o in (0x0c,0x10,0x14):struct.pack_into('<I',z,r+o,0)
for i in range(4):
 p=r+0x28+i*0x1c;struct.pack_into('<fHH',z,p,0.0 if i==0 else 1000.0,1 if i==0 else 0,0)
struct.pack_into('<I',z,r+0x98,0);struct.pack_into('<i',z,r+0x9c,0);struct.pack_into('<I',z,r+0xa0,0);struct.pack_into('<I',z,r+0xa4,0xffffffff);struct.pack_into('<f',z,r+0xa8,10.0)
for i,v in enumerate((-1.,-1.,-1.)):struct.pack_into('<f',z,r+0xac+i*4,v)
for i,v in enumerate((1.,1.,1.)):struct.pack_into('<f',z,r+0xb8+i*4,v)
struct.pack_into('<Hh',z,r+0xc4,1,-1);struct.pack_into('<I',z,r+0xcc,123);z[r+0xd0]=0;z[r+0xd1]=0;struct.pack_into('<II',z,r+0xd4,0,0)
c=r+0xdc;z[c:c+3]=b'm\0';c+=2
# boneNames u16
struct.pack_into('<H',z,c,1);c+=2
# partClassification
z[c]=1;c+=1
# baseMat 0x20
c+=0x20
sr=c;c+=0x38
z[sr]=0;z[sr+1]=0;struct.pack_into('<HH',z,sr+2,1,1);z[sr+6]=0;struct.pack_into('<I',z,sr+0xc,0xffffffff);struct.pack_into('<4h',z,sr+0x10,0,0,0,0);struct.pack_into('<I',z,sr+0x18,0);struct.pack_into('<I',z,sr+0x1c,0xffffffff);struct.pack_into('<II',z,sr+0x20,0,0)
# inline vertex then tri
v=c
for i in range(8):struct.pack_into('<I',z,c+i*4,i+1)
c+=0x20;t=c;struct.pack_into('<HHH',z,c,0,0,0);c+=6
# material handle packed/noninline is okay for parser
struct.pack_into('<I',z,c,0x10000001);c+=4
roots=scan_xmodel_roots(bytes(z));assert roots==[r],roots
x=parse_xmodel(bytes(z),r);assert x.model.name=='m' and x.model.surfaces[0].vertex_source==v and x.model.surfaces[0].triangle_source==t
res=resolve_skeleton_and_surface_reuse(bytes(z),[x]);assert res=={'packed_skeleton':0,'packed_skeleton_ambiguous':0,'packed_blend':0,'packed_rigid':0}
print({'passed':True,'root':hex(r),'surface_root':hex(sr),'end':hex(x.physical_after_material_handles)})

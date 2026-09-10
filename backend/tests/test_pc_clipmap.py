from __future__ import annotations
import struct
from cod4porter.backend.v4_ffio import encode_pc_packed
from cod4porter.pc_clipmap import parse_clipmap


def p16(b,o,v): struct.pack_into('<H',b,o,v)
def pi16(b,o,v): struct.pack_into('<h',b,o,v)
def p32(b,o,v): struct.pack_into('<I',b,o,v & 0xffffffff)
def pi32(b,o,v): struct.pack_into('<i',b,o,v)
def pf(b,o,v): struct.pack_into('<f',b,o,float(v))
def pv3(b,o,v):
    for i,x in enumerate(v): pf(b,o+i*4,x)

root=0x200
plane_phys=0x80
z=bytearray(0x900)
# one unmistakable normalized PC plane
pv3(z,plane_phys,(0.6,0.8,0.0));pf(z,plane_phys+0x0c,4.25);z[plane_phys+0x10]=3;z[plane_phys+0x11]=5
plane_raw=encode_pc_packed(4,0x1000)
side_raw=encode_pc_packed(4,0x2000)
edge_raw=encode_pc_packed(4,0x3000)
# ClipMap root
pi32(z,root+4,1);p32(z,root+8,1);p32(z,root+0x0c,plane_raw)
p32(z,root+0x10,0);p32(z,root+0x18,1);p32(z,root+0x20,1);p32(z,root+0x28,1)
p32(z,root+0x30,1);p32(z,root+0x38,1);p32(z,root+0x40,1);p32(z,root+0x48,0);p32(z,root+0x50,0)
p32(z,root+0x58,0);pi32(z,root+0x60,0);pi32(z,root+0x6c,0);pi32(z,root+0x74,0);pi32(z,root+0x7c,0)
p32(z,root+0x84,1);p16(z,root+0x8c,1);pi32(z,root+0x94,1);pi32(z,root+0x98,1);pi32(z,root+0xa0,1)
p32(z,root+0xa4,0xfffffffe);p32(z,root+0xa8,0xffffffff);p16(z,root+0xf4,0);p16(z,root+0xf6,0);p32(z,root+0x118,0x12345678)
# embedded box model (keep finite but deliberately not plane-like)
pv3(z,root+0xac,(0,0,0));pv3(z,root+0xb8,(2,3,4));pf(z,root+0xc4,5);p16(z,root+0xc8,0);p16(z,root+0xca,0);pi32(z,root+0xcc,0);pi32(z,root+0xd0,0);pv3(z,root+0xd4,(0,0,0));pv3(z,root+0xe0,(2,3,4));pi32(z,root+0xec,0);pi16(z,root+0xf0,0)

cur=root+0x11c
# material 0x48
name=b'clip_test\0';z[cur:cur+len(name)]=name;pi32(z,cur+0x40,7);pi32(z,cur+0x44,11);cur+=0x48
# brush side 0x0c
p32(z,cur,plane_raw);p32(z,cur+4,0);pi16(z,cur+8,-1);z[cur+0x0a]=1;cur+=0x0c
# edge byte
z[cur]=2;cur+=1
# node 8
p32(z,cur,plane_raw);pi16(z,cur+4,-1);pi16(z,cur+6,-2);cur+=8
# leaf 0x2c
p16(z,cur,0);p16(z,cur+2,0);pi32(z,cur+4,1);pi32(z,cur+8,2);pv3(z,cur+0x0c,(-3,-4,-5));pv3(z,cur+0x18,(3,4,5));pi32(z,cur+0x24,0);pi16(z,cur+0x28,0);cur+=0x2c
# leaf brush node 0x14, count <= 0
z[cur]=0;pi16(z,cur+2,0);pi32(z,cur+4,3);pf(z,cur+8,1.25);pf(z,cur+0x0c,2.5);p16(z,cur+0x10,0);p16(z,cur+0x12,0);cur+=0x14
# submodel 0x48
pv3(z,cur,(-3,-4,-5));pv3(z,cur+0x0c,(3,4,5));pf(z,cur+0x18,7.1);p16(z,cur+0x1c,0);p16(z,cur+0x1e,0);pi32(z,cur+0x20,1);pi32(z,cur+0x24,2);pv3(z,cur+0x28,(-3,-4,-5));pv3(z,cur+0x34,(3,4,5));pi32(z,cur+0x40,0);pi16(z,cur+0x44,0);cur+=0x48
# main brush 0x50, one non-axial side + edge
pv3(z,cur,(-2,-2,-2));pi32(z,cur+0x0c,5);pv3(z,cur+0x10,(2,2,2));p32(z,cur+0x1c,1);p32(z,cur+0x20,side_raw)
for i in range(6): pi16(z,cur+0x24+i*2,0)
p32(z,cur+0x30,edge_raw)
for i in range(6): pi16(z,cur+0x34+i*2,0);z[cur+0x40+i]=1
cur+=0x50
# visibility: 1 cluster x 1 byte
z[cur]=0xff;cur+=1
# MapEnts root and allocation
p32(z,cur,0xffffffff);p32(z,cur+4,0xffffffff);pi32(z,cur+8,4);cur+=0x0c;z[cur:cur+4]=b'x\0\0\0';cur+=4
# box brush 0x50 with null side/edge pointers
pv3(z,cur,(-1.1,-1.2,-1.3));pi32(z,cur+0x0c,5);pv3(z,cur+0x10,(1.2,1.3,1.4));p32(z,cur+0x1c,0);p32(z,cur+0x20,0);p32(z,cur+0x30,0);cur+=0x50

clip=parse_clipmap(bytes(z[:cur+16]),'mp_test')
assert clip.root_offset==root
assert len(clip.planes)==1 and abs(clip.planes[0].normal[0]-0.6)<1e-6
assert len(clip.materials)==1 and clip.materials[0].name=='clip_test'
assert len(clip.brush_sides)==1 and clip.brush_sides[0].plane_index==0
assert len(clip.nodes)==1 and clip.nodes[0].plane_index==0
assert len(clip.leaves)==1 and clip.cluster_count==1 and clip.cluster_bytes==1 and clip.visibility==b'\xff'
assert len(clip.brushes)==1 and clip.brushes[0].first_side==0 and clip.brushes[0].base_edge==0
assert clip.mapents_text=='x' and clip.mapents_allocated==4
assert clip.checksum==0x12345678
print({'passed':True,'root':hex(root),'known_end':hex(clip.known_end),'materials':len(clip.materials),'brushes':len(clip.brushes)})

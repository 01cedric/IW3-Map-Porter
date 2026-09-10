import struct
from cod4porter.pc_material import parse_material_at,scan_materials
r=0x100;z=bytearray(0x500)
struct.pack_into('<I',z,r,0xffffffff);z[r+4:r+8]=bytes([1,5,1,1]);struct.pack_into('<Q',z,r+8,0x1234);struct.pack_into('<I',z,r+0x10,0x20);struct.pack_into('<H',z,r+0x14,7);z[r+0x18:r+0x18+34]=b'\xff'*34
z[r+0x3a]=1;z[r+0x3b]=1;z[r+0x3c]=1;z[r+0x3d]=2;z[r+0x3e]=3;struct.pack_into('<I',z,r+0x40,0x12345678);struct.pack_into('<III',z,r+0x44,0xffffffff,0xffffffff,0xffffffff)
c=r+0x50;z[c:c+4]=b'mat\0';c+=4
# texture table with inline image
struct.pack_into('<I4B I',z,c,0xdeadbeef,1,2,3,5,0xffffffff);c+=0x0c
# image root 0x24; name and no loaddef
struct.pack_into('<I',z,c+4,0);struct.pack_into('<I',z,c+0x20,0xffffffff);c+=0x24;z[c:c+4]=b'img\0';c+=4
# constant
for i in range(8):struct.pack_into('<I',z,c+i*4,i+1)
c+=0x20
# state
struct.pack_into('<II',z,c,0x11,0x22);c+=8
m,end=parse_material_at(bytes(z),r)
assert m.name=='mat' and m.textures[0].inline_image_name=='img' and m.state_bits==((0x11,0x22),) and end==c
ss=scan_materials(bytes(z),{'mat'});assert len(ss)==1 and ss[0].root_offset==r
print({'passed':True,'end':hex(end),'textures':len(m.textures),'states':len(m.state_bits)})

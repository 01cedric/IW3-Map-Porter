"""Reject corrupt linked buffers even when stream consumption was correct."""
from pathlib import Path
import struct,sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools/ps3_loader_emulator'))
from worldcheck import validate_world
class Memory:
    def __init__(self): self.data=bytearray(0x10000)
    def read(self,p,n): return bytes(self.data[p:p+n])
    def write(self,p,b): self.data[p:p+len(b)]=b
    def u32(self,p): return struct.unpack_from('>I',self.data,p)[0]
    def w32(self,p,v): struct.pack_into('>I',self.data,p,v)
m=Memory();h=0x100;ranges=[(0x1000,0x3000)];inv={(4,'mat'):0x9000,(3,'model'):0xa000}
for o,v in {0x34:3,0x38:0x1000,0x10:3,0x14:0x1100,0x1c:1,0x29c:0x1200,0x250:1,0x2a4:0x1300,0x44:84,0x48:0x1400}.items():m.w32(h+o,v)
m.write(0x1100,struct.pack('>3H',0,1,2));m.write(0x1200,struct.pack('>IIIHHII',0,0,0,3,1,0,0x9000));m.w32(0x1320,0xa000)
assert validate_world(m,h,inv,ranges)['passed']
def reject(change,restore,text):
    change()
    try:validate_world(m,h,inv,ranges)
    except RuntimeError as e:assert text in str(e),str(e)
    else:raise AssertionError('corruption accepted: '+text)
    restore()
reject(lambda:m.w32(h+0x14,0x3ffe),lambda:m.w32(h+0x14,0x1100),'zone memory')
reject(lambda:m.w32(0x1210,1),lambda:m.w32(0x1210,0),'triangle range')
reject(lambda:m.write(0x1100,struct.pack('>H',3)),lambda:m.write(0x1100,bytes(2)),'vertex index')
reject(lambda:m.w32(0x1200,4),lambda:m.w32(0x1200,0),'vertex layers')
reject(lambda:m.w32(0x1214,0x9004),lambda:m.w32(0x1214,0x9000),'material is not registered')
reject(lambda:m.w32(0x1320,0xa004),lambda:m.w32(0x1320,0xa000),'XModel is not registered')
print('PASS: valid world and six independently corrupt linked buffer/reference cases')

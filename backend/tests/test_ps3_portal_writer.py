"""Cell relocations, endian conversion and loader traversal of a two-cell world."""
import os, struct, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.pc_gfxworld import parse_gfxworld, PC_ROOT
from cod4porter.gfxworld_portals import resolve_portals
from cod4porter.assets.gfxworld import GfxWorldNode
from cod4porter.graph import write_graph
from cod4porter.backend.v4_ffio import decode_ps3_pointer

z=bytearray(PC_ROOT)
def put(offset,value):struct.pack_into('<I',z,offset,value)
for offset,value in {8:1,0xc:1,0x18:1,0x30:1,0xf0:2,0x150:1}.items():put(offset,value)
for offset in (0xf4,0xf8,0x104,0x12c,0x154,0x34,0x294):put(offset,0xffffffff)
z+=b'mp_portals\0'+bytes(20)+bytes(2)
cells=len(z);z+=bytes(2*0x38)
portals=[];points=(1.,2.,3.,4.,5.,6.,7.,8.,9.)
for ci in range(2):
    put(cells+ci*0x38+0x20,1);put(cells+ci*0x38+0x24,0xffffffff)
    p=len(z);portals.append(p);z+=bytes(0x44)
    struct.pack_into('<4f',z,p+0xc,1.,0.,0.,16.)
    z[p+0x1c:p+0x20]=bytes((0,1,2,0))
    put(p+0x20,0x40000801+(1-ci)*0x38)
    put(p+0x24,0xffffffff);z[p+0x28]=3
    struct.pack_into('<6f',z,p+0x2c,1.,2.,3.,4.,5.,6.)
    z+=struct.pack('<9f',*points)
z+=bytes(2)+bytes(0x38)+bytes(0x2c)+bytes(0x30)
z=bytes(z)
report=parse_gfxworld(z,'mp_portals',dict(planes=1,submodels=1,static_models=0,dynamic_models=0,dynamic_brushes=0))
bindings=resolve_portals(z,report)
assert [bindings[p].target_cell for p in portals]==[1,0]
node=GfxWorldNode(z,report,'gfx:test');result=write_graph([node]);target=result.zone.zone_bytes
row=result.context.results['gfxworld:gfx:test'];base=row['cell_physical']
first=base+2*0x38;second=first+0x44+3*12
for ci,p in enumerate((first,second)):
    assert struct.unpack_from('>4f',target,p+0xc)==(1.,0.,0.,16.)
    assert struct.unpack_from('>6f',target,p+0x2c)==(1.,2.,3.,4.,5.,6.)
    assert struct.unpack_from('>9f',target,p+0x44)==points
    assert target[p:p+12]==bytes(12) and target[p+0x28]==3
refs=[decode_ps3_pointer(struct.unpack_from('>I',target,p+0x20)[0]) for p in (first,second)]
assert refs[0].block==refs[1].block==4 and refs[0].offset-refs[1].offset==0x38
bad=bytearray(z);struct.pack_into('<I',bad,portals[0]+0x20,0x30000839)
try:resolve_portals(bytes(bad),report)
except ValueError:pass
else:raise AssertionError('Foreign block neighbour accepted')
bad=bytearray(z);struct.pack_into('<I',bad,portals[0]+0x20,0x40000838)
try:resolve_portals(bytes(bad),report)
except ValueError:pass
else:raise AssertionError('Misaligned neighbour accepted')
if os.environ.get('IW3_PS3_ELF'):
    sys.path.insert(0,str(ROOT/'tools/ps3_loader_emulator'))
    from zoneload import ZoneLoader
    loader=ZoneLoader(target);loader.trace=[];loader.load()
    assert loader.pos==len(target) and all(h<=s for h,s in zip(loader.high,loader.sizes))
    reads=[t for t in loader.trace if t[0]==0xc5628]
    assert len(reads)==2 and all(t[2]==0x44 for t in reads)
    cell_read=next(t for t in loader.trace if t[0]==0xc5728)
    for ci,read in enumerate(reads):
        assert loader.m.u32(read[4]+0x20)==cell_read[4]+(1-ci)*0x38
    print('Real CoD4 PS3 portal loader PASS')
print('test_ps3_portal_writer PASS')

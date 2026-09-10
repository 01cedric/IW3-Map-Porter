"""Exercise emitted child addresses and independent linked SPU tree checks."""
from pathlib import Path
import struct
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tools/ps3_loader_emulator'))
from cod4porter.assets.gfxworld import GfxWorldNode
from cod4porter.gfxworld_aabb import ps3_children_offset
from cod4porter.pc_gfxworld import parse_gfxworld,PC_ROOT,PC_CELL
from cod4porter.graph import write_graph
from test_gfxworld_node import fixture
from worldcheck import validate_aabb_trees


def main():
    # Eleven records: the first child is ten records away. The legacy offset
    # 440 is also divisible by 40, so alignment alone would miss this error.
    z,name=fixture();z=bytearray(z)
    cell=PC_ROOT+len(name)+1+0x14+2
    struct.pack_into('<II',z,cell+0x18,11,0xffffffff)
    records=bytearray(11*44)
    struct.pack_into('<H',records,0x18,1)
    struct.pack_into('<i',records,0x28,440)
    z[cell+PC_CELL:cell+PC_CELL]=records
    rep=parse_gfxworld(bytes(z),name,dict(planes=1,submodels=1,static_models=0,dynamic_models=0,dynamic_brushes=0))
    result=write_graph([GfxWorldNode(bytes(z),rep,'gfx:tree')])
    start=result.context.results['gfxworld:gfx:tree']['cell_physical']+PC_CELL
    assert struct.unpack_from('>i',result.zone.zone_bytes,start+0x24)[0]==400
    assert struct.unpack_from('>H',result.zone.zone_bytes,start+400+0x18)[0]==0
    for offset,children,index,count in [(43,1,0,11),(484,1,0,11),(44,2,10,11),(0,1,0,11),(-44,1,1,11)]:
        try:ps3_children_offset(offset,children,index,count)
        except ValueError:pass
        else:raise AssertionError('invalid source edge accepted')
    assert ps3_children_offset(44,2,0,3)==40

    class Memory:
        data=bytearray(0x5000)
        def read(self,p,n):return bytes(self.data[p:p+n])
        def u32(self,p):return struct.unpack_from('>I',self.data,p)[0]
    m=Memory();world=0x100;cell=0x1000;tree=0x1100
    struct.pack_into('>I',m.data,world+0xfc,1)
    struct.pack_into('>I',m.data,world+0x110,cell)
    struct.pack_into('>II',m.data,cell+0x18,3,tree)
    struct.pack_into('>H',m.data,tree+0x18,2)
    struct.pack_into('>i',m.data,tree+0x24,40)
    ranges=[(0x1000,0x3000)]
    assert validate_aabb_trees(m,world,ranges)['parents']==1
    for bad in [44,120,0,-40]:
        struct.pack_into('>i',m.data,tree+0x24,bad)
        try:validate_aabb_trees(m,world,ranges)
        except RuntimeError:pass
        else:raise AssertionError('invalid linked child edge accepted')
    struct.pack_into('>i',m.data,tree+0x24,40)
    struct.pack_into('>HI',m.data,tree+40+0x1e,1,0x18c)
    try:validate_aabb_trees(m,world,ranges)
    except RuntimeError as e:assert 'zone memory' in str(e)
    else:raise AssertionError('console-like low DMA pointer accepted')
    struct.pack_into('>HI',m.data,tree+40+0x1e,1,0x2000)
    struct.pack_into('>I',m.data,world+0x250,1)
    assert validate_aabb_trees(m,world,ranges)['static_model_indices']==1
    struct.pack_into('>H',m.data,0x2000,1)
    try:validate_aabb_trees(m,world,ranges)
    except RuntimeError as e:assert 'out of range' in str(e)
    else:raise AssertionError('invalid static model index accepted')
    print('PASS: emitted child rebasing, invalid source/linked edges, DMA pointers and model indices')


if __name__=='__main__':main()

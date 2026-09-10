import struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.world_core import *
from cod4porter.graph import write_graph,AssetType,GraphContext
from cod4porter.zone_writer import TEMP_BLOCK
from dataclasses import dataclass,field

@dataclass
class NameCarrier:
    symbol:str='clip:test';type:AssetType=field(init=False,default=AssetType.CLIPMAP_MP);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w,c):
        rp=w.physical_position;w.define_symbol(self.symbol);c.register_root(self.symbol,rp);w.write_u32(0,'dummy clip root');w.define_symbol(self.symbol+'::name');w.write_latin1z('maps/mp/test.d3dbsp','map name')

def main():
    name='maps/mp/test.d3dbsp';z=bytearray(0x10+len(name)+1+0x44+len('lightdef/test')+1)
    struct.pack_into('<IiiI',z,0,0xffffffff,1,1,0xffffffff);c=0x10;z[c:c+len(name)+1]=name.encode()+b'\0';c+=len(name)+1
    z[c]=2;z[c+1]=1;z[c+2]=3
    for j in range(15):struct.pack_into('<f',z,c+4+j*4,float(j)+.5)
    struct.pack_into('<I',z,c+0x40,0xffffffff);c+=0x44;z[c:c+14]=b'lightdef/test\0'
    cw=parse_comworld(bytes(z),name,1);assert cw.primary_lights[0].def_name=='lightdef/test'
    r=write_graph([ComWorldNode(cw,'com:test'),GameWorldMpNode(name,'clip:test::name','game:test'),NameCarrier()])
    assert [int(x) for x in r.asset_types]==[0x0e,0x10,0x0d]
    assert r.context.results['comworld:com:test']['primary_light_count']==1
    print('PASS test_world_core')
if __name__=='__main__':main()

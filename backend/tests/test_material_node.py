from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.material import *
from cod4porter.assets import MaterialNode,ExternalImageNode
from cod4porter.graph import *
from dataclasses import dataclass,field
from cod4porter.zone_writer import TEMP_BLOCK
@dataclass
class ExtTech:
 name:str;symbol:str;type:AssetType=field(init=False,default=AssetType.TECHSET);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
 def write(self,w,c):
  rp=w.physical_position;c.register_root(self.symbol,rp);w.define_symbol(self.symbol);r=bytearray(0x70);r[:4]=(0xFFFFFFFF).to_bytes(4,'big');w.write_in_block(r,'tech');w.write_latin1z(','+self.name,'name')
def main():
 tex=MaterialTexture(0x1234,0,4,3,2,0,'foo',1);entry=bytes([0]+[255]*33);m=MaterialRecord(0,'mat',0,4,0,0,0,0,1,entry,1,0xFFFFFFFF,'Inline',1,0,0,3,0,(tex,),(),((1,2),),{});st=convert_state(m,[0]*34)
 assets=[ExtTech('2d','tech:2d'),ExternalImageNode('foo','img:foo'),MaterialNode(m,st,bytes(0x38),'tech:2d',('img:foo',),'mat:mat')];r=write_graph(assets);row=r.context.results['material:mat:mat'];assert row['state_count']==1
 assert row['state_object_locations'][0].block==0 and row['state_object_locations'][0].offset==row['root_location'].offset+0x80
 print(json.dumps({'passed':True,'zone_bytes':len(r.zone.zone_bytes)},indent=2))
if __name__=='__main__':main()

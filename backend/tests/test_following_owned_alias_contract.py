from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.graph import AssetType,Dependency,GraphContext,OwnedPointerKind,OwnershipEdge,owned_alias_symbol,write_graph
from cod4porter.zone_writer import FOLLOWING,LARGE_BLOCK,TEMP_BLOCK


@dataclass
class Child:
    symbol:str='child'
    type:AssetType=field(init=False,default=AssetType.MATERIAL)
    root_block:int=field(init=False,default=TEMP_BLOCK)
    dependencies:tuple=field(init=False,default=())
    def write(self,w,c):
        physical=w.physical_position;location=w.define_symbol(self.symbol);c.register_root(self.symbol,physical)
        w.write_u32(0,'child TEMP root');c.register_result('child',{'root_physical':physical,'root_location':location})


@dataclass
class Owner:
    child_symbol:str='child';symbol:str='owner'
    type:AssetType=field(init=False,default=AssetType.XMODEL)
    root_block:int=field(init=False,default=TEMP_BLOCK)
    @property
    def dependencies(self):return (Dependency(self.child_symbol,AssetType.MATERIAL,'inline child'),)
    def write(self,w,c:GraphContext):
        physical=w.physical_position;location=w.define_symbol(self.symbol);c.register_root(self.symbol,physical);w.write_u32(0,'owner root')
        w.push_block(LARGE_BLOCK);field=w.current_location;c.bind_owned_pointer_field(w,self.symbol,self.child_symbol,'field',field);w.write_u32(FOLLOWING,'owner FOLLOWING field');w.pop_block()
        c.write_owned_child(w,self.symbol,self.child_symbol,'field');c.register_result('owner',{'root_physical':physical,'root_location':location,'field_location':field})


@dataclass
class Consumer:
    child_symbol:str='child';symbol:str='consumer'
    type:AssetType=field(init=False,default=AssetType.FX)
    root_block:int=field(init=False,default=TEMP_BLOCK)
    @property
    def dependencies(self):return (Dependency(self.child_symbol,AssetType.MATERIAL,'packed reuse'),)
    def write(self,w,c):
        physical=w.physical_position;location=w.define_symbol(self.symbol);c.register_root(self.symbol,physical)
        w.write_pointer_to(c.reference_symbol(self.child_symbol),description='later packed child reuse');c.register_result('consumer',{'root_physical':physical,'root_location':location})


owner=Owner();consumer=Consumer();child=Child()
edge=OwnershipEdge('owner','child','field',OwnedPointerKind.FOLLOWING,'fixture',0)
result=write_graph((owner,consumer,child),top_level_assets=(owner,consumer),ownership_edges=(edge,))
field=result.context.results['owner']['field_location'];alias=result.zone.symbols[owned_alias_symbol('child')]
reuse=[x for x in result.zone.relocations if x.description=='later packed child reuse']
assert alias==field and alias.block==4
assert len(reuse)==1 and reuse[0].target==field
assert all(x.target.block!=0 for x in result.zone.relocations)
print({'passed':True,'alias_block':alias.block,'alias_offset':alias.offset,'temp_relocations':0})

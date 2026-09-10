from __future__ import annotations

from dataclasses import dataclass,field
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.backend.v4_ffio import parse_xasset_list
from cod4porter.graph import AssetType,Dependency
from cod4porter.plan import DispositionKind,PortPlan,SourceDisposition
from cod4porter.zone_writer import TEMP_BLOCK


@dataclass
class Node:
    symbol:str
    type:AssetType
    dependencies:tuple=()
    root_block:int=field(init=False,default=TEMP_BLOCK)
    def write(self,w,c):
        physical=w.physical_position;location=w.define_symbol(self.symbol);c.register_root(self.symbol,physical)
        w.write_u32(0,self.symbol+' root');c.register_result('test:'+self.symbol,{'root_physical':physical,'root_location':location})


dependency=Node('dependency',AssetType.TECHSET)
consumer=Node('consumer',AssetType.XMODEL,(Dependency('dependency',AssetType.TECHSET,'must load first'),))
plan=PortPlan(
    (consumer,dependency),(),
    (
        SourceDisposition(0,3,DispositionKind.EMITTED,'consumer',AssetType.XMODEL,'source first'),
        SourceDisposition(1,5,DispositionKind.EMITTED,'dependency',AssetType.TECHSET,'source later'),
    ),
    'mp_test',[],(),
)
assert [x.symbol for x in plan.top_level_assets()]==['dependency','consumer']
result=plan.build()
assert [x.type_id for x in parse_xasset_list(result.retail_zone,'ps3').assets]==[AssetType.TECHSET,AssetType.XMODEL]
print({'passed':True,'order':[x.symbol for x in plan.top_level_assets()]})

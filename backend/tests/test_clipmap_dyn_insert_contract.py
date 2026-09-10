from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import runpy
import struct
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.assets.clipmap import ClipMapNode
from cod4porter.graph import nested_symbol,write_graph
from cod4porter.pc_clipmap import DynEnt,DynEntOwnedPhysPreset
from cod4porter.physics import PhysPreset
from cod4porter.zone_writer import INSERT


base=runpy.run_path(str(Path(__file__).with_name('test_pc_clipmap.py')))['clip']
preset=PhysPreset(',insert_test',1,1.0,0.1,0.2,1.0,1.0,0.0,0.0,False,None)
model=DynEnt('model',0,0,(0,0,0,1),(0,0,0),0,0,0,0,0,INSERT,1,(0,0,0),(0,0,0),(0,0,0),0)
brush=replace(model,list_kind='brush')
owners=(
    DynEntOwnedPhysPreset('model',0,0,'insert',preset,0,0),
    DynEntOwnedPhysPreset('brush',0,1,'following',replace(preset,name=',following_test'),0,0),
)
symbol='clipmap.insert.test'
node=ClipMapNode(replace(base,dyn_models=(model,),dyn_brushes=(brush,),dyn_owned_phys_presets=owners),symbol)
graph=write_graph((node,));row=graph.context.results['clipmap:'+symbol];zone=graph.zone.zone_bytes

assert struct.unpack_from('>I',zone,row['dyn_model_physical']+0x30)[0]==INSERT
insert_symbol=nested_symbol(symbol,'dynEnt.model.0.physPreset.insert')
assert graph.zone.symbols[insert_symbol].block==4 and graph.zone.symbols[insert_symbol].offset%4==0
assert any(x.identity==insert_symbol and x.block==4 and x.size==4 for x in graph.zone.canonical_allocations)
owned=row['physpreset_bindings']['owned_rows']
assert [x['list_kind'] for x in owned]==['model','brush']
assert owned[0]['pointer_kind']=='insert' and owned[1]['pointer_kind']=='following'
assert owned[0]['root_physical']<row['dyn_brush_physical']<owned[1]['root_physical']
for child in owned:
    p=child['root_physical']
    assert struct.unpack_from('>I',zone,p+0x1c)[0]==0xffffffff
    name=child['name'].encode('latin1')+b'\0'
    assert zone[p+0x2c:p+0x2c+len(name)+1]==name+b'\0'
print({'passed':True,'insert_alias':graph.zone.symbols[insert_symbol].offset,'per_list_order':['model roots','model child','brush roots','brush child']})

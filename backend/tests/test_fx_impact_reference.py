"""Showdown Xmas: a packed impact effect must become a PS3 effect-name reference."""
from dataclasses import replace
from pathlib import Path
import struct,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_fx_runner_replay import _reference,_element
from cod4porter.pc_fx import FxEffectReference,resolve_fx_runner_bindings
from cod4porter.assets.fx import OwnedFxNode
from cod4porter.graph import write_graph

raw=0x4031cf99
impact=replace(_element(4),element_type=0,effect_on_impact=FxEffectReference(raw,None))
target=_reference(287,0xa68544,0xa6898f,'props/watermelon_splat')
owner=_reference(288,0xa6898f,0xa69e37,'props/watermelon',impact)
bindings=resolve_fx_runner_bindings('any_map',(target,owner))
assert bindings.target_asset_indices_by_raw=={raw:287}
assert bindings.bindings[0].visual_index==-1
node=OwnedFxNode(owner.graph,{},'fx:test',effect_names_by_raw={raw:target.name})
assert struct.unpack_from('>I',node._elem_root(impact),0xd8)[0]==0xffffffff
out=write_graph([node],[]).zone.zone_bytes
assert b'props/watermelon_splat\0' in out
assert struct.pack('>I',raw) not in out
try:resolve_fx_runner_bindings('any_map',(replace(target,physical_end_offset=target.physical_end_offset-1,graph=replace(target.graph,physical_end_offset=target.physical_end_offset-1)),owner))
except ValueError:pass
else:raise AssertionError('Broken physical target boundary accepted')
print('PASS: impact-effect target identity and PS3 name serialization.')

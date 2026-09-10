"""Shared draw-model shells and backward draw-cell alias proof."""
from pathlib import Path
import sys,struct
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.gfxworld_xmodels import read_shared_shell,resolve_draw_aliases
from cod4porter.backend.v4_ffio import encode_pc_packed
root=b'\xff'*4+b'\0'*216+b',prop_flag_opfor\0'
name,end=read_shared_shell(root,0)
assert name=='prop_flag_opfor' and end==len(root)
for bad in (root[:80],root[:4]+b'\x01'+root[5:],root.replace(b',prop',b'xprop')):
    try:read_shared_shell(bad,0)
    except ValueError:pass
    else:raise AssertionError('Unproven shared shell accepted')
base=0x120000
raw=encode_pc_packed(4,base+0x38)
aliases,actual=resolve_draw_aliases([0xffffffff,raw,raw],{0:'flag'},{})
assert actual==base and aliases=={raw:'flag'}
try:resolve_draw_aliases([0xffffffff,encode_pc_packed(6,32)],{0:'flag'},{})
except ValueError:pass
else:raise AssertionError('Wrong block accepted')
# Two equally possible earlier shared owners cannot establish an exact base.
try:resolve_draw_aliases([0xffffffff,0xffffffff,encode_pc_packed(4,base+0x38+76)],{0:'a',1:'b'},{})
except ValueError:pass
else:raise AssertionError('Ambiguous owner accepted')
print('PASS: shared model identity, bounds, backward alias and ambiguity checks.')

from cod4porter.assets.external import ExternalXModelNode
from cod4porter.readback import _family_check,_result_key
node=ExternalXModelNode('prop_flag_opfor','test')
zone=b'\0'*36+b'\xff'*4+b'\0'*200+node.serialized_name.encode()+b'\0'
row={'root_physical':36,'name_physical':36+0xcc}
assert _result_key(node,{'xmodel.external:test':row})=='xmodel.external:test'
assert _family_check(zone,[8192]*7,node,row)['passed']
bad=bytearray(zone);bad[40]=1
assert not _family_check(bytes(bad),[8192]*7,node,row)['passed']
bad=bytearray(zone);bad[36+0xcc+1]=ord('x')
assert not _family_check(bytes(bad),[8192]*7,node,row)['passed']
print('PASS: independent external XModel shell readback rejects corrupt body and identity.')

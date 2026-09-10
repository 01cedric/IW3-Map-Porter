from pathlib import Path
import tempfile
from cod4porter.assets.external import ExternalTechniqueSetNode
from cod4porter.assets.image import ExternalImageNode
from cod4porter.assets.basic import LightDefNode,RawFileNode,StringTableNode
from cod4porter.plan import PortPlan,SourceDisposition,DispositionKind
from cod4porter.readback import inspect_main_fastfile
from cod4porter.main_ff import save_main_fastfile
from cod4porter.zone_writer import FOLLOWING
import struct

tech=ExternalTechniqueSetNode('2d','t:2d',True)
img=ExternalImageNode('attenuation','i:attenuation')
light=LightDefNode('light',3,7,img.symbol,'l:light')
raw=RawFileNode('a.txt',b'abc','r:a')
st=StringTableNode('t.csv',2,1,('a','b'),'s:t')
assets=(tech,img,light,raw,st)
disps=tuple(SourceDisposition(i,0,DispositionKind.EMITTED,a.symbol,a.type,'synthetic deep-readback fixture') for i,a in enumerate(assets))
plan=PortPlan(assets,(),disps,'test')
b=plan.build()
image_row=b.graph_results['image.external:i:attenuation']
image_root=image_row['root_physical']
assert b.serialized_zone[image_root:image_root+0x30]==bytes(0x30)
assert struct.unpack_from('>I',b.serialized_zone,image_root+0x30)[0]==FOLLOWING
with tempfile.TemporaryDirectory() as td:
    p=Path(td)/'a.ff';save_main_fastfile(p,b,True)
    rb=inspect_main_fastfile(p,plan,b)
    assert rb.passed,rb
    assert rb.relocation_checks['passed']
    assert all(x['passed'] for x in rb.family_checks),rb.family_checks
    assert rb.pointer_leaks==0
print('test_deep_readback PASS')

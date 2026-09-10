"""Production layout floors and loading-image metadata regression coverage."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.plan import PortPlan, SourceDisposition, DispositionKind
from cod4porter.graph import AssetType
from cod4porter.assets.image import ImageNode, ImageResourcePolicy
from cod4porter.pc_load_converter import _image_asset_from_iwd
from cod4porter.backend.v4_iwd import IwiImage,IwiMip
from cod4porter.assets.image import from_iwi

image=IwiImage('loadscreen_mp_test',6,11,'dxt1',195,4,4,1,(0,0,0,0),(IwiMip(0,0,4,4,b'\0'*8),))
source=SimpleNamespace(levelbriefing_image=SimpleNamespace(loaddef=SimpleNamespace(width=4,height=4,depth=1,pc_format=0x31545844,level_count=1,flags=2),name=image.name,semantic=2),levelbriefing_name=image.name)
iwd=SimpleNamespace(image=image)
assert _image_asset_from_iwd(source,iwd).resource[:8]==b'\0'*8
for bad in (replace(image,flags=199),replace(image,width=8),replace(image,format_name='dxt5')):
    try:_image_asset_from_iwd(source,SimpleNamespace(image=bad))
    except ValueError:pass
    else:raise AssertionError('Invalid pixel layout accepted')
node=ImageNode(from_iwi(image),'image:test',ImageResourcePolicy.FASTFILE_DELAYED)
plan=PortPlan([node],[],[SourceDisposition(0,6,DispositionKind.EMITTED,node.symbol,AssetType.IMAGE)],'mp_test')
measured=plan.measure_block_sizes()
built=plan.build()
assert measured==built.block_sizes,(measured,built.block_sizes)
assert measured[0]>=8192 and measured[2]>=8192,measured
print('PASS: production budget floors match the writer; non-layout flags accepted; incompatible images rejected.')

from __future__ import annotations
from cod4porter.plan import *
from cod4porter.assets.basic import RawFileNode,ExternalFxNode
from cod4porter.assets.image import ImageNode,neutral_bc1_image
from cod4porter.graph import AssetType
from cod4porter.backend.v4_ffio import XAssetList,XAsset

assets=[RawFileNode('maps/mp/x.gsc',b'x','raw.x'),ExternalFxNode('stock_fx','fx.stock')]
# synthetic source: raw is emitted, source FX is proven native/shared rather than redundantly emitted.
src=XAssetList('pc',0,0,(),(XAsset(0,0,0x1f,'rawfile',0xffffffff),XAsset(1,8,0x19,'fx',0xffffffff)),0,0)
p=PortPlan(assets,(),(
    SourceDisposition(0,0x1f,DispositionKind.EMITTED,'raw.x',AssetType.RAWFILE,'owned source RawFile'),
    SourceDisposition(1,0x19,DispositionKind.EXTERNAL_NATIVE,None,None,'name-only stock FX proven native'),
),'mp_x')
r=p.validate(src);assert r['source_assets']==2 and r['dispositions']['emitted']==1 and r['dispositions']['external_native']==1
# Missing source classification must fail; no 766/767 green status.
try:PortPlan(assets,(),p.dispositions[:1],'mp_x').validate(src);raise AssertionError('missing closure accepted')
except ValueError as e:assert 'closure incomplete' in str(e)

# A destination-only IWD image remains a real top-level XAsset without inventing a fake PC
# source row.  Source closure and destination cardinality are intentionally independent.
orphan=ImageNode(neutral_bc1_image('compass_map_mp_test',2),'image.orphan')
with_orphan=PortPlan(tuple(assets)+(orphan,),(),p.dispositions,'mp_x',[],(),(orphan.symbol,))
ro=with_orphan.validate(src)
assert ro['source_assets']==2 and ro['destination_assets']==2
assert tuple(a.symbol for a in with_orphan.top_level_assets())==('raw.x','image.orphan')
try:
    PortPlan(tuple(assets)+(orphan,),(),p.dispositions,'mp_x',[],(),('raw.x',)).validate(src)
    raise AssertionError('source-emitted/supplemental collision accepted')
except ValueError as e:assert 'already emitted from source' in str(e)
print(r)

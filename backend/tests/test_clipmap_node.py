from __future__ import annotations
import runpy,struct
from pathlib import Path
from cod4porter.assets.clipmap import ClipMapNode
from cod4porter.graph import write_graph,AssetType
from cod4porter.backend.v4_ffio import parse_xasset_list,decode_ps3_pointer

ns=runpy.run_path(str(Path(__file__).with_name('test_pc_clipmap.py')))
clip=ns['clip']
node=ClipMapNode(clip,'clipmap.test')
g=write_graph([node])
assert g.asset_types==(AssetType.CLIPMAP_MP,)
res=g.context.results['clipmap:clipmap.test']
z=g.zone.zone_bytes
rp=res['root_physical']
beu32=lambda o:struct.unpack_from('>I',z,o)[0]
bei32=lambda o:struct.unpack_from('>i',z,o)[0]
beu16=lambda o:struct.unpack_from('>H',z,o)[0]
assert bei32(rp+8)==1
assert beu32(rp+0x18)==1
assert beu32(rp+0x20)==1
assert beu16(rp+0x8c)==1
assert beu32(rp+0xa4)==0xfffffffe and beu32(rp+0xa8)==0xffffffff
assert beu32(rp+0x118)==0x12345678
assert decode_ps3_pointer(beu32(rp)).kind=='packed'
assert decode_ps3_pointer(beu32(rp+0x0c)).kind=='packed'
al=parse_xasset_list(z,'ps3')
assert len(al.assets)==1 and al.assets[0].type_name=='col_map_mp'
# No provenance/fallback descriptors; all data is the canonical graph itself.
assert res['planes']==1 and res['brushes']==1 and res['triangles']==0
print({'passed':True,'zone_bytes':len(z),'block_sizes':g.zone.block_sizes,'root':hex(rp),'relocations':len(g.zone.relocations)})

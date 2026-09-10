from __future__ import annotations
import tempfile
from pathlib import Path
from cod4porter.assets.basic import RawFileNode,StringTableNode,ExternalFxNode
from cod4porter.main_ff import build_main_fastfile,save_main_fastfile
from cod4porter.backend.v4_ffio import read_ps3_fastfile,parse_xasset_list,parse_zone_header

assets=[ExternalFxNode('misc/test','fx.test'),RawFileNode('maps/mp/mp_test.gsc',b'main(){}','raw.test'),StringTableNode('mp/mapsTable.csv',2,1,['mp_test','CUSTOM'],'st.test')]
r=build_main_fastfile(assets,['a',None,'b'])
assert r.deterministic and r.asset_count==3 and r.script_string_count==3
assert len(r.retail_zone)%0x10000==0
assert parse_zone_header(r.retail_zone,'ps3').zone_memory_size==len(r.serialized_zone)-36
al=parse_xasset_list(r.retail_zone,'ps3')
assert [a.type_name for a in al.assets]==['rawfile','stringtable','fx']
with tempfile.TemporaryDirectory() as td:
    p=Path(td)/'mp_test.ff';save_main_fastfile(p,r);doc=read_ps3_fastfile(p);assert doc.zone==r.retail_zone and all(b.raw_bytes==0x10000 for b in doc.blocks)
print({'passed':True,'serialized':len(r.serialized_zone),'retail':len(r.retail_zone),'ff':len(r.fastfile),'assets':r.asset_counts})

from __future__ import annotations
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
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

# Port-shaped sequence with OWNED techsets: the porter lays the same node
# objects out once for the zone-budget measurement and twice inside
# build_main_fastfile.  22.2.1 kept the emission registry's dedup dicts across
# those runs, so the build referenced decl/shader symbols only defined in the
# measurement writer (the mp_gulag 'relocation target ... was never defined').
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_rsx_techset_pipeline import _pc_techset
from cod4porter.assets.localize import LocalizeEntryNode
from cod4porter.assets.techset import OwnedTechniqueSetNode,TechsetEmissionRegistry
from cod4porter.graph import write_graph
from cod4porter.rsx.techset_compile import compile_techniqueset

registry=TechsetEmissionRegistry()
owned=[OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('wc_custom_a')),'techset:aaaa:wc_custom_a',registry),
       OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('mc_custom_b')),'techset:bbbb:mc_custom_b',registry),
       LocalizeEntryNode('MPUI_TEST','Testwert','localize:0001:MPUI_TEST')]
measured=write_graph(owned)          # the budget measurement pass
assert measured.zone.block_sizes
# preserve_order=True mirrors plan.build: the port follows the PC source
# order; canonical_assets stays fail-closed for families without FIX114 proof.
r2=build_main_fastfile(owned,[],preserve_order=True)   # then the real two-run build
assert r2.deterministic and r2.asset_count==3
al2=parse_xasset_list(r2.retail_zone,'ps3')
assert sorted(a.type_name for a in al2.assets)==['localize','techset','techset']
assert registry.emitted_shaders==2 and registry.reused_shaders==6,(registry.emitted_shaders,registry.reused_shaders)

print({'passed':True,'serialized':len(r.serialized_zone),'retail':len(r.retail_zone),'ff':len(r.fastfile),'assets':r.asset_counts,'owned_techset_build':r2.asset_counts})

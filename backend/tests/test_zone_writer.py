from pathlib import Path
import sys, tempfile, json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.graph import write_graph
from cod4porter.assets import RawFileNode,StringTableNode,ExternalFxNode
from cod4porter.backend.v4_ffio import parse_zone_header,parse_xasset_list

def main():
    assets=[RawFileNode('maps/mp/test.gsc',b'hello','raw:test'),StringTableNode('tables/test.csv',2,1,['a',None],'str:test'),ExternalFxNode('fx/test','fx:test')]
    r=write_graph(assets,['one',None,'three'])
    h=parse_zone_header(r.zone.zone_bytes,'ps3');al=parse_xasset_list(r.zone.zone_bytes,'ps3')
    assert len(al.assets)==3 and len(al.script_strings)==3
    assert [a.type_id for a in al.assets]==[0x21,0x22,0x1B]
    assert r.context.results['rawfile:raw:test']['sha256']
    print(json.dumps({'passed':True,'zone_bytes':len(r.zone.zone_bytes),'block_sizes':h.block_sizes,'assets':len(al.assets),'script_strings':len(al.script_strings)},indent=2))
if __name__=='__main__':main()

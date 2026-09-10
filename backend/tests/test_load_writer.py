from pathlib import Path
import os,sys,json,tempfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.load_zone import read_load_document,build_load_zone,compute_load_block_sizes,write_load_fastfile
from cod4porter.backend.v4_ffio import read_ps3_fastfile,parse_zone_header
BASE=ROOT/'reference'/'mp_getaway_load_version09.ff'
HARDWARE=ROOT/'reference'/'mp_getaway_load_fix89b.ff'
# Measured 2026-09-03 on the real Retail PS3 mp_shipment_load.ff.  Its rawfile name
# ("mp_shipment_load") and loadscreen name are each one byte longer than getaway's,
# and the allocator replay lands on its declared Block 4 = 198 exactly.
RETAIL_SHIPMENT_LOAD_BLOCKS=(196,0,0,0,198,0,1223424)
# mp_getaway load content ends Block 4 at 197.  The Fix89B baseline file declares 198
# because it inherited the Shipment header, not because sizes are rounded: both Retail
# main zones declare an ODD Block 4 (17,503,405 and 24,202,937).
GETAWAY_LOAD_BLOCKS=(196,0,0,0,197,0,1223424)
def main():
    d=read_load_document(BASE);reb=build_load_zone(d);orig=read_ps3_fastfile(BASE).zone
    exact=compute_load_block_sizes(d)
    assert exact==GETAWAY_LOAD_BLOCKS,exact
    assert parse_zone_header(reb,'ps3').block_sizes==exact
    assert reb[36:]==orig[36:],next((i for i,(a,b) in enumerate(zip(reb[36:],orig[36:]),36) if a!=b),None)
    with tempfile.TemporaryDirectory() as td:
        out=Path(td)/'mp_getaway_load.ff';r=write_load_fastfile(BASE,out)
        assert r['physical_payload_exact_vs_source'] and r['logical_header_recomputed']
        assert tuple(r['block_sizes'])==exact
    # Retail anchor: regenerating the real Retail PS3 load zone must reproduce it byte for byte.
    retail=os.environ.get('IW3_PORTER_PS3_SHIPMENT_LOAD_FF')
    retail_exact=None
    if retail and Path(retail).is_file():
        rd=read_load_document(retail)
        assert tuple(rd.block_sizes)==RETAIL_SHIPMENT_LOAD_BLOCKS,rd.block_sizes
        assert compute_load_block_sizes(rd)==RETAIL_SHIPMENT_LOAD_BLOCKS
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'mp_shipment_load.ff';rr=write_load_fastfile(retail,out)
            assert rr['exact_zone_vs_source'],rr
            assert read_ps3_fastfile(out).zone==read_ps3_fastfile(retail).zone
        retail_exact=True
    print(json.dumps({'passed':True,'getaway_block4':exact[4],'retail_shipment_load_exact':retail_exact,
                      'zone_bytes':len(reb),'materials':len(d.materials)},indent=2))
if __name__=='__main__':main()

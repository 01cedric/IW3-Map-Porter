from pathlib import Path
import argparse,json,os,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.backend.v4_ffio import read_pc_fastfile,parse_xasset_list
from cod4porter.pc_xmodel import parse_all_xmodels,resolve_xmodel_materials
from cod4porter.pc_material import attach_xmodel_inline_materials_and_tails


def main(argv=None):
    parser=argparse.ArgumentParser(description='Full mp_getaway XModel material causal-replay oracle')
    parser.add_argument('--pc-getaway-ff',type=Path)
    args=parser.parse_args(argv)
    explicit=args.pc_getaway_ff or (Path(os.environ['IW3_PORTER_PC_GETAWAY_FF']) if os.environ.get('IW3_PORTER_PC_GETAWAY_FF') else None)
    fixture=(explicit.expanduser().resolve() if explicit else ROOT/'reference'/'original_pc'/'mp_getaway_original_pc.ff')
    if explicit and not fixture.is_file():raise FileNotFoundError(f'explicit PC Getaway fixture does not exist: {fixture}')
    if not fixture.is_file():
        print(json.dumps({'passed':True,'skipped':True,'reason':'optional full mp_getaway PC fixture not present; use --pc-getaway-ff or IW3_PORTER_PC_GETAWAY_FF'},indent=2))
        raise SystemExit(77)
    started=time.perf_counter()
    doc=read_pc_fastfile(fixture)
    assets=parse_xasset_list(doc.zone,'pc')
    xmodels=parse_all_xmodels(doc.zone,assets)
    attach_xmodel_inline_materials_and_tails(doc.zone,xmodels)
    result=resolve_xmodel_materials(xmodels,assets,{})

    expected={
        0x392768:'mc/mtl_orange',
        0xF7D938:'mc/mtl_ghillie_mesh_mp',
        0x399E5C:'mc/mtl_furniture_basket_mesh',
        0xFEE94C:'mc/mtl_opforce_3hole_ski_mask_sp',
        0x1009918:'mc/mtl_opforce_ski_mask_sp',
        0x1078768:'mc/mtl_arab_regular_ski_mask_sp',
        0xF8392C:'mc/mtl_usmc_body_mp',
        0xFBA830:'mc/mtl_opforce_fullwrap_sp',
        0xFBA834:'mc/mtl_opforce_hands_sp',
        0x103EA60:'mc/mtl_arab_regular_lod_sp',
        0x105A548:'mc/mtl_arab_regular_helmet_hands_sp',
        0x1060684:'mc/mtl_arab_regular_sadiq_sp',
        0x7C748:'mc/mtl_furniture_modern_patio_3_seater',
        0xB67EC:'mc/mtl_pool_towel1_white',
        0x109DDC:'mc/mtl_magazine_english',
    }
    actual={target:result['aliases'][target][0] for target in expected}
    assert actual==expected
    assert result['inline']==308
    assert result['packed']==1006
    assert result['targets']==223
    assert result['reliable_bases']==131
    assert result['block4_replay_aliases']==24
    assert result['resolved_aliases']==223
    assert not result['unresolved']
    assert not result['exact_unresolved']
    assert not result['ambiguous']
    assert result['runtime_fallbacks']==0
    replay=result['block4_replay']
    assert replay['algorithm']=='layered-dag-top2'
    assert replay['groups']==14
    assert replay['remaining_targets']==0
    print(json.dumps({
        'passed':True,'fixture':fixture.name,'seconds':round(time.perf_counter()-started,3),
        'inline':result['inline'],'packed_slots':result['packed'],'unique_targets':result['targets'],
        'causal_bases':result['reliable_bases'],'replay_aliases':result['block4_replay_aliases'],
        'resolved_aliases':result['resolved_aliases'],'unresolved_slots':len(result['unresolved']),
        'checked_aliases':{hex(k):v for k,v in expected.items()},
    },indent=2))


if __name__=='__main__':main()

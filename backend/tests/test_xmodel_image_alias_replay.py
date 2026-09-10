from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.backend.v4_ffio import read_pc_fastfile,parse_xasset_list
from cod4porter.pc_technique import top_level_technique_sets
from cod4porter.technique_binding import bind_all
from cod4porter.pc_xmodel import parse_all_xmodels
from cod4porter.pc_material import attach_xmodel_inline_materials_and_tails
from cod4porter.pc_clipmap import parse_clipmap
from cod4porter.pc_gfxworld import parse_gfxworld
from cod4porter.pc_fx import top_level_fx
from cod4porter.source_plan import build_material_universe


FIXTURE_ENV='IW3_PORTER_PC_GETAWAY_FF'
DEFAULT_FIXTURE=ROOT/'reference'/'original_pc'/'mp_getaway_original_pc.ff'


def fixture_path(argv=None)->Path|None:
    parser=argparse.ArgumentParser(description='Full mp_getaway XModel/Image alias replay oracle')
    parser.add_argument('--pc-getaway-ff',type=Path,help=f'PC mp_getaway FastFile; alternatively set {FIXTURE_ENV}')
    args=parser.parse_args(argv)
    explicit=args.pc_getaway_ff
    if explicit is None and os.environ.get(FIXTURE_ENV):explicit=Path(os.environ[FIXTURE_ENV])
    if explicit is not None:
        path=explicit.expanduser().resolve()
        if not path.is_file():raise FileNotFoundError(f'explicit PC Getaway fixture does not exist: {path}')
        return path
    if DEFAULT_FIXTURE.is_file():return DEFAULT_FIXTURE
    return None


def main(argv=None):
    pc=fixture_path(argv)
    if pc is None:
        print(json.dumps({
            'passed':True,'skipped':True,
            'reason':f'optional full PC Getaway fixture missing; use --pc-getaway-ff or {FIXTURE_ENV}',
            'default_fixture':str(DEFAULT_FIXTURE),
        },indent=2))
        raise SystemExit(77)

    doc=read_pc_fastfile(pc)
    assets=parse_xasset_list(doc.zone,'pc')
    techniques=tuple(top_level_technique_sets(doc.zone,assets))
    bindings=tuple(bind_all(techniques))
    xmodels=parse_all_xmodels(doc.zone,assets)
    xmodel_materials=attach_xmodel_inline_materials_and_tails(doc.zone,xmodels)
    clip=parse_clipmap(doc.zone,'mp_getaway')
    counts={
        'planes':len(clip.planes),'submodels':len(clip.submodels),
        'static_models':len(clip.static_models),'dynamic_models':len(clip.dyn_models),
        'dynamic_brushes':len(clip.dyn_brushes),
    }
    gfx=parse_gfxworld(doc.zone,'mp_getaway',counts)
    fx_refs=tuple(top_level_fx(doc.zone,assets))
    fx_graphs=tuple(x.graph for x in fx_refs if x.source_owned and x.graph is not None)
    universe=build_material_universe(
        doc.zone,assets,techniques,bindings,xmodels,xmodel_materials,gfx,fx_graphs,
        runtime_compatible=True,
    )
    proof=universe.image_proof
    replay=proof['xmodel_block4_cell_replay']

    assert replay['accepted_target_count']==76,replay['accepted_target_count']
    assert replay['accepted_slot_count']==210,replay['accepted_slot_count']
    assert replay['rejected_target_count']==4,replay['rejected_target_count']
    assert not replay['cycles']
    secondary=replay['unanchored_multitarget_replay']
    assert secondary['minimum_targets']==2
    assert secondary['accepted_model_count']==8
    assert secondary['accepted_target_count']==21
    assert secondary['accepted_slot_count']==29
    assert secondary['candidate_model_count']==8
    assert secondary['candidate_base_count']==10
    assert secondary['single_target_candidates_rejected']==5
    assert secondary['bases']=={
        55:1072304,
        195:3156116,
        241:3701684,
        243:3720568,
        281:3950184,
        283:3979660,
        308:16518688,
        324:17084164,
    }
    assert all(row['accepted'] for row in secondary['intervals'])
    assert proof['block4_cell_alias_target_count']==76
    assert proof['block4_cell_alias_targets_used']==76
    assert proof['block4_cell_replay_binding_count']==210
    assert proof['block4_cell_replay_unique_signature_slots']==104
    assert proof['block4_cell_replay_ambiguous_signature_slots']==106
    assert proof['neutral_fallback_count']==60
    assert sum(proof['binding_mode_counts'].values())==1289
    assert proof['binding_mode_counts']['inline']==1019
    assert proof['binding_mode_counts']['xmodel_block4_cell_replay']==210

    print({
        'passed':True,
        'fixture':str(pc),
        'accepted_targets':replay['accepted_target_count'],
        'accepted_slots':replay['accepted_slot_count'],
        'ambiguous_slots_resolved':proof['block4_cell_replay_ambiguous_signature_slots'],
        'secondary_models':secondary['accepted_model_count'],
        'secondary_targets':secondary['accepted_target_count'],
        'neutral_before_secondary':proof['neutral_fallback_count']+secondary['accepted_slot_count'],
        'neutral_after':proof['neutral_fallback_count'],
        'rejected_targets':replay['rejected_target_count'],
    })


if __name__=='__main__':main()

#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.pc_fx import resolve_fx_runner_bindings
from cod4porter.assets.basic import ExternalFxNode
from cod4porter.assets.fx import OwnedFxNode
from cod4porter.assets.sound import SoundNode
from cod4porter.graph import AssetType,write_graph
from cod4porter.sound_binding import resolve_sound_bindings,stage_iwd_sounds
from cod4porter.source_plan import analyze_core_source


def _fixture_paths()->tuple[Path,Path]:
    explicit=os.environ.get('IW3_GETAWAY_PC_FF')
    if explicit:
        pc=Path(explicit)
        iwd=Path(os.environ.get('IW3_GETAWAY_PC_IWD',str(pc.with_suffix('.iwd'))))
        return pc,iwd
    base=ROOT/'reference'/'original_pc'
    return base/'mp_getaway_original_pc.ff',base/'mp_getaway_original_pc.iwd'


def _run_real_getaway_contract()->dict[str,object]:
    pc,iwd=_fixture_paths()
    if not pc.is_file() or not iwd.is_file():
        return {'skipped':True,'reason':'set IW3_GETAWAY_PC_FF/IW3_GETAWAY_PC_IWD to run the retail-source regression'}

    core=analyze_core_source(
        pc,'mp_getaway',(iwd,),runtime_compatible=True,sound_policy='map-owned',
    )

    staged=stage_iwd_sounds(core.iwd_sounds)
    sound=resolve_sound_bindings(core.sound_catalog,staged,preserve_all_lists=False)
    owned_indices=[row.source.source_asset_index for row in sound.owned_lists]
    assert sound.passed,sound.unresolved_owned
    assert owned_indices==list(range(658,672))
    assert sound.source_list_count==160
    assert sound.owned_list_count==14
    assert sound.external_native_count==146
    assert sound.custom_iwd_references==16
    assert sound.runtime_null_soundfile_references==0
    assert sum(
        ap.source.sound_file is not None
        for row in sound.owned_lists for ap in row.aliases
    )==16
    loaded_plans=[
        ap.sound_file for row in sound.owned_lists for ap in row.aliases
        if ap.sound_file is not None
    ]
    assert len(loaded_plans)==16
    # Sixteen alias references use fifteen unique IWD members; one exact
    # LoadedSound identity is reused through a packed B4 pointer-field alias.
    assert sound.staged_payload_count==15
    assert len({row.staged_asset_name.casefold() for row in loaded_plans if row.staged_asset_name})==15
    assert all(
        ap.source.sound_file is None or ap.sound_file is not None
        for row in sound.owned_lists for ap in row.aliases
    )

    # Serialize the real fourteen-list closure, not only the binding report.
    sound_nodes=[
        SoundNode(row,f'sound:getaway:{row.source.source_asset_index:04d}',allow_null_source_soundfiles=False)
        for row in sound.owned_lists
    ]
    sound_graph=write_graph(sound_nodes)
    sound_rows=[sound_graph.context.results['sound:'+node.symbol] for node in sound_nodes]
    assert sum(row['loaded_payloads'] for row in sound_rows)==16
    assert sum(row['unique_loaded_payloads'] for row in sound_rows)==16
    assert sound_graph.zone.block_sizes[3]==0
    for row in sound_rows:
        assert row['root_location'].block==0 and row['root_location'].offset%4==0
        for loaded in row['loaded_sound_rows']:
            assert loaded['symbol_location'].block==4 and loaded['symbol_location'].offset%4==0
            assert loaded['root_location'].block==0 and loaded['root_location'].offset%4==0
            assert loaded['symbol_location']!=loaded['root_location']
            assert loaded['name_location'].block==4
            assert loaded['payload_location'].block==0
            assert loaded['payload_location'].offset==loaded['root_location'].offset+0x1c
            assert loaded['payload_bytes']>0

    runners=resolve_fx_runner_bindings(core.map_name,core.fx_references)
    assert runners.reference_count==3
    assert runners.target_count==2
    assert runners.source_block4_end_anchor is None
    assert [
        (row.owner_source_asset_index,row.element_index,row.serialized_pointer,
         row.source_block4_alias_offset,row.target_source_asset_index)
        for row in runners.bindings
    ]==[
        (686,0,0x40F1717D,0xF1717C,685),
        (686,1,0x40F1717D,0xF1717C,685),
        (694,0,0x40F1A385,0xF1A384,693),
    ]
    assert {row.target_name for row in runners.bindings}=={
        'props/car_glass_med','misc/insects_light_flies_sml',
    }
    relabeled=resolve_fx_runner_bindings('arbitrary_map_name',core.fx_references)
    assert relabeled.bindings==runners.bindings

    # Exercise OwnedFxNode's real graph-reference path on the two source owner
    # graphs.  Target shells stand in only for this isolated writer test; the
    # integrated plan points the same symbols at the real owned top-level FX.
    refs={row.source_asset_index:row for row in core.fx_references}
    target_symbols={685:'fx:test:car_glass_med',693:'fx:test:insects_light_flies_sml'}
    target_nodes=[
        ExternalFxNode(refs[index].name,symbol)
        for index,symbol in target_symbols.items()
    ]
    owner_nodes=[]
    for owner_index in (686,694):
        graph=refs[owner_index].graph
        assert graph is not None
        raw_map={
            raw:(AssetType.FX,target_symbols[target_index])
            for raw,target_index in runners.target_asset_indices_by_raw.items()
            if any(row.owner_source_asset_index==owner_index and row.serialized_pointer==raw for row in runners.bindings)
        }
        owner_nodes.append(OwnedFxNode(graph,{},f'fx:test:owner:{owner_index}',packed_visual_symbols_by_raw=raw_map))
    fx_graph=write_graph(target_nodes+owner_nodes)
    runner_relocations=[row for row in fx_graph.zone.relocations if row.description=='FX single graph visual']
    assert len(runner_relocations)==3
    assert {row.target_symbol for row in runner_relocations}=={
        'fx:test:car_glass_med::$xasset_header',
        'fx:test:insects_light_flies_sml::$xasset_header',
    }

    return {
        'skipped':False,'sound_owned':sound.owned_list_count,
        'sound_external':sound.external_native_count,
        'loaded_sound_references':sound.custom_iwd_references,
        'serialized_loaded_roots':sum(row['unique_loaded_payloads'] for row in sound_rows),
        'runner_references':runners.reference_count,
        'runner_targets':runners.target_count,
        'source_b4_end_anchor':runners.source_block4_end_anchor,
    }


if __name__=='__main__':
    print(_run_real_getaway_contract())

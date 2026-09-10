#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.pc_fx import (
    FxEffectReference,
    FxElement,
    FxReference,
    FxVisual,
    OwnedFxGraph,
    VisualKind,
    resolve_fx_runner_bindings,
)


def _element(index:int,*raws:int)->FxElement:
    null_effect=FxEffectReference(0,None)
    return FxElement(
        index=index,root_bytes=b'\0'*0xfc,element_type=10,
        visual_count=len(raws),velocity_interval_count=0,
        visual_state_interval_count=0,velocity_pointer_raw=0,
        velocity_bytes=b'',visual_state_pointer_raw=0,
        visual_state_bytes=b'',
        visual_pointer_raw=(raws[0] if len(raws)==1 else 0xffffffff),
        visuals=tuple(FxVisual(raw,VisualKind.PACKED_UNRESOLVED) for raw in raws),
        effect_on_impact=null_effect,effect_on_death=null_effect,
        effect_emitted=null_effect,trail_pointer_raw=0,trail=None,
    )


def _reference(index:int,root:int,end:int,name:str,*elements:FxElement)->FxReference:
    graph=OwnedFxGraph(
        root_offset=root,physical_end_offset=end,name=name,
        root_bytes=b'\0'*0x20,looping_element_count=len(elements),
        one_shot_element_count=0,emission_element_count=0,
        elements=tuple(elements),inline_material_roots=(),
    )
    return FxReference(index,root,end,name,True,graph,())


def _nuked_runner_corridors()->tuple[FxReference,...]:
    # Reduced topology from the retail mp_nuked PC zone.  Only the typed
    # XAsset indices, exact physical boundaries, and runner words matter.
    glass_med=_reference(310,0x04DA7F70,0x04DA9246,'props/car_glass_med')
    glass_large=_reference(
        311,0x04DA9246,0x04DA95F4,'props/car_glass_large',
        _element(0,0x4198F74D),_element(1,0x4198F74D),
    )
    powerline_bounds=(
        (327,0x04DB1321,0x04DB4079,'explosions/powerlines_a'),
        (328,0x04DB4079,0x04DB6DBD,'explosions/powerlines_b'),
        (329,0x04DB6DBD,0x04DB87F9,'explosions/powerlines_f'),
        (330,0x04DB87F9,0x04DBBE01,'explosions/powerlines_g'),
        (331,0x04DBBE01,0x04DBECD1,'explosions/powerlines_c'),
    )
    powerlines=tuple(_reference(*row) for row in powerline_bounds)
    camera=_reference(
        332,0x04DBECD1,0x04DBFCBF,'props/securitycamera_explosion',
        _element(2,0x41992A2F,0x41995635,0x41998359,0x41999D75),
        _element(3,0x4199D35D),
    )
    return (glass_med,glass_large,*powerlines,camera)


def _assert_value_error(fragment:str,call):
    try:call()
    except ValueError as exc:
        assert fragment in str(exc),str(exc)
    else:raise AssertionError(f"expected ValueError containing {fragment!r}")


def test_mp_nuked_runner_replay_is_topology_driven_and_complete():
    refs=_nuked_runner_corridors()
    result=resolve_fx_runner_bindings('mp_nuked',refs)
    assert result.reference_count==7
    assert result.target_count==6
    assert result.source_block4_end_anchor is None
    assert [(row.serialized_pointer,row.target_source_asset_index) for row in result.bindings]==[
        (0x4198F74D,310),(0x4198F74D,310),
        (0x41992A2F,327),(0x41995635,328),(0x41998359,329),
        (0x41999D75,330),(0x4199D35D,331),
    ]
    # A map label cannot select or alter the replay.
    assert resolve_fx_runner_bindings('not_mp_nuked',refs).bindings==result.bindings


def test_runner_replay_fails_closed_when_target_corridor_is_broken():
    refs=list(_nuked_runner_corridors())
    broken=refs[4]
    refs[4]=_reference(
        broken.source_asset_index,broken.root_offset+1,
        broken.physical_end_offset,broken.name,
    )
    _assert_value_error(
        'not physically contiguous',
        lambda:resolve_fx_runner_bindings('any_map',refs),
    )


def test_runner_replay_fails_closed_when_alias_order_is_ambiguous():
    target_a=_reference(40,0x1000,0x1100,'target/a')
    target_b=_reference(41,0x1100,0x1200,'target/b')
    owner=_reference(
        42,0x1200,0x1300,'owner',
        _element(0,0x41000201,0x41000101),
    )
    _assert_value_error(
        'not in unique LoadStream order',
        lambda:resolve_fx_runner_bindings('any_map',(target_a,target_b,owner)),
    )


def test_runner_replay_fails_closed_without_consecutive_typed_target():
    target=_reference(50,0x2000,0x2100,'target')
    owner=_reference(52,0x2100,0x2200,'owner',_element(0,0x41000101))
    _assert_value_error(
        'lacks consecutive typed target',
        lambda:resolve_fx_runner_bindings('any_map',(target,owner)),
    )


if __name__=='__main__':
    test_mp_nuked_runner_replay_is_topology_driven_and_complete()
    test_runner_replay_fails_closed_when_target_corridor_is_broken()
    test_runner_replay_fails_closed_when_alias_order_is_ambiguous()
    test_runner_replay_fails_closed_without_consecutive_typed_target()
    print('PASS test_fx_runner_replay')

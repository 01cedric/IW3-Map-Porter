#!/usr/bin/env python3
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from cod4porter.backend.v4_ffio import decode_pc_pointer
from cod4porter.pc_fx import (
    FX_ELEM_SIZE,
    FxEffectReference,
    FxElement,
    FxReference,
    FxRunnerBinding,
    FxRunnerResolution,
    FxVisual,
    OwnedFxGraph,
    VisualKind,
    resolve_fx_material_bindings,
)
from cod4porter.pc_technique import TechniqueSet


def _align(value:int,alignment:int=4)->int:
    return (value+alignment-1)&~(alignment-1)


def _packed_b4(offset:int)->int:
    raw=0x40000001+offset
    pointer=decode_pc_pointer(raw)
    assert (pointer.kind,pointer.block,pointer.offset)==('packed',4,offset)
    return raw


def _zone_with_materials(*rows:tuple[int,str])->bytes:
    size=max(root+0x50+len(name.encode('latin-1'))+1 for root,name in rows)+0x20
    zone=bytearray(size)
    for root,name in rows:
        # Minimal exact PC Material: owned name and no texture/constant/state arrays.
        struct.pack_into('<I',zone,root,0xffffffff)
        encoded=name.encode('latin-1')+b'\0'
        zone[root+0x50:root+0x50+len(encoded)]=encoded
    return bytes(zone)


def _element(index:int,visual:FxVisual)->FxElement:
    null_effect=FxEffectReference(0,None)
    return FxElement(
        index=index,root_bytes=b'\0'*FX_ELEM_SIZE,element_type=0,
        visual_count=1,velocity_interval_count=0,visual_state_interval_count=0,
        velocity_pointer_raw=0,velocity_bytes=b'',visual_state_pointer_raw=0,
        visual_state_bytes=b'',visual_pointer_raw=visual.serialized_pointer,
        visuals=(visual,),effect_on_impact=null_effect,effect_on_death=null_effect,
        effect_emitted=null_effect,trail_pointer_raw=0,trail=None,
    )


def _reference(index:int,root:int,end:int,name:str,*elements:FxElement)->FxReference:
    graph=OwnedFxGraph(
        root_offset=root,physical_end_offset=end,name=name,root_bytes=b'\0'*0x20,
        looping_element_count=len(elements),one_shot_element_count=0,
        emission_element_count=0,elements=tuple(elements),
        inline_material_roots=tuple(
            int(v.material_root)
            for element in elements for v in element.visuals
            if v.kind is VisualKind.MATERIAL and v.material_root is not None
        ),
    )
    return FxReference(index,root,end,name,True,graph,graph.inline_material_roots)


def _anchor(target:FxReference,member_start:int)->FxRunnerResolution:
    return FxRunnerResolution((FxRunnerBinding(
        999,'typed/runner',0,0,0x41000001,member_start,
        target.source_asset_index,target.name,
    ),),None)


def _assert_value_error(fragment:str,call)->None:
    try:call()
    except ValueError as exc:
        assert fragment in str(exc),str(exc)
    else:raise AssertionError(f'expected ValueError containing {fragment!r}')


def test_exact_material_cell_replay_and_causality()->None:
    material_root=0x600
    material_name=',shared_visual'
    zone=_zone_with_materials((material_root,material_name))
    member_start=0x240
    target_name='typed/target'
    element_base=_align(member_start+len(target_name)+1)
    pointer_cell=element_base+0xbc
    packed=_packed_b4(pointer_cell)
    target=_reference(
        10,0x100,0x200,target_name,
        _element(0,FxVisual(0xffffffff,VisualKind.MATERIAL,material_root,material_name)),
    )
    owner=_reference(
        11,0x200,0x300,'typed/owner',
        _element(0,FxVisual(packed,VisualKind.PACKED_UNRESOLVED)),
    )

    result=resolve_fx_material_bindings(zone,(target,owner),_anchor(target,member_start),raw_values=(packed,))
    assert result.reference_count==1
    assert result.target_count==1
    assert result.unresolved_raws==()
    row=result.bindings[0]
    assert (row.serialized_pointer,row.source_block4_pointer_cell_offset)==(packed,pointer_cell)
    assert (row.target_owner_source_asset_index,row.target_material_root,row.target_material_name)==(
        10,material_root,material_name,
    )

    # The same address cannot be consumed by an earlier owner.  Asset/element/
    # visual ordering is part of the causal proof, not merely an address match.
    earlier_owner=_reference(
        9,0x080,0x100,'typed/earlier_owner',
        _element(0,FxVisual(packed,VisualKind.PACKED_UNRESOLVED)),
    )
    rejected=resolve_fx_material_bindings(
        zone,(earlier_owner,target),_anchor(target,member_start),raw_values=(packed,),
    )
    assert rejected.reference_count==0
    assert rejected.unresolved_raws==(packed,)


def test_shell_technique_bridge_reaches_later_fx_owner()->None:
    first_root,second_root=0x700,0x800
    first_material=',first'
    second_material=',second'
    zone=_zone_with_materials((first_root,first_material),(second_root,second_material))
    member_start=0x400
    first_name='typed/first'
    first=_reference(
        20,0x1000,0x1100,first_name,
        _element(0,FxVisual(0xffffffff,VisualKind.MATERIAL,first_root,first_material)),
    )
    first_end=_align(member_start+len(first_name)+1)+FX_ELEM_SIZE+len(first_material)+1
    technique_name=',shell_only'
    technique=TechniqueSet(
        0x1100,technique_name,0,False,0,(0,)*34,21,
    )
    second_physical_root=0x1100+0x94+len(technique_name)+1
    second_name='typed/second'
    second_start=first_end+len(technique_name)+1
    second_cell=_align(second_start+len(second_name)+1)+0xbc
    packed=_packed_b4(second_cell)
    second=_reference(
        22,second_physical_root,0x1300,second_name,
        _element(0,FxVisual(0xffffffff,VisualKind.MATERIAL,second_root,second_material)),
    )
    owner=_reference(
        23,0x1300,0x1400,'typed/consumer',
        _element(0,FxVisual(packed,VisualKind.PACKED_UNRESOLVED)),
    )

    result=resolve_fx_material_bindings(
        zone,(first,second,owner),_anchor(first,member_start),(technique,),(packed,),
    )
    assert result.unresolved_raws==()
    assert result.target_material_roots_by_raw=={packed:second_root}
    assert result.source_member_starts_by_asset[22]==second_start

    # Any non-NULL TechniqueSet slot destroys the shell-only bridge proof.
    non_shell=TechniqueSet(
        technique.root_offset,technique.name,0,False,0,(0xffffffff,)+(0,)*33,21,
    )
    rejected=resolve_fx_material_bindings(
        zone,(first,second,owner),_anchor(first,member_start),(non_shell,),(packed,),
    )
    assert rejected.reference_count==0
    assert rejected.unresolved_raws==(packed,)


def test_owner_cell_collision_fails_closed()->None:
    zone=_zone_with_materials((0x900,',a'),(0xa00,',b'))
    member_start=0x500
    name='same_length'
    left=_reference(
        30,0x2000,0x2100,name,
        _element(0,FxVisual(0xffffffff,VisualKind.MATERIAL,0x900,',a')),
    )
    right=_reference(
        31,0x3000,0x3100,name,
        _element(0,FxVisual(0xffffffff,VisualKind.MATERIAL,0xa00,',b')),
    )
    owner=_reference(
        32,0x3100,0x3200,'consumer',
        _element(0,FxVisual(_packed_b4(_align(member_start+len(name)+1)+0xbc),VisualKind.PACKED_UNRESOLVED)),
    )
    anchors=FxRunnerResolution((
        _anchor(left,member_start).bindings[0],
        _anchor(right,member_start).bindings[0],
    ),None)
    _assert_value_error(
        'owner-cell collision',
        lambda:resolve_fx_material_bindings(zone,(left,right,owner),anchors),
    )


if __name__=='__main__':
    test_exact_material_cell_replay_and_causality()
    test_shell_technique_bridge_reaches_later_fx_owner()
    test_owner_cell_collision_fails_closed()
    print('PASS test_fx_material_replay')

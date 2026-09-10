"""A type-10 (runner) FX visual is an FxEffectDefRef: the PS3 loader reads a *name*.

EBOOT ``Load_FxElemDefVisuals`` (0xD77D0) dispatches a runner's single visual to
``Load_FxEffectDefRef`` (0xCE580), which is ``Load_XString`` followed by
``DB_ConvertFxEffectDefRef`` (0xDF9D8) - ``DB_FindXAssetHeader(FX, name)``.  Retail
mp_crash stores those visuals as packed aliases to the *name string* of the target effect
('dust/dust_ground_gust', 'props/car_glass_med', ...).

The porter aliased the target effect's XAssetList header cell instead - correct for an
asset handle, wrong for a name - so the engine read the asset list as a string and looked
up effects called ``"\\x10`\\x1a\\xbc"``, which the emulated database resolved to
``misc/missing_fx`` (2 runners in mp_getaway, 6 in mp_nuked).  Now the name is written
inline behind the element (FOLLOWING), the same loader path retail's aliases take.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assets.fx import OwnedFxNode  # noqa: E402
from cod4porter.graph import AssetType, GraphContext, header_symbol  # noqa: E402
from cod4porter.pc_fx import (  # noqa: E402
    FxEffectReference, FxElement, FxVisual, OwnedFxGraph, VisualKind, FX_ELEM_SIZE, FX_ROOT_SIZE,
)
from cod4porter.zone_writer import (  # noqa: E402
    RelocatingZoneWriter, FOLLOWING, LARGE_BLOCK, TEMP_BLOCK,
)

RUNNER_RAW = 0x4198F74D
TARGET_NAME = 'props/car_glass_med'
TARGET_SYMBOL = 'fx:0310:props_car_glass_med'


def _runner_element(index: int, *raws: int) -> FxElement:
    null_effect = FxEffectReference(0, None)
    root = bytearray(FX_ELEM_SIZE)
    root[0xB0] = 10                     # elemType = FX_ELEM_TYPE_RUNNER
    root[0xB1] = len(raws)              # visualCount
    return FxElement(
        index=index, root_bytes=bytes(root), element_type=10,
        visual_count=len(raws), velocity_interval_count=0, visual_state_interval_count=0,
        velocity_pointer_raw=0, velocity_bytes=b'', visual_state_pointer_raw=0,
        visual_state_bytes=b'',
        visual_pointer_raw=(raws[0] if len(raws) == 1 else 0xFFFFFFFF),
        visuals=tuple(FxVisual(raw, VisualKind.PACKED_UNRESOLVED) for raw in raws),
        effect_on_impact=null_effect, effect_on_death=null_effect,
        effect_emitted=null_effect, trail_pointer_raw=0, trail=None,
    )


def _write(elements) -> tuple[bytes, dict, int]:
    graph = OwnedFxGraph(
        root_offset=0x1000, physical_end_offset=0x2000, name='props/car_glass_large',
        root_bytes=b'\0' * FX_ROOT_SIZE, looping_element_count=len(elements),
        one_shot_element_count=0, emission_element_count=0,
        elements=tuple(elements), inline_material_roots=(),
    )
    node = OwnedFxNode(
        graph, {}, 'fx:0311:props_car_glass_large',
        packed_visual_symbols_by_raw={RUNNER_RAW: (AssetType.FX, TARGET_SYMBOL)},
        packed_visual_names_by_raw={RUNNER_RAW: TARGET_NAME},
    )
    nodes = {node.symbol: node, TARGET_SYMBOL: object()}
    context = GraphContext(nodes=nodes, top_level_symbols=(TARGET_SYMBOL,))
    writer = RelocatingZoneWriter()
    writer.push_block(LARGE_BLOCK)
    writer.define_symbol(header_symbol(TARGET_SYMBOL))
    writer.reserve_in_block(4, 'target FX XAssetHeader cell fixture')
    writer.pop_block()
    writer.push_block(TEMP_BLOCK)
    writer.align_logical_only(4)
    node.write(writer, context)
    writer.pop_block()
    output = writer.build()
    result = context.results['fx.owned:' + node.symbol]
    return output.zone_bytes, result, output.zone_bytes.find(b'props/car_glass_large\0')


# --- the dependency on the target effect survives: it must be linked before the lookup ---
def test_runner_target_stays_a_dependency():
    elements = [_runner_element(0, RUNNER_RAW)]
    graph = OwnedFxGraph(
        root_offset=0, physical_end_offset=0, name='x', root_bytes=b'\0' * FX_ROOT_SIZE,
        looping_element_count=1, one_shot_element_count=0, emission_element_count=0,
        elements=tuple(elements), inline_material_roots=(),
    )
    node = OwnedFxNode(graph, {}, 'fx:0001:x',
                       packed_visual_symbols_by_raw={RUNNER_RAW: (AssetType.FX, TARGET_SYMBOL)},
                       packed_visual_names_by_raw={RUNNER_RAW: TARGET_NAME})
    assert [(d.target_symbol, d.expected_type) for d in node.dependencies] == [(TARGET_SYMBOL, AssetType.FX)]


# --- single runner visual: +0xBC is FOLLOWING and the name string follows the element ---
def test_single_runner_visual_is_an_inline_name():
    zone, result, name_pos = _write([_runner_element(0, RUNNER_RAW)])
    # locate the element by its elemType/visualCount bytes
    elem = zone.find(bytes([10, 1, 0, 0]) + b'\0' * 8 + struct.pack('>I', FOLLOWING), name_pos) - 0xB0
    assert elem > 0
    assert struct.unpack_from('>I', zone, elem + 0xBC)[0] == FOLLOWING
    payload = zone[elem + FX_ELEM_SIZE:]
    assert payload.startswith(TARGET_NAME.encode() + b'\0'), payload[:32]
    assert result['string_visuals'] == 1
    # no packed alias may point at the target's XAssetList cell any more
    assert struct.pack('>I', 0x80000001) not in zone[elem:elem + FX_ELEM_SIZE]


# --- runner visual arrays (retail mp_crash has a 3-entry one): every entry is a name ---
def test_runner_visual_array_entries_are_inline_names():
    zone, result, name_pos = _write([_runner_element(0, RUNNER_RAW, RUNNER_RAW, RUNNER_RAW)])
    elem = zone.find(bytes([10, 3, 0, 0]) + b'\0' * 8 + struct.pack('>I', FOLLOWING), name_pos) - 0xB0
    assert elem > 0
    assert struct.unpack_from('>I', zone, elem + 0xBC)[0] == FOLLOWING       # the array follows
    payload = zone[elem + FX_ELEM_SIZE:]
    # 3 x FOLLOWING pointer cells, then the three names in order
    assert struct.unpack_from('>III', payload, 0) == (FOLLOWING, FOLLOWING, FOLLOWING)
    names = payload[12:].split(b'\0')[:3]
    assert names == [TARGET_NAME.encode()] * 3
    assert result['string_visuals'] == 3


# --- a runner without a resolvable target name is a hard error, never a silent alias ---
def test_runner_without_target_name_is_rejected():
    elements = [_runner_element(0, RUNNER_RAW)]
    graph = OwnedFxGraph(
        root_offset=0, physical_end_offset=0, name='x', root_bytes=b'\0' * FX_ROOT_SIZE,
        looping_element_count=1, one_shot_element_count=0, emission_element_count=0,
        elements=tuple(elements), inline_material_roots=(),
    )
    node = OwnedFxNode(graph, {}, 'fx:0001:x',
                       packed_visual_symbols_by_raw={RUNNER_RAW: (AssetType.FX, TARGET_SYMBOL)})
    try:
        node._initial_visual_pointer(elements[0])
    except ValueError as exc:
        assert 'runner visual has no target effect name' in str(exc)
    else:
        raise AssertionError('missing runner name accepted')


if __name__ == '__main__':
    test_runner_target_stays_a_dependency()
    test_single_runner_visual_is_an_inline_name()
    test_runner_visual_array_entries_are_inline_names()
    test_runner_without_target_name_is_rejected()
    print('ok')

#!/usr/bin/env python3
"""Per-material auto-quarantine: a planning failure costs the surface, not the map.

Runtime-compatible ports collect any material whose planning raises (constant
contracts, platform state, image identity - present or FUTURE error classes)
and rebind it to the engine's ,$default exactly like the hardware-proven
CP12-A closure.  Full ports (quarantine=None) still stop on the first error.
"""
from __future__ import annotations
import json
import sys
from dataclasses import replace as dc_replace
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.material import MaterialRecord, MaterialStateConversion
from cod4porter.material_planning import MaterialUniverse, PlannedMaterial, plan_materials
from cod4porter.material_graph import build_material_graph
from cod4porter.technique_binding import TechniqueBinding, TechniqueEvidence


def rec(name, root=1):
    return MaterialRecord(root, name, 0, 0, 0, 0, 0, 0, 0, bytes([255] * 34),
                          0, 0, 'None', 0, 0, 0, 0, 0, (), (), (), {})


def test_plan_materials_quarantine_collects_instead_of_raising():
    # An empty asset list resolves no TechniqueSet alias, so plan_material
    # raises its very first proof error for every material.
    asset_list = SimpleNamespace(assets=(), structural_index=None)
    bad = rec('wc/broken_surface', root=0x100)
    proof = {'passed': True, 'bindings': (), 'unresolved': []}

    quarantine = []
    planned = plan_materials([(7, bad)], asset_list, (), (),
                             allow_runtime_neutral=True,
                             resolved_image_proof=proof, quarantine=quarantine)
    assert planned == ()
    assert len(quarantine) == 1, quarantine
    row = quarantine[0]
    assert row['name'] == 'wc/broken_surface' and row['source_asset_index'] == 7
    assert 'TechniqueSet' in row['reason'], row

    # Full-port semantics unchanged: no collector -> the same error stops.
    try:
        plan_materials([(7, bad)], asset_list, (), (),
                       allow_runtime_neutral=False, resolved_image_proof=proof)
    except ValueError as error:
        assert 'TechniqueSet' in str(error)
    else:
        raise AssertionError('full port must keep failing closed')


def test_graph_rebinds_quarantined_materials_to_default():
    binding = TechniqueBinding('2d', '2d', False, TechniqueEvidence.CANONICAL_STOCK, True, 'x', 5)
    state = MaterialStateConversion(bytes([255] * 26), (), 0, 0, 0)
    healthy = PlannedMaterial(rec('foo'), state, bytes(0x38), binding, (), 10)
    quarantined = {'name': 'wc/broken_surface', 'root': 0x100,
                   'source_asset_index': 7, 'reason': 'missing constants 0xdeadbeef'}
    universe = MaterialUniverse((), (), (), (), (healthy.source,), frozenset(),
                                {'passed': True, 'bindings': (), 'unresolved': []},
                                (healthy,), (), {'packed': 0, 'unresolved': []},
                                (quarantined,))
    graph = build_material_graph(universe, {})
    default_symbol = graph.material_symbol_by_name['default']
    assert graph.material_symbol_by_name['wc/broken_surface'] == default_symbol
    assert graph.material_symbol_by_name['foo'] != default_symbol
    assert any(getattr(n, 'name', None) == '$default' for n in graph.material_nodes)
    assert graph.diagnostics['materials_quarantined'] == 1
    rows = graph.diagnostics['materials_quarantined_rows']
    assert rows[0]['name'] == 'wc/broken_surface' and 'missing constants' in rows[0]['reason']

    # A healthy identity with the same name keeps priority over quarantine.
    shadowed = dc_replace(universe, quarantined_materials=(
        {'name': 'foo', 'root': 1, 'source_asset_index': None, 'reason': 'late duplicate'},))
    graph2 = build_material_graph(shadowed, {})
    assert graph2.material_symbol_by_name['foo'] != graph2.material_symbol_by_name['default']


def main():
    test_plan_materials_quarantine_collects_instead_of_raising()
    test_graph_rebinds_quarantined_materials_to_default()
    print(json.dumps({'passed': True, 'tests': 2}))


if __name__ == '__main__':
    main()

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from cod4porter.assets.basic import LightDefNode
from cod4porter.assets.image import ExternalImageNode
from cod4porter.backend.v4_ffio import parse_xasset_list
from cod4porter.graph import (
    OwnedPointerKind,
    OwnershipEdge,
    owned_alias_symbol,
)
from cod4porter.main_ff import build_main_fastfile, save_main_fastfile
from cod4porter.plan import DispositionKind, PortPlan, SourceDisposition
from cod4porter.readback import inspect_main_fastfile
from cod4porter.zone_writer import INSERT,INSERT_BLOCK


def make_fixture():
    image = ExternalImageNode('attenuation/linear', 'image:attenuation')
    owner = LightDefNode('light_owner', 3, 7, image.symbol, 'lightdef:owner')
    reuser = LightDefNode('light_reuser', 3, 8, image.symbol, 'lightdef:reuser')
    edge = OwnershipEdge(
        owner.symbol,
        image.symbol,
        'attenuation',
        OwnedPointerKind.INSERT,
        'LightDef owns attenuation image',
        0,
    )
    return image, owner, reuser, edge


def test_support_node_is_not_promoted_to_global_xasset() -> None:
    image, owner, reuser, edge = make_fixture()
    result = build_main_fastfile(
        (owner, reuser, image),
        preserve_order=True,
        top_level_assets=(owner, reuser),
        ownership_edges=(edge,),
    )

    xassets = parse_xasset_list(result.retail_zone, 'ps3')
    assert result.asset_count == 2
    assert [asset.type_name for asset in xassets.assets] == ['lightdef', 'lightdef']
    assert result.graph_results['graph.placement']['written_nodes'] == 3
    assert result.graph_results['graph.placement']['top_level_symbols'] == [owner.symbol, reuser.symbol]
    assert result.graph_results[f'graph.owned:{image.symbol}']['owner_symbol'] == owner.symbol


def test_first_reference_is_insert_and_reuse_is_packed_alias() -> None:
    image, owner, reuser, edge = make_fixture()
    result = build_main_fastfile(
        (owner, reuser, image),
        preserve_order=True,
        top_level_assets=(owner, reuser),
        ownership_edges=(edge,),
    )

    owner_root = result.graph_results[f'lightdef:{owner.symbol}']['root_physical']
    reuser_root = result.graph_results[f'lightdef:{reuser.symbol}']['root_physical']
    owner_pointer = struct.unpack_from('>I', result.serialized_zone, owner_root + 4)[0]
    reuser_pointer = struct.unpack_from('>I', result.serialized_zone, reuser_root + 4)[0]

    assert owner_pointer == INSERT
    assert reuser_pointer not in (0, 0xFFFFFFFF, INSERT)

    relocation = next(
        row for row in result.relocation_rows
        if row['physical_field_offset'] == reuser_root + 4
    )
    assert relocation['target_symbol'] == owned_alias_symbol(image.symbol)
    # Follow the production PS3 insert-cell contract instead of pinning the old
    # pre-C10 block-3 assumption.  Block 3 is now the delayed-image FIFO and
    # persistent INSERT aliases live in INSERT_BLOCK.
    assert relocation['target_block'] == INSERT_BLOCK
    assert relocation['serialized_pointer'] == reuser_pointer



def test_independent_readback_checks_owned_support_roots() -> None:
    image, owner, reuser, edge = make_fixture()
    dispositions = (
        SourceDisposition(0, 0x13, DispositionKind.EMITTED, owner.symbol, owner.type, 'owner fixture'),
        SourceDisposition(1, 0x13, DispositionKind.EMITTED, reuser.symbol, reuser.type, 'reuser fixture'),
    )
    plan = PortPlan(
        (owner, reuser, image),
        (),
        dispositions,
        'owner-aware-fixture',
        ownership_edges=(edge,),
    )
    result = plan.build()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'owner-aware.ff'
        save_main_fastfile(path, result, verify=True)
        report = inspect_main_fastfile(path, plan, result)
    assert report.passed, report
    assert len(report.family_checks) == 3
    support = [row for row in report.family_checks if row['placement'] == 'owned-support']
    assert len(support) == 1
    assert support[0]['symbol'] == image.symbol
    assert support[0]['passed']


def test_owner_graph_is_deterministic() -> None:
    image, owner, reuser, edge = make_fixture()
    first = build_main_fastfile(
        (owner, reuser, image),
        preserve_order=True,
        top_level_assets=(owner, reuser),
        ownership_edges=(edge,),
    )
    second = build_main_fastfile(
        (owner, reuser, image),
        preserve_order=True,
        top_level_assets=(owner, reuser),
        ownership_edges=(edge,),
    )
    assert first.serialized_zone == second.serialized_zone
    assert first.fastfile_sha256 == second.fastfile_sha256


def main() -> None:
    test_support_node_is_not_promoted_to_global_xasset()
    test_first_reference_is_insert_and_reuse_is_packed_alias()
    test_independent_readback_checks_owned_support_roots()
    test_owner_graph_is_deterministic()
    print({'passed': True, 'top_level_assets': 2, 'support_nodes': 1, 'ownership_edges': 1})


if __name__ == '__main__':
    main()

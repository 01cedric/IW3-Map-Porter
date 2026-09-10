from __future__ import annotations

from types import SimpleNamespace

from cod4porter.image_alias_replay import replay_xmodel_image_aliases
from cod4porter.backend.v4_ffio import FOLLOWING, INSERT, encode_pc_packed
from cod4porter.material_planning import resolve_image_identities


def tex(*, name: str | None, raw: int, signature: tuple[int, int, int, int, int]):
    return SimpleNamespace(
        inline_image_name=name,
        inline_image_root=0x800 if name else None,
        serialized_resource_pointer=raw,
        resource_pointer=raw,
        name_hash=signature[0],
        name_start=signature[1],
        name_end=signature[2],
        sampler=signature[3],
        semantic=signature[4],
    )


def mat(root: int, name: str, textures: tuple, *, water: bool = False):
    return SimpleNamespace(
        root_offset=root,
        name=name,
        textures=textures,
        constants=(),
        state_bits=(),
        contains_water=water,
        water_by_texture={},
    )


def resolution(base: int = 0x100):
    return {
        'causal_base_diagnostics': {
            'chain': {'selected_rows': ({'model_index': 0, 'base': base},)},
        },
        'block4_replay': {'rows': ()},
    }


def test_pointer_alias_precedes_ambiguous_signature() -> None:
    sig = (0x1111, 0, 7, 3, 2)
    source = mat(0x200, 'mat/a', (tex(name='image_a', raw=0xFFFFFFFF, signature=sig),))
    competitor = mat(0x300, 'mat/b', (tex(name='image_b', raw=0xFFFFFFFF, signature=sig),))
    # base + one Material* cell + XString('mat/a') + align(4) + image member offset
    target = 0x114
    request = mat(0x400, 'mat/request', (tex(name=None, raw=encode_pc_packed(4, target), signature=sig),))
    xmodel = SimpleNamespace(
        model=SimpleNamespace(name='xmodel_test', surfaces=(object(),)),
        inline_material_offsets=(source.root_offset,),
    )

    replay = replay_xmodel_image_aliases((xmodel,), (source,), (source, competitor, request), resolution())
    assert replay['aliases'] == {target: 'image_a'}
    assert replay['accepted_target_count'] == 1
    assert replay['accepted_slot_count'] == 1

    out = resolve_image_identities(
        (source, competitor, request), SimpleNamespace(assets=()), allow_runtime_neutral=False,
        block4_cell_aliases=replay['aliases'],
    )
    binding = next(b for b in out['bindings'] if b.material_root == request.root_offset)
    assert binding.image_name == 'image_a'
    assert binding.mode == 'xmodel_block4_cell_replay'
    assert out['block4_cell_replay_binding_count'] == 1
    assert not out['unresolved']


def test_signature_cross_check_rejects_false_address_collision() -> None:
    source_sig = (0x1111, 0, 7, 3, 2)
    other_sig = (0x2222, 0, 7, 3, 2)
    source = mat(0x200, 'mat/a', (tex(name='image_a', raw=0xFFFFFFFF, signature=source_sig),))
    other = mat(0x300, 'mat/other', (tex(name='image_b', raw=0xFFFFFFFF, signature=other_sig),))
    target = 0x114
    request = mat(0x400, 'mat/request', (tex(name=None, raw=encode_pc_packed(4, target), signature=other_sig),))
    xmodel = SimpleNamespace(
        model=SimpleNamespace(name='xmodel_test', surfaces=(object(),)),
        inline_material_offsets=(source.root_offset,),
    )
    replay = replay_xmodel_image_aliases((xmodel,), (source,), (source, other, request), resolution())
    assert replay['aliases'] == {}
    assert replay['rejected_target_count'] == 1


def test_insert_aware_direct_cell_outranks_texture_signature() -> None:
    owner_a_sig = (0x1111, 0, 7, 3, 2)
    owner_b_sig = (0x2222, 0, 7, 3, 2)
    request_sig = (0x3333, 0, 7, 3, 2)
    first = mat(0x200, 'm1', (tex(name='image_a', raw=FOLLOWING, signature=owner_a_sig),))
    second = mat(0x300, 'm2', (tex(name='image_b', raw=FOLLOWING, signature=owner_b_sig),))
    second.textures[0].inline_image_root = 0x900
    competitor = mat(0x400, 'other', (tex(name='image_c', raw=FOLLOWING, signature=request_sig),))
    # The first image's INSERT name and loadDef pointers each reserve b4+4. Therefore the
    # second MaterialTextureDef::image cell is 0x134, not the legacy replay's 0x12C.
    request = mat(0x500, 'request', (tex(name=None, raw=encode_pc_packed(4, 0x134), signature=request_sig),))
    xmodel = SimpleNamespace(
        model=SimpleNamespace(name='xmodel_insert', surfaces=(object(), object())),
        inline_material_offsets=(first.root_offset, second.root_offset),
        material_handle_pointers=(FOLLOWING, FOLLOWING),
    )
    zone = bytearray(0xA00)
    for material in (first, second):
        zone[material.root_offset:material.root_offset + 4] = FOLLOWING.to_bytes(4, 'little')
        zone[material.root_offset + 0x44:material.root_offset + 0x48] = FOLLOWING.to_bytes(4, 'little')
    for image_root in (0x800, 0x900):
        zone[image_root + 4:image_root + 8] = INSERT.to_bytes(4, 'little')
        zone[image_root + 0x20:image_root + 0x24] = INSERT.to_bytes(4, 'little')

    replay = replay_xmodel_image_aliases(
        (xmodel,), (first, second), (first, second, competitor, request), resolution(),
        pc_zone=bytes(zone),
    )
    assert replay['insert_aware_layout']
    assert replay['insert_alias_cells_replayed'] == 4
    assert replay['aliases'][0x134] == 'image_b'
    assert replay['exact_direct_alias_targets'] == (0x134,)

    proof = resolve_image_identities(
        (first, second, competitor, request), SimpleNamespace(assets=(), asset_pool_offset=0),
        block4_cell_aliases=replay['aliases'],
        block4_exact_cell_aliases=replay['exact_direct_alias_targets'],
    )
    binding = next(b for b in proof['bindings'] if b.material_root == request.root_offset)
    assert binding.image_name == 'image_b'
    assert binding.mode == 'xmodel_block4_cell_replay'


def test_packed_cell_chain_is_resolved_transitively() -> None:
    sig = (0x3333, 1, 9, 4, 5)
    # With two surface handles, the first image cell is 0x114. The owned image name advances
    # block 4 before the second MaterialTextureDef table, whose image cell is therefore 0x130.
    first = mat(0x210, 'm1', (tex(name='image_chain', raw=0xFFFFFFFF, signature=sig),))
    second = mat(0x220, 'm2', (tex(name=None, raw=encode_pc_packed(4, 0x114), signature=sig),))
    request = mat(0x230, 'm3', (tex(name=None, raw=encode_pc_packed(4, 0x130), signature=sig),))
    xmodel = SimpleNamespace(
        model=SimpleNamespace(name='xmodel_chain', surfaces=(object(), object())),
        inline_material_offsets=(first.root_offset, second.root_offset),
    )
    replay = replay_xmodel_image_aliases((xmodel,), (first, second), (first, second, request), resolution())
    assert replay['aliases'][0x130] == 'image_chain'
    assert replay['packed_edge_cell_count'] == 1


def test_unique_multitarget_unanchored_base_is_recovered() -> None:
    sig_a = (0xA001, 0, 3, 3, 2)
    sig_b = (0xB002, 4, 7, 3, 5)
    owner = mat(0x200, 'm', (
        tex(name='image_a', raw=0xFFFFFFFF, signature=sig_a),
        tex(name='image_b', raw=0xFFFFFFFF, signature=sig_b),
    ))
    # Unknown model base 0x200: one Material* handle, XString "m", align(4), then
    # MaterialTextureDef::image cells at +0x10 and +0x1C.
    request = mat(0x400, 'request', (
        tex(name=None, raw=encode_pc_packed(4, 0x210), signature=sig_a),
        tex(name=None, raw=encode_pc_packed(4, 0x21C), signature=sig_b),
    ))
    anchor = SimpleNamespace(
        model=SimpleNamespace(name='anchor', surfaces=(object(),)),
        inline_material_offsets=(None,),
    )
    unknown = SimpleNamespace(
        model=SimpleNamespace(name='unknown', surfaces=(object(),)),
        inline_material_offsets=(owner.root_offset,),
    )
    replay = replay_xmodel_image_aliases(
        (anchor, unknown), (owner,), (owner, request), resolution(0x100),
    )
    secondary = replay['unanchored_multitarget_replay']
    assert secondary['accepted_model_count'] == 1
    assert secondary['accepted_target_count'] == 2
    assert secondary['bases'] == {1: 0x200}
    assert replay['aliases'][0x210] == 'image_a'
    assert replay['aliases'][0x21C] == 'image_b'


def test_single_target_unanchored_candidate_is_rejected() -> None:
    sig = (0xC003, 0, 5, 3, 2)
    owner = mat(0x200, 'm', (
        tex(name='image_single', raw=0xFFFFFFFF, signature=sig),
    ))
    request = mat(0x400, 'request', (
        tex(name=None, raw=encode_pc_packed(4, 0x210), signature=sig),
    ))
    anchor = SimpleNamespace(
        model=SimpleNamespace(name='anchor', surfaces=(object(),)),
        inline_material_offsets=(None,),
    )
    unknown = SimpleNamespace(
        model=SimpleNamespace(name='unknown', surfaces=(object(),)),
        inline_material_offsets=(owner.root_offset,),
    )
    replay = replay_xmodel_image_aliases(
        (anchor, unknown), (owner,), (owner, request), resolution(0x100),
    )
    secondary = replay['unanchored_multitarget_replay']
    assert secondary['minimum_targets'] == 2
    assert secondary['accepted_model_count'] == 0
    assert secondary['accepted_target_count'] == 0
    assert secondary['single_target_candidates_rejected'] >= 1
    assert 0x210 not in replay['aliases']


def main() -> None:
    test_pointer_alias_precedes_ambiguous_signature()
    test_signature_cross_check_rejects_false_address_collision()
    test_insert_aware_direct_cell_outranks_texture_signature()
    test_packed_cell_chain_is_resolved_transitively()
    test_unique_multitarget_unanchored_base_is_recovered()
    test_single_target_unanchored_candidate_is_rejected()
    print('test_image_alias_replay: PASS')


if __name__ == '__main__':
    main()

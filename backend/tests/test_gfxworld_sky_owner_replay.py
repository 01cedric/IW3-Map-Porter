from __future__ import annotations

from types import SimpleNamespace

from cod4porter.backend.v4_ffio import FOLLOWING, INSERT
from cod4porter.image_alias_replay import replay_gfxworld_sky_image_owner_alias


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def put32(zone: bytearray, offset: int, value: int) -> None:
    zone[offset:offset + 4] = int(value).to_bytes(4, 'little')


def branch(name: str, start: int, end: int):
    return SimpleNamespace(name=name, start=start, end=end)


def image(label: str, root: int, name: str, end: int):
    return SimpleNamespace(label=label, root_offset=root, name=name, end_offset=end)


def fixture(*, sky_name: str = 'arbitrary/sky_identity', reflection_count: int = 2):
    zone = bytearray(0x1000)
    root = 0x20
    sky_root = 0x300
    sky_end = 0x400
    sun_start, sun_end = sky_end, sky_end + 0x40
    reflection_start = sun_end
    reflection_roots_end = reflection_start + reflection_count * 0x10

    put32(zone, root + 0x28, INSERT)
    put32(zone, root + 0xC8, FOLLOWING)
    put32(zone, root + 0xE8, FOLLOWING)
    put32(zone, root + 0xF4, FOLLOWING)
    put32(zone, root + 0xF8, FOLLOWING)
    put32(zone, root + 0x104, FOLLOWING)
    put32(zone, sky_root + 0x20, FOLLOWING)
    put32(zone, sky_root + 0x04, INSERT)
    put32(zone, sun_start + 0x3C, 0)

    images = [image('skyImage', sky_root, sky_name, sky_end)]
    physical = reflection_roots_end
    reflection_names = []
    for index in range(reflection_count):
        put32(zone, reflection_start + index * 0x10 + 0x0C, FOLLOWING)
        name = f'arbitrary/reflection_{index}'
        reflection_names.append(name)
        image_root = physical
        put32(zone, image_root + 0x20, FOLLOWING)
        put32(zone, image_root + 0x04, INSERT)
        physical += 0x30
        images.append(image(f'reflectionProbe[{index}]', image_root, name, physical))

    reflection_end = physical
    plane_count = 3
    plane_start = reflection_end
    plane_end = plane_start + plane_count * 0x14
    node_count = 5
    node_start = plane_end
    node_end = node_start + node_count * 2
    cells_start = node_end
    branches = (
        branch('sunLight', sun_start, sun_end),
        branch('reflectionProbes', reflection_start, reflection_end),
        branch('planes', plane_start, plane_end),
        branch('nodes', node_start, node_end),
        branch('cells/AABB/portals', cells_start, cells_start + 0x38),
    )
    report = SimpleNamespace(
        root_offset=root,
        reflection_probe_count=reflection_count,
        plane_count=plane_count,
        node_count=node_count,
        cell_count=1,
        full=SimpleNamespace(images=tuple(images), branches=branches),
    )

    target = 0x100
    cursor = target + 4
    cursor += len(sky_name.encode('latin-1')) + 1
    cursor = align(cursor, 4) + 4
    cursor = align(cursor, 4) + 0x40
    cursor = align(cursor, 4) + reflection_count * 0x10
    for name in reflection_names:
        cursor += len(name.encode('latin-1')) + 1
        cursor = align(cursor, 4) + 4
    cursor = align(cursor, 4) + plane_count * 0x14
    cursor = align(cursor, 2) + node_count * 2
    cell_base = align(cursor, 4)
    requests = {target: ({'material_root': sky_root + 0x1000, 'texture_index': 0},)}
    return bytes(zone), report, target, cell_base, requests


def test_arbitrary_identity_and_counts_close_exactly() -> None:
    zone, report, target, cell_base, requests = fixture(
        sky_name='unrelated/custom_sky', reflection_count=2,
    )
    replay = replay_gfxworld_sky_image_owner_alias(zone, report, cell_base, requests)
    assert replay['aliases'] == {target: 'unrelated/custom_sky'}
    assert replay['exact_direct_alias_targets'] == (target,)
    row = replay['provenance'][target]
    assert row['predicted_cell_base'] == row['proven_cell_base'] == cell_base
    assert row['reflection_image_count'] == 2


def test_anchor_mismatch_and_non_insert_owner_fail_closed() -> None:
    zone, report, target, cell_base, requests = fixture()
    replay = replay_gfxworld_sky_image_owner_alias(zone, report, cell_base + 4, requests)
    assert replay['aliases'] == {}
    assert 'candidate count' in replay['reason']

    changed = bytearray(zone)
    put32(changed, report.root_offset + 0x28, FOLLOWING)
    replay = replay_gfxworld_sky_image_owner_alias(bytes(changed), report, cell_base, requests)
    assert replay['aliases'] == {}
    assert replay['reason'] == 'skyImage is not INSERT'


def main() -> None:
    test_arbitrary_identity_and_counts_close_exactly()
    test_anchor_mismatch_and_non_insert_owner_fail_closed()
    print('test_gfxworld_sky_owner_replay: PASS')


if __name__ == '__main__':
    main()

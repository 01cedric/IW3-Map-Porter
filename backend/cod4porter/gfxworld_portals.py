"""Resolve PC portal neighbours before relocating the cell array to PS3.

CoD4 PS3 Load_GfxPortal (0xC54B0) reads 0x44 bytes, resolves the cell
pointer at +0x20, and reads vertexCount (+0x28) vec3s through +0x24.
Load_GfxCellArray (0xC56F0) uses a 0x38 stride. These are IW3 layouts;
the smaller IW4 portal structure is not interchangeable.
"""
from dataclasses import dataclass
from .backend.v4_ffio import decode_pc_pointer
from .gfxworld_aabb import resolve_aabb_reuse, u16, u32, i32, ensure, require_inline
from .pc_gfxworld import PC_CELL, PC_AABB, PC_PORTAL


@dataclass(frozen=True)
class PortalBinding:
    record_offset: int
    source_cell: int
    target_cell: int
    vertices_offset: int
    vertex_count: int


def resolve_portals(zone, report, aabb=None):
    """Return exact cell-array relocations; reject ambiguous or foreign pointers."""
    branch = next(b for b in report.full.branches if b.name == 'cells/AABB/portals')
    roots = branch.start
    cursor = roots + report.cell_count * PC_CELL
    records = []
    for ci in range(report.cell_count):
        cell = roots + ci * PC_CELL
        ac, pc, cc = (i32(zone, cell + o) for o in (0x18, 0x20, 0x28))
        aroots = cursor
        cursor += ac * PC_AABB
        for ai in range(ac):
            p = aroots + ai * PC_AABB
            if decode_pc_pointer(u32(zone, p + 0x24)).kind in ('following', 'insert'):
                cursor += u16(zone, p + 0x22) * 2
        proots = cursor
        cursor += pc * PC_PORTAL
        for pi in range(pc):
            p = proots + pi * PC_PORTAL
            ensure(zone, p, PC_PORTAL, 'portal')
            count = zone[p + 0x28]
            neighbour = decode_pc_pointer(u32(zone, p + 0x20))
            if neighbour.kind != 'packed' or neighbour.block != 4:
                raise ValueError(f'Portal at 0x{p:X} has no packed block-4 neighbour cell')
            if count:
                require_inline(u32(zone, p + 0x24), 'portal vertices')
            elif u32(zone, p + 0x24):
                raise ValueError('Empty portal has a non-null vertex pointer')
            ensure(zone, cursor, count * 12, 'portal vertices')
            records.append((p, ci, neighbour.offset, cursor, count))
            cursor += count * 12
        cursor += cc * 4 + zone[cell + 0x30]
    if cursor != branch.end:
        raise ValueError('Portal resolver did not reach the cell branch end')
    if not records:
        return {}
    aabb = aabb or resolve_aabb_reuse(zone, report)
    if aabb.bounds_perfect_candidates == 1:
        bases = {aabb.block4_cell_logical_base}
    else:
        # Every neighbour must address an exact record in this one contiguous array.
        # A connected graph referencing the first and last cell fixes its base uniquely.
        target = records[0][2]
        bases = {target - i * PC_CELL for i in range(report.cell_count)
                 if target - i * PC_CELL >= 0 and (target - i * PC_CELL) % 4 == 0}
    for _, _, target, _, _ in records:
        bases = {base for base in bases
                 if 0 <= target - base < report.cell_count * PC_CELL
                 and (target - base) % PC_CELL == 0}
    if len(bases) != 1:
        raise ValueError(f'Portal cell-array base is not uniquely proven ({len(bases)} matches)')
    base = next(iter(bases))
    return {p: PortalBinding(p, ci, (target-base)//PC_CELL, vertices, count)
            for p, ci, target, vertices, count in records}

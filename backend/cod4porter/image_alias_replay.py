from __future__ import annotations

from collections import defaultdict
from typing import Mapping, Sequence

from .backend.v4_ffio import decode_pc_pointer
from .material import MaterialRecord
from .material_planning import serialized_image_pointer, texture_signature
from .gfxworld_aabb import resolve_aabb_reuse
from .pc_gfxworld import PC_AABB, PC_BRUSH_MODEL, PC_CELL, PC_PORTAL
from .pc_material import parse_material_at
from .pc_xmodel import PcXModelIntermediate
from .xmodel import (
    COLLISION_LEAF_SIZE,
    COLLISION_NODE_SIZE,
    COLLISION_TREE_ROOT_SIZE,
    PC_MODEL_COLLISION_SURFACE_SIZE,
    PC_XSURFACE_ROOT_SIZE,
    RIGID_VERT_LIST_SIZE,
    XBONE_INFO_SIZE,
)


_PC_COLLISION_TRI_SIZE = 0x30


# PC water_t: float floatTime; complex_s *H0; float *wTerm; int M, N; float scalars[11];
# GfxImage *image  -> 0x44 bytes, image pointer at +0x40.
PC_WATER_ROOT = 0x44
PC_WATER_IMAGE_POINTER = 0x40


def _align(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError('alignment must be a positive power of two')
    return (value + alignment - 1) & ~(alignment - 1)


def _normal_image_name(value: str | None) -> str | None:
    if value is None:
        return None
    out = value.replace('\\', '/').lstrip('/')
    return out or None


def _insert_alias(cursor: int, raw: int, *, exact: bool) -> tuple[int, int]:
    """Replay an INSERT alias whose pointee is loaded in the persistent block-4 stream."""
    if exact and decode_pc_pointer(int(raw)).kind == 'insert':
        return _align(cursor, 4) + 4, 1
    return cursor, 0


def _source_word(zone: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + 4 > len(zone):
        raise ValueError(f'{label} pointer word outside PC zone at 0x{offset:X}')
    return int.from_bytes(zone[offset:offset + 4], 'little')


def _source_i32(zone: bytes, offset: int, label: str) -> int:
    raw = _source_word(zone, offset, label)
    return raw - 0x100000000 if raw & 0x80000000 else raw


def _xmodel_handle_bases(resolution: Mapping[str, object]) -> tuple[dict[int, int], list[dict]]:
    """Collect the material-handle bases selected by the CP19 XModel resolver.

    The resolver publishes two independently auditable sources: the causal anchor chain and
    interval replay assignments.  A model may occur in both only when the bases agree.
    """
    bases: dict[int, int] = {}
    evidence: list[dict] = []

    causal = resolution.get('causal_base_diagnostics', {})
    if isinstance(causal, Mapping):
        chain = causal.get('chain', {})
        rows = chain.get('selected_rows', ()) if isinstance(chain, Mapping) else ()
        for row in rows or ():
            mi = int(row['model_index'])
            base = int(row['base'])
            old = bases.get(mi)
            if old is not None and old != base:
                raise ValueError(f'XModel {mi} has conflicting causal handle bases 0x{old:X}/0x{base:X}')
            bases[mi] = base
            evidence.append({'model_index': mi, 'base': base, 'source': 'causal-anchor-chain'})

    replay = resolution.get('block4_replay', {})
    groups = replay.get('rows', ()) if isinstance(replay, Mapping) else ()
    for group in groups or ():
        for row in group.get('assignment', ()):
            mi = int(row['model_index'])
            base = int(row['base'])
            old = bases.get(mi)
            if old is not None and old != base:
                raise ValueError(f'XModel {mi} has conflicting replay handle bases 0x{old:X}/0x{base:X}')
            bases[mi] = base
            evidence.append({
                'model_index': mi,
                'base': base,
                'source': 'material-target-interval-replay',
                'target': int(row.get('target', 0)),
                'surface_index': int(row.get('surface_index', -1)),
            })
    return bases, evidence


def _signature_names(materials: Sequence[MaterialRecord]) -> dict[tuple, set[str]]:
    out: dict[tuple, set[str]] = defaultdict(set)
    for material in materials:
        for texture in material.textures:
            name = _normal_image_name(texture.inline_image_name)
            if name is not None:
                out[texture_signature(texture)].add(name)
    return out


def _packed_requests(materials: Sequence[MaterialRecord]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for material in materials:
        for texture_index, texture in enumerate(material.textures):
            raw = serialized_image_pointer(material, texture_index)
            pointer = decode_pc_pointer(raw)
            if pointer.kind == 'packed' and pointer.block == 4 and pointer.offset is not None:
                out[int(pointer.offset)].append({
                    'material': material.name,
                    'material_root': int(material.root_offset),
                    'texture_index': texture_index,
                    'signature': texture_signature(texture),
                    'raw': int(raw),
                })
    return out


def _relative_xmodel_layouts(
    xmodels: Sequence[PcXModelIntermediate],
    material_by_root: Mapping[int, MaterialRecord],
    pc_zone: bytes | None = None,
    *, base_residue: int | None = None,
) -> list[dict]:
    """Replay each XModel's normal/block-4 Material child footprint relative to Material**.

    The layout intentionally includes only allocations measured by the Getaway source family:
    Material XStrings, MaterialTextureDef tables, inline GfxImage XStrings, constants and state
    tables. Image roots/loadDefs/resources live in other streams. Water tails remain excluded until
    a dedicated replay exists, so those models cannot become secondary base anchors.
    """
    if base_residue is None:
        variants = {r: _relative_xmodel_layouts(xmodels, material_by_root, pc_zone,
                                               base_residue=r) for r in (0, 4, 8, 12)}
        return [dict(row, alignment_variants={r: variants[r][i] for r in variants})
                for i, row in enumerate(variants[0])]
    layouts: list[dict] = []
    exact = pc_zone is not None
    for model_index, source in enumerate(xmodels):
        cursor = base_residue + 4 * len(source.model.surfaces)
        cells: list[dict] = []
        incomplete = False
        insert_alias_cells = 0
        for surface_index, root in enumerate(source.inline_material_offsets):
            if root is None:
                continue
            material = material_by_root.get(int(root))
            if material is None:
                raise ValueError(
                    f"XModel '{source.model.name}' inline Material root 0x{int(root):X} missing from universe"
                )
            if exact:
                cursor, count = _insert_alias(
                    cursor, int(source.material_handle_pointers[surface_index]), exact=True,
                )
                insert_alias_cells += count
                cursor, count = _insert_alias(
                    cursor, _source_word(pc_zone, material.root_offset, f"Material '{material.name}'.name"), exact=True,
                )
                insert_alias_cells += count
            material_start = cursor
            cursor += len(material.name.encode('latin-1')) + 1
            if material.textures:
                if exact:
                    cursor, count = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x44, f"Material '{material.name}'.textureTable"), exact=True,
                    )
                    insert_alias_cells += count
                cursor = _align(cursor, 4)
                table_base = cursor
                cursor += len(material.textures) * 0x0C
                for texture_index, texture in enumerate(material.textures):
                    raw = serialized_image_pointer(material, texture_index)
                    pointer = decode_pc_pointer(raw)
                    cells.append({
                        'relative_address': table_base + texture_index * 0x0C + 8 - base_residue,
                        'surface_index': surface_index,
                        'material': material.name,
                        'material_root': int(material.root_offset),
                        'material_logical_start': material_start - base_residue,
                        'texture_index': texture_index,
                        'raw': int(raw),
                        'pointer_kind': pointer.kind,
                        'pointer_block': pointer.block,
                        'pointer_offset': pointer.offset,
                        'inline_identity': _normal_image_name(texture.inline_image_name),
                        'signature': texture_signature(texture),
                    })
                for texture_index, texture in enumerate(material.textures):
                    pointer = decode_pc_pointer(serialized_image_pointer(material, texture_index))
                    if exact and pointer.kind in ('following', 'insert'):
                        image_root = getattr(texture, 'inline_image_root', None)
                        if image_root is None:
                            raise ValueError(
                                f"Material '{material.name}' inline GfxImage has no parsed source root"
                            )
                        # The image root itself lives in TEMP.  An INSERT image pointer still
                        # reserves its persistent alias cell before that TEMP allocation.
                        cursor, count = _insert_alias(
                            cursor, serialized_image_pointer(material, texture_index), exact=True,
                        )
                        insert_alias_cells += count
                        cursor, count = _insert_alias(
                            cursor, _source_word(pc_zone, int(image_root) + 0x20, f"Material '{material.name}' GfxImage.name"), exact=True,
                        )
                        insert_alias_cells += count
                    name = _normal_image_name(texture.inline_image_name)
                    if name is not None:
                        cursor += len(name.encode('latin-1')) + 1
                    if exact and pointer.kind in ('following', 'insert'):
                        # GfxImage is loaded in declared reorder order: name, then texture.
                        # DB_InsertPointer for GfxTexture::loadDef owns a persistent block-4
                        # alias cell even though the descriptor/resource pointee is TEMP.
                        cursor, count = _insert_alias(
                            cursor, _source_word(pc_zone, int(image_root) + 0x04, f"Material '{material.name}' GfxImage.loadDef"), exact=True,
                        )
                        insert_alias_cells += count
            if material.constants:
                if exact:
                    cursor, count = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x48, f"Material '{material.name}'.constantTable"), exact=True,
                    )
                    insert_alias_cells += count
                cursor = _align(cursor, 16)
                cursor += len(material.constants) * 0x20
            if material.state_bits:
                if exact:
                    cursor, count = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x4C, f"Material '{material.name}'.stateTable"), exact=True,
                    )
                    insert_alias_cells += count
                cursor = _align(cursor, 4)
                cursor += len(material.state_bits) * 8
            if material.contains_water:
                incomplete = True
                break
        layouts.append({
            'model_index': model_index,
            'model': source.model.name,
            'size': cursor - base_residue,
            'base_residue': base_residue,
            'cells': cells,
            'incomplete_water_tail': incomplete,
            'insert_alias_cells': insert_alias_cells,
        })
    return layouts


def _request_identity_sets(
    requests: Mapping[int, Sequence[dict]],
    signature_names: Mapping[tuple, set[str]],
) -> dict[int, set[str]]:
    """Return identities supported independently by every use of each packed target."""
    out: dict[int, set[str]] = {}
    for target, uses in requests.items():
        common: set[str] | None = None
        for use in uses:
            candidates = set(signature_names.get(tuple(use['signature']), set()))
            common = candidates if common is None else common & candidates
        out[int(target)] = common or set()
    return out


def _owner_precedes_all_uses(cell: Mapping[str, object], uses: Sequence[dict]) -> bool:
    owner = (int(cell['material_root']), int(cell['texture_index']))
    return all(owner < (int(use['material_root']), int(use['texture_index'])) for use in uses)


def _layout_at_base(layout: dict, base: int) -> dict:
    return layout.get('alignment_variants', {}).get(base & 15, layout)


def _variant_cells(layout: dict):
    for variant in layout.get('alignment_variants', {None: layout}).values():
        for cell in variant['cells']:
            yield variant.get('base_residue'), cell


def _recover_unanchored_bases(
    layouts: Sequence[dict],
    known_bases: Mapping[int, int],
    requests: Mapping[int, Sequence[dict]],
    signature_names: Mapping[tuple, set[str]],
    already_resolved_targets: set[int],
    *,
    minimum_targets: int = 2,
) -> dict:
    """Recover additional XModel bases from multi-cell image evidence.

    This is deliberately stricter than the primary replay. A candidate must map at least two
    distinct packed targets, fit between already-proven neighbouring XModel regions, precede every
    use it claims, and participate in the unique maximum non-overlapping assignment for that anchor
    interval. Single-target candidates are reported but never accepted because common texture
    signatures make them too collision-prone.
    """
    if minimum_targets < 2:
        raise ValueError('unanchored image replay requires at least two independent targets')
    by_index = {int(row['model_index']): row for row in layouts}
    target_names = _request_identity_sets(requests, signature_names)
    unresolved = set(int(x) for x in requests) - set(int(x) for x in already_resolved_targets)
    known_rows = sorted((int(mi), int(base), int(_layout_at_base(by_index[int(mi)], int(base))['size'])) for mi, base in known_bases.items())

    def neighbours(model_index: int):
        lower = max((row for row in known_rows if row[0] < model_index), default=None)
        upper = min((row for row in known_rows if row[0] > model_index), default=None)
        return lower, upper

    candidate_models: list[dict] = []
    single_target_candidates = 0
    rejected_causal = 0
    rejected_bounds = 0
    for layout in layouts:
        model_index = int(layout['model_index'])
        if model_index in known_bases or layout.get('incomplete_water_tail'):
            continue
        lower, upper = neighbours(model_index)
        candidate_bases: set[int] = set()
        for residue, cell in _variant_cells(layout):
            name = cell.get('inline_identity')
            if not name:
                continue
            for target in unresolved:
                if name not in target_names.get(target, set()):
                    continue
                if not _owner_precedes_all_uses(cell, requests[target]):
                    rejected_causal += 1
                    continue
                base = int(target) - int(cell['relative_address'])
                if base < 0 or base & 3 or (residue is not None and base & 15 != residue):
                    continue
                candidate_bases.add(base)

        candidates: list[dict] = []
        for base in sorted(candidate_bases):
            variant = _layout_at_base(layout, base)
            end = base + int(variant['size'])
            if lower is not None and base < lower[1] + lower[2]:
                rejected_bounds += 1
                continue
            if upper is not None and end > upper[1]:
                rejected_bounds += 1
                continue
            mapped: dict[int, dict] = {}
            conflict = False
            for cell in variant['cells']:
                name = cell.get('inline_identity')
                if not name:
                    continue
                target = base + int(cell['relative_address'])
                if target not in unresolved or name not in target_names.get(target, set()):
                    continue
                if not _owner_precedes_all_uses(cell, requests[target]):
                    continue
                old = mapped.get(target)
                if old is not None and old['identity'] != name:
                    conflict = True
                    break
                mapped[target] = {
                    'target': target,
                    'identity': name,
                    'material': cell['material'],
                    'material_root': int(cell['material_root']),
                    'texture_index': int(cell['texture_index']),
                    'relative_address': int(cell['relative_address']),
                }
            if conflict or not mapped:
                continue
            row = {
                'model_index': model_index,
                'model': layout['model'],
                'base': base,
                'end': end,
                'mapped_target_count': len(mapped),
                'mapped_targets': [mapped[k] for k in sorted(mapped)],
            }
            if len(mapped) < minimum_targets:
                single_target_candidates += 1
                continue
            candidates.append(row)
        if candidates:
            interval = (
                lower[0] if lower is not None else -1,
                upper[0] if upper is not None else len(layouts),
            )
            candidate_models.append({
                'model_index': model_index,
                'model': layout['model'],
                'interval': interval,
                'candidates': candidates,
            })

    intervals: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for row in candidate_models:
        intervals[tuple(row['interval'])].append(row)

    accepted_rows: list[dict] = []
    interval_reports: list[dict] = []
    for interval, model_rows in sorted(intervals.items()):
        model_rows = sorted(model_rows, key=lambda row: int(row['model_index']))
        lower_index, upper_index = interval
        lower_end = -1
        if lower_index >= 0:
            lower_end = int(known_bases[lower_index]) + int(_layout_at_base(by_index[lower_index], int(known_bases[lower_index]))['size'])
        upper_start = 1 << 60
        if upper_index < len(layouts):
            upper_start = int(known_bases[upper_index])

        outcomes: list[tuple[tuple[int, int], tuple, list[dict]]] = []
        explored = 0

        def visit(position: int, last_end: int, used_targets: frozenset[int], selected: list[dict]) -> None:
            nonlocal explored
            explored += 1
            if explored > 200_000:
                raise ValueError(f'unanchored image replay interval {interval} exceeded search bound')
            if position == len(model_rows):
                score = (len(used_targets), len(selected))
                signature = tuple((int(row['model_index']), int(row['base'])) for row in selected)
                outcomes.append((score, signature, list(selected)))
                return
            row = model_rows[position]
            visit(position + 1, last_end, used_targets, selected)
            for candidate in row['candidates']:
                target_set = frozenset(int(x['target']) for x in candidate['mapped_targets'])
                if int(candidate['base']) < last_end or int(candidate['end']) > upper_start:
                    continue
                if used_targets & target_set:
                    continue
                selected.append(candidate)
                visit(position + 1, int(candidate['end']), used_targets | target_set, selected)
                selected.pop()

        visit(0, lower_end, frozenset(), [])
        outcomes.sort(key=lambda row: (row[0], row[1]), reverse=True)
        best_score = outcomes[0][0] if outcomes else (0, 0)
        best = [row for row in outcomes if row[0] == best_score]
        unique = len({row[1] for row in best}) == 1
        second_score = next((row[0] for row in outcomes if row[0] != best_score), (0, 0))
        accepted = bool(best_score[0]) and unique and best_score[0] > second_score[0]
        selected_rows = best[0][2] if accepted else []
        accepted_rows.extend(selected_rows)
        interval_reports.append({
            'interval': list(interval),
            'candidate_models': len(model_rows),
            'candidate_bases': sum(len(row['candidates']) for row in model_rows),
            'explored_assignments': explored,
            'best_score': list(best_score),
            'second_score': list(second_score),
            'best_assignment_count': len({row[1] for row in best}),
            'accepted': accepted,
            'assignment': selected_rows,
        })

    aliases: dict[int, str] = {}
    bases: dict[int, int] = {}
    conflicts: list[dict] = []
    for row in accepted_rows:
        model_index = int(row['model_index'])
        base = int(row['base'])
        old_base = bases.get(model_index)
        if old_base is not None and old_base != base:
            conflicts.append({'model_index': model_index, 'left_base': old_base, 'right_base': base})
            continue
        bases[model_index] = base
        for mapped in row['mapped_targets']:
            target = int(mapped['target'])
            name = str(mapped['identity'])
            old = aliases.get(target)
            if old is not None and old != name:
                conflicts.append({'target': target, 'left_identity': old, 'right_identity': name})
            else:
                aliases[target] = name
    if conflicts:
        raise ValueError(f'unanchored image replay produced conflicting assignments: {conflicts[:4]}')

    # A one-target interval is normally too weak: common normal/color signatures create many
    # plausible bases.  It becomes exact when two proven neighbouring XModels enclose exactly one
    # unanchored owner and that owner's complete typed layout admits exactly one target/cell/base
    # tuple.  This is a structural interval proof, not a relaxed signature guess.
    single_owner_rows: list[dict] = []
    unresolved_single = unresolved - set(aliases)
    accepted_models = set(bases)
    for layout in layouts:
        model_index = int(layout['model_index'])
        if model_index in known_bases or model_index in accepted_models or layout.get('incomplete_water_tail'):
            continue
        lower, upper = neighbours(model_index)
        if lower is None or upper is None or upper[0] != lower[0] + 2 or model_index != lower[0] + 1:
            continue
        lower_end = int(lower[1]) + int(lower[2])
        upper_start = int(upper[1])
        candidates: list[dict] = []
        for residue, cell in _variant_cells(layout):
            name = cell.get('inline_identity')
            if not name:
                continue
            for target in unresolved_single:
                if name not in target_names.get(target, set()):
                    continue
                if not _owner_precedes_all_uses(cell, requests[target]):
                    continue
                base = int(target) - int(cell['relative_address'])
                if residue is not None and base & 15 != residue:
                    continue
                end = base + int(_layout_at_base(layout, base)['size'])
                if base < lower_end or end > upper_start or base < 0 or base & 3:
                    continue
                candidates.append({
                    'model_index': model_index, 'model': layout['model'],
                    'base': base, 'end': end, 'target': int(target),
                    'identity': str(name), 'material': cell['material'],
                    'material_root': int(cell['material_root']),
                    'texture_index': int(cell['texture_index']),
                    'relative_address': int(cell['relative_address']),
                    'lower_model_index': int(lower[0]), 'lower_end': lower_end,
                    'upper_model_index': int(upper[0]), 'upper_start': upper_start,
                })
        unique = {
            (row['target'], row['identity'], row['base'], row['material_root'], row['texture_index']): row
            for row in candidates
        }
        if len(unique) != 1:
            continue
        row = next(iter(unique.values()))
        target = int(row['target'])
        name = str(row['identity'])
        if target in aliases and aliases[target] != name:
            raise ValueError(f'single-owner interval target 0x{target:X} conflicts with {aliases[target]!r}/{name!r}')
        aliases[target] = name
        bases[model_index] = int(row['base'])
        accepted_models.add(model_index)
        single_owner_rows.append(row)

    return {
        'aliases': aliases,
        'bases': bases,
        'accepted_rows': accepted_rows,
        'accepted_model_count': len(bases),
        'accepted_target_count': len(aliases),
        'accepted_slot_count': sum(len(requests[target]) for target in aliases),
        'candidate_model_count': len(candidate_models),
        'candidate_base_count': sum(len(row['candidates']) for row in candidate_models),
        'single_target_candidates_rejected': single_target_candidates,
        'causal_rejections': rejected_causal,
        'bounds_rejections': rejected_bounds,
        'minimum_targets': minimum_targets,
        'intervals': interval_reports,
        'single_owner_interval_replay': {
            'accepted_model_count': len(single_owner_rows),
            'accepted_target_count': len(single_owner_rows),
            'accepted_slot_count': sum(len(requests[int(row['target'])]) for row in single_owner_rows),
            'rows': single_owner_rows,
        },
    }


class _ClosedXModelIntervalStop(Exception):
    """An unsupported member stops one candidate interval without weakening other intervals."""


def _replay_closed_xmodel_intervals(
    pc_zone: bytes,
    xmodels: Sequence[PcXModelIntermediate],
    layouts: Sequence[dict],
    material_by_root: Mapping[int, MaterialRecord],
    known_bases: Mapping[int, int],
    requests: Mapping[int, Sequence[dict]],
) -> dict:
    """Recover Material** bases only when a complete XModel B4 interval closes exactly.

    Each interval begins and ends at independently recovered Material** bases. Between those
    anchors this function replays every persistent PC default-normal allocation in source-XAsset
    order: the lower model tail, each next model prefix, its Material graph, collision arrays and
    XBoneInfo tail. Predicted owner cells become identity evidence only if the replay lands on the
    upper anchor byte-for-byte. Inline PhysPreset XStrings are replayed from bounded source bytes. PhysGeom
    and water tails are deliberately unsupported, so an interval containing one is discarded rather than approximated.
    """
    if not pc_zone or len(layouts) != len(xmodels):
        return {
            'aliases': {}, 'bases': {}, 'exact_direct_alias_targets': (),
            'accepted_interval_count': 0, 'accepted_target_count': 0,
            'accepted_slot_count': 0, 'intervals': (),
        }

    layout_by_index = {int(row['model_index']): row for row in layouts}
    anchors = sorted((int(index), int(base)) for index, base in known_bases.items())

    def stop(message: str) -> None:
        raise _ClosedXModelIntervalStop(message)

    def pointer_kind(raw: int, label: str):
        pointer = decode_pc_pointer(int(raw))
        if pointer.kind not in ('null', 'following', 'insert', 'packed'):
            stop(f'{label} has unsupported pointer kind {pointer.kind}')
        return pointer

    def inline_allocation(cursor: int, raw: int, size: int, alignment: int, label: str) -> tuple[int, bool, int]:
        pointer = pointer_kind(raw, label)
        if pointer.kind not in ('following', 'insert'):
            return cursor, False, 0
        cursor, aliases = _insert_alias(cursor, raw, exact=True)
        return _align(cursor, alignment) + int(size), True, aliases

    def validate_material_layout(model_index: int) -> None:
        source = xmodels[model_index]
        layout = layout_by_index[model_index]
        if layout.get('incomplete_water_tail'):
            stop(f'XModel[{model_index}] contains an unsupported water Material tail')
        for surface_index, root in enumerate(source.inline_material_offsets):
            raw = int(source.material_handle_pointers[surface_index])
            kind = pointer_kind(raw, f'XModel[{model_index}].material[{surface_index}]').kind
            if root is None:
                if kind in ('following', 'insert'):
                    stop(f'XModel[{model_index}] inline Material has no parsed root')
                continue
            if kind not in ('following', 'insert'):
                stop(f'XModel[{model_index}] parsed Material root is not inline')

    def tail(model_index: int, base: int) -> tuple[int, list[dict], int]:
        source = xmodels[model_index]
        model = source.model
        validate_material_layout(model_index)
        cursor = int(base) + len(model.surfaces) * 4
        cells: list[dict] = []
        aliases = 0
        for surface_index, material_root in enumerate(source.inline_material_offsets):
            if material_root is None:
                continue
            material = material_by_root.get(int(material_root))
            if material is None:
                stop(f'XModel[{model_index}] inline Material root is absent from typed universe')
            handle_raw = int(source.material_handle_pointers[surface_index])
            cursor, count = _insert_alias(cursor, handle_raw, exact=True)
            aliases += count
            name_raw = _source_word(pc_zone, material.root_offset, 'Material.name')
            cursor, count = _insert_alias(cursor, name_raw, exact=True)
            aliases += count
            cursor += len(material.name.encode('latin-1')) + 1
            if material.contains_water:
                stop(f'XModel[{model_index}] contains an unsupported water Material tail')
            if material.textures:
                table_raw = _source_word(pc_zone, material.root_offset + 0x44, 'Material.textureTable')
                if decode_pc_pointer(table_raw).kind not in ('following', 'insert'):
                    stop(f'XModel[{model_index}] inline Material texture table is not inline')
                cursor, count = _insert_alias(cursor, table_raw, exact=True)
                aliases += count
                cursor = _align(cursor, 4)
                table_base = cursor
                cursor += len(material.textures) * 0x0C
                for texture_index, texture in enumerate(material.textures):
                    raw = serialized_image_pointer(material, texture_index)
                    pointer = decode_pc_pointer(raw)
                    name = _normal_image_name(texture.inline_image_name)
                    if name:
                        cells.append({
                            'model_index': model_index, 'model': model.name,
                            'surface_index': surface_index, 'material': material.name,
                            'material_root': int(material.root_offset),
                            'texture_index': texture_index,
                            'cell_address': table_base + texture_index * 0x0C + 8,
                            'raw': int(raw), 'pointer_kind': pointer.kind,
                            'pointer_block': pointer.block, 'pointer_offset': pointer.offset,
                            'inline_identity': name, 'signature': texture_signature(texture),
                        })
                for texture_index, texture in enumerate(material.textures):
                    raw = serialized_image_pointer(material, texture_index)
                    pointer = decode_pc_pointer(raw)
                    if pointer.kind not in ('following', 'insert'):
                        continue
                    image_root = texture.inline_image_root
                    if image_root is None:
                        stop(f'XModel[{model_index}] inline GfxImage root is missing')
                    cursor, count = _insert_alias(cursor, raw, exact=True)
                    aliases += count
                    image_name_raw = _source_word(pc_zone, int(image_root) + 0x20, 'GfxImage.name')
                    cursor, count = _insert_alias(cursor, image_name_raw, exact=True)
                    aliases += count
                    name = _normal_image_name(texture.inline_image_name)
                    if name is not None:
                        cursor += len(name.encode('latin-1')) + 1
                    loaddef_raw = _source_word(pc_zone, int(image_root) + 0x04, 'GfxImage.loadDef')
                    cursor, count = _insert_alias(cursor, loaddef_raw, exact=True)
                    aliases += count
            if material.constants:
                constant_raw = _source_word(pc_zone, material.root_offset + 0x48, 'Material.constantTable')
                if decode_pc_pointer(constant_raw).kind not in ('following', 'insert'):
                    stop(f'XModel[{model_index}] inline Material constants are not inline')
                cursor, count = _insert_alias(cursor, constant_raw, exact=True)
                aliases += count
                cursor = _align(cursor, 16) + len(material.constants) * 0x20
            if material.state_bits:
                state_raw = _source_word(pc_zone, material.root_offset + 0x4C, 'Material.stateTable')
                if decode_pc_pointer(state_raw).kind not in ('following', 'insert'):
                    stop(f'XModel[{model_index}] inline Material states are not inline')
                cursor, count = _insert_alias(cursor, state_raw, exact=True)
                aliases += count
                cursor = _align(cursor, 4) + len(material.state_bits) * 8

        collision_count = int(model.num_collision_surfaces)
        if collision_count < 0:
            stop(f'XModel[{model_index}] has negative collision-surface count')
        collision_raw = _source_word(pc_zone, int(source.root_offset) + 0x98, 'XModel.collSurfs')
        cursor, collision_inline, count = inline_allocation(
            cursor, collision_raw, collision_count * PC_MODEL_COLLISION_SURFACE_SIZE, 4,
            f'XModel[{model_index}].collSurfs',
        )
        aliases += count
        if collision_inline:
            roots = model.collision_surface_source
            if collision_count and roots is None:
                stop(f'XModel[{model_index}] inline collision surfaces have no physical roots')
            for collision_index in range(collision_count):
                root = int(roots) + collision_index * PC_MODEL_COLLISION_SURFACE_SIZE
                tri_raw = _source_word(pc_zone, root, 'XModelCollSurf.collTris')
                tri_count = _source_i32(pc_zone, root + 4, 'XModelCollSurf.numCollTris')
                if tri_count < 0:
                    stop(f'XModel[{model_index}] collision[{collision_index}] has negative tri count')
                cursor, tri_inline, count = inline_allocation(
                    cursor, tri_raw, tri_count * _PC_COLLISION_TRI_SIZE, 4,
                    f'XModel[{model_index}].collision[{collision_index}].collTris',
                )
                aliases += count
                if tri_count and not tri_inline and pointer_kind(tri_raw, 'XModelCollSurf.collTris').kind == 'null':
                    stop(f'XModel[{model_index}] collision tri count has NULL pointer')
        elif collision_count and pointer_kind(collision_raw, 'XModel.collSurfs').kind == 'null':
            stop(f'XModel[{model_index}] collision-surface count has NULL pointer')

        bone_raw = _source_word(pc_zone, int(source.root_offset) + 0xA4, 'XModel.boneInfo')
        cursor, _bone_inline, count = inline_allocation(
            cursor, bone_raw, int(model.num_bones) * XBONE_INFO_SIZE, 4,
            f'XModel[{model_index}].boneInfo',
        )
        aliases += count

        preset_raw = _source_word(pc_zone, int(source.root_offset) + 0xD4, 'XModel.physPreset')
        if pointer_kind(preset_raw, 'XModel.physPreset').kind in ('following', 'insert'):
            preset = model.inline_phys_preset
            root = getattr(preset, 'root_offset', None)
            if root is None:
                stop(f'XModel[{model_index}] inline PhysPreset has no typed source root')
            cursor, count = _insert_alias(cursor, preset_raw, exact=True)
            aliases += count
            # PhysPreset's root is TEMP; only its two XStrings occupy persistent B4.
            physical = int(root) + 0x2C
            for offset, label in ((0, 'name'), (0x1C, 'soundAliasPrefix')):
                raw = _source_word(pc_zone, int(root) + offset, f'PhysPreset.{label}')
                if pointer_kind(raw, f'PhysPreset.{label}').kind not in ('following', 'insert'):
                    continue
                cursor, count = _insert_alias(cursor, raw, exact=True)
                aliases += count
                end = pc_zone.find(b'\0', physical, min(len(pc_zone), physical + 513))
                if end < 0:
                    stop(f'XModel[{model_index}] PhysPreset.{label} is unterminated')
                cursor += end - physical + 1
                physical = end + 1
            if physical != getattr(preset, 'physical_end', None):
                stop(f'XModel[{model_index}] PhysPreset physical boundary does not close')
        raw = _source_word(pc_zone, int(source.root_offset) + 0xD8, 'XModel.physGeoms')
        if pointer_kind(raw, 'XModel.physGeoms').kind in ('following', 'insert'):
            stop(f'XModel[{model_index}] owns unsupported inline physGeoms')
        return cursor, cells, aliases

    def prefix(model_index: int, cursor: int) -> tuple[int, int]:
        source = xmodels[model_index]
        model = source.model
        root = int(source.root_offset)
        aliases = 0

        name_raw = _source_word(pc_zone, root, 'XModel.name')
        name_pointer = pointer_kind(name_raw, f'XModel[{model_index}].name')
        if name_pointer.kind in ('following', 'insert'):
            cursor, count = _insert_alias(cursor, name_raw, exact=True)
            aliases += count
            cursor += len(model.name.encode('latin-1')) + 1

        nonroot = int(model.num_bones) - int(model.num_root_bones)
        if nonroot < 0:
            stop(f'XModel[{model_index}] root-bone count exceeds bone count')
        skeleton = (
            ('boneNames', 0x08, int(model.num_bones) * 2, 2),
            ('parentList', 0x0C, nonroot, 1),
            ('quats', 0x10, nonroot * 8, 2),
            ('trans', 0x14, nonroot * 16, 4),
            ('partClassification', 0x18, int(model.num_bones), 1),
            ('baseMat', 0x1C, int(model.num_bones) * 0x20, 4),
        )
        for label, member_offset, size, alignment in skeleton:
            raw = _source_word(pc_zone, root + member_offset, f'XModel.{label}')
            cursor, inline, count = inline_allocation(
                cursor, raw, size, alignment, f'XModel[{model_index}].{label}',
            )
            aliases += count
            if size and not inline and pointer_kind(raw, f'XModel[{model_index}].{label}').kind == 'null':
                stop(f'XModel[{model_index}] {label} size has NULL pointer')

        surface_raw = _source_word(pc_zone, root + 0x20, 'XModel.surfs')
        cursor, surfaces_inline, count = inline_allocation(
            cursor, surface_raw, len(model.surfaces) * PC_XSURFACE_ROOT_SIZE, 4,
            f'XModel[{model_index}].surfs',
        )
        aliases += count
        if model.surfaces and not surfaces_inline:
            stop(f'XModel[{model_index}] surface roots are not inline')

        for surface_index, surface in enumerate(model.surfaces):
            surface_root = getattr(surface, 'pc_root', None)
            if surface_root is None:
                stop(f'XModel[{model_index}] surface[{surface_index}] has no PC root')
            blend_raw = _source_word(pc_zone, int(surface_root) + 0x18, 'XSurface.vertsBlend')
            cursor, blend_inline, count = inline_allocation(
                cursor, blend_raw, int(surface.blend_element_count) * 2, 2,
                f'XModel[{model_index}].surface[{surface_index}].vertsBlend',
            )
            aliases += count
            if surface.blend_element_count and not blend_inline and pointer_kind(blend_raw, 'XSurface.vertsBlend').kind == 'null':
                stop(f'XModel[{model_index}] blend count has NULL pointer')

            rigid_raw = _source_word(pc_zone, int(surface_root) + 0x24, 'XSurface.vertList')
            rigid_count = int(surface.rigid_vert_list_count)
            cursor, rigid_inline, count = inline_allocation(
                cursor, rigid_raw, rigid_count * RIGID_VERT_LIST_SIZE, 4,
                f'XModel[{model_index}].surface[{surface_index}].vertList',
            )
            aliases += count
            if rigid_count and not rigid_inline and pointer_kind(rigid_raw, 'XSurface.vertList').kind == 'null':
                stop(f'XModel[{model_index}] rigid-list count has NULL pointer')
            if rigid_inline:
                rigid_root = source.inline_rigid_offsets[surface_index]
                if rigid_count and rigid_root is None:
                    stop(f'XModel[{model_index}] inline rigid list has no physical root')
                nested = int(rigid_root) + rigid_count * RIGID_VERT_LIST_SIZE if rigid_root is not None else 0
                for rigid_index in range(rigid_count):
                    row = int(rigid_root) + rigid_index * RIGID_VERT_LIST_SIZE
                    tree_raw = _source_word(pc_zone, row + 8, 'XRigidVertList.collisionTree')
                    tree_pointer = pointer_kind(tree_raw, 'XRigidVertList.collisionTree')
                    if tree_pointer.kind not in ('following', 'insert'):
                        continue
                    cursor, count = _insert_alias(cursor, tree_raw, exact=True)
                    aliases += count
                    cursor = _align(cursor, 4) + COLLISION_TREE_ROOT_SIZE
                    tree = nested
                    nested += COLLISION_TREE_ROOT_SIZE
                    node_count = _source_word(pc_zone, tree + 0x18, 'XSurfaceCollisionTree.nodeCount')
                    node_raw = _source_word(pc_zone, tree + 0x1C, 'XSurfaceCollisionTree.nodes')
                    cursor, node_inline, count = inline_allocation(
                        cursor, node_raw, node_count * COLLISION_NODE_SIZE, 16,
                        f'XModel[{model_index}].surface[{surface_index}].tree[{rigid_index}].nodes',
                    )
                    aliases += count
                    if node_inline:
                        nested += node_count * COLLISION_NODE_SIZE
                    elif node_count and pointer_kind(node_raw, 'XSurfaceCollisionTree.nodes').kind == 'null':
                        stop(f'XModel[{model_index}] collision node count has NULL pointer')
                    leaf_count = _source_word(pc_zone, tree + 0x20, 'XSurfaceCollisionTree.leafCount')
                    leaf_raw = _source_word(pc_zone, tree + 0x24, 'XSurfaceCollisionTree.leafs')
                    cursor, leaf_inline, count = inline_allocation(
                        cursor, leaf_raw, leaf_count * COLLISION_LEAF_SIZE, 2,
                        f'XModel[{model_index}].surface[{surface_index}].tree[{rigid_index}].leafs',
                    )
                    aliases += count
                    if leaf_inline:
                        nested += leaf_count * COLLISION_LEAF_SIZE
                    elif leaf_count and pointer_kind(leaf_raw, 'XSurfaceCollisionTree.leafs').kind == 'null':
                        stop(f'XModel[{model_index}] collision leaf count has NULL pointer')

        handle_raw = _source_word(pc_zone, root + 0x24, 'XModel.materialHandles')
        handle_pointer = pointer_kind(handle_raw, f'XModel[{model_index}].materialHandles')
        if handle_pointer.kind not in ('following', 'insert'):
            stop(f'XModel[{model_index}] Material** array is not inline')
        cursor, count = _insert_alias(cursor, handle_raw, exact=True)
        aliases += count
        return _align(cursor, 4), aliases

    accepted_aliases: dict[int, str] = {}
    accepted_bases: dict[int, int] = {}
    exact_targets: set[int] = set()
    reports: list[dict] = []
    accepted_intervals = 0
    for anchor_position in range(len(anchors) - 1):
        lower_index, lower_base = anchors[anchor_position]
        upper_index, upper_base = anchors[anchor_position + 1]
        if upper_index <= lower_index + 1:
            continue
        report = {
            'lower_model_index': lower_index, 'lower_base': lower_base,
            'upper_model_index': upper_index, 'upper_base': upper_base,
            'accepted': False,
        }
        try:
            for model_index in range(lower_index, upper_index):
                left_asset = int(xmodels[model_index].model.source_asset_index)
                right_asset = int(xmodels[model_index + 1].model.source_asset_index)
                if left_asset < 0 or right_asset != left_asset + 1:
                    stop('XModel interval is interrupted by a non-XModel XAsset')

            cursor, _lower_cells, alias_count = tail(lower_index, lower_base)
            predicted_bases: dict[int, int] = {}
            predicted_cells: list[dict] = []
            for model_index in range(lower_index + 1, upper_index + 1):
                predicted_base, count = prefix(model_index, cursor)
                alias_count += count
                if model_index == upper_index:
                    if predicted_base != upper_base:
                        stop(
                            f'upper Material** closure mismatch: predicted 0x{predicted_base:X}, '
                            f'anchor 0x{upper_base:X}'
                        )
                    break
                predicted_bases[model_index] = predicted_base
                cursor, cells, count = tail(model_index, predicted_base)
                alias_count += count
                predicted_cells.extend(cells)

            interval_aliases: dict[int, str] = {}
            cell_by_target: dict[int, dict] = {}
            for cell in predicted_cells:
                target = int(cell['cell_address'])
                if target not in requests:
                    continue
                name = str(cell['inline_identity'])
                if not _owner_precedes_all_uses(cell, requests[target]):
                    stop(f'predicted owner cell 0x{target:X} does not precede every packed use')
                old = interval_aliases.get(target)
                if old is not None and old != name:
                    stop(f'predicted owner cell 0x{target:X} has conflicting identities')
                interval_aliases[target] = name
                cell_by_target[target] = cell

            for target, name in interval_aliases.items():
                old = accepted_aliases.get(target)
                if old is not None and old != name:
                    raise ValueError(
                        f'closed XModel intervals conflict at block4+0x{target:X}: {old!r}/{name!r}'
                    )
                accepted_aliases[target] = name
                exact_targets.add(target)
            for model_index, base in predicted_bases.items():
                old = accepted_bases.get(model_index)
                if old is not None and old != base:
                    raise ValueError(
                        f'closed XModel intervals conflict for model {model_index}: 0x{old:X}/0x{base:X}'
                    )
                accepted_bases[model_index] = base

            accepted_intervals += 1
            report.update({
                'accepted': True,
                'predicted_bases': dict(predicted_bases),
                'upper_predicted_base': upper_base,
                'insert_alias_cells_replayed': alias_count,
                'target_count': len(interval_aliases),
                'slot_count': sum(len(requests[target]) for target in interval_aliases),
                'targets': [
                    {
                        'target': target, 'identity': interval_aliases[target],
                        'model_index': int(cell_by_target[target]['model_index']),
                        'material_root': int(cell_by_target[target]['material_root']),
                        'texture_index': int(cell_by_target[target]['texture_index']),
                        'cell_address': target,
                    }
                    for target in sorted(interval_aliases)
                ],
            })
        except _ClosedXModelIntervalStop as exc:
            report['reason'] = str(exc)
        reports.append(report)

    return {
        'aliases': accepted_aliases,
        'bases': accepted_bases,
        'exact_direct_alias_targets': tuple(sorted(exact_targets)),
        'accepted_interval_count': accepted_intervals,
        'accepted_model_count': len(accepted_bases),
        'accepted_target_count': len(accepted_aliases),
        'accepted_slot_count': sum(len(requests[target]) for target in accepted_aliases),
        'intervals': tuple(reports),
    }


def replay_xmodel_image_aliases(
    xmodels: Sequence[PcXModelIntermediate],
    xmodel_materials: Sequence[MaterialRecord],
    all_materials: Sequence[MaterialRecord],
    xmodel_material_resolution: Mapping[str, object],
    *,
    pc_zone: bytes | None = None,
) -> dict:
    """Replay XModel-owned PC block-4 MaterialTextureDef pointer cells.

    Packed GfxImage references do not point at image roots. They point at previously loaded
    ``MaterialTextureDef::u.image`` cells.  Once an XModel's Material** base has been recovered,
    its owned Material graph can be replayed to recover those cell addresses. When source bytes
    are supplied, the replay also accounts for INSERT alias cells whose pointees
    live in block 4 (XStrings and Material member tables). TEMP-root/loadDef aliases stay out of
    this cursor. An exact address hit on a directly named cell is then sufficient identity proof;
    TextureDef signatures remain the guard for transitive/inferred hits.

    This deliberately does not guess identities for signatures with no source observation and it
    stops replaying a model after water data, whose block-4 tail requires a separate world/water
    stream model.
    """
    material_by_root = {int(m.root_offset): m for m in xmodel_materials}
    bases, base_evidence = _xmodel_handle_bases(xmodel_material_resolution)
    signature_names = _signature_names(all_materials)
    requests = _packed_requests(all_materials)
    relative_layouts = _relative_xmodel_layouts(xmodels, material_by_root, pc_zone)

    cell_identity: dict[int, str] = {}
    cell_edge: dict[int, int] = {}
    cell_rows: list[dict] = []
    conflicts: list[dict] = []
    stopped_for_water: list[dict] = []

    def set_identity(address: int, name: str, row: dict) -> None:
        old = cell_identity.get(address)
        if old is not None and old != name:
            conflicts.append({'address': address, 'left': old, 'right': name, 'row': row})
            return
        cell_identity[address] = name

    for model_index, base in sorted(bases.items()):
        if not 0 <= model_index < len(xmodels):
            raise ValueError(f'XModel image replay model index {model_index} outside source list')
        source = xmodels[model_index]
        cursor = int(base) + 4 * len(source.model.surfaces)
        for surface_index, root in enumerate(source.inline_material_offsets):
            if root is None:
                continue
            material = material_by_root.get(int(root))
            if material is None:
                raise ValueError(
                    f"XModel '{source.model.name}' inline Material root 0x{int(root):X} missing from universe"
                )
            if pc_zone is not None:
                cursor, _ = _insert_alias(
                    cursor, int(source.material_handle_pointers[surface_index]), exact=True,
                )
                cursor, _ = _insert_alias(
                    cursor, _source_word(pc_zone, material.root_offset, f"Material '{material.name}'.name"), exact=True,
                )
            material_start = cursor
            cursor += len(material.name.encode('latin-1')) + 1
            if material.textures:
                if pc_zone is not None:
                    cursor, _ = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x44, f"Material '{material.name}'.textureTable"), exact=True,
                    )
                cursor = _align(cursor, 4)
                table_base = cursor
                cursor += len(material.textures) * 0x0C
                for texture_index, texture in enumerate(material.textures):
                    address = table_base + texture_index * 0x0C + 8
                    raw = serialized_image_pointer(material, texture_index)
                    pointer = decode_pc_pointer(raw)
                    name = _normal_image_name(texture.inline_image_name)
                    row = {
                        'model_index':model_index,'model':source.model.name,
                        'surface_index':surface_index,'material':material.name,
                        'material_root':int(material.root_offset),'material_logical_start':material_start,
                        'texture_index':texture_index,'cell_address':address,'raw':int(raw),
                        'pointer_kind':pointer.kind,'pointer_block':pointer.block,'pointer_offset':pointer.offset,
                        'inline_identity':name,'signature':list(texture_signature(texture)),
                    }
                    cell_rows.append(row)
                    if name is not None:
                        set_identity(address, name, row)
                    elif pointer.kind == 'packed' and pointer.block == 4 and pointer.offset is not None:
                        old = cell_edge.get(address)
                        if old is not None and old != int(pointer.offset):
                            conflicts.append({'address':address,'left_target':old,'right_target':int(pointer.offset),'row':row})
                        else:
                            cell_edge[address] = int(pointer.offset)
                for texture_index, texture in enumerate(material.textures):
                    pointer = decode_pc_pointer(serialized_image_pointer(material, texture_index))
                    if pc_zone is not None and pointer.kind in ('following','insert'):
                        image_root = getattr(texture, 'inline_image_root', None)
                        if image_root is None:
                            raise ValueError(f"Material '{material.name}' inline GfxImage has no parsed source root")
                        cursor, _ = _insert_alias(
                            cursor, serialized_image_pointer(material, texture_index), exact=True,
                        )
                        cursor, _ = _insert_alias(
                            cursor, _source_word(pc_zone, int(image_root) + 0x20, f"Material '{material.name}' GfxImage.name"), exact=True,
                        )
                    name = _normal_image_name(texture.inline_image_name)
                    if name is not None:
                        cursor += len(name.encode('latin-1')) + 1
                    if pc_zone is not None and pointer.kind in ('following','insert'):
                        cursor, _ = _insert_alias(
                            cursor, _source_word(pc_zone, int(image_root) + 0x04, f"Material '{material.name}' GfxImage.loadDef"), exact=True,
                        )
            if material.constants:
                if pc_zone is not None:
                    cursor, _ = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x48, f"Material '{material.name}'.constantTable"), exact=True,
                    )
                cursor = _align(cursor, 16)
                cursor += len(material.constants) * 0x20
            if material.state_bits:
                if pc_zone is not None:
                    cursor, _ = _insert_alias(
                        cursor, _source_word(pc_zone, material.root_offset + 0x4C, f"Material '{material.name}'.stateTable"), exact=True,
                    )
                cursor = _align(cursor, 4)
                cursor += len(material.state_bits) * 8
            if material.contains_water:
                stopped_for_water.append({
                    'model_index':model_index,'model':source.model.name,'material':material.name,
                    'reason':'water block-4 tail requires dedicated replay',
                })
                break

    if conflicts:
        raise ValueError(f'XModel image alias replay produced {len(conflicts)} conflicting cells: {conflicts[:3]}')

    # Propagate cells that originally contained packed aliases to the final inline image identity.
    resolved_cells = dict(cell_identity)
    cycles: list[list[int]] = []

    def resolve_cell(address: int, trail: tuple[int, ...] = ()) -> str | None:
        if address in resolved_cells:
            return resolved_cells[address]
        if address in trail:
            cycles.append(list(trail + (address,)))
            return None
        target = cell_edge.get(address)
        if target is None:
            return None
        name = resolve_cell(target, trail + (address,))
        if name is not None:
            resolved_cells[address] = name
        return name

    for address in tuple(cell_edge):
        resolve_cell(address)

    accepted: dict[int, str] = {}
    rejected: list[dict] = []
    for target in sorted(set(requests) & set(resolved_cells)):
        name = resolved_cells[target]
        uses = requests[target]
        checks = []
        for use in uses:
            candidates = signature_names.get(tuple(use['signature']), set())
            checks.append({
                'material': use['material'],
                'texture_index': use['texture_index'],
                'candidate_count': len(candidates),
                'candidate_names': sorted(candidates),
                'accepted_identity_present': name in candidates,
            })
        exact_direct = pc_zone is not None and target in cell_identity
        # Exact INSERT-aware address equality outranks a TextureDef signature: the latter is only
        # a layout class and legitimately differs between an owner and a later aliasing use.
        if exact_direct or checks and all(row['accepted_identity_present'] for row in checks):
            accepted[target] = name
        else:
            rejected.append({'target': target, 'identity': name, 'uses': checks, 'reason': 'signature cross-check failed'})

    # First span every typed B4 allocation between independent Material** anchors. Predictions
    # become exact only when the next anchor closes byte-for-byte.
    full_intervals = (
        _replay_closed_xmodel_intervals(
            pc_zone, xmodels, relative_layouts, material_by_root, bases, requests,
        )
        if pc_zone is not None else
        {'aliases': {}, 'bases': {}, 'exact_direct_alias_targets': (),
         'accepted_target_count': 0, 'accepted_slot_count': 0, 'intervals': ()}
    )
    full_interval_targets = set(int(x) for x in full_intervals['exact_direct_alias_targets'])
    for target, name in sorted(full_intervals['aliases'].items()):
        old = accepted.get(int(target))
        if old is not None and old != name:
            raise ValueError(
                f'GfxImage target block4+0x{int(target):X} conflicts between {old!r} and {name!r}'
            )
        accepted[int(target)] = str(name)
    for model_index, base in sorted(full_intervals['bases'].items()):
        old = bases.get(int(model_index))
        if old is not None and old != int(base):
            raise ValueError(
                f'XModel {model_index} full-stream base conflicts with 0x{old:X}/0x{int(base):X}'
            )
        bases[int(model_index)] = int(base)
        base_evidence.append({
            'model_index': int(model_index), 'base': int(base),
            'source': 'exact-bidirectionally-closed-full-xmodel-b4-replay',
        })

    # Some XModels have no recoverable Material** anchor because their material handles are all
    # inline. Recover only multi-target bases that are uniquely forced between neighbouring proven
    # ranges. This second stage never accepts a one-target/name-shape guess.
    secondary = _recover_unanchored_bases(
        relative_layouts,bases,requests,signature_names,set(accepted),minimum_targets=2,
    )
    single_owner_models={
        int(row['model_index'])
        for row in secondary.get('single_owner_interval_replay',{}).get('rows',())
    }
    for model_index, base in sorted(secondary['bases'].items()):
        old = bases.get(model_index)
        if old is not None and old != base:
            raise ValueError(f'XModel {model_index} secondary image base conflicts with 0x{old:X}/0x{base:X}')
        bases[model_index] = base
        base_evidence.append({
            'model_index': model_index,'base': base,
            'source': (
                'unique-single-owner-bounded-interval-replay'
                if model_index in single_owner_models else
                'unique-multitarget-image-cell-replay'
            ),
        })
    for target, name in sorted(secondary['aliases'].items()):
        old = accepted.get(target)
        if old is not None and old != name:
            raise ValueError(f'GfxImage target block4+0x{target:X} conflicts between {old!r} and {name!r}')
        accepted[target] = name
    rejected = [row for row in rejected if int(row['target']) not in accepted]

    return {
        'aliases': accepted,
        'alias_modes': {
            int(target): (
                'xmodel_full_stream_interval_replay'
                if int(target) in full_interval_targets else
                'xmodel_block4_cell_replay'
            )
            for target in accepted
        },
        'exact_direct_alias_targets':tuple(sorted(
            t for t in accepted
            if pc_zone is not None and (t in cell_identity or t in full_interval_targets)
        )),
        'insert_alias_cells_replayed':sum(int(row.get('insert_alias_cells',0)) for row in relative_layouts),
        'insert_aware_layout':pc_zone is not None,
        'handle_bases': bases,
        'handle_base_evidence': base_evidence,
        'handle_base_count': len(bases),
        'replayed_cell_count': len(cell_rows),
        'direct_identity_cell_count': len(cell_identity),
        'packed_edge_cell_count': len(cell_edge),
        'resolved_cell_count': len(resolved_cells),
        'packed_request_target_count': len(requests),
        'candidate_used_target_count': len((set(requests) & set(resolved_cells)) | set(secondary['aliases'])),
        'accepted_target_count': len(accepted),
        'accepted_slot_count': sum(len(requests[target]) for target in accepted),
        'rejected_targets': rejected,
        'rejected_target_count': len(rejected),
        'cycles': cycles,
        'stopped_for_water': stopped_for_water,
        'unanchored_multitarget_replay': secondary,
        'closed_full_stream_interval_replay': full_intervals,
        'cells': cell_rows,
    }


def replay_top_level_material_image_xstrings(
    pc_zone: bytes,
    asset_list,
    clipmap,
    techniques: Sequence[object],
    top_materials: Sequence[object],
) -> dict:
    """Recover top-level Material image names from a causal global B4 XString replay.

    A delayed GfxImage can own its root/loadDef while reusing an earlier XString for its name.
    The packed name pointer is exact identity evidence only when the earlier allocation is itself
    proven by typed LoadStream ownership.  This bridge starts at ClipMap DynEnt owner cells,
    replays owned PhysPreset XStrings, crosses only physically adjacent TechniqueSet XAssets, and
    finally replays the top-level Material.  Any source-order gap or unmodelled owned member stops
    the bridge without guessing.
    """
    if not top_materials:
        return {'slot_names': {}, 'passed': True, 'reason': 'no top-level Materials', 'xstrings': {}}

    assets_by_index = {int(a.index): a for a in asset_list.assets}
    techniques_by_index = {int(t.source_asset_index): t for t in techniques}
    top = sorted(top_materials, key=lambda row: int(row.source_asset_index))
    owners = sorted(clipmap.dyn_owned_phys_presets, key=lambda row: int(row.definition_ordinal))
    diagnostics = {'stage': 'dynent-anchor', 'fail_closed': True}

    def stopped(reason: str) -> dict:
        return {
            'slot_names': {}, 'passed': True, 'reason': reason, 'xstrings': {},
            'diagnostics': dict(diagnostics),
        }

    # FOLLOWING PhysPreset pointers name their definition-field cell exactly.  Those cells recover
    # each DynEnt definition-list base without using a physical file offset.
    list_bases: dict[str, int] = {}
    list_evidence: dict[str, list[str]] = defaultdict(list)
    for owner in owners:
        if owner.pointer_kind != 'following' or owner.pointer_owner_loadstream_offset is None:
            continue
        if not owner.pointer_owner_evidence:
            continue
        base = int(owner.pointer_owner_loadstream_offset) - int(owner.pointer_field_relative_offset)
        if base < 0 or base & 3:
            return stopped('invalid DynEnt definition-list base')
        old = list_bases.get(owner.list_kind)
        if old is not None and old != base:
            return stopped(f'conflicting {owner.list_kind} DynEnt definition-list bases')
        list_bases[owner.list_kind] = base
        list_evidence[owner.list_kind].extend(str(x) for x in owner.pointer_owner_evidence)

    counts = {'model': len(clipmap.dyn_models), 'brush': len(clipmap.dyn_brushes)}
    cursor = None
    for kind in ('model', 'brush'):
        count = counts[kind]
        if not count:
            continue
        base = list_bases.get(kind)
        if base is None:
            return stopped(f'{kind} DynEnt list has no exact owner-cell base')
        expected = _align(cursor, 4) if cursor is not None else base
        if base != expected:
            return stopped(f'{kind} DynEnt list base is not the typed successor of the previous list')
        cursor = base + count * 0x60
    if cursor is None or not owners:
        return stopped('no owned DynEnt PhysPreset bridge')

    xstrings: dict[int, dict] = {}
    allocation_rows: list[dict] = []

    def add_xstring(address: int, value: str, provenance: dict) -> None:
        normal = _normal_image_name(value) if provenance.get('image_name') else value
        if normal is None:
            raise ValueError('empty replayed XString identity')
        old = xstrings.get(address)
        if old is not None and old['value'] != normal:
            raise ValueError(
                f"global B4 XString 0x{address:X} conflicts: {old['value']!r}/{normal!r}"
            )
        xstrings[address] = {'value': normal, **provenance}

    def replay_string(raw: int, value: str | None, *, identity_proven: bool, label: str) -> bool:
        nonlocal cursor
        pointer = decode_pc_pointer(int(raw))
        if pointer.kind in ('null', 'packed'):
            return True
        if pointer.kind not in ('following', 'insert') or value is None or not identity_proven:
            return False
        if pointer.kind == 'insert':
            cursor = _align(cursor, 4) + 4
        address = cursor
        add_xstring(address, value, {'source': label, 'pointer_kind': pointer.kind})
        size = len(value.encode('latin-1')) + 1
        allocation_rows.append({'address': address, 'size': size, 'label': label})
        cursor += size
        return True

    physical = int(owners[0].source_root)
    for owner in owners:
        if int(owner.source_root) != physical:
            return stopped('owned PhysPreset graph is not physically contiguous')
        if owner.pointer_kind == 'insert':
            cursor = _align(cursor, 4)
            if owner.pointer_owner_loadstream_offset is None or cursor != int(owner.pointer_owner_loadstream_offset):
                return stopped('PhysPreset INSERT owner cell disagrees with typed replay')
            cursor += 4
        elif owner.pointer_kind != 'following':
            return stopped('owned PhysPreset pointer is neither FOLLOWING nor INSERT')
        preset = owner.preset
        if not replay_string(
            int(preset.serialized_name_pointer), preset.name,
            identity_proven=bool(preset.name_identity_proven),
            label=f'DynEnt {owner.list_kind}[{owner.index}] PhysPreset.name',
        ):
            return stopped('unproven owned PhysPreset name XString')
        sound = preset.sound_alias_prefix
        sound_proven = bool(preset.sound_alias_prefix_identity_proven)
        if not replay_string(
            int(preset.serialized_sound_alias_prefix_pointer), sound,
            identity_proven=sound_proven,
            label=f'DynEnt {owner.list_kind}[{owner.index}] PhysPreset.soundAliasPrefix',
        ):
            return stopped('unproven owned PhysPreset sound-prefix XString')
        physical = int(owner.source_end)
    if physical != int(clipmap.known_end):
        return stopped('ClipMap known_end does not equal the owned PhysPreset graph end')

    # IW3 declares distinct SP/MP ClipMap asset types; this parser's typed root can back either.
    clip_assets = [a for a in asset_list.assets if int(a.type_id) in (0x0A, 0x0B)]
    if len(clip_assets) != 1:
        return stopped('global XString bridge requires one ClipMap XAsset')
    first_material_index = int(top[0].source_asset_index)
    if first_material_index <= int(clip_assets[0].index):
        return stopped('top-level Material does not follow the ClipMap')

    diagnostics['stage'] = 'technique-bridge'
    for asset_index in range(int(clip_assets[0].index) + 1, first_material_index):
        asset = assets_by_index.get(asset_index)
        technique = techniques_by_index.get(asset_index)
        if asset is None or int(asset.type_id) != 0x05 or technique is None:
            return stopped('non-TechniqueSet XAsset interrupts ClipMap/Material bridge')
        if int(technique.root_offset) != physical:
            return stopped('TechniqueSet is not physically adjacent to previous owner')
        if decode_pc_pointer(_source_word(pc_zone, technique.root_offset, 'TechniqueSet.name')).kind not in ('following','insert'):
            return stopped('bridging TechniqueSet name is not inline')
        if decode_pc_pointer(int(technique.remapped_pointer)).kind not in ('null', 'packed'):
            return stopped('bridging TechniqueSet owns an unmodelled remap')
        if any(decode_pc_pointer(int(raw)).kind not in ('null', 'packed') for raw in technique.technique_pointers):
            return stopped('bridging TechniqueSet owns an unmodelled technique')
        if not replay_string(
            _source_word(pc_zone, technique.root_offset, 'TechniqueSet.name'), technique.name,
            identity_proven=True, label=f'TechniqueSet XAsset[{asset_index}].name',
        ):
            return stopped('TechniqueSet name replay failed')
        physical = int(technique.root_offset) + 0x94 + len(technique.name.encode('latin-1')) + 1

    slot_names: dict[tuple[int, int], str] = {}
    slot_rows: list[dict] = []
    diagnostics['stage'] = 'top-level-material'
    expected_index = first_material_index
    for row in top:
        if int(row.source_asset_index) != expected_index:
            return stopped('top-level Material XAssets are not consecutive')
        material = row.material
        if int(material.root_offset) != physical:
            return stopped('top-level Material is not physically adjacent to bridge')
        name_raw = _source_word(pc_zone, material.root_offset, f"Material '{material.name}'.name")
        if not replay_string(name_raw, material.name, identity_proven=True, label=f'Material XAsset[{expected_index}].name'):
            return stopped('top-level Material name replay failed')
        if material.contains_water:
            return stopped('top-level Material water graph is not supported by XString bridge')
        if material.textures:
            table_raw = _source_word(pc_zone, material.root_offset + 0x44, f"Material '{material.name}'.textureTable")
            table_pointer = decode_pc_pointer(table_raw)
            if table_pointer.kind not in ('following', 'insert'):
                return stopped('top-level Material texture table is not inline')
            cursor, _ = _insert_alias(cursor, table_raw, exact=True)
            cursor = _align(cursor, 4) + len(material.textures) * 0x0C
            for texture_index, texture in enumerate(material.textures):
                image_raw = serialized_image_pointer(material, texture_index)
                image_pointer = decode_pc_pointer(image_raw)
                if image_pointer.kind not in ('following', 'insert'):
                    continue
                image_root = texture.inline_image_root
                if image_root is None:
                    return stopped('inline top-level GfxImage root was not parsed')
                cursor, _ = _insert_alias(cursor, image_raw, exact=True)
                image_name_raw = _source_word(pc_zone, int(image_root) + 0x20, 'GfxImage.name')
                image_name_pointer = decode_pc_pointer(image_name_raw)
                if image_name_pointer.kind == 'packed':
                    if image_name_pointer.block != 4 or image_name_pointer.offset is None:
                        return stopped('top-level GfxImage name is not a block-4 XString reference')
                    evidence = xstrings.get(int(image_name_pointer.offset))
                    if evidence is None:
                        return stopped('top-level GfxImage name target is not an earlier proven XString')
                    key = (int(material.root_offset), texture_index)
                    slot_names[key] = str(evidence['value'])
                    slot_rows.append({
                        'material_root': int(material.root_offset), 'texture_index': texture_index,
                        'name_target': int(image_name_pointer.offset), 'identity': str(evidence['value']),
                        'xstring_source': evidence['source'],
                    })
                elif image_name_pointer.kind in ('following', 'insert'):
                    name = _normal_image_name(texture.inline_image_name)
                    if name is None:
                        return stopped('inline top-level GfxImage name identity missing')
                    if image_name_pointer.kind == 'insert':
                        cursor = _align(cursor, 4) + 4
                    add_xstring(cursor, name, {
                        'source': f"Material '{material.name}' GfxImage[{texture_index}].name",
                        'pointer_kind': image_name_pointer.kind, 'image_name': True,
                    })
                    cursor += len(name.encode('latin-1')) + 1
                elif image_name_pointer.kind != 'null':
                    return stopped('unsupported top-level GfxImage name pointer')
                cursor, _ = _insert_alias(
                    cursor, _source_word(pc_zone, int(image_root) + 0x04, 'GfxImage.loadDef'), exact=True,
                )
        if material.constants:
            raw = _source_word(pc_zone, material.root_offset + 0x48, f"Material '{material.name}'.constantTable")
            if decode_pc_pointer(raw).kind not in ('following', 'insert'):
                return stopped('top-level Material constants are not inline')
            cursor, _ = _insert_alias(cursor, raw, exact=True)
            cursor = _align(cursor, 16) + len(material.constants) * 0x20
        if material.state_bits:
            raw = _source_word(pc_zone, material.root_offset + 0x4C, f"Material '{material.name}'.stateTable")
            if decode_pc_pointer(raw).kind not in ('following', 'insert'):
                return stopped('top-level Material states are not inline')
            cursor, _ = _insert_alias(cursor, raw, exact=True)
            cursor = _align(cursor, 4) + len(material.state_bits) * 8
        physical = int(parse_material_at(pc_zone, int(material.root_offset))[1])
        expected_index += 1

    diagnostics.update({
        'stage': 'complete', 'list_bases': dict(list_bases),
        'xstring_allocation_count': len(xstrings), 'slot_count': len(slot_names),
        'final_block4_cursor': cursor, 'physical_end': physical,
        'allocations': allocation_rows, 'slots': slot_rows,
        'anchor_evidence': {key: tuple(value) for key, value in list_evidence.items()},
    })
    return {
        'slot_names': slot_names, 'passed': True, 'reason': None,
        'xstrings': {address: row['value'] for address, row in xstrings.items()},
        'diagnostics': diagnostics,
    }


def replay_gfxworld_sky_image_owner_alias(
    pc_zone: bytes,
    gfxworld,
    cell_base: int,
    requests: Mapping[int, Sequence[dict]],
) -> dict:
    """Invert the typed sky-to-cell B4 suffix to recover ``GfxWorld::skyImage``.

    ``skyImage`` is an XAsset pointer whose INSERT cell persists in block 4 while its GfxImage
    shell is temporary.  The cell address is accepted only when replaying the sky image, sun,
    reflection images, planes and nodes lands exactly on the independently proven GfxCell base.
    Candidate starts come solely from real packed image requests; names never select a start.
    """
    root = int(gfxworld.root_offset)
    sky_raw = _source_word(pc_zone, root + 0x28, 'GfxWorld.skyImage')
    if decode_pc_pointer(sky_raw).kind != 'insert':
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'skyImage is not INSERT'}

    branch_by_name = {row.name: row for row in gfxworld.full.branches}
    images_by_label = {row.label: row for row in gfxworld.full.images}
    sky = images_by_label.get('skyImage')
    if sky is None:
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'parsed skyImage is missing'}
    sky_name = _normal_image_name(sky.name)
    name_raw = _source_word(pc_zone, int(sky.root_offset) + 0x20, 'sky GfxImage.name')
    if sky_name is None or decode_pc_pointer(name_raw).kind not in ('following', 'insert'):
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'skyImage name is not an exact inline XString'}

    required_branches = ('sunLight', 'reflectionProbes', 'planes', 'nodes', 'cells/AABB/portals')
    if any(name not in branch_by_name for name in required_branches):
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'sky-to-cell physical suffix is incomplete'}
    sun = branch_by_name['sunLight']
    reflections = branch_by_name['reflectionProbes']
    planes = branch_by_name['planes']
    nodes = branch_by_name['nodes']
    cells = branch_by_name['cells/AABB/portals']
    if not (
        int(sky.end_offset) == int(sun.start) and int(sun.end) == int(reflections.start)
        and int(reflections.end) == int(planes.start) and int(planes.end) == int(nodes.start)
        and int(nodes.end) == int(cells.start)
    ):
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'sky-to-cell physical branches are not adjacent'}

    class Stop(Exception):
        pass

    def insert(cursor: int, raw: int, label: str) -> tuple[int, int]:
        pointer = decode_pc_pointer(int(raw))
        if pointer.kind not in ('null', 'following', 'insert', 'packed'):
            raise Stop(f'{label} pointer kind is unsupported')
        return _insert_alias(cursor, raw, exact=True)

    def image_members(cursor: int, image, label: str) -> int:
        raw = _source_word(pc_zone, int(image.root_offset) + 0x20, f'{label}.name')
        pointer = decode_pc_pointer(raw)
        if pointer.kind not in ('null', 'following', 'insert', 'packed'):
            raise Stop(f'{label}.name pointer kind is unsupported')
        cursor, _ = insert(cursor, raw, f'{label}.name')
        if pointer.kind in ('following', 'insert'):
            name = _normal_image_name(image.name)
            if name is None:
                raise Stop(f'{label}.name inline identity is missing')
            cursor += len(name.encode('latin-1')) + 1
        loaddef_raw = _source_word(pc_zone, int(image.root_offset) + 0x04, f'{label}.loadDef')
        cursor, _ = insert(cursor, loaddef_raw, f'{label}.loadDef')
        return cursor

    def replay(candidate: int) -> tuple[int, dict]:
        if candidate & 3:
            raise Stop('sky INSERT candidate is not four-byte aligned')
        cursor = int(candidate) + 4
        cursor = image_members(cursor, sky, 'skyImage')

        sun_raw = _source_word(pc_zone, root + 0xC8, 'GfxWorld.sunLight')
        sun_pointer = decode_pc_pointer(sun_raw)
        if sun_pointer.kind not in ('following', 'insert'):
            raise Stop('sunLight is not inline')
        cursor, _ = insert(cursor, sun_raw, 'GfxWorld.sunLight')
        cursor = _align(cursor, 4) + 0x40
        if int(sun.end) != int(sun.start) + 0x40:
            raise Stop('inline sun LightDef suffix is not modelled')
        lightdef_raw = _source_word(pc_zone, int(sun.start) + 0x3C, 'GfxLight.lightDef')
        if decode_pc_pointer(lightdef_raw).kind not in ('null', 'packed'):
            raise Stop('sun LightDef owns an unsupported inline graph')

        reflection_raw = _source_word(pc_zone, root + 0xE8, 'GfxWorld.reflectionProbes')
        reflection_count = int(gfxworld.reflection_probe_count)
        if reflection_count and decode_pc_pointer(reflection_raw).kind not in ('following', 'insert'):
            raise Stop('reflection root array is not inline')
        cursor, _ = insert(cursor, reflection_raw, 'GfxWorld.reflectionProbes')
        cursor = _align(cursor, 4) + reflection_count * 0x10
        reflection_roots = int(reflections.start)
        physical = reflection_roots + reflection_count * 0x10
        reflection_image_count = 0
        for index in range(reflection_count):
            owner_raw = _source_word(
                pc_zone, reflection_roots + index * 0x10 + 0x0C,
                f'GfxReflectionProbe[{index}].reflectionImage',
            )
            pointer = decode_pc_pointer(owner_raw)
            if pointer.kind not in ('null', 'following', 'insert', 'packed'):
                raise Stop('reflection image owner pointer kind is unsupported')
            if pointer.kind not in ('following', 'insert'):
                continue
            cursor, _ = insert(cursor, owner_raw, f'GfxReflectionProbe[{index}].reflectionImage')
            image = images_by_label.get(f'reflectionProbe[{index}]')
            if image is None or int(image.root_offset) != physical:
                raise Stop('reflection image physical owner order is not exact')
            cursor = image_members(cursor, image, f'reflectionProbe[{index}]')
            physical = int(image.end_offset)
            reflection_image_count += 1
        if physical != int(reflections.end):
            raise Stop('reflection image physical replay does not reach branch end')

        plane_raw = _source_word(pc_zone, root + 0xF4, 'GfxWorld.planes')
        if int(gfxworld.plane_count) and decode_pc_pointer(plane_raw).kind not in ('following', 'insert'):
            raise Stop('plane array is not inline')
        cursor, _ = insert(cursor, plane_raw, 'GfxWorld.planes')
        cursor = _align(cursor, 4) + int(gfxworld.plane_count) * 0x14

        node_raw = _source_word(pc_zone, root + 0xF8, 'GfxWorld.nodes')
        if int(gfxworld.node_count) and decode_pc_pointer(node_raw).kind not in ('following', 'insert'):
            raise Stop('node array is not inline')
        cursor, _ = insert(cursor, node_raw, 'GfxWorld.nodes')
        cursor = _align(cursor, 2) + int(gfxworld.node_count) * 2

        cell_raw = _source_word(pc_zone, root + 0x104, 'GfxWorld.cells')
        if int(gfxworld.cell_count) and decode_pc_pointer(cell_raw).kind not in ('following', 'insert'):
            raise Stop('cell array is not inline')
        cursor, _ = insert(cursor, cell_raw, 'GfxWorld.cells')
        predicted_cell_base = _align(cursor, 4)
        return predicted_cell_base, {
            'sky_alias_cell': int(candidate), 'sky_image_root': int(sky.root_offset),
            'sky_identity': sky_name, 'reflection_image_count': reflection_image_count,
            'predicted_cell_base': predicted_cell_base, 'proven_cell_base': int(cell_base),
        }

    candidates = []
    structural_reason = None
    for target in sorted(int(value) for value in requests if int(value) < int(cell_base)):
        try:
            predicted, row = replay(target)
        except Stop as exc:
            structural_reason = str(exc)
            break
        if predicted == int(cell_base):
            candidates.append(row)
    if structural_reason is not None:
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': structural_reason}
    if len(candidates) != 1:
        return {
            'aliases': {}, 'exact_direct_alias_targets': (),
            'reason': f'sky INSERT inverse candidate count is {len(candidates)}',
            'candidates': tuple(candidates),
        }
    row = candidates[0]
    target = int(row['sky_alias_cell'])
    if any(int(use['material_root']) <= int(sky.root_offset) for use in requests[target]):
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'reason': 'sky alias does not precede every packed use'}
    return {
        'aliases': {target: sky_name},
        'exact_direct_alias_targets': (target,),
        'reason': None,
        'provenance': {target: row},
        'candidate_count': 1,
    }


def structural_image_aliases(all_materials: Sequence[MaterialRecord], structural_index) -> dict:
    """Serve every packed block-4 image request from the structural reader.

    The reader already resolved every Material texture (and water_t image)
    pointer field to its GfxImage OBJECT root during the typed zone walk, so
    cell identity needs no block-4 cursor replay, no AABB anchor and no
    backward topology proofs: each requesting FIELD maps to its image object
    through the reader's allocation map, and the request's own packed offset
    keys the alias.  Requests that meet at one cell must agree on the image.
    """
    objects = {int(row['root']): row
               for row in structural_index.report.get('objects', ())
               if row.get('root') is not None}
    requests = _packed_requests(all_materials)
    aliases: dict[int, str] = {}
    provenance: dict[int, dict] = {}
    slots = 0
    for material in all_materials:
        if not material.textures:
            continue
        table = None
        for texture_index in range(len(material.textures)):
            raw = serialized_image_pointer(material, texture_index)
            pointer = decode_pc_pointer(int(raw))
            if pointer.kind != 'packed' or pointer.block != 4 or pointer.offset is None:
                continue
            if table is None:
                table = structural_index.pointer(int(material.root_offset) + 0x44)
                if not isinstance(table, int):
                    raise ValueError(
                        f"Material '{material.name}' texture table did not structurally resolve")
            cell_field = table + texture_index * 0x0C + 8
            if material.water_by_texture.get(texture_index) is not None:
                water_root = structural_index.pointer(cell_field)
                if not isinstance(water_root, int):
                    raise ValueError(
                        f"Material '{material.name}' water_t did not structurally resolve")
                field = water_root + PC_WATER_IMAGE_POINTER
            else:
                field = cell_field
            target = structural_index.pointer(field)
            if not isinstance(target, int):
                raise ValueError(
                    f"Material '{material.name}' texture[{texture_index}] image pointer "
                    'did not structurally resolve')
            row = objects.get(target)
            if row is None or row.get('type') != 'GfxImage':
                raise ValueError(
                    f"Material '{material.name}' texture[{texture_index}] packed image resolves "
                    f"to {(row or {}).get('type', 'no object')!r} at 0x{target:X}")
            name = _normal_image_name(row.get('name'))
            if not name:
                raise ValueError(f'structurally resolved GfxImage at 0x{target:X} has no name')
            offset = int(pointer.offset)
            old = aliases.get(offset)
            if old is not None and old != name:
                raise ValueError(
                    f'structural image alias conflict at block4+0x{offset:X}: {old!r}/{name!r}')
            aliases[offset] = name
            slots += 1
            provenance.setdefault(offset, {
                'owner': 'structural-reader', 'material': material.name,
                'material_root': int(material.root_offset),
                'texture_index': texture_index, 'image_root': target,
            })
    if set(aliases) != set(requests):
        missing = sorted(set(requests) - set(aliases))
        raise ValueError('structural image aliases missed requests: '
                         + ', '.join(f'0x{x:X}' for x in missing[:8]))
    return {
        'aliases': aliases,
        'exact_direct_alias_targets': tuple(sorted(aliases)),
        'alias_modes': {offset: 'gfxworld_structural_exact' for offset in aliases},
        'accepted_target_count': len(aliases), 'accepted_slot_count': slots,
        'provenance': provenance,
        'diagnostics': {'mode': 'structural_exact', 'requests': len(requests),
                        'served': len(aliases)},
    }


def replay_gfxworld_image_aliases(
    pc_zone: bytes,
    gfxworld,
    gfx_materials: Sequence[MaterialRecord],
    all_materials: Sequence[MaterialRecord],
    *,
    structural_index=None,
) -> dict:
    """Replay the GfxWorld-owned MaterialTextureDef graph from a typed AABB anchor.

    The cursor replay stays authoritative because its byproducts (the exact
    graph end feeds ``draw_array_base``) are needed beyond identity.  A
    structural index adds an INDEPENDENT certification: every replayed cell
    identity is compared against the reader's per-field resolution, and a
    graph that offers fewer than two backward topology proofs (mp_osg_raid)
    is accepted when the two derivations agree instead of failing closed.

    The world preamble is replayed from the exact block-4 Cell base proven by packed AABB reuse.
    Retail streams may place up to one final 16-byte alignment pad before recursively loading the
    Material graph.  The pad is accepted only when the graph's own packed image pointers uniquely
    select one base by landing on earlier typed ``MaterialTextureDef::image`` cells.  Names never
    participate in base selection; they are read only after exact cell identity is established.
    """
    structural = None
    structural_error = None
    if structural_index is not None:
        try:
            structural = structural_image_aliases(all_materials, structural_index)
        except ValueError as error:
            structural_error = str(error)
    if not gfx_materials:
        return {'aliases': {}, 'exact_direct_alias_targets': (), 'accepted_target_count': 0,
                'accepted_slot_count': 0, 'diagnostics': {'reason': 'no GfxWorld Materials'}}

    def branch(name: str):
        rows = [row for row in gfxworld.full.branches if row.name == name]
        if len(rows) != 1:
            raise ValueError(f'GfxWorld replay branch {name!r} cardinality={len(rows)}')
        return rows[0]

    def u16(offset: int) -> int:
        if offset < 0 or offset + 2 > len(pc_zone):
            raise ValueError('GfxWorld replay u16 outside zone')
        return int.from_bytes(pc_zone[offset:offset + 2], 'little')

    def i32(offset: int) -> int:
        value = _source_word(pc_zone, offset, 'GfxWorld replay i32')
        return value - 0x100000000 if value & 0x80000000 else value

    def allocate(cursor: int, raw: int, size: int, alignment: int, label: str) -> int:
        pointer = decode_pc_pointer(int(raw))
        if size == 0:
            if pointer.kind not in ('null', 'following', 'insert', 'packed'):
                raise ValueError(f'{label} has unsupported zero-count pointer')
            return cursor
        if pointer.kind not in ('following', 'insert'):
            raise ValueError(f'{label} count is nonzero but pointer is {pointer.kind}')
        cursor, _ = _insert_alias(cursor, raw, exact=True)
        return _align(cursor, alignment) + size

    aabb = resolve_aabb_reuse(pc_zone, gfxworld)
    if not aabb.block4_cell_logical_base or aabb.bounds_perfect_candidates != 1:
        raise ValueError('GfxWorld image replay requires one exact AABB block-4 anchor')
    requests = _packed_requests(all_materials)
    sky_replay = replay_gfxworld_sky_image_owner_alias(
        pc_zone, gfxworld, int(aabb.block4_cell_logical_base), requests,
    )
    cell_branch = branch('cells/AABB/portals')
    roots = int(cell_branch.start)
    physical = roots + int(gfxworld.cell_count) * PC_CELL
    cursor = int(aabb.block4_cell_logical_base) + int(gfxworld.cell_count) * PC_CELL
    states = []
    for cell_index in range(int(gfxworld.cell_count)):
        root = roots + cell_index * PC_CELL
        states.append((
            i32(root + 0x18), _source_word(pc_zone, root + 0x1C, 'GfxCell.aabbTree'),
            i32(root + 0x20), _source_word(pc_zone, root + 0x24, 'GfxCell.portals'),
            i32(root + 0x28), _source_word(pc_zone, root + 0x2C, 'GfxCell.cullGroups'),
            pc_zone[root + 0x30], _source_word(pc_zone, root + 0x34, 'GfxCell.reflectionProbes'),
        ))
    for aabb_count, aabb_raw, portal_count, portal_raw, cull_count, cull_raw, probe_count, probe_raw in states:
        if aabb_count:
            cursor = allocate(cursor, aabb_raw, aabb_count * PC_AABB, 4, 'GfxCell.aabbTree')
            aabb_roots = physical
            physical += aabb_count * PC_AABB
            for index in range(aabb_count):
                root = aabb_roots + index * PC_AABB
                count = u16(root + 0x22)
                raw = _source_word(pc_zone, root + 0x24, 'GfxAabbTree.smodelIndexes')
                pointer = decode_pc_pointer(raw)
                if pointer.kind in ('following', 'insert'):
                    cursor = allocate(cursor, raw, count * 2, 2, 'GfxAabbTree.smodelIndexes')
                    physical += count * 2
                elif pointer.kind not in ('null', 'packed'):
                    raise ValueError('unsupported GfxAabbTree.smodelIndexes pointer')
        if portal_count:
            cursor = allocate(cursor, portal_raw, portal_count * PC_PORTAL, 4, 'GfxCell.portals')
            portal_roots = physical
            physical += portal_count * PC_PORTAL
            for index in range(portal_count):
                root = portal_roots + index * PC_PORTAL
                count = pc_zone[root + 0x28]
                raw = _source_word(pc_zone, root + 0x24, 'GfxPortal.vertices')
                if count:
                    cursor = allocate(cursor, raw, count * 0x0C, 4, 'GfxPortal.vertices')
                    physical += count * 0x0C
        if cull_count:
            cursor = allocate(cursor, cull_raw, cull_count * 4, 4, 'GfxCell.cullGroups')
            physical += cull_count * 4
        if probe_count:
            cursor = allocate(cursor, probe_raw, probe_count, 1, 'GfxCell.reflectionProbes')
            physical += probe_count
    if physical != int(cell_branch.end):
        raise ValueError(
            f'GfxWorld cell replay physical end 0x{physical:X} != 0x{int(cell_branch.end):X}'
        )

    lighting = branch('lightmaps/LightGrid')
    physical = int(lighting.start)
    root = int(gfxworld.root_offset)
    cursor = allocate(
        cursor, _source_word(pc_zone, root + 0x10C, 'GfxWorld.lightmaps'),
        int(gfxworld.lightmap_count) * 8, 4, 'GfxWorld.lightmaps',
    )
    lightmap_roots = physical
    physical += int(gfxworld.lightmap_count) * 8
    images_by_root = {int(image.root_offset): image for image in gfxworld.full.images}
    lightmap_insert_aliases = 0
    for lightmap_index in range(int(gfxworld.lightmap_count)):
        for member_offset, member_name in ((0, 'primary'), (4, 'secondary')):
            raw = _source_word(
                pc_zone, lightmap_roots + lightmap_index * 8 + member_offset,
                f'GfxLightmapArray.{member_name}',
            )
            pointer = decode_pc_pointer(raw)
            if pointer.kind not in ('following', 'insert'):
                if pointer.kind != 'null':
                    raise ValueError('packed GfxWorld lightmap owner is unsupported')
                continue
            cursor, count = _insert_alias(cursor, raw, exact=True)
            lightmap_insert_aliases += count
            image = images_by_root.get(physical)
            if image is None:
                raise ValueError(f'GfxWorld lightmap image root 0x{physical:X} was not parsed')
            name_raw = _source_word(pc_zone, physical + 0x20, 'GfxImage.name')
            cursor, count = _insert_alias(cursor, name_raw, exact=True)
            lightmap_insert_aliases += count
            if decode_pc_pointer(name_raw).kind in ('following', 'insert'):
                cursor += len(image.name.encode('latin-1')) + 1
            loaddef_raw = _source_word(pc_zone, physical + 0x04, 'GfxImage.loadDef')
            cursor, count = _insert_alias(cursor, loaddef_raw, exact=True)
            lightmap_insert_aliases += count
            physical = int(image.end_offset)

    grid = root + 0x110
    grid_rows = (
        (0x1C, int(gfxworld.full.lightgrid_row_count), 2, 2, 'rowDataStart'),
        (0x24, int(gfxworld.full.lightgrid_raw_bytes), 1, 1, 'rawRowData'),
        (0x2C, int(gfxworld.full.lightgrid_entry_count), 4, 4, 'entries'),
        (0x34, int(gfxworld.full.lightgrid_color_count), 0xA8, 4, 'colors'),
    )
    for pointer_offset, count, size, alignment, label in grid_rows:
        raw = _source_word(pc_zone, grid + pointer_offset, f'GfxLightGrid.{label}')
        pointer = decode_pc_pointer(raw)
        cursor = allocate(cursor, raw, count * size, alignment, f'GfxLightGrid.{label}')
        if count and pointer.kind in ('following', 'insert'):
            physical += count * size
    if physical != int(lighting.end):
        raise ValueError(
            f'GfxWorld lighting replay physical end 0x{physical:X} != 0x{int(lighting.end):X}'
        )

    models = branch('brush-model/material-memory roots')
    physical = int(models.start)
    cursor = allocate(
        cursor, _source_word(pc_zone, root + 0x154, 'GfxWorld.models'),
        int(gfxworld.model_count) * PC_BRUSH_MODEL, 4, 'GfxWorld.models',
    )
    physical += int(gfxworld.model_count) * PC_BRUSH_MODEL
    cursor = allocate(
        cursor, _source_word(pc_zone, root + 0x178, 'GfxWorld.materialMemory'),
        int(gfxworld.material_memory_count) * 8, 4, 'GfxWorld.materialMemory',
    )
    material_memory_roots = physical
    physical += int(gfxworld.material_memory_count) * 8
    if physical != int(models.end):
        raise ValueError(
            f'GfxWorld models replay physical end 0x{physical:X} != 0x{int(models.end):X}'
        )
    preamble_cursor = cursor

    inline_roots = tuple(int(x) for x in gfxworld.full.inline_material_roots)
    roots_in_order = []
    for index in range(int(gfxworld.material_memory_count)):
        raw = _source_word(pc_zone, material_memory_roots + index * 8, 'MaterialMemory.material')
        pointer = decode_pc_pointer(raw)
        if pointer.kind in ('following', 'insert'):
            if len(roots_in_order) >= len(inline_roots):
                raise ValueError('GfxWorld MaterialMemory owns more inline Materials than were parsed')
            roots_in_order.append(inline_roots[len(roots_in_order)])
        elif pointer.kind != 'packed':
            raise ValueError('GfxWorld MaterialMemory has unsupported null/non-owner material')
    if tuple(roots_in_order) != inline_roots[:len(roots_in_order)]:
        raise ValueError('GfxWorld MaterialMemory owner order is not exact')
    # A GfxWorld can own up to two further inline Materials through the sun/outdoor
    # alias pointers at root+0x180 and root+0x184.  The source parser appends those
    # after the materialMemory graph because they are serialized after the vertex and
    # vertex-layer branches, so they are deliberately outside this replay: their B4
    # allocations begin past graph_cursor and cannot affect the cells replayed here.
    # Before this was distinguished, any map with a sun or outdoor Material failed as
    # "MaterialMemory owner order is not exact" - mp_getaway has both.
    trailing_roots = inline_roots[len(roots_in_order):]
    if len(trailing_roots) > 2:
        raise ValueError(
            f'GfxWorld has {len(trailing_roots)} inline Materials beyond materialMemory; '
            'only the sun/outdoor alias pair is a known owner'
        )
    material_by_root = {int(material.root_offset): material for material in gfx_materials}
    if set(material_by_root) != set(inline_roots):
        raise ValueError('GfxWorld replay material universe differs from the parsed inline Materials')
    if any(root not in material_by_root for root in roots_in_order):
        raise ValueError('GfxWorld replay material universe differs from MaterialMemory owners')

    # Replay on the absolute B4 cursor. Aligning a relative offset and translating it later is not
    # equivalent when a Material constant table requests 16-byte alignment.
    graph_base = preamble_cursor
    graph_cursor = graph_base
    cell_rows: list[dict] = []
    loaddef_insert_aliases = 0
    for material_index, material_root in enumerate(roots_in_order):
        material = material_by_root[material_root]
        owner_raw = _source_word(
            pc_zone, material_memory_roots + material_index * 8, 'MaterialMemory.material',
        )
        graph_cursor, _ = _insert_alias(graph_cursor, owner_raw, exact=True)
        graph_cursor, _ = _insert_alias(
            graph_cursor, _source_word(pc_zone, material.root_offset, 'Material.name'), exact=True,
        )
        graph_cursor += len(material.name.encode('latin-1')) + 1
        if material.textures:
            table_raw = _source_word(pc_zone, material.root_offset + 0x44, 'Material.textureTable')
            graph_cursor, _ = _insert_alias(graph_cursor, table_raw, exact=True)
            graph_cursor = _align(graph_cursor, 4)
            table_base = graph_cursor
            graph_cursor += len(material.textures) * 0x0C
            for texture_index, texture in enumerate(material.textures):
                if texture_index in material.water_by_texture:
                    # A water TextureDef's +8 cell points at the water_t object, not at a
                    # GfxImage.  Its image pointer cell lives inside that object and is
                    # recorded in the pointee pass below, once its B4 address is known.
                    continue
                raw = serialized_image_pointer(material, texture_index)
                pointer = decode_pc_pointer(raw)
                cell_rows.append({
                    'cell_address': table_base + texture_index * 0x0C + 8,
                    'material_index': material_index, 'material_root': int(material.root_offset),
                    'material': material.name, 'texture_index': texture_index,
                    'raw': int(raw), 'pointer_kind': pointer.kind,
                    'pointer_block': pointer.block, 'pointer_offset': pointer.offset,
                    'identity': _normal_image_name(texture.inline_image_name),
                })
            for texture_index, texture in enumerate(material.textures):
                water = material.water_by_texture.get(texture_index)
                if water is not None:
                    # water_t is a persistent block-4 object exactly like the TextureDef
                    # array: 0x44 PC bytes, then the inline H0 (complex, 8 bytes) and wTerm
                    # (float, 4 bytes) arrays of M*N entries each.  Replaying it keeps every
                    # later cell address in this GfxWorld correct; skipping it shifted the
                    # whole remaining graph, which is why water maps failed closed here.
                    graph_cursor = _align(graph_cursor, 4)
                    water_base = graph_cursor
                    graph_cursor += PC_WATER_ROOT
                    entries = int(water.m) * int(water.n)
                    if entries and decode_pc_pointer(int(water.h0_pointer)).kind in ('following', 'insert'):
                        graph_cursor = _align(graph_cursor, 4) + entries * 8
                    if entries and decode_pc_pointer(int(water.wterm_pointer)).kind in ('following', 'insert'):
                        graph_cursor = _align(graph_cursor, 4) + entries * 4
                    raw = int(water.image_pointer)
                    pointer = decode_pc_pointer(raw)
                    cell_rows.append({
                        'cell_address': water_base + PC_WATER_IMAGE_POINTER,
                        'material_index': material_index, 'material_root': int(material.root_offset),
                        'material': material.name, 'texture_index': texture_index,
                        'raw': raw, 'pointer_kind': pointer.kind,
                        'pointer_block': pointer.block, 'pointer_offset': pointer.offset,
                        'identity': _normal_image_name(texture.inline_image_name),
                    })
                else:
                    raw = serialized_image_pointer(material, texture_index)
                    pointer = decode_pc_pointer(raw)
                if pointer.kind not in ('following', 'insert'):
                    continue
                image_root = texture.inline_image_root
                if image_root is None:
                    raise ValueError('GfxWorld inline GfxImage root missing')
                graph_cursor, _ = _insert_alias(graph_cursor, raw, exact=True)
                name_raw = _source_word(pc_zone, int(image_root) + 0x20, 'GfxImage.name')
                graph_cursor, _ = _insert_alias(graph_cursor, name_raw, exact=True)
                name = _normal_image_name(texture.inline_image_name)
                if name is not None:
                    graph_cursor += len(name.encode('latin-1')) + 1
                loaddef_raw = _source_word(pc_zone, int(image_root) + 0x04, 'GfxImage.loadDef')
                graph_cursor, count = _insert_alias(graph_cursor, loaddef_raw, exact=True)
                loaddef_insert_aliases += count
        if material.constants:
            graph_cursor, _ = _insert_alias(
                graph_cursor, _source_word(pc_zone, material.root_offset + 0x48, 'Material.constantTable'), exact=True,
            )
            graph_cursor = _align(graph_cursor, 16) + len(material.constants) * 0x20
        if material.state_bits:
            graph_cursor, _ = _insert_alias(
                graph_cursor, _source_word(pc_zone, material.root_offset + 0x4C, 'Material.stateTable'), exact=True,
            )
            graph_cursor = _align(graph_cursor, 4) + len(material.state_bits) * 8

    # Every graph-local packed edge must point backwards to an exactly replayed TextureDef cell.
    # This topology check is independent of names and rejects a cursor that merely has plausible
    # aggregate size.
    cells_by_address = {int(row['cell_address']): row for row in cell_rows}
    topology_targets: set[int] = set()
    for use in cell_rows:
        if use['pointer_kind'] != 'packed' or use['pointer_block'] != 4 or use['pointer_offset'] is None:
            continue
        target = int(use['pointer_offset'])
        owner = cells_by_address.get(target)
        if owner is None or int(owner['material_index']) >= int(use['material_index']):
            continue
        topology_targets.add(target)
    structurally_certified = False
    if len(topology_targets) < 2:
        # The graph offers too few packed backward edges to certify itself.
        # The structural reader's per-field resolution certifies instead: the
        # replayed identities are compared against it below, and any mismatch
        # or missing coverage raises there.
        if structural is None:
            raise ValueError(
                'GfxWorld absolute owner replay has fewer than two backward topology proofs'
                + (f' (structural certification unavailable: {structural_error})'
                   if structural_error else ''))
        structurally_certified = True

    identity_by_address: dict[int, str] = {}
    edge_by_address: dict[int, int] = {}
    provenance_by_address: dict[int, dict] = {}
    for row in cell_rows:
        address = int(row['cell_address'])
        name = row['identity']
        if name:
            old = identity_by_address.get(address)
            if old is not None and old != name:
                raise ValueError(f'GfxWorld replay identity conflict at b4+0x{address:X}')
            identity_by_address[address] = str(name)
            provenance_by_address[address] = {
                'owner': 'gfxworld.materialMemory', 'graph_base': graph_base,
                'material': row['material'], 'material_root': row['material_root'],
                'texture_index': row['texture_index'], 'cell_address': address,
            }
        elif row['pointer_kind'] == 'packed' and row['pointer_block'] == 4 and row['pointer_offset'] is not None:
            edge_by_address[address] = int(row['pointer_offset'])

    resolved = dict(identity_by_address)
    cycles = []
    def resolve(address: int, trail: tuple[int, ...] = ()) -> str | None:
        if address in resolved:
            return resolved[address]
        if address in trail:
            cycles.append(trail + (address,))
            return None
        target = edge_by_address.get(address)
        if target is None:
            return None
        name = resolve(target, trail + (address,))
        if name is not None:
            resolved[address] = name
            provenance_by_address[address] = {
                'owner': 'gfxworld.materialMemory.alias-chain', 'cell_address': address,
                'target_cell': target, 'target_provenance': provenance_by_address.get(target),
            }
        return name
    for address in tuple(edge_by_address):
        resolve(address)
    if cycles:
        raise ValueError(f'GfxWorld image-cell alias cycles: {cycles[:2]}')

    aliases = {target: resolved[target] for target in sorted(set(requests) & set(resolved))}
    provenance = {target: provenance_by_address[target] for target in aliases}
    for target, name in sky_replay.get('aliases', {}).items():
        old = aliases.get(int(target))
        if old is not None and old != name:
            raise ValueError(
                f'GfxWorld sky replay conflicts at block4+0x{int(target):X}: {old!r}/{name!r}'
            )
        aliases[int(target)] = str(name)
        provenance[int(target)] = sky_replay['provenance'][int(target)]
    sky_targets = set(int(x) for x in sky_replay.get('exact_direct_alias_targets', ()))
    exact_direct = tuple(sorted((set(aliases) & set(identity_by_address)) | sky_targets))
    structural_matches = 0
    structural_mismatches = []
    if structural is not None:
        for target, name in aliases.items():
            expected = structural['aliases'].get(int(target))
            if expected == name:
                structural_matches += 1
            else:
                structural_mismatches.append({'target': int(target), 'replayed': str(name),
                                              'structural': expected})
        if structurally_certified:
            # The structural reader is the certifier here: the derivations
            # must agree on every replayed cell and prove at least one.
            if structural_mismatches:
                raise ValueError(
                    'GfxWorld replay disagrees with the structural reader at '
                    + ', '.join(f"b4+0x{row['target']:X}" for row in structural_mismatches[:4]))
            if not structural_matches:
                raise ValueError(
                    'GfxWorld replay has neither topology proofs nor a structurally matched cell')
    return {
        'aliases': aliases, 'exact_direct_alias_targets': exact_direct,
        'structurally_certified': structurally_certified,
        'structural_matches': structural_matches,
        'structural_mismatches': structural_mismatches,
        'alias_modes': {
            int(target): (
                'gfxworld_sky_owner_interval_replay'
                if int(target) in sky_targets else
                'gfxworld_block4_owner_replay'
            )
            for target in aliases
        },
        'accepted_target_count': len(aliases),
        'accepted_slot_count': sum(len(requests[target]) for target in aliases),
        'graph_base': graph_base, 'graph_size': graph_cursor - graph_base,
        'preamble_cursor': preamble_cursor, 'alignment_padding': 0,
        'topology_target_count': len(topology_targets), 'topology_second_score': 0,
        'loaddef_insert_alias_cells': loaddef_insert_aliases,
        'lightmap_insert_alias_cells': lightmap_insert_aliases,
        'aabb_cell_base': int(aabb.block4_cell_logical_base),
        'provenance': provenance,
        'sky_owner_interval_replay': sky_replay,
        'cells': cell_rows,
        'diagnostics': {
            'anchor': 'GfxWorld AABB packed-reuse/bounds proof',
            'preamble_cursor': preamble_cursor, 'graph_base': graph_base,
            'alignment_padding': 0,
            'absolute_cursor_replay': True,
            'topology_targets': tuple(sorted(topology_targets)),
        },
    }

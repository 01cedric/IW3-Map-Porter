"""Replay verified PS3 allocation events and relocate native pointer fields.

Physical stream positions and logical block positions are independent. Alignment
moves only the logical cursor for this retail stream. TEMP frames restore their
parent cursor; persistent blocks and INSERT cells retain their allocations.
"""
import bisect
import struct
from cod4porter.zone_writer import RelocatingZoneWriter


def rebuild(zone, profile, replacements=None, inserts=None, generated=None, recovery=None):
    replacements = replacements or {}
    inserts = inserts or {}
    generated = generated or {}
    generated_allocations = 0
    writer = RelocatingZoneWriter()
    writer.write_raw(replacements.get(36, zone[36:52]))
    mappings = {b: [] for b in range(7)}
    physical = []
    clones = []

    def event(record, scope=None):
        nonlocal generated_allocations
        op = record[0]
        if op == 0:
            writer.push_block(record[1])
        elif op == 1:
            writer.pop_block()
        elif op == 2:
            writer.align_logical_only(record[1] + 1)
        elif op == 5:
            writer.write_in_block(bytes.fromhex(record[1]))
            generated_allocations += 1
        elif op == 6:
            emit_clone(record[1])
        elif op in (3, 4):
            old_address = record[1]
            block, offset = divmod(old_address - 0x40000000, 0x08000000)
            if writer.current_block_index != block:
                raise ValueError('Relocation profile block state diverged.')
            new_offset = writer.current_location.offset
            if op == 3:
                _, _, position, size = record
                changes = scope['replacements'] if scope is not None else replacements
                data = changes.get(position, zone[position:position + size])
                new_position = writer.physical_position
                writer.write_in_block(data)
                table = scope['physical'] if scope is not None else physical
                table.append((position, size, new_position, len(data)))
                new_size = len(data)
            else:
                size = new_size = record[2]
                writer.reserve_logical_only(size)
            table = scope['mappings'][block] if scope is not None else mappings[block]
            table.append((offset, size, new_offset, new_size))
        else:
            raise ValueError('Unknown relocation profile operation.')

    def emit_clone(clone):
        scope = dict(mappings={b: [] for b in range(7)}, physical=[],
                     replacements=clone['replacements'], fixups=clone['fixups'])
        for cloned_record in clone['events']:
            event(cloned_record, scope)
        clones.append(scope)

    for index, record in enumerate(profile['events']):
        for generated_record in generated.get(index, []):
            event(generated_record)
        for clone in inserts.get(index, []):
            emit_clone(clone)
        event(record)

    # Persistent addresses remain monotonic. TEMP frames can reuse addresses;
    # a profile with packed references into those frames needs lifetime tracking.
    starts = {b: [a[0] for a in rows] for b, rows in mappings.items() if b != 0}

    def fix_fields(fields, ranges, local_mappings=None):
        physical_starts = [row[0] for row in ranges]
        local_starts = ({b: [a[0] for a in rows] for b, rows in local_mappings.items()}
                        if local_mappings else {})
        for position, raw, kind in fields:
            # A typed pointer can deliberately select another existing asset or
            # XString. Relocate the edited packed value, not the captured value.
            source = bisect.bisect_right(physical_starts, position) - 1
            if source < 0:
                raise ValueError('Pointer field has no serialized allocation.')
            old_pos, old_size, new_pos, output_size = ranges[source]
            if not old_pos <= position or position + 4 > old_pos + old_size or old_size != output_size:
                raise ValueError('Pointer field is outside a stable typed record.')
            raw = struct.unpack_from('>I', writer.physical, new_pos + position - old_pos)[0]
            if raw == 0xFFFFFFFF:
                # Replacement supplies inline data at this field's native load point.
                # The generated event stream and native-loader check own its payload.
                continue
            if raw in (0, 0xFFFFFFFE):
                raise ValueError('An existing packed pointer cannot change to an inline allocation marker.')
            block, offset = (raw - 1) >> 29, (raw - 1) & 0x1FFFFFFF
            if block == 0 or block not in starts:
                raise ValueError('Packed reference requires unsupported block lifetime tracking.')
            rows, index = mappings[block], starts[block]
            if local_mappings and local_mappings[block]:
                candidate = bisect.bisect_right(local_starts[block], offset) - 1
                if candidate >= 0:
                    start, size, _, _ = local_mappings[block][candidate]
                    if start <= offset < start + size:
                        rows, index = local_mappings[block], local_starts[block]
            target = bisect.bisect_right(index, offset) - 1
            if target < 0:
                raise ValueError('Packed target has no allocation.')
            start, size, new_start, new_size = rows[target]
            if not start <= offset < start + size or offset - start >= new_size:
                raise ValueError('Packed reference points outside its rebuilt allocation.')
            source = bisect.bisect_right(physical_starts, position) - 1
            if source < 0:
                raise ValueError('Pointer field has no serialized allocation.')
            old_pos, old_size, new_pos, output_size = ranges[source]
            if not old_pos <= position or position + 4 > old_pos + old_size or old_size != output_size:
                raise ValueError('Pointer field is outside a stable typed record.')
            pointer = (block << 29) + new_start + offset - start + 1
            struct.pack_into('>I', writer.physical, new_pos + position - old_pos, pointer)

    fix_fields(profile['fixups'], physical)
    for scope in clones:
        fix_fields(scope['fixups'], scope['physical'], scope['mappings'])
    original_header = struct.unpack_from('>9I', zone)
    delta = [writer.block_positions[i] - profile['high'][i] for i in range(7)]
    # The first word describes serialized stream bytes, not the sum of
    # logical block growth. Logical alignment introduces no physical padding.
    stream_delta = writer.physical_position - profile['stream_end']
    header = (original_header[0] + stream_delta, original_header[1],
              *[original_header[i + 2] + delta[i] for i in range(7)])
    if any(value < 0 or value > 0xFFFFFFFF for value in header):
        raise ValueError('Rebuilt zone declaration is outside the format limits.')
    struct.pack_into('>9I', writer.physical, 0, *header)
    writer.write_raw(zone[profile['stream_end']:])
    if recovery is not None:
        # Reverse recipe: keep unchanged exported spans, store only replaced
        # original allocations. This avoids embedding a duplicate retail UI.
        parts = [zone[:52].hex()]
        cursor = 52
        for old_pos, size, new_pos, new_size in physical:
            if old_pos != cursor:
                raise ValueError('Original recovery allocations are not contiguous.')
            original = zone[old_pos:old_pos+size]
            if size == new_size and original == writer.physical[new_pos:new_pos+size]:
                if isinstance(parts[-1], list) and sum(parts[-1]) == new_pos:
                    parts[-1][1] += size
                else:
                    parts.append([new_pos,size])
            else:
                parts.append(original.hex())
            cursor += size
        if cursor != profile['stream_end']:
            raise ValueError('Original recovery stream did not reach its end.')
        parts.append(zone[cursor:].hex())
        recovery['parts'] = parts
    return bytes(writer.physical), dict(
        delta=delta, serialized_stream_delta=stream_delta,
        allocations=len(physical) + sum(len(s['physical']) for s in clones),
        fixups=len(profile['fixups']) + sum(len(s['fixups']) for s in clones),
        added_rows=sum(c.get('kind', 'row') == 'row' for rows in inserts.values() for c in rows),
        generated_allocations=generated_allocations, declared_blocks=list(header[2:]))

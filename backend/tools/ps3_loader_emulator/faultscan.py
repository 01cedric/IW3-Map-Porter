"""Fault forensics for the emulated PS3 zone loader.

When DB_LoadXFile faults inside an asset, the raw report ("access to unmapped
address 0x3f03") names neither the asset nor the poisoned bytes.  This module
turns that fault into evidence.

Two independent signals:

* **The over-read.**  The loader streams each payload with a size taken from a
  field it just read.  A field with a wrong value makes ``Load_Stream`` consume
  far too much, and the loader then dereferences whatever bytes now sit where
  the next structural pointer was expected.  The single largest recent stream
  read therefore points straight at the field that lied - this is the root
  cause, and the poisoned pointer that finally faults is only its aftermath.

* **The fault address.**  PS3 packed pointers are ``((block << 29) | offset)
  + 1`` with blocks 0..6.  The 3-bit block field admits one invalid value -
  block 7, raw range 0xE0000000..0xFFFFFFFD (FOLLOWING/INSERT sit at the top).
  The console's block-base table has seven entries, so a block-7 cell adds an
  out-of-table base (0 on the emulated globals) and dereferences the bare
  offset: a low-page fault whose address IS the poisoned cell's offset field.
  A fault inside a mapped block instead decodes back to (block, offset) via the
  zone's own base table.

Everything is pure computation over bytes and the recorded stream ring - no
ELF, no emulator state - so the porter's test suite can pin the behavior.
"""
from __future__ import annotations

import re

FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE
BLOCK7_MIN = 0xE0000000
BLOCK7_MAX = 0xFFFFFFFD
CENSUS_CAP = 512 * 1024          # scan this many bytes from the asset ROOT for the census
CENSUS_LIMIT = 40
OVERSIZED_READ = 1 << 20         # a single stream read past 1 MiB is the over-read signal

_FAULT_RE = re.compile(r'unmapped address (0x[0-9a-fA-F]+|\d+)')


def predicted_block7_values(fault_address: int) -> list[int]:
    """Raw u32s that would send the loader to ``fault_address`` via the
    invalid block-7 decode (the read may land a few bytes into the target)."""
    out = []
    for delta in (0, 1, 2, 3):
        offset = (fault_address - delta) & 0x1FFFFFFF
        out.append(((((7 << 29) | offset) + 1) & 0xFFFFFFFF))
    return out


def analyse_fault_address(message: str, block_base, sizes) -> dict | None:
    match = _FAULT_RE.search(message or '')
    if not match:
        return None
    text = match.group(1)
    address = int(text, 16 if text.startswith('0x') else 10)
    out = {'fault_address': '0x%X' % address}
    if address < 0x10000:
        out['model'] = ('low-page fault: a pointer cell decoded as PS3 block 7 - an encoding no '
                        'valid zone uses - so the loader added an out-of-table base and reached '
                        'offset 0x%X of nothing' % address)
        out['predicted_cell_values'] = ['0x%08X' % v for v in predicted_block7_values(address)]
        return out
    for block, base in enumerate(block_base or ()):
        if base and 0 <= address - base < 0x08000000:
            declared = sizes[block] if sizes and block < len(sizes) else None
            out['model'] = ('access %d bytes into block %d (declared size %s): a pointer offset '
                            'beyond what this zone allocates'
                            % (address - base, block, declared))
            out['block'] = block
            out['block_offset'] = address - base
            return out
    out['model'] = 'address outside every zone block and below no known mapping model'
    return out


def oversized_reads(stream_ring) -> list[dict]:
    """Recent stream reads large enough to be an over-read, largest first.
    Each is a candidate for the field whose value the writer got wrong."""
    rows = []
    for (file_offset, dest, n, block) in (stream_ring or ()):
        if n >= OVERSIZED_READ:
            rows.append({'read_bytes': n, 'from_file_offset': file_offset,
                         'dest': '0x%X' % dest, 'dest_block': block})
    rows.sort(key=lambda r: -r['read_bytes'])
    return rows


def block7_census(window: bytes, window_file_start: int, asset_root: int,
                  *, limit: int = CENSUS_LIMIT) -> list[dict]:
    """Every u32 (any byte alignment) in block-7 pointer space, minus the
    FOLLOWING/INSERT markers.  Structural data almost never lives there, so
    hits are candidates for a poisoned pointer cell.  Offsets are reported both
    absolute and relative to the asset root; 4-aligned hits are listed first."""
    rows = []
    n = len(window)

    def aligned_word(position):
        a = position & ~3
        return int.from_bytes(window[a:a + 4], 'big') if a + 4 <= n else None

    for i in range(0, n - 3):
        value = int.from_bytes(window[i:i + 4], 'big')
        if value in (FOLLOWING, INSERT) or not (BLOCK7_MIN <= value <= BLOCK7_MAX):
            continue
        if i % 4 and (aligned_word(i) in (FOLLOWING, INSERT) or aligned_word(i + 3) in (FOLLOWING, INSERT)):
            continue                                        # spill from an adjacent marker
        encoded = (value - 1) & 0xFFFFFFFF
        file_offset = window_file_start + i
        rows.append({'file_offset': file_offset,
                     'offset_from_asset_root': file_offset - asset_root,
                     'value': '0x%08X' % value,
                     'ps3_decode': 'block 7 (invalid) offset 0x%X' % (encoded & 0x1FFFFFFF),
                     'aligned4': i % 4 == 0})
    rows.sort(key=lambda r: (not r['aligned4'], r['file_offset']))
    return rows[:limit]


def exact_matches(zone: bytes, window_start: int, window_end: int, asset_root: int,
                  predicted_values) -> list[dict]:
    """Every occurrence of a predicted fault-cell value across the WHOLE asset
    window (uncapped - a 4-byte scan is cheap even over tens of MiB)."""
    if not predicted_values:
        return []
    targets = {v.to_bytes(4, 'big'): v for v in predicted_values}
    rows = []
    for pattern, value in targets.items():
        start = window_start
        while True:
            hit = zone.find(pattern, start, window_end)
            if hit < 0:
                break
            rows.append({'file_offset': hit, 'offset_from_asset_root': hit - asset_root,
                         'value': '0x%08X' % value, 'aligned4': (hit - asset_root) % 4 == 0})
            start = hit + 1
            if len(rows) >= 64:
                break
    rows.sort(key=lambda r: (not r['aligned4'], r['file_offset']))
    return rows


def build_fault_forensics(message: str, zone: bytes, asset_file_start: int, stream_pos: int,
                          asset_index: int, asset_type: str, sizes, block_base,
                          stream_ring) -> dict:
    """Assemble the full forensics dict + human log lines for one load fault."""
    window_start = asset_file_start
    window_end = min(max(stream_pos, window_start), len(zone))
    span = window_end - window_start
    fault = analyse_fault_address(message, block_base, sizes)
    predicted = [int(v, 16) for v in fault['predicted_cell_values']] if fault and 'predicted_cell_values' in fault else []

    over = oversized_reads(stream_ring)
    matches = exact_matches(zone, window_start, window_end, asset_file_start, predicted)
    # The census scans only the first CENSUS_CAP bytes from the asset ROOT,
    # where the structural pointer cells live (a runaway payload can push the
    # window to tens of MiB; the poisoned cell is near the root, not the tail).
    census_end = min(window_end, window_start + CENSUS_CAP)
    census = block7_census(zone[window_start:census_end], window_start, asset_file_start)

    recent = [{'read_bytes': n, 'from_file_offset': f, 'dest': '0x%X' % p, 'block': b}
              for (f, p, n, b) in (stream_ring or [])][-16:]

    lines = ['fault forensics: asset %d (%s), stream span 0x%X..0x%X (%d bytes consumed)'
             % (asset_index, asset_type, asset_file_start, window_end, span)]
    if fault:
        lines.append('fault model: %s' % fault['model'])
    if over:
        top = over[0]
        lines.append('OVER-READ: a single stream read of %d bytes (%.1f MiB) from file 0x%X - '
                     'the size field driving this read is the likely root cause, not the cell that '
                     'finally faulted' % (top['read_bytes'], top['read_bytes'] / (1 << 20),
                                          top['from_file_offset']))
    if matches:
        for row in matches[:6]:
            lines.append('poisoned cell (exact fault-address match) at file 0x%X = asset_root+0x%X: %s'
                         % (row['file_offset'], row['offset_from_asset_root'], row['value']))
    elif census:
        for row in census[:6]:
            lines.append('block-7 pointer candidate at file 0x%X = asset_root+0x%X: %s -> %s'
                         % (row['file_offset'], row['offset_from_asset_root'], row['value'], row['ps3_decode']))
    if not matches and not census and not over:
        lines.append('no over-read and no block-7 cell near the root: the fault offset may be a '
                     'valid-block pointer past its declared size - see the fault model')
    return {
        'schema': 'iw3-linker-fault-forensics/v2',
        'asset_index': asset_index,
        'asset_type': asset_type,
        'stream_span': [asset_file_start, window_end],
        'bytes_consumed': span,
        'fault': fault,
        'oversized_reads': over,
        'exact_fault_matches': matches,
        'block7_census': census,
        'census_bytes_scanned': census_end - window_start,
        'recent_stream_ops': recent,
        'log_lines': lines,
    }

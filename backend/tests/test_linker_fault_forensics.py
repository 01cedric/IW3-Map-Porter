#!/usr/bin/env python3
"""Fault forensics for the emulated loader (faultscan.py, v2).

A MEMORY FAULT during zone load must name the failing asset AND why it faulted.
The strongest signal is the over-read: a wrong size field makes Load_Stream
consume far too much and the loader then dereferences the bytes that follow.
These tests pin the over-read detection, the block-7 fault model (PS3 packed
pointers are ((block<<29)|offset)+1 with blocks 0..6; block 7 is the one
invalid encoding, raw 0xE0000000..0xFFFFFFFD), the exact fault-address match
scan over the whole asset window, and the root-anchored census.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'ps3_loader_emulator'))

from faultscan import (analyse_fault_address, block7_census, build_fault_forensics,
                       exact_matches, oversized_reads, predicted_block7_values,
                       FOLLOWING, INSERT)

BASES = [0x40000000 + i * 0x08000000 for i in range(7)]
SIZES = [8192, 230128, 8192, 83741184, 33879872, 0, 14378968]


def test_predicted_values_cover_the_reported_fault():
    values = predicted_block7_values(0x3F03)
    assert values[0] == (((7 << 29) | 0x3F03) + 1) == 0xE0003F04
    assert values[1] == 0xE0003F03


def test_analyse_low_page_and_block_models():
    low = analyse_fault_address('access to unmapped address 0x3f03', BASES, SIZES)
    assert low['fault_address'] == '0x3F03' and 'block 7' in low['model']
    assert '0xE0003F04' in low['predicted_cell_values']

    inside = analyse_fault_address('access to unmapped address %#x' % (BASES[3] + 0x123), BASES, SIZES)
    assert inside['block'] == 3 and inside['block_offset'] == 0x123
    assert analyse_fault_address('some other error', BASES, SIZES) is None


def test_oversized_reads_ranks_the_over_read():
    ring = [(0x100, 0x40000000, 64, 0),
            (0x200, 0x50000000, 12_000_000, 3),
            (0x300, 0x60000000, 2_000_000, 4)]
    over = oversized_reads(ring)
    assert len(over) == 2                       # the 64-byte read is below the 1 MiB threshold
    assert over[0]['read_bytes'] == 12_000_000 and over[0]['from_file_offset'] == 0x200
    assert over[0]['dest_block'] == 3


def test_census_finds_planted_cell_and_skips_markers():
    window = bytearray(b'\x00' * 64)
    struct.pack_into('>I', window, 12, FOLLOWING)
    struct.pack_into('>I', window, 16, INSERT)
    struct.pack_into('>I', window, 24, 0xE0003F04)
    struct.pack_into('>I', window, 40, ((3 << 29) | 0x10) + 1)   # legal block 3
    rows = block7_census(bytes(window), 0x1000, 0x1000)
    assert len(rows) == 1, rows
    assert rows[0]['file_offset'] == 0x1018 and rows[0]['offset_from_asset_root'] == 0x18
    assert rows[0]['value'] == '0xE0003F04' and rows[0]['aligned4']


def test_census_skips_marker_spill_but_keeps_real_cells():
    window = bytearray(b'\x00' * 64)
    window[8:16] = b'\xFF' * 8                       # two FOLLOWING markers
    struct.pack_into('>I', window, 21, 0xE1000010)  # unaligned real cell
    struct.pack_into('>I', window, 28, FOLLOWING)
    struct.pack_into('>I', window, 32, 0xE0000042)  # aligned real cell
    offsets = {r['file_offset'] for r in block7_census(bytes(window), 0, 0)}
    assert 21 in offsets and 32 in offsets
    assert all(not (8 <= off < 16) for off in offsets)


def test_exact_matches_scan_whole_window_uncapped():
    zone = bytearray(b'\x00' * (1 << 20))
    struct.pack_into('>I', zone, 0x40, 0xE0003F04)          # near the root
    struct.pack_into('>I', zone, 0xF0000, 0xE0003F03)       # far past a 512 KiB census cap
    rows = exact_matches(bytes(zone), 0, len(zone), 0, predicted_block7_values(0x3F03))
    found = {(r['file_offset'], r['value']) for r in rows}
    assert (0x40, '0xE0003F04') in found and (0xF0000, '0xE0003F03') in found


def test_build_forensics_prioritizes_over_read_and_reports_root_relative():
    # A 12 MiB stream span (runaway blob) with the poisoned cell right after the root.
    span = 12 * 1024 * 1024
    zone = bytearray(b'\x00' * (0x1000 + span))
    root = 0x1000
    struct.pack_into('>I', zone, root + 0x2C, 0xE0003F04)   # poisoned cell, root+0x2C
    ring = [(root, 0x40000000, 0x40, 0), (root + 0x40, 0x5A000000, span - 0x100, 3)]
    report = build_fault_forensics('access to unmapped address 0x3f03', bytes(zone),
                                   root, root + span, 291, 'TECHSET', SIZES, BASES, ring)
    assert report['schema'] == 'iw3-linker-fault-forensics/v2'
    assert report['asset_index'] == 291 and report['asset_type'] == 'TECHSET'
    assert report['bytes_consumed'] == span
    # the over-read is detected and ranked first
    assert report['oversized_reads'] and report['oversized_reads'][0]['read_bytes'] == span - 0x100
    assert any('OVER-READ' in line for line in report['log_lines'])
    # the poisoned cell is found by exact match and reported relative to the root
    hit = report['exact_fault_matches'][0]
    assert hit['offset_from_asset_root'] == 0x2C
    assert any('asset_root+0x2C' in line for line in report['log_lines'])
    json.dumps(report)                                       # stays serializable


def main():
    test_predicted_values_cover_the_reported_fault()
    test_analyse_low_page_and_block_models()
    test_oversized_reads_ranks_the_over_read()
    test_census_finds_planted_cell_and_skips_markers()
    test_census_skips_marker_spill_but_keeps_real_cells()
    test_exact_matches_scan_whole_window_uncapped()
    test_build_forensics_prioritizes_over_read_and_reports_root_relative()
    print(json.dumps({'passed': True, 'tests': 7}))


if __name__ == '__main__':
    main()

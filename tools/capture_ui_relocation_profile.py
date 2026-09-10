"""Capture a PS3 UI allocation profile for format research; no UI edits are made.

A new profile also needs a reviewed menu layout/template before the GUI can use it.
"""
import argparse
import bisect
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from gui_emulator import validate_elf
    from cod4porter.backend.v4_ffio import read_ps3_fastfile
    validate_elf(str(args.elf))
    os.environ['IW3_PS3_ELF'] = str(args.elf.resolve())
    sys.path.insert(0, str(ROOT / 'backend/tools/ps3_loader_emulator'))
    from zoneload import ZoneLoader
    zone = read_ps3_fastfile(args.ui).zone
    loader = ZoneLoader(zone, verbose=True)
    events, fixups, pending, allocations = [], [], [], []
    native_read = loader.stubs[0xb3be8]

    def read(cpu):
        pending.append((loader.pos, cpu.r[4] & 0xFFFFFFFF, cpu.r[3] & 0xFFFFFFFF))
        native_read(cpu)
    loader.stubs[0xb3be8] = read

    def record(op, cpu):
        amount, address = cpu.r[3] & 0xFFFFFFFF, loader.cursor()
        if pending:
            position, _, destination = pending[0]
            total = sum(row[1] for row in pending)
            if op != 'inc' or total != amount or destination != address or position+total != loader.pos:
                raise ValueError('Unmodeled read/allocation sequence; profile capture stopped.')
            if any(a[0]+a[1] != b[0] or a[2]+a[1] != b[2] for a,b in zip(pending,pending[1:])):
                raise ValueError('Noncontiguous native read group.')
            events.append([3,address,position,amount])
            allocations.append((address,amount,position,loader.pos))
            pending.clear()
        elif op == 'inc': events.append([4,address,amount])
        elif op == 'align': events.append([2,amount])
        elif op == 'push': events.append([0,amount])
        elif op == 'pop': events.append([1])

    for address, operation in ((0xe03d8,'align'),(0xe03f8,'inc')):
        native = loader.stubs[address]
        def wrapper(cpu, native=native, operation=operation):
            record(operation,cpu)
            native(cpu)
        loader.stubs[address] = wrapper
    for address, operation in ((0xe0318,'push'),(0xe0388,'pop')):
        loader.cpu.hooks[address] = lambda cpu, operation=operation: record(operation,cpu)
    for address, kind in ((0xe0458,'alias'),(0xe0490,'pointer')):
        loader.cpu.hooks[address] = lambda cpu, kind=kind: fixups.append(
            (kind,loader.pos,cpu.r[3]&0xFFFFFFFF,loader.m.u32(cpu.r[3]&0xFFFFFFFF)))
    print('Capturing native allocations; this can take several minutes.',flush=True)
    loader.load()
    if pending or loader.unknown or any(a>b for a,b in zip(loader.high,loader.sizes)):
        raise ValueError('Native capture did not finish within the declared blocks.')
    per_block = {b:[] for b in range(7)}
    for allocation in allocations:
        per_block[(allocation[0]-0x40000000)//0x08000000].append(allocation)
    starts = {b:[a[0] for a in rows] for b,rows in per_block.items() if b!=0}
    fields=[]
    for kind,position,address,raw in fixups:
        block=(address-0x40000000)//0x08000000
        if block==0:
            found=next((a for a in reversed(per_block[0]) if a[2]<position and a[0]<=address and address+4<=a[0]+a[1]),None)
        else:
            index=bisect.bisect_right(starts[block],address)-1
            found=per_block[block][index] if index>=0 else None
        if found is None:raise ValueError('Native fixup has no serialized source.')
        base,size,source,_=found
        if not base<=address or address+4>base+size:raise ValueError('Fixup exceeds its typed record.')
        field=source+address-base
        if struct.unpack_from('>I',zone,field)[0]!=raw:raise ValueError('Fixup source differs from original bytes.')
        fields.append([field,raw,kind])
    profile=dict(source_sha256=hashlib.sha256(zone).hexdigest(),zone_size=len(zone),
                 stream_end=loader.pos,events=events,fixups=fields,high=loader.high)
    from gui_ui_relocation import rebuild
    if rebuild(zone,profile)[0]!=zone:raise ValueError('Captured profile failed byte-exact replay.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(zlib.compress(json.dumps(profile,separators=(',',':')).encode(),9))
    print(f'PASS: {len(allocations)} allocations, {len(fields)} fixups, byte-exact replay.')


if __name__=='__main__':main()

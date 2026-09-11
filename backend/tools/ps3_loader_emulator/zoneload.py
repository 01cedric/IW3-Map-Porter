#!/usr/bin/env python3
"""Emulate the IW3 PS3 fastfile loader over a decompressed zone stream."""
import os
import struct, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf
from ppcemu import Mem, Cpu, UnknownCall, MASK32, MASK64

S_STREAM = 0x10605D00          # DB stream position state
G_LOAD = 0x102FDD80            # loader var block
V_XASSETLIST = 0x102FE29C
V_SCRIPTSTRINGS = 0x102FE2F8
V_XASSET = 0x102FE294
DEFER_COUNT = 0x10605D0C
DEFER_TABLE = 0x10605F30

BLOCK_BASE = [0x40000000 + i * 0x08000000 for i in range(7)]
SCRATCH = 0x20000000
STACK_TOP = 0x30000000

TYPE_NAMES = {0: 'XMODELPIECES', 1: 'PHYSPRESET', 2: 'XANIM', 3: 'XMODEL', 4: 'MATERIAL',
              5: 'PIXELSHADER', 6: 'VERTEXSHADER', 7: 'TECHSET', 8: 'IMAGE', 9: 'SOUND',
              10: 'SNDCURVE', 11: 'LOADED_SOUND', 12: 'CLIPMAP_SP', 13: 'CLIPMAP_MP',
              14: 'COMWORLD', 15: 'GAMEWORLD_SP', 16: 'GAMEWORLD_MP', 17: 'MAP_ENTS',
              18: 'GFXWORLD', 19: 'LIGHTDEF', 21: 'FONT', 22: 'MENUFILE', 23: 'MENU',
              24: 'LOCALIZE', 25: 'WEAPON', 27: 'FX', 28: 'IMPACTFX', 33: 'RAWFILE',
              34: 'STRINGTABLE'}


class ZoneLoader:
    def __init__(self, zone, verbose=False, machine=None, block_base=None):
        self.z = zone
        self.pos = 0
        self.verbose = verbose
        self.overflow = []
        self.unknown = collections.Counter()
        self.notes = []
        self.block_base = list(block_base) if block_base else list(BLOCK_BASE)
        if machine is None:
            self.m = Mem()
            # map ELF segments
            for off, size, base in ps3elf.SEGS:
                self.m.write(base, ps3elf.DATA[off:off + size])
            self.stubs = {}
            self._install_stubs()
            self.cpu = Cpu(self.m, self.stubs)
            self.cpu.r[1] = STACK_TOP
            self.cpu.r[2] = 0x6C6988          # TOC base used by the disassembler annotations
        else:
            # a shared machine: memory, cpu and stubs persist across zones; only the
            # stream source (this zone's bytes) is rebound.
            self.m = machine.m; self.cpu = machine.cpu; self.stubs = machine.stubs
            machine.bind_zone(self)
            self.links = machine.links; self.externals = machine.externals
        self.scratch = SCRATCH
        self.trace = None
        # Always-on ring of the most recent stream reads: on a load fault this
        # maps the poisoned bytes back to their zone-file offsets (faultscan).
        self.stream_ring = collections.deque(maxlen=32)
        def on_loadstream(cpu):
            ats = cpu.r[3] & MASK32; ptr = cpu.r[4] & MASK32; n = cpu.r[5] & MASK32
            if not (ats and n): return
            block = self.m.u32(S_STREAM + 8)
            self.stream_ring.append((self.pos, ptr, n, block))
            if self.trace is not None:
                self.trace.append((cpu.lr - 4, self.pos, n, block, ptr))
        self.cpu.hooks[0xe0608] = on_loadstream
        self.hdr = struct.unpack('>9I', zone[:36])
        self.sizes = list(self.hdr[2:9])
        self.pos = 36
        self.filereads = 0

    # ---------------------------------------------------------------- stubs
    def _install_stubs(self):
        def db_load(cpu):
            ptr = cpu.r[3] & MASK64
            n = cpu.r[4] & MASK32
            if n > (1 << 31): n = 0
            data = self.z[self.pos:self.pos + n]
            if len(data) < n:
                raise EOFError('stream exhausted: want %d at %#x (have %d)' % (n, self.pos, len(data)))
            self.m.write(ptr, data)
            self.pos += n
            self.filereads += n
            cpu.r[3] = 0
        def memset(cpu):
            ptr = cpu.r[3] & MASK64; v = cpu.r[4] & 0xFF; n = cpu.r[5] & MASK32
            if n and n < (1 << 28): self.m.write(ptr, bytes([v]) * n)
            cpu.r[3] = ptr
        def nop(cpu):
            cpu.r[3] = 0
        def ret1(cpu):
            cpu.r[3] = 1
        self.stubs[0xb3be8] = db_load
        self.stubs[0x60a390] = memset
        def sl_getstring(cpu):
            self._slnext += 1
            cpu.r[3] = self._slnext
        self._slnext = 0
        RUNTIME_NOP = tuple(int(x,0) for x in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime_nop.txt')).read().split())
        for a in RUNTIME_NOP:
            self.stubs[a] = nop
        self.stubs[0x180578] = sl_getstring
        def link_entry(cpu):
            # DB_LinkXAssetEntry(outSlot, type, header) -> identity link, records externals
            out = cpu.r[3] & MASK64; typ = cpu.r[4] & MASK32; hdr = cpu.r[5] & MASK32
            self.m.w32(out, hdr)
            self.links.append((typ, hdr))
            cpu.r[3] = 0
        self.links = []
        self.stubs[0xdfa28] = link_entry

        def find_header(cpu):
            # DB_FindXAssetHeader(type, name) -> a resolved external header.  No stream bytes.
            typ = cpu.r[3] & MASK32
            nameptr = cpu.r[4] & MASK64
            raw = self.m.read(nameptr, 128)
            nm = raw.split(b'\x00')[0].decode('latin1', 'replace')
            self.externals.append((typ, nm))
            self._extnext += 0x100
            cpu.r[3] = SCRATCH + 0x100000 + self._extnext
        self.externals = []
        self._extnext = 0
        self.stubs[0xdf478] = find_header

        def alloc_pos(cpu):
            mask = cpu.r[3] & MASK32
            cur = self.m.u32(S_STREAM + 4)
            cur = (cur + mask) & (~mask & MASK32)
            self.m.w32(S_STREAM + 4, cur)
            self._mark(cur)
            cpu.r[3] = cur
        def inc_pos(cpu):
            n = cpu.r[3] & MASK32
            cur = (self.m.u32(S_STREAM + 4) + n) & MASK32
            self.m.w32(S_STREAM + 4, cur)
            self._mark(cur)
            cpu.r[3] = 0
        self.stubs[0xe03d8] = alloc_pos
        self.stubs[0xe03f8] = inc_pos

    def _mark(self, cur):
        b = self.m.u32(S_STREAM + 8)
        used = (cur - self.block_base[b]) & MASK32
        if used > self.high[b]:
            self.high[b] = used

    def note(self, msg):
        self.notes.append(msg)
        if self.verbose: print(msg)

    # -------------------------------------------------------------- helpers
    def call(self, addr, *args):
        return self.cpu.run(addr, list(args))

    def cursor(self): return self.m.u32(S_STREAM + 4)
    def cur_block(self): return self.m.u32(S_STREAM + 8)

    def block_use(self):
        """Current per-block high-water usage in bytes."""
        out = []
        cur_idx = self.cur_block(); cur = self.cursor()
        for i in range(7):
            saved = self.m.u32(S_STREAM + 0x10 + 4 * i)
            v = cur if i == cur_idx else saved
            out.append((v - self.block_base[i]) & MASK32 if v else 0)
        return out

    # ---------------------------------------------------------------- setup
    def setup(self):
        m = self.m
        blocks = SCRATCH
        for i in range(7):
            m.w32(blocks + i * 8, self.block_base[i])
            m.w32(blocks + i * 8 + 4, self.sizes[i])
        self.blocks_addr = blocks
        m.w32(DEFER_COUNT, 0)
        self.high = [0] * 7
        self.call(0xe0228, blocks)

    def track(self):
        pass

    # ----------------------------------------------------------------- load
    def load(self):
        m = self.m
        self.setup()
        xal = SCRATCH + 0x1000
        m.w32(V_XASSETLIST, xal)
        # DB_LoadXFileData(xal, 16)
        m.write(xal, self.z[self.pos:self.pos + 16]); self.pos += 16
        self.call(0xe0318, 4)                       # DB_PushStreamPos(VIRTUAL)
        m.w32(V_SCRIPTSTRINGS, xal)
        self.call(0xd52d0, 0)                       # Load_ScriptStringList(false)
        self.call(0xe0388)                          # pop
        self.track()
        self.string_end = self.pos
        self.call(0xe0318, 4)
        count = m.u32(xal + 8)
        assets_marker = m.u32(xal + 12)
        self.asset_count = count
        rows = []
        self.assets = rows
        if assets_marker:
            p = self.call(0xbed60)                  # DB_AllocStreamPos for the XAsset array
            m.w32(xal + 12, p)
            m.w32(V_XASSET, p)
            self.call(0xe0608, 1, p, count * 8)     # Load_Stream(true, assets, count*8)
            self.track()
            for i in range(count):
                typ = m.u32(p)
                before = self.pos
                bu = self.block_use()
                m.w32(V_XASSET, p)
                try:
                    self.call(0xda950, 0)           # Load_XAsset(false)
                except Exception as exc:
                    self.note('ASSET %d type %s FAILED at file %#x: %r'
                              % (i, TYPE_NAMES.get(typ, typ), before, exc))
                    try:
                        from faultscan import build_fault_forensics
                        self.fault_forensics = build_fault_forensics(
                            str(exc), self.z, before, self.pos, i,
                            str(TYPE_NAMES.get(typ, typ)), self.sizes,
                            self.block_base, list(self.stream_ring))
                        for line in self.fault_forensics.get('log_lines', ()):
                            self.note(line)
                    except Exception:
                        self.fault_forensics = None
                    raise
                self.track()
                au = self.block_use()
                rows.append({'index': i, 'type': typ, 'name': TYPE_NAMES.get(typ, str(typ)),
                             'file_start': before, 'file_end': self.pos,
                             'blocks': [au[k] - bu[k] for k in range(7)]})
                p += 8
        self.call(0xe0388)
        self.track()
        self.assets = rows
        # flush deferred (block 2/3) payloads
        dcount = m.u32(DEFER_COUNT)
        self.deferred_count = dcount
        self.deferred_bytes = 0
        for i in range(dcount):
            ptr = m.u32(DEFER_TABLE + i * 8)
            size = m.u32(DEFER_TABLE + i * 8 + 4)
            self.deferred_bytes += size
        self.call(0xe04c0)
        return rows

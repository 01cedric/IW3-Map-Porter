#!/usr/bin/env python3
"""Multi-zone PS3 asset database emulation: load the always-loaded zones and a map zone
into one memory image, run the game's own DB_LinkXAssetEntry for every asset, and then
ask the game's own DB_FindXAssetHeader what the engine finds after the map has loaded.

This is the stage after the stream loader: it reproduces the asset pool limits, the
",name" external resolution, the zone override rules, the default-asset fallbacks and
every Com_Error / Com_Printf the DB code emits - by executing that code.
"""
import os, struct, sys, collections, zlib, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf
from ppcemu import Mem, Cpu, UnknownCall, MASK32, MASK64
from zoneload import (ZoneLoader, S_STREAM, SCRATCH, STACK_TOP, TYPE_NAMES)

DB_BASE = 0x102FE380              # g_assetPool / DB state
ENTRY_POOL = DB_BASE + 0x50000 - 0xCC8     # XAssetEntry[0] (16 bytes each); index 0 unused
ENTRY_POOL_FREE = ENTRY_POOL + 16          # first free entry threaded by DB init
ENTRY_POOL_COUNT = 29998
POOL_HEADS = 0x69516C             # per-type: pointer to the free-list head variable
POOL_INITS = 0x680A64             # per-type: init function descriptor
POOL_SIZES = 0x1001945C           # per-type: pool capacity
TYPE_NAME_TABLE = 0x100E17C4      # per-type: name string pointer
DEFAULT_NAME_TABLE = 0x68094C     # per-type: default asset name pointer
NUM_TYPES = 35

F_DB_LINK = 0xdfa28               # DB_LinkXAssetEntry(XAssetHeader *slot, type, header)
F_DB_FIND = 0xdf478               # DB_FindXAssetHeader(XAssetHeader *out, type, name)
F_GET_NAME = 0xb3278              # DB_GetXAssetName(XAssetEntry *)
F_TYPE_SIZE = 0xb32e8             # DB_GetXAssetTypeSize(type)
F_COM_ERROR = 0x161f58
F_COM_PRINTF = (0x162718, 0x162588, 0x1623b0)
F_SYS_SLEEP = 0x170920
F_DB_COPYINFO = 0xde970           # post-load: re-add every asset whose name already existed
COPYINFO_COUNT = DB_BASE + 0x140000 + 20488
G_ZONE_INDEX = DB_BASE + 0xC0000 + 20444   # index of the zone being loaded (DB thread, 0xDD820)
ZONE_RECORDS = DB_BASE + 8                 # {char name[64]; int allocFlags} per zone, index 1..32
ZONE_RECORD_SIZE = 68
# allocFlags the engine passes to DB_LoadXAssets (0x1635D0 startup list, 0x19E58C map,
# 0x905E8 <map>_load); DB_AddXAsset resolves duplicates by comparing them (0xDB2A0).
ZONE_FLAGS = {'code_post_gfx_mp': 0, 'patch_mp': 8, 'ui_mp': 2, 'common_mp': 1}
ZONE_FLAGS_MAP = 2
ZONE_FLAGS_MAP_LOAD = 4
F_MEMCPY = 0x114c0
F_MEMSET = 0x60a390
# string list (SL_*): the engine keeps interned strings in 12-byte entries hanging off
# *(SL_POOL_VAR); a handle h names entry h, its characters start at +4.  The real
# allocator/hash table is replaced by a native interner that writes the same entry format,
# so SL_ConvertToString and the ref-count helpers run unmodified.
SL_POOL_VAR = 0x10B50000
SL_POOL = SCRATCH + 0x400000
F_SL_GETSTRINGOFSIZE = 0x17fd28   # SL_GetStringOfSize(str, user, size, type)
SL_NATIVE_TRAPS = (0x17f5f8, 0x17f6c0, 0x17f908, 0x17fa70, 0x1805e0, 0x17f214)


class ComError(RuntimeError):
    pass


class MemFault(RuntimeError):
    pass


class FaultingMem(Mem):
    """Memory that faults below the first ELF segment: a NULL-relative access on the PS3."""
    LOW = 0x10000
    def _pg(self, a):
        if a < self.LOW:
            raise MemFault('access to unmapped address %#x' % a)
        return Mem._pg(self, a)


class Machine:
    def __init__(self, real_link=True, fault_low=True, verbose=False):
        self.m = FaultingMem() if fault_low else Mem()
        for off, size, base in ps3elf.SEGS:
            self.m.write(base, ps3elf.DATA[off:off + size])
        self.stubs = {}
        self.cpu = Cpu(self.m, self.stubs)
        self.cpu.r[1] = STACK_TOP
        self.cpu.r[2] = 0x6C6988
        self.current = None
        self.links = []; self.externals = []
        self.com_log = []
        self.verbose = verbose
        self.next_base = 0x40000000
        self.zones = []
        self.real_link = real_link
        self.material_registration_calls = 0
        self._slnext = 0
        self._install_stubs()
        self.type_names = [self.cstr(ps3elf.u32(TYPE_NAME_TABLE + 4 * t)) for t in range(NUM_TYPES)]
        self.pool_sizes = [ps3elf.u32(POOL_SIZES + 4 * t) for t in range(NUM_TYPES)]
        self.default_names = [self.cstr(ps3elf.u32(DEFAULT_NAME_TABLE + 4 * t)) for t in range(NUM_TYPES)]

    # ------------------------------------------------------------------ util
    def cstr(self, addr, limit=256):
        if not addr or addr < 0x10000: return '<bad string ptr %#x>' % addr
        raw = self.m.read(addr, limit)
        return raw.split(b'\0')[0].decode('latin1', 'replace')

    def format(self, fmt, args):
        """Minimal printf: %s %d %i %u %x %c %% with optional width/precision."""
        out = []; i = 0; ai = 0
        while i < len(fmt):
            c = fmt[i]
            if c != '%':
                out.append(c); i += 1; continue
            j = i + 1
            while j < len(fmt) and fmt[j] in '-+ #0123456789.l': j += 1
            if j >= len(fmt): break
            conv = fmt[j]
            if conv == '%': out.append('%')
            else:
                v = args[ai] if ai < len(args) else 0; ai += 1
                if conv == 's': out.append(self.cstr(v & MASK32))
                elif conv in 'di': out.append(str(v - (1 << 32) if (v & MASK32) & 0x80000000 else v & MASK32))
                elif conv == 'u': out.append(str(v & MASK32))
                elif conv in 'xX': out.append('%x' % (v & MASK32))
                elif conv == 'c': out.append(chr(v & 0xFF))
                elif conv in 'fg': out.append('<float>')
                else: out.append('%' + conv)
            i = j + 1
        return ''.join(out)

    def call(self, addr, *args):
        return self.cpu.run(addr, list(args))

    def nested_call(self, addr, *args):
        """Run a function from inside a hook without disturbing the interrupted execution:
        the whole register file, condition/link/count registers and the call history are
        saved around the call and the callee gets its own stack frame below the red zone."""
        c = self.cpu
        saved = (list(c.r), c.cr, c.lr, c.ctr, c.xer, c.pc, list(c.calls), list(c.f))
        c.r[1] = (c.r[1] - 0x1000) & ~0xF
        try:
            return c.run(addr, list(args))
        finally:
            c.r, c.cr, c.lr, c.ctr, c.xer, c.pc, c.calls = list(saved[0]), saved[1], saved[2], saved[3], saved[4], saved[5], saved[6]
            c.f = saved[7]

    def bind_zone(self, zl):
        self.current = zl

    # ----------------------------------------------------------------- stubs
    def _install_stubs(self):
        st = self.stubs
        def db_load(cpu):
            zl = self.current
            ptr = cpu.r[3] & MASK64; n = cpu.r[4] & MASK32
            if n > (1 << 31): n = 0
            data = zl.z[zl.pos:zl.pos + n]
            if len(data) < n:
                raise EOFError('stream exhausted: want %d at %#x (have %d)' % (n, zl.pos, len(data)))
            self.m.write(ptr, data); zl.pos += n; zl.filereads += n
            cpu.r[3] = 0
        def memset(cpu):
            ptr = cpu.r[3] & MASK64; v = cpu.r[4] & 0xFF; n = cpu.r[5] & MASK32
            if n and n < (1 << 28): self.m.write(ptr, bytes([v]) * n)
            cpu.r[3] = ptr
        def memcpy(cpu):
            dst = cpu.r[3] & MASK64; src = cpu.r[4] & MASK64; n = cpu.r[5] & MASK32
            if n and n < (1 << 28): self.m.write(dst, self.m.read(src, n))
            cpu.r[3] = dst
        def nop(cpu): cpu.r[3] = 0
        def strlen(cpu):
            p = cpu.r[3] & MASK64; n = 0
            while self.m.u8(p + n): n += 1
            cpu.r[3] = n
        st[0x10ab0] = strlen           # glink -> libc strlen (uses cntlzd, not emulated)
        def sl_getstringofsize(cpu):
            sp = cpu.r[3] & MASK32; user = cpu.r[4] & 0xFF; size = cpu.r[5] & MASK32
            raw = self.m.read(sp, size) if size else b'\0'
            h = self._sl.get(raw)
            if h is None:
                h = self._slnext
                self._slnext += (size + 4 + 11) // 12
                self.m.write(SL_POOL + 12 * h, bytes([size & 0xFF, user, 0, 1]) + raw)
                self._sl[raw] = h
            else:
                rc = self.m.u16(SL_POOL + 12 * h + 2)
                self.m.w16(SL_POOL + 12 * h + 2, (rc + 1) & 0xFFFF)
            cpu.r[3] = h
        self._sl = {}
        self._slnext = 1
        self.m.w32(SL_POOL_VAR, SL_POOL)
        def sl_trap(cpu):
            raise RuntimeError('native string-list function %#x entered (called from %#x)' % (cpu.pc, cpu.lr - 4))
        def alloc_pos(cpu):
            mask = cpu.r[3] & MASK32
            cur = (self.m.u32(S_STREAM + 4) + mask) & (~mask & MASK32)
            self.m.w32(S_STREAM + 4, cur); self.current._mark(cur); cpu.r[3] = cur
        def inc_pos(cpu):
            n = cpu.r[3] & MASK32
            cur = (self.m.u32(S_STREAM + 4) + n) & MASK32
            self.m.w32(S_STREAM + 4, cur); self.current._mark(cur); cpu.r[3] = 0
        def com_error(cpu):
            code = cpu.r[3] & MASK32
            msg = self.format(self.cstr(cpu.r[4] & MASK32), [cpu.r[k] for k in range(5, 11)])
            self.com_log.append(('ERROR', code, msg))
            raise ComError(msg)
        def com_printf(cpu):
            chan = cpu.r[3] & MASK32
            msg = self.format(self.cstr(cpu.r[4] & MASK32), [cpu.r[k] for k in range(5, 11)])
            self.com_log.append(('PRINT', chan, msg))
            if self.verbose: print('  [Com_Printf %d] %s' % (chan, msg.rstrip()))
            cpu.r[3] = 0
        st[0xb3be8] = db_load
        st[F_MEMSET] = memset
        st[F_MEMCPY] = memcpy
        # 0x243908 is reflected CRC-32 (0xEDB88320), initial/final complement.
        # Verified against the executable for empty/binary data and nonzero seeds.
        # Keep material registration native; accelerate only its pure checksum.
        def crc32(cpu):
            data=self.m.read(cpu.r[3] & MASK32, cpu.r[4] & MASK32)
            cpu.r[3]=zlib.crc32(data,cpu.r[5] & MASK32) & MASK32
        if hashlib.sha256(ps3elf.read(0x243908,0xDC)).hexdigest() == '956d5a883dbaaf4ae0223ddbed1070caf4af80b22aeda0c5995e3a3312fa3d3c':
            st[0x243908] = crc32
        st[F_SL_GETSTRINGOFSIZE] = sl_getstringofsize
        for a in SL_NATIVE_TRAPS: self.cpu.hooks[a] = sl_trap
        st[0xe03d8] = alloc_pos
        st[0xe03f8] = inc_pos
        st[F_COM_ERROR] = com_error
        for a in F_COM_PRINTF: st[a] = com_printf
        st[F_SYS_SLEEP] = nop
        # Single-threaded model: we are the main thread, nothing else is loading, critical
        # sections are free, and time advances one millisecond per query.
        def ret1(cpu): cpu.r[3] = 1
        def millis(cpu):
            self._ms += 1; cpu.r[3] = self._ms
        self._ms = 1000
        st[0x171228] = ret1            # Sys_IsMainThread
        st[0x1711d8] = nop             # Sys_IsRenderThread / other-thread test
        for a in (0x170ed0, 0x170e90, 0x170d30, 0x170d18, 0x170d08, 0x251318, 0x170a00,
                  0xddba8, 0xddc20, 0xddbf8, 0x1714e8):
            st[a] = nop                # critical sections / thread yields / render syncs
        st[0x257938] = millis          # Sys_Milliseconds
        # record every DB_FindXAssetHeader the engine performs while loading
        self.lookups = []
        def on_find(cpu):
            self.lookups.append((cpu.lr - 4, cpu.r[4] & MASK32, self.cstr(cpu.r[5] & MASK32)))
        self.cpu.hooks[F_DB_FIND] = on_find
        # DB_CreateDefaultEntry(type, name): an external ",name" that no loaded zone
        # provides - the engine clones the type's default asset under that name.
        self.default_clones = []
        def on_default(cpu):
            self.default_clones.append((cpu.r[3] & MASK32, self.cstr(cpu.r[4] & MASK32),
                                        self.current.name if self.current else None))
        self.cpu.hooks[0xdde20] = on_default
        # DB_AddXAsset: a real asset arriving for a name that currently holds a default
        # clone replaces the clone in place (0xDE43C) - the forward-reference case.
        self.default_replaced = []
        def on_replace(cpu):
            self.default_replaced.append((cpu.r[28] & MASK32, self.cstr(cpu.r[27] & MASK32)))
        self.cpu.hooks[0xde43c] = on_replace
        # Load_SndAliasCustom re-resolves every loaded sound alias list through the DB by
        # name (0x1D3A1C..0x1D3A3C).  That is sound-system bookkeeping the map zones never
        # exercise (they carry no sounds); skip the lookup and keep the loaded pointer.
        self.cpu.patches[0x1d3a1c] = 0x1d3a40
        nops = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime_nop.txt')
        for a in (int(x, 0) for x in open(nops).read().split()):
            st[a] = nop
        # Execute native material registration, after checking that its named
        # constant searches cannot run beyond the supplied table. GPU execution
        # and the other renderer initialization functions remain outside scope.
        from materialcheck import validate_material_constants
        def check_material(cpu):
            validate_material_constants(self.m, cpu.r[3] & MASK32, self.cstr)
            self.material_registration_calls += 1
        st.pop(0x335578, None)
        self.cpu.hooks[0x335578] = check_material
        if not self.real_link:
            def link_entry(cpu):
                out = cpu.r[3] & MASK64; hdr = cpu.r[5] & MASK32
                self.m.w32(out, hdr); cpu.r[3] = 0
            st[F_DB_LINK] = link_entry
            def find_header(cpu):
                cpu.r[3] = 0
            st[F_DB_FIND] = find_header

    # --------------------------------------------------------------- DB init
    def db_init(self):
        """The first-time initialisation DB_LoadXAssets performs (EBOOT 0xDEE48..0xDEEF8):
        every per-type asset pool is threaded into its free list by the type's own init
        function, and the XAssetEntry pool is threaded and hung off *DB_BASE."""
        for t in range(NUM_TYPES):
            head = ps3elf.u32(POOL_HEADS + 4 * t)
            if not head: continue
            desc = ps3elf.u32(POOL_INITS + 4 * t)
            fn = ps3elf.u32(desc)
            self.call(fn, head, self.pool_sizes[t])
        base = ENTRY_POOL_FREE
        self.m.w32(DB_BASE, base)
        for i in range(ENTRY_POOL_COUNT):
            self.m.w32(base + 16 * i, base + 16 * (i + 1))
        self.m.w32(DB_BASE + 0xC4628, 0)
        self.inited = True

    # ------------------------------------------------------------- snapshot
    def save(self, path):
        import pickle, zlib
        state = {'pages': self.m.pages, 'regs': list(self.cpu.r), 'cr': self.cpu.cr, 'fregs': list(self.cpu.f),
                 'next_base': self.next_base, 'slnext': self._slnext, 'sl': self._sl, 'ms': self._ms,
                 'zones': [(z.name, len(z.assets), z.block_base, z.sizes) for z in self.zones],
                 'com_log': self.com_log, 'lookups': self.lookups,
                 'default_clones': self.default_clones, 'default_replaced': self.default_replaced}
        with open(path, 'wb') as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)

    def restore(self, path):
        import pickle
        with open(path, 'rb') as f:
            state = pickle.load(f)
        self.m.pages = state['pages']
        self.cpu.r = state['regs']; self.cpu.cr = state['cr']
        self.cpu.f = state.get('fregs', [0.0] * 32)
        self.next_base = state['next_base']; self._slnext = state['slnext']; self._ms = state['ms']
        self._sl = state.get('sl', {})
        self.com_log = state['com_log']; self.lookups = state['lookups']
        self.default_clones = state.get('default_clones', []); self.default_replaced = state.get('default_replaced', [])
        self.zone_summaries = state['zones']
        self.inited = True

    # ------------------------------------------------------------- zone load
    def alloc_blocks(self, sizes):
        bases = []
        for s in sizes:
            bases.append(self.next_base)
            self.next_base += ((s + 0xFFFFF) & ~0xFFFFF) + 0x100000
        return bases

    def register_zone(self, name, flags):
        """What the DB thread does before DB_LoadXFile (0xDD810..0xDD8A0): take the first
        free zone record, store the name and allocFlags, make it the current zone."""
        for idx in range(1, 33):
            rec = ZONE_RECORDS + ZONE_RECORD_SIZE * idx
            if self.m.u8(rec) == 0:
                break
        else:
            raise RuntimeError('no free zone record')
        self.m.write(rec, name.encode('latin1')[:63].ljust(64, b'\0'))
        self.m.w32(rec + 64, flags & MASK32)
        self.m.w32(G_ZONE_INDEX, idx)
        return idx

    def load_zone(self, zone_bytes, name, flags=None):
        sizes = struct.unpack('>9I', zone_bytes[:36])[2:9]
        bases = self.alloc_blocks(sizes)
        zl = ZoneLoader(zone_bytes, machine=self, block_base=bases)
        zl.name = name
        if flags is None:
            flags = ZONE_FLAGS.get(name, ZONE_FLAGS_MAP_LOAD if name.endswith('_load') else ZONE_FLAGS_MAP)
        zl.zone_index = self.register_zone(name, flags)
        zl.flags = flags
        before = self.pool_usage()
        zl.load()
        # DB_LoadXFile is followed by the redundant-asset pass: every asset that arrived
        # under a name already in the database (a default clone made for a forward
        # ",name" reference, or a genuine duplicate) is re-added with override=1 there.
        n = self.m.u32(COPYINFO_COUNT)
        zl.redundant = []
        for i in range(min(n, 2048)):
            e = self.m.u32(COPYINFO_COUNT + 4 + 4 * i)
            t = self.m.u32(e); h = self.m.u32(e + 4)
            zl.redundant.append((t, self.entry_name(t, h) if t < NUM_TYPES else '?'))
        self.call(F_DB_COPYINFO)
        zl.pool_delta = {t: self.pool_usage()[t] - before[t] for t in range(NUM_TYPES)}
        self.zones.append(zl)
        return zl

    # -------------------------------------------------------------- queries
    def pool_usage(self):
        """Entries consumed per type: walk the per-type free lists against capacity."""
        out = {}
        for t in range(NUM_TYPES):
            head = ps3elf.u32(POOL_HEADS + 4 * t)
            if not head or not self.pool_sizes[t]:
                out[t] = 0; continue
            free = 0; p = self.m.u32(head); guard = self.pool_sizes[t] + 2
            while p and guard:
                free += 1; p = self.m.u32(p); guard -= 1
            out[t] = self.pool_sizes[t] - free
        return out

    def find(self, typ, name):
        """DB_FindXAssetHeader as the game runs it.  Returns (header, error_text)."""
        nm = SCRATCH + 0x8000
        self.m.write(nm, name.encode('latin1') + b'\0')
        out = SCRATCH + 0x8400
        self.m.w32(out, 0)
        n0 = len(self.com_log)
        try:
            self.call(F_DB_FIND, out, typ, nm)
        except ComError as e:
            return None, str(e)
        return self.m.u32(out), None

    def entry_name(self, typ, header):
        """DB_GetXAssetName over a synthetic XAssetEntry."""
        e = SCRATCH + 0x8800
        self.m.w32(e, typ); self.m.w32(e + 4, header)
        return self.cstr(self.call(F_GET_NAME, e) & MASK32)

    HASH_SLOTS = 30000
    HASH_TABLE = DB_BASE + 0x408D8

    def asset_table(self):
        """Every (type, header, entry) reachable from the hash table - the live DB view."""
        out = []
        for h in range(self.HASH_SLOTS):
            idx = self.m.u16(self.HASH_TABLE + 2 * h)
            guard = 4096
            while idx and guard:
                e = ENTRY_POOL + 16 * idx
                out.append((self.m.u32(e), self.m.u32(e + 4), e))
                idx = self.m.u16(e + 0x0A); guard -= 1
        return out

    def inventory(self):
        """{(type, name): header} for everything linked so far."""
        inv = {}
        for typ, hdr, e in self.asset_table():
            inv[(typ, self.entry_name(typ, hdr))] = hdr
        return inv

#!/usr/bin/env python3
"""Minimal big-endian PowerPC64 interpreter for the CoD4 PS3 fastfile loader.

The loader's integer subset and the floating loads/comparisons used by material
registration are implemented; this is not a complete PowerPC CPU. Memory is a
sparse page table; ELF segments are mapped read/write, BSS is zero-filled on
demand, and the seven zone blocks are mapped at synthetic bases.
"""
import os
import math
import struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps3elf

PAGE = 0x10000
MASK64 = (1 << 64) - 1
MASK32 = 0xFFFFFFFF


class Mem:
    def __init__(self):
        self.pages = {}
    def _pg(self, a):
        p = a >> 16
        pg = self.pages.get(p)
        if pg is None:
            pg = bytearray(PAGE); self.pages[p] = pg
        return pg
    def write(self, a, data):
        a &= MASK64
        while data:
            pg = self._pg(a); off = a & 0xFFFF
            n = min(len(data), PAGE - off)
            pg[off:off + n] = data[:n]
            data = data[n:]; a += n
    def read(self, a, n):
        a &= MASK64
        out = bytearray()
        while n:
            pg = self._pg(a); off = a & 0xFFFF
            k = min(n, PAGE - off)
            out += pg[off:off + k]
            n -= k; a += k
        return bytes(out)
    def u8(self, a): return self.read(a, 1)[0]
    def u16(self, a): return struct.unpack('>H', self.read(a, 2))[0]
    def u32(self, a): return struct.unpack('>I', self.read(a, 4))[0]
    def u64(self, a): return struct.unpack('>Q', self.read(a, 8))[0]
    def w8(self, a, v): self.write(a, bytes([v & 0xFF]))
    def w16(self, a, v): self.write(a, struct.pack('>H', v & 0xFFFF))
    def w32(self, a, v): self.write(a, struct.pack('>I', v & MASK32))
    def w64(self, a, v): self.write(a, struct.pack('>Q', v & MASK64))


class UnknownCall(RuntimeError):
    pass


def s64(v): return v - (1 << 64) if v & (1 << 63) else v
def s32(v):
    v &= MASK32
    return v - (1 << 32) if v & 0x80000000 else v


class Cpu:
    def __init__(self, mem, stubs, trace=False):
        self.m = mem; self.r = [0] * 32; self.cr = 0; self.lr = 0; self.ctr = 0
        self.xer = 0; self.stubs = stubs; self.trace = trace; self.steps = 0
        self.f = [0.0] * 32
        self.calls = []
        self.hooks = {}
        self.patches = {}
        self.reserve = None
        self.allowed = [(0x10240, 0x656478)]
    # ---- condition register helpers -------------------------------------
    def set_cr(self, field, a, b, signed=True, w=True):
        if w:
            a = s32(a) if signed else (a & MASK32)
            b = s32(b) if signed else (b & MASK32)
        else:
            a = s64(a) if signed else (a & MASK64)
            b = s64(b) if signed else (b & MASK64)
        c = 8 if a < b else (4 if a > b else 2)
        sh = (7 - field) * 4
        self.cr = (self.cr & ~(0xF << sh)) | (c << sh)
    def crbit(self, n): return (self.cr >> (31 - n)) & 1
    def run(self, entry, args, maxsteps=400_000_000):
        for i, v in enumerate(args): self.r[3 + i] = v & MASK64
        RET = 0x00DEAD00
        self.lr = RET
        self.pc = entry
        try:
            return self._run(entry, RET, self.steps + maxsteps)
        except Exception as exc:
            if not getattr(exc, '_pc_annotated', False):
                exc._pc_annotated = True
                exc.pc = self.pc
                exc.callsites = [(hex(a), hex(b)) for a, b in self.calls[-6:]]
                exc.args = (('%s [pc=%#x, calls=%s]' % (exc.args[0] if exc.args else '', self.pc, exc.callsites)),) + tuple(exc.args[1:])
            raise

    def _run(self, entry, RET, maxsteps):
        pc = entry
        m = self.m
        while True:
            self.pc = pc
            if pc == RET: return self.r[3]
            st = self.stubs.get(pc)
            if st is not None:
                # ELFv1 ABI: a glink stub saves the caller's TOC at 40(r1) before the
                # cross-module call and the caller reloads it afterwards.  Natively stubbed
                # functions must leave that slot valid.
                m.w64(self.r[1] + 40, self.r[2])
                st(self)
                pc = self.lr; continue
            if not any(lo <= pc < hi for lo, hi in self.allowed):
                raise UnknownCall('unstubbed call to %#x from %#x' % (pc, self.calls[-1][0] if self.calls else 0))
            self.steps += 1
            if self.steps > maxsteps: raise RuntimeError('step limit at %#x' % pc)
            if self.hooks and pc in self.hooks: self.hooks[pc](self)
            if self.patches and pc in self.patches:
                pc = self.patches[pc]; continue
            w = struct.unpack('>I', ps3elf.read(pc, 4))[0] if pc < 0x10000000 else m.u32(pc)
            op = w >> 26
            npc = pc + 4
            if op == 14:    # addi
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                self.r[d] = ((self.r[a] if a else 0) + si) & MASK64
            elif op == 15:  # addis
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = (w & 0xFFFF) << 16
                si = si - 0x100000000 if si & 0x80000000 else si
                self.r[d] = ((self.r[a] if a else 0) + si) & MASK64
            elif op == 24:  # ori
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] | (w & 0xFFFF)
            elif op == 25:  # oris
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] | ((w & 0xFFFF) << 16)
            elif op == 32:  # lwz
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                self.r[d] = m.u32(((self.r[a] if a else 0) + si) & MASK64)
            elif op == 34:  # lbz
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                self.r[d] = m.u8(((self.r[a] if a else 0) + si) & MASK64)
            elif op == 40:  # lhz
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                self.r[d] = m.u16(((self.r[a] if a else 0) + si) & MASK64)
            elif op == 42:  # lha
                d = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                v = m.u16(((self.r[a] if a else 0) + si) & MASK64)
                self.r[d] = (v - 0x10000 if v & 0x8000 else v) & MASK64
            elif op == 36:  # stw
                s = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                m.w32(((self.r[a] if a else 0) + si) & MASK64, self.r[s])
            elif op == 38:  # stb
                s = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                m.w8(((self.r[a] if a else 0) + si) & MASK64, self.r[s])
            elif op == 44:  # sth
                s = (w >> 21) & 31; a = (w >> 16) & 31; si = w & 0xFFFF
                si = si - 0x10000 if si & 0x8000 else si
                m.w16(((self.r[a] if a else 0) + si) & MASK64, self.r[s])
            elif op in (48, 50):  # lfs / lfd (single operands widen to double)
                d = (w >> 21) & 31; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                ea = ((self.r[a] if a else 0) + si) & MASK64
                self.f[d] = struct.unpack('>f' if op == 48 else '>d', m.read(ea, 4 if op == 48 else 8))[0]
            elif op in (52, 54):  # stfs / stfd
                d = (w >> 21) & 31; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                ea = ((self.r[a] if a else 0) + si) & MASK64
                m.write(ea, struct.pack('>f' if op == 52 else '>d', self.f[d]))
            elif op == 63 and ((w >> 1) & 0x3FF) in (12, 72, 846):
                d = (w >> 21) & 31; b = (w >> 11) & 31; xo = (w >> 1) & 0x3FF
                if xo == 12: self.f[d] = struct.unpack('>f', struct.pack('>f', self.f[b]))[0]  # frsp
                elif xo == 72: self.f[d] = self.f[b]  # fmr
                else: self.f[d] = float(struct.unpack('>q', struct.pack('>d', self.f[b]))[0])  # fcfid
            elif op in (59, 63) and ((w >> 1) & 31) in (18, 20, 21, 25):
                d = (w >> 21) & 31; a = (w >> 16) & 31; b = (w >> 11) & 31; c = (w >> 6) & 31
                xo = (w >> 1) & 31
                if xo == 18: v = self.f[a] / self.f[b]
                elif xo == 20: v = self.f[a] - self.f[b]
                elif xo == 21: v = self.f[a] + self.f[b]
                else: v = self.f[a] * self.f[c]
                self.f[d] = struct.unpack('>f', struct.pack('>f', v))[0] if op == 59 else v
            elif op == 63 and ((w >> 1) & 0x3FF) == 0:  # fcmpu
                bf = (w >> 23) & 7; a = (w >> 16) & 31; b = (w >> 11) & 31
                x, y = self.f[a], self.f[b]
                c = 1 if math.isnan(x) or math.isnan(y) else (8 if x < y else 4 if x > y else 2)
                sh = (7 - bf) * 4
                self.cr = (self.cr & ~(15 << sh)) | (c << sh)
                # FPSCR exceptions are outside the supported subset. Registration
                # uses finite material values and reads this CR field only.
            elif op == 58:  # ld / ldu / lwa
                d = (w >> 21) & 31; a = (w >> 16) & 31
                ds = w & 0xFFFC
                ds = ds - 0x10000 if ds & 0x8000 else ds
                ea = ((self.r[a] if a else 0) + ds) & MASK64
                x = w & 3
                if x == 0: self.r[d] = m.u64(ea)
                elif x == 1: self.r[d] = m.u64(ea); self.r[a] = ea
                else:
                    v = m.u32(ea); self.r[d] = (v - (1 << 32) if v & 0x80000000 else v) & MASK64
            elif op == 62:  # std / stdu
                s = (w >> 21) & 31; a = (w >> 16) & 31
                ds = w & 0xFFFC
                ds = ds - 0x10000 if ds & 0x8000 else ds
                ea = ((self.r[a] if a else 0) + ds) & MASK64
                m.w64(ea, self.r[s])
                if w & 1: self.r[a] = ea
            elif op == 11:  # cmpi
                bf = (w >> 23) & 7; l = (w >> 21) & 1; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                self.set_cr(bf, self.r[a], si & MASK64, True, not l)
            elif op == 10:  # cmpli
                bf = (w >> 23) & 7; l = (w >> 21) & 1; a = (w >> 16) & 31
                self.set_cr(bf, self.r[a], w & 0xFFFF, False, not l)
            elif op == 7:   # mulli
                d = (w >> 21) & 31; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                self.r[d] = (s64(self.r[a]) * si) & MASK64
            elif op == 21:  # rlwinm
                s = (w >> 21) & 31; a = (w >> 16) & 31
                sh = (w >> 11) & 31; mb = (w >> 6) & 31; me = (w >> 1) & 31
                v = self.r[s] & MASK32
                v = ((v << sh) | (v >> (32 - sh))) & MASK32 if sh else v
                mask = 0
                i = mb
                while True:
                    mask |= 1 << (31 - i)
                    if i == me: break
                    i = (i + 1) & 31
                self.r[a] = v & mask
                if w & 1: self.set_cr(0, self.r[a], 0, True, True)
            elif op == 30:  # rldicl / rldicr / rldic
                s = (w >> 21) & 31; a = (w >> 16) & 31
                sh = ((w >> 11) & 31) | (((w >> 1) & 1) << 5)
                mb6 = ((w >> 6) & 31) | (((w >> 5) & 1) << 5)
                kind = (w >> 2) & 7
                v = ((self.r[s] << sh) | (self.r[s] >> (64 - sh))) & MASK64 if sh else self.r[s]
                if kind == 0:   # rldicl: mask mb..63
                    self.r[a] = v & ((1 << (64 - mb6)) - 1)
                elif kind == 1:  # rldicr: mask 0..me
                    me = mb6
                    self.r[a] = v & (((1 << (me + 1)) - 1) << (63 - me))
                else:
                    raise RuntimeError('rld kind %d at %#x' % (kind, pc))
            elif op == 16:  # bc
                bo = (w >> 21) & 31; bi = (w >> 16) & 31
                bd = w & 0xFFFC
                bd = bd - 0x10000 if bd & 0x8000 else bd
                tgt = (bd if w & 2 else pc + bd) & MASK64
                if self._cond(bo, bi):
                    if w & 1: self.lr = pc + 4
                    npc = tgt
            elif op == 18:  # b / bl
                li = w & 0x03FFFFFC
                if li & 0x02000000: li -= 0x04000000
                tgt = li if (w & 2) else pc + li
                if w & 1:
                    self.lr = pc + 4
                    self.calls.append((pc, tgt))
                npc = tgt
            elif op == 19:
                xo = (w >> 1) & 0x3FF
                bo = (w >> 21) & 31; bi = (w >> 16) & 31
                if xo == 16:   # bclr
                    if self._cond(bo, bi):
                        t = self.lr
                        if w & 1: self.lr = pc + 4
                        npc = t
                elif xo == 528:  # bcctr
                    if self._cond(bo, bi):
                        t = self.ctr
                        if w & 1: self.lr = pc + 4
                        npc = t
                elif xo == 150:  # isync
                    pass
                elif xo in (33, 129, 193, 225, 257, 289, 417, 449):  # cr logical
                    bt = (w >> 21) & 31; ba = (w >> 16) & 31; bb = (w >> 11) & 31
                    A = self.crbit(ba); B = self.crbit(bb)
                    if xo == 257: v = A & B
                    elif xo == 449: v = A | B
                    elif xo == 193: v = A ^ B
                    elif xo == 225: v = 1 - (A | B)
                    elif xo == 33: v = 1 - (A ^ B)
                    elif xo == 129: v = A & (1 - B)
                    elif xo == 289: v = 1 - (A ^ B)
                    else: v = A | (1 - B)
                    sh = 31 - bt
                    self.cr = (self.cr & ~(1 << sh)) | (v << sh)
                else:
                    raise RuntimeError('op19 xo=%d at %#x' % (xo, pc))
            elif op == 31:
                xo = (w >> 1) & 0x3FF
                d = (w >> 21) & 31; a = (w >> 16) & 31; b = (w >> 11) & 31
                if xo == 444:    # or / mr
                    self.r[a] = self.r[d] | self.r[b]
                    if w & 1: self.set_cr(0, self.r[a], 0, True, True)
                elif xo == 266 or xo == 10:  # add / addc
                    self.r[d] = (self.r[a] + self.r[b]) & MASK64
                elif xo == 40:   # subf
                    self.r[d] = (self.r[b] - self.r[a]) & MASK64
                elif xo == 986:  # extsw
                    v = self.r[d] & MASK32
                    self.r[a] = (v - (1 << 32) if v & 0x80000000 else v) & MASK64
                elif xo == 922:  # extsh
                    v = self.r[d] & 0xFFFF
                    self.r[a] = (v - 0x10000 if v & 0x8000 else v) & MASK64
                elif xo == 954:  # extsb
                    v = self.r[d] & 0xFF
                    self.r[a] = (v - 0x100 if v & 0x80 else v) & MASK64
                elif xo == 0:    # cmp
                    bf = (w >> 23) & 7; l = (w >> 21) & 1
                    self.set_cr(bf, self.r[a], self.r[b], True, not l)
                elif xo == 32:   # cmpl
                    bf = (w >> 23) & 7; l = (w >> 21) & 1
                    self.set_cr(bf, self.r[a], self.r[b], False, not l)
                elif xo == 19:   # mfcr / mfocrf
                    self.r[d] = self.cr & MASK32
                elif xo == 20:   # lwarx  (single-threaded: plain load + reservation)
                    ea = ((self.r[a] if a else 0) + self.r[b]) & MASK64
                    self.r[d] = m.u32(ea); self.reserve = ea
                elif xo == 84:   # ldarx
                    ea = ((self.r[a] if a else 0) + self.r[b]) & MASK64
                    self.r[d] = m.u64(ea); self.reserve = ea
                elif xo == 150:  # stwcx.  (always succeeds: CR0.EQ = 1)
                    ea = ((self.r[a] if a else 0) + self.r[b]) & MASK64
                    m.w32(ea, self.r[d]); self.cr = (self.cr & ~0xF0000000) | 0x20000000
                elif xo == 214:  # stdcx.
                    ea = ((self.r[a] if a else 0) + self.r[b]) & MASK64
                    m.w64(ea, self.r[d]); self.cr = (self.cr & ~0xF0000000) | 0x20000000
                elif xo in (598, 854, 86, 278, 246, 1014, 54, 470, 982, 758):  # sync/eieio/dcbf/dcbt/dcbtst/dcbz/dcbst/dcbi/icbi/dcba
                    if xo == 1014:  # dcbz: zero the 128-byte cache block
                        ea = ((self.r[a] if a else 0) + self.r[b]) & MASK64
                        m.write(ea & ~127, b'\0' * 128)
                elif xo == 371:  # mftb
                    self.r[d] = self.steps & MASK64
                elif xo == 144:  # mtcrf
                    fxm = (w >> 12) & 0xFF
                    m2 = 0
                    for i in range(8):
                        if fxm & (1 << (7 - i)): m2 |= 0xF << ((7 - i) * 4)
                    self.cr = (self.cr & ~m2) | (self.r[d] & m2)
                elif xo == 339:  # mfspr
                    spr = ((w >> 16) & 0x1F) | (((w >> 11) & 0x1F) << 5)
                    self.r[d] = self.lr if spr == 8 else (self.ctr if spr == 9 else self.xer)
                elif xo == 467:  # mtspr
                    spr = ((w >> 16) & 0x1F) | (((w >> 11) & 0x1F) << 5)
                    if spr == 8: self.lr = self.r[d]
                    elif spr == 9: self.ctr = self.r[d]
                    else: self.xer = self.r[d]
                elif xo == 235 or xo == 235:  # mullw
                    self.r[d] = (s32(self.r[a]) * s32(self.r[b])) & MASK64
                elif xo == 233:  # mulld
                    self.r[d] = (s64(self.r[a]) * s64(self.r[b])) & MASK64
                elif xo == 824:  # srawi
                    sh = (w >> 11) & 31
                    v = s32(self.r[d])
                    self.r[a] = (v >> sh) & MASK64
                    self.xer = self.xer  # CA ignored
                elif xo == 202:  # addze
                    ca = (self.xer >> 29) & 1
                    self.r[d] = (self.r[a] + ca) & MASK64
                elif xo == 60:   # andc
                    self.r[a] = self.r[d] & (~self.r[b] & MASK64)
                elif xo == 28:   # and
                    self.r[a] = self.r[d] & self.r[b]
                    if w & 1: self.set_cr(0, self.r[a], 0, True, True)
                elif xo == 444 + 0: pass
                elif xo == 151:  # stwx
                    m.w32(((self.r[a] if a else 0) + self.r[b]) & MASK64, self.r[d])
                elif xo == 23:   # lwzx
                    self.r[d] = m.u32(((self.r[a] if a else 0) + self.r[b]) & MASK64)
                elif xo == 87:   # lbzx
                    self.r[d] = m.u8(((self.r[a] if a else 0) + self.r[b]) & MASK64)
                elif xo == 279:  # lhzx
                    self.r[d] = m.u16(((self.r[a] if a else 0) + self.r[b]) & MASK64)
                elif xo == 215:  # stbx
                    m.w8(((self.r[a] if a else 0) + self.r[b]) & MASK64, self.r[d])
                elif xo == 407:  # sthx
                    m.w16(((self.r[a] if a else 0) + self.r[b]) & MASK64, self.r[d])
                elif xo == 21:   # ldx
                    self.r[d] = m.u64(((self.r[a] if a else 0) + self.r[b]) & MASK64)
                elif xo == 149:  # stdx
                    m.w64(((self.r[a] if a else 0) + self.r[b]) & MASK64, self.r[d])
                elif xo == 24:   # slw
                    sh = self.r[b] & 63
                    self.r[a] = 0 if sh > 31 else ((self.r[d] << sh) & MASK32)
                elif xo == 536:  # srw
                    sh = self.r[b] & 63
                    self.r[a] = 0 if sh > 31 else ((self.r[d] & MASK32) >> sh)
                elif xo == 316:  # xor
                    self.r[a] = self.r[d] ^ self.r[b]
                elif xo == 459 or xo == 459:  # divwu
                    d1 = self.r[a] & MASK32; d2 = self.r[b] & MASK32
                    self.r[d] = 0 if d2 == 0 else (d1 // d2) & MASK64
                elif xo == 491:  # divw
                    d1 = s32(self.r[a]); d2 = s32(self.r[b])
                    self.r[d] = 0 if d2 == 0 else int(abs(d1) // abs(d2)) * (1 if (d1 < 0) == (d2 < 0) else -1) & MASK64
                elif xo == 11:   # mulhwu
                    self.r[d] = (((self.r[a] & MASK32) * (self.r[b] & MASK32)) >> 32) & MASK64
                elif xo == 75:   # mulhw
                    self.r[d] = ((s32(self.r[a]) * s32(self.r[b])) >> 32) & MASK64
                elif xo == 8:    # subfc
                    res = (self.r[b] & MASK64) - (self.r[a] & MASK64)
                    self.r[d] = res & MASK64
                    self.xer = (self.xer & ~(1 << 29)) | ((1 << 29) if res >= 0 else 0)
                elif xo == 136:  # subfe
                    ca = (self.xer >> 29) & 1
                    res = (~self.r[a] & MASK64) + self.r[b] + ca
                    self.r[d] = res & MASK64
                    self.xer = (self.xer & ~(1 << 29)) | ((1 << 29) if res > MASK64 else 0)
                elif xo == 138:  # adde
                    ca = (self.xer >> 29) & 1
                    res = self.r[a] + self.r[b] + ca
                    self.r[d] = res & MASK64
                    self.xer = (self.xer & ~(1 << 29)) | ((1 << 29) if res > MASK64 else 0)
                elif xo == 27:   # sld
                    sh = self.r[b] & 127
                    self.r[a] = 0 if sh > 63 else (self.r[d] << sh) & MASK64
                elif xo == 539:  # srd
                    sh = self.r[b] & 127
                    self.r[a] = 0 if sh > 63 else (self.r[d] >> sh)
                elif xo == 26:   # cntlzw
                    v = self.r[d] & MASK32
                    n = 0
                    while n < 32 and not (v & (1 << (31 - n))): n += 1
                    self.r[a] = n
                elif xo == 476:  # nand
                    self.r[a] = ~(self.r[d] & self.r[b]) & MASK64
                elif xo == 124:  # nor
                    self.r[a] = ~(self.r[d] | self.r[b]) & MASK64
                elif xo == 284:  # eqv
                    self.r[a] = ~(self.r[d] ^ self.r[b]) & MASK64
                elif xo == 104:  # neg
                    self.r[d] = (-s64(self.r[a])) & MASK64
                else:
                    raise RuntimeError('op31 xo=%d at %#x' % (xo, pc))
            elif op == 12 or op == 13:  # addic / addic.
                d = (w >> 21) & 31; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                res = self.r[a] + si
                self.r[d] = res & MASK64
                self.xer = (self.xer & ~(1 << 29)) | ((1 << 29) if res > MASK64 else 0)
                if op == 13: self.set_cr(0, self.r[d], 0, True, True)
            elif op == 8:   # subfic
                d = (w >> 21) & 31; a = (w >> 16) & 31
                si = w & 0xFFFF; si = si - 0x10000 if si & 0x8000 else si
                res = si - self.r[a]
                self.r[d] = res & MASK64
                self.xer = (self.xer & ~(1 << 29)) | ((1 << 29) if (si & MASK64) >= (self.r[a] & MASK64) else 0)
            elif op == 26:  # xori
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] ^ (w & 0xFFFF)
            elif op == 27:  # xoris
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] ^ ((w & 0xFFFF) << 16)
            elif op == 29:  # andis.
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] & ((w & 0xFFFF) << 16)
                self.set_cr(0, self.r[a], 0, True, True)
            elif op == 20:  # rlwimi
                s = (w >> 21) & 31; a = (w >> 16) & 31
                sh = (w >> 11) & 31; mb = (w >> 6) & 31; me = (w >> 1) & 31
                v = self.r[s] & MASK32
                v = ((v << sh) | (v >> (32 - sh))) & MASK32 if sh else v
                mask = 0; i = mb
                while True:
                    mask |= 1 << (31 - i)
                    if i == me: break
                    i = (i + 1) & 31
                self.r[a] = ((self.r[a] & MASK32) & ~mask) | (v & mask)
            elif op == 23:  # rlwnm
                s = (w >> 21) & 31; a = (w >> 16) & 31; b = (w >> 11) & 31
                sh = self.r[b] & 31; mb = (w >> 6) & 31; me = (w >> 1) & 31
                v = self.r[s] & MASK32
                v = ((v << sh) | (v >> (32 - sh))) & MASK32 if sh else v
                mask = 0; i = mb
                while True:
                    mask |= 1 << (31 - i)
                    if i == me: break
                    i = (i + 1) & 31
                self.r[a] = v & mask
            elif op == 28:  # andi.
                s = (w >> 21) & 31; a = (w >> 16) & 31
                self.r[a] = self.r[s] & (w & 0xFFFF)
                self.set_cr(0, self.r[a], 0, True, True)
            else:
                raise RuntimeError('op %d at %#x (w=%08x)' % (op, pc, w))
            pc = npc
    def _cond(self, bo, bi):
        ctr_ok = True
        if not (bo & 4):
            self.ctr = (self.ctr - 1) & MASK64
            ctr_ok = (self.ctr != 0) if not (bo & 2) else (self.ctr == 0)
        cond_ok = True
        if not (bo & 16):
            cond_ok = self.crbit(bi) == ((bo >> 3) & 1)
        return ctr_ok and cond_ok

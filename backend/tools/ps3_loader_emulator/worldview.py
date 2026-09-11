#!/usr/bin/env python3
"""Read a linked PS3 GfxWorld out of the emulated machine memory (big-endian, pointers
already converted by the game's own loader) into plain numpy arrays.

Layout: the PS3 GfxWorld root as the porter writes it and as Load_GfxWorld (EBOOT 0xD6A50)
consumes it; every offset below was cross-checked against retail mp_shipment in memory.
"""
import struct, re
import numpy as np

# GfxWorld root
O_NAME, O_BASENAME = 0x00, 0x04
O_INDEXCOUNT, O_INDICES = 0x10, 0x14
O_SURFACECOUNT = 0x1C
O_SKYSURFCOUNT, O_SKYSURFS = 0x24, 0x28
O_VERTEXCOUNT, O_VERTICES = 0x34, 0x38
O_LAYERS = 0x48
O_SUNLIGHT = 0xD4
O_MODELCOUNT, O_MODELS = 0x15C, 0x160
O_MINS = 0x164
O_DPVS = 0x250
D_SMODELCOUNT, D_STATICSURFCOUNT = 0x00, 0x04
D_SORTEDSURF, D_SMODELINSTS, D_SURFACES, D_CULLGROUPS, D_SMODELDRAW = 0x44, 0x48, 0x4C, 0x50, 0x54
SURFACE = 0x34
DRAW = 0x2C
VERTEX = 0x10
LAYER = 0x1C
BRUSHMODEL = 0x38


class Mem:
    """Thin big-endian reader over the emulator's sparse memory."""
    def __init__(self, m): self.m = m
    def u8(self, a): return self.m.u8(a)
    def u16(self, a): return self.m.u16(a)
    def u32(self, a): return self.m.u32(a)
    def f32(self, a): return struct.unpack('>f', self.m.read(a, 4))[0]
    def cstr(self, a, n=256):
        if not a or a < 0x10000: return ''
        return self.m.read(a, n).split(b'\0')[0].decode('latin1', 'replace')
    def arr(self, a, n, dtype):
        raw = self.m.read(a, n * np.dtype(dtype).itemsize)
        return np.frombuffer(raw, dtype=dtype)


def read_world(m, hdr):
    M = Mem(m)
    w = {}
    w['name'] = M.cstr(M.u32(hdr + O_NAME)); w['base'] = M.cstr(M.u32(hdr + O_BASENAME))
    ic = M.u32(hdr + O_INDEXCOUNT); vc = M.u32(hdr + O_VERTEXCOUNT); sc = M.u32(hdr + O_SURFACECOUNT)
    w['index_count'], w['vertex_count'], w['surface_count'] = ic, vc, sc
    w['indices'] = M.arr(M.u32(hdr + O_INDICES), ic, '>u2').astype(np.int64)
    vraw = M.arr(M.u32(hdr + O_VERTICES), vc * 4, '>f4').reshape(vc, 4)
    w['pos'] = np.array(vraw[:, :3], dtype=np.float64)
    lp = M.u32(hdr + O_LAYERS)
    w['layers_raw'] = b''
    if lp:
        # the layer stream is at least vc*LAYER bytes; retail's merged materials make it
        # longer (wider per-surface records), so read generously up to the next block use
        lraw = M.m.read(lp, vc * LAYER)
        w['layers_raw'] = M.m.read(lp, vc * 64)
        la = np.frombuffer(lraw, dtype=np.uint8).reshape(vc, LAYER)
        w['color'] = la[:, 0:4].copy()                       # r,g,b,a bytes
        w['texcoord'] = np.frombuffer(lraw, dtype='>f4').reshape(vc, LAYER // 4)[:, 1:3].copy()
        w['lmapcoord'] = np.frombuffer(lraw, dtype='>f4').reshape(vc, LAYER // 4)[:, 3:5].copy()
        w['normal_packed'] = np.frombuffer(lraw, dtype='>u4').reshape(vc, LAYER // 4)[:, 5].copy()
    else:
        w['color'] = np.zeros((vc, 4), np.uint8)
    d = hdr + O_DPVS
    w['smodel_count'] = M.u32(d + D_SMODELCOUNT); w['static_surface_count'] = M.u32(d + D_STATICSURFCOUNT)
    sp = M.u32(d + D_SURFACES)
    sraw = M.m.read(sp, sc * SURFACE)
    surfs = []
    mats = {}
    for i in range(sc):
        o = i * SURFACE
        layer_off, first_vertex, first_idx = struct.unpack_from('>III', sraw, o)
        span, tri_count = struct.unpack_from('>HH', sraw, o + 0xC)
        base_index, material = struct.unpack_from('>II', sraw, o + 0x10)
        lm, refl, plight, flags = struct.unpack_from('>BBBB', sraw, o + 0x18)
        bounds = struct.unpack_from('>6f', sraw, o + 0x1C)
        if material not in mats:
            mats[material] = M.cstr(M.u32(material)) if material >= 0x10000 else '<null %#x>' % material
        surfs.append((layer_off, first_vertex, first_idx, span, tri_count, base_index, material, lm, refl, plight, flags, bounds))
    w['surfaces'] = surfs; w['materials'] = mats
    # static model draw instances
    n = w['smodel_count']; dp = M.u32(d + D_SMODELDRAW)
    draws = []
    if n and dp:
        draw = M.m.read(dp, n * DRAW)
        for i in range(n):
            o = i * DRAW
            cull = struct.unpack_from('>f', draw, o)[0]
            origin = struct.unpack_from('>3f', draw, o + 4)
            axes = struct.unpack_from('>3I', draw, o + 0x10)
            scale = struct.unpack_from('>f', draw, o + 0x1C)[0]
            model = struct.unpack_from('>I', draw, o + 0x20)[0]
            refl, plight = draw[o + 0x24], draw[o + 0x25]
            draws.append((cull, origin, axes, scale, model, refl, plight))
    w['smodels'] = draws
    w['mins'] = struct.unpack('>3f', M.m.read(hdr + O_MINS, 12)); w['maxs'] = struct.unpack('>3f', M.m.read(hdr + O_MINS + 12, 12))
    # Sky surfaces: indices into the surface array (u32 each, exactly as the
    # porter serialized them and Load_GfxWorld consumed them).
    sky_count = M.u32(hdr + O_SKYSURFCOUNT); sky_ptr = M.u32(hdr + O_SKYSURFS)
    w['sky_surface_indices'] = (
        [int(x) for x in M.arr(sky_ptr, sky_count, '>u4')] if sky_count and sky_ptr else [])
    # Sun: the linked GfxLight (color at +4, direction at +0x10); absent on
    # worlds without a sunLight branch.
    sun_ptr = M.u32(hdr + O_SUNLIGHT)
    if sun_ptr:
        color = [M.f32(sun_ptr + 4 + i * 4) for i in range(3)]
        direction = [M.f32(sun_ptr + 0x10 + i * 4) for i in range(3)]
        finite = all(abs(v) < 1e6 and v == v for v in color + direction)
        w['sun'] = {'color': color, 'direction': direction} if finite else None
    else:
        w['sun'] = None
    return w


def unpack_x11y11z10n(v):
    """RSX X11Y11Z10N: three signed normalized components (x:11, y:11, z:10)."""
    x = v & 0x7FF; y = (v >> 11) & 0x7FF; z = (v >> 22) & 0x3FF
    if x >= 0x400: x -= 0x800
    if y >= 0x400: y -= 0x800
    if z >= 0x200: z -= 0x400
    return (x / 1023.0, y / 1023.0, z / 511.0)


def parse_spawns(entity_string):
    """(origin, angles) for every mp_*_spawn / info_player_start in a MapEnts string."""
    out = []
    for block in re.findall(r'\{(.*?)\}', entity_string, re.S):
        kv = dict(re.findall(r'"([^"]+)"\s+"([^"]*)"', block))
        cn = kv.get('classname', '')
        if cn.startswith('mp_') and 'spawn' in cn or cn == 'info_player_start':
            try:
                o = tuple(float(x) for x in kv.get('origin', '0 0 0').split())
                a = tuple(float(x) for x in kv.get('angles', '0 0 0').split())
            except ValueError:
                continue
            out.append((cn, o, a))
    return out


def surface_strides(surfaces):
    """Byte stride of the per-surface vertex layer records, from consecutive layer offsets.
    Plain materials use 28; retail's merged '*' materials use wider records."""
    fv = surfaces[:, 1]; lo = surfaces[:, 0]
    groups = {}
    for k in range(len(surfaces)): groups.setdefault((int(fv[k]), int(lo[k])), []).append(k)
    keys = sorted(groups); st = {}
    for (f, l), (f2, l2) in zip(keys[:-1], keys[1:]):
        n = f2 - f
        if n > 0 and (l2 - l) % n == 0:
            for k in groups[(f, l)]: st[k] = (l2 - l) // n
    return st


def vertex_colors(d):
    """Per-vertex RGBA read through each surface's own layer record (any stride)."""
    surfaces = d['surfaces']; layers = d['layers_raw']; n = len(d['pos'])
    col = np.full((n, 4), 255, np.uint8)
    st = surface_strides(surfaces)
    for k, s in enumerate(surfaces):
        layer_off, fv, first, span, tc, base = (int(x) for x in s[:6])
        stride = st.get(k)
        if stride is None: continue          # last group: stride unknown, keep white
        ii = np.unique(d['indices'][base:base + tc * 3]) + fv
        for v in ii:
            o = layer_off + (int(v) - fv) * stride
            if o + 4 <= len(layers): col[v] = layers[o:o + 4]
    return col

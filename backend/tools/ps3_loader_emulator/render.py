#!/usr/bin/env python3
"""Software renderer for a dumped world (.npz + .meta): z-buffered triangles, two shadings.

    python3 render.py <world.npz> <out_prefix> [--views N] [--size WxH] [--spawn K ...]

Produces <out_prefix>_<k>_mat.png (flat colour per material name, Lambert-lit by the face
normal) and <out_prefix>_<k>_vcol.png (Gouraud vertex colour = baked lighting lane).
"""
import sys, os, math, pickle, hashlib, json
import numpy as np
from PIL import Image


def mat_color(name):
    # colour by material identity, not by platform naming: Retail PS3 says w/ and m/ where
    # the PC (and our port) says wc/ and mc/
    key = name.split('/', 1)[-1] if '/' in name else name
    h = hashlib.md5(key.encode('latin1', 'replace')).digest()
    return np.array([64 + h[0] * 3 // 4, 64 + h[1] * 3 // 4, 64 + h[2] * 3 // 4], dtype=np.float64)


def camera(origin, angles, fov_deg=80.0, aspect=4 / 3):
    pitch, yaw, roll = (math.radians(a) for a in angles)
    # IW axes: x forward, y left, z up; angles (pitch, yaw, roll) in degrees
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    forward = np.array([cp * cy, cp * sy, -sp])
    right = np.array([sy, -cy, 0.0])
    up = np.cross(right, forward)
    f = 1.0 / math.tan(math.radians(fov_deg) / 2)
    return np.array(origin, dtype=np.float64), forward, right, up, f, aspect


def project(pts, cam, W, H, near=4.0):
    o, fwd, right, up, f, aspect = cam
    d = pts - o
    z = d @ fwd; x = d @ right; y = d @ up
    with np.errstate(divide='ignore', invalid='ignore'):
        sx = (W / 2) * (1 - (x / z) * f / aspect)   # right axis -> screen left (IW y is left)
        sy = (H / 2) * (1 - (y / z) * f)
    return sx, sy, z


def rasterize(tris, sx, sy, z, colors, W, H, gouraud=None):
    """tris: (T,3) vertex ids; colors: (T,3) flat colour or gouraud (V,3) per-vertex colour."""
    img = np.zeros((H, W, 3), np.float64)
    zbuf = np.full((H, W), np.inf)
    valid = (z[tris] > 1.0).all(axis=1)
    order = np.nonzero(valid)[0]
    X = sx[tris]; Y = sy[tris]; Z = z[tris]
    # cull triangles entirely outside the screen
    inside = (X.max(axis=1) >= 0) & (X.min(axis=1) < W) & (Y.max(axis=1) >= 0) & (Y.min(axis=1) < H)
    order = order[inside[order]]
    for t in order:
        x0, x1, x2 = X[t]; y0, y1, y2 = Y[t]
        xmin = max(int(math.floor(min(x0, x1, x2))), 0); xmax = min(int(math.ceil(max(x0, x1, x2))), W - 1)
        ymin = max(int(math.floor(min(y0, y1, y2))), 0); ymax = min(int(math.ceil(max(y0, y1, y2))), H - 1)
        if xmax < xmin or ymax < ymin: continue
        det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(det) < 1e-9: continue
        xs = np.arange(xmin, xmax + 1) + 0.5; ys = np.arange(ymin, ymax + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        l1 = ((gx - x0) * (y2 - y0) - (x2 - x0) * (gy - y0)) / det
        l2 = ((x1 - x0) * (gy - y0) - (gx - x0) * (y1 - y0)) / det
        l0 = 1 - l1 - l2
        m = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
        if not m.any(): continue
        # perspective-correct depth: interpolate 1/z
        iz = l0 / Z[t, 0] + l1 / Z[t, 1] + l2 / Z[t, 2]
        zz = 1.0 / iz
        sub = zbuf[ymin:ymax + 1, xmin:xmax + 1]
        upd = m & (zz < sub)
        if not upd.any(): continue
        sub[upd] = zz[upd]
        if gouraud is None:
            img[ymin:ymax + 1, xmin:xmax + 1][upd] = colors[t]
        else:
            c = (l0[..., None] * gouraud[tris[t, 0]] + l1[..., None] * gouraud[tris[t, 1]] + l2[..., None] * gouraud[tris[t, 2]])
            img[ymin:ymax + 1, xmin:xmax + 1][upd] = c[upd]
    return img, zbuf


def world_triangles(d):
    pos = d['pos']; idx = d['indices']; surfs = d['surfaces']
    tris = []; mat_ids = []
    for s in surfs:
        layer_off, fv, first, span, tc, base, mat = s[:7]
        ii = idx[base:base + tc * 3].reshape(-1, 3) + fv
        tris.append(ii); mat_ids.append(np.full(len(ii), mat))
    return pos, np.concatenate(tris), np.concatenate(mat_ids)


def model_triangles(d, models):
    """World-space triangles of every static model draw instance (LOD 0)."""
    from worldview import unpack_x11y11z10n
    by_hdr = {m['hdr']: (n, m) for n, m in models.items() if 'hdr' in m}
    P = []; T = []; C = []; base = 0; missing = 0
    for row in d['smodels']:
        cull, ox, oy, oz, a0, a1, a2, scale, hdr = row[:9]
        m = by_hdr.get(int(hdr))
        if m is None: missing += 1; continue
        name, md = m
        axes = np.array([unpack_x11y11z10n(int(a)) for a in (a0, a1, a2)])
        dist, nsurf, first = md['lods'][0]
        for s in md['surfaces'][first:first + nsurf]:
            if s['vc'] == 0 or s['tc'] == 0: continue
            local = s['pos'] * scale
            world = np.array([ox, oy, oz]) + local @ axes
            P.append(world); T.append(s['tri'] + base); base += len(world)
            C.append(np.full(len(s['tri']), int(hashlib.md5(name.encode()).hexdigest()[:7], 16)))
    if not P: return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros(0, np.int64), missing
    return np.concatenate(P), np.concatenate(T), np.concatenate(C), missing


def render_views(npz, out_prefix, views=4, size=(640, 480), spawn_ids=None, sun=(0.3, 0.5, 0.8), models_pkl=None):
    z = np.load(npz); d = {k: z[k] for k in z.files}; meta = pickle.load(open(npz + '.meta', 'rb'))
    pos, tris, mat_ids = world_triangles(d)
    if models_pkl:
        models = pickle.load(open(models_pkl, 'rb'))
        mp, mt, mc, missing = model_triangles(d, models)
        print('static models: %d triangles from %d draw instances (%d without model)' % (len(mt), len(d['smodels']), missing))
        if len(mt):
            mt = mt + len(pos); pos = np.concatenate([pos, mp]); tris = np.concatenate([tris, mt])
            mat_ids = np.concatenate([mat_ids, -1 - mc])   # negative ids: model colour keys
            vcol_extra = np.full((len(mp), 4), 200, np.uint8)
            d['color'] = np.concatenate([d['color'], vcol_extra])
    mats = meta['materials']
    names = {m: (mats.get(m, '?') if m >= 0 else 'model#%d' % (-1 - m)) for m in np.unique(mat_ids)}   # model keys are stable md5 prefixes
    col_by_mat = {m: mat_color(n) for m, n in names.items()}
    base_col = np.array([col_by_mat[m] for m in mat_ids])
    # face normals -> Lambert
    p0, p1, p2 = pos[tris[:, 0]], pos[tris[:, 1]], pos[tris[:, 2]]
    n = np.cross(p1 - p0, p2 - p0); ln = np.linalg.norm(n, axis=1); ln[ln == 0] = 1; n /= ln[:, None]
    sunv = np.array(sun) / np.linalg.norm(sun)
    lam = 0.45 + 0.55 * np.abs(n @ sunv)
    flat = base_col * lam[:, None]
    from worldview import vertex_colors
    nworld = len(d['color']) if not models_pkl or not len(mt) else len(d['color']) - len(mp)
    wc = vertex_colors({'surfaces': d['surfaces'], 'layers_raw': d['layers_raw'], 'pos': pos[:nworld], 'indices': d['indices']})
    vcol = np.concatenate([wc[:, :3], d['color'][nworld:, :3]]).astype(np.float64)
    # lightmap-coordinate visualisation (u -> red, v -> green) for the world; models grey
    lmuv = None
    if len(d['lmapcoord']) == nworld:
        lm = np.nan_to_num(d['lmapcoord'].astype(np.float64))
        lmuv = np.concatenate([np.stack([lm[:, 0] * 255, lm[:, 1] * 255, np.full(nworld, 80.0)], axis=1),
                               np.full((len(pos) - nworld, 3), 120.0)])
    spawns = meta['spawns']
    if spawn_ids is None:
        step = max(1, len(spawns) // views)
        spawn_ids = list(range(0, len(spawns), step))[:views]
    W, H = size
    stats = {}
    for k, sid in enumerate(spawn_ids):
        cn, o, a = spawns[sid]
        eye = np.array(o) + np.array([0, 0, 60.0])
        cam = camera(eye, (max(-10, a[0]), a[1], 0))
        sx, sy, z = project(pos, cam, W, H)
        img, zb = rasterize(tris, sx, sy, z, flat, W, H)
        Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save('%s_%d_mat.png' % (out_prefix, k))
        img2, _ = rasterize(tris, sx, sy, z, None, W, H, gouraud=vcol)
        Image.fromarray(np.clip(img2, 0, 255).astype(np.uint8)).save('%s_%d_vcol.png' % (out_prefix, k))
        if lmuv is not None:
            img3, _ = rasterize(tris, sx, sy, z, None, W, H, gouraud=lmuv)
            Image.fromarray(np.clip(img3, 0, 255).astype(np.uint8)).save('%s_%d_lmuv.png' % (out_prefix, k))
        cover = float(np.isfinite(zb).mean())
        stats[k] = {'spawn': sid, 'classname': cn, 'origin': o, 'angles': a, 'coverage': cover}
        print('view %d: %s at %s angles %s coverage %.3f' % (k, cn, o, a, cover)); sys.stdout.flush()
    json.dump(stats, open(out_prefix + '_views.json', 'w'), indent=1)
    return stats


if __name__ == '__main__':
    npz = sys.argv[1]; out = sys.argv[2]
    views = int(sys.argv[sys.argv.index('--views') + 1]) if '--views' in sys.argv else 4
    size = (640, 480)
    if '--size' in sys.argv:
        w, h = sys.argv[sys.argv.index('--size') + 1].split('x'); size = (int(w), int(h))
    sp = None
    if '--spawn' in sys.argv:
        i = sys.argv.index('--spawn') + 1; sp = []
        while i < len(sys.argv) and sys.argv[i].isdigit(): sp.append(int(sys.argv[i])); i += 1
    mp = sys.argv[sys.argv.index('--models') + 1] if '--models' in sys.argv else None
    render_views(npz, out, views, size, sp, models_pkl=mp)

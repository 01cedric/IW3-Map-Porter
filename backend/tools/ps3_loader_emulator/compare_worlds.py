#!/usr/bin/env python3
"""Compare two dumped worlds surface by surface (matched by identical bounds):
triangle geometry, vertex colour (baked lighting), texture/lightmap coordinates, normals.

    python3 compare_worlds.py <a.world.npz> <b.world.npz>
"""
import sys, pickle, collections
import numpy as np
from worldview import unpack_x11y11z10n


def load(p):
    z = np.load(p); d = {k: z[k] for k in z.files}; m = pickle.load(open(p + '.meta', 'rb'))
    return d, m


def surface_vertices(d, i):
    s = d['surfaces'][i]
    layer_off, fv, first, span, tc, base = s[:6]
    ii = d['indices'][base:base + tc * 3] + fv
    return ii.reshape(-1, 3)


def main(pa, pb):
    A, ma = load(pa); B, mb = load(pb)
    bb = collections.defaultdict(list)
    for j, b in enumerate(B['bounds']): bb[tuple(np.round(b, 2))].append(j)
    pairs = []
    for i, b in enumerate(A['bounds']):
        js = bb.get(tuple(np.round(b, 2)))
        if js and len(js) == 1: pairs.append((i, js[0]))
    print('surfaces: A=%d B=%d, uniquely bounds-matched pairs=%d' % (len(A['bounds']), len(B['bounds']), len(pairs)))
    stats = collections.Counter(); worst = {}
    tri_equal = 0; tri_total = 0
    color_d = []; tex_d = []; lm_d = []; nrm_d = []
    vcount_mismatch = 0
    def strides(D):
        s = D['surfaces']; fv = s[:, 1]; lo = s[:, 0]
        groups = {}
        for k in range(len(s)): groups.setdefault((int(fv[k]), int(lo[k])), []).append(k)
        keys = sorted(groups); st = {}
        for (f, l), (f2, l2) in zip(keys[:-1], keys[1:]):
            n = f2 - f
            if n > 0 and (l2 - l) % n == 0:
                for k in groups[(f, l)]: st[k] = (l2 - l) // n
        return st
    stA = strides(A); stB = strides(B)
    def lane(D, st, k, v):
        """(color, texcoord, lmapcoord, normal_packed) of vertex v inside surface k."""
        s = D['surfaces'][k]; stride = st.get(k, 28)
        if stride != 28: return None
        o = int(s[0]) + (int(v) - int(s[1])) * stride
        raw = D['layers_raw'][o:o + 28].tobytes()
        if len(raw) < 28: return None
        import struct as _s
        col = np.frombuffer(raw[:4], np.uint8); f = _s.unpack('>4f', raw[4:20]); nrm = _s.unpack('>I', raw[20:24])[0]
        return col, np.array(f[:2]), np.array(f[2:]), nrm
    skipped_layers = 0
    for i, j in pairs:
        ta = surface_vertices(A, i); tb = surface_vertices(B, j)
        if ta.shape != tb.shape:
            stats['tri-count-differs'] += 1; continue
        pa_ = A['pos'][ta]; pb_ = B['pos'][tb]
        # same triangles in the same order?
        same = np.allclose(pa_, pb_, atol=1e-3)
        tri_total += len(ta)
        if same: tri_equal += len(ta); stats['geometry-identical'] += 1
        else:
            # try as sets of triangles (order may differ)
            ka = {tuple(np.round(t.flatten(), 2)) for t in pa_}; kb = {tuple(np.round(t.flatten(), 2)) for t in pb_}
            if ka == kb: stats['geometry-identical-reordered'] += 1; tri_equal += len(ta)
            else: stats['geometry-differs'] += 1; worst.setdefault('geometry', (i, j)); continue
        # vertex attributes: match triangle by its three positions, vertex by position inside it
        # (only where both sides use the plain 28-byte layer record; retail's merged '*'
        # materials carry wider per-surface layer records that this reader does not decode)
        if stA.get(i, 28) != 28 or stB.get(j, 28) != 28: skipped_layers += 1; continue
        tb_by_key = {}
        for t in tb:
            tb_by_key[tuple(sorted(tuple(np.round(B['pos'][v], 2)) for v in t))] = t
        seen = set()
        for t in ta:
            key = tuple(sorted(tuple(np.round(A['pos'][v], 2)) for v in t))
            u3 = tb_by_key.get(key)
            if u3 is None: vcount_mismatch += 3; continue
            posb = {tuple(np.round(B['pos'][u], 2)): u for u in u3}
            for v in t:
                if v in seen: continue
                seen.add(v)
                u = posb.get(tuple(np.round(A['pos'][v], 2)))
                if u is None: vcount_mismatch += 1; continue
                LA = lane(A, stA, i, v); LB = lane(B, stB, j, u)
                if LA is None or LB is None: stats['lane-unavailable'] += 1; continue
                ca = LA[0][:3].astype(int); cb = LB[0][:3].astype(int)
                color_d.append(np.abs(ca - cb).max())
                if np.isnan(LA[1]).any() or np.isnan(LB[1]).any(): stats['nan-texcoord'] += 1
                else:
                    dd = np.abs(LA[1] - LB[1]).max(); tex_d.append(dd)
                    if dd > 1e-3:
                        frac = np.abs((LA[1] - LB[1]) - np.round(LA[1] - LB[1])).max()
                        stats['texcoord-integer-offset' if frac < 1e-3 else 'texcoord-real-diff'] += 1
                        if frac >= 1e-3 and stats['texcoord-example'] < 5:
                            stats['texcoord-example'] += 1
                            print('   texcoord example: A %s  B %s  pos %s  matA %r matB %r' % (LA[1], LB[1], A['pos'][v], ma['materials'][int(A['surfaces'][i][6])], mb['materials'][int(B['surfaces'][j][6])]))
                if np.isnan(LA[2]).any() or np.isnan(LB[2]).any(): stats['nan-lmap'] += 1
                else: lm_d.append(np.abs(LA[2] - LB[2]).max())
                na = np.array(unpack_x11y11z10n(int(LA[3]))); nb = np.array(unpack_x11y11z10n(int(LB[3])))
                nrm_d.append(float(np.abs(na - nb).max()))
                if np.abs(na - nb).max() > 0.05 and stats['normal-example'] < 3:
                    stats['normal-example'] += 1; print('   normal example: A %#x %s  B %#x %s pos %s' % (LA[3], na.round(3), LB[3], nb.round(3), A['pos'][v]))
    print('surface verdicts:', dict(stats), ' (attribute compare skipped on %d surfaces with wide layer records)' % skipped_layers)
    print('triangles identical: %d / %d' % (tri_equal, tri_total))
    def summ(name, arr, thr):
        if not arr: print('  %s: n/a' % name); return
        a = np.array(arr)
        print('  %-14s n=%d  max=%.4f  mean=%.5f  >%g: %d (%.2f%%)' % (name, len(a), a.max(), a.mean(), thr, (a > thr).sum(), 100.0 * (a > thr).mean()))
    print('per-vertex attribute differences on matched vertices (unmatched by position: %d):' % vcount_mismatch)
    summ('colour (0-255)', color_d, 8)
    summ('texcoord', tex_d, 1e-3)
    summ('lmapcoord', lm_d, 1e-3)
    summ('normal', nrm_d, 0.02)
    if worst: print('first differing surfaces:', worst)
    # colour histogram to see whether one side is unlit/blank
    for tag, D in (('A', A), ('B', B)):
        c = D['color'][:, :3].astype(int)
        print('  %s colour mean rgb=%s  alpha mean=%.1f  zero-colour vertices=%d' % (tag, c.mean(axis=0).round(1), D['color'][:, 3].mean(), (c.sum(axis=1) == 0).sum()))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])

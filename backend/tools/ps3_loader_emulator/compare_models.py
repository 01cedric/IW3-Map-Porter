#!/usr/bin/env python3
"""Compare dumped XModel geometry of two zones by model name and surface index.

    python3 compare_models.py <a.models.pkl> <b.models.pkl>
"""
import sys, pickle, collections, struct
import numpy as np
from worldview import unpack_x11y11z10n


def half(u):
    return struct.unpack('<e', struct.pack('<H', u))[0]


def main(pa, pb):
    A = pickle.load(open(pa, 'rb')); B = pickle.load(open(pb, 'rb'))
    common = sorted(set(A) & set(B))
    print('models: A=%d B=%d common=%d  A-only=%s  B-only=%s' % (len(A), len(B), len(common),
          sorted(set(A) - set(B))[:6], sorted(set(B) - set(A))[:6]))
    st = collections.Counter(); col_d = []; tex_d = []; nrm_d = []; tan_d = []; examples = []
    for name in common:
        ma, mb = A[name], B[name]
        if ma['numsurfs'] != mb['numsurfs']: st['surface-count-differs'] += 1; continue
        if not np.allclose(ma['mins'], mb['mins'], atol=0.01) or not np.allclose(ma['maxs'], mb['maxs'], atol=0.01): st['bounds-differ'] += 1
        for k, (sa, sb) in enumerate(zip(ma['surfaces'], mb['surfaces'])):
            if sa['vc'] != sb['vc'] or sa['tc'] != sb['tc']:
                st['surface-size-differs'] += 1
                if len(examples) < 5: examples.append((name, k, sa['vc'], sb['vc'], sa['tc'], sb['tc']))
                continue
            if (sa['deformed'], sa['rigid'] == 1) != (sb['deformed'], sb['rigid'] == 1): st['stream-kind-differs'] += 1
            if not np.allclose(sa['pos'], sb['pos'], atol=1e-3): st['positions-differ'] += 1; continue
            if not np.array_equal(sa['tri'], sb['tri']):
                # same triangle set?
                ka = {tuple(sorted(t)) for t in sa['tri']}; kb = {tuple(sorted(t)) for t in sb['tri']}
                st['indices-identical-reordered' if ka == kb else 'indices-differ'] += 1
                if ka != kb: continue
            else: st['geometry-identical'] += 1
            if sa['material'] != sb['material']: st['material-name-differs'] += 1
            sec_a = sa['sec']; sec_b = sb['sec']
            if len(sec_a) != len(sec_b) or len(sec_a) == 0: st['no-secondary'] += 1; continue
            col_d.append(np.abs(sec_a[:, :3].astype(int) - sec_b[:, :3].astype(int)).max(axis=1))
            ta = np.frombuffer(sec_a[:, 4:8].tobytes(), '>u2').reshape(-1, 2); tb = np.frombuffer(sec_b[:, 4:8].tobytes(), '>u2').reshape(-1, 2)
            fa = np.vectorize(half)(ta); fb = np.vectorize(half)(tb)
            tex_d.append(np.abs(fa - fb).max(axis=1))
            na = np.frombuffer(sec_a[:, 8:12].tobytes(), '>u4'); nb = np.frombuffer(sec_b[:, 8:12].tobytes(), '>u4')
            nrm_d.append(np.array([np.abs(np.array(unpack_x11y11z10n(int(x))) - np.array(unpack_x11y11z10n(int(y)))).max() for x, y in zip(na, nb)]))
            xa = np.frombuffer(sec_a[:, 12:16].tobytes(), '>u4'); xb = np.frombuffer(sec_b[:, 12:16].tobytes(), '>u4')
            tan_d.append(np.array([np.abs(np.array(unpack_x11y11z10n(int(x))) - np.array(unpack_x11y11z10n(int(y)))).max() for x, y in zip(xa, xb)]))
    print('surface verdicts:', dict(st))
    for e in examples: print('   size differs:', e)
    def summ(nm, arrs, thr):
        if not arrs: print('  %s: n/a' % nm); return
        a = np.concatenate(arrs)
        print('  %-14s n=%d  max=%.4f  mean=%.5f  >%g: %d (%.2f%%)' % (nm, len(a), a.max(), a.mean(), thr, (a > thr).sum(), 100.0 * (a > thr).mean()))
    summ('colour (0-255)', col_d, 8); summ('texcoord', tex_d, 1e-3); summ('normal', nrm_d, 0.02); summ('tangent', tan_d, 0.02)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])

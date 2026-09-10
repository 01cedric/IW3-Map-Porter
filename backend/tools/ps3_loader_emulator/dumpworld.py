#!/usr/bin/env python3
"""Load a map zone into the emulated database and dump its linked GfxWorld/MapEnts to .npz.

    python3 dumpworld.py <zone.bin> <mapname> [out.npz]
"""
import sys, os, time, pickle
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dbworld import Machine
from worldview import read_world, parse_spawns, Mem

zone, mapname = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else zone + '.world.npz'
M = Machine(real_link=True, fault_low=True)
M.restore(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'support_db.snapshot'))
t = time.time()
zl = M.load_zone(open(zone, 'rb').read(), mapname)
print('loaded %d assets in %.1fs' % (len(zl.assets), time.time() - t))
bsp = 'maps/mp/%s.d3dbsp' % mapname
gfx, err = M.find(18, bsp)
ents, err2 = M.find(17, bsp)
print('gfx_map header %#x  map_ents header %#x' % (gfx, ents))
w = read_world(M.m, gfx)
R = Mem(M.m)
es = R.cstr(R.u32(ents + 4), 4 << 20)
spawns = parse_spawns(es)
print('world %r base %r: %d verts, %d indices, %d surfaces, %d smodels, %d materials, %d spawns'
      % (w['name'], w['base'], w['vertex_count'], w['index_count'], w['surface_count'], w['smodel_count'], len(w['materials']), len(spawns)))
print('mins', w['mins'], 'maxs', w['maxs'])
# empirical check of the index/vertex base semantics against the surface bounds
pos = w['pos']; idx = w['indices']
def inside(p, b, eps=1.0):
    return np.all(p >= np.array(b[:3]) - eps, axis=1) & np.all(p <= np.array(b[3:]) + eps, axis=1)
hyp = {'firstVertex+idx': 0, 'first+idx': 0, 'idx': 0}
bad = {k: 0 for k in hyp}
for s in w['surfaces'][:400]:
    layer_off, fv, first, span, tc, base, mat, lm, refl, pl, flags, bounds = s
    ii = idx[base:base + tc * 3]
    for k, off in (('firstVertex+idx', fv), ('first+idx', first), ('idx', 0)):
        vv = ii + off
        if vv.max() >= len(pos): bad[k] += 1; continue
        ok = inside(pos[vv], bounds)
        if not ok.all(): bad[k] += 1
print('surfaces (of 400) violating bounds per hypothesis:', bad)
s0 = w['surfaces'][0]
print('surface[0]:', s0[:11], 'bounds', [round(x, 1) for x in s0[11]])
np.savez_compressed(out, pos=pos, indices=idx, color=w['color'],
                    texcoord=w.get('texcoord', np.zeros((0, 2))), lmapcoord=w.get('lmapcoord', np.zeros((0, 2))),
                    normal_packed=w.get('normal_packed', np.zeros(0, np.uint32)),
                    layers_raw=np.frombuffer(w['layers_raw'], dtype=np.uint8),
                    surfaces=np.array([s[:11] for s in w['surfaces']], dtype=np.int64),
                    bounds=np.array([s[11] for s in w['surfaces']], dtype=np.float64),
                    smodels=np.array([(d[0], *d[1], *d[2], d[3], d[4], d[5], d[6]) for d in w['smodels']], dtype=np.float64) if w['smodels'] else np.zeros((0, 12)),
                    mins=np.array(w['mins']), maxs=np.array(w['maxs']))
pickle.dump({'materials': w['materials'], 'spawns': spawns, 'name': w['name'], 'base': w['base'],
             'entity_string': es}, open(out + '.meta', 'wb'))
print('written', out)

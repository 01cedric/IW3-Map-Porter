#!/usr/bin/env python3
"""Dump every XModel the map zone linked (geometry of all LOD surfaces) from emulated memory.

    python3 dumpmodels.py <zone.bin> <mapname> [out.pkl]
"""
import sys, os, time, pickle, struct
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dbworld import Machine, NUM_TYPES
from worldview import Mem

zone, mapname = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else zone + '.models.pkl'
M = Machine(real_link=True, fault_low=True)
M.restore(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'support_db.snapshot'))
inv0 = M.inventory()
t = time.time()
zl = M.load_zone(open(zone, 'rb').read(), mapname)
print('loaded %d assets in %.1fs' % (len(zl.assets), time.time() - t))
inv = M.inventory()
R = Mem(M.m)
models = {}
for (typ, name), hdr in inv.items():
    if typ != 3 or (typ, name) in inv0: continue
    numsurfs = R.u8(hdr + 6); surfs = R.u32(hdr + 0x20); mats = R.u32(hdr + 0x24)
    lods = []
    for li in range(4):
        d = hdr + 0x28 + li * 0x18
        lods.append((R.f32(d), R.u16(d + 4), R.u16(d + 6)))
    mins = struct.unpack('>3f', M.m.read(hdr + 0x9C, 12)); maxs = struct.unpack('>3f', M.m.read(hdr + 0xA8, 12))
    surf_list = []
    for si in range(numsurfs):
        s = surfs + si * 0x4C
        tile, deformed = R.u8(s), R.u8(s + 1); vc = R.u16(s + 2); tc = R.u16(s + 4)
        tri_p = R.u32(s + 8); prim_p = R.u32(s + 0x18); sec_p = R.u32(s + 0x24); rigid = R.u32(s + 0x30)
        tri = np.frombuffer(M.m.read(tri_p, tc * 6), '>u2').astype(np.int64).reshape(-1, 3) if tc and tri_p >= 0x10000 else np.zeros((0, 3), np.int64)
        prim = np.frombuffer(M.m.read(prim_p, vc * 16), '>f4').reshape(-1, 4) if vc and prim_p >= 0x10000 else np.zeros((0, 4), np.float32)
        sec = np.frombuffer(M.m.read(sec_p, vc * 16), np.uint8).reshape(-1, 16) if vc and sec_p >= 0x10000 else np.zeros((0, 16), np.uint8)
        mat_p = R.u32(mats + si * 4) if mats >= 0x10000 else 0
        mat_name = R.cstr(R.u32(mat_p)) if mat_p >= 0x10000 else ''
        surf_list.append({'tile': tile, 'deformed': deformed, 'vc': vc, 'tc': tc, 'rigid': rigid, 'tri': tri,
                          'pos': np.array(prim[:, :3], np.float64), 'sec': sec.copy(), 'material': mat_name})
    models[name] = {'numsurfs': numsurfs, 'lods': lods, 'mins': mins, 'maxs': maxs, 'surfaces': surf_list,
                    'bones': R.u8(hdr + 4), 'hdr': hdr}
print('dumped %d models, %d surfaces, %d vertices' % (len(models), sum(len(m['surfaces']) for m in models.values()),
      sum(s['vc'] for m in models.values() for s in m['surfaces'])))
pickle.dump(models, open(out, 'wb'))
print('written', out)

#!/usr/bin/env python3
"""Dump the linked PS3 ClipMap (collision) of a map zone from emulated memory and, given a
second dump, compare: counts, planes, brush AABBs/contents, leafs, collision vertices and
triangles.

    python3 dumpclip.py <zone.bin> <mapname> [out.pkl]
    python3 dumpclip.py --compare a.clip.pkl b.clip.pkl
"""
import sys, os, time, pickle, struct
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PLANE, BRUSH, LEAF, NODE, VERTEX, BRUSH_SIDE = 0x14, 0x50, 0x2C, 0x08, 0x0C, 0x0C


def dump(zone, mapname, out):
    from dbworld import Machine
    from worldview import Mem
    M = Machine(real_link=True, fault_low=True)
    M.restore(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'support_db.snapshot'))
    zl = M.load_zone(open(zone, 'rb').read(), mapname)
    hdr, err = M.find(13, 'maps/mp/%s.d3dbsp' % mapname)
    R = Mem(M.m); rd = M.m.read
    c = {}
    c['name'] = R.cstr(R.u32(hdr))
    counts = {}
    for label, off in (('planes', 8), ('staticModels', 0x10), ('materials', 0x18), ('brushSides', 0x20), ('brushEdges', 0x28),
                       ('nodes', 0x30), ('leafs', 0x38), ('leafBrushNodes', 0x40), ('leafBrushes', 0x48), ('verts', 0x58),
                       ('borders', 0x6C), ('partitions', 0x74), ('aabbs', 0x7C), ('submodels', 0x84)):
        counts[label] = R.u32(hdr + off)
    counts['tris'] = R.u32(hdr + 0x60); counts['brushes'] = R.u16(hdr + 0x8C)
    counts['clusters'] = R.u32(hdr + 0x94); counts['dynModels'] = R.u16(hdr + 0xF4); counts['dynBrushes'] = R.u16(hdr + 0xF6)
    c['counts'] = counts
    n = counts['planes']; p = R.u32(hdr + 0xC)
    raw = rd(p, n * PLANE)
    c['planes'] = np.array([struct.unpack_from('>4fBB', raw, i * PLANE) for i in range(n)], dtype=object)
    n = counts['brushes']; p = R.u32(hdr + 0x90); raw = rd(p, n * BRUSH)
    c['brushes'] = [struct.unpack_from('>3fi3fI', raw, i * BRUSH) + (struct.unpack_from('>H', raw, i * BRUSH + 0x24)[0],) for i in range(n)]
    n = counts['leafs']; p = R.u32(hdr + 0x3C); raw = rd(p, n * LEAF)
    c['leafs'] = [struct.unpack_from('>HHii3f3fih', raw, i * LEAF) for i in range(n)]
    n = counts['nodes']; p = R.u32(hdr + 0x34); raw = rd(p, n * NODE)
    c['nodes'] = [struct.unpack_from('>Ihh', raw, i * NODE) for i in range(n)]
    n = counts['verts']; p = R.u32(hdr + 0x5C)
    c['verts'] = np.frombuffer(rd(p, n * VERTEX), '>f4').reshape(n, 3).astype(np.float64)
    n = counts['tris']; p = R.u32(hdr + 0x64)
    c['tris'] = np.frombuffer(rd(p, n * 6), '>u2').reshape(n, 3).astype(np.int64)
    n = counts['brushSides']; p = R.u32(hdr + 0x24); raw = rd(p, n * BRUSH_SIDE)
    c['brushSides'] = [struct.unpack_from('>IIhBB', raw, i * BRUSH_SIDE) for i in range(n)]
    c['planes_ptr'] = R.u32(hdr + 0xC)
    # brush side planes are pointers into the plane array: turn them into indices
    pp = c['planes_ptr']
    c['brushSidePlane'] = [(s[0] - pp) // PLANE if s[0] >= pp else -1 for s in c['brushSides']]
    print('clipmap %r: %s' % (c['name'], counts))
    pickle.dump(c, open(out, 'wb')); print('written', out)


def compare(pa, pb):
    A = pickle.load(open(pa, 'rb')); B = pickle.load(open(pb, 'rb'))
    print('counts A:', A['counts']); print('counts B:', B['counts'])
    same = {k: A['counts'][k] == B['counts'].get(k) for k in A['counts']}
    print('count agreement:', {k: v for k, v in same.items()})
    # planes: compare as sets of rounded tuples (order may differ) and in order
    pa_ = [tuple(round(float(x), 4) for x in r[:4]) + (r[4], r[5]) for r in A['planes']]
    pb_ = [tuple(round(float(x), 4) for x in r[:4]) + (r[4], r[5]) for r in B['planes']]
    print('planes: in-order identical %d/%d, as sets: A-only %d, B-only %d'
          % (sum(1 for x, y in zip(pa_, pb_) if x == y), max(len(pa_), len(pb_)), len(set(pa_) - set(pb_)), len(set(pb_) - set(pa_))))
    # type/signbits lanes
    ta = [(r[4], r[5]) for r in A['planes']]; tb = [(r[4], r[5]) for r in B['planes']]
    print('plane type/signbits identical in order: %d/%d' % (sum(1 for x, y in zip(ta, tb) if x == y), min(len(ta), len(tb))))
    ba = {(tuple(round(v, 2) for v in b[:3]), b[3], tuple(round(v, 2) for v in b[4:7])) for b in A['brushes']}
    bb = {(tuple(round(v, 2) for v in b[:3]), b[3], tuple(round(v, 2) for v in b[4:7])) for b in B['brushes']}
    print('brushes (mins, contents, maxs): common %d, A-only %d, B-only %d' % (len(ba & bb), len(ba - bb), len(bb - ba)))
    na = {tuple(round(v, 2) for v in b[:6]) for b in A['brushes']}
    la = {(l[2], l[3], tuple(round(v, 2) for v in l[4:10])) for l in A['leafs']}; lb = {(l[2], l[3], tuple(round(v, 2) for v in l[4:10])) for l in B['leafs']}
    print('leafs (contents, bounds): common %d, A-only %d, B-only %d' % (len(la & lb), len(la - lb), len(lb - la)))
    va = {tuple(np.round(v, 2)) for v in A['verts']}; vb = {tuple(np.round(v, 2)) for v in B['verts']}
    print('collision verts: common %d, A-only %d, B-only %d' % (len(va & vb), len(va - vb), len(vb - va)))
    tra = {tuple(sorted(tuple(np.round(A['verts'][i], 2)) for i in t)) for t in A['tris']}
    trb = {tuple(sorted(tuple(np.round(B['verts'][i], 2)) for i in t)) for t in B['tris']}
    print('collision triangles: common %d, A-only %d, B-only %d' % (len(tra & trb), len(tra - trb), len(trb - tra)))
    # brush side -> plane consistency: every side plane index in range
    print('brush side plane indices out of range: A %d, B %d' % (sum(1 for i in A['brushSidePlane'] if i < 0 or i >= A['counts']['planes']),
                                                                 sum(1 for i in B['brushSidePlane'] if i < 0 or i >= B['counts']['planes'])))


if __name__ == '__main__':
    if sys.argv[1] == '--compare': compare(sys.argv[2], sys.argv[3])
    else: dump(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else sys.argv[1] + '.clip.pkl')

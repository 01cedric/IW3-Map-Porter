#!/usr/bin/env python3
"""Compare a dumped PS3 world (from emulated memory) with its PC source zone, vertex by vertex.

    python3 compare_pc_world.py <ps3.world.npz> <pc_map.ff> <mapname>

Position, colour (D3DCOLOR->RGBA), texture/lightmap coordinates and packed normal (PC
PackedUnitVec -> RSX X11Y11Z10N) are compared per surface; the surface order of the PS3
zone is the PC order.
"""
import sys, os, struct, pickle, collections
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from cod4porter.backend.v4_ffio import read_pc_fastfile
from cod4porter.pc_clipmap import parse_clipmap
from cod4porter.pc_gfxworld import parse_gfxworld, PC_WORLD_VERTEX, PC_SURFACE
from worldview import unpack_x11y11z10n

npz, pcff, mapname = sys.argv[1:4]
Z = np.load(npz); P = {k: Z[k] for k in Z.files}
doc = read_pc_fastfile(pcff); z = doc.zone
clip = parse_clipmap(z, mapname)
clip_counts = {'planes': len(clip.planes), 'submodels': len(clip.submodels), 'static_models': len(clip.static_models),
               'dynamic_models': len(clip.dyn_models), 'dynamic_brushes': len(clip.dyn_brushes)}
g = parse_gfxworld(z, mapname, clip_counts)
br = {b.name: b for b in g.full.branches}
vb = br['vd.vertices']; ib = br['indices']; dp = br['DPVS serialized graph']
vc = g.vertex_count
pcv = np.frombuffer(z[vb.start:vb.start + vc * PC_WORLD_VERTEX], np.uint8).reshape(vc, PC_WORLD_VERTEX)
pc_pos = np.frombuffer(pcv[:, 0:12].tobytes(), '<f4').reshape(vc, 3).astype(np.float64)
pc_col = pcv[:, 0x10:0x14]                                  # b,g,r,a
pc_tex = np.frombuffer(pcv[:, 0x14:0x1C].tobytes(), '<f4').reshape(vc, 2)
pc_lm = np.frombuffer(pcv[:, 0x1C:0x24].tobytes(), '<f4').reshape(vc, 2)
pc_nrm = pcv[:, 0x24:0x28]; pc_tan = pcv[:, 0x28:0x2C]
pc_idx = np.frombuffer(z[ib.start:ib.start + g.index_count * 2], '<u2').astype(np.int64)
# PC surfaces come after sorted-surf + smodel insts in the DPVS branch
sortcnt = g.static_surface_count + g.static_surface_count_no_decal
soff = dp.start + sortcnt * 2 + g.static_model_count * 0x1C
pc_surf = []
for i in range(g.surface_count):
    p = soff + i * PC_SURFACE
    fv, vcnt, tcnt, base, mat = struct.unpack_from('<iHHiI', z, p + 4)
    pc_surf.append((fv, vcnt, tcnt, base))
print('PC: %d verts, %d indices, %d surfaces; PS3: %d verts, %d indices, %d surfaces'
      % (vc, g.index_count, g.surface_count, len(P['pos']), len(P['indices']), len(P['surfaces'])))


def pc_unit(raw):
    scale = (int(raw[3]) + 192) / 32385.0
    return np.array([(int(raw[i]) - 127) * scale for i in range(3)])


ps = P['surfaces']; layers = P['layers_raw']
st = collections.Counter(); col_d = []; tex_d = []; lm_d = []; nrm_d = []; tan_d = []
n = min(len(ps), len(pc_surf))
for i in range(n):
    fv, vcnt, tcnt, base = pc_surf[i]
    layer_off, pfv, first, span, ptc, pbase = ps[i][:6]
    if tcnt != ptc: st['tri-count-differs'] += 1; continue
    ta = pc_idx[base:base + tcnt * 3] + fv
    tb = P['indices'][pbase:pbase + ptc * 3] + pfv
    if not np.allclose(pc_pos[ta], P['pos'][tb], atol=1e-3): st['positions-differ'] += 1; continue
    st['geometry-identical'] += 1
    # per-vertex attributes via the triangle correspondence
    pairs = {}
    for a, b in zip(ta, tb): pairs[int(b)] = int(a)
    for b, a in pairs.items():
        o = int(layer_off) + (b - int(pfv)) * 28
        rec = layers[o:o + 28].tobytes()
        col = np.frombuffer(rec[:4], np.uint8); f = struct.unpack('>4f', rec[4:20]); nrm, tan = struct.unpack('>II', rec[20:28])
        pcc = pc_col[a]; rgba = np.array([pcc[2], pcc[1], pcc[0], pcc[3]])
        col_d.append(np.abs(col.astype(int) - rgba.astype(int)).max())
        tex_d.append(np.abs(np.array(f[:2]) - pc_tex[a]).max()); lm_d.append(np.abs(np.array(f[2:]) - pc_lm[a]).max())
        nrm_d.append(np.abs(np.array(unpack_x11y11z10n(nrm)) - pc_unit(pc_nrm[a])).max())
        tan_d.append(np.abs(np.array(unpack_x11y11z10n(tan)) - pc_unit(pc_tan[a])).max())
print('surface verdicts:', dict(st))
def summ(nm, arr, thr):
    a = np.array(arr); print('  %-14s n=%d  max=%.4f  mean=%.5f  >%g: %d (%.2f%%)' % (nm, len(a), a.max(), a.mean(), thr, (a > thr).sum(), 100.0 * (a > thr).mean()))
summ('colour (0-255)', col_d, 1); summ('texcoord', tex_d, 1e-5); summ('lmapcoord', lm_d, 1e-5); summ('normal', nrm_d, 0.01); summ('tangent', tan_d, 0.01)

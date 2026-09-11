"""Export linked PS3 geometry to a compact, non-executable WPF scene format."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import pickle
import struct
import numpy as np
from entity_parser import parse_entities


class PlainMetadata(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError('Metadata may contain only plain values.')


def load_dump(path):
    path = Path(path)
    with np.load(path, allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    with open(str(path) + '.meta', 'rb') as f: meta = PlainMetadata(f).load()
    if not isinstance(meta, dict): raise ValueError('Invalid world metadata.')
    return arrays, meta


def export_scene(d, meta, output_dir, models=None, textures=None):
    pos = np.asarray(d['pos'], dtype=np.float64).reshape(-1, 3)
    indices = np.asarray(d['indices'], dtype=np.int64).reshape(-1)
    if not np.isfinite(pos).all(): raise ValueError('Geometry contains NaN/Infinity.')
    groups = {}
    mats = meta.get('materials', {})
    uv = np.zeros((len(pos), 2), dtype=np.float64)
    uv_missing = set()
    from worldview import surface_strides
    strides = surface_strides(np.asarray(d['surfaces']))
    layers = bytes(d.get('layers_raw', b''))
    for si, surface in enumerate(d['surfaces']):
        layer, fv, first, span, tc, base, mat = map(int, surface[:7])
        if tc == 0: continue
        if tc < 0 or base < 0 or base + tc * 3 > len(indices): raise ValueError('Surface index range is outside the map.')
        tri = indices[base:base + tc * 3] + fv
        if tri.min() < 0 or tri.max() >= len(pos): raise ValueError('Surface references invalid vertices.')
        name = str(mats.get(mat, mats.get(str(mat), 'material#%d' % mat)))
        stride = strides.get(si, 28 if not name.startswith('*') else 0)
        vertices = np.unique(tri)
        lo, hi = int(vertices[0]), int(vertices[-1])
        off = layer + (lo - fv) * stride + 4
        if stride < 12 or off < 0 or off + (hi-lo) * stride + 8 > len(layers):
            fallback=name+' [missing world UV]'
            uv_missing.add(fallback);groups.setdefault(fallback,[]).append(tri);continue
        values = np.ndarray((hi-lo+1, 2), dtype='>f4', buffer=layers, offset=off, strides=(stride, 4))[vertices-lo]
        if not np.isfinite(values).all():
            fallback=name+' [missing world UV]'
            uv_missing.add(fallback);groups.setdefault(fallback,[]).append(tri);continue
        uv[vertices] = values
        groups.setdefault(name, []).append(tri)
    # Static model geometry comes from the same linked emulated machine, not from PC sources.
    if models:
        from worldview import unpack_x11y11z10n
        by_header={m['hdr']:(name,m) for name,m in models.items()}
        positions=[pos];coordinates=[uv];offset=len(pos);missing=0
        for row in d['smodels']:
            _,ox,oy,oz,a0,a1,a2,scale,hdr=row[:9]
            found=by_header.get(int(hdr))
            if found is None:missing+=1;continue
            model_name,model=found
            axes=np.array([unpack_x11y11z10n(int(a)) for a in (a0,a1,a2)])
            _,count,first=model['lods'][0]
            for surface in model['surfaces'][first:first+count]:
                if not surface['vc'] or not surface['tc']:continue
                points=np.array([ox,oy,oz])+surface['pos']*scale@axes
                model_uv=surface.get('uv')
                name=surface.get('material','model/'+model_name)
                if model_uv is None:
                    # Keep untextured geometry separate so it cannot suppress a
                    # textured world/model group with the same material name.
                    name+=' [missing model UV]';model_uv=np.zeros((len(points),2));uv_missing.add(name)
                if model_uv.shape!=(len(points),2) or not np.isfinite(model_uv).all():raise ValueError('Invalid XModel UV array.')
                tri=surface['tri']
                if tri.min()<0 or tri.max()>=len(points):raise ValueError('Invalid XModel triangle.')
                positions.append(points);coordinates.append(model_uv)
                groups.setdefault(name,[]).append((tri+offset).reshape(-1));offset+=len(points)
        pos=np.concatenate(positions);uv=np.concatenate(coordinates)
    else: missing = len(d.get('smodels', []))
    if not groups: raise ValueError('The loaded world contains no renderable triangles.')
    mesh_path = Path(output_dir) / 'world.scene.bin'
    triangles = 0
    with mesh_path.open('wb') as f:
        f.write(b'IW3SCN2\0'); f.write(struct.pack('<I', len(groups)))
        for name, chunks in sorted(groups.items()):
            all_indices = np.concatenate(chunks)
            unique, inverse = np.unique(all_indices, return_inverse=True)
            local = np.asarray(pos[unique], dtype='<f4')
            if not np.isfinite(local).all(): raise ValueError('Vertex values exceed the preview range.')
            key = name.split('/', 1)[-1]; h = hashlib.sha256(key.encode('utf-8')).digest()
            color = bytes([64 + h[0] * 3 // 4, 64 + h[1] * 3 // 4, 64 + h[2] * 3 // 4, 255])
            f.write(color); f.write(struct.pack('<II', len(local), len(inverse)))
            f.write(local.tobytes()); f.write(np.asarray(uv[unique], dtype='<f4').tobytes())
            f.write(np.asarray(inverse, dtype='<u4').tobytes())
            triangles += len(inverse) // 3
    entities = parse_entities(meta.get('entity_string', ''))
    if not entities:
        # Legacy dumps may have only the old spawn list; retain it without inventing other entities.
        for i, (cn, origin, angles) in enumerate(meta.get('spawns', [])):
            entities.append(dict(id=i, classname=cn, targetname='', origin=list(origin), angles=list(angles), is_spawn=True,
                                 properties={'classname': cn, 'origin': ' '.join(map(str, origin))}))
    texture_rows = {str(i): textures[name] for i, name in enumerate(sorted(groups)) if textures and name in textures and name not in uv_missing}
    fx_file = 'fx-simulation.json' if (Path(output_dir) / 'fx-simulation.json').is_file() else None
    fx_placements_file = 'fx-placements.json' if (Path(output_dir) / 'fx-placements.json').is_file() else None
    # Sky groups render unlit in the viewer; match on the group's material
    # name (the '[missing ...]' fallback suffix does not change identity).
    sky_names = {str(x).lstrip(',').casefold() for x in meta.get('sky_material_names', ())}
    sky_groups = [i for i, name in enumerate(sorted(groups))
                  if name.split(' [', 1)[0].lstrip(',').casefold() in sky_names] if sky_names else []
    lighting = None
    sun = meta.get('sun')
    if sun and sun.get('color') and sun.get('direction'):
        color = [max(0.0, min(4.0, float(v))) for v in sun['color'][:3]]
        direction = [float(v) for v in sun['direction'][:3]]
        # The linked sun colour + a proportional ambient term; preview
        # lighting only, no zone bytes are derived from this.
        lighting = {'sun_color': color, 'sun_direction': direction,
                    'ambient': [min(1.0, 0.30 + 0.25 * v) for v in color]}
    scene = dict(schema='iw3-desktop-scene/v2', mesh_file=mesh_path.name, name=meta.get('name', ''), textures=texture_rows,
                 entities=entities, triangles=triangles, material_groups=len(groups),
                 fx_file=fx_file, fx_placements_file=fx_placements_file,
                 sky_groups=sky_groups, lighting=lighting,
                 static_models_missing=missing, mins=pos.min(axis=0).tolist(), maxs=pos.max(axis=0).tolist(),
                 rendering='%d material groups textured; RSX fragment programs are executed in software for material previews (see material-rsx-report.json). FX playback uses the deterministic preview simulation in fx-simulation.json.' % len(texture_rows))
    scene_path = Path(output_dir) / 'world.scene.json'
    scene_path.write_text(json.dumps(scene, indent=2, ensure_ascii=True), encoding='utf-8')
    return scene_path

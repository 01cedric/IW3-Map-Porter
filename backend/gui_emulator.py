"""Desktop orchestration of the supplied real-code loader/linker emulator."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

EMU = Path(__file__).resolve().parent / 'tools/ps3_loader_emulator'
SUPPORT_ORDER = ('code_post_gfx_mp', 'ui_mp', 'common_mp')


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while data := f.read(1 << 20): h.update(data)
    return h.hexdigest()


def zone_bytes(path):
    from cod4porter.backend.v4_ffio import read_ps3_fastfile
    path = Path(path)
    return read_ps3_fastfile(path).zone if path.suffix.lower() == '.ff' else path.read_bytes()


def validate_elf(path):
    path = Path(path)
    if not str(path).strip() or not path.is_file():
        raise ValueError('Select the decrypted PS3 EBOOT ELF first (elf_path is empty or not a file).')
    with path.open('rb') as f: header = f.read(64)
    if len(header) < 64 or header[:6] != b'\x7fELF\x02\x02' or struct.unpack_from('>H', header, 18)[0] != 21:
        raise ValueError('Expected a decrypted PS3 PPC64 big-endian EBOOT ELF, not a SELF/BIN file.')
    # The upstream emulator has a fixed ELF file/virtual-address profile.
    if path.stat().st_size < 0x7B0000 + 0x106780:
        raise ValueError('The ELF is too small for the existing emulator\'s CoD4 address profile.')
    phoff=struct.unpack_from('>Q',header,32)[0]
    entry_size,entry_count=struct.unpack_from('>HH',header,54)
    expected={(0,0x66D5B8,0x10000),(0x670000,0x5440C,0x680000),
              (0x6D0000,0xD5C80,0x10000000),(0x7B0000,0x106780,0x100E0000)}
    if entry_size<56 or entry_count>64 or phoff+entry_size*entry_count>path.stat().st_size:
        raise ValueError('Invalid ELF program-header table.')
    segments=set()
    with path.open('rb') as f:
        for i in range(entry_count):
            f.seek(phoff+i*entry_size);typ,flags,off,va,pa,size,memsize,alignment=struct.unpack('>IIQQQQQQ',f.read(56))
            if typ==1:segments.add((off,size,va))
        if not expected<=segments:
            raise ValueError('This ELF does not match the emulator\'s CoD4 segment/address profile.')
        for va,opcode in ((0xD6A50,0xF821FF81),(0xC54B0,0xF821FF81),(0xC56F0,0xF821FF71)):
            f.seek(va-0x10000)
            if f.read(4)!=struct.pack('>I',opcode):
                raise ValueError('CoD4 loader signature differs at 0x%X.'%va)
    os.environ['IW3_PS3_ELF'] = str(path.resolve())


def support_cache(s, emit):
    from dbworld import Machine
    folder = Path(s['support_directory'])
    sources = []
    for name in SUPPORT_ORDER:
        candidates = [folder / (name + '.ff'), folder / ('zone_' + name + '.bin')]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None: raise ValueError('Support zone is missing: ' + str(candidates[0]))
        sources.append((name, path))
    emit('stage', 'Identifying ELF and retail support zones')
    identity = {'elf': digest(s['elf_path']), 'zones': {name: digest(path) for name, path in sources},
                'emulator': {name: digest(EMU / name) for name in ('dbworld.py', 'zoneload.py', 'ppcemu.py', 'ps3elf.py', 'runtime_nop.txt', 'materialcheck.py')}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    local = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.cache')) / 'IW3MapPorter' / 'emulator-cache' / key
    local.mkdir(parents=True, exist_ok=True)
    snapshot = local / 'support_db.snapshot'
    if snapshot.is_file() and (local / 'identity.json').is_file():
        emit('log', 'Reusing matching support snapshot: ' + key[:12]); return local
    machine = Machine(real_link=True); machine.db_init()
    for i, (name, source) in enumerate(sources):
        emit('stage', 'Linking retail support (%d/3): %s' % (i + 1, name))
        data = zone_bytes(source)
        loader = machine.load_zone(data, name)
        emit('log', '%s: %d assets loaded' % (name, len(loader.assets)))
    temp = local / 'support_db.snapshot.tmp'
    machine.save(temp); temp.replace(snapshot)
    (local / 'identity.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    return local


def read_models(machine, required_headers=None):
    import numpy as np
    from worldview import Mem
    R = Mem(machine.m); models = {}
    for (typ, name), hdr in machine.inventory().items():
        if typ != 3 or (required_headers is not None and hdr not in required_headers): continue
        ns = R.u8(hdr + 6); surfs = R.u32(hdr + 0x20); handles = R.u32(hdr + 0x24)
        lods = [(R.f32(hdr + 0x28 + li * 0x18), R.u16(hdr + 0x2C + li * 0x18), R.u16(hdr + 0x2E + li * 0x18)) for li in range(4)]
        rows = []
        first, end = lods[0][2], lods[0][2] + lods[0][1]
        if first < 0 or end > ns: raise ValueError("Invalid XModel LOD surface range: " + name)
        for si in range(ns):
            if si < first or si >= end:
                rows.append(dict(vc=0, tc=0)); continue
            sp = surfs + si * 0x4C; vc = R.u16(sp + 2); tc = R.u16(sp + 4)
            tp = R.u32(sp + 8); vp = R.u32(sp + 0x18)
            if (vc and vp < 0x10000) or (tc and tp < 0x10000): raise ValueError('Invalid linked XModel pointer: ' + name)
            pos = np.frombuffer(machine.m.read(vp, vc * 16), '>f4').reshape(-1, 4)[:, :3].astype(np.float64)
            tri = np.frombuffer(machine.m.read(tp, tc * 6), '>u2').reshape(-1, 3).astype(np.int64)
            if len(tri) and (tri.min() < 0 or tri.max() >= vc): raise ValueError('XModel index is outside the vertex array: ' + name)
            secondary=R.u32(sp+0x24)
            uv=None
            if vc and secondary>=0x10000:
                raw=machine.m.read(secondary,vc*16)
                values=np.ndarray((vc,2),dtype='>f2',buffer=raw,offset=4,strides=(16,2)).astype(np.float64)
                if np.isfinite(values).all():uv=values
            mat=R.u32(handles+si*4) if handles>=0x10000 else 0
            mat_name=R.cstr(R.u32(mat)) if mat>=0x10000 else 'model/'+name+'/surface/'+str(si)
            rows.append(dict(vc=vc, tc=tc, pos=pos, tri=tri, uv=uv, material_header=mat, material=mat_name))
        models[name] = dict(hdr=hdr, lods=lods, surfaces=rows)
    return models


def export_linked(machine, name, out, settings, zone):
    import numpy as np
    from worldview import read_world, Mem
    from gui_scene import export_scene
    from gui_materials import export_materials
    bsp = 'maps/mp/%s.d3dbsp' % name
    gfx, error = machine.find(18, bsp)
    ents, ent_error = machine.find(17, bsp)
    if not gfx or error or not ents or ent_error: raise ValueError('Could not link GfxWorld/MapEnts.')
    world = read_world(machine.m, gfx); R = Mem(machine.m)
    text = R.cstr(R.u32(ents + 4), 4 << 20)
    d = dict(world)
    d['surfaces'] = np.asarray([s[:11] for s in world['surfaces']], dtype=np.int64).reshape(-1, 11)
    d['smodels'] = np.asarray([(v[0], *v[1], *v[2], v[3], v[4], v[5], v[6]) for v in world['smodels']], dtype=np.float64).reshape(-1, 11)
    models=read_models(machine,{int(row[4]) for row in world['smodels']})
    materials=dict(world['materials'])
    for model in models.values():
        for surface in model['surfaces']:
            if surface.get('material_header',0)>=0x10000:
                materials[surface['material_header']]=surface['material']
    textures = export_materials(machine, materials, settings, zone, out)
    try:
        from fx_simulation import export_fx_simulations
        by_material = {name.lstrip(','): filename for name, filename in textures.items()}
        export_fx_simulations(machine, out, textures_by_material=by_material)
    except Exception as exc:  # noqa: BLE001 - the scene stays usable without FX playback
        (Path(out) / 'fx-simulation.json').write_text(
            json.dumps({'schema': 'iw3-fx-simulation/v1', 'effects': [],
                        'errors': [{'error': str(exc)}]}), encoding='utf-8')
    try:
        from gui_fx_placements import export_fx_placements
        export_fx_placements(machine, name, out)
    except Exception:  # noqa: BLE001 - ambient placement is a preview extra
        pass
    sky_materials = sorted({
        str(world['materials'].get(world['surfaces'][i][6], '')).lstrip(',')
        for i in world.get('sky_surface_indices', ())
        if 0 <= i < len(world['surfaces'])
    } - {''})
    meta = dict(materials=world['materials'], name=world['name'], entity_string=text,
                sun=world.get('sun'), sky_material_names=sky_materials)
    return export_scene(d, meta, out, models, textures)


def linker_message(report, cached=False, preview=True):
    if report.get('status') not in (None, 'ok'):
        return 'Linker check failed: ' + str(report.get('error') or report['status'])
    message = ('Cached linked scene ready.' if cached else 'Linker completed; MapEnts navigation is ready.') if preview else 'Native map checks completed.'
    missing = report.get('externals_missing', [])
    if missing:
        message += ' %d native references use default assets.' % len(missing)
    scripts = report.get('script_validation', {})
    count = len(scripts.get('missing_script_references', []))
    assets = {(r['asset_type'], r['name'].casefold()) for r in scripts.get('missing_literal_assets', [])}
    if count or assets:
        message += ' Script scan: %d unresolved script/function references, %d unavailable asset names.' % (count, len(assets))
    if scripts.get('malformed_rawfiles'):
        message += ' %d malformed GSC rawfiles.' % len(scripts['malformed_rawfiles'])
    return message + ' Script execution and PS3 game startup remain unverified.'


def _execute(command, s, out, emit, artifact):
    sys.path.insert(0, str(EMU))
    if command == 'preview':
        from gui_scene import load_dump, export_scene
        emit('stage', 'Reading world dump and MapEnts for the 3D preview')
        d, meta = load_dump(s['world_dump'])
        scene = export_scene(d, meta, out); artifact(scene, 'scene')
        return 'unproven', 'World dump opened with material colors. RSX hardware is untested.'
    validate_elf(s['elf_path'])
    key = None
    if command == 'link':
        from gui_cache import key_for, restore
        emit('stage', 'Checking linked-scene cache and input hashes')
        key = key_for(s, Path(__file__).resolve().parent)
        if not s.get('force_scene_rebuild', False):
            cached = restore(key, out)
            if cached:
                cached_report = {}
                for name in sorted(cached['files']):
                    if name.endswith('.link.json'):
                        artifact(out / name, 'linker')
                        cached_report = json.loads((out / name).read_text(encoding='utf-8'))
                artifact(out / 'material-rsx-report.json', 'materials-rsx')
                artifact(out / 'world.scene.json', 'scene')
                (out / 'scene-cache.json').write_text(json.dumps(dict(hit=True, key=key, source_run=cached['source_run']), indent=2))
                artifact(out / 'scene-cache.json', 'cache')
                emit('log', 'Reused a verified cached scene; no map emulation or geometry export was repeated.')
                return 'unproven', linker_message(cached_report, cached=True)
        emit('log', 'No reusable scene selected. Building and caching a linked scene.')
    emit('stage', 'Decompressing PS3 fastfile')
    data = zone_bytes(s['ps3_ff'])
    if command == 'emulate':
        from zoneload import ZoneLoader
        emit('stage', 'Running PS3 loader code')
        loader = ZoneLoader(data); error = None
        try: loader.load()
        except Exception as exc: error = '%s: %s' % (type(exc).__name__, exc)
        expected = struct.unpack_from('>I', data, 0)[0] + 36 if len(data) >= 36 else -1
        blocks_fit = all(h <= size for h, size in zip(loader.high, loader.sizes))
        ok = error is None and loader.pos == expected and blocks_fit
        report = dict(ok=ok, error=error, assets=getattr(loader, 'assets', []), high=loader.high, sizes=loader.sizes,
                      pos=loader.pos, expected_stream_end=expected, length=len(data), blocks_fit=blocks_fit,
                      hardware_tested=False)
        path = out / 'loader.emu.json'; path.write_text(json.dumps(report, indent=2), encoding='utf-8'); artifact(path, 'emulator')
        return ('unproven', 'Loader code completed; stream end and block bounds match.') if ok else ('failed', 'Loader check failed: ' + str(error or 'stream/block bounds'))
    support = support_cache(s, emit)
    import linkcheck
    zone = out / (s['map_name'] + '.zone.bin'); zone.write_bytes(data)
    emit('stage', 'Linking map and checking engine lookups')
    from gui_inputs import load_companion
    companion=load_companion(s)
    link_args=['linkcheck',str(zone),s['map_name'],'--support',str(support)]
    if companion is not None:
        load_zone=out/(s['map_name']+'_load.zone.bin');load_zone.write_bytes(zone_bytes(companion))
        link_args+=['--load-zone',str(load_zone)]
        emit('log','Checking loading companion before the main map: '+companion.name)
    else:
        emit('log','No loading companion selected; this check covers the main fastfile only.')
    def on_linked_techsets(machine, result):
        emit('stage', 'Extracting linked TechniqueSets for RSX calibration and donor export')
        import techsetcheck
        r = techsetcheck.run(machine, out, source_label=s['map_name'])
        artifact(r['calibration'], 'rsx-calibration')
        artifact(r['donors'], 'techset-donors')
        artifact(r['donor_payloads'], 'techset-donors')
        emit('log', 'TechniqueSets extracted: %d; calibration %s; walk errors: %d.'
             % (r['techsets_extracted'],
                'PASSED' if r['calibration_passed'] else 'FAILED (see rsx-calibration.json)',
                r['walk_errors']))
    def on_linked(machine, result):
        emit('stage', 'Reading linked geometry, XModels and MapEnts for the 3D preview')
        scene = export_linked(machine, s['map_name'], out, s, data)
        artifact(out / 'material-rsx-report.json', 'materials-rsx')
        material_report=json.loads((out/'material-rsx-report.json').read_text())
        emit('log','Preview textures: %d materials textured, %d without diffuse pixels.' % (material_report['diffuse_textures_exported'],len(material_report['missing_diffuse_materials'])))
        if material_report['missing_diffuse_materials']:
            emit('log','Missing preview pixels: select the PC CoD4 main folder under Port > PC texture library, then rebuild the scene. Rebuild FastFiles separately to include those pixels on PS3.')
        artifact(scene, 'scene')
    hook = on_linked if command == 'link' else on_linked_techsets if command == 'techsets' else None
    rc = linkcheck.main(link_args, on_linked=hook)
    report = Path(str(zone) + '.link.json')
    result = {}
    if report.is_file():
        artifact(report, 'linker')
        result = json.loads(report.read_text(encoding='utf-8'))
    if rc == 0 and key and (out / 'world.scene.json').is_file():
        from gui_cache import store
        emit('stage', 'Saving linked scene for faster reopening')
        try: store(key, out, report)
        except (OSError, ValueError) as exc: emit('log', 'Scene cache was not saved: ' + str(exc))
    return ('unproven', linker_message(result, preview=command == 'link')) if rc == 0 else ('failed',
        linker_message(result) if result.get('status') not in (None, 'ok') else 'Linker check failed; see the report.')


def execute(command, s, out, emit, artifact):
    started = time.perf_counter(); phases = []; current = None; tick = started
    def tracked(kind, message='', **values):
        nonlocal current, tick
        if kind == 'stage':
            now = time.perf_counter()
            if current is not None: phases.append(dict(stage=current, seconds=round(now-tick, 4)))
            current = message; tick = now
        emit(kind, message, **values)
    try:
        return _execute(command, s, out, tracked, artifact)
    finally:
        now = time.perf_counter()
        if current is not None: phases.append(dict(stage=current, seconds=round(now-tick, 4)))
        path = out / 'preview-timings.json'
        path.write_text(json.dumps(dict(command=command, total_seconds=round(now-started, 4), phases=phases), indent=2))
        artifact(path, 'timings')

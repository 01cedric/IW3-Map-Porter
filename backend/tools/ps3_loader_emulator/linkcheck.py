#!/usr/bin/env python3
"""Link-phase check: load the always-loaded PS3 zones and a map zone into one emulated
asset database, running the game's own DB_LinkXAssetEntry, then ask the game's own
DB_FindXAssetHeader for what the engine looks up after a map load.

    python3 linkcheck.py <map_zone.bin> <mapname> [--support DIR] [--no-world] [--no-snapshot] [-v]

Reports the engine's Com_Error text on failure, every ",name" external that resolved to a
default asset, asset pool usage against the PS3 pool limits, and the post-load lookups.
"""
import os, sys, json, struct, collections, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dbworld import Machine, ComError, MemFault, NUM_TYPES
from ppcemu import UnknownCall, MASK32
from zoneload import TYPE_NAMES

SUPPORT_ORDER = ('code_post_gfx_mp', 'ui_mp', 'common_mp')


def load_support(M, support_dir, log):
    for name in SUPPORT_ORDER:
        path = os.path.join(support_dir, 'zone_%s.bin' % name)
        z = open(path, 'rb').read()
        t = time.time()
        zl = M.load_zone(z, name)
        log('support zone %-18s %5d assets  %6.1fs' % (name, len(zl.assets), time.time() - t))


def main(argv, *, on_linked=None):
    if len(argv) < 3:
        print(__doc__); return 2
    map_zone, mapname = argv[1], argv[2]
    support_dir = os.path.dirname(os.path.abspath(__file__))
    if '--support' in argv:
        support_dir = argv[argv.index('--support') + 1]
    verbose = '-v' in argv
    out = {'map': mapname, 'zone': map_zone,
           'scope': 'asset-stream-database-and-material-registration', 'hardware_tested': False,
           'gpu_shader_execution': False, 'game_startup_executed': False}
    def log(msg):
        print(msg); sys.stdout.flush()

    M = Machine(real_link=True, fault_low=True, verbose=verbose)
    snap = os.path.join(support_dir, 'support_db.snapshot')
    if os.path.exists(snap) and '--no-snapshot' not in argv:
        t = time.time(); M.restore(snap)
        log('restored always-loaded zones from snapshot (%s) in %.1fs: %s'
            % (os.path.basename(snap), time.time() - t, ', '.join('%s=%d' % (n, c) for n, c, _b, _s in M.zone_summaries)))
    else:
        M.db_init()
        try:
            load_support(M, support_dir, log)
        except ComError as e:
            log('support zone Com_Error: %s' % e); return 1
    n_lookups0 = len(M.lookups); n_clones0 = len(M.default_clones); n_replaced0 = len(M.default_replaced)
    n_log0 = len(M.com_log)
    n_materials0 = M.material_registration_calls
    inv_support = M.inventory()
    usage_support = collections.Counter(t for (t, _n) in inv_support)
    log('support inventory: %d assets' % len(inv_support))

    if '--load-zone' in argv:
        load_path=argv[argv.index('--load-zone')+1]
        try:
            load_bytes=open(load_path,'rb').read();started=time.perf_counter()
            companion=M.load_zone(load_bytes,mapname+'_load')
            expected=struct.unpack_from('>I',load_bytes)[0]+36
            valid=companion.pos==expected and all(h<=s for h,s in zip(companion.high,companion.sizes))
            out['loading_companion']={'path':load_path,'assets':len(companion.assets),
                'seconds':time.perf_counter()-started,'pos':companion.pos,'expected':expected,
                'high':companion.high,'sizes':companion.sizes,'passed':valid}
            if not valid:raise ValueError('Loading companion stream/block bounds differ')
            log('loading companion: %d assets, stream and block bounds match'%len(companion.assets))
        except Exception as exc:
            out.update(status='loading_companion_error',error=str(exc))
            json.dump(out,open(map_zone+'.link.json','w'),indent=1)
            log('loading companion failed: '+str(exc));return 1

    # ---------------------------------------------------------------- map zone
    z = open(map_zone, 'rb').read()
    t = time.time()
    status = 'ok'; error = None
    try:
        zl = M.load_zone(z, mapname)
        log('map zone %-26s %5d assets  %6.1fs' % (mapname, len(zl.assets), time.time() - t))
    except ComError as e:
        status = 'com_error'; error = str(e)
        zl = M.current
        log('map zone Com_Error after %d assets: %s' % (len(zl.assets), e))
    except MemFault as e:
        status = 'fault'; error = str(e)
        zl = M.current
        log('map zone MEMORY FAULT after %d assets: %s  (pc history %s)'
            % (len(zl.assets), e, [hex(a) for a, _ in M.cpu.calls[-4:]]))
    except (UnknownCall, EOFError, RuntimeError) as e:
        status = 'emulation_error'; error = repr(e)
        zl = M.current
        log('map zone emulation stopped after %d assets: %r' % (len(zl.assets), e))
    out['status'] = status; out['error'] = error
    out['material_registration'] = {'native': True,
        'calls': M.material_registration_calls - n_materials0,
        'constant_guard': True, 'checksum_implementation': 'verified native CRC-32 helper'}
    out['assets_loaded'] = len(zl.assets)
    expected=struct.unpack_from('>I',z)[0]+36
    stream_ok=zl.pos==expected and all(h<=s for h,s in zip(zl.high,zl.sizes))
    out['stream']={'pos':zl.pos,'expected':expected,'high':zl.high,'sizes':zl.sizes,'passed':stream_ok}
    if status=='ok' and not stream_ok:
        status='stream_error';out.update(status=status,error='Main stream/block bounds differ')

    inv_all = M.inventory()
    if status == 'ok':
        from physicscheck import validate_physics_sound_prefixes
        try:
            out['physics_sound_validation'] = validate_physics_sound_prefixes(M.m, inv_all)
            log('Physics sound prefixes: %d checked, %d empty strings' %
                (out['physics_sound_validation']['checked'], len(out['physics_sound_validation']['empty_prefixes'])))
        except RuntimeError as exc:
            status = 'physics_sound_error'
            out.update(status=status, error=str(exc), physics_sound_validation={'passed':False, 'error':str(exc)})
            log('Physics sound validation failed: ' + str(exc))
    usage_all = collections.Counter(t for (t, _n) in inv_all)

    # ------------------------------------------------------- pool usage / limits
    rows = []
    for t in range(NUM_TYPES):
        cap = M.pool_sizes[t]
        if not cap: continue
        used = usage_all.get(t, 0); before = usage_support.get(t, 0)
        rows.append({'type': t, 'name': M.type_names[t], 'capacity': cap,
                     'support': before, 'map': used - before, 'total': used,
                     'free': cap - used})
    out['pools'] = rows
    log('\nasset pools (PS3 capacity / always-loaded / this map / free):')
    for r in rows:
        if r['map'] or r['total'] > 0.8 * r['capacity']:
            flag = '  <-- FULL' if r['free'] <= 0 else ('  <-- tight' if r['free'] < 0.1 * r['capacity'] else '')
            log('   %-16s %6d / %6d / %6d / %6d%s' % (r['name'], r['capacity'], r['support'], r['map'], r['free'], flag))

    # ---------------------------------------------------- externals in the map zone
    # The engine itself tells us which ",name" references it could not satisfy: every
    # DB_CreateDefaultEntry call is an external that no loaded zone provides.  A clone
    # that a real asset of this zone replaces later (forward reference) is harmless; one
    # that survives the load is the type's default asset at runtime.
    clones = M.default_clones[n_clones0:]
    replaced = set(M.default_replaced[n_replaced0:])
    survivors = [(t, n) for t, n, _z in clones if (t, n) not in replaced]
    out['default_clones'] = [(M.type_names[t], n) for t, n, _z in clones]
    out['forward_refs_resolved'] = [(M.type_names[t], n) for t, n in replaced]
    out['externals_missing'] = [(M.type_names[t], n, M.default_names[t]) for t, n in survivors]
    out['dependency_closure'] = {'complete': not survivors, 'default_asset_count': len(survivors),
                                'counts_by_type': dict(collections.Counter(M.type_names[t] for t, _ in survivors))}
    log('\nexternal references the always-loaded zones do not provide: %d (%d of them defined later in this zone)'
        % (len(clones), len(replaced)))
    if survivors:
        log('externals that stay DEFAULT assets at runtime:')
        for t, n in survivors:
            log('   %-10s %-48s -> default %r' % (M.type_names[t], n, M.default_names[t]))
    else:
        log('every external reference resolves (always-loaded zones or this zone)')

    # ------------------------------------------------------ redundant assets
    # Assets of this zone whose (type, name) already existed: the engine keeps the copy of
    # the zone with the higher priority (common_mp beats a map zone), so these bytes are
    # dead weight in the map fastfile, never a failure.
    red = getattr(zl, 'redundant', [])
    out['redundant'] = [(M.type_names[t], n) for t, n in red]
    by_type = collections.Counter(M.type_names[t] for t, _n in red)
    log('\nredundant assets (already provided by an always-loaded zone): %d %s'
        % (len(red), dict(by_type) if red else ''))
    for t, n in red[:8]: log('   %-10s %s' % (M.type_names[t], n))
    if len(red) > 8: log('   ...')

    # -------------------------------------------------------- post-load lookups
    bsp = 'maps/mp/%s.d3dbsp' % mapname
    lookups = [(13, bsp, 'CM_LoadMap'), (14, bsp, 'Com_LoadWorld'), (18, bsp, 'R_LoadWorld'),
               (16, bsp, 'GameWorldMp')]
    if '--no-world' in argv: lookups = []      # a load zone / non-map zone
    res = []
    log('\npost-load engine lookups:')
    for typ, name, who in lookups:
        hdr, err = M.find(typ, name)
        ok = hdr is not None and hdr != 0 and err is None
        res.append({'type': M.type_names[typ], 'name': name, 'caller': who, 'ok': ok,
                    'header': hdr, 'error': err})
        log('   %-14s %-9s %-34s %s' % (who, M.type_names[typ], name, 'OK' if ok else 'FAILED: %s' % err))
    out['lookups'] = res
    if status == 'ok' and not all(r['ok'] for r in res):
        status = 'world_lookup_error'
        out.update(status=status, error='One or more required world assets could not be resolved')

    if status == 'ok' and lookups:
        from worldcheck import validate_world
        ranges = [(base, size) for z in M.zones for base, size in zip(z.block_base, z.sizes)]
        ranges += [(base, size) for _, _, bases, sizes in getattr(M, 'zone_summaries', [])
                   for base, size in zip(bases, sizes)]
        try:
            out['world_validation'] = validate_world(M.m, res[2]['header'], inv_all, ranges)
            log('linked world: geometry bounds and registered references match')
        except RuntimeError as exc:
            status = 'world_validation_error'
            out.update(status=status, error=str(exc), world_validation={'passed': False, 'error': str(exc)})
            log('linked world validation failed: ' + str(exc))

    if status == 'ok' and lookups:
        from scriptcheck import inspect_linked_scripts
        out['script_validation'] = inspect_linked_scripts(M, inv_all, mapname)
        scripts = out['script_validation']
        log('static script check: %d unresolved script/function references, %d unavailable literal asset requests; '
            'scripts were not compiled or executed' %
            (len(scripts['missing_script_references']), len(scripts['missing_literal_assets'])))

    # ------------------------------------------------- material -> techset binding
    # After linking, every Material.techniqueSet must point at a real technique set (one
    # that common_mp registered), never at the map zone's own ",name" shell.
    techsets = {hdr for (t, n), hdr in inv_all.items() if t == 7}
    bad = 0; checked = 0
    for (t, n), hdr in inv_all.items():
        if t != 4: continue
        ts = M.m.u32(hdr + 0x70)
        checked += 1
        if ts not in techsets:
            bad += 1
            if bad <= 5: log('   material %r techniqueSet %#x is not a registered technique set' % (n, ts))
    out['material_techset_bindings'] = {'checked': checked, 'unbound': bad}
    if status == 'ok' and bad:
        status = 'material_binding_error'
        out.update(status=status, error=f'{bad} material TechniqueSet pointers are not registered')
    out['shader_conversion'] = 'none; materials reference existing PS3 technique sets'
    log('\nmaterial -> techniqueSet bindings: %d checked, %d unbound' % (checked, bad))

    # --------------------------------------------- lookups the loader itself made
    made = M.lookups[n_lookups0:]
    normalized_inventory = {(t, n.casefold()) for t, n in inv_all}
    missing_lookups = [(hex(lr), M.type_names[t] if t < NUM_TYPES else t, n) for lr, t, n in made
                       if (t, n.casefold()) not in normalized_inventory]
    out['loader_lookups'] = len(made); out['loader_lookups_missing'] = missing_lookups
    out['loader_lookup_names'] = [(hex(lr), M.type_names[t] if t < NUM_TYPES else t, n) for lr, t, n in made]
    log('\nDB_FindXAssetHeader calls made while loading the map: %d, unresolved: %d' % (len(made), len(missing_lookups)))
    for lr, t, n in made[:16]:
        log('   %-10s %-40r %s' % (M.type_names[t] if t < NUM_TYPES else t, n, 'MISSING' if (t, n.casefold()) not in normalized_inventory else 'found'))
    if len(made) > 16: log('   ...')

    # ------------------------------------------------------------------ log
    prints = [m for k, c, m in M.com_log[n_log0:] if k == 'PRINT']
    out['com_printf'] = prints[-50:]
    if prints:
        log('\nengine output during load (%d lines, last 12):' % len(prints))
        for m in prints[-12:]: log('   ' + m.rstrip())
    json.dump(out, open(map_zone + '.link.json', 'w'), indent=1)
    log('\nresult: %s' % status.upper())
    # Desktop export reuses this exact linked machine; no second map load required.
    if on_linked is not None and status == 'ok' and all(r['ok'] for r in res):
        on_linked(M, out)
    return 0 if status == 'ok' and all(r['ok'] for r in res) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

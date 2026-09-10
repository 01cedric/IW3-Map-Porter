"""JSON-lines desktop adapter. Conversion algorithms remain in cod4porter.

Every request has its own output directory. A terminal event is emitted on every
handled outcome; process exit alone is never treated as a successful conversion.
"""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT.parent / 'VERSION').read_text(encoding='utf-8').strip()
sys.path.insert(0, str(ROOT))
WIRE = sys.stdout


def emit(kind, message='', **values):
    WIRE.write(json.dumps(dict(type=kind, message=message, **values), ensure_ascii=True, default=str) + '\n')
    WIRE.flush()


class LogStream(io.TextIOBase):
    def __init__(self): self.pending = ''
    def writable(self): return True
    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line: emit('log', line.rstrip('\r'))
        return len(text)
    def flush(self):
        if self.pending: emit('log', self.pending); self.pending = ''


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=True, default=str), encoding='utf-8')
    return str(Path(path).resolve())


def artifact(path, category='report'):
    emit('artifact', Path(path).name, path=str(Path(path).resolve()), category=category)


def must_file(path, label):
    if not path or not Path(path).is_file(): raise ValueError(label + ' is missing: ' + str(path or ''))
    return str(Path(path).resolve())


def automatic_budget(s):
    # New desktop profiles serialize this switch, including profiles loaded
    # from older versions. Explicit legacy numeric/off API requests retain
    # their meaning; requests with no budget choice use automatic mode.
    value=s.get('automatic_zone_budget', 'zone_budget_bytes' not in s)
    if not isinstance(value,bool):raise ValueError('Automatic zone budget must be true or false.')
    return value


def budget_of(s):
    if automatic_budget(s):
        from cod4porter.ps3_budget import DEFAULT_ZONE_BUDGET_BYTES
        return DEFAULT_ZONE_BUDGET_BYTES
    value = str(s.get('zone_budget_bytes', 'off')).strip().lower()
    if value == 'off': return None
    budget = int(value)
    if budget < 1: raise ValueError('Zone budget must be positive or off.')
    return budget


def common_options(s):
    choices = {
        'gfxworld_resource_policy': ('fastfile-delayed', 'self-contained'),
        'material_image_resource_policy': ('fastfile-delayed', 'balanced-split', 'self-contained'),
        'unresolved_image_policy': ('reference', 'neutral'),
        'unresolved_material_policy': ('reference', 'default'),
        'primary_light_policy': ('source', 'sun-only'),
        'vertex_layer_policy': ('source', 'omit'),
        'portal_policy': ('source', 'omit'),
    }
    options = {}
    for key, allowed in choices.items():
        value = s.get(key, allowed[0])
        if value not in allowed: raise ValueError('Invalid option: ' + key)
        options[key] = value
    options['texture_library_directory']=s.get('texture_library_directory') or None
    options.update(runtime_compatible=bool(s.get('runtime_compatible', True)),
                   boot_isolation=bool(s.get('boot_isolation', False)), sound_policy='omit')
    return options


def validate_input(s, command):
    name = s.get('map_name', '')
    if not re.fullmatch(r'mp_[a-z0-9_]+', name): raise ValueError('Invalid map name: ' + name)
    must_file(s.get('pc_ff'), 'PC main fastfile')
    for path in s.get('iwd_paths', []): must_file(path, 'IWD')
    if command == 'port':
        if s.get('require_load_file', True) or s.get('pc_load_ff'):
            must_file(s.get('pc_load_ff'), 'PC-_load.ff')
        if s.get('ps3_load_reference'):
            must_file(s['ps3_load_reference'], 'PS3 loading-zone reference')
            if not s.get('pc_load_ff'): raise ValueError('The reference requires a PC _load.ff.')
        if int(s.get('image_budget_min_dimension', 32)) < 1 or int(s.get('texture_max_edge', 0)) < 0:
            raise ValueError('Invalid texture limits.')
    return name


def verification_failures(structural, preflight, preflight_rc):
    """Keep the two independent audit verdicts and their actionable failures."""
    failures = []
    for stage, rows in (
        ('Structural audit', structural.get('structural_failures', [])),
        ('PS3 preflight', [r for r in preflight.get('checks', []) if r.get('passed') is False]),
    ):
        for row in rows:
            failures.append(dict(stage=stage,
                check=row.get('check_id', row.get('key', 'unknown')),
                actual=row.get('observed', row.get('actual')),
                expected=row.get('expected'), detail=row.get('detail', '')))
    for stage, failed in (('Structural audit', not structural.get('structural_passed')),
                          ('PS3 preflight', preflight_rc != 0)):
        if failed and not any(r['stage'] == stage for r in failures):
            failures.append(dict(stage=stage, check='REPORT_UNAVAILABLE', actual=None,
                expected='failure details', detail='The stage failed without check details. See its report and log.'))
    return failures


def native_verification(name, main, load, s, out, blocked=False):
    """Automatically check the actual output pair, never a previous GUI selection."""
    from gui_emulator import execute, SUPPORT_ORDER
    coverage = dict(map=name, status='not_checked', hardware_tested=False,
        game_startup_tested=False, script_execution_tested=False,
        scope='native asset linking, material contracts, physics sound prefixes, world bounds and static script dependencies')
    missing = []
    if not s.get('elf_path') or not Path(s['elf_path']).is_file():
        missing.append('matching decrypted target ELF')
    folder = Path(s['support_directory']) if s.get('support_directory') else None
    for zone in SUPPORT_ORDER:
        if folder is None or not any((folder / candidate).is_file() for candidate in (zone + '.ff', 'zone_' + zone + '.bin')):
            missing.append('retail support zone ' + zone)
    if blocked:
        message = 'Native and static script checks skipped because structural audit or preflight failed.'
        coverage['reason'] = 'blocked_by_failed_audit'
        status = 'failed'
    elif missing:
        message = 'Native and static script checks skipped: configure ' + ', '.join(missing) + ' in Emulator & Linker.'
        coverage.update(reason='missing_prerequisites', missing=missing)
        status = 'unproven'
    else:
        settings = dict(s, map_name=name, ps3_ff=str(main), ps3_load_ff=str(load) if load else '')
        emit('stage', 'Automatically checking native linking and script dependencies')
        try:
            status, message = execute('validate', settings, out, emit, artifact)
            coverage.update(status='failed' if status == 'failed' else 'completed', message=message,
                            report=name + '.zone.bin.link.json')
        except Exception as exc:
            status = 'failed'; message = 'Automatic native checks failed: ' + str(exc)
            coverage.update(status='failed', error=str(exc))
    path = out / (name + '.check-coverage.json')
    save(path, coverage); artifact(path, 'diagnostic'); emit('log', message)
    return status, message


def verify(report_path, s, out):
    from tools.c11_audit import audit
    from tools.ps3_preflight import main as preflight
    report_path = Path(must_file(report_path, 'Port report'))
    doc = json.loads(report_path.read_text(encoding='utf-8-sig'))
    name = doc.get('map', '')
    if not re.fullmatch(r'mp_[a-z0-9_]+', name): raise ValueError('Invalid map name in the report.')
    # Copies moved to another machine are audited against the files alongside the report.
    main = report_path.parent / (name + '.ff')
    load = report_path.parent / (name + '_load.ff')
    must_file(main, 'PS3 main fastfile')
    if doc.get('load') is not None: must_file(load, 'PS3-_load.ff')
    emit('stage', 'Saved fastfiles: structural audit')
    result = audit(report_path, main, load if doc.get('load') is not None else None)
    path = out / (name + '.structural-audit.json')
    save(path, result); artifact(path, 'audit')
    fallback_rows = doc.get('assembly_diagnostics', {}).get('fx_visual_fallbacks', [])
    for detail in (doc.get('load') or {}).get('full_fidelity_blockers', []):
        emit('log', 'Loading-screen fidelity warning: '+detail)
    for row in fallback_rows:
        emit('log', 'FX fidelity warning: {effect}, element {element}, visual {visual}, '
             '{source_pointer} uses {replacement}.'.format(**row))
    emit('log', 'Structural checks: %s/%s' % (result['structural_passed_count'], result['structural_total']))
    emit('stage', 'PS3 preflight: blocks, budget and references')
    preflight_path = out / (name + '.ps3-preflight.json')
    args = [str(main)]
    if doc.get('load') is not None: args.append(str(load))
    args += ['--report', str(report_path), '--ps3-zone-budget', str(budget_of(s) or 'off'), '--out', str(preflight_path)]
    for support in s.get('support_zones', []): args += ['--support-zone', must_file(support, 'Support zone')]
    rc = preflight(args)
    preflight_doc = {}
    if preflight_path.is_file():
        artifact(preflight_path, 'preflight')
        preflight_doc = json.loads(preflight_path.read_text(encoding='utf-8'))
    if not preflight_doc.get('ps3_native_dependencies', {}).get('support_zones'):
        emit('log', 'Native dependency availability was not checked: no target support zones were supplied.')
    if not result['structural_passed'] or rc != 0:
        native_verification(name, main, load if doc.get('load') is not None else None, s, out, blocked=True)
        failures = verification_failures(result, preflight_doc, rc)
        failure_path = out / (name + '.verification-failures.json')
        save(failure_path, {'structural_passed': result['structural_passed'],
            'preflight_passed': rc == 0, 'failures': failures})
        artifact(failure_path, 'diagnostic')
        for row in failures:
            emit('log', '{stage} FAILED: {check} — {detail}; actual={actual}; expected={expected}'.format(**row))
        labels = [row['stage'] + ': ' + row['check'] for row in failures]
        message = 'Verification failed. ' + '; '.join(labels[:4])
        if len(labels) > 4: message += '; and %d more' % (len(labels) - 4)
        return 'failed', message + '. See ' + failure_path.name + '.'
    native_status, native_message = native_verification(name, main,
        load if doc.get('load') is not None else None, s, out)
    if native_status == 'failed': return native_status, native_message
    fidelity = bool(doc.get('full_fidelity_passed') and result['full_fidelity_passed'])
    if s.get('require_full_fidelity') and not fidelity:
        return 'failed', 'Structural checks passed; the requested full-fidelity check failed.'
    fidelity_message = '' if fidelity else ' Full fidelity is incomplete; see the structural audit fidelity findings.'
    return 'unproven', 'Structural audit and preflight passed. ' + native_message + fidelity_message


def run(request):
    if request.get('schema_version') != 1: raise ValueError('Unknown request version.')
    if sys.version_info < (3, 11): raise ValueError('Python 3.11 or newer is required.')
    command = request['command']; s = request.get('settings', {})
    out = Path(request['output_dir']).resolve(); out.mkdir(parents=True, exist_ok=True)
    emit('log', 'Python %s | IW3MapPorter Version %s | %s' % (sys.version.split()[0], VERSION, command))
    if command == 'doctor':
        import cod4porter.porter
        import numpy
        import PIL
        result = {'python': sys.version, 'executable': sys.executable, 'backend': str(ROOT),
                  'numpy': numpy.__version__, 'pillow': PIL.__version__, 'sound_policy': 'omit',
                  'elf_available': bool(s.get('elf_path') and Path(s['elf_path']).is_file()),
                  'note': 'The emulator requires a PS3 EBOOT ELF matching its fixed address profile.'}
        artifact(save(out / 'environment.json', result))
        return 'passed', 'Python, porter and preview dependencies are available.'
    if command == 'tests':
        emit('stage', 'Porter self-tests')
        completed = subprocess.Popen([sys.executable, '-u', str(ROOT / 'tools/run_tests.py'), '--timeout', '120'],
                                     cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace')
        lines = []
        for line in completed.stdout:
            lines.append(line); emit('log', line.rstrip())
        rc = completed.wait()
        (out / 'backend-tests.log').write_text(''.join(lines), encoding='utf-8')
        artifact(out / 'backend-tests.log', 'log')
        return ('passed', 'Self-tests completed; see the log for skipped fixture tests.') if rc == 0 else ('failed', 'Self-tests failed.')
    if command=='mapmenu-picture':
        from gui_menu_picture import execute
        return execute(s,out,emit,artifact)
    if command in ('mapmenu-inspect', 'mapmenu-write'):
        from gui_mapmenu import execute
        return execute(command, s, out, emit, artifact)
    if command in ('emulate', 'link', 'preview'):
        from gui_emulator import execute
        return execute(command, s, out, emit, artifact)
    if command == 'verify': return verify(s.get('report_path'), s, out)
    if command not in ('port', 'analyze'): raise ValueError('Unknown job: ' + command)
    name = validate_input(s, command); options = common_options(s)
    emit('stage', 'Reading PC assets and building the PS3 asset graph')
    if command == 'analyze':
        from cod4porter.assembler import analyze_and_assemble
        assembly = analyze_and_assemble(s['pc_ff'], name, s.get('iwd_paths', []), **options)
        path = out / (name + '.analysis.json')
        save(path, {'map': name, 'source': dict(assembly.source.diagnostics), 'assembly': dict(assembly.diagnostics),
                    'note': 'Analysis is not a build or hardware test.'}); artifact(path, 'analysis')
        return 'passed', 'Source analysis completed. No fastfiles were generated.'
    from cod4porter.porter import port_map
    chosen_budget=budget_of(s)
    if automatic_budget(s):
        emit('log', f'Automatic PS3 zone budget: {chosen_budget} bytes from the largest measured retail profile. '
             'This is not a measurement of free console memory. Texture reduction is applied only when needed to fit the budget or an explicit edge limit.')
    cube = json.loads(Path(must_file(s['cubemap_evidence'], 'Cubemap evidence')).read_text(encoding='utf-8-sig')) if s.get('cubemap_evidence') else None
    emit('log', 'Conversion is running. The core does not provide reliable progress percentages; the current stage and elapsed time remain visible.')
    result = port_map(s['pc_ff'], name, s.get('iwd_paths', []), out,
                      pc_load_ff=s.get('pc_load_ff') or None, retail_load_reference=s.get('ps3_load_reference') or None,
                      cubemap_target_face_hashes=cube, zone_budget_bytes=chosen_budget,
                      texture_max_edge=int(s.get('texture_max_edge', 0)),
                      image_budget_min_dimension=int(s.get('image_budget_min_dimension', 32)), **options)
    artifact(result['report_path'], 'port-report'); artifact(result['main_output'], 'fastfile')
    image_plan=result.get('ps3_zone_budget_plan') or {}
    if chosen_budget is not None:
        emit('log', f"Zone budget result: {result.get('ps3_zone_allocation',{}).get('declared_zone_bytes','unknown')} / {chosen_budget} bytes; "
             f"{image_plan.get('scaled_images',0)} textures reduced.")
    pixel_report = out / (name + '.source-pixels.json')
    if pixel_report.is_file():
        artifact(pixel_report, 'image-coverage')
        coverage = result.get('source_pixel_coverage', {})
        missing = coverage.get('images_without_source_pixels', 0)
        if missing:
            emit('log', f'{missing} image names have no supplied source pixels. See {pixel_report.name} for affected materials and check target support-zone availability.')
    load = out / (name + '_load.ff')
    if load.is_file(): artifact(load, 'fastfile')
    status, message = verify(result['report_path'], s, out)
    coverage = result.get('source_pixel_coverage', {})
    missing_color = len(coverage.get('missing_diffuse_images', ()))
    unresolved_slots = len(coverage.get('synthetic_neutral_bindings', ()))
    if missing_color or unresolved_slots:
        message += (f' Texture coverage: {missing_color} color images lack supplied pixels; '
                    f'{unresolved_slots} slots have unresolved image identities. '
                    'See the source-pixels report before console testing.')
    return status, message


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--request', required=True)
    args = parser.parse_args(); stream = LogStream(); request = None
    status = 'failed'; message = 'Job failed.'
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8-sig'))
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status, message = run(request)
    except (Exception, SystemExit) as exc:
        message = '%s: %s' % (type(exc).__name__, exc)
        emit('log', traceback.format_exc())
        if request and request.get('output_dir'):
            out = Path(request['output_dir'])
            if out.is_dir():
                for path in sorted(out.glob('*.json')):
                    if path.name != 'request.json': artifact(path, 'diagnostic')
    finally:
        stream.flush()
        summary = dict(status=status, message=message, hardware_tested=False)
        if request and request.get('output_dir'):
            out = Path(request['output_dir'])
            if out.is_dir():
                try: artifact(save(out / 'job-summary.json', summary))
                except OSError as exc: emit('log', 'Could not save the completion report: ' + str(exc))
        emit('result', message, status=status)
    return 2 if status == 'failed' else 0


if __name__ == '__main__': raise SystemExit(main())

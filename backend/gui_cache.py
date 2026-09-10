"""Content-addressed, atomic cache of successfully linked desktop scenes."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def cache_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.cache')) / 'IW3MapPorter' / 'scene-cache'


def key_for(settings, backend):
    files = {'map': Path(settings['ps3_ff']), 'elf': Path(settings['elf_path'])}
    from gui_inputs import load_companion
    companion=load_companion(settings)
    if companion is not None:files['load_companion']=companion
    folder = Path(settings['support_directory'])
    for name in ('code_post_gfx_mp', 'ui_mp', 'common_mp'):
        files[name] = next((p for p in (folder / (name + '.ff'), folder / ('zone_' + name + '.bin')) if p.is_file()), folder / (name + '.ff'))
    ff = files['map']; report = ff.with_name(ff.stem + '.python-port-report.json')
    if settings.get('report_path') and Path(settings['report_path']).is_file(): report = Path(settings['report_path'])
    if report.is_file(): files['material_report'] = report
    # Selected IWDs can supply diffuse previews for native/model images whose
    # generated PS3 pixels are unavailable. Content changes must invalidate a scene.
    for index,p in enumerate(settings.get('iwd_paths', [])):
        if Path(p).is_file():files['iwd/'+str(index)]=Path(p)
    if settings.get('texture_library_directory'):
        from cod4porter.texture_library import library_archives
        for index,p in enumerate(library_archives(settings['texture_library_directory'])):
            files['texture_library/'+str(index)]=p
    backend = Path(backend)
    for p in sorted(backend.rglob('*.py')):
        if not {'tests', 'gui_tests', '__pycache__'} & set(p.relative_to(backend).parts): files['code/' + p.relative_to(backend).as_posix()] = p
    nop = backend / 'tools/ps3_loader_emulator/runtime_nop.txt'
    if nop.is_file(): files['runtime_nop'] = nop
    import numpy, PIL
    identity = {'schema': 1, 'map_name': settings['map_name'], 'format': ff.suffix.lower(),
                'numpy': numpy.__version__, 'pillow': PIL.__version__, 'files': {k: digest(v) for k, v in files.items()}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def safe_file(folder, name):
    if not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError('Invalid scene cache filename.')
    p = Path(folder) / name
    if p.is_symlink(): raise ValueError('Scene cache links are not allowed.')
    return p


def restore(key, output):
    entry = cache_root() / key
    try:
        manifest = json.loads((entry / 'manifest.json').read_text())
        if manifest['key'] != key: return None
        scene = json.loads(safe_file(entry, 'world.scene.json').read_text())
        required = {'world.scene.json', scene['mesh_file'], 'material-rsx-report.json', *scene.get('textures', {}).values()}
        if not required <= manifest['files'].keys(): return None
        for name, expected in manifest['files'].items():
            if digest(safe_file(entry, name)) != expected: return None
        for name in manifest['files']:
            shutil.copyfile(safe_file(entry, name), safe_file(output, name))
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(key, output, report):
    output = Path(output); scene = json.loads((output / 'world.scene.json').read_text())
    names = {'world.scene.json', scene['mesh_file'], 'material-rsx-report.json', report.name, *scene.get('textures', {}).values()}
    root = cache_root(); root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.pending-', dir=root))
    try:
        checks = {}
        for name in sorted(names):
            src = safe_file(output, name); dst = safe_file(staging, name)
            shutil.copyfile(src, dst); checks[name] = digest(dst)
        manifest = {'key': key, 'files': checks, 'source_run': str(output.resolve())}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        target = root / key
        if target.exists(): shutil.rmtree(target)
        staging.rename(target)
    finally:
        if staging.exists(): shutil.rmtree(staging)

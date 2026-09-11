"""Ambient FX placements for the 3D preview, from the map's own createfx scripts.

The linked map zone carries its GSC rawfiles.  ``maps/mp/<map>_fx.gsc``
registers effect keys (``level._effect["key"] = loadfx("path")``) and
``maps/createfx/<map>_fx.gsc`` places them (``createLoopEffect("key")`` /
``createOneshotEffect`` / ``createExploder`` followed by
``ent.v["origin"] = (x, y, z)``).  This module reads both scripts straight
out of the emulated machine and writes ``fx-placements.json`` so the desktop
viewer can play every ambient loop - weather included, rain and snow are
exactly such placed loops - at its real world position.

Preview-only: nothing here feeds zone serialization.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

RAWFILE_TYPE = 0x21   # PS3 IW3 rawfile XAsset family

_REGISTER = re.compile(
    r'level\._effect\s*\[\s*"([^"]+)"\s*\]\s*=\s*loadfx\s*\(\s*"([^"]+)"\s*\)',
    re.IGNORECASE)
_CREATE = re.compile(
    r'create(Loop|Oneshot|OneShot)?(Effect|Exploder)\s*\(\s*"([^"]+)"\s*\)',
    re.IGNORECASE)
_ORIGIN = re.compile(
    r'\.v\s*\[\s*"origin"\s*\]\s*=\s*\(\s*(-?[\d.eE+]+)\s*,\s*(-?[\d.eE+]+)\s*,\s*(-?[\d.eE+]+)\s*\)')
_ANGLES = re.compile(
    r'\.v\s*\[\s*"angles"\s*\]\s*=\s*\(\s*(-?[\d.eE+]+)\s*,\s*(-?[\d.eE+]+)\s*,\s*(-?[\d.eE+]+)\s*\)')
_FXID = re.compile(r'\.v\s*\[\s*"fxid"\s*\]\s*=\s*"([^"]+)"', re.IGNORECASE)


def _rawfile_text(machine, name: str) -> str | None:
    root, error = machine.find(RAWFILE_TYPE, name)
    if not root or error:
        return None
    reader = machine.m
    buffer = struct.unpack('>I', reader.read(root + 8, 4))[0]
    length = struct.unpack('>i', reader.read(root + 4, 4))[0]
    if not buffer or length <= 0 or length > 8 << 20:
        return None
    data = reader.read(buffer, length)
    return data.split(b'\0')[0].decode('latin-1', 'replace')


def _strip_comments(text: str) -> str:
    """String-aware GSC comment stripper: ``//`` and ``/* */`` outside string
    literals become spaces; anything inside ``"..."`` stays verbatim (GSC
    treats backslashes in paths literally, so no escape handling)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        char = text[i]
        if in_string:
            if char == '"':
                in_string = False
            out.append(char)
            i += 1
        elif char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif char == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            out.append(' ')
        elif char == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            out.append(' ')
        else:
            out.append(char)
            i += 1
    return ''.join(out)


def _norm_effect_path(value: str) -> str:
    """Match the FX name normalization used everywhere else in the porter:
    backslashes become slashes, duplicate separators collapse, no leading
    slash.  Zone FX names are stored exactly in this shape."""
    out = value.replace('\\', '/')
    while '//' in out:
        out = out.replace('//', '/')
    return out.lstrip('/')


def parse_registrations(text: str) -> dict[str, str]:
    return {key: _norm_effect_path(path)
            for key, path in _REGISTER.findall(_strip_comments(text))}


def parse_placements(text: str) -> list[dict]:
    """Statement-ordered walk: a create* opens a placement, the following
    ``ent.v[...]`` lines fill it until the next create* (or end)."""
    rows: list[dict] = []
    current: dict | None = None
    for statement in _strip_comments(text).split(';'):
        create = _CREATE.search(statement)
        if create is not None:
            if current is not None and current.get('origin') is not None:
                rows.append(current)
            kind = 'exploder' if create.group(2).lower() == 'exploder' else (
                'oneshot' if (create.group(1) or '').lower() == 'oneshot' else 'loop')
            current = {'fxid': create.group(3), 'kind': kind,
                       'origin': None, 'angles': [0.0, 0.0, 0.0]}
            continue
        if current is None:
            continue
        origin = _ORIGIN.search(statement)
        if origin is not None:
            current['origin'] = [float(origin.group(i)) for i in (1, 2, 3)]
        angles = _ANGLES.search(statement)
        if angles is not None:
            current['angles'] = [float(angles.group(i)) for i in (1, 2, 3)]
        fxid = _FXID.search(statement)
        if fxid is not None:
            current['fxid'] = fxid.group(1)
    if current is not None and current.get('origin') is not None:
        rows.append(current)
    return rows


def export_fx_placements(machine, map_name: str, out_dir) -> Path | None:
    register_text = _rawfile_text(machine, f'maps/mp/{map_name}_fx.gsc')
    createfx_text = _rawfile_text(machine, f'maps/createfx/{map_name}_fx.gsc')
    registrations = parse_registrations(register_text or '')
    placements = parse_placements(createfx_text or '')
    resolved = []
    unresolved = []
    for row in placements:
        effect = registrations.get(row['fxid'])
        if effect is None:
            unresolved.append(row['fxid'])
            continue
        resolved.append({'fxid': row['fxid'], 'effect': effect, 'kind': row['kind'],
                         'origin': row['origin'], 'angles': row['angles']})
    document = {
        'schema': 'iw3-fx-placements/v1',
        'map': map_name,
        'placements': resolved,
        'registered_effects': registrations,
        'unresolved_fxids': sorted(set(unresolved)),
        'sources': {
            'registrations': f'maps/mp/{map_name}_fx.gsc' if register_text else None,
            'createfx': f'maps/createfx/{map_name}_fx.gsc' if createfx_text else None,
        },
        'note': 'Preview placements parsed from the map\'s own createfx scripts; '
                'script execution is not emulated.',
    }
    target = Path(out_dir) / 'fx-placements.json'
    target.write_text(json.dumps(document, indent=2, ensure_ascii=True), encoding='utf-8')
    return target

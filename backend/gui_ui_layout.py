"""Resolve menu offsets only for an exact, captured UI revision."""
from pathlib import Path
import hashlib
import json

_CACHE = []

def _mapping(zone):
    for cached_zone, mapping in _CACHE:
        if cached_zone is zone: return mapping
    from gui_revision import ui_identity
    identity=ui_identity(zone)
    name=identity.get('editor_profile')
    if not name: raise ValueError('This UI has no validated editing layout: '+identity['zone_sha256'])
    mapping = {}
    if name != 'ui-menu-layout-v1.json.zlib':
        path=Path(__file__).parent/'reference'/name.replace('.json.zlib','.offsets.json')
        mapping = {int(k):v for k,v in json.loads(path.read_text()).items()}
    _CACHE.append((zone, mapping))
    del _CACHE[:-2]
    return mapping

def layout_value(zone, value):
    if isinstance(value,tuple):return tuple(layout_value(zone,v) for v in value)
    if type(value) is int:return _mapping(zone).get(value,value)
    return value

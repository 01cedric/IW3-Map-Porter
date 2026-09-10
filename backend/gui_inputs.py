"""Resolve the loading companion used by a linked-map check."""
from pathlib import Path
import re


def load_companion(settings):
    explicit=settings.get('ps3_load_ff','')
    if explicit:
        path=Path(explicit)
        if not path.is_file():raise ValueError('PS3 loading companion is missing: '+str(path))
        return path
    main=Path(settings['ps3_ff'])
    if main.suffix.lower()!='.ff':return None
    match=re.fullmatch(r'(.+?)(\(\d+\))?',main.stem)
    name,suffix=match.groups()
    if name.endswith('_load'):return None
    path=main.with_name(name+'_load'+(suffix or '')+'.ff')
    return path if path.is_file() else None

"""Exact-name PC texture supplements, bounded to images used by this map."""
from pathlib import Path
from .backend.v4_iwd import inspect_iwds


def library_archives(folder):
    root=Path(folder)
    if not root.is_dir():raise ValueError('PC texture library directory does not exist: '+str(root))
    files=sorted(p.resolve() for p in root.iterdir() if p.is_file() and p.suffix.lower()=='.iwd')
    if not files:raise ValueError('PC texture library contains no IWD archives. Select the PC main directory.')
    return files


def supplement_images(images,archives,required):
    # Map-specific images take priority; contradictory library-only copies fail.
    missing={n.casefold() for n in required}-set(images)
    extra,_=inspect_iwds(archives,include_sounds=False,image_names=missing)
    images.update(extra)
    return len(extra)

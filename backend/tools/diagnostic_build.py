#!/usr/bin/env python3
"""Build a deliberately reduced PS3 main zone to localize a hardware load failure.

This bypasses the porter's fidelity gates on purpose: the output is not a port, it is a
measuring instrument.  Every reduction keeps the XAssetList, the asset order and every
cross-asset reference intact, so the zone still loads the way a real one does - only the
payload behind one asset family is gone.

    --xmodels external   replace every owned XModel with a ,name reference shell.  The
                         ClipMap static models and the GfxWorld smodel draws reach models
                         through their XAssetList alias cell, so they stay valid; what
                         disappears is skeleton, XSurfaces, vertices, indices, collision and
                         every Material/GfxImage those models owned.

Usage:
    python tools/diagnostic_build.py --pc-ff mp_getaway.ff --map mp_getaway \\
        --iwd mp_getaway.iwd --out OUT --xmodels external [policy flags as in run_porter.py]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.assembler import analyze_and_assemble  # noqa: E402
from cod4porter.assets.gfxworld import GfxWorldResourcePolicy  # noqa: E402
from cod4porter.assets.image import ImageResourcePolicy  # noqa: E402
from cod4porter.main_ff import save_main_fastfile  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='build a reduced diagnostic PS3 zone')
    ap.add_argument('--pc-ff', required=True)
    ap.add_argument('--map', required=True)
    ap.add_argument('--iwd', action='append', default=[])
    ap.add_argument('--out', required=True)
    ap.add_argument('--xmodels', choices=['source', 'external'], default='source')
    ap.add_argument('--runtime-compatible', action='store_true')
    ap.add_argument('--sound-policy', default='omit')
    ap.add_argument('--unresolved-image-policy', default='neutral')
    ap.add_argument('--unresolved-material-policy', default='default')
    ap.add_argument('--primary-light-policy', default='source')
    ap.add_argument('--gfxworld-vertex-layers', dest='vertex_layer_policy', default='source')
    ap.add_argument('--gfxworld-portals', dest='portal_policy', default='omit')
    ns = ap.parse_args(argv)

    assembly = analyze_and_assemble(
        ns.pc_ff, ns.map, ns.iwd,
        runtime_compatible=ns.runtime_compatible,
        gfxworld_resource_policy=GfxWorldResourcePolicy.FASTFILE_DELAYED,
        material_image_resource_policy=ImageResourcePolicy.FASTFILE_DELAYED,
        sound_policy=ns.sound_policy,
        unresolved_image_policy=ns.unresolved_image_policy,
        unresolved_material_policy=ns.unresolved_material_policy,
        primary_light_policy=ns.primary_light_policy,
        vertex_layer_policy=ns.vertex_layer_policy,
        portal_policy=ns.portal_policy,
    )
    plan = assembly.plan
    note = {'xmodels': ns.xmodels}
    if ns.xmodels == 'external':
        plan, stats = plan.with_external_xmodels()
        note.update(stats)
    build = plan.build(assembly.source.asset_list)
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f'{ns.map}.ff'
    save_main_fastfile(target, build)
    summary = {
        'out': str(target),
        'fastfile_bytes': len(build.fastfile),
        'zone_bytes': len(build.retail_zone),
        'block_sizes': list(build.block_sizes),
        'asset_count': build.asset_count,
        'asset_counts': build.asset_counts,
        'fastfile_sha256': build.fastfile_sha256,
        **note,
    }
    (out / f'{ns.map}.diagnostic.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

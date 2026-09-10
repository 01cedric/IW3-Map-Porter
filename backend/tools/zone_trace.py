#!/usr/bin/env python3
"""Dump the complete logical layout of a generated PS3 zone.

The zone writer already models what IW3's loader does with the stream: which block each
allocation lands in, what its logical offset inside that block is, and how large it is.
``--trace`` makes it record every one of those steps.  This tool runs one serialization
pass with tracing on and writes the result as a flat table, so a zone can be inspected
the way the loader walks it - allocation by allocation - instead of as a wall of bytes.

Usage:
    python tools/zone_trace.py --pc-ff mp_shipment.ff --map mp_shipment \\
        [--iwd x.iwd ...] [--out trace.txt] [<policy flags as in run_porter.py port>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from cod4porter.assembler import analyze_and_assemble  # noqa: E402
from cod4porter.assets.gfxworld import GfxWorldResourcePolicy  # noqa: E402
from cod4porter.assets.image import ImageResourcePolicy  # noqa: E402
from cod4porter.graph import write_graph  # noqa: E402
from cod4porter.zone_writer import SerializationOptions  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='dump the logical layout of a generated PS3 zone')
    ap.add_argument('--pc-ff', required=True)
    ap.add_argument('--map', required=True)
    ap.add_argument('--iwd', action='append', default=[])
    ap.add_argument('--out', default='zone_trace.txt')
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
    plan.validate(assembly.source.asset_list)
    result = write_graph(
        plan.serialized_assets(), plan.script_strings,
        SerializationOptions(apply_retail_block_floors=True, enable_trace=True),
        top_level_assets=plan.top_level_assets(),
        ownership_edges=plan.active_ownership_edges(),
    )
    out = Path(ns.out)
    out.write_text('\n'.join(result.zone.trace), encoding='utf-8')
    Path(str(out) + '.results.json').write_text(
        json.dumps(result.context.results, indent=1, default=str), encoding='utf-8')
    print({'out': str(out), 'steps': len(result.zone.trace),
           'block_sizes': list(result.zone.block_sizes),
           'zone_bytes': len(result.zone.zone_bytes)})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

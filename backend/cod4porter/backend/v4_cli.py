from __future__ import annotations
import argparse,json
from dataclasses import asdict
from pathlib import Path

from .v4_source import inspect_pc_source,audit_csharp_source
from .v4_assets import scan_asset_families
from .v4_ffio import read_pc_fastfile,read_ps3_fastfile,summarize_fastfile
from .v4_load import parse_load_zone
from .v4_gate import evaluate_full_fidelity
from .v4_pipeline import run_pipeline
from .v4_retail import build_retail_evidence
from .v4_readiness import run_readiness
from .v4_selftest import run as run_selftest

HERE=Path(__file__).resolve().parent

def write(path,obj):Path(path).write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding='utf-8')

def main():
    ap=argparse.ArgumentParser(description='FIX114 V4 independent backend CLI');sp=ap.add_subparsers(dest='cmd',required=True)
    p=sp.add_parser('inspect-source');p.add_argument('pc_ff');p.add_argument('--iwd',action='append',default=[]);p.add_argument('--deep-assets',action='store_true');p.add_argument('--deep-sound',action='store_true');p.add_argument('--output',default='FIX114_V4_PC_SOURCE.json')
    p=sp.add_parser('scan-assets');p.add_argument('pc_ff');p.add_argument('--output',default='FIX114_V4_ASSET_FAMILIES.json')
    p=sp.add_parser('inspect-load');p.add_argument('ps3_load');p.add_argument('--output',default='FIX114_V4_LOAD_PROFILE.json')
    p=sp.add_parser('audit-source');p.add_argument('source_root');p.add_argument('--output',default='FIX114_V4_CSHARP_AUDIT.json')
    p=sp.add_parser('retail-evidence');p.add_argument('spec');p.add_argument('--output',default='FIX114_V4_RETAIL_EVIDENCE.json')
    p=sp.add_parser('gate');p.add_argument('evidence',nargs='+');p.add_argument('--output',default='FIX114_V4_FULL_FIDELITY_GATE.json')
    p=sp.add_parser('pipeline');p.add_argument('config')
    p=sp.add_parser('selftest');p.add_argument('--reference-root',required=True);p.add_argument('--output',default=str(HERE/'FIX114_V4_SELFTEST.json'))
    p=sp.add_parser('readiness');p.add_argument('--reference-root',required=True);p.add_argument('--selftest',default=str(HERE/'FIX114_V4_SELFTEST.json'));p.add_argument('--full-gate');p.add_argument('--output',default=str(HERE/'FIX114_V4_READINESS.json'))
    a=ap.parse_args()
    if a.cmd=='inspect-source':r=inspect_pc_source(a.pc_ff,a.iwd,{'script_strings':469,'xassets':767,'iwi':735,'audio':17},a.deep_assets,a.deep_sound);write(a.output,r);print(a.output);return 0
    if a.cmd=='scan-assets':r=scan_asset_families(read_pc_fastfile(a.pc_ff).zone);write(a.output,r);print(a.output);return 0
    if a.cmd=='inspect-load':d=read_ps3_fastfile(a.ps3_load);r=asdict(parse_load_zone(d.zone));write(a.output,r);print(a.output);return 0
    if a.cmd=='audit-source':r=audit_csharp_source(a.source_root);write(a.output,r);print(a.output);return 0 if r['passed'] else 2
    if a.cmd=='retail-evidence':r=build_retail_evidence(json.loads(Path(a.spec).read_text(encoding='utf-8-sig')));write(a.output,r);print(a.output);return 0
    if a.cmd=='gate':
        docs=[json.loads(Path(x).read_text(encoding='utf-8-sig')) for x in a.evidence];r=evaluate_full_fidelity(*docs);write(a.output,r);print(json.dumps({'full_fidelity_passed':r['full_fidelity_passed'],'passed_count':r['passed_count'],'required_count':r['required_count'],'failed':r['failed']},indent=2));return 0 if r['full_fidelity_passed'] else 2
    if a.cmd=='pipeline':r=run_pipeline(json.loads(Path(a.config).read_text(encoding='utf-8-sig')));return 0 if r['full_fidelity_passed'] else 2
    if a.cmd=='selftest':r=run_selftest(Path(a.reference_root));write(a.output,r);print(json.dumps({'passed':r['passed'],'check_count':r['check_count'],'failed':[x['name'] for x in r['failed']]},indent=2));return 0 if r['passed'] else 1
    if a.cmd=='readiness':r=run_readiness(Path(a.reference_root),Path(a.selftest),Path(a.full_gate) if a.full_gate else None);write(a.output,r);print(json.dumps({'backend_engine_ready':r['backend_engine_ready'],'getaway_full_fidelity_proven':r['getaway_full_fidelity_proven'],'failed':r['failed']},indent=2));return 0 if r['backend_engine_ready'] else 1
    return 2
if __name__=='__main__':raise SystemExit(main())

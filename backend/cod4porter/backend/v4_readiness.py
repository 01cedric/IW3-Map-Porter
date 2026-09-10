from __future__ import annotations

import argparse
import json
from pathlib import Path
import py_compile
import hashlib

from .v4_ffio import read_ps3_fastfile, summarize_fastfile
from .v4_load import parse_load_zone
from .v4_source import audit_csharp_source, compare_tree_manifest
from .v4_pipeline import run_pipeline

HERE=Path(__file__).resolve().parent
MODULES=[
 'v4_ffio.py','v4_load.py','v4_iwd.py','v4_proofs.py','v4_sound.py','v4_images.py',
 'v4_assets.py','v4_source.py','v4_retail.py','v4_gate.py','v4_pipeline.py','v4_selftest.py','v4_readiness.py'
]
FIX89_MAIN='0F94852CA8A3882D049B95CCD6C46DA7E6088F64D3E319A1D2E042041953ADF2'
FIX89_LOAD='428A9C45E0FB167CE8C6495760E8E2A524F8CE93A76D445D23053A31B4B84E50'


def parse_manifest(path:Path)->dict[str,str]:
    out={}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        h,p=line.split(None,1);p=p.strip();p=p[2:] if p.startswith('./') else p;out[p]=h.upper()
    return out


def run_readiness(reference_root:Path,selftest_path:Path,full_gate_path:Path|None=None)->dict:
    checks=[]
    def add(name,passed,proof):checks.append({'name':name,'passed':bool(passed),'proof':proof})

    missing=[m for m in MODULES if not (HERE/m).exists()]
    add('all V4 backend modules present',not missing,{'required':MODULES,'missing':missing})
    compile_errors=[]
    for m in MODULES:
        p=HERE/m
        if not p.exists():continue
        try:py_compile.compile(str(p),doraise=True)
        except Exception as exc:compile_errors.append({'module':m,'error':str(exc)})
    add('all V4 Python modules compile',not compile_errors,{'errors':compile_errors})

    st=json.loads(selftest_path.read_text(encoding='utf-8')) if selftest_path.exists() else {}
    add('release selftest passed',bool(st.get('passed')) and int(st.get('check_count',0))>=100,{'passed':st.get('passed'),'check_count':st.get('check_count'),'failed':st.get('failed')})

    baseline=reference_root/'hardware_baseline/Fix89B';main=baseline/'mp_getaway.ff';load=baseline/'mp_getaway_load.ff'
    try:
        md=read_ps3_fastfile(main);ld=read_ps3_fastfile(load)
        add('Fix89B hardware FastFile hashes exact',md.file_sha256==FIX89_MAIN and ld.file_sha256==FIX89_LOAD,{'main':md.file_sha256,'load':ld.file_sha256})
        lp=parse_load_zone(ld.zone)
        add('Fix89B strict _load profile parses',lp.trailing_nonzero==0 and lp.xassets==5,{'materials':[m.name for m in lp.materials],'resource_bytes':lp.resource_bytes,'trailing_capacity':lp.trailing_capacity,'trailing_nonzero':lp.trailing_nonzero})
    except Exception as exc:
        add('Fix89B hardware FastFile hashes exact',False,{'error':str(exc)});add('Fix89B strict _load profile parses',False,{'error':str(exc)})

    manifest=parse_manifest(HERE/'FIX113_REFERENCE_MANIFEST.sha256');integrity=compare_tree_manifest(reference_root,manifest)
    add('FIX113 reference tree unchanged',integrity['passed'],integrity)

    audit=audit_csharp_source(reference_root);by={x['id']:x['present'] for x in audit.get('findings',[])}
    required_debt=['sound_curve_pool_index','neutral_images','water_default_fallback','platformstate_zero','material_state_neutralization','stringtable_compatibility']
    missing_debt=[d for d in required_debt if not by.get(d)]
    add('known FIX113 integration debt still detected',audit.get('passed') and not missing_debt,{'required_debt':required_debt,'missing_debt':missing_debt,'files_scanned':audit.get('files_scanned')})

    # A backend-only run against the hardware-good _load must execute, but it MUST NOT claim the
    # whole map is full-fidelity without original-PC/conversion/retail evidence.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pipe=run_pipeline({'output_dir':str(Path(td)/'out'),'ps3_load':str(load),'source_root':str(reference_root)})
    add('one-pass pipeline executes fail-closed',pipe.get('backend_pipeline_passed') and not pipe.get('full_fidelity_passed'),{'pipeline_passed':pipe.get('backend_pipeline_passed'),'full_fidelity_passed':pipe.get('full_fidelity_passed'),'missing':pipe.get('full_fidelity_gate',{}).get('missing')})

    backend_ready=all(c['passed'] for c in checks)
    gate={}
    if full_gate_path and full_gate_path.exists():gate=json.loads(full_gate_path.read_text(encoding='utf-8-sig'))
    getaway_proven=bool(gate.get('full_fidelity_passed'))
    return {
      'revision':'FIX114-backend-v4',
      'backend_engine_ready':backend_ready,
      'getaway_full_fidelity_proven':getaway_proven,
      'reference_project_modified':not integrity['passed'],
      'check_count':len(checks),'checks':checks,'failed':[c['name'] for c in checks if not c['passed']],
      'separation':'backend_engine_ready certifies the independent analysis/proof engine; getaway_full_fidelity_proven is only true after original PC bytes + conversion output + required retail/hardware evidence pass all 26 gates.'
    }


def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--reference-root',required=True);ap.add_argument('--selftest',default=str(HERE/'FIX114_V4_SELFTEST.json'));ap.add_argument('--full-gate');ap.add_argument('--output',default=str(HERE/'FIX114_V4_READINESS.json'));a=ap.parse_args()
    result=run_readiness(Path(a.reference_root),Path(a.selftest),Path(a.full_gate) if a.full_gate else None);Path(a.output).write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'backend_engine_ready':result['backend_engine_ready'],'getaway_full_fidelity_proven':result['getaway_full_fidelity_proven'],'check_count':result['check_count'],'failed':result['failed']},indent=2))
    return 0 if result['backend_engine_ready'] else 1
if __name__=='__main__':raise SystemExit(main())

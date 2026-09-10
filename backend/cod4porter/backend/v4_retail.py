from __future__ import annotations

import json
from pathlib import Path

from .v4_gate import evidence
from .v4_iwd import parse_iwi, cubemap_face_fingerprints, prove_cubemap_bijection
from .v4_proofs import PlatformStateCatalog, parse_pc_water, water_observation


def _hx(value:str|None)->bytes:
    if not value:return b''
    s=''.join(c for c in str(value) if c in '0123456789abcdefABCDEF')
    if len(s)&1:raise ValueError('hex string has odd length')
    return bytes.fromhex(s)


def build_platformstate_retail_evidence(section:dict)->dict:
    required=list(section.get('required_signatures') or [])
    observations=list(section.get('observations') or [])
    cat=PlatformStateCatalog()
    for row in observations:
        sig=str(row['signature']);payload=_hx(row['platform_state_hex'])
        cat.add(sig,payload,str(row.get('source') or 'retail-observation'))
    coverage=cat.coverage(required)
    return {'source':'retail_observation','evidence':[
        evidence('platform_state_exact',bool(required) and coverage['passed'],'retail_observation',{
            'required_count':len(required),'observation_count':len(observations),'coverage':coverage
        })
    ]}


def build_water_retail_evidence(section:dict)->dict:
    pc_root=_hx(section.get('pc_root_hex')); ps3_root=_hx(section.get('ps3_root_hex'))
    pc=parse_pc_water(pc_root,_hx(section.get('pc_h0_hex')),_hx(section.get('pc_wterm_hex')))
    proof=water_observation(pc,ps3_root,_hx(section.get('ps3_h0_hex')),_hx(section.get('ps3_wterm_hex')))
    proof['source_description']=section.get('source_description')
    return {'source':'retail_observation','evidence':[evidence('water_exact',proof['passed'],'retail_observation',proof)]}


def build_cubemap_evidence(section:dict)->list[dict]:
    src_path=Path(section['source_iwi'])
    img=parse_iwi(src_path.name,src_path.read_bytes())
    source_hashes=cubemap_face_fingerprints(img,True)
    proof=prove_cubemap_bijection(img,section.get('target_face_hashes') or [])
    source_doc={'source':'pc_source_deep','evidence':[evidence('cubemap_exact',img.is_cubemap and len(source_hashes)==6,'pc_source_deep',{
        'source_iwi':str(src_path.resolve()),'source_face_hashes':source_hashes,'face_count':img.face_count
    })]}
    retail_doc={'source':'retail_observation','evidence':[evidence('cubemap_exact',proof.get('passed',False),'retail_observation',proof)]}
    return [source_doc,retail_doc]


def build_retail_evidence(spec:dict)->dict:
    docs=[]
    if spec.get('platform_state'):docs.append(build_platformstate_retail_evidence(spec['platform_state']))
    if spec.get('water'):docs.append(build_water_retail_evidence(spec['water']))
    if spec.get('cubemap'):docs.extend(build_cubemap_evidence(spec['cubemap']))
    return {'revision':'FIX114-backend-v4','evidence_documents':docs,'evidence':[e for d in docs for e in d.get('evidence',[])]}


def main()->int:
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('spec');ap.add_argument('--output',default='FIX114_V4_RETAIL_EVIDENCE.json');a=ap.parse_args()
    result=build_retail_evidence(json.loads(Path(a.spec).read_text(encoding='utf-8-sig')))
    Path(a.output).write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'output':a.output,'evidence_count':len(result['evidence'])},indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())

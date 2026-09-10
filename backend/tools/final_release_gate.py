#!/usr/bin/env python3
"""Fail-closed final release gate for the CoD4 PC->PS3 porter.

This module intentionally does not infer success from a coarse progress number.
Every release-blocking condition must be present in the machine-readable report
and must have the exact accepted value. Missing evidence is a failure.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import argparse, hashlib, json, os, re, subprocess, sys, time

@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    observed: Any
    expected: Any
    evidence: str


def _walk(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield p, v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            yield p, v
            yield from _walk(v, p)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _find(report: Any, aliases: Sequence[str]) -> tuple[str | None, Any]:
    wanted = {_norm(x) for x in aliases}
    # Exact normalized leaf name has priority, then full-path suffix.
    rows = list(_walk(report))
    for p, v in rows:
        if _norm(p.rsplit('.', 1)[-1].split('[',1)[0]) in wanted:
            return p, v
    for p, v in rows:
        np = _norm(p)
        if any(np.endswith(a) for a in wanted):
            return p, v
    return None, None


def _zero_gate(report: Any, name: str, aliases: Sequence[str]) -> Gate:
    p, v = _find(report, aliases)
    ok = p is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0
    return Gate(name, ok, v if p is not None else '<missing>', 0, p or 'missing report field')


def _bool_gate(report: Any, name: str, aliases: Sequence[str], expected: bool = True) -> Gate:
    p, v = _find(report, aliases)
    ok = p is not None and type(v) is bool and v is expected
    return Gate(name, ok, v if p is not None else '<missing>', expected, p or 'missing report field')


def _positive_gate(report: Any, name: str, aliases: Sequence[str]) -> Gate:
    p, v = _find(report, aliases)
    ok = p is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    return Gate(name, ok, v if p is not None else '<missing>', '> 0', p or 'missing report field')


def _sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''): h.update(b)
    return h.hexdigest().upper()


def _source_audit(root: Path) -> list[Gate]:
    prod=[]
    for p in root.rglob('*'):
        if not p.is_file() or p.suffix.lower() not in {'.py','.cmd','.bat','.ps1','.sh'}: continue
        s=str(p).lower()
        if any(x in s for x in ('test','fixture','reference_inputs','__pycache__')): continue
        try: txt=p.read_text(encoding='utf-8',errors='replace')
        except OSError: continue
        prod.append((p,txt))
    forbidden={
      'production allow-unproven-retail flag': re.compile(r'--allow-unproven-retail\b',re.I),
      'production runtime-compatible isolation flag': re.compile(r'--runtime-compatible\b',re.I),
      'silent default material substitution': re.compile(r'(?<![A-Za-z0-9_])default_material\s*(?:=|:)\s*["\']?,?default',re.I),
      'silent neutral image substitution': re.compile(r'neutral[_ -]?image[_ -]?fallback\s*(?:=|:)\s*(?:true|1)',re.I),
    }
    gates=[]
    for name,rx in forbidden.items():
        hits=[]
        for p,txt in prod:
            for m in rx.finditer(txt):
                line=txt.count('\n',0,m.start())+1
                hits.append(f'{p.relative_to(root)}:{line}')
        gates.append(Gate(name,not hits,hits[:50],[], 'production source audit'))
    return gates


def evaluate(report: Any, root: Path) -> list[Gate]:
    gates=[
      _zero_gate(report,'no unresolved XModel material slots',['unresolved_xmodel_material_slots','xmodel_material_slots_unresolved','xmodelMaterialUnresolved']),
      _zero_gate(report,'no unresolved packed material targets',['unresolved_packed_material_targets','packed_material_targets_unresolved']),
      _zero_gate(report,'no default material substitutions',['default_material_substitutions','default_material_fallbacks','runtime_material_fallbacks']),
      _zero_gate(report,'no unresolved image slots',['unresolved_image_slots','gfximage_closure_unresolved','image_slots_unresolved']),
      _zero_gate(report,'no unresolved packed image targets',['unresolved_packed_image_targets','unique_unresolved_image_targets','image_targets_unresolved']),
      _zero_gate(report,'no neutral image fallbacks',['neutral_image_fallbacks','neutral_images','image_fallbacks']),
      _zero_gate(report,'no omitted source images',['omitted_iwd_images','unemitted_iwd_images','missing_source_images']),
      _zero_gate(report,'no unproven cube/special images',['unproven_cube_images','unresolved_cube_images','special_image_fallbacks']),
      _zero_gate(report,'no unresolved PlatformStates',['unresolved_platform_states','platform_states_unresolved']),
      _zero_gate(report,'no PlatformState fallbacks',['platform_state_fallbacks','platformstate_fallbacks','sortkey_family_fallbacks']),
      _zero_gate(report,'no delayed images without native container',['delayed_images_without_container','unbacked_delayed_images','missing_streamed_image_payloads']),
      _zero_gate(report,'no relocation errors',['relocation_errors','invalid_relocations']),
      _zero_gate(report,'no source pointer leaks',['source_pointer_leaks','pc_pointer_leaks']),
      _zero_gate(report,'no unresolved output references',['unresolved_output_references','unresolved_output_refs','dangling_asset_references']),
      _zero_gate(report,'no fallback assets of any kind',['fallback_asset_count','total_fallbacks','fallbacks_remaining']),
      _bool_gate(report,'generic PC load fastfile was parsed',['pc_load_input_parsed','source_load_ff_parsed','generic_load_input_used']),
      _bool_gate(report,'generic PS3 load fastfile was generated',['generic_load_ff_converter','load_ff_generated_from_pc','generic_load_conversion']),
      _bool_gate(report,'load output does not use a map template',['load_output_template_free','map_specific_load_template_free','load_template_free']),
      _bool_gate(report,'owner graph closure complete',['owner_graph_closure_complete','asset_graph_closed']),
      _bool_gate(report,'global PC loadstream replay exact',['global_loadstream_replay_exact','loadstream_replay_exact']),
      _bool_gate(report,'retail image layout independently verified',['retail_image_layout_verified','image_layout_independent_verification']),
      _bool_gate(report,'all fidelity gates passed',['all_fidelity_gates_passed','fidelity_complete']),
      _positive_gate(report,'regression tests executed',['regression_tests_total','tests_total']),
      _zero_gate(report,'regression test failures',['regression_tests_failed','tests_failed','failed_tests']),
    ]
    gates.extend(_source_audit(root))
    return gates


def main(argv: Sequence[str] | None=None) -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('report',type=Path)
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--out',type=Path)
    ns=ap.parse_args(argv)
    try: report=json.loads(ns.report.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'RELEASE BLOCKED: cannot read report: {e}',file=sys.stderr); return 2
    gates=evaluate(report,ns.root)
    result={
      'schema':'cod4-pc-to-ps3-final-release-gate/v1',
      'created_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
      'report':str(ns.report.resolve()),
      'report_sha256':_sha256(ns.report),
      'passed':all(g.passed for g in gates),
      'passed_count':sum(g.passed for g in gates),
      'total_count':len(gates),
      'gates':[asdict(g) for g in gates],
    }
    out=ns.out or ns.report.with_name(ns.report.stem+'.final-release-gate.json')
    out.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    for g in gates:
        print(('[PASS] ' if g.passed else '[FAIL] ')+g.name+f' | observed={g.observed!r}')
    print(('FINAL RELEASE PASSED' if result['passed'] else 'FINAL RELEASE BLOCKED')+f" ({result['passed_count']}/{result['total_count']})")
    return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())

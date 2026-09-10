#!/usr/bin/env python3
from pathlib import Path
import importlib.util, tempfile, json, sys
p=Path(__file__).resolve().parents[1]/'tools'/'final_release_gate.py'
s=importlib.util.spec_from_file_location('final_release_gate',p); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m)
# Empty evidence must fail every mandatory report-backed gate.
g=m.evaluate({},Path(__file__).resolve().parents[1])
assert g and not all(x.passed for x in g)
assert any(x.observed=='<missing>' for x in g)
# Explicit zeros alone must not accidentally satisfy positive/bool evidence.
assert not m._bool_gate({},'x',['x']).passed
assert not m._positive_gate({'x':0},'x',['x']).passed
print('PASS: final release gate is fail-closed')

#!/usr/bin/env python3
from cod4porter import asset_provenance as m

l=m.Ledger(strict=True)
l.add(m.Resolution("image","foo",None,"foo",[m.Evidence(m.EvidenceKind.SOURCE_POINTER,"pc-zone","block4+0x10")]))
assert l.require("image","foo").output_identity=="foo"
try:l.add(m.Resolution("image","bar",None,"default",[m.Evidence(m.EvidenceKind.NEUTRAL,"fallback","white")]))
except m.ProvenanceError:pass
else:raise AssertionError("neutral evidence accepted")
try:l.validate_complete([("image","foo"),("image","missing")])
except m.ProvenanceError:pass
else:raise AssertionError("missing evidence accepted")
print("PASS: strict asset provenance")

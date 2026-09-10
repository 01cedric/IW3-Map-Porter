"""The output graph, including per-material rebinding, owns dependencies."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cod4porter.porter import _native_dependency_manifest
from cod4porter.assets.external import ExternalTechniqueSetNode
from cod4porter.technique_binding import TechniqueBinding, TechniqueEvidence

b = TechniqueBinding('mc_l_sm_t0c0', 'mc_l_sm_b0c0', False,
                     TechniqueEvidence.PS3_FAMILY_SUBSTITUTION, True, 'constant-compatible')
source = SimpleNamespace(materials=SimpleNamespace(planned_owned=(SimpleNamespace(technique=b),)),
                         technique_bindings=(SimpleNamespace(candidate_name='obsolete_graph'),))
assembly = SimpleNamespace(source=source, plan=SimpleNamespace(assets=(
    ExternalTechniqueSetNode('mc_l_sm_b0c0','selected'),
    ExternalTechniqueSetNode('2d','extra-serialized-reference'))))
report = _native_dependency_manifest(assembly)
names = {x['required_ps3_name'] for x in report['techsets']['entries']}
assert names == {'mc_l_sm_b0c0','2d'}
assert report['techsets']['unique_required'] == 2
print('PASS: dependency manifest follows serialized graph, not stale source bindings')

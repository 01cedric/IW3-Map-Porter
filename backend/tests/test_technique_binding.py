"""TechniqueSet binding is decided by measured PS3 availability, not by name grammar.

The porter never converts a shader graph: a PC MaterialTechniqueSet becomes a ``,name``
that the console resolves through DB_FindXAssetHeader.  Version 13 accepted any name
matching the IW3 stock grammar, so a PC-only technique such as ``mc_l_hsm_r0c0`` was
emitted verbatim and could never resolve on PS3 - a silent load failure.

Binding now consults ``reference/ps3_technique_availability.json``, measured from a real
PS3 installation, and fails closed with the exact list when a name is absent.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.pc_technique import TechniqueSet
from cod4porter.technique_binding import (
    TechniqueEvidence,
    audited_retail_names,
    available_ps3_names,
    bind_all,
    bind_technique,
    is_available,
    ps3_candidates,
)


def t(name, idx=1):
    return TechniqueSet(0, name, 0, False, 0, (0,) * 34, idx)


# The load-zone TechniqueSet must survive, including its exact lower-case spelling.
two_d = bind_technique(t("2d"))
assert two_d.can_bind_native and two_d.candidate_name == "2d", two_d

# A shared PC marker is not evidence that PS3 owns the graph.
custom = bind_technique(t(",foo_custom"))
assert custom.evidence is TechniqueEvidence.UNSUPPORTED and not custom.can_bind_native

# The sm2/ prefix is still stripped before lookup.
assert bind_technique(t("sm2/mc_l_sm_r0c0")).candidate_name == "mc_l_sm_r0c0"

# Exact availability binds exactly and reports where the evidence came from.
exact = bind_technique(t("m_l_sm_r0c0"))
assert exact.can_bind_native and not exact.substitution
assert exact.evidence in (TechniqueEvidence.PS3_AVAILABLE_EXACT, TechniqueEvidence.AUDITED_RETAIL)

# PS3 ships no hard-shadow-map lighting variant.  The substitution keeps every texture
# channel and only softens the shadow, so it is preferred over dropping vertex colour.
assert not is_available("mc_l_hsm_r0c0")
order = ps3_candidates("mc_l_hsm_r0c0")
assert order[0] == "mc_l_hsm_r0c0" and order[1] == "mc_l_sm_r0c0", order
substituted = bind_technique(t("mc_l_hsm_r0c0"))
assert substituted.can_bind_native
assert substituted.evidence is TechniqueEvidence.PS3_FAMILY_SUBSTITUTION
assert substituted.candidate_name == "mc_l_sm_r0c0"
assert substituted.substitution == "mc_l_hsm_r0c0 -> mc_l_sm_r0c0"

# m (model) and w (world) are different vertex formats and are never exchanged.
assert all(not x.startswith("w") for x in ps3_candidates("mc_l_sm_r0c0"))
assert all(not x.startswith("m") for x in ps3_candidates("wc_l_sm_b0c0"))

# A name with no PS3 equivalent fails closed, and the message names it.
assert not bind_technique(t("definitely_custom_shader_xyz")).can_bind_native
try:
    bind_all([t("definitely_custom_shader_xyz")])
except ValueError as exc:
    assert "definitely_custom_shader_xyz" in str(exc)
    assert "ps3_technique_availability.json" in str(exc)
else:
    raise AssertionError("an unavailable TechniqueSet must block the port")

assert len(audited_retail_names()) > 20
assert len(available_ps3_names()) > 50

print("test_technique_binding PASS")

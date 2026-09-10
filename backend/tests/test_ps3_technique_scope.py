"""A PS3 map may only reference TechniqueSets that stay loaded while it runs.

Measured 2026-09-04 on a real PS3 installation.  Retail ``mp_crash.ff`` owns 98
MaterialTechniqueSets and Retail ``mp_shipment.ff`` owns 69: on PS3 a map carries its
own world shaders.  Two map zones are never loaded at the same time, so a name that was
only ever observed *owned by another map* cannot be resolved from this map -
``DB_FindXAssetHeader`` has nothing to find, and the load stops.

Version 15 built its availability catalog from every zone it was given, Retail map
zones included, and therefore accepted 22 names that no always-loaded zone provides
(``w_l_sm_r0c0n0``, ``wc_sky``, ``w_water``, ``mc_unlit`` ...).  This test locks the
provenance rule and the substitution outcome for exactly those names.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.technique_binding import (  # noqa: E402
    _NOT_RESOLVABLE,
    _resolve,
    _source_resolves,
    available_ps3_names,
    is_available,
    ps3_candidates,
)

# --- the provenance rule -----------------------------------------------------------
assert _source_resolves("support:common_mp:owned")
assert _source_resolves("support:code_post_gfx_mp:owned")
assert _source_resolves("support:common_mp:referenced")
assert _source_resolves("retail_map:mp_crash:referenced")
# Owned by another map, or merely inherited from an unverified bundled list, is not
# evidence that this map can resolve the name.
assert not _source_resolves("retail_map:mp_crash:owned")
assert not _source_resolves("retail_map:mp_shipment:owned")
assert not _source_resolves("version13_bundled_catalog")

# --- the names that must be rejected ------------------------------------------------
MAP_OWNED_ONLY = (
    "w_l_sm_r0c0",
    "w_l_sm_r0c0n0",
    "w_l_sm_r0c0s0",
    "w_l_sm_r0c0d0n0s0",
    "w_l_sm_b0c0d0n0",
    "w_water",
    "wc_sky",
    "wc_unlit_add",
    "wc_unlit_multiply",
    "wc_l_sm_r0c0n0s0",
    "wc_l_sm_t0c0n0s0",
    "mc_unlit",
    "mc_ambient_t0c0",
    "mc_l_r0c0n0s0",
    "mc_l_sm_r0c0n0",
    "mc_l_sm_r0c0d0n0s0",
    "mc_l_sm_t0c0n0s0",
    "m_shadowcaster",
    "m_unlit_replace",
    "m_l_sm_r0c0d0",
    "m_l_sm_t0c0n0",
    "effect_zfeather_falloff",
)
for name in MAP_OWNED_ONLY:
    assert not is_available(name), f"{name} must not count as resolvable"
    assert name.casefold() in _NOT_RESOLVABLE, name

# --- every one of them must still reach a name that IS resolvable -------------------
substitutions = {}
for name in MAP_OWNED_ONLY:
    hit = next((r[0] for c in ps3_candidates(name) if (r := _resolve(c))), None)
    assert hit is not None, f"{name} has no resolvable substitute"
    assert is_available(hit), (name, hit)
    substitutions[name] = hit

# The substitution must stay inside its vertex-format family wherever one exists:
# model graphs never become world graphs and the reverse, because the two declare
# different vertex formats and the loader binds the declaration, not the name.
for name, hit in substitutions.items():
    if name.startswith(("w_", "wc_")) and "_l_" in name:
        assert hit.startswith(("w_", "wc_")), (name, hit)
    if name.startswith(("m_", "mc_")) and "_l_" in name:
        assert hit.startswith(("m_", "mc_")), (name, hit)

# Spot-check the documented degradations so a later ladder change cannot silently
# reroute them somewhere less faithful.
assert substitutions["m_l_sm_r0c0d0"] == "m_l_sm_r0c0d0s0"     # keeps the detail map
assert substitutions["mc_l_sm_r0c0n0"] == "mc_l_sm_r0c0n0s0"   # keeps vertex colour
assert substitutions["mc_l_sm_r0c0d0n0s0"] == "mc_l_sm_r0c0n0s0"
assert substitutions["m_shadowcaster"] == "shadowcaster"
assert substitutions["mc_unlit"] == "m_unlit"
assert substitutions["wc_sky"] == "unlit"
assert substitutions["w_water"] == "wc_l_sm_b0c0"

# --- the resolvable catalog itself --------------------------------------------------
available = available_ps3_names()
# Everything the always-loaded zones own, and nothing a map zone merely keeps to itself.
for name in ("m_l_sm_r0c0", "mc_l_sm_r0c0n0s0", "wc_l_sm_b0c0n0s0", "unlit",
             "shadowcaster", "2d", "cinematic", "effect_zfeather_falloff_add"):
    assert name in available, name
for name in MAP_OWNED_ONLY:
    assert name not in available, name

print({
    "skipped": False,
    "resolvable_names": len(available),
    "rejected_names": len(_NOT_RESOLVABLE),
    "map_owned_rebound": len(substitutions),
})

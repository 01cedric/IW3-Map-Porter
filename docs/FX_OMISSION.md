# Unsupported effect omission

Default: **Options → Conversion profile → Unsupported effects → omit**.
Select `error` for strict behavior. Python API: `unsupported_fx_policy="omit"`;
CLI: `--unsupported-fx omit` (or `error`). Old desktop profiles use the new default.

The structural reader records typed asset-pointer edges and named FX references.
The conversion planner finds TechniqueSets without a supported native PS3 mapping,
then removes effects that depend on them, including parent effects. It prunes
exclusive material, image and TechniqueSet dependencies. Shared dependencies remain
when another retained owner needs them. Source objects are listed by root in the
report; a supported native PS3 technique can still be needed by other converted
materials under the same name.

This policy does not delete world geometry or model assets to hide unsupported
shaders. Those cases stop with an explanation. It detects unsupported shader
mappings using the bundled PS3 catalog, not arbitrary missing assets on every
console installation. Missing pixel and material policies remain separate options.

For map-owned text GSC files, literal loads of omitted FX are removed. Generated
createfx registration blocks for those effects are removed, and direct PlayFX,
PlayFXOnTag and PlayLoopedFX calls receive an isdefined guard in the same script.
Existing rawfile assets carry these edits; empty FX placeholder assets are not
created. Unknown dynamic loadfx requests, method-form playback or unrecognized
literal consumers stop rather than silently leaving a known missing request.
This is a bounded text transformation, not a GSC compiler or a general dataflow
analysis of retail scripts. Script compilation/execution and PS3 startup need
console testing. Script-only edits are logged in the port report.

Discovery Night removes `env/weather/fx_snow_debris_plume_sm` and
`maps/mp_maps/fx_mp_snow_wall_lg_os`, the latter depending on the former. Their
spot shaders and exclusive material/image payloads are not emitted. Other snow,
weather or lighting effects are retained when supported. Intentional omissions
always prevent a full-fidelity success claim, even when structural checks pass.


## Scope change in 22.2.0

Unsupported PS3 shader dependencies are first sent to the PC-to-RSX
compiler; an effect is omitted only when compilation itself fails, and
the report names the blocking construct per TechniqueSet. Maps that
previously lost effects (white-effect symptoms) now keep them with
compiled shader graphs unless the compiler reports a real blocker.

# Status — 22.2.9

[Back to README](../README.md)

PC-to-PS3 conversion, automatic texture budgeting, loading-picture preparation,
menu relocation and export reopening are available. Converted maps have reached
gameplay in console tests; this does not establish universal map compatibility.

The custom private-match launch script temporarily disables `useSvMapPreloading`
before direct map startup. It saves the previous value, queues restoration and
handles an interrupted override on the next private launch. BLES startup is
confirmed. The captured reference/BLES and BLUS UI exports passed the native
loader check. The tested BLUS-30072 console setup is also confirmed working.
Second-console joining and runtime restoration still require validation.
No EBOOT patch is needed.

Remaining work: PS3 hardware testing of compiled-RSX zones, second-console
joining/stock-map restoration, loading-screen title localization (the title
shown while the map is still loading lives in the UI/localized zones), and
regional UI profiles beyond the captured ones (the capture tool supports
adding them from an untouched ui_mp.ff).

## 22.2.9 runaway compiled-shader blob fails closed; sharper fault forensics

The 22.2.8 forensics did their job: re-run on mp_wmd_night, the emulated
loader reported the failing asset as **291 (TECHSET)** with the block-7 fault
model - and the elimination it enables points to the root cause. A single
asset load consumed ~12 MiB before faulting, and only one field in the whole
techset ABI can drive a stream read that large: the u32 blob-length in a
compiled shader root (argument counts are bytes, the pass count is a u16 -
at most ~1.5 MiB). So one compiled RSX shader blob was multi-megabyte: a
runaway translation. The console streams exactly that many bytes and then
dereferences the trailing data as its next structural pointer - the
`unmapped address 0x3f03` fault is the aftermath, not the cause.

* **Fail closed on the runaway blob.** The compiler now refuses any RSX
  shader blob past a 256 KiB ceiling (real IW3 programs are a few KiB; the
  test pipeline emits a few hundred bytes) with a `TechsetCompileError`. That
  is exactly the exception the sourcing ladder already catches, so the
  techset drops onto its substitution/omission path instead of serializing a
  length field that freezes the console. No owned techset with a poisoned
  length is ever written. Maps without a compiled owned techset - every map
  shipped working so far - are untouched.
* **Sharper forensics.** `faultscan` v2 leads with the real signal: the
  single largest recent stream read (the over-read) is surfaced first as the
  likely wrong size field, since the poisoned pointer that finally faults is
  only its aftermath. The block-7 census is now anchored at the asset ROOT
  (structural cells live there, not in the multi-MiB tail) and reports every
  offset relative to the asset root, and the exact fault-address match scan
  now covers the whole asset window uncapped.

Why this is the fix and not a guess: by elimination a 12 MiB single-asset
read can only be a blob-length field, and refusing to serialize an
impossible length is correct regardless of which translator path produced
it. The forensics remain the backstop that pins any future variant to an
exact `asset_root+0xNNN` cell. If a real (non-runaway) compiled techset
later needs its bytes debugged, that evidence now arrives in one run. The
underlying translator question - why one shader ballooned - is separate
quality work; this release makes shipping it impossible.

(`tests/test_shader_blob_ceiling.py`, `tests/test_linker_fault_forensics.py`;
suites 129 PASS / 3 SKIP / 0 FAIL including all GUI bridge tests.)

## 22.2.8 linker fault forensics: a MEMORY FAULT now names the poisoned bytes

mp_wmd_night - the first map that carries a COMPILED PC-to-RSX TechniqueSet
through the emulated native loader - ports cleanly (91/91 structural checks,
18/18 preflight) but faults during emulated linking after 291 assets:
`access to unmapped address 0x3f03`. The raw fault names neither the asset
nor the bytes, so this release makes the emulated loader diagnose itself:

* **The fault model.** PS3 packed pointers are `((block << 29) | offset) + 1`
  with blocks 0..6. The 3-bit block field admits exactly one invalid value -
  block 7, raw range 0xE0000000..0xFFFFFFFD (FOLLOWING/INSERT sit at its
  top) - and the console's block-base table has only seven entries, so a
  block-7 cell adds an out-of-table base and dereferences the bare offset:
  a low-page fault whose address IS the poisoned cell's offset field. The
  0x3f03 fault therefore pins the cell value to 0xE0003F04/0xE0003F03. No
  writer path produces such values (relocations, markers and packed encodes
  are all range-guarded), so the leak sits in a verbatim data copy that the
  console walks as a pointer - the forensics identify which one.
* **Fault forensics** (`tools/ps3_loader_emulator/faultscan.py`). On any
  load fault, the zone loader now reports: the failing asset (index, type,
  file window - recorded per asset all along, but never surfaced), the
  fault-address model (block-7 decode or out-of-range offset in a mapped
  block), a census of every block-7-space u32 in the failing asset's file
  window (marker-spill aware, 2 MiB cap for huge assets), exact matches
  between census cells and the values the fault address predicts, and the
  last 32 stream reads (file offset -> destination block). Everything goes
  into the console log AND `<zone>.link.json` (`fault_forensics`,
  `load_notes`) for both the map zone and the loading companion.
* **Self-diagnosis.** `error-autopsy.json` gained the class
  `linker/pointer-cell-fault` pointing at the forensics in the link report.

Re-running the mp_wmd_night link check on this version prints the failing
asset and the exact zone-file offsets of the poisoned cells; that evidence
drives the writer-side fix in the next release. Detection was never the gap -
the automatic native check caught the zone before any console test - this
closes the DIAGNOSIS gap in the same run.

(`tests/test_linker_fault_forensics.py`; suites 128 PASS / 3 SKIP / 0 FAIL
including all GUI bridge tests.)

## 22.2.7 loading-screen format authority; sun lighting, sky and ambient FX in the 3D preview

Triggered by the mp_osg_raid log (`IWI format dxt5 does not match PC load
format dxt1` at the very end of the port) plus the request to show effects,
weather, skybox and lighting in the 3D map view.

* **Loading-screen converter: the IWI is the pixel authority.** PC streams
  the loadscreen pixels from the IWI at runtime; the loadDef format byte in
  the fastfile is a stub. A format difference between the two is therefore
  metadata drift in the source map, not damage: `from_iwi` already
  serializes the IWI's own format and sizes, so the converter now accepts
  the mismatch and records `loadscreen_format_from_iwi:...` in
  `compatibility_fallbacks` instead of failing the whole port. Dimension
  and mip-count mismatches (a genuinely different image) still stop, and
  the error-autopsy catalog gained a `loadscreen/source-mismatch` entry for
  them. Also fixed: the autopsy artifact was listed twice in the GUI event
  stream.

* **Linked sun lighting.** `worldview.read_world` now reads the GfxWorld
  sunLight (`GfxLight` at root+0xD4: colour, direction) and the scene
  export derives `lighting` from it - sun colour, sun direction and a
  proportional ambient term. The desktop viewer builds its light rig from
  these values (the zone stores the direction toward the sun; the viewer
  negates it for WPF's travel direction) and keeps the historical neutral
  rig as fallback for scenes without a sun.

* **Sky rendering.** The sky surface list (`skySurfCount`/`skyStartSurfs`
  at root+0x24/0x28) resolves each sky surface's material; the scene export
  publishes the affected material groups as `sky_groups` and the viewer
  renders them unlit (black diffuse + emissive texture), so the skybox
  shows at full brightness instead of being shaded like level geometry.

* **Placed ambient FX and weather.** New `gui_fx_placements.py` reads the
  map's own GSC rawfiles out of linked memory - `maps/mp/<map>_fx.gsc`
  (`level._effect[...] = loadfx(...)` registrations, string-aware comment
  stripping, separator normalization) and `maps/createfx/<map>_fx.gsc`
  (`createLoopEffect`/`createOneshotEffect`/`createExploder` plus origin,
  angles and fxid overrides in statement order) - and writes
  `fx-placements.json`. Weather is exactly such placed loops (rain, snow).
  The viewer's new "Ambient FX" toggle plays every placed loop/oneshot at
  its real world position simultaneously, each looping on its own timeline
  (exploders are trigger-driven and stay out), within a preview budget
  (64 placements / 24k particles, skips reported). The scene cache now
  restores `fx-placements.json` alongside `fx-simulation.json`, so
  reopened scenes keep weather. Script execution is not emulated; the
  parser reads what createfx scripts declare.

* Verified: full backend suite 101 PASS / 14 SKIP / 0 FAIL (new
  `test_fx_placements.py`: registrations incl. backslash and duplicate
  separator normalization, the placement state machine, the exported
  document contract over a stub machine), GUI bridge tests 15/15, C#
  structural mirror audit (XAML parse, handler resolution across
  partials, brace balance, snake_case JSON contract on both sides).

## 22.2.6 material auto-quarantine and failure self-diagnosis

Two self-healing mechanisms so an asset-shaped failure fixes itself inside
the porter instead of needing a new porter build:

* **Material quarantine.** In runtime-compatible mode every per-material
  planning failure - constant/sampler contracts, platform state, image
  identity, and any FUTURE error class raised inside `plan_material` - no
  longer stops the conversion: the material is collected and rebound to the
  engine's own `$default` Material, the exact closure CP12-A boot isolation
  proved on hardware for every material at once, now applied one surface at
  a time.  The affected surface renders as the engine default; name, source
  index and the exact failure reason are reported
  (`material_graph.materials_quarantined_rows`, plus a port-log summary).
  Full ports (`runtime_compatible=false`) keep failing closed on the first
  error, unchanged.
* **Failure self-diagnosis.** Every failed job now writes
  `error-autopsy.json`: the exception, the porter-side stack frames, map/
  version/stage context, and a catalog classification with what the failure
  means and which knob addresses it (disk-full, unsupported techset, zone
  budget, missing support zones, parser evidence-model gaps, ...).  Unknown
  failures are recorded verbatim and stay hard stops: the catalog reacts
  only to PROVEN classes - inventing fixes for unproven failures is how
  silent PS3 freezes are made.

(`tests/test_material_quarantine.py`; suite 109 PASS / 5 SKIP / 0 FAIL.)

## 22.2.5 structurally certified GfxWorld image replay

First complete port of a previously impossible map in this series
(mp_bo2slums: 26 compiled/owned techsets, budget fit with 26 reduced
textures, 91/91 structural checks, 18/18 preflight). mp_osg_raid then
stopped with `GfxWorld absolute owner replay has fewer than two backward
topology proofs`: the block-4 owner replay certified its cursor solely
through packed backward edges inside the material-memory graph, and small
graphs simply do not OFFER two such edges.

The replay now carries a second, independent certifier: the structural
reader's per-field resolution (`structural_image_aliases`) maps every packed
image request straight to its GfxImage object through the reader's
allocation map — no cursor, no anchor, no quorum. The cursor replay remains
authoritative (its exact graph end feeds `draw_array_base`), but a graph
with fewer than two topology proofs is accepted when every replayed cell
identity matches the structural resolution (at least one matched cell;
any disagreement or missing coverage still fails closed). Maps with a full
topology quorum behave exactly as before; the comparison result is recorded
in the report (`structural_matches`/`structural_mismatches`), and the
shared-draw-model gate accepts either certification
(`tests/test_image_alias_structural.py`; suite 108 PASS / 5 SKIP / 0 FAIL).

## 22.2.4 structural-exact ClipMap brush identity

mp_salvage2 stopped with `brush edge range`: the ClipMap parser inferred the
block-4 bases of the brushSides/brushEdges arrays as the minimum over every
brush's packed pointers. One brush with no sides and all-zero per-axis
adjacency counts but a stale compiler pointer below the true array shifted
that base, and every HEALTHY brush then overflowed. The parser now resolves
each brush/leaf-brush-node/partition pointer field individually through the
structural reader's allocation map (the same evidence `planes` already
used), cross-checks the sequentially walked array starts against the
structurally resolved `brushsides`/`brushEdges`/`leafbrushes`/`borders`/
`brushes` root fields (any divergence is a precise `walk drift` stop), and
downgrades a stale edge pointer to the null edge ONLY on brushes whose six
adjacency counts are zero — the engine never dereferences it there, and the
writer serializes it as the array base exactly like a null edge. A stale
pointer on a brush that does use adjacency still fails closed. The DynEntDef
certification keeps its own inference inputs and simply declines when they
are unprovable. The min-inference remains only for callers without a
structural index (`tests/test_clipmap_structural_exact.py` replays the
poisoned-base failure, the exact-mode recovery, the strict case and the
drift stop; suite 107 PASS / 5 SKIP / 0 FAIL).

## 22.2.3 fail closed per effect, not per map

Goal of this release: an odd corner of ONE map must cost that corner, not the
whole conversion — no map-specific patches.

**FX runner root cause (mp_wmd_night).** The runner resolver modelled every
distinct packed alias of an owner as a NEW typed target and paired it
positionally with the immediately preceding FX run. But the linker
deduplicates assets by name: an alias an earlier owner already proved is a
REUSE of an existing block-4 cell and re-serializes no target. An owner mixing
a reused alias with new ones (`fx A` uses shared smoke, `fx B` uses the same
smoke plus a new spark) therefore mis-paired and died with `conflicting typed
targets`. The preceding-run model now applies to the new aliases only; reused
aliases must decode to exactly their proven cell, new cells must postdate
every proven cell (block-4 allocation is monotonic over the LoadStream), and
per-owner proofs commit atomically. The mp_nuked corridor replay and all
fail-closed rejections are unchanged.

**Unprovable FX route into omission.** Every remaining way an owner's packed
runner/impact evidence can be unprovable (broken corridors, packed visual
tables, missing typed targets) no longer stops the port: the resolver
collects the owner, and `plan_omissions` seeds it into the same transitive
per-effect omission as an unsupported shader — the effect and its exclusive
dependents are omitted and reported (`unresolvable_fx` in the report);
`--unsupported-fx-policy error` still stops instead. The assembler
re-verifies that no unprovable owner survived outside the omission set.

**Disk space.** The structural reader now receives the decompressed zone over
stdin instead of staging a second full copy on disk (a large map on a tight
drive failed whole conversions with ENOSPC), and an out-of-space error
anywhere in a job now reports plainly which file could not be written and
that the output and TEMP drives need space, instead of a bare traceback.

Suite: 106 test files PASS / 5 SKIP / 0 FAIL, all 15 GUI test files pass.
Hard stops remain only where the ZONE would be unsafe or the source is
structurally corrupt; everything asset-shaped degrades per asset and is
reported.

## 22.2.2 write-run isolation, sampler contracts and translator audit fixes

The second mp_gulag blocker: a port lays the zone out three times with fresh
writers (block-size measurement pass, then the two determinism builds), while
the owned-techset nodes shared one emission registry. Its dedup dicts survived
across runs, so the next run planned packed references to decl/shader symbols
only defined in the previous writer — `relocation target
'techset:...:wc_tools::decl....' was never defined`. The registry now resets
itself per write run (`TechsetEmissionRegistry.for_writer`, weakref-held), and
`test_main_ff.py` / `test_rsx_techset_pipeline.py` replay the full
measure-then-build sequence over owned techsets.

A full audit round (three independent reviewers plus a mechanical
write-idempotency sweep over every node type) then fixed, each with a
regression test where testable:

* **Owned sampler contracts.** The engine's texture scan has no end check
  either (mirrored in `materialcheck.py`): owned/donor techsets now also
  require every type-2 sampler nameHash in the material's texture table —
  critical for donor transplants, whose sampler set can exceed the material.
* **Translator:** `sincos` no longer clobbers its own source in the in-place
  form (dest == source, both stages); `cmp` selects through condition codes
  instead of `sge`+`lrp`, so a non-finite rejected operand (the `rcp`+`cmp`
  divide-guard idiom) no longer poisons the result with NaN; `log`/`logp`
  carry D3D9's `|x|` as an RSX absolute modifier, and the scalar RSX
  interpreter now models raw LG2 (NaN for negative input) so the differential
  would catch a missing modifier; vertex programs over 512 RSX slots and
  multi-row pixel code constants fail closed.
* **CgBinary:** the vertex `attributeOutputMask` is written in the documented
  cellGcm bit layout (COL0/COL1/BFC/FOG/PSIZE, TEX0-7 at bits 14-21, TEX8 at
  12) instead of a home-grown `1 << result` encoding; a new calibration rule
  (`vertex_attribute_output_mask_is_gcm_encoded`) verifies the layout against
  retail vertex programs on the next *Extract TechniqueSets + donors* run.
* **Technique identity:** argument tables and declarations (and donor flags)
  are folded into technique content keys, so two techniques differing only in
  constants can never merge into one emitted technique.
* **Compilation policy:** shared `,name` techsets and compiles yielding zero
  PS3 techniques are refused (the omission policy applies as before) instead
  of fabricating an empty owned techset; a corrupt donor-catalog entry now
  falls back to compilation for that techset and is recorded, instead of
  aborting the whole port.
* **Readback:** the LOADED_SOUND result prefix was a bare string (iterated
  per character — map-owned standalone sounds failed verification after a
  correct build); the prefix table is now shape-checked at import.
* **Preview/GUI:** FX simulation bounds raw spawn/element counts from linked
  memory (a corrupt looping element could stall the export unrecoverably) and
  budgets particles per effect; the scene cache stores `fx-simulation.json`
  so FX playback survives cache-hit reopens; the emulator names an
  empty/missing ELF path instead of a stray directory error; the FX playback
  timer stops on window close; donor transplants are labeled as such in the
  dependency manifest even without a material binding.

Suite: 106 test files PASS / 5 SKIP / 0 FAIL (24 differential translator
tests), all 15 GUI test files pass, C# verified by the structural mirror
audit. Compiled-RSX zones still await PS3 hardware testing.

## 22.2.1 fix: material-constant contracts for owned TechniqueSets

22.2.0 validated every material→TechniqueSet binding against the retail
name-keyed constant-contract catalog. A freshly compiled techset (for example
`wc_tools` on mp_gulag's `wc/caulk_shadow`) has no retail entry, so the port
failed closed with `missing constants unknown target contract` before the
compiled graph was ever serialized. Owned techsets — compiled from the map's
own PC shaders or transplanted from a retail donor — carry their complete
MaterialShaderArgument tables, so `bind_material_constants` now measures their
contract from the artifact itself: every type-0/6 argument's nameHash must be
present in the material's constant table, with the exact missing hashes named
on failure. Constants the translator dropped as unread never become
requirements. Non-owned bindings keep the retail catalog gate unchanged
(`tests/test_material_constants_owned.py`, suite 106 PASS / 5 SKIP / 0 FAIL).

## 22.2.0 general PC-to-RSX shader compilation

TechniqueSets without a native PS3 identity are no longer refused or pruned:
the porter parses the complete PC technique graph (techniques, passes, vertex
declarations, D3D9 SM2/SM3 shader bytecode, argument tables) and compiles it
into an owned PS3 TechniqueSet - RSX/NV40 vertex and fragment microcode in
Sony CgBinaryProgram containers, argument tables rebuilt for the PS3 register
model (vertex constants keep their `c#`, samplers keep their unit, pixel
constants become Cg parameter indices with runtime patch tables), and the
26-slot console technique layout derived from the PC 34-slot list minus the
hardware-instancing family. Instruction bit layouts mirror the vendored
IW4Studio decoder field for field, and the translator is proven by
differential execution: every supported construct runs through a D3D9
interpreter and the encoded RSX program and must agree (97 → 110 tests).

Sourcing order per TechniqueSet: native reference → donor transplant →
compiled → family substitution (policy `auto`; `prefer` also replaces
substitutions with faithful compiled graphs, `off` restores name binding
only). The Emulator's new *Extract TechniqueSets + donors* command links any
retail PS3 zone, walks every linked techset, verifies each compiler layout
assumption against that retail data (`rsx-calibration.json`: pixel `dest` =
Cg parameter index, patch sites hit inline-constant payloads, control bytes
equal the Cg descriptor, declaration shapes, register files) and exports a
donor catalog for byte-faithful transplants. Effect omission now applies only
when compilation itself fails and reports the exact blocking construct.

Compiled zones pass the writer's structural readback and the loader-shape
stream walk; they have NOT yet been started on PS3 hardware. Run the
calibration once against retail zones before console testing - it fails
closed with the exact rule if any layout assumption does not hold there.

## 22.2.0 RSX preview execution and particle simulation

The linked 3D preview now executes real RSX fragment programs in software:
for every material the selected technique's microcode is decoded and run
vectorized over texture space with the material's actual sampled images,
constants patched the way the console patches them (parameter defaults, then
material/literal/code arguments). The baked result - including shader alpha,
so foliage and effect transparency are visible - replaces the plain diffuse
preview; run-time-only values use reported neutral stand-ins and every
material lists its blockers in `material-rsx-report.json`. Vertex programs
are not executed for the bake (texture animation shows t=0).

FX assets get a deterministic particle simulation from the linked FxEffectDef
data (spawn cadence, lifetimes, origin ranges, first-interval velocity with
gravity, visual-state interpolation) exported as `fx-simulation.json`; the 3D
tab plays the timelines as camera-facing billboards at the selected entity.
Sprites only; models/lights/sounds/decals and emitted child effects are
listed as skipped. This is a preview simulation, not engine playback.

## 22.2.0 localization and regional support

Owned PC LocalizeEntry assets (MPUI names, map descriptions, script strings)
are now serialized into the generated zone, so those keys resolve on any
regional installation once the map is loaded - previously they were refused
outright. The loading-SCREEN title (shown before the map zone loads) still
comes from the UI/localized zones and remains open. Unknown ui_mp revisions
are still rejected fail-closed, and the rejection now names the capture tool
and workflow for adding a byte-exact profile for that revision.


## 22.1.22 structural reader

The PC map conversion entry point now requires a complete structural walk.
Discovery Night and its load file pass. The main map remains blocked by the
unsupported effect_spot shader family. Other asset layouts have schema definitions
but are not all validated against real files. Existing semantic conversion and
rendering limitations remain. No new PS3 startup test was performed.

## 22.1.23 effect omission

Unsupported PS3 shader dependencies can now omit the owning FX and parent effects.
Only exclusive dependencies are pruned; world/model users of unsupported shaders
still stop conversion. Known literal script requests are removed and map-owned
FX playback is guarded. Dynamic or unrecognized script consumers stop explicitly.
Discovery Night exports without its unsupported snow-wall/debris effects.
No general shader compiler, GSC execution or new PS3 hardware test is claimed.

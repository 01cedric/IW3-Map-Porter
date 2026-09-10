# IW3 PS3 FastFile Loader Emulator

A working emulator of Call of Duty 4's PlayStation 3 fastfile loader. It runs the real
`DB_Load*` code out of the game's EBOOT over a decompressed zone stream, so it answers the
question a generated zone otherwise only answers on hardware:

**does this zone load, and does it fit in the blocks it declares?**

It is not a model of the loader. It executes the loader.

## Why it exists

A PS3 fastfile is a single linear stream. Every asset is read in the order the loader walks
it, and the loader takes its lengths from counts inside the data it has already read. One
array written one element too long and the loader is off by that many bytes for the rest of
the file: the next asset's header is decoded from the middle of the previous asset, counts
come out as nonsense, and the loader asks the streaming layer for hundreds of megabytes that
will never arrive. On hardware that looks like a load screen that stops moving.

Nothing in a static checker catches this, because every individual structure is well formed.
The only reliable oracle is the loader itself.

## What it does

* `ppcemu.py` - a big-endian PowerPC64 interpreter. Integer subset only (the `DB_Load*` code
  uses no floating point), sparse page-table memory, ~60 opcodes.
* `zoneload.py` - the DB stream machine around it: maps the EBOOT's segments, allocates the
  seven zone blocks at synthetic bases, initialises the loader state at `0x10605D00`, and
  drives `Load_XAssetList` -> `Load_XAsset` for every asset in the zone. `DB_LoadXFileData`
  is the only I/O primitive, and it is backed by the zone bytes, so the emulator's file
  cursor **is** the loader's.
* Hardware-facing leaf calls (RSX buffer creation, shader upload, texture registration,
  script string interning, asset-table linking) are stubbed. None of them consume stream
  bytes; the validation below is what proves the stub set is complete.

## Validation

Run against Retail PS3 zones, the emulator consumes the stream to the exact declared end and
reproduces every declared block size:

| zone | assets | stream end | blocks 1-6 declared vs used |
|---|---|---|---|
| Retail `mp_shipment` | 445 | `0x543EF64` = header word 0 + 36 | exact on all six |
| Retail `mp_crash` | 695 | `0x7FE8C9F` = header word 0 + 36 | exact on all six |

Block 0 (TEMP) comes out 16 bytes under the declared size on both, which is the linker's own
rounding.

## Usage

```
export IW3_PS3_ELF=/path/to/eboot.elf        # decrypted EBOOT, ELF form
python3 runemu.py zone.bin                   # a *decompressed* zone (not the .ff)
```

`zone.bin` is the inflated stream: the 36-byte zone header followed by the asset stream. The
porter's `cod4porter.backend.v4_ffio.read_ps3_fastfile(path).zone` produces exactly that.

Output on success:

```
OK assets 255 pos 0x1adb57b / 0x1ae0000
```

and a `zone.bin.emu.json` next to the input with, per asset, its type, its byte range in the
stream, and how much of each block it consumed - plus the high-water mark of every block.

On failure it reports the last asset that completed, the file offset where the loader went
wrong, and the call that failed. That is normally enough to name the structure.

## Stage 1 - the link phase (`dbworld.py`, `linkcheck.py`)

The stream loader answers "does it load". The link phase answers "what does the game make
of it": every asset goes through the real `DB_LinkXAssetEntry` / `DB_AddXAsset`, into the
real asset pools with the real capacities, `,name` externals are resolved against the real
always-loaded zones, duplicates go through the real `Redundant asset` pass, and afterwards
the real `DB_FindXAssetHeader` is asked what `CM_LoadMap`, `Com_LoadWorld`, `R_LoadWorld`
and the game-world lookup would find. Every `Com_Error` / `Com_Printf` the DB code emits is
captured; a `Com_Error` is the text the console would show.

```
python3 extract_zone.py code_post_gfx_mp.ff zone_code_post_gfx_mp.bin   # from the Retail disc
python3 extract_zone.py ui_mp.ff            zone_ui_mp.bin
python3 extract_zone.py common_mp.ff        zone_common_mp.bin
python3 support_snapshot.py .                 # ~7 min once: links the three zones, writes support_db.snapshot
python3 linkcheck.py zone_mp_nuked.bin mp_nuked [--support DIR]
python3 linkcheck.py zone_mp_nuked_load.bin mp_nuked_load --no-world
```

`linkcheck.py` prints pool usage against the PS3 limits, the externals no loaded zone
provides (and whether the zone itself defines them later - the default-clone forward
reference the engine resolves in the redundant-asset pass), the redundant assets, the four
post-load lookups, the material -> technique-set bindings and every lookup the loader made,
and writes `<zone>.link.json`. This is what found the FX runner names (defect 13): the
engine looked up effects called `"\x10`\x1a\xbc"` because a runner visual was written as an
asset handle where the loader reads a name.

Emulated on the engine's behalf: `SL_GetStringOfSize` (a native interner that writes the
engine's 12-byte string-list entries so `SL_ConvertToString` runs unmodified), `strlen`,
critical sections, thread checks (single-threaded: we are the main thread) and the
render-sync calls around the redundant-asset pass. Zones are registered with the
`allocFlags` the engine uses (`0x1635D0`, `0x19E58C`, `0x905E8`), so the override rules
between zones are the real ones.

## Stage 2 - the linked world, read back from memory (`worldview.py`, `dumpworld.py`, ...)

After the link phase the map's GfxWorld and XModels sit in emulated memory exactly as the
game would see them - pointers converted by the game's own loader. `dumpworld.py` and
`dumpmodels.py` read them out (vertices, layer records, indices, surfaces, materials, static
model placements, MapEnts spawns; every XModel LOD surface with both vertex streams) and
the `compare_*.py` tools hold them against a reference:

```
python3 dumpworld.py  zone_mp_shipment.bin mp_shipment       # -> .world.npz + .meta
python3 dumpmodels.py zone_mp_shipment.bin mp_shipment       # -> .models.pkl
python3 compare_worlds.py   ours.world.npz retail.world.npz    # same map on both platforms
python3 compare_models.py   ours.models.pkl retail.models.pkl
python3 compare_pc_world.py ours.world.npz  mp_x.ff mp_x       # against the PC source zone
python3 render.py ours.world.npz out/prefix --views 3 --models ours.models.pkl
python3 dumpclip.py zone_mp_shipment.bin mp_shipment          # ClipMap -> .clip.pkl
python3 dumpclip.py --compare ours.clip.pkl retail.clip.pkl
```

`render.py` is a small z-buffer rasteriser: one image per view coloured by material name
(lit by the face normal), one by vertex colour, one by lightmap coordinate - taken from
the map's own spawn points. It draws what the data says, not what RSX would; its value is
that a wrong vertex layout, a wrong index base or a wrong placement matrix is visible at a
glance, and that two zones of the same map can be compared pixel by pixel.

Retail's merged decal materials (`*3n_10_4` ...) carry wider per-surface vertex layer
records (36..64 bytes); the reader derives each surface's stride from consecutive layer
offsets and compares attributes only where both sides use the plain 28-byte record.

## The other tools

* `shapediff.py our_zone.bin retail_zone.bin` - for a map that exists on both platforms,
  loads both and compares, per loaded array and per 4-byte lane, whether the lane is a
  pointer, a float, zero or a plain value. A lane that is a pointer in Retail and a number in
  ours (or four orders of magnitude apart) is a field-layout bug that the stream check cannot
  see, because it does not change any length. This is what found the `GfxBrushModel` field
  widths and the `Material.drawSurf` byte order.
* `stride.py <call site>` - recovers the per-element stride at a `Load_Stream` call site by
  symbolically evaluating how `r5` was computed from the element count in `r4`.
* `ppcdis.py <start> <end>` - disassembly with TOC-relative loads annotated with the string
  or address they resolve to. (Named so it does not shadow the standard library's `dis`,
  which numpy imports.)
* `fn.py <entry>` - disassembles one whole function, with automatic end detection.
* `runtime_nop.txt` - the stubbed leaf functions, one address per line. If a new zone reaches
  a hardware call that is not in this list the run stops with `UnknownCall` and names the
  boundary; add it only after checking the function consumes no stream.

## Limits

* Stage 1 and 2 need the three always-loaded Retail zones and ~1 GB of RAM for the snapshot.
* The stubs are validated by the two Retail zones above. A zone containing asset types those
  two do not (menus, weapons, XAnim) may reach an unstubbed call.
* It proves that a zone *loads* and *fits*. It does not prove the content is right - that is
  what `shapediff.py` against a Retail zone of the same map is for.
* Block 0 is a stack: the emulator tracks its high-water mark through `DB_AllocStreamPos` and
  `DB_IncStreamPos` rather than at asset boundaries, so nested TEMP frames are accounted
  correctly.

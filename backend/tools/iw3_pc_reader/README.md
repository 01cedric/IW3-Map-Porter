# IW3 PC structural reader

Standalone Python reader for IW3 PC v5 FastFiles. It starts at XAssetList and
follows schema-defined roots, field order, pointer arrays, unions and nested
allocations. It tracks the nine logical blocks separately from physical bytes,
including TEMP scopes, runtime allocations, INSERT aliases and packed pointers.
It does not locate assets by searching for names or byte signatures.

```
python reader.py input.ff --out inventory.json
```

The porter calls this separate program through a temporary zone file and a JSON
report. Normal map conversion requires a successful walk and writes
`<map>.pc-structure.json` before semantic conversion. Reader failure is reported
with the top-level asset index, field path and stream offset. There is no scanner
fallback in that conversion path. Older standalone diagnostic APIs without a
structural index retain their existing scanners.

## Source and license

Schema definitions and loading rules are adapted from OpenAssetTools by Jan
Laupetin and the OpenAssetTools contributors, commit
`9dca965366541504b71fa8cfb7ac049cb9b717e1`:
https://github.com/Laupetin/OpenAssetTools/tree/9dca965366541504b71fa8cfb7ac049cb9b717e1

Reference files: `src/Common/Game/IW3/IW3_Assets.h`,
`src/ZoneCode/Game/IW3/IW3_Commands.txt`, and its `XAssets` command files.
Loader semantics were checked against `ZoneLoadTemplate.cpp`,
`ContentLoaderIW3.cpp` and `ZoneInputStream.cpp` in that project.

Modified 2026-09-10: generated 32-bit C layout schema and an independent Python
load-stream interpreter, validation and JSON file interface, including typed
asset dependency edges and named effect references. These reader files,
tests and bundled definitions are distributed under GPL-3.0; see LICENSE. Full
corresponding source and generator inputs are included. No warranty.

Regenerate `schema.json` with `python generate_schema.py` (requires pycparser,
development only). Normal reading needs only the Python standard library.

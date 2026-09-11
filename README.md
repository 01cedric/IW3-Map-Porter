<p align="center">
  <img src="docs/assets/banner.svg" alt="IW3MapPorter" width="100%">
</p>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-22.2.9-62d6b3">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%20x64-0078d4">
  <img alt="Target" src="https://img.shields.io/badge/target-CoD4%20PS3-444444">
</p>

**IW3MapPorter converts Call of Duty 4 PC multiplayer maps into PS3 FastFiles.**
It includes a Windows desktop application, a Python conversion backend and tools
for inspecting maps and editing the in-game map menu.

## Basic workflow

1. Open the PC map `.ff`, its `_load.ff` and matching `.iwd` archives.
2. Add a PC texture library if the map uses textures from the base game.
3. Convert the map and review the reports. The tool creates PS3 `.ff` and `_load.ff` files.
4. Open a supported `ui_mp.ff` in **Map Menu Editor**, add the map and export the menu.
5. Install the exported FastFiles on your PS3, keeping backups of replaced files.

**Set loading picture** reads a ported PS3 `_load.ff`, resizes its image and embeds
it in the menu. IWD archives are PC inputs; they are not installed on the PS3.

## Structural PC reader

**PC → RSX shader compilation** is available: TechniqueSets without a native
PS3 identity are compiled from their D3D9 bytecode into owned PS3 TechniqueSets
with RSX microcode (see [status](docs/STATUS.md) for the proof chain and the
donor/calibration workflow). Effect omission now applies only to techsets the
compiler itself rejects, and the report names the blocking construct.
See [effect omission](docs/FX_OMISSION.md) for scope and limitations.

See [reader coverage](backend/tools/iw3_pc_reader/README.md) for tested scope.

## Included

| Status | Function |
|:---:|---|
| ✅ | PC → PS3 map and loading-screen conversion for supported assets. |
| ✅ | Automatic zone budgeting; selected textures shrink only when needed. |
| ✅ | FastFile readback, structural checks and diagnostic reports. |
| ✅ | Yellow Custom Maps submenu with names, descriptions and pictures. |
| ✅ | Up to 16 custom maps alongside the 16 stock maps; exported UIs can be reopened. |
| ✅ | General PC → RSX shader compilation with retail donor transplants and layout calibration. |
| ✅ | Owned LocalizeEntry serialization: MPUI/script strings resolve on any regional installation. |
| ⚠️ | 3D map/model previews, MapEnts editing and loader/linker tools; vertex-program animation shows its t=0 state. |
| ⚠️ | Exact reference/BLES and BLUS-30072 UI profiles; other revisions need matching profiles. |

## Still missing

- Loading-screen title localization: the title shown while the map is still
  loading lives in the UI/localized zones, outside the map pair.
- Universal game-region and executable-revision compatibility; new ui_mp
  revisions need a captured relocation profile (the tool guides you).
- Second-console joining and stock-map restoration testing.
- Foliage/effect fidelity on console for maps that previously relied on
  family substitutions: switch shader compilation to `prefer` to carry the
  faithful compiled graphs instead, then verify on hardware.

Missing source textures must be supplied separately. Passing file checks does not
guarantee that every map starts or renders correctly on a console.

## BLES / BLUS support

| Version / region | Support in 22.2.9 |
|---|---|
| BLES-00149 | ✅ Menu editing and custom online startup confirmed. |
| BLUS-30072 | ✅ Menu editing and custom online startup confirmed. |
| BLES-00115, BLES-00148, BLES-00154, BLES-00155, BLES-00156 | ⚠️ IDs are catalogued, but individual revisions have not all been validated. Only a matching captured UI is editable. |
| Other BLES / BLUS UI revisions | ❌ No editing support unless their UI matches an existing profile. Map compatibility is unverified. |
| BLJS-10031 / BLJM-60173 | ❌ No dedicated UI editing profiles; map compatibility is unverified. |
| Disc executables / update 1.40 | ⚠️ Metadata inspection is available; universal loader compatibility is not implemented. |

The editor checks the actual UI file, not just the title ID. Unknown revisions are
rejected. These limits concern UI editing and validation; they do not prove that
converted maps fail on every untested region. [Profile details](docs/REGIONAL_SUPPORT.md).

## Run or build

**Source:** open `IW3MapPorter.sln` in Visual Studio 2026 and the .NET 9 targeting components installed. The Windows Python
runtime, NumPy and Pillow are included.

## Credits

- **jakes625 / Jacob Schroeder** — RSX code from [IW4Studio](https://github.com/jacob-schroeder/IW4Studio).
- **OpenAssetTools contributors, including michaeloliverx** — the separately invoked [IWI wavelet converter](backend/tools/iwi_wavelet/README.md).
- **Python, NumPy, Pillow and .NET contributors** — dependencies.

Third-party licenses remain with their components and in [licenses](licenses).
Call of Duty belongs to its respective rights holders; this project is unofficial.

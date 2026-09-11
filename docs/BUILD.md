# Building IW3MapPorter

[Back to README](../README.md)

Use Windows x64, Visual Studio 2026 with .NET desktop development, and the .NET 9
SDK/targeting components. The solution targets net9.0 and net9.0-windows.
The source package includes Python 3.13.7, NumPy and Pillow under `runtime/python`;
keep that directory in place when building. No game data is needed to compile.
FFmpeg must be on PATH for conversion paths that use it.

From the repository root:

```powershell
dotnet build IW3MapPorter.sln -c Release
runtime\python\python.exe backend\tools\run_tests.py --timeout 120
runtime\python\python.exe -m unittest discover -s backend/gui_tests -v
dotnet tests/IW3MapPorter.IntegrationChecks/bin/Release/net9.0/IW3MapPorter.IntegrationChecks.dll . runtime/python/python.exe
```

Publish to a new or empty destination so stale backend files cannot be retained:

```powershell
dotnet publish src/IW3MapPorter.Desktop/IW3MapPorter.Desktop.csproj -c Release -r win-x64 --self-contained true -o artifacts/portable
runtime\python\python.exe tools\package_release.py --portable artifacts/portable
```

`tools/release.ps1` runs these build/test/package stages with an isolated staging
folder. It stops on errors. Generated releases are under `artifacts/releases`;
they include the executable, .NET runtime, Python backend and Python runtime.
Do not commit generated build directories or upload them as source.

## Tests with external fixtures

Tests that need game files skip when those inputs are absent. Run
`runtime\python\python.exe backend\tools\run_tests.py --help` for supported
fixture arguments. Getaway sound/clipmap regressions also accept
`IW3_GETAWAY_PC_FF` and `IW3_GETAWAY_PC_IWD`. Legacy self-tests accept
`--reference-root`; their default is `backend/reference/legacy`.
Keep original game data outside the repository. Fixture-dependent checks and
console tests are separate from a successful build.

## Repository contents

- `src`: C# desktop, viewer and application services.
- `backend`: Python conversion, UI export, reference profiles and tests.
- `runtime`: bundled Windows Python and dependencies, including their notices.
- `tests`: C# integration checks.
- `tools`: build, package and inspection scripts.
- `docs`: usage, implementation and compatibility notes.

`VERSION` controls application and package version numbers. Update it when
changing application behavior. Current release: 22.1.23.

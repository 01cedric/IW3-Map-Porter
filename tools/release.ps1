param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$ReleaseVersion = (Get-Content (Join-Path $ProjectRoot 'VERSION') -Raw).Trim()
if ($ReleaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'VERSION must contain major.minor.patch.' }
$PythonExe = Join-Path $ProjectRoot 'runtime/python/python.exe'
if (-not (Test-Path $PythonExe)) { throw 'Bundled Python is missing. Extract the complete source ZIP.' }
$StageRoot = Join-Path $ProjectRoot ('artifacts/staging/' + $ReleaseVersion + '-' + [guid]::NewGuid().ToString('N'))
$PortableRoot = Join-Path $StageRoot 'IW3MapPorter'
Push-Location $ProjectRoot
try {
    & dotnet build IW3MapPorter.sln -c Release -m:1 -nr:false
    if ($LASTEXITCODE -ne 0) { throw 'Solution build failed.' }
    & $PythonExe backend/tools/run_tests.py --timeout 120
    if ($LASTEXITCODE -ne 0) { throw 'Porter tests failed.' }
    & $PythonExe -m unittest discover -s backend/gui_tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Desktop Python tests failed.' }
    & dotnet tests/IW3MapPorter.IntegrationChecks/bin/Release/net9.0/IW3MapPorter.IntegrationChecks.dll $ProjectRoot $PythonExe
    if ($LASTEXITCODE -ne 0) { throw '.NET integration checks failed.' }
    & dotnet publish src/IW3MapPorter.Desktop/IW3MapPorter.Desktop.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=false -p:PublishTrimmed=false -o $PortableRoot -m:1 -nr:false
    if ($LASTEXITCODE -ne 0) { throw 'Windows publish failed.' }
    & $PythonExe tools/package_release.py --portable $PortableRoot
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
    Write-Host ('Version ' + $ReleaseVersion + ' archives are in artifacts/releases.')
}
finally { Pop-Location }

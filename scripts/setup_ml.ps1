# Install the standard six-detector runtime; CLIP remains optional.
[CmdletBinding()]
param([string]$PythonExe = 'python', [switch]$Clip)
$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$MlRoot = Join-Path $AppRoot 'detector_runtime'
$MlPython = Join-Path $MlRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $MlPython)) {
    & $PythonExe -c "import sys; assert (3,12) <= sys.version_info[:2] < (3,14), 'Use Python 3.12 or 3.13'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or 3.13 is required for the detector runtime.' }
    & $PythonExe -m venv (Join-Path $MlRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the detector virtual environment.' }
}
$Check = @'
import importlib.metadata as m
import sys
try:
    ready = all(m.version(p) == v for p, v in [('numpy', '2.5.4'), ('scipy', '1.18.1'), ('Pillow', '13.0.0')])
except m.PackageNotFoundError:
    ready = False
sys.exit(0 if ready else 1)
'@
& $MlPython -c $Check
if ($LASTEXITCODE -ne 0) {
    Write-Host '[ML] Installing six-detector dependencies...' -ForegroundColor Cyan
    & $MlPython -m pip install -r (Join-Path $MlRoot 'requirements-runtime.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Detector dependency installation failed. Check network access and retry.' }
}
if ($Clip) {
    Write-Host '[ML] Installing CLIP dependencies and verifying the bundled public encoder...' -ForegroundColor Cyan
    & $MlPython -m pip install -r (Join-Path $MlRoot 'requirements-clip.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Optional CLIP dependency installation failed.' }
    & $MlPython (Join-Path $AppRoot 'scripts\download_clip.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional CLIP asset download or SHA verification failed.' }
}
Write-Host '[ML] Six-detector runtime ready.' -ForegroundColor DarkGray

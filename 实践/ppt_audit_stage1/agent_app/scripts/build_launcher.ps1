# Build the lightweight EXE launchers. No Python runtime or application files are embedded.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Source = Join-Path $Root 'tools\launcher\SlideSentinelLauncher.cs'
$Icon = Join-Path $Root 'tools\launcher\app.ico'

$Candidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$Compiler = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Compiler) {
    throw 'C# compiler not found. Install .NET Framework 4.x developer components.'
}
foreach ($Path in @($Source, $Icon)) {
    if (-not (Test-Path $Path)) { throw "Missing launcher input: $Path" }
}

foreach ($Name in @('SlideSentinel.exe', 'SlideSentinel.Stop.exe')) {
    $Output = Join-Path $Root $Name
    & $Compiler /nologo /target:winexe /optimize+ /platform:anycpu /win32icon:"$Icon" /out:"$Output" "$Source"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Output)) {
        throw "Failed to build $Name"
    }
    $Item = Get-Item $Output
    Write-Host ("Built {0} ({1:N1} KB)" -f $Item.Name, ($Item.Length / 1KB)) -ForegroundColor Green
}

Write-Host 'The EXE files are launchers only; keep the complete source tree beside them.' -ForegroundColor Cyan

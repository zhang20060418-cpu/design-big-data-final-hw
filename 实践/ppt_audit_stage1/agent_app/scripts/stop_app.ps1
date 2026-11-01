# 停止 SlideSentinel 后端服务（端口 8000）
param([int]$Port = 8000)
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PidFile = Join-Path $Root '.run\backend.pid'
$stopped = $false

if (Test-Path $PidFile) {
    $saved = 0
    [void][int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$saved)
    if ($saved -gt 0) {
        try { Stop-Process -Id $saved -Force -ErrorAction Stop; $stopped = $true } catch { }
    }
    Remove-Item $PidFile -Force
}

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $procId = [int]$conn[0].OwningProcess
    try { Stop-Process -Id $procId -Force -ErrorAction Stop; $stopped = $true } catch { }
}

if ($stopped) { Write-Host "SlideSentinel 已停止（端口 $Port）。" -ForegroundColor Green }
else { Write-Host "没有发现正在运行的服务。" -ForegroundColor Yellow }

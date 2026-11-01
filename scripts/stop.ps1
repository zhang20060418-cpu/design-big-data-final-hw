# SlideSentinel 一键停止：读取 .run\backend.pid 并结束监听端口的进程，然后弹窗确认。
param(
    [int]$Port = 8000,
    [switch]$Quiet
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PidFile = Join-Path $Root '.run\backend.pid'

if (Test-Path $PidFile) {
    $saved = 0
    [void][int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$saved)
    if ($saved -gt 0) {
        try { Stop-Process -Id $saved -Force -ErrorAction Stop } catch { }
    }
    Remove-Item $PidFile -Force
}

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    try { Stop-Process -Id ([int]$conn[0].OwningProcess) -Force -ErrorAction Stop } catch { }
}

Start-Sleep -Milliseconds 600
$running = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/system/health" -UseBasicParsing -TimeoutSec 2
    $running = ($resp.StatusCode -eq 200)
} catch {
    $running = $false
}

try {
    if (-not $Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        if ($running) {
            [System.Windows.Forms.MessageBox]::Show("服务仍在运行（端口 $Port），请稍后重试。", 'SlideSentinel', 'OK', 'Warning') | Out-Null
        } else {
            [System.Windows.Forms.MessageBox]::Show('SlideSentinel 已停止。', 'SlideSentinel', 'OK', 'Information') | Out-Null
        }
    }
} catch {
}

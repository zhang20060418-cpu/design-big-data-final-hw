# SlideSentinel 一键启动：已在运行则直接打开工作台；未运行则拉起后端（隐藏窗口）后打开浏览器。
param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Url = "http://127.0.0.1:$Port/"
$Health = "http://127.0.0.1:$Port/api/system/health"

function Test-Health {
    try {
        $resp = Invoke-WebRequest -Uri $Health -UseBasicParsing -TimeoutSec 2
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-PortBusy {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return ($null -ne $conn)
    } catch {
        return $false
    }
}

function Show-Message([string]$Text, [string]$Title, [string]$Icon) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', $Icon) | Out-Null
    } catch {
    }
}

function Start-Backend {
    $python = Join-Path $Root 'backend\.venv\Scripts\python.exe'
    $backend = Join-Path $Root 'backend'
    if (-not (Test-Path $python)) {
        Show-Message "未找到 Python 环境：`n$python`n`n请先在仓库根目录运行一次 scripts\start_app.ps1 完成初始化。" 'SlideSentinel' 'Error'
        return $false
    }

    $runDir = Join-Path $Root '.run'
    New-Item -ItemType Directory -Force $runDir | Out-Null

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = "-m uvicorn app.main:app --host 127.0.0.1 --port $Port"
    $psi.WorkingDirectory = $backend
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    Set-Content -Path (Join-Path $runDir 'backend.pid') -Value $proc.Id -Encoding ascii

    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Health) { return $true }
        if ($proc.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Main {
    if (-not (Test-Health) -and (Test-PortBusy)) {
        for ($i = 0; $i -lt 12; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-Health) { break }
        }
    }

    if (-not (Test-Health)) {
        if (Test-PortBusy) {
            Show-Message "端口 $Port 已被其他程序占用，SlideSentinel 无法启动。`n请关闭占用该端口的程序，或先运行停止快捷方式。" 'SlideSentinel' 'Warning'
            return
        }
        if (-not (Start-Backend)) {
            Show-Message "后端未能在 20 秒内启动。`n请改用 scripts\start_backend.ps1 前台运行以查看具体错误。" 'SlideSentinel' 'Error'
            return
        }
    }

    if (-not $NoBrowser) {
        Start-Process $Url
    }
}

Main

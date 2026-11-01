# SlideSentinel 一键启动脚本（Windows PowerShell 5.1+）
# 用法：
#   .\scripts\start_app.ps1              构建并启动后端，自动打开浏览器
#   .\scripts\start_app.ps1 -Rebuild     强制重新构建前端
#   .\scripts\start_app.ps1 -Foreground  在当前窗口运行（Ctrl+C 停止）
#   .\scripts\start_app.ps1 -Stop        停止由本脚本启动的服务

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$Stop,
    [switch]$NoBrowser,
    [switch]$Rebuild,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$Python = Join-Path $Backend '.venv\Scripts\python.exe'
$RunDir = Join-Path $Root '.run'
$PidFile = Join-Path $RunDir 'backend.pid'
$OutLog = Join-Path $RunDir 'backend.out.log'
$ErrLog = Join-Path $RunDir 'backend.err.log'

function Get-PortOwner([int]$PortNumber) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction Stop
        if ($conn) { return [int]$conn[0].OwningProcess }
    } catch { }
    return 0
}

if ($Stop) {
    $stopped = $false
    if (Test-Path $PidFile) {
        $saved = 0
        [void][int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$saved)
        if ($saved -gt 0) {
            try { Stop-Process -Id $saved -Force -ErrorAction Stop; $stopped = $true } catch { }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    $owner = Get-PortOwner $Port
    if ($owner -gt 0) {
        try { Stop-Process -Id $owner -Force -ErrorAction Stop; $stopped = $true } catch { }
    }
    if ($stopped) { Write-Host "已停止 SlideSentinel（端口 $Port）。" -ForegroundColor Green }
    else { Write-Host "没有发现正在运行的服务（端口 $Port）。" -ForegroundColor Yellow }
    exit 0
}

# ---------------------------------------------------------------- 环境检查
if (-not (Test-Path $Python)) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        throw "未找到 Python。请安装 Python 3.11+ 并勾选 Add to PATH，然后重新运行。"
    }
    Write-Host "[1/3] 首次运行：创建 Python 虚拟环境并安装依赖（约 2-4 分钟）…" -ForegroundColor Cyan
    & python -m venv (Join-Path $Backend '.venv')
    if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败。" }
    & $Python -m pip install --upgrade pip --quiet
    & $Python -m pip install -r (Join-Path $Backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "安装后端依赖失败，请检查网络或代理。" }
}
else {
    Write-Host "[1/3] Python 环境就绪。" -ForegroundColor DarkGray
}

$sitePackages = Join-Path $Backend '.venv\Lib\site-packages'
$missing = $false
foreach ($pkg in @('fastapi', 'pptx', 'PIL', 'win32com', 'jinja2', 'httpx')) {
    if (-not (Test-Path (Join-Path $sitePackages $pkg))) { $missing = $true }
}
if ($missing) {
    Write-Host "检测到依赖缺失，正在补装…" -ForegroundColor Yellow
    & $Python -m pip install -r (Join-Path $Backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "后端依赖安装失败。" }
}

# ---------------------------------------------------------------- 前端构建
$Dist = Join-Path $Frontend 'dist\index.html'
$needBuild = $Rebuild.IsPresent -or (-not (Test-Path $Dist))
if (-not $needBuild) {
    $newest = Get-ChildItem (Join-Path $Frontend 'src') -Recurse -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest -and $newest.LastWriteTime -gt (Get-Item $Dist).LastWriteTime) { $needBuild = $true }
}

if ($needBuild) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) { throw "未找到 Node.js / npm。请安装 Node.js 20+ 后重试。" }
    Write-Host "[2/3] 构建前端（首次需要安装 npm 依赖）…" -ForegroundColor Cyan
    Push-Location $Frontend
    try {
        if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
            npm install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw "npm install 失败。" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败。" }
    }
    finally { Pop-Location }
}
else {
    Write-Host "[2/3] 前端构建已是最新。" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 端口检查
$owner = Get-PortOwner $Port
if ($owner -gt 0) {
    Write-Host "端口 $Port 已被进程 $owner 占用。" -ForegroundColor Yellow
    Write-Host "如果这是上一次启动的服务，请先运行： .\scripts\start_app.ps1 -Stop" -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------- 启动
Write-Host "[3/3] 启动后端服务…" -ForegroundColor Cyan
New-Item -ItemType Directory -Force $RunDir | Out-Null

if ($Foreground) {
    Write-Host ""
    Write-Host "  工作台：   http://127.0.0.1:$Port/" -ForegroundColor Green
    Write-Host "  API 文档： http://127.0.0.1:$Port/docs" -ForegroundColor Green
    Write-Host "  按 Ctrl+C 停止。" -ForegroundColor DarkGray
    Push-Location $Backend
    try { & $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port }
    finally { Pop-Location }
    exit 0
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = "-m uvicorn app.main:app --host 127.0.0.1 --port $Port"
$psi.WorkingDirectory = $Backend
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$proc = [System.Diagnostics.Process]::Start($psi)
Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii

$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-WebRequest "http://127.0.0.1:$Port/api/system/health" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    if ($proc.HasExited) { break }
}

if (-not $healthy) {
    Write-Host "后端启动失败。" -ForegroundColor Red
    Write-Host "请运行 .\scripts\start_backend.ps1 在前台查看具体错误。" -ForegroundColor Yellow
    exit 1
}

$url = "http://127.0.0.1:$Port/"
Write-Host ""
Write-Host "  SlideSentinel 已启动" -ForegroundColor Green
Write-Host "  工作台：   $url" -ForegroundColor White
Write-Host "  API 文档： http://127.0.0.1:$Port/docs" -ForegroundColor DarkGray
Write-Host "  停止服务： SlideSentinel.Stop.exe（或 .\scripts\stop_app.ps1）" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) {
    try { Start-Process $url } catch { Write-Host "请手动在浏览器打开：$url" -ForegroundColor Yellow }
}

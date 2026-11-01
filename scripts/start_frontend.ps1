# 启动 Vite 开发服务器（端口 5173），需要后端已在 8000 运行
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Frontend = Join-Path $Root 'frontend'

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) { throw "未找到 Node.js / npm，请安装 Node.js 20+。" }

if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
    Write-Host "安装前端依赖…" -ForegroundColor Cyan
    Push-Location $Frontend
    try { npm install --no-audit --no-fund } finally { Pop-Location }
}

try {
    $health = Invoke-WebRequest 'http://127.0.0.1:8000/api/system/health' -UseBasicParsing -TimeoutSec 2
    if ($health.StatusCode -ne 200) { throw 'not ready' }
} catch {
    Write-Host "提示：后端 8000 端口暂不可用，请先运行 .\scripts\start_backend.ps1" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  开发界面： http://127.0.0.1:5173/" -ForegroundColor Green
Write-Host "  后端接口： http://127.0.0.1:8000/docs" -ForegroundColor DarkGray
Write-Host "  按 Ctrl+C 停止。" -ForegroundColor DarkGray
Push-Location $Frontend
try { npm run dev } finally { Pop-Location }

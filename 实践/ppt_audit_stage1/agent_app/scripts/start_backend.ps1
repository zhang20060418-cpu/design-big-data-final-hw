# 仅启动后端（开发/调试用），前端请另开窗口运行 scripts\start_frontend.ps1
param([int]$Port = 8000)
& (Join-Path $PSScriptRoot 'start_app.ps1') -Port $Port -NoBrowser -Foreground

# Export selected slides to PNG (1920x1080, 144 px/in) with local PowerPoint.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File render_pages.ps1
# Source decks are opened read-only and without a window; nothing is written back.

$ErrorActionPreference = "Stop"

$base = Join-Path $PSScriptRoot "..\..\python-ppt\downloaded_pptx"
$outDir = Join-Path $PSScriptRoot "pages"
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

$jobs = @(
    @{ File = "AddAgendaSlide_TestA.pptx";     Slide = 3;  Out = "AddAgendaSlide_TestA_p3.png" },
    @{ File = "CalibriToTimesTestA.pptx";      Slide = 14; Out = "CalibriToTimesTestA_p14.png" },
    @{ File = "CenterAlignTitles_TestA.pptx";  Slide = 4;  Out = "CenterAlignTitles_TestA_p4.png" },
    @{ File = "CenterAlignTitles_TestA.pptx";  Slide = 5;  Out = "CenterAlignTitles_TestA_p5.png" }
)

$ppt = New-Object -ComObject PowerPoint.Application
try {
    Write-Output ("PowerPoint " + $ppt.Version + " build " + $ppt.Build)
    foreach ($job in $jobs) {
        $src = Join-Path $base $job.File
        $out = Join-Path $outDir $job.Out
        $pres = $ppt.Presentations.Open($src, $true, $false, $false)  # ReadOnly, Untitled, WithWindow
        try {
            $pres.Slides.Item($job.Slide).Export($out, "PNG", 1920, 1080)
        } finally {
            $pres.Close()
        }
        $size = (Get-Item $out).Length
        Write-Output ("[OK] " + $job.Out + "  " + $size + " bytes")
    }
} finally {
    $ppt.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($ppt) | Out-Null
}

param(
    [Parameter(Mandatory=$true)][string]$BenchmarkRoot,
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][ValidateSet('originals','controlled')][string]$Phase
)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
# Independent benchmark preparation only. No models, labels, or previous data.
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($BenchmarkRoot).TrimEnd('\','/')
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'BenchmarkRoot must exist.' }
function Local-Path([string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative)) { $p = [IO.Path]::GetFullPath($Relative) }
    else { $p = [IO.Path]::GetFullPath((Join-Path $root $Relative)) }
    if (-not $p.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes BenchmarkRoot: $Relative"
    }
    return $p
}
function Ensure-Parent([string]$Path) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
}
function Hash-File([string]$Path) {
    $stream=$null; $sha=$null
    try {
        $stream=[IO.File]::OpenRead($Path); $sha=[Security.Cryptography.SHA256]::Create()
        return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant()
    } finally { if ($null -ne $stream) { $stream.Dispose() }; if ($null -ne $sha) { $sha.Dispose() } }
}
function Release-Com($Object) {
    if ($null -ne $Object -and [Runtime.InteropServices.Marshal]::IsComObject($Object)) {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($Object)
    }
}
function Close-Owned($Presentation) {
    if ($null -ne $Presentation) {
        try { $Presentation.Saved = -1; $Presentation.Close() } catch {}
        Release-Com $Presentation
    }
}
function Box($Shape) {
    return [ordered]@{left_pt=[double]$Shape.Left; top_pt=[double]$Shape.Top;
        width_pt=[double]$Shape.Width; height_pt=[double]$Shape.Height}
}
function Font-Info($Shape) {
    $r = [ordered]@{name=$null; size_pt=$null; auto_size=$null; error=$null}
    try {
        $r.name = [string]$Shape.TextFrame.TextRange.Font.Name
        $r.size_pt = [double]$Shape.TextFrame.TextRange.Font.Size
        $r.auto_size = [int]$Shape.TextFrame.AutoSize
    } catch { $r.error = $_.Exception.Message }
    return $r
}
function Snapshot($Shape) {
    return [ordered]@{shape_id=[int]$Shape.Id; shape_name=[string]$Shape.Name;
        bbox=(Box $Shape); font=(Font-Info $Shape)}
}
function Select-Body($Slide, [double]$Width, [double]$Height) {
    # Return an integer index, never a COM shape via PowerShell's output pipeline.
    $best=0; $length=11; $script:bodySelectionErrors=@()
    $count=[int]$Slide.Shapes.Count
    for ($i=1; $i -le $count; $i++) {
        $s = $Slide.Shapes.Item($i)
        try {
            if ([int]$s.Visible -eq 0 -or [int]$s.HasTextFrame -eq 0) { continue }
            if ([int]$s.TextFrame.HasText -eq 0) { continue }
            if ([int]$s.Type -eq 14) {
                if (@(1,3,13,14,15,16) -contains [int]$s.PlaceholderFormat.Type) { continue }
            }
            if ($s.Width -le 0 -or $s.Height -le 0 -or $s.Left -ge $Width -or $s.Top -ge $Height) { continue }
            if ($s.Top -gt .90*$Height -and $s.Height -lt .10*$Height) { continue }
            $text = [string]$s.TextFrame.TextRange.Text
            $n = ($text -replace '\s','').Length
            if ($n -lt 12 -or $text -match '^\s*(copyright|all rights reserved|\u7248\u6743\u6240\u6709|\u7b2c\s*\d+\s*\u9875)') { continue }
            if ($s.Top -lt .22*$Height -and $s.Height -lt .12*$Height) { continue }
            if ($n -gt $length) { $best=$i; $length=$n }
        } catch { $script:bodySelectionErrors += ("shape ${i}: "+$_.Exception.Message) }
        finally { Release-Com $s }
    }
    return $best
}
function Modify-Slide($Slide, [string]$Variant, [double]$Width, [double]$Height) {
    $op = [ordered]@{operation_status='failed'; modification_type=$Variant;
        before=$null; after=$null; added_shapes=@(); selection_errors=@(); error=$null}
    $index=[int](Select-Body $Slide $Width $Height)
    $op.selection_errors=@($script:bodySelectionErrors)
    if ($index -eq 0) { $op.error='No eligible visible native body text shape with >=12 non-whitespace characters outside title/footer bands.'; return $op }
    $target=$Slide.Shapes.Item($index)
    try {
        $op.before = Snapshot $target
        $b = $op.before.bbox
        switch ($Variant) {
            'font_small' {
                $target.TextFrame.AutoSize = 0
                try { $target.TextFrame2.AutoSize = 0 } catch {}
                $target.TextFrame.TextRange.Font.Size = 8
                if ($target.TextFrame.TextRange.Font.Size -ne 8 -or $target.TextFrame.AutoSize -ne 0) { throw 'Font/autosize verification failed.' }
            }
            'occlusion' {
                $cover = $null
                try {
                    $cover=$Slide.Shapes.AddShape(1, [single]($b.left_pt+.10*$b.width_pt),
                        [single]($b.top_pt+.25*$b.height_pt), [single](.8*$b.width_pt), [single](.5*$b.height_pt))
                    $cover.Name='benchmark_opaque_occlusion'; $cover.Fill.Solid()
                    $cover.Fill.ForeColor.RGB=12632256; $cover.Fill.Transparency=0
                    $cover.Line.Visible=0; $cover.ZOrder(0)
                    $op.added_shapes += (Snapshot $cover)
                } finally { Release-Com $cover }
            }
            'clutter' {
                for ($j=1; $j -le 4; $j++) {
                    $range=$null; $copy=$null
                    try {
                        $range=$target.Duplicate(); $copy=$range.Item(1)
                        $copy.Name="benchmark_clutter_$j"
                        $copy.Left=[single][Math]::Max(0,[Math]::Min($Width-$b.width_pt,$b.left_pt+$j*.08*$b.width_pt))
                        $copy.Top=[single][Math]::Max(0,[Math]::Min($Height-$b.height_pt,$b.top_pt+$j*[Math]::Min(8,.06*$b.height_pt)))
                        $copy.ZOrder(0); $op.added_shapes += (Snapshot $copy)
                    } finally { Release-Com $copy; Release-Com $range }
                }
            }
            'overflow' { $target.Left=[single]($Width-.40*$b.width_pt) }
            default { throw "Unsupported variant: $Variant" }
        }
        $op.after=Snapshot $target
        $op.operation_status='modified_pending_human_review'
    } catch { $op.error=$_.Exception.Message } finally { Release-Com $target }
    return $op
}
function Export-Slide($Slide, [string]$DocumentId, [string]$Variant, [int]$Index, [double]$Width, [double]$Height) {
    $relative='renders/{0}/{1}/s{2:D4}.png' -f $DocumentId,$Variant,$Index
    $png=Local-Path $relative; $scale=1920/[Math]::Max($Width,$Height)
    $pw=[Math]::Max(1,[int][Math]::Floor($Width*$scale+.5))
    $ph=[Math]::Max(1,[int][Math]::Floor($Height*$scale+.5))
    $r=[ordered]@{png_path=$relative; render_status='failed'; requested_width=$pw;
        requested_height=$ph; png_width=$null; png_height=$null; png_sha256=$null; error=$null}
    $image=$null
    try {
        if (Test-Path -LiteralPath $png) { throw 'Refusing to overwrite existing PNG.' }
        Ensure-Parent $png; $Slide.Export($png,'PNG',$pw,$ph)
        $image=[Drawing.Image]::FromFile($png)
        $r.png_width=[int]$image.Width; $r.png_height=[int]$image.Height
        if ($image.Width -ne $pw -or $image.Height -ne $ph) { throw 'Decoded PNG size differs from proportional export request.' }
        $r.png_sha256=Hash-File $png
        $r.render_status='rendered_pending_visual_review'
    } catch { $r.error=$_.Exception.Message } finally { if ($null -ne $image) { $image.Dispose() } }
    return $r
}
function Open-Owned([string]$Path, [bool]$ReadOnly) {
    if ($preexistingPaths -contains $Path) { throw 'Refusing to reuse a presentation already open in PowerPoint.' }
    return $app.Presentations.Open($Path,$ReadOnly,$false,$false)
}
Add-Type -AssemblyName System.Drawing
$planFile=Local-Path $PlanPath
$plan=Get-Content -LiteralPath $planFile -Raw -Encoding UTF8 | ConvertFrom-Json
$entries=@($plan.documents)
if ($Phase -eq 'originals' -and $null -ne $plan.probes) { $entries += @($plan.probes) }
$seenIds=@{}
foreach ($d in $entries) {
    if ([string]$d.document_id -notmatch '^[A-Za-z0-9_-]+$') { throw 'Unsafe or empty document_id.' }
    if ($seenIds.ContainsKey([string]$d.document_id)) { throw 'Duplicate document_id in plan.' }
    $seenIds[[string]$d.document_id]=$true
    [void](Local-Path ([string]$d.original_path))
}
$fonts=New-Object Drawing.Text.InstalledFontCollection
$installed=@($fonts.Families | ForEach-Object { $_.Name } | Sort-Object -Unique); $fonts.Dispose()
$run=[ordered]@{schema_version='benchmark-native-com-v2'; phase=$Phase;
    started_at=[DateTimeOffset]::Now.ToString('o'); long_edge=1920;
    labels_generated=$false; existing_powerpoint_process=$false; existing_presentation_count=$null;
    powerpoint_version=$null; installed_font_families=$installed;
    font_check_limit='Referenced/installed font names do not prove absence of substitution; human visual review required.';
    documents=@(); warnings=@(); fatal_error=$null}
$app=$null; $pres=$null; $oldSecurity=$null; $securityChanged=$false; $preexistingPaths=@()
try {
    $run.existing_powerpoint_process=@(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
    $app=New-Object -ComObject PowerPoint.Application
    $run.powerpoint_version=[string]$app.Version; $run.existing_presentation_count=[int]$app.Presentations.Count
    for ($i=1; $i -le $app.Presentations.Count; $i++) {
        $existing=$app.Presentations.Item($i)
        try { $preexistingPaths += [string]$existing.FullName } finally { Release-Com $existing }
    }
    try { $oldSecurity=$app.AutomationSecurity; $app.AutomationSecurity=3; $securityChanged=$true }
    catch { $run.warnings += ('AutomationSecurity unavailable: '+$_.Exception.Message) }
    foreach ($d in $entries) {
        $id=[string]$d.document_id; $original=Local-Path ([string]$d.original_path)
        $doc=[ordered]@{document_id=$id; source_class=[string]$d.source_class; original_path=[string]$d.original_path;
            slide_count=$null; slide_width_pt=$null; slide_height_pt=$null; aspect_ratio=$null;
            referenced_fonts=@(); font_query_error=$null; variants=@(); pages=@(); error=$null}
        try {
            if (-not (Test-Path -LiteralPath $original -PathType Leaf)) { throw 'Copied original PPTX missing.' }
            $pres=Open-Owned $original $true
            $w=[double]$pres.PageSetup.SlideWidth; $h=[double]$pres.PageSetup.SlideHeight
            $doc.slide_count=[int]$pres.Slides.Count; $doc.slide_width_pt=$w; $doc.slide_height_pt=$h; $doc.aspect_ratio=$w/$h
            try { for ($i=1; $i -le $pres.Fonts.Count; $i++) { $doc.referenced_fonts += [string]$pres.Fonts.Item($i).Name } }
            catch { $doc.font_query_error=$_.Exception.Message }
            if ($Phase -eq 'originals') {
                for ($i=1; $i -le $pres.Slides.Count; $i++) {
                    $slide=$null
                    try {
                        $slide=$pres.Slides.Item($i)
                        $doc.pages += [ordered]@{slide_index=$i; native_slide_id=[int]$slide.SlideID;
                            shape_count=[int]$slide.Shapes.Count; render=(Export-Slide $slide $id 'original' $i $w $h)}
                    } catch { $doc.pages += [ordered]@{slide_index=$i; render_status='failed'; error=$_.Exception.Message} }
                    finally { Release-Com $slide }
                }
            }
            Close-Owned $pres; $pres=$null
            if ($Phase -eq 'controlled') {
                $indices=@($d.selected_slide_indices | ForEach-Object { [int]$_ } | Sort-Object -Unique)
                if ($indices.Count -eq 0) { $doc.variants=@(); $doc.error='No selected native body-text pages; controlled variants not generated.' }
                foreach ($variant in @('font_small','occlusion','clutter','overflow')) {
                    if ($indices.Count -eq 0) { break }
                    $relative="pptx/variants/${id}_${variant}.pptx"; $path=Local-Path $relative
                    $v=[ordered]@{variant_id=$variant; pptx_path=$relative; save_reopen_verified=$false;
                        pptx_sha256=$null; pages=@(); error=$null}
                    try {
                        if (Test-Path -LiteralPath $path) { throw 'Refusing to overwrite existing variant PPTX.' }
                        Ensure-Parent $path; Copy-Item -LiteralPath $original -Destination $path -ErrorAction Stop
                        $pres=Open-Owned $path $false
                        foreach ($i in $indices) {
                            $slide=$null; $p=[ordered]@{slide_index=$i; native_slide_id=$null; operation=$null; render=$null}
                            try {
                                if ($i -lt 1 -or $i -gt $pres.Slides.Count) { throw 'Selected slide index out of range.' }
                                $slide=$pres.Slides.Item($i); $p.native_slide_id=[int]$slide.SlideID
                                $p.operation=Modify-Slide $slide $variant $w $h
                            } catch { $p.operation=[ordered]@{operation_status='failed'; error=$_.Exception.Message} }
                            finally { Release-Com $slide }
                            $v.pages += $p
                        }
                        $pres.Save(); Close-Owned $pres; $pres=$null
                        $pres=Open-Owned $path $true
                        if ($pres.Slides.Count -ne $doc.slide_count -or [Math]::Abs($pres.PageSetup.SlideWidth-$w) -gt .01 -or [Math]::Abs($pres.PageSetup.SlideHeight-$h) -gt .01) { throw 'Saved/reopened presentation geometry or slide count changed.' }
                        $v.save_reopen_verified=$true
                        foreach ($p in $v.pages) {
                            if ($p.operation.operation_status -ne 'modified_pending_human_review') { continue }
                            $slide=$null
                            try { $slide=$pres.Slides.Item($p.slide_index); $p.render=Export-Slide $slide $id $variant $p.slide_index $w $h }
                            finally { Release-Com $slide }
                        }
                        $v.pptx_sha256=Hash-File $path
                    } catch { $v.error=$_.Exception.Message }
                    finally { Close-Owned $pres; $pres=$null }
                    $doc.variants += $v
                }
            }
        } catch { $doc.error=$_.Exception.Message }
        finally { Close-Owned $pres; $pres=$null }
        $run.documents += $doc
    }
} catch { $run.fatal_error=$_.Exception.ToString() }
finally {
    Close-Owned $pres
    if ($null -ne $app) {
        if ($securityChanged) { try { $app.AutomationSecurity=$oldSecurity } catch { $run.warnings += 'Could not restore AutomationSecurity.' } }
        if (-not $run.existing_powerpoint_process -and $run.existing_presentation_count -eq 0) {
            try { if ($app.Presentations.Count -eq 0) { $app.Quit() } } catch {}
        }
        Release-Com $app
    }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    $run.finished_at=[DateTimeOffset]::Now.ToString('o')
    $log=Local-Path ("operations_${Phase}.json")
    [IO.File]::WriteAllText($log,($run | ConvertTo-Json -Depth 25),(New-Object Text.UTF8Encoding($false)))
}
[pscustomobject]@{phase=$Phase; log_file=("operations_${Phase}.json"); document_count=$run.documents.Count; fatal_error_present=($null -ne $run.fatal_error)} | ConvertTo-Json -Compress
if ($null -ne $run.fatal_error) { exit 1 }

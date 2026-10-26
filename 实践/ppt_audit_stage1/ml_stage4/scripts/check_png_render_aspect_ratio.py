"""PowerPoint COM render checks using synthetic pages, never SlideAudit pages.

Only temporary probes beneath ml_stage4/.cache/png_layout_render are written.
The existing stages 1--3 rendering code is neither imported nor modified.
No pptx/pywin32 dependency is required: Windows PowerShell provides native COM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image


BASE = Path(__file__).resolve().parents[1]
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
COLORS = {
    "probe_circle": [255, 0, 255],
    "probe_rectangle": [0, 180, 210],
    "probe_left": [255, 0, 0],
    "probe_right": [0, 160, 0],
    "probe_top": [0, 0, 255],
    "probe_bottom": [255, 130, 0],
}


def proportional_export_size(width: int | float, height: int | float,
                             long_edge: int = 1920) -> tuple[int, int]:
    """Return integer export dimensions from actual PPTX/COM slide dimensions.

    Each short side uses nearest integer rounding, minimum one pixel. The page
    is never forced into 16:9. Rounding can introduce at most half a pixel.
    """
    if not width > 0 or not height > 0 or long_edge < 1:
        raise ValueError("Positive slide dimensions and long edge required")
    scale = long_edge / max(width, height)
    return max(1, int(width * scale + .5)), max(1, int(height * scale + .5))


POWERSHELL = r'''
param([Parameter(Mandatory=$true)][string]$ProbeDirectory,
      [Parameter(Mandatory=$true)][int]$LongEdge)
$ErrorActionPreference = 'Stop'
$app = $null
$pres = $null
$hadProcess = @(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
$existingCount = $null
$records = @()
function Add-ColoredShape($Slide, $Name, [int]$Kind,
                         [double]$X, [double]$Y, [double]$W, [double]$H,
                         [int]$R, [int]$G, [int]$B) {
    $shape = $Slide.Shapes.AddShape($Kind, [single]$X, [single]$Y,
                                  [single]$W, [single]$H)
    $shape.Name = $Name
    $shape.Line.Visible = 0
    $shape.Fill.Solid()
    $shape.Fill.ForeColor.RGB = $R + 256 * $G + 65536 * $B
    $shape.Fill.Transparency = 0
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shape)
}
try {
    $app = New-Object -ComObject PowerPoint.Application
    $existingCount = [int]$app.Presentations.Count
    foreach ($case in @(@{name='widescreen_16_9'; w=720; h=405},
                        @{name='standard_4_3'; w=720; h=540})) {
        $pres = $app.Presentations.Add(0)
        $pres.PageSetup.SlideWidth = [single]$case.w
        $pres.PageSetup.SlideHeight = [single]$case.h
        $slide = $pres.Slides.Add(1, 12)
        $slide.FollowMasterBackground = 0
        $slide.Background.Fill.Solid()
        $slide.Background.Fill.ForeColor.RGB = 16777215
        $w = [double]$pres.PageSetup.SlideWidth
        $h = [double]$pres.PageSetup.SlideHeight
        $s = 0.045 * [Math]::Min($w, $h)
        Add-ColoredShape $slide 'probe_circle' 9 (0.18*$w) (0.25*$h) (0.15*$h) (0.15*$h) 255 0 255
        Add-ColoredShape $slide 'probe_rectangle' 1 (0.50*$w) (0.40*$h) (0.26*$w) (0.10*$h) 0 180 210
        Add-ColoredShape $slide 'probe_left' 1 (0.005*$w) (0.45*$h) $s $s 255 0 0
        Add-ColoredShape $slide 'probe_right' 1 ($w-$s-0.005*$w) (0.45*$h) $s $s 0 160 0
        Add-ColoredShape $slide 'probe_top' 1 (0.45*$w) (0.005*$h) $s $s 0 0 255
        Add-ColoredShape $slide 'probe_bottom' 1 (0.45*$w) ($h-$s-0.005*$h) $s $s 255 130 0
        $pptx = Join-Path $ProbeDirectory ($case.name + '.pptx')
        $pres.SaveAs($pptx, 24)
        $scale = $LongEdge / [Math]::Max($w, $h)
        $pixelW = [Math]::Max(1, [int][Math]::Floor($w * $scale + 0.5))
        $pixelH = [Math]::Max(1, [int][Math]::Floor($h * $scale + 0.5))
        $png = Join-Path $ProbeDirectory ($case.name + '_proportional.png')
        $slide.Export($png, 'PNG', $pixelW, $pixelH)
        $records += [ordered]@{case=$case.name; mode='proportional'; pptx=$pptx;
          png=$png; slide_width_pt=$w; slide_height_pt=$h;
          requested_width=$pixelW; requested_height=$pixelH}
        if ($case.name -eq 'standard_4_3') {
            $legacy = Join-Path $ProbeDirectory ($case.name + '_fixed_1920x1080.png')
            $slide.Export($legacy, 'PNG', 1920, 1080)
            $records += [ordered]@{case=$case.name; mode='legacy_fixed_1920x1080';
              pptx=$pptx; png=$legacy; slide_width_pt=$w; slide_height_pt=$h;
              requested_width=1920; requested_height=1080}
        }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($slide)
        $pres.Close()
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pres)
        $pres = $null
    }
    $payload = [ordered]@{success=$true; powerpoint_version=[string]$app.Version;
      preexisting_powerpoint_process=$hadProcess;
      preexisting_presentation_count=$existingCount; records=$records}
    $payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $ProbeDirectory 'com_records.json') -Encoding UTF8
} catch {
    $payload = [ordered]@{success=$false; error=$_.Exception.ToString();
      preexisting_powerpoint_process=$hadProcess; preexisting_presentation_count=$existingCount;
      records=$records}
    $payload | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $ProbeDirectory 'com_records.json') -Encoding UTF8
    exit 1
} finally {
    if ($null -ne $pres) {
        try { $pres.Close() } catch {}
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pres)
    }
    if ($null -ne $app) {
        # Never quit a preexisting application or user presentation.
        if ((-not $hadProcess) -and ($existingCount -eq 0)) {
            try { if ($app.Presentations.Count -eq 0) { $app.Quit() } } catch {}
        }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
'''


def pptx_geometry(path: Path) -> dict:
    """Read dimensions and shape coordinates from the actual saved OOXML."""
    with zipfile.ZipFile(path) as archive:
        presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
        size = presentation.find("p:sldSz", NS)
        slide = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
    width, height = int(size.attrib["cx"]), int(size.attrib["cy"])
    shapes = {}
    for shape in slide.findall(".//p:sp", NS):
        name = shape.find("p:nvSpPr/p:cNvPr", NS).attrib["name"]
        transform = shape.find("p:spPr/a:xfrm", NS)
        offset, extent = transform.find("a:off", NS), transform.find("a:ext", NS)
        shapes[name] = [int(offset.attrib["x"]), int(offset.attrib["y"]),
                        int(extent.attrib["cx"]), int(extent.attrib["cy"])]
    return {"slide_width_emu": width, "slide_height_emu": height,
            "aspect_ratio": width / height, "shape_boxes_emu": shapes}


def analyze_record(record: dict) -> dict:
    geometry = pptx_geometry(Path(record["pptx"]))
    with Image.open(record["png"]) as image:
        image.load()
        rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    height, width, _ = rgb.shape
    sw, sh = geometry["slide_width_emu"], geometry["slide_height_emu"]
    measurements = {}
    all_boxes_match = True
    for name, color in COLORS.items():
        mask = np.all(np.abs(rgb - np.asarray(color, dtype=np.int16)) <= 3, axis=2)
        ys, xs = np.nonzero(mask)
        x, y, w, h = geometry["shape_boxes_emu"][name]
        expected = np.asarray([x/sw*width, y/sh*height,
                               (x+w)/sw*width, (y+h)/sh*height])
        if len(xs):
            observed = np.asarray([xs.min(), ys.min(), xs.max()+1, ys.max()+1])
            max_error = float(np.max(np.abs(expected-observed)))
            aspect = float((observed[2]-observed[0])/(observed[3]-observed[1]))
            shape_error = abs(aspect/(w/h)-1)
            boxes_match = max_error <= 2.5
            measurements[name] = {
                "color_rgb": color, "matching_pixel_count": int(len(xs)),
                "bbox_pixels_xyxy": observed.tolist(),
                "expected_bbox_using_output_scale_xyxy": expected.tolist(),
                "bbox_max_error_pixels": max_error,
                "observed_shape_aspect_ratio": aspect,
                "original_shape_aspect_ratio": w/h,
                "relative_shape_aspect_error": float(shape_error),
                "position_and_extent_match": bool(boxes_match),
            }
        else:
            boxes_match = False
            measurements[name] = {"matching_pixel_count": 0,
                                  "position_and_extent_match": False}
        all_boxes_match &= boxes_match
    circle_error = measurements["probe_circle"].get("relative_shape_aspect_error", float("inf"))
    rectangle_error = measurements["probe_rectangle"].get("relative_shape_aspect_error", float("inf"))
    aspect_relative_error = abs((width/height)/(sw/sh)-1)
    shapes_preserved = circle_error <= .02 and rectangle_error <= .02
    edge_present = all(measurements[n]["matching_pixel_count"] > 0
                       for n in ["probe_left", "probe_right", "probe_top", "probe_bottom"])
    return {**record, **geometry,
            "png_width": width, "png_height": height, "png_aspect_ratio": width/height,
            "output_aspect_relative_error": aspect_relative_error,
            "circle_and_rectangle_shape_preserved": bool(shapes_preserved),
            "all_four_edge_markers_present": bool(edge_present),
            "all_color_bboxes_match_full_page_mapping": bool(all_boxes_match),
            "stretch_detected": bool(not shapes_preserved),
            "crop_or_padding_or_offset_detected": bool(not all_boxes_match),
            "passed": bool(aspect_relative_error <= .001 and shapes_preserved
                           and edge_present and all_boxes_match),
            "png_sha256": hashlib.sha256(Path(record["png"]).read_bytes()).hexdigest(),
            "measurements": measurements}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=BASE / ".cache" / "png_layout_render")
    parser.add_argument("--long-edge", type=int, default=1920)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    allowed = (BASE / ".cache" / "png_layout_render").resolve()
    if output != allowed and allowed not in output.parents:
        raise ValueError("Probe output must remain under ml_stage4/.cache/png_layout_render")
    output.mkdir(parents=True, exist_ok=True)
    helper = output / "render_probe.ps1"
    helper.write_text(POWERSHELL, encoding="utf-8-sig")
    result = {"algorithm_version": "pptx-proportional-render-audit-v1",
              "status": "failed", "test_data": "synthetic PPTX only; no SlideAudit data",
              "interpreter": sys.executable, "long_edge": args.long_edge,
              "content_check": "Known-color circle, rectangle and four edge squares; decoded RGB masks compared to saved OOXML geometry",
              "bbox_tolerance_pixels": 2.5, "shape_aspect_relative_tolerance": .02,
              "legacy_stage2_render_rule": "Slide.Export(..., PNG, 1920, 1080); existing code unchanged"}
    run = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                          "-STA", "-ExecutionPolicy", "Bypass", "-File", str(helper),
                          "-ProbeDirectory", str(output), "-LongEdge", str(args.long_edge)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    records_path = output / "com_records.json"
    if records_path.exists():
        com = json.loads(records_path.read_text(encoding="utf-8-sig"))
        result["powerpoint_com"] = com
        if com["success"] and run.returncode == 0:
            result["renders"] = [analyze_record(r) for r in com["records"]]
            proportional = [r for r in result["renders"] if r["mode"] == "proportional"]
            result["status"] = "passed" if len(proportional) == 2 and all(r["passed"] for r in proportional) else "failed"
    if result["status"] != "passed":
        result["subprocess_returncode"] = run.returncode
        result["subprocess_stdout"] = run.stdout[-4000:]
        result["subprocess_stderr"] = run.stderr[-4000:]
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"status": result["status"], "result_json": str(output / "result.json"),
               "renders": [{key: record[key] for key in (
                   "case", "mode", "png_width", "png_height", "passed",
                   "stretch_detected", "crop_or_padding_or_offset_detected",
                   "all_four_edge_markers_present")}
                           for record in result.get("renders", [])]}
    if result["status"] != "passed":
        summary["error"] = result.get("powerpoint_com", {}).get("error", run.stderr[-2000:])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

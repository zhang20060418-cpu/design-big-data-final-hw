"""Isolated PowerPoint rendering worker.

Run as a subprocess so that a hung COM call can be terminated without taking
the API server down.  Only read-only access is used; the source file hash is
verified before and after rendering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pptx", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--long-edge", type=int, default=1920)
    args = parser.parse_args()

    pptx = Path(args.pptx).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": False,
        "engine": "powerpoint-com",
        "long_edge": args.long_edge,
        "slides": [],
        "error": None,
        "error_code": None,
        "powerpoint_version": None,
        "preexisting_powerpoint": False,
        "source_sha256_before": sha256(pptx),
        "source_sha256_after": None,
    }

    try:
        import win32com.client as win32
    except Exception as exc:  # pragma: no cover - environment dependent
        report["error"] = f"pywin32 不可用：{exc}"
        report["error_code"] = "pywin32_unavailable"
        _write(out_dir, report)
        return 2

    app = None
    pres = None
    owned_app = False
    try:
        import subprocess
        before = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"],
            capture_output=True, creationflags=0x08000000,
        ).stdout.decode("mbcs", errors="ignore").lower()
        report["preexisting_powerpoint"] = "powerpnt.exe" in before
        try:
            app = win32.DispatchEx("PowerPoint.Application")
            owned_app = True
        except Exception as exc:
            report["error"] = f"无法启动 PowerPoint：{exc}"
            report["error_code"] = "powerpoint_unavailable"
            raise
        try:
            report["powerpoint_version"] = str(app.Version)
        except Exception:
            pass
        try:
            app.AutomationSecurity = 3  # force-disable macros
        except Exception:
            pass
        pres = app.Presentations.Open(str(pptx), ReadOnly=True, Untitled=False, WithWindow=False)
        width_pt = float(pres.PageSetup.SlideWidth)
        height_pt = float(pres.PageSetup.SlideHeight)
        report["slide_width_pt"] = width_pt
        report["slide_height_pt"] = height_pt
        scale = args.long_edge / max(width_pt, height_pt)
        px_w = max(1, int(round(width_pt * scale)))
        px_h = max(1, int(round(height_pt * scale)))
        report["pixel_width"] = px_w
        report["pixel_height"] = px_h
        for index in range(1, pres.Slides.Count + 1):
            entry = {"index": index, "status": "failed", "file": None, "error": None}
            try:
                png = out_dir / f"s{index:04d}.png"
                pres.Slides(index).Export(str(png), "PNG", px_w, px_h)
                entry.update({
                    "status": "rendered",
                    "file": png.name,
                    "bytes": png.stat().st_size,
                    "sha256": sha256(png),
                })
            except Exception as exc:
                entry["error"] = str(exc)
            report["slides"].append(entry)
        report["ok"] = all(s["status"] == "rendered" for s in report["slides"])
        # Reuse this exact read-only COM presentation for native measurements.
        # Failure is a separate capability gap; successful PNG exports remain usable.
        try:
            from six_native import extract_native_contexts
            report["six_measurement"] = extract_native_contexts(
                pptx, pres, out_dir, report["slides"], report["powerpoint_version"] or "unknown",
            )
        except Exception as exc:
            report["six_measurement"] = {
                "status": "failed", "error": str(exc),
                "error_type": type(exc).__name__, "shared_render_COM_session": True,
            }
    except Exception as exc:
        report["error"] = str(exc)
        report["error_code"] = report["error_code"] or "render_failed"
        report["traceback"] = traceback.format_exc()[-2000:]
    finally:
        if pres is not None:
            try:
                pres.Close()
            except Exception:
                pass
        if app is not None and owned_app:
            try:
                if not report["preexisting_powerpoint"] and app.Presentations.Count == 0:
                    app.Quit()
            except Exception:
                pass
        try:
            report["source_sha256_after"] = sha256(pptx)
        except Exception:
            pass
        _write(out_dir, report)
    return 0 if report["ok"] else 1


def _write(out_dir: Path, report: dict) -> None:
    tmp = out_dir / "render_report.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out_dir / "render_report.json")


if __name__ == "__main__":
    sys.exit(main())

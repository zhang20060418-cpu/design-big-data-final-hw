"""Render service: drives the isolated PowerPoint worker and builds thumbnails."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import THUMB_LONG_EDGE

WORKER = Path(__file__).with_name("render_worker.py")


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, creationflags=0x08000000, timeout=15,
        )
    except Exception:
        pass


def render_pptx(pptx_path: Path, out_dir: Path, long_edge: int, timeout_s: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(exist_ok=True)
    cmd = [
        sys.executable, "-B", str(WORKER),
        "--pptx", str(pptx_path),
        "--out", str(out_dir),
        "--long-edge", str(long_edge),
    ]
    timed_out = False
    stdout = stderr = ""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=0x08000000,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            stdout, stderr = proc.communicate(timeout=30)
    except Exception as exc:
        return {"ok": False, "error": f"无法启动渲染进程：{exc}", "error_code": "spawn_failed", "slides": []}

    report_path = out_dir / "render_report.json"
    report: dict[str, Any] = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
    if timed_out:
        return {
            "ok": False,
            "error": f"渲染超时（超过 {timeout_s} 秒），已终止 PowerPoint 子进程。",
            "error_code": "render_timeout",
            "slides": [],
            "worker_stdout": stdout[-800:],
            "worker_stderr": stderr[-800:],
        }
    if not report:
        return {
            "ok": False,
            "error": "渲染进程未产生结果文件。",
            "error_code": "no_report",
            "slides": [],
            "worker_stdout": stdout[-800:],
            "worker_stderr": stderr[-800:],
        }

    slides: list[dict[str, Any]] = []
    for entry in report.get("slides", []):
        item = {
            "index": entry.get("index"),
            "status": entry.get("status", "failed"),
            "error": entry.get("error"),
            "width": None,
            "height": None,
            "png": None,
            "thumb": None,
        }
        if entry.get("status") == "rendered" and entry.get("file"):
            png_path = out_dir / entry["file"]
            try:
                with Image.open(png_path) as img:
                    item["width"], item["height"] = img.size
                    thumb = thumbs / f"s{int(entry['index']):04d}.jpg"
                    ratio = THUMB_LONG_EDGE / max(img.size)
                    img.convert("RGB").resize(
                        (max(1, round(img.size[0] * ratio)), max(1, round(img.size[1] * ratio))),
                        Image.LANCZOS,
                    ).save(thumb, quality=82, optimize=True)
                    item["thumb"] = thumb.name
                item["png"] = png_path.name
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = f"PNG 校验失败：{exc}"
        slides.append(item)

    return {
        "ok": bool(report.get("ok")),
        "error": report.get("error"),
        "error_code": report.get("error_code"),
        "slides": slides,
        "meta": {
            "engine": report.get("engine"),
            "long_edge": report.get("long_edge"),
            "pixel_width": report.get("pixel_width"),
            "pixel_height": report.get("pixel_height"),
            "powerpoint_version": report.get("powerpoint_version"),
            "preexisting_powerpoint": report.get("preexisting_powerpoint"),
            "source_sha256_before": report.get("source_sha256_before"),
            "source_sha256_after": report.get("source_sha256_after"),
            "source_unchanged": report.get("source_sha256_before") == report.get("source_sha256_after"),
            "six_measurement": report.get("six_measurement", {"status": "not_checked"}),
        },
    }

"""为第三阶段测试页生成与第二阶段同口径的文字可见性证据。

复用 `outputs/recheck/text_visibility_recheck.py` 中的 COM 探针、文字范围、
像素墨迹与判定函数，不改写已有模块；本文件只负责把测试页接进同一套证据格式。

输出：outputs/agent/stage3_evidence.json
      outputs/agent/evidence/*.png（标注图）
      outputs/agent/render/stage3/*.png（干净页面图，用于视觉复核）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RECHECK_MOD = ROOT / "outputs" / "recheck" / "text_visibility_recheck.py"
DECK = ROOT / "test_samples" / "stage3" / "stage3_visual_cases.pptx"
EXPECTED = ROOT / "test_samples" / "stage3" / "expected.json"
OUT = ROOT / "outputs" / "agent"
RENDER = OUT / "render" / "stage3"
EVIDENCE = OUT / "evidence"


def load_recheck_module():
    spec = importlib.util.spec_from_file_location("tvr_module", RECHECK_MOD)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {RECHECK_MOD}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tvr_module"] = module
    spec.loader.exec_module(module)
    return module


def severity_of(overflow: list[dict[str, Any]]) -> str:
    if not overflow:
        return "none"
    mx = max(x["distance_in"] for x in overflow)
    if mx <= 0.1:
        return "low"
    if mx <= 0.5:
        return "medium"
    return "high"


def main() -> int:
    tvr = load_recheck_module()
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    RENDER.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    probe = tvr.probe_deck(DECK, None, RENDER, force=True)
    if probe.get("errors"):
        print(json.dumps(probe["errors"], ensure_ascii=False, indent=2))
        return 1
    page = {"width_pt": probe["slide_width_pt"], "height_pt": probe["slide_height_pt"]}

    cases: list[dict[str, Any]] = []
    page_shapes: dict[str, list[dict[str, Any]]] = {}
    for slide_rec in probe["slides"]:
        idx = slide_rec["index"]
        shapes = slide_rec["shapes"]
        page_shapes[str(idx)] = [
            {
                "z": s["z"],
                "id": s["id"],
                "name": s["name"],
                "type": s["type"],
                "shape_type": s["shape_type"],
                "bbox_pt": {
                    "left_pt": s["left_pt"], "top_pt": s["top_pt"],
                    "width_pt": s["width_pt"], "height_pt": s["height_pt"],
                    "right_pt": round((s["left_pt"] or 0) + (s["width_pt"] or 0), 3),
                    "bottom_pt": round((s["top_pt"] or 0) + (s["height_pt"] or 0), 3),
                },
                "fill_visible": s["fill_visible"],
                "line_visible": s["line_visible"],
                "has_text": s["has_text"],
                "text_excerpt": (s["text"] or "")[:80],
            }
            for s in shapes
        ]
        for item in expected["cases"]:
            if item["slide"] != idx:
                continue
            shape = next((s for s in shapes if s["name"] == item["shape_name"]), None)
            if shape is None:
                raise RuntimeError(f"{DECK.name} p{idx} 找不到形状 {item['shape_name']}")
            geom = tvr.shape_geometry(shape)
            overflow = tvr.overflow_sides(geom, page["width_pt"], page["height_pt"])
            stage1_rec = {
                "directions": overflow,
                "severity": severity_of(overflow),
            } if overflow else None
            png = Path(slide_rec["png"])
            meta = {
                "case_id": item["case_id"],
                "group": "stage3_case",
                "source": "stage3_synthetic",
            }
            case = tvr.build_case(meta, shape, page, stage1_rec, png, DECK, idx)
            case["expected"] = {
                "second_pass": item["expected_second_pass"],
                "route": item["expected_route"],
                "why": item["why"],
            }
            case["clean_png"] = str(png)
            annot = EVIDENCE / f"{item['case_id']}_stage3_p{idx}_id{shape['id']}_annotated.png"
            case["annotation"].update(tvr.annotate_case(case, png, annot))
            case["annotation"]["clean_png"] = str(png)
            cases.append(case)

    stage1_style = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": {
            "name": "stage3_probe",
            "reuses": "outputs/recheck/text_visibility_recheck.py",
            "powerpoint": probe.get("powerpoint_version"),
        },
        "deck": str(DECK),
        "deck_sha256": probe["deck_sha256"],
        "page": page,
        "cases": cases,
        "page_shapes": page_shapes,
    }
    out = OUT / "stage3_evidence.json"
    out.write_text(json.dumps(stage1_style, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {out}")
    for c in cases:
        print(f"  {c['case_id']} p{c['slide']} id={c['shape_id']} "
              f"status={c['second_pass']['status']} overflow={c['page_overflow']['max_pt']}pt "
              f"bg_share={(c['render_evidence'].get('ink') or {}).get('bg_share')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

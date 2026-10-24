"""最小智能体调度器：结构检测 + 文字可见性复检 -> 路由 -> 按需视觉复核 -> 融合结论。

刻意保持为“可解释的规则调度”，不包装成自主规划：
- 每条规则有 id、触发条件、依据、决策；
- 只有 recheck 才会进入视觉复核；
- 复核按页去重并受最大次数限制；
- 没有证据时输出“待人工核实”，不推断为无缺陷。

输入（只读，不修改）：
  outputs/stage1_structure.json
  outputs/stage1_violations.json
  outputs/recheck/text_recheck.json
  outputs/agent/stage3_evidence.json   （由 stage3_probe.py 生成）

输出：
  outputs/agent/agent_findings.json
  outputs/agent/agent_run_log.json
  outputs/agent/agent_report.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from review_client import (BaseReviewer, HostEvidenceReviewer, MockReviewer,
                           OpenAICompatReviewer, build_prompt, summarize_calls)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
RECHECK = OUT / "recheck"
AGENT = OUT / "agent"
STAGE1_STRUCT = OUT / "stage1_structure.json"
STAGE1_VIOL = OUT / "stage1_violations.json"
RECHECK_JSON = RECHECK / "text_recheck.json"
STAGE3_JSON = AGENT / "stage3_evidence.json"
PROBE_DIR = RECHECK / "probe"
RULES_VERSION = "1.0"

FINAL_STATUS = {
    "confirmed": "确认问题",
    "possible": "可能有问题",
    "manual": "待人工核实",
    "clean": "未发现问题",
    "not_applicable": "不适用",
}
DECISION_CN = {"skip": "跳过", "report": "直接记录", "recheck": "视觉复核"}

ROUTING_RULES = [
    {
        "id": "R1", "decision": "skip", "name": "空形状跳过",
        "condition": "has_text=false 且 empty_graphics=true（无填充、无边框、无效果）",
        "rationale": "对象在渲染中不可见，文字可见性检查不适用；保留结构记录即可",
        "avoids_api": True,
    },
    {
        "id": "R2", "decision": "skip", "name": "无文字对象跳过",
        "condition": "has_text=false，但存在可见填充或边框",
        "rationale": "对象可见但不承载文字，本轮只审文字可见性，不产生文字缺陷结论",
        "avoids_api": True,
    },
    {
        "id": "R3", "decision": "report", "name": "已确认裁切直接记录",
        "condition": "second_pass=confirmed_clip 且 confidence=high",
        "rationale": "COM 文字范围越界 + 页边像素墨迹两项独立证据一致，无需再问模型",
        "avoids_api": True,
    },
    {
        "id": "R4", "decision": "recheck", "name": "不确定进入视觉复核",
        "condition": "second_pass=uncertain，或 confidence=low，或 needs_multimodal_review=true",
        "rationale": "规则证据不足或相互矛盾，交由视觉模型补充可观察证据",
        "avoids_api": False,
    },
    {
        "id": "R5", "decision": "recheck", "name": "上层不透明对象遮挡",
        "condition": "更高 z 序、填充可见的对象覆盖文字范围 ≥15%",
        "rationale": "页边裁切规则看不到遮挡；这是结构性可见性风险，需要看图确认",
        "avoids_api": False,
    },
    {
        "id": "R6", "decision": "recheck", "name": "图片背景可读性",
        "condition": "文字范围与图片重叠 ≥50%，且无页边裁切",
        "rationale": "无裁切不等于可读；文字压在复杂图片上属于人工判断不确定的情况",
        "avoids_api": False,
    },
    {
        "id": "R7", "decision": "report", "name": "无可见裁切直接记录",
        "condition": "second_pass=no_visible_clip、confidence=high，且无质量信号/遮挡/图片背景风险",
        "rationale": "COM 排版范围完整在页内，且墨迹不贴边，足以否定“页面裁切”",
        "avoids_api": True,
    },
    {
        "id": "R8", "decision": "report", "name": "未检查记录覆盖缺口",
        "condition": "coverage_status=not_checked（第二轮未检查该对象）",
        "rationale": "没有可见性证据，只能记录“待人工核实”，不得写成“未发现问题”",
        "avoids_api": True,
    },
]


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 任务一：覆盖统计
# --------------------------------------------------------------------------- #
def build_coverage(structure: dict[str, Any], recheck: dict[str, Any]) -> dict[str, Any]:
    page_cov = {(p["file"], p["slide"]): p for p in recheck["page_coverage"]["pages"]}
    candidate_objs = {(c["file"], c["slide"], c["shape_id"]): c for c in recheck["cases"]
                      if c["group"] == "real_case"}

    type_counts: Counter[str] = Counter()
    text_total = 0
    checked = 0
    pages: list[dict[str, Any]] = []
    not_checked: list[dict[str, Any]] = []

    for deck in structure["decks"]:
        if not deck.get("parse_ok"):
            continue
        for slide in deck["slides"]:
            texts = [s for s in slide["shapes"] if s.get("has_text")]
            cov = page_cov.get((deck["file"], slide["index"]))
            checked_ids = set(cov["checked_shape_ids"]) if cov else set()
            cands = [(k, v) for k, v in candidate_objs.items()
                     if k[0] == deck["file"] and k[1] == slide["index"]]
            for s in slide["shapes"]:
                type_counts[s["shape_type"]] += 1
            text_total += len(texts)
            checked += len([s for s in texts if s["shape_id"] in checked_ids])
            n_texts = len(texts)
            n_checked = len([s for s in texts if s["shape_id"] in checked_ids])
            missing = [s for s in texts if s["shape_id"] not in checked_ids]
            for s in missing:
                not_checked.append({
                    "file": deck["file"], "slide": slide["index"],
                    "shape_id": s["shape_id"], "name": s["name"],
                    "shape_type": s["shape_type"],
                    "coverage_status": "not_checked",
                    "reason": "第二轮只复检第一阶段越界候选，未对该对象做 COM 排版与像素核对",
                })
            if n_texts == 0:
                page_status = "complete"
                page_note = "本页无含文字对象，文字可见性检查没有待查项"
            elif n_checked == n_texts:
                page_status = "complete"
                page_note = None
            elif n_checked > 0:
                page_status = "partial"
                page_note = ("本页只有第一阶段越界候选做了 COM 排版与像素核对，"
                             "其余含文字对象未检查")
            else:
                page_status = "not_checked"
                page_note = ("本页含文字对象，但都未做 COM 排版与像素核对；"
                             "不能据此认为本页没有文字缺陷")
            pages.append({
                "file": deck["file"], "slide": slide["index"],
                "text_objects_total": n_texts,
                "stage2_candidate_objects": len(cands),
                "text_objects_checked": n_checked,
                "text_objects_not_checked": len(missing),
                "checked_shape_ids": sorted(checked_ids),
                "not_checked_shape_ids": [s["shape_id"] for s in missing],
                "coverage_status": page_status,
                "note": page_note,
            })

    pages_with_candidates = sum(1 for p in pages if p["stage2_candidate_objects"])
    pages_with_text_checked = sum(1 for p in pages if p["text_objects_checked"])
    page_status_counts = Counter(p["coverage_status"] for p in pages)
    pc = recheck["page_coverage"]["real_pages"]
    return {
        "scope": {
            "decks": len([d for d in structure["decks"] if d.get("parse_ok")]),
            "slides": sum(d["slide_count"] for d in structure["decks"] if d.get("parse_ok")),
            "shape_records": sum(len(s["shapes"]) for d in structure["decks"]
                                 for s in d["slides"]),
            "text_objects": text_total,
            "shape_types": dict(sorted(type_counts.items())),
            "geometry_excluded": {
                "PICTURE": type_counts.get("PICTURE", 0),
                "LINE": type_counts.get("LINE", 0),
                "GROUP": type_counts.get("GROUP", 0),
                "note": "第一阶段不检查图片/线条/组容器的外框；组成员会递归检查",
            },
        },
        "stage1_geometry": {
            "objects_with_geometry_checked": sum(
                len(s["shapes"]) for d in structure["decks"] for s in d["slides"]
            ) - type_counts.get("PICTURE", 0) - type_counts.get("LINE", 0)
            - type_counts.get("GROUP", 0),
            "text_objects_with_geometry_checked": text_total,
            "coverage": "全部可解析文字对象均做了对象外框越界检测（只看外框，不代表文字被裁切）",
        },
        "stage2_text_visibility": {
            "pages_inspected": pages_with_candidates,
            "pages_with_text_checked": pages_with_text_checked,
            "pages_total": sum(d["slide_count"] for d in structure["decks"] if d.get("parse_ok")),
            "text_objects_on_inspected_pages": pc["text_objects_total"],
            "text_objects_checked": pc["text_objects_checked"],
            "text_objects_skipped_on_inspected_pages": pc["text_objects_skipped"],
            "text_objects_checked_global": checked,
            "text_objects_not_checked_global": text_total - checked,
            "text_coverage_ratio": round(checked / text_total, 4) if text_total else 0.0,
            "page_status_counts": {
                "complete": page_status_counts.get("complete", 0),
                "partial": page_status_counts.get("partial", 0),
                "not_checked": page_status_counts.get("not_checked", 0),
            },
            "page_status_definition": {
                "complete": "本页全部含文字对象都做了第二轮复检；无文字对象的页面也计为 complete",
                "partial": "本页只有部分含文字对象做了第二轮复检",
                "not_checked": "本页含文字对象，但没有任何一个做过第二轮复检",
            },
            "coverage_statement": (
                f"第二轮不是全页文字扫描：只复检第一阶段 4 个越界候选；"
                f"4 个候选页共 {pc['text_objects_total']} 个含文字对象，"
                f"实际检查 {pc['text_objects_checked']} 个、跳过 {pc['text_objects_skipped']} 个；"
                f"全局 {text_total} 个文字对象中检查 {checked} 个"
                f"（{checked / text_total:.1%}），其余 {text_total - checked} 个为 not_checked。"
            ),
            "candidate_objects": len(candidate_objs),
            "candidate_text_objects": sum(1 for c in candidate_objs.values()
                                          if c["visibility"]["has_text"]),
            "candidate_not_applicable": sum(1 for c in candidate_objs.values()
                                            if c["second_pass"]["status"] == "not_applicable"),
        },
        "not_checked_policy": (
            "not_checked 只表示“没有做文字可见性检查”，不等于没有缺陷；"
            "本报告不会把未检查对象计入“未发现问题”。"
        ),
        "pages": pages,
        "not_checked_objects": not_checked,
        "not_checked_count": len(not_checked),
    }


# --------------------------------------------------------------------------- #
# 结构旁证：遮挡与图片背景
# --------------------------------------------------------------------------- #
def _bbox_from_probe(s: dict[str, Any]) -> dict[str, float]:
    left, top = s["left_pt"] or 0.0, s["top_pt"] or 0.0
    width, height = s["width_pt"] or 0.0, s["height_pt"] or 0.0
    return {"left_pt": left, "top_pt": top, "width_pt": width, "height_pt": height,
            "right_pt": round(left + width, 3), "bottom_pt": round(top + height, 3)}


def _bbox_from_evidence(s: dict[str, Any]) -> dict[str, float]:
    return s["bbox_pt"]


def _siblings_for_case(case: dict[str, Any], stage3: dict[str, Any] | None,
                       cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if case["group"] == "stage3_case" and stage3:
        raw = stage3["page_shapes"].get(str(case["slide"]), [])
        return [{"z": s["z"], "id": s["id"], "name": s["name"],
                 "shape_type": s["shape_type"], "fill_visible": s["fill_visible"],
                 "line_visible": s["line_visible"], "has_text": s["has_text"],
                 "bbox": _bbox_from_evidence(s)} for s in raw]
    path = case["deck_path"]
    if path not in cache:
        digest = sha256_file(Path(path))
        shapes: list[dict[str, Any]] = []
        for f in PROBE_DIR.glob("*.json"):
            data = load_json(f)
            if data.get("deck_sha256") != digest:
                continue
            for sl in data.get("slides", []):
                if sl["index"] != case["slide"]:
                    continue
                shapes = [{"z": s["z"], "id": s["id"], "name": s["name"],
                           "shape_type": s["shape_type"],
                           "fill_visible": s["fill_visible"],
                           "line_visible": s["line_visible"],
                           "has_text": s["has_text"],
                           "bbox": _bbox_from_probe(s)} for s in sl["shapes"]]
                break
            if shapes:
                break
        cache[path] = shapes
    return cache[path]


def _intersection(a: dict[str, float], b: dict[str, float]) -> float:
    w = min(a["right_pt"], b["right_pt"]) - max(a["left_pt"], b["left_pt"])
    h = min(a["bottom_pt"], b["bottom_pt"]) - max(a["top_pt"], b["top_pt"])
    return max(0.0, w) * max(0.0, h)


def occlusion_evidence(case: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
    box = case["text_layout"]["visible_bbox_pt"]
    if not box:
        return {"checked": False, "reason": "没有文字排版范围"}
    area = box["width_pt"] * box["height_pt"]
    if area <= 0:
        return {"checked": False, "reason": "文字范围为 0 面积"}
    z_text = case["visibility"]["z_order"]
    covers = []
    for s in siblings:
        if s["id"] == case["shape_id"] or s["z"] <= z_text:
            continue
        visible = (s["fill_visible"] not in (0, None)) or s["shape_type"] == "PICTURE"
        if not visible:
            continue
        ratio = _intersection(box, s["bbox"]) / area
        if ratio > 0.01:
            covers.append({"shape_id": s["id"], "name": s["name"], "z": s["z"],
                           "shape_type": s["shape_type"], "overlap_ratio": round(ratio, 4)})
    covers.sort(key=lambda x: x["overlap_ratio"], reverse=True)
    return {
        "checked": True,
        "basis": "COM z-order + Fill.Visible + 轴对齐外接框",
        "text_area_pt2": round(area, 1),
        "max_overlap_ratio": covers[0]["overlap_ratio"] if covers else 0.0,
        "covers": covers[:5],
        "threshold": 0.15,
        "note": "仅统计 z 序更高且填充可见的对象；重叠比例按文字可见范围面积计算",
    }


def picture_overlap_evidence(case: dict[str, Any],
                             siblings: list[dict[str, Any]]) -> dict[str, Any]:
    box = case["text_layout"]["visible_bbox_pt"]
    if not box:
        return {"checked": False, "reason": "没有文字排版范围"}
    area = box["width_pt"] * box["height_pt"]
    if area <= 0:
        return {"checked": False, "reason": "文字范围为 0 面积"}
    pics = []
    for s in siblings:
        if s["shape_type"] != "PICTURE":
            continue
        ratio = _intersection(box, s["bbox"]) / area
        if ratio > 0.01:
            pics.append({"shape_id": s["id"], "name": s["name"], "z": s["z"],
                         "overlap_ratio": round(ratio, 4)})
    pics.sort(key=lambda x: x["overlap_ratio"], reverse=True)
    return {
        "checked": True,
        "max_overlap_ratio": pics[0]["overlap_ratio"] if pics else 0.0,
        "pictures": pics[:5],
        "threshold": 0.5,
        "note": "文字范围与图片区域重叠比例；用于触发可读性复核，不直接判定缺陷",
    }


# --------------------------------------------------------------------------- #
# 任务二：路由
# --------------------------------------------------------------------------- #
def route_case(case: dict[str, Any], coverage_status: str, occ: dict[str, Any],
               pic: dict[str, Any]) -> dict[str, Any]:
    sp = case["second_pass"]
    vis = case["visibility"]
    status = sp["status"]
    conf = sp["confidence"]
    quality = sp.get("quality_signals") or []
    geo = case["geometry_evidence"]
    max_pt = case["page_overflow"]["max_pt"]

    def decision(rule_id: str, dec: str, trigger: str, reason: str,
                 avoided: bool, avoided_reason: str | None = None) -> dict[str, Any]:
        rule = next(r for r in ROUTING_RULES if r["id"] == rule_id)
        return {
            "decision": dec,
            "decision_cn": DECISION_CN[dec],
            "rule_id": rule_id,
            "rule_name": rule["name"],
            "trigger": trigger,
            "reason": reason,
            "evidence_inputs": [
                {"source": "stage1_geometry", "fact": geo.get("note") or "",
                 "frame_severity": geo.get("geometry_severity"),
                 "frame_max_overflow_pt": max(
                     [x["distance_pt"] for x in case["geometry_evidence"]["frame_overflow_sides"]],
                     default=0.0),
                 "visible_text_overflow_pt": max_pt},
                {"source": "stage2_visibility", "status": status, "confidence": conf,
                 "reasons": sp.get("reasons", []),
                 "page_overflow_pt": max_pt,
                 "edge_ink": (case.get("render_evidence", {}).get("ink") or {}).get("edge")},
                {"source": "occlusion_check", "ratio": occ.get("max_overlap_ratio"),
                 "checked": occ.get("checked")},
                {"source": "picture_overlap", "ratio": pic.get("max_overlap_ratio"),
                 "checked": pic.get("checked")},
            ],
            "api_call_planned": dec == "recheck",
            "api_call_avoided": avoided,
            "api_call_avoided_reason": avoided_reason,
        }

    if not vis["has_text"] and vis.get("empty_graphics"):
        return decision("R1", "skip", "has_text=false 且 empty_graphics=true",
                        "对象无文字、无填充、无边框、无可见效果，渲染不可见；"
                        "文字可见性检查不适用，保留结构记录即可", True,
                        "视觉复核不会产生任何文字证据")
    if not vis["has_text"]:
        return decision("R2", "skip", "has_text=false（存在可见图形）",
                        "对象不承载文字，本轮只审文字可见性；不产生文字缺陷结论", True,
                        "无文字可供核对")
    if status == "confirmed_clip" and conf == "high":
        return decision("R3", "report", "confirmed_clip + confidence=high",
                        "COM 文字范围越出页面且页边检出对应墨迹，两项独立证据一致",
                        True, "证据已足够明确，无需模型重复判断")
    if status == "uncertain" or conf == "low" or sp.get("needs_multimodal_review"):
        why = sp.get("unresolved_reason") or "；".join(sp.get("reasons", [])[:2])
        return decision("R4", "recheck",
                        f"status={status}, confidence={conf}, "
                        f"needs_multimodal={sp.get('needs_multimodal_review')}",
                        f"规则证据不足或相互矛盾：{why}", False)
    if occ.get("max_overlap_ratio", 0) >= 0.15:
        return decision("R5", "recheck",
                        f"上层对象遮挡比例 {occ['max_overlap_ratio']:.1%} ≥ 15%",
                        "文字虽未越出页面，但被更高 z 序的不透明对象覆盖；"
                        "页边裁切规则天然看不到这类问题", False)
    if pic.get("max_overlap_ratio", 0) >= 0.5 and max_pt <= 0:
        return decision("R6", "recheck",
                        f"文字与图片重叠 {pic['max_overlap_ratio']:.1%} ≥ 50%",
                        "无裁切不等于可读；文字整体压在图片上，需要看图判断对比度", False)
    if status == "no_visible_clip" and conf == "high" and not quality:
        return decision("R7", "report", "no_visible_clip + high + 无质量信号",
                        "COM 可见文字范围完整落在页内，且渲染墨迹不贴边；"
                        "按页面裁切定义不成立", True, "规则证据已足够否定页面裁切")
    if coverage_status == "not_checked":
        return decision("R8", "report", "coverage_status=not_checked",
                        "第二阶段未检查该对象，没有文字可见性证据；"
                        "只能记录覆盖缺口，不能写成未发现问题", True,
                        "无异常信号，按成本策略不主动复核；结论保持待人工核实")
    return decision("R8", "report", "无匹配规则，按覆盖缺口处理",
                    "现有证据不足以给出缺陷结论，保持待人工核实", True,
                    "不满足复核触发条件")


# --------------------------------------------------------------------------- #
# 任务四：融合
# --------------------------------------------------------------------------- #
def anomaly_level(case: dict[str, Any], occ: dict[str, Any],
                  pic: dict[str, Any] | None = None) -> str:
    geo_sev = case["geometry_evidence"].get("geometry_severity")
    max_pt = case["page_overflow"]["max_pt"]
    levels = {"none": 0, "low": 1, "medium": 2, "high": 3}
    score = levels.get(geo_sev, 0)
    if max_pt > 0:
        score = max(score, 3 if max_pt >= 18 else 2 if max_pt >= 6 else 1)
    if occ.get("max_overlap_ratio", 0) >= 0.4:
        score = max(score, 3)
    elif occ.get("max_overlap_ratio", 0) >= 0.15:
        score = max(score, 2)
    if pic and pic.get("max_overlap_ratio", 0) >= 0.5:
        score = max(score, 1)
    return {0: "none", 1: "low", 2: "medium", 3: "high"}[score]


def severity_for_clip(max_pt: float) -> str:
    if max_pt >= 18:
        return "high"
    if max_pt >= 6:
        return "medium"
    if max_pt > 0:
        return "low"
    return "none"


def _local_clip_direction(status: str) -> str | None:
    """本地第二阶段对“是否裁切”的方向性判断；uncertain/not_applicable 不表态。"""
    return {"confirmed_clip": "clip", "no_visible_clip": "no_clip"}.get(status)


def _ai_clip_direction(verdict: str) -> str | None:
    return {"visible_clip": "clip", "no_visible_problem": "no_clip"}.get(verdict)


def fuse(finding: dict[str, Any], ai_obj: dict[str, Any] | None,
         ai_error: str | None) -> dict[str, Any]:
    case = finding["_case"]
    sp = case["second_pass"]
    route = finding["route"]
    cov = finding["coverage_status"]
    occ = finding["occlusion_evidence"]
    max_pt = case["page_overflow"]["max_pt"]

    if cov == "not_checked":
        return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                "reliability": "low", "severity": "unknown",
                "reason": "第二阶段未检查该对象，没有文字可见性证据；待人工核实"}
    if route["decision"] == "skip":
        if not case["visibility"]["has_text"] and case["visibility"].get("empty_graphics"):
            detail = "不可见空形状（无文字、无填充、无边框、无可见效果）"
        else:
            detail = "对象不承载文字"
        return {"status": "not_applicable", "status_cn": FINAL_STATUS["not_applicable"],
                "reliability": "high", "severity": "none",
                "reason": f"{detail}，文字可见性检查不适用；不代表整页没有文字缺陷"}

    # 只有真的进入复核队列，才允许视觉证据影响结论。
    # 关键修正：不再在读取 ai_obj 之前按 confirmed_clip 直接定案。
    if route["decision"] == "recheck":
        if ai_error:
            return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                    "reliability": "low", "severity": "unknown",
                    "reason": f"视觉复核失败：{ai_error}"}
        if ai_obj is None:
            return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                    "reliability": "low", "severity": "unknown",
                    "reason": "需要视觉复核，但未调用或超出调用预算；不作缺陷推断"}
        verdict = ai_obj["verdict"]
        conf = ai_obj["confidence"]

        # 方向性冲突：本地说裁切/不裁切，视觉说相反。任何一侧置信度都不能单独定案。
        local_dir = _local_clip_direction(sp["status"])
        ai_dir = _ai_clip_direction(verdict)
        if local_dir and ai_dir and local_dir != ai_dir:
            return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                    "status_detail": "证据冲突，待人工核实",
                    "conflict": True,
                    "reliability": "low", "severity": "unknown",
                    "reason": (f"证据冲突：本地文字可见性判 {sp['status']}"
                               f"（confidence={sp['confidence']}），"
                               f"视觉复核判 {verdict}（confidence={conf}），"
                               "两者对“是否裁切”的方向相反；"
                               "不在冲突未解决时给出确认结论，转人工核实")}

        if verdict == "visible_clip":
            sev = severity_for_clip(max_pt) if max_pt > 0 else "medium"
            return {"status": "confirmed" if conf != "low" else "possible",
                    "status_cn": FINAL_STATUS["confirmed"] if conf != "low" else FINAL_STATUS["possible"],
                    "reliability": "high" if conf == "high" else "medium",
                    "severity": sev,
                    "reason": "视觉模型在截图中观察到文字被页面边界切断；"
                              f"与 COM 越界 {max_pt:.1f} pt 一致"}
        if verdict == "visible_occlusion":
            ratio = occ.get("max_overlap_ratio", 0)
            sev = "high" if ratio >= 0.4 else "medium" if ratio >= 0.15 else "low"
            return {"status": "confirmed" if conf != "low" else "possible",
                    "status_cn": FINAL_STATUS["confirmed"] if conf != "low" else FINAL_STATUS["possible"],
                    "reliability": "high" if conf == "high" else "medium",
                    "severity": sev,
                    "reason": "视觉模型确认文字被上层对象覆盖；"
                              f"结构侧重叠比例 {ratio:.1%}"}
        if verdict == "low_readability":
            return {"status": "possible", "status_cn": FINAL_STATUS["possible"],
                    "reliability": "medium" if conf != "low" else "low",
                    "severity": "low",
                    "reason": "文字未被裁切/遮挡，但视觉模型判断对比度或背景干扰影响阅读；"
                              "属于设计判断，需人工确认"}
        if verdict == "no_visible_problem":
            if conf == "low":
                return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                        "reliability": "low", "severity": "unknown",
                        "reason": "视觉模型认为未见问题但置信度低，规则侧仍有疑点"}
            return {"status": "clean", "status_cn": FINAL_STATUS["clean"],
                    "reliability": "medium", "severity": "none",
                    "reason": "视觉模型未观察到裁切/遮挡，规则侧疑点被视觉证据否定"}
        return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
                "reliability": "low", "severity": "unknown",
                "reason": f"视觉模型无法确定：{ai_obj.get('uncertainty') or '未给出原因'}"}

    # 规则直接闭环（report）：本地证据必须自身达到 high 才允许定案。
    if sp["status"] == "confirmed_clip" and sp["confidence"] == "high":
        sev = severity_for_clip(max_pt)
        return {"status": "confirmed", "status_cn": FINAL_STATUS["confirmed"],
                "reliability": "high", "severity": sev,
                "reason": f"COM 文字范围越出页面 {max_pt:.1f} pt，且页边检出对应墨迹"}
    if sp["status"] == "no_visible_clip" and sp["confidence"] == "high":
        return {"status": "clean", "status_cn": FINAL_STATUS["clean"],
                "reliability": sp["confidence"], "severity": "none",
                "reason": "COM 可见文字范围完整落在页内，墨迹不贴边"}
    return {"status": "manual", "status_cn": FINAL_STATUS["manual"],
            "reliability": "low", "severity": "unknown",
            "reason": "现有证据不足以给出结论"}


def suggestion_for(finding: dict[str, Any], final: dict[str, Any]) -> str | None:
    case = finding["_case"]
    evidence = (finding.get("ai_review") or {}).get("evidence") or {}
    verdict = evidence.get("verdict")
    if final["status"] == "confirmed" and (
            verdict == "visible_clip" or finding["stage2_visibility"]["status"] == "confirmed_clip"):
        return ("缩短文字或减小字号，或上移/缩小文本框，使可见文字距页面边缘至少 6 pt；"
                "同时检查自动缩放是否真正生效")
    if final["status"] == "confirmed" and verdict == "visible_occlusion":
        return "调整叠放次序，把正文置于顶层；或为文字让出空间，避免不透明形状压住正文"
    if final["status"] == "possible":
        return "提高文字与背景对比度（加深字色、加半透明底板或弱化背景图），并请设计者确认"
    if finding["coverage_status"] == "not_checked":
        return "纳入全页文字复检或人工抽查；在补齐证据前不要按“无缺陷”处理"
    if final["status"] == "clean" and case["visibility"]["empty_graphics"]:
        return None
    return None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_findings(cases: list[dict[str, Any]], stage3: dict[str, Any] | None) -> list[dict[str, Any]]:
    cache: dict[str, list[dict[str, Any]]] = {}
    findings = []
    for case in cases:
        siblings = _siblings_for_case(case, stage3, cache)
        occ = occlusion_evidence(case, siblings)
        pic = picture_overlap_evidence(case, siblings)
        cov_status = ("not_applicable" if case["second_pass"]["status"] == "not_applicable"
                      else "checked")
        route = route_case(case, cov_status, occ, pic)
        findings.append({
            "finding_id": f"F-{case['case_id']}",
            "case_id": case["case_id"],
            "group": case["group"],
            "file": case["file"],
            "slide": case["slide"],
            "shape_id": case["shape_id"],
            "shape_name": case["name"],
            "shape_type": case["shape_type"],
            "text_excerpt": " ".join((case["text_excerpt"] or "").replace("\r", "\n").split()),
            "coverage_status": cov_status,
            "coverage": {
                "stage2_checked_this_object": cov_status != "not_checked",
                "scope_note": ("第二轮只复检第一阶段越界候选；本对象是候选之一"
                               if case["group"] == "real_case"
                               else "第三阶段合成测试页，按同一套 COM/像素口径检查"),
            },
            "stage1_geometry": {
                "note": "第一阶段只判断对象外框越界，不代表文字被裁切",
                "frame_bbox_pt": case["geometry_evidence"]["frame_bbox_pt"],
                "overflow_sides": case["geometry_evidence"]["frame_overflow_sides"],
                "frame_overflow_pt": max(
                    [x["distance_pt"] for x in case["geometry_evidence"]["frame_overflow_sides"]],
                    default=0.0),
                "visible_text_overflow_pt": case["page_overflow"]["max_pt"],
                "geometry_severity": case["geometry_evidence"]["geometry_severity"],
                "in_stage1_result": (case["group"] == "real_case"
                                     and case["geometry_evidence"]["in_stage1_result"]),
            },
            "stage2_visibility": {
                "status": case["second_pass"]["status"],
                "status_cn": case["second_pass"]["status_cn"],
                "confidence": case["second_pass"]["confidence"],
                "reasons": case["second_pass"]["reasons"],
                "evidence_sources": case["second_pass"]["evidence_sources"],
                "visible_bbox_pt": case["text_layout"]["visible_bbox_pt"],
                "frame_exceed": case["text_layout"]["text_exceeds_inner_frame"],
                "quality_signals": case["second_pass"].get("quality_signals") or [],
                "needs_multimodal_review": case["second_pass"].get("needs_multimodal_review"),
                "bg_share": (case["render_evidence"].get("ink") or {}).get("bg_share"),
                "bg_reliable": (case["render_evidence"].get("ink") or {}).get("bg_reliable"),
                "edge_ink": (case["render_evidence"].get("ink") or {}).get("edge"),
                "probe_error": case["text_layout"].get("probe_error"),
            },
            "occlusion_evidence": occ,
            "picture_overlap_evidence": pic,
            "route": route,
            "_case": case,
        })
    return findings


def attach_ai(findings: list[dict[str, Any]], reviewer: BaseReviewer | None,
              max_calls: int, prompts_dir: Path | None = None) -> dict[str, Any]:
    pages: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        if f["route"]["decision"] == "recheck":
            pages.setdefault(f"{f['file']}#p{f['slide']}", []).append(f)

    records_by_case: dict[str, tuple[Any, str | None]] = {}
    skipped: list[dict[str, Any]] = []
    for page_key, items in pages.items():
        if reviewer is None:
            for f in items:
                skipped.append({"page": page_key, "case_ids": [f["case_id"]],
                                "reason": "未配置复核器（no reviewer）"})
            continue
        if not reviewer.can_call():
            for f in items:
                skipped.append({"page": page_key, "case_ids": [f["case_id"]],
                                "reason": "达到最大复核次数"})
            continue
        cases = [f["_case"] | {"agent_hint": f["route"]["reason"]} for f in items]
        image = Path(items[0]["_case"]["render_evidence"]["page_png"])
        with Image.open(image) as im:
            prompt = build_prompt(cases, items[0]["_case"]["page"]["width_pt"],
                                  items[0]["_case"]["page"]["height_pt"], im.width, im.height)
        if prompts_dir is not None:
            prompts_dir.mkdir(parents=True, exist_ok=True)
            safe = page_key.replace(".pptx", "").replace("#", "_")
            (prompts_dir / f"{safe}.txt").write_text(prompt, encoding="utf-8")
        rec = reviewer.review_page(page_key, cases, image, prompt)
        if rec is None:
            continue
        for f in items:
            records_by_case[f["case_id"]] = (rec, None)
        if rec.error or not rec.response:
            for f in items:
                records_by_case[f["case_id"]] = (rec, rec.error or "无有效响应")
            continue
        by_id = {int(o["shape_id"]): o for o in rec.response["objects"]}
        for f in items:
            records_by_case[f["case_id"]] = (rec, None)
            f["_ai_obj"] = by_id.get(f["shape_id"])

    for f in findings:
        if f["route"]["decision"] != "recheck":
            f["ai_review"] = None
            continue
        rec, err = records_by_case.get(f["case_id"], (None, "未进入复核队列"))
        obj = f.pop("_ai_obj", None)
        if rec is None:
            f["ai_review"] = {"called": False, "reason": err, "evidence": None}
        else:
            f["ai_review"] = {
                "called": rec.response is not None and rec.error is None,
                "call_id": rec.call_id,
                "mode": rec.mode,
                "model": rec.model,
                "simulated": rec.simulated,
                "image": rec.image,
                "error": err,
                "evidence": obj,
                "response_page_assessment": (rec.response or {}).get("page_assessment"),
                "suggestion": (rec.response or {}).get("suggestion"),
                "note": rec.note,
            }

    return {
        "pages_queued": len(pages),
        "pages_queued_detail": {k: [f["case_id"] for f in v] for k, v in pages.items()},
        "skipped": skipped,
        "reviewer_mode": reviewer.mode if reviewer else "none",
        "max_calls": max_calls,
    }


def decision_table(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for f in findings:
        rows.append({
            "finding_id": f["finding_id"],
            "file": f["file"], "slide": f["slide"], "shape_id": f["shape_id"],
            "name": f["shape_name"],
            "stage1": f["stage1_geometry"]["geometry_severity"],
            "stage2": f["stage2_visibility"]["status"],
            "coverage": f["coverage_status"],
            "decision": f["route"]["decision"],
            "rule": f["route"]["rule_id"],
            "trigger": f["route"]["trigger"],
            "ai": bool(f.get("ai_review") and f["ai_review"].get("called")),
            "final": f["final_status"],
            "severity": f["defect_severity"],
        })
    return rows


def build_report(result: dict[str, Any]) -> str:
    cov = result["coverage"]
    s2 = cov["stage2_text_visibility"]
    summ = result["summary"]
    ai = result["ai_review"]
    lines = [
        "# PPT 质量审查智能体 · 最小闭环报告",
        "",
        f"生成时间：{result['generated_at']}　规则版本：{result['agent']['rules_version']}　"
        f"复核模式：`{ai['mode']}`" + ("（模拟测试，不构成真实复核）" if ai["simulated"] else ""),
        "",
        "## 一、结论摘要",
        "",
        f"- 融合结论：{summ['statement']}",
        f"- 复核调用：{ai['vision_reviews']} 次视觉复核（宿主 {ai['host_vision_calls']} 次 / "
        f"第三方 API {ai['provider_api_calls']} 次），HTTP 请求 {ai['api_http_calls']} 次；"
        f"队列 {ai['pages_queued']} 页，成功复核 {ai.get('pages_reviewed_succeeded', 0)} 页"
        + (f"（另有 {ai['pages_reviewed_simulated']} 页为模拟）"
           if ai.get("pages_reviewed_simulated") else "")
        + f"，上限 {ai['max_calls']} 次。",
    ]
    real = ai.get("real_api_integration") or {}
    if real:
        lines.append(f"- 真实 API 联调：`{real.get('status')}`——{real.get('reason')}")
    if ai.get("notice"):
        lines.append(f"- 说明：{ai['notice']}")
    lines += [
        "",
        "| 最终状态 | 数量 |",
        "| --- | ---: |",
    ]
    for k in ("确认问题", "可能有问题", "待人工核实", "未发现问题", "不适用"):
        lines.append(f"| {k} | {summ['by_final_status'].get(k, 0)} |")
    lines += [
        "",
        "## 二、第二轮到底覆盖了多少对象",
        "",
        f"- 语料：{cov['scope']['decks']} 份 PPT、{cov['scope']['slides']} 页、"
        f"{cov['scope']['text_objects']} 个含文字对象。",
        f"- 第一阶段：对 {cov['stage1_geometry']['text_objects_with_geometry_checked']} 个文字对象"
        "做了对象外框越界检测（只看外框）。",
        f"- 第二阶段：只复检第一阶段 4 个越界候选，涉及 4 页；"
        f"这 4 页共有 {s2['text_objects_on_inspected_pages']} 个含文字对象，"
        f"实际检查 {s2['text_objects_checked']} 个、跳过 {s2['text_objects_skipped_on_inspected_pages']} 个。",
        f"- 全局文字可见性覆盖率：{s2['text_objects_checked_global']}/{cov['scope']['text_objects']}"
        f" = {s2['text_coverage_ratio']:.1%}；其余 {s2['text_objects_not_checked_global']} 个"
        "标记为 `not_checked`。",
        f"- 页面覆盖状态：complete {s2['page_status_counts']['complete']} 页 / "
        f"partial {s2['page_status_counts']['partial']} 页 / "
        f"not_checked {s2['page_status_counts']['not_checked']} 页"
        f"（共 {s2['pages_total']} 页）。`complete` 只表示本页待查文字都查过，"
        "不表示整份 PPT 查完。",
        "",
        "| 候选页 | 含文字对象 | 已检查 | 未检查 | 第二阶段候选对象 | 页面覆盖状态 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for p in [x for x in cov["pages"] if x["stage2_candidate_objects"]]:
        lines.append(f"| {p['file']} p{p['slide']} | {p['text_objects_total']} | "
                     f"{p['text_objects_checked']} | {p['text_objects_not_checked']} | "
                     f"{p['stage2_candidate_objects']} | {p['coverage_status']} |")
    p4 = next((x for x in cov["pages"]
               if x["file"] == "CenterAlignTitles_TestA.pptx" and x["slide"] == 4), None)
    if p4:
        lines += [
            "",
            f"> 第 4 页有 {p4['text_objects_total']} 个含文字对象，实际检查 "
            f"{p4['text_objects_checked']} 个、未检查 {p4['text_objects_not_checked']} 个；"
            "该页唯一的第二阶段候选是空形状（AutoShape 2），其 `not_applicable` 只说明"
            "“该对象没有文字”，不能推断整页文字没有裁切风险。",
        ]
    lines += [
        "",
        "> `not_checked` 只表示没有做文字可见性检查，不等于没有缺陷；"
        "本报告不会把未检查对象计入“未发现问题”。",
        "",
        "## 三、路由规则（按顺序命中，第一条命中即停止）",
        "",
        "| 规则 | 决策 | 触发条件 | 依据 | 是否省掉调用 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in result["routing_rules"]:
        lines.append(f"| {r['id']} {r['name']} | {DECISION_CN[r['decision']]} | "
                     f"{r['condition']} | {r['rationale']} | {'是' if r['avoids_api'] else '否'} |")
    lines += [
        "",
        "## 四、逐条决策",
        "",
        "| 案例 | 文件/页/对象 | 第一阶段 | 第二阶段 | 覆盖 | 决策 | 规则 | 最终状态 | 严重度 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["decisions"]:
        lines.append(
            f"| {row['finding_id']} | {row['file']} p{row['slide']} id={row['shape_id']} | "
            f"{row['stage1'] or '-'} | {row['stage2']} | {row['coverage']} | "
            f"{DECISION_CN[row['decision']]} | {row['rule']} | {row['final']} | {row['severity']} |"
        )
    lines += ["", "## 五、视觉复核记录", ""]
    if not ai["calls"]:
        lines.append("- 本次没有发起视觉复核。")
    else:
        for c in ai["calls"]:
            lines.append(f"### {c['call_id']} · {c['page']}"
                         + ("（模拟）" if c["simulated"] else ""))
            lines.append("")
            lines.append(f"- 模式：`{c['mode']}`；模型：`{c['model'] or '—'}`；"
                         f"HTTP：{c['http_status'] if c['http_status'] is not None else '—'}；"
                         f"耗时：{c['latency_ms'] if c['latency_ms'] is not None else '—'} ms")
            lines.append(f"- 图片：`{c['image']}`；prompt 指纹：`{c['prompt_sha256']}`")
            if c["usage"]:
                lines.append(f"- Token：prompt {c['usage'].get('prompt_tokens')} / "
                             f"completion {c['usage'].get('completion_tokens')} / "
                             f"total {c['usage'].get('total_tokens')}")
            else:
                lines.append("- Token：不可得（该模式不返回 usage）")
            if c["cost"]:
                lines.append(f"- 费用：{c['cost']}")
            else:
                lines.append("- 费用：不可得")
            if c["error"]:
                lines.append(f"- **错误**：{c['error']}")
            if c["response"]:
                if c["response"].get("page_assessment"):
                    lines.append(f"- 页面判断：{c['response']['page_assessment']}")
                for o in c["response"].get("objects", []):
                    lines.append(f"  - shape_id={o['shape_id']} → `{o['verdict']}`"
                                 f"（{o['confidence']}）：" + " ".join(o["observations"]))
            lines.append("")
    lines += ["## 六、融合结论与修改建议", ""]
    for f in result["findings"]:
        lines.append(f"### {f['finding_id']} · {f['file']} p{f['slide']} id={f['shape_id']}")
        lines.append("")
        lines.append(f"- 异常程度：`{f['anomaly_level']}`；证据可靠性：`{f['evidence_reliability']}`；"
                     f"最终缺陷严重度：`{f['defect_severity']}`")
        lines.append(f"- 路由：{f['route']['decision']}（{f['route']['rule_id']}）——{f['route']['reason']}")
        if f.get("ai_review") and f["ai_review"].get("called"):
            ev = f["ai_review"]["evidence"] or {}
            lines.append(f"- 视觉证据：`{ev.get('verdict')}`（{ev.get('confidence')}）："
                         + " ".join(ev.get("observations", [])))
            if ev.get("evidence_refs"):
                lines.append(f"- 证据位置：{'; '.join(ev['evidence_refs'])}")
        lines.append(f"- 最终状态：**{f['final_status']}**——{f['final_reason']}")
        if f.get("evidence_conflict"):
            lines.append("- 冲突标记：**证据冲突，待人工核实**（本地与视觉方向相反）")
        if f.get("suggestion"):
            lines.append(f"- 建议：{f['suggestion']}")
        lines.append("")
    lines += [
        "## 七、调度成本与未覆盖项",
        "",
        "| 指标 | 数量 | 说明 |",
        "| --- | ---: | --- |",
    ]
    sv = result["summary"]["api_savings"]
    real_n = sum(1 for f in result["findings"] if f["group"] == "real_case")
    synth_n = len(result["findings"]) - real_n
    lines += [
        f"| 候选对象 | {sv['candidate_objects_total']} | "
        f"{real_n} 条真实候选 + {synth_n} 条合成用例 |",
        f"| 规则直接闭环 | {sv['rule_closed_candidates']} | skip + report，未请求视觉复核 |",
        f"| 需要视觉复核 | {sv['recheck_candidates']} | 进入复核队列的候选对象 |",
        f"| 复核队列页数 | {sv['review_pages_queued']} | 按页去重 |",
        f"| 实际成功复核页数 | {sv['review_pages_succeeded']} | 有响应且无错误 |",
        f"| 未检查文字对象 | {sv['not_checked_objects']} | 覆盖缺口，**不计入节省的调用** |",
        "",
    ]
    for item in sv["details"]:
        lines.append(f"- {item}")
    lines += ["", "## 八、局限", ""]
    for item in result["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="PPT 质量审查智能体（最小闭环）")
    ap.add_argument("--reviewer", choices=["auto", "api", "mock", "host", "none"],
                    default="auto", help="auto=有 Key 用 api，否则 none")
    ap.add_argument("--max-calls", type=int, default=4, help="最多视觉复核页数")
    ap.add_argument("--host-evidence", type=Path,
                    default=AGENT / "host_review.json", help="host 模式的复核 JSON")
    ap.add_argument("--out-dir", type=Path, default=AGENT)
    args = ap.parse_args()

    started = time.time()
    out_dir = args.out_dir

    structure = load_json(STAGE1_STRUCT)
    violations = load_json(STAGE1_VIOL)
    recheck = load_json(RECHECK_JSON)
    stage3 = load_json(STAGE3_JSON) if STAGE3_JSON.exists() else None

    cases = [c for c in recheck["cases"] if c["group"] == "real_case"]
    if stage3:
        cases += stage3["cases"]

    coverage = build_coverage(structure, recheck)
    findings = build_findings(cases, stage3)

    reviewer: BaseReviewer | None
    if args.reviewer == "api":
        reviewer = OpenAICompatReviewer.from_env(max_calls=args.max_calls)
        if reviewer is None:
            print("错误：--reviewer api 需要环境变量 PPT_REVIEW_API_KEY（或 OPENAI_API_KEY）")
            return 2
    elif args.reviewer == "mock":
        reviewer = MockReviewer(max_calls=args.max_calls)
    elif args.reviewer == "host":
        if not args.host_evidence.exists():
            print(f"错误：host 模式需要复核证据 {args.host_evidence}")
            return 2
        reviewer = HostEvidenceReviewer(args.host_evidence, max_calls=args.max_calls)
    elif args.reviewer == "none":
        reviewer = None
    else:  # auto
        reviewer = OpenAICompatReviewer.from_env(max_calls=args.max_calls)

    out_dir.mkdir(parents=True, exist_ok=True)
    ai_meta = attach_ai(findings, reviewer, args.max_calls, out_dir / "review_prompts")
    for f in findings:
        ai_obj = (f.get("ai_review") or {}).get("evidence") if f.get("ai_review") else None
        ai_err = (f.get("ai_review") or {}).get("error") if f.get("ai_review") else None
        final = fuse(f, ai_obj, ai_err)
        f["final_status"] = final["status_cn"]
        f["final_status_code"] = final["status"]
        f["final_status_detail"] = final.get("status_detail") or final["status_cn"]
        f["final_reason"] = final["reason"]
        f["evidence_conflict"] = bool(final.get("conflict"))
        f["anomaly_level"] = anomaly_level(f["_case"], f["occlusion_evidence"],
                                           f["picture_overlap_evidence"])
        f["evidence_reliability"] = final["reliability"]
        f["defect_severity"] = final["severity"]
        f["suggestion"] = suggestion_for(f, final)

    for f in findings:
        f.pop("_case", None)

    by_final = Counter(f["final_status"] for f in findings)
    by_sev = Counter(f["defect_severity"] for f in findings)
    reviewed = sum(1 for f in findings if (f.get("ai_review") or {}).get("called"))
    recheck_n = sum(1 for f in findings if f["route"]["decision"] == "recheck")
    rule_closed = [f for f in findings if f["route"]["decision"] in ("skip", "report")]
    reviewed_pages_all = {(f["file"], f["slide"]) for f in findings
                          if (f.get("ai_review") or {}).get("called")}
    simulated_pages = {(f["file"], f["slide"]) for f in findings
                       if (f.get("ai_review") or {}).get("called")
                       and (f.get("ai_review") or {}).get("simulated")}
    reviewed_pages = reviewed_pages_all - simulated_pages
    savings = {
        "candidate_objects_total": len(findings),
        "rule_closed_candidates": len(rule_closed),
        "recheck_candidates": recheck_n,
        "review_pages_queued": ai_meta["pages_queued"],
        "review_pages_succeeded": len(reviewed_pages),
        "review_pages_simulated": len(simulated_pages),
        "review_pages_skipped": max(0, ai_meta["pages_queued"] - len(reviewed_pages_all)),
        "not_checked_objects": coverage["not_checked_count"],
        "not_checked_counted_as_savings": False,
        "details": [
            f"候选对象 {len(findings)} 个：规则直接闭环 {len(rule_closed)} 个，"
            f"进入视觉复核 {recheck_n} 个。",
            f"复核队列 {ai_meta['pages_queued']} 页，实际成功复核 {len(reviewed_pages)} 页"
            + (f"（另有 {len(simulated_pages)} 页为模拟，不计入成功复核）"
               if simulated_pages else "")
            + f"；按页去重，最多 {args.max_calls} 次。",
            f"{coverage['not_checked_count']} 个未检查文字对象不计入“节省的调用”："
            "它们是覆盖缺口，状态保持 not_checked / 待人工核实，"
            "不能用“省了调用”来解释。",
        ],
    }
    na_count = sum(1 for f in findings if f["final_status_code"] == "not_applicable")
    real_n = sum(1 for f in findings if f["group"] == "real_case")
    synth_n = len(findings) - real_n
    statement = (f"候选对象 {len(findings)} 条（真实语料 {real_n} 条 + 合成用例 {synth_n} 条）："
                 f"{by_final.get('确认问题', 0)} 条确认问题、"
                 f"{by_final.get('可能有问题', 0)} 条可能有问题、"
                 f"{by_final.get('待人工核实', 0)} 条待人工核实、"
                 f"{by_final.get('未发现问题', 0)} 条未发现问题、"
                 f"{by_final.get('不适用', 0)} 条不适用（无可见文字）；"
                 f"真实语料 {coverage['scope']['text_objects']} 个文字对象中只检查了 "
                 f"{coverage['stage2_text_visibility']['text_objects_checked_global']} 个，"
                 f"另有 {coverage['not_checked_count']} 个未检查对象保持 not_checked / 待人工核实，"
                 "不能据此宣称整套 PPT 无文字缺陷。")

    ai_summary = summarize_calls(reviewer.calls if reviewer else [], reviewer.mode if reviewer else "none")
    key_present = bool(os.environ.get("PPT_REVIEW_API_KEY")
                       or os.environ.get("OPENAI_API_KEY"))
    real_api_done = ai_summary["provider_api_calls"] > 0 and ai_summary["api_http_calls"] > 0
    ai_summary.update({
        "enabled": reviewer is not None,
        "mode": reviewer.mode if reviewer else "none",
        "model": reviewer.model if reviewer else None,
        "api_key_present": key_present,
        "pages_queued": ai_meta["pages_queued"],
        "pages_queued_detail": ai_meta["pages_queued_detail"],
        "pages_reviewed_succeeded": len(reviewed_pages),
        "pages_reviewed_simulated": len(simulated_pages),
        "max_calls": args.max_calls,
        "skipped": ai_meta["skipped"],
        "notice": ("模拟测试：未调用任何模型，不能宣称完成真实 AI 复核"
                   if (reviewer and reviewer.simulated) else
                   "宿主视觉工具复核：未配置 API Key，程序未发起 HTTP 请求；"
                   "Token/费用不可得，不等同于第三方 API 复核"
                   if (reviewer and reviewer.mode == "host") else None),
        "real_api_integration": {
            "status": "completed" if real_api_done else "not_completed",
            "api_key_present": key_present,
            "api_http_calls": ai_summary["api_http_calls"],
            "provider_api_calls": ai_summary["provider_api_calls"],
            "reason": ("已完成第三方多模态 API 调用，响应与用量见 calls"
                       if real_api_done else
                       "本机未配置 PPT_REVIEW_API_KEY / OPENAI_API_KEY，"
                       "未发起任何第三方 API 调用；真实 API 联调尚未完成"
                       if not key_present else
                       "检测到 API Key，但本次没有运行 api 模式，未发起第三方调用"),
            "command": ("PPT_REVIEW_API_KEY=<set> python agent_router.py --reviewer api "
                        "--max-calls 2 --out-dir outputs/agent/api_run"),
        },
    })

    if ai_summary["simulated"]:
        review_note = ("本次运行使用模拟复核（mode=mock），未读取图像、未调用任何模型，"
                       "只能验证调度链路，不能作为复核结论。")
    elif ai_summary["mode"] == "host":
        review_note = ("本次运行未配置 PPT_REVIEW_API_KEY，第三方 API 调用为 0；"
                       f"{ai_summary['vision_reviews']} 次复核由宿主多模态图像工具完成（mode=host），"
                       "Token 与费用不可得，不等同于第三方 API 复核。")
    elif ai_summary["mode"] == "api":
        review_note = (f"本次运行通过 API 完成 {ai_summary['api_http_calls']} 次调用，"
                       f"模型 `{ai_summary['model']}`；Token 用量见调用记录。")
    else:
        review_note = "本次运行未执行视觉复核。"

    result = {
        "generated_at": now_iso(),
        "agent": {
            "name": "ppt_quality_agent",
            "version": "0.1.0",
            "rules_version": RULES_VERSION,
            "description": "规则调度 + 按需视觉复核的最小闭环；不是自主规划智能体",
        },
        "inputs": {
            "stage1_structure": {"path": str(STAGE1_STRUCT), "sha256": sha256_file(STAGE1_STRUCT)},
            "stage1_violations": {"path": str(STAGE1_VIOL), "sha256": sha256_file(STAGE1_VIOL)},
            "text_recheck": {"path": str(RECHECK_JSON), "sha256": sha256_file(RECHECK_JSON)},
            "stage3_evidence": ({"path": str(STAGE3_JSON), "sha256": sha256_file(STAGE3_JSON)}
                                if stage3 else None),
        },
        "coverage": coverage,
        "routing_rules": ROUTING_RULES,
        "decisions": decision_table(findings),
        "findings": findings,
        "ai_review": ai_summary,
        "summary": {
            "statement": statement,
            "by_final_status": dict(by_final),
            "by_defect_severity": dict(by_sev),
            "reviewed_objects": reviewed,
            "recheck_objects": recheck_n,
            "rule_closed_candidates": len(rule_closed),
            "review_pages_succeeded": len(reviewed_pages),
            "not_applicable_objects": na_count,
            "page_status_counts": coverage["stage2_text_visibility"]["page_status_counts"],
            "api_savings": savings,
        },
        "limitations": [
            review_note,
            (f"第二轮只覆盖第一阶段越界候选，不是全页文字扫描；"
             f"{coverage['not_checked_count']} 个文字对象为 not_checked，"
             "不能按“无缺陷”处理。"),
            ("真实第三方 API 联调尚未完成：本机没有配置 PPT_REVIEW_API_KEY / OPENAI_API_KEY。"
             "host 模式是宿主看图，不是第三方 API 调用。"
             if not real_api_done else
             f"真实第三方 API 已完成 {ai_summary['provider_api_calls']} 次调用。"),
            "页面裁切只回答“文字是否被页面边界切断”，不回答内容正确性、语义或版式美观。",
            "遮挡规则基于 COM z 序、Fill.Visible 与轴对齐外接框，旋转/组合对象可能低估重叠。",
            "图片背景可读性规则只做区域重叠判断，是否真的难读必须由视觉复核或人工确认。",
            "视觉模型只看到导出的静态 PNG；动画、切页效果、字体替换后的差异不在检查范围。",
            "合成测试页用于验证调度与复核能力，不代表真实语料的缺陷分布。",
        ],
    }

    findings_path = out_dir / "agent_findings.json"
    log_path = out_dir / "agent_run_log.json"
    report_path = out_dir / "agent_report.md"
    findings_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(build_report(result), encoding="utf-8")

    log = {
        "started_at": datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
        "finished_at": now_iso(),
        "duration_ms": int((time.time() - started) * 1000),
        "reviewer_mode": ai_summary["mode"],
        "simulated": ai_summary["simulated"],
        "api_key_present": ai_summary["api_key_present"],
        "max_calls": args.max_calls,
        "rules_version": RULES_VERSION,
        "inputs": result["inputs"],
        "decision_counts": dict(Counter(f["route"]["decision"] for f in findings)),
        "final_counts": dict(by_final),
        "evidence_conflicts": sum(1 for f in findings if f.get("evidence_conflict")),
        "review_pages_succeeded": len(reviewed_pages),
        "real_api_status": ai_summary["real_api_integration"]["status"],
        "ai_calls": ai_summary["calls"],
        "skipped": ai_summary["skipped"],
        "errors": [c["error"] for c in ai_summary["calls"] if c["error"]],
        "outputs": [str(findings_path), str(report_path)],
    }
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"written: {findings_path}")
    print(f"written: {report_path}")
    print(f"written: {log_path}")
    print(statement)
    print(f"复核模式={ai_summary['mode']} 视觉复核={ai_summary['vision_reviews']} "
          f"HTTP={ai_summary['api_http_calls']} 队列={ai_summary['pages_queued']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

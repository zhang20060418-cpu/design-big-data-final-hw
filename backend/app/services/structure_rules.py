"""Layer 1 — explainable structure rules.

Every finding here is a *candidate* backed by a geometric or typographic
measurement.  Nothing is presented as a confirmed design defect: confirmation
requires human review or an explicitly authorised AI review.
"""
from __future__ import annotations

import math
import re
from typing import Any

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
PAGE_NUMBER = re.compile(r"^[\d\s/.\-–—]+$")

MIN_FONT_PT = 9.0
EDGE_TOLERANCE_IN = 0.04


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _intersection(a: dict, b: dict) -> tuple[float, float]:
    x = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    y = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return x, y


def _clamp(box: dict, width: float, height: float) -> dict:
    x = min(max(0.0, box["x"]), width)
    y = min(max(0.0, box["y"]), height)
    w = max(0.001, min(box["w"], width - x))
    h = max(0.001, min(box["h"], height - y))
    return {"x": _r(x), "y": _r(y), "w": _r(w), "h": _r(h)}


def _issue(slide_index: int, rule_id: str, defect_type: str, title: str,
           summary: str, recommendation: str, severity: str,
           bbox: dict | None, element_id: str | None, confidence: str,
           details: dict | None = None, ordinal: int = 1) -> dict:
    return {
        "issue_id": f"{rule_id}-s{slide_index:04d}-{ordinal:02d}",
        "document_id": None,  # filled by the orchestrator
        "slide_index": slide_index,
        "defect_type": defect_type,
        "title": title,
        "evidence_source": "structure_rule",
        "rule_id": rule_id,
        "rule_name": title,
        "evidence_summary": summary,
        "element_id": element_id,
        "bbox": bbox,
        "coordinate_space": "inches",
        "visual_probability": None,
        "severity": severity,
        "confidence": confidence,
        "review_status": "unreviewed",
        "review_result": None,
        "recommendation": recommendation,
        "execution_status": "candidate",
        "details": details or {},
    }


def analyze(detail: dict[str, Any], document: dict[str, Any],
            native_contexts: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    width = float(detail["slide_width_in"])
    height = float(detail["slide_height_in"])
    slide_area = max(1e-6, width * height)
    issues: list[dict] = []
    risks: list[dict] = []
    native_pages = {int(p["page_index"]): p for p in native_contexts or []}

    for slide in detail["slides"]:
        idx = int(slide["index"])
        blocks = slide.get("blocks", [])
        elements = slide.get("elements", [])
        per_slide: list[dict] = []
        measured = {o["id"]: o for o in native_pages.get(idx, {}).get("objects", [])}

        # ---------------------------------------------------------------- R01 bounds
        r01 = 0
        for el in elements:
            if el["kind"] not in ("text", "table", "chart"):
                continue
            box = el["bbox"]
            over = max(-box["x"], -box["y"], box["x"] + box["w"] - width, box["y"] + box["h"] - height)
            if over <= EDGE_TOLERANCE_IN:
                continue
            r01 += 1
            if r01 > 3:
                break
            sides = []
            if box["x"] < -EDGE_TOLERANCE_IN:
                sides.append("左")
            if box["y"] < -EDGE_TOLERANCE_IN:
                sides.append("上")
            if box["x"] + box["w"] > width + EDGE_TOLERANCE_IN:
                sides.append("右")
            if box["y"] + box["h"] > height + EDGE_TOLERANCE_IN:
                sides.append("下")
            per_slide.append(_issue(
                idx, "R01", "content_overflow_cutoff", "对象越出画布边界",
                f"元素「{el['name']}」{('/'.join(sides) or '边界')}越出画布 {over:.2f} 英寸，"
                f"文本框范围 {_r(box['x'])},{_r(box['y'])} → {_r(box['x']+box['w'])},{_r(box['y']+box['h'])}（英寸）。",
                "将该元素移回画布安全区，或缩小字号/宽度；若为刻意出血的大字装饰请在人工复核时排除。",
                "high" if over >= 0.12 else "medium",
                _clamp(box, width, height), None, "high",
                {"element": el["name"], "overflow_in": _r(over), "sides": sides,
                 "box_in": {k: _r(v) for k, v in box.items()}},
                r01,
            ))

        # ------------------------------------------------------- R02 text box overflow
        r02 = 0
        for block in blocks:
            if r02 >= 3:
                break
            if block.get("auto_size") and "TEXT_TO_FIT_SHAPE" in block["auto_size"]:
                continue  # PowerPoint shrinks the text instead of spilling
            text = block["text"] or ""
            if not text.strip():
                continue
            size = max(block["sizes"]) if block["sizes"] else 18.0
            if size < 5:
                continue
            cjk = len(CJK.findall(text))
            other = max(0, len(re.sub(r"\s+", "", text)) - cjk)
            est_w_pt = (cjk * 1.0 + other * 0.52) * size
            inner_w_pt = max(12.0, (block["bbox"]["w"] - 0.2) * 72)
            inner_h_pt = max(12.0, (block["bbox"]["h"] - 0.16) * 72)
            lines = max(1, math.ceil(est_w_pt / inner_w_pt))
            need_h_pt = lines * size * 1.32
            box = block["bbox"]
            if not block.get("word_wrap", True):
                right_in = box["x"] + 0.1 + est_w_pt / 72
                if right_in > width - 0.02:
                    r02 += 1
                    per_slide.append(_issue(
                        idx, "R02", "content_overflow_cutoff", "文本不换行并越出画布",
                        f"元素「{block['name']}」关闭了自动换行，按 {size:g}pt 估算文本宽度约 "
                        f"{est_w_pt/72:.2f} 英寸，右端将到达 {right_in:.2f} 英寸（画布宽 {width:.2f}）。",
                        "开启自动换行，或缩短文案/降低字号，确保文本右端留在画布内。",
                        "high", _clamp({"x": width - 0.9, "y": box["y"], "w": 0.9, "h": box["h"]}, width, height),
                        block["element_id"], "low",
                        {"estimated_width_in": _r(est_w_pt / 72), "font_pt": size}, r02,
                    ))
                continue
            if lines <= 1:
                continue
            bottom_in = box["y"] + 0.08 + need_h_pt / 72
            overflow_in = bottom_in - min(box["y"] + box["h"], height)
            if bottom_in > height - 0.02:
                r02 += 1
                per_slide.append(_issue(
                    idx, "R02", "content_overflow_cutoff", "文本高度超出画布",
                    f"元素「{block['name']}」按 {size:g}pt 估算需 {lines} 行、约 {need_h_pt/72:.2f} 英寸高，"
                    f"文本下沿约到 {bottom_in:.2f} 英寸，超出画布（{height:.2f}）。",
                    "压缩文案或降低字号，并把文本框上移；确认导出后最后一行完整可见。",
                    "high", _clamp({"x": box["x"], "y": max(0.0, height - 0.9), "w": box["w"], "h": 0.9}, width, height),
                    block["element_id"], "low",
                    {"estimated_height_in": _r(need_h_pt / 72), "lines": lines, "font_pt": size}, r02,
                ))
            elif need_h_pt > inner_h_pt * 1.55 and overflow_in > 0.25:
                r02 += 1
                per_slide.append(_issue(
                    idx, "R02", "content_overflow_cutoff", "文本明显超出文本框",
                    f"元素「{block['name']}」估算需 {lines} 行（约 {need_h_pt/72:.2f} 英寸），"
                    f"文本框仅 {box['h']:.2f} 英寸，超出约 {overflow_in:.2f} 英寸，可能压到下方元素。",
                    "增大文本框高度或减少文字；检查下方元素是否被文字覆盖。",
                    "medium", _clamp(box, width, height), block["element_id"], "low",
                    {"estimated_height_in": _r(need_h_pt / 72), "box_height_in": _r(box["h"]),
                     "lines": lines, "font_pt": size}, r02,
                ))

        # -------------------------------------------------------------- R03 tiny font
        tiny = []
        for block in blocks:
            if not block["sizes"] or block["chars"] < 15:
                continue
            smallest = min(block["sizes"])
            if smallest >= MIN_FONT_PT:
                continue
            if block["bbox"]["y"] + block["bbox"]["h"] > height * 0.93:
                continue  # footer zone
            if PAGE_NUMBER.match(block["text"].strip()):
                continue
            if block.get("placeholder"):
                continue
            tiny.append((block, smallest))
        if tiny:
            block, smallest = min(tiny, key=lambda pair: pair[1])
            names = "、".join(f"「{b['name']}」" for b, _ in tiny[:3])
            more = f" 等 {len(tiny)} 个文本对象" if len(tiny) > 3 else ""
            per_slide.append(_issue(
                idx, "R03", "improper_font_sizing", "正文字号偏小",
                f"{names}{more}的最小显式字号为 {smallest:g}pt（低于 {MIN_FONT_PT:g}pt 参考线），"
                f"其中「{block['name']}」含 {block['chars']} 个字符，位于 "
                f"{block['bbox']['x']:.2f},{block['bbox']['y']:.2f} 英寸处。",
                "将关键正文提升到 12pt 以上，或在投影场景下重新排版为更少文字。",
                "medium" if smallest < 7.5 else "low",
                _clamp(block["bbox"], width, height), block["element_id"], "medium",
                {"min_pt": smallest, "affected_blocks": len(tiny),
                 "chars": sum(b["chars"] for b, _ in tiny)}, 1,
            ))

        # ----------------------------------------------------------- R04 dense layout
        text_elements = [e for e in elements if e["kind"] == "text"]
        boxes = len(text_elements)
        chars = int(slide.get("text_chars", 0))
        pics = int(slide.get("pictures", 0))
        area_sum = sum(e["bbox"]["w"] * e["bbox"]["h"] for e in text_elements) / slide_area
        if (boxes >= 16 and chars >= 550) or (boxes >= 20 and chars >= 300) or (pics >= 8 and chars >= 250):
            per_slide.append(_issue(
                idx, "R04", "cluttered_layout", "版面元素密度偏高",
                f"本页有 {boxes} 个文本对象、{chars} 个字符、{pics} 张图片；文本对象面积合计约占画布 "
                f"{area_sum*100:.0f}%。属于密度候选，需结合分区是否清楚判断。",
                "按信息层级拆页，或加大分组间距、减少同层并列元素数量。",
                "low", None, None, "low",
                {"text_boxes": boxes, "chars": chars, "pictures": pics,
                 "text_area_ratio": _r(min(area_sum, 3.0), 2)}, 1,
            ))

        # --------------------------------------------------------- R05 overlap candidates
        r05 = 0
        block_by_i = {b["element_i"]: b for b in blocks if "element_i" in b}
        for element in elements:
            if r05 >= 2:
                break
            block = block_by_i.get(element["i"])
            if not block:
                continue
            target_boxes = [element["bbox"]]
            if native_contexts is not None:
                actual = measured.get(f"s{idx:04d}-shapeid{block.get('shape_id')}", {})
                target_boxes = [{k: float(p["bbox"][k])*(width if k in ("x", "w") else height) for k in ("x", "y", "w", "h")}
                                for p in actual.get("paragraphs", []) if p.get("text_chars", 0) and p.get("bbox")]
                if not target_boxes or abs(actual.get("rotation", 0) or 0) > 1e-6:
                    continue  # Missing actual text layout cannot prove glyph loss.
            best = None
            for cover in elements[element["i"] + 1:]:
                if not cover["opaque"]:
                    continue
                if cover["kind"] == "text" and cover["chars"] == 0:
                    continue
                if cover["bbox"]["w"] * cover["bbox"]["h"] < slide_area * 0.03:
                    continue
                if native_contexts is not None:
                    upper = measured.get(f"s{idx:04d}-shapeid{cover.get('shape_id')}", {})
                    opacity = upper.get("opacity") or {}
                    alpha = opacity.get("alpha", 1. if opacity.get("state") == "fully_opaque" else None)
                    if alpha is None or alpha < .6: continue
                    if upper.get("kind") != "image" and upper.get("geometry_type") != "rect": continue
                for text_box in target_boxes:
                    ix, iy = _intersection(text_box, cover["bbox"])
                    if ix <= 0 or iy <= 0: continue
                    ratio = ix*iy/max(1e-6, text_box["w"]*text_box["h"])
                    if ratio >= .45 and (best is None or ratio > best[1]):
                        best = (cover, ratio, {"x": max(text_box["x"], cover["bbox"]["x"]),
                                              "y": max(text_box["y"], cover["bbox"]["y"]), "w": ix, "h": iy})
            if not best:
                continue
            cover, ratio, inter = best
            r05 += 1
            per_slide.append(_issue(
                idx, "R05", "occluded_content", "文字排版区域与上层对象相交" if native_contexts is not None else "文本框与上层对象相交候选",
                f"元素「{element['name']}」的{'实测文字排版区域' if native_contexts is not None else '文本框'}有 {ratio*100:.0f}% 的面积与上层「{cover['name']}」"
                f"（{cover['kind']}）覆盖；覆盖区域约 {inter['w']:.2f}×{inter['h']:.2f} 英寸。",
                "调整图层顺序或移动覆盖对象；若上层为半透明装饰，请确认文字仍完整可读。",
                "high" if ratio >= 0.7 else "medium",
                _clamp(inter, width, height), block["element_id"], "medium",
                {"covered_by": cover["name"], "covered_ratio": _r(ratio, 2), "actual_paragraph_layout_used": native_contexts is not None}, r05,
            ))

        issues.extend(per_slide[:8])

        if slide.get("content_mode") == "image_only":
            risks.append({
                "risk_id": f"R06-s{idx:04d}",
                "slide_index": idx,
                "type": "structure_limited",
                "title": "整页位图，原生结构检查受限",
                "detail": "该页由一张铺满画布的图片构成，文本不在 PowerPoint 对象层，"
                          "结构规则无法读取字号、文本框与对象位置；仅视觉检查可用。",
                "severity": "info",
            })

    if document.get("missing_fonts"):
        risks.append({
            "risk_id": "R07-document",
            "slide_index": None,
            "type": "font_substitution",
            "title": "存在本机未安装字体",
            "detail": "以下字体在本机未安装，PowerPoint 渲染时会替换，可能导致换行与溢出差异："
                      + "、".join(document["missing_fonts"][:12]),
            "severity": "warn",
        })

    return issues, risks

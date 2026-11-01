"""AI-assisted layout repair: plan from findings, edit locally, re-check.

The model never receives the deck.  It only picks one safe operation per finding
from a fixed vocabulary and writes a one-line rationale; :func:`apply_plan` does
the actual edits with python-pptx on a copy of the source file.  The copy is then
parsed and re-analysed with the same structure rules, so the outcome is measured
against the findings that drove it instead of being declared successful.

Every finding keeps its own verdict: ``applied`` (the rule no longer fires),
``partial`` (changed but still detected), ``manual`` (needs a human edit, e.g.
redrawing an icon) or ``skipped`` (target cannot be edited safely, e.g. a shape
inside a group).
"""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
import zipfile
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.util import Emu, Pt

from ..config import DEFECT_LABELS_ZH, RENDER_LONG_EDGE, RENDER_TIMEOUT_S
from . import annotate, layout_ops, structure_rules, verdicts, visual_diff
from .pptx_parser import _is_opaque, _iter_leaves, parse_document

from .renderer import render_pptx

EMU_IN = 914400
MARGIN_IN = 0.03
FONT_TARGET_PT = 12.0
FONT_MAX_PT = 14.0
MAX_REPAIRS = 6
REVIEW_MAX_ROUNDS = 3          # 勾选「LLM 复核」时的默认轮次上限
LAYER_STRATEGY = "text_front"  # text_front 保留底色；cover_back 作为兜底

STRUCTURE_TYPES = ("content_overflow_cutoff", "improper_font_sizing",
                   "cluttered_layout", "occluded_content")

ACTION_LABEL = {
    "move_inside": "移回画布并收进衬底",
    "wrap_and_grow": "开启换行并撑高文本框",
    "raise_font": "正文字号提到 12pt",
    "bring_front": "把被遮文字提到最上层",
    "tidy_layout": "整理间距与对齐",
    "manual": "留待人工处理",
}

# 每种缺陷允许的自动动作；模型只能在这个集合里选。
ALLOWED = {
    "content_overflow_cutoff": ("move_inside", "wrap_and_grow"),
    "occluded_content": ("bring_front",),
    "improper_font_sizing": ("raise_font",),
    "cluttered_layout": ("tidy_layout",),
    "excessive_whitespace": ("manual",),
    "generative_design_anomaly": ("manual",),
}

# 能靠「改一个矩形」解决的，才值得为它再开一轮模型调用；整理版式是幂等的。
PROVABLE = ("move_inside", "wrap_and_grow", "raise_font", "bring_front")

MANUAL_REASON = {
    "cluttered_layout": "拥挤页要合并模块或拆页，自动整理只能拉开间距，留待人工按建议重排。",
    "excessive_whitespace": "留白页要补内容或放大主体，自动拉伸会破坏版式比例，留待人工补内容。",
    "generative_design_anomaly": "图标语义不符需要重画素材，系统不生成图形，留待人工替换。",
}

DETECTOR_REASON = "该条来自视觉检测器，只有归一化区域、没有对象级坐标，自动改写会误伤其他元素，留待人工确认。"


# --------------------------------------------------------------------- paths
def repairs_root(task_dir: Path) -> Path:
    return Path(task_dir) / "repairs"


def _new_id() -> str:
    return "r-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_status(task_dir: Path, repair_id: str) -> dict | None:
    path = repairs_root(task_dir) / repair_id / "status.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_repairs(task_dir: Path) -> list[dict]:
    root = repairs_root(task_dir)
    if not root.is_dir():
        return []
    items = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        status = load_status(task_dir, folder.name)
        if status:
            items.append(status)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def latest(task_dir: Path) -> dict | None:
    items = list_repairs(task_dir)
    return items[0] if items else None


def _prune(task_dir: Path) -> None:
    root = repairs_root(task_dir)
    if not root.is_dir():
        return
    folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for folder in folders[MAX_REPAIRS:]:
        try:
            shutil.rmtree(folder)
        except OSError:
            pass


# -------------------------------------------------------------------- hints
def hints_path(task_dir: Path) -> Path:
    return repairs_root(task_dir) / "hints.json"


def load_hints(task_dir: Path) -> dict[str, str]:
    """Human notes per page, kept across repair runs.  Never edits the report."""
    try:
        data = json.loads(hints_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items = data.get("hints") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(key): str(value)[:600] for key, value in items.items() if str(value).strip()}


def save_hint(task_dir: Path, slide_index: int, text: str) -> dict[str, str]:
    hints = load_hints(task_dir)
    key = str(int(slide_index))
    text = (text or "").strip()[:600]
    if text:
        hints[key] = text
    else:
        hints.pop(key, None)
    path = hints_path(task_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"hints": hints, "updated_at": _now()},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return hints


# -------------------------------------------------------------------- brief
def _fmt_box(box: dict | None, width: float, height: float) -> str:
    if not box:
        return "未给出对象坐标"
    try:
        x, y = float(box["x"]), float(box["y"])
        w, h = float(box["w"]), float(box["h"])
    except (KeyError, TypeError, ValueError):
        return "未给出对象坐标"
    return (f"x={x:.2f}, y={y:.2f}, w={w:.2f}, h={h:.2f} 英寸"
            f"（页宽 {width:.2f} × 页高 {height:.2f}）")


def deck_layout(path: Path) -> dict[int, dict]:
    """Per page: the content blocks and the cards they sit on.

    Handed to the model so "put it back on its card" is a concrete instruction
    with coordinates instead of a wish.  Unreadable decks degrade to ``{}``.
    """
    try:
        prs = Presentation(str(path))
    except Exception:
        return {}
    width = prs.slide_width / EMU_IN
    height = prs.slide_height / EMU_IN
    page_area = max(1e-6, width * height)
    layout: dict[int, dict] = {}
    for index, slide in enumerate(prs.slides, 1):
        leaves = [leaf for leaf in _iter_leaves(slide.shapes) if not leaf.group_path]
        blocks = []
        for leaf in leaves:
            if not layout_ops.is_content(leaf, page_area):
                continue
            rect = layout_ops.box(leaf)
            text = _text(leaf.shape).strip()
            blocks.append({
                "name": leaf.shape.name,
                "kind": "text" if text else "shape",
                "box": {key: round(value, 2) for key, value in rect.items()},
                "text": text[:20],
            })
            if len(blocks) >= 14:
                break
        cards = []
        for leaf in leaves:
            if layout_ops.is_group_member(leaf) or not layout_ops.filled(leaf.shape):
                continue
            rect = layout_ops.box(leaf)
            if not 0.9 <= layout_ops.area_of(rect) <= 0.85 * page_area:
                continue
            holds = [block["name"] for block in blocks
                     if layout_ops.inside_ratio(
                         {"x": block["box"]["x"], "y": block["box"]["y"],
                          "w": block["box"]["w"], "h": block["box"]["h"]}, rect) >= 0.90]
            if not holds:
                continue
            cards.append({"name": leaf.shape.name,
                          "box": {key: round(value, 2) for key, value in rect.items()},
                          "holds": holds[:8]})
            if len(cards) >= 8:
                break
        layout[index] = {"slide": index, "size": [round(width, 2), round(height, 2)],
                         "cards": cards, "blocks": blocks}
    return layout


def page_layout(path: Path, slide_index: int) -> dict:
    return deck_layout(path).get(int(slide_index)) or {"slide": slide_index,
                                                       "size": [], "cards": [], "blocks": []}


def _card_of(layout: dict | None, element: str | None = None,
             box: dict | None = None) -> dict | None:
    """Which card the finding belongs to: by element name first, then by area."""
    if not layout:
        return None
    cards = layout.get("cards") or []
    if element:
        for card in cards:
            if element in (card.get("holds") or []):
                return card
    if box:
        for card in cards:
            rect = card.get("box") or {}
            try:
                if layout_ops.inside_ratio(box, rect) >= 0.80:
                    return card
            except (KeyError, TypeError):
                continue
    return None


LAYOUT_RULES = (
    "版式要求（必须同时满足，不能只让告警消失）：\n"
    "- 文字要落在它所属的圆角矩形衬底内：不压边、不出卡片（距内缘至少 0.12 英寸），"
    "并保持原有对齐关系（原来左对齐的仍左对齐，不要重新居中）；"
    "衬底装不下时向下延展，但不能越出画布、不能压住下方内容。\n"
    "- 同一列的块左边缘对齐，同一行的块上边缘对齐；相邻块间距保持一致（0.10–0.30 英寸）。\n"
    "- 不允许用「删掉内容」「把字号缩到 9pt 以下」「把元素藏起来」来应付检查。\n"
    "- 同一页的多条缺陷要全部给出去向，不允许只处理最显眼的一条。"
)


def page_brief(payload: dict, slide_index: int, ops: list[dict] | None = None,
               hints: dict[str, str] | None = None, layout: dict | None = None) -> dict:
    """A ready-to-paste prompt describing one page's findings and the fix rules.

    "普通容易修复的页" and "需要人工的页" use the same text: the difference is
    that the automatic pass has already applied the safe edits, so the prompt
    lists them under "已完成" and only asks the reader to handle the rest.
    """
    document = payload.get("document") or {}
    width = float(document.get("slide_width_in") or 13.333)
    height = float(document.get("slide_height_in") or 7.5)
    issues = [issue for issue in payload.get("issues") or []
              if verdicts.is_visible(issue) and int(issue.get("slide_index") or 0) == int(slide_index)]
    page_ops = {op["issue_id"]: op for op in (ops or [])
                if int(op.get("slide_index") or 0) == int(slide_index)}
    hint = (hints or {}).get(str(slide_index), "")

    lines = [
        "你是一名 PPT 版式修复师。只修改下面列出的问题区域，"
        "其他元素的位置、字号、颜色、层级一律保持原样，不要重画整页。"
        "这一页有几条缺陷就处理几条，不允许只改最显眼的一条。",
        "",
        f"文件：{document.get('file_name') or 'slides.pptx'}"
        f"（{document.get('aspect_label') or ''}，{width:.2f} × {height:.2f} 英寸，"
        f"共 {document.get('slide_count') or '?'} 页）",
        f"当前页：第 {slide_index} 页。标注图：annotated/s{int(slide_index):04d}.png，"
        "图中的编号与下面的条目一一对应。",
        "",
        "需要修复的问题：",
    ]
    if not issues:
        lines.append("（这一页没有可见缺陷。）")
    for number, issue in enumerate(issues, 1):
        op = page_ops.get(issue.get("issue_id")) or {}
        label = DEFECT_LABELS_ZH.get(issue.get("defect_type"), issue.get("defect_type"))
        lines.append(f"{number}. [{label}] {issue.get('title') or ''}".rstrip())
        element = (issue.get("details") or {}).get("element") or issue.get("element_id") or "未命名对象"
        lines.append(f"   对象：{element}")
        lines.append(f"   范围：{_fmt_box(issue.get('bbox'), width, height)}")
        card = _card_of(layout, element, issue.get("bbox"))
        if card:
            lines.append(f"   所属衬底：{card['name']}"
                         f"（{_fmt_box(card.get('box'), width, height)}）")
        summary = (issue.get("evidence_summary") or "").strip()
        if summary:
            lines.append(f"   现状：{summary[:240]}")
        if issue.get("recommendation"):
            lines.append(f"   期望：{issue['recommendation']}")
        if op.get("status") == "applied":
            lines.append(f"   系统已完成：{op.get('detail') or ACTION_LABEL.get(op.get('action'), '')}")
        elif op.get("status") in ("manual", "skipped") and op.get("detail"):
            lines.append(f"   系统未处理：{op['detail']}")
    if hint:
        lines += ["", "人工附加要求（必须遵守）：", f"「{hint}」"]
    lines += ["", LAYOUT_RULES, "",
              "输出要求：逐条给出具体改法（移动多少、字号多少、层级怎么调），"
              "每条对应上面的编号；不要改动清单之外的内容。"]
    return {
        "slide": int(slide_index),
        "title": next((issue.get("title") for issue in issues), ""),
        "issues": [
            {
                "number": number,
                "issue_id": issue.get("issue_id"),
                "defect_type": issue.get("defect_type"),
                "label": DEFECT_LABELS_ZH.get(issue.get("defect_type"), issue.get("defect_type")),
                "title": issue.get("title"),
                "element": (issue.get("details") or {}).get("element") or issue.get("element_id"),
                "box": issue.get("bbox"),
                "status": (page_ops.get(issue.get("issue_id")) or {}).get("status"),
                "detail": (page_ops.get(issue.get("issue_id")) or {}).get("detail"),
            }
            for number, issue in enumerate(issues, 1)
        ],
        "hint": hint,
        "prompt": "\n".join(lines),
    }


def build_bundle(task_dir: Path, repair_id: str, payload: dict,
                 ops: list[dict] | None = None) -> Path:
    """Zip the source deck, the annotated pages and the prompts for one run."""
    folder = repairs_root(task_dir) / repair_id
    target = folder / "bundle.zip"
    hints = load_hints(task_dir)
    document = payload.get("document") or {}
    width = float(document.get("slide_width_in") or 13.333)
    height = float(document.get("slide_height_in") or 7.5)

    annots = annotate.annotate_pages(task_dir, folder / "annots", payload.get("issues") or [],
                                     width, height)
    pages = sorted({int(issue.get("slide_index") or 0) for issue in payload.get("issues") or []
                    if verdicts.is_visible(issue) and int(issue.get("slide_index") or 0) > 0})
    layout = deck_layout(Path(task_dir) / "source" / "source.pptx")

    prompt_parts = [
        f"# {document.get('file_name') or 'slides.pptx'} 修改任务包",
        "",
        f"- 页面尺寸：{width:.2f} × {height:.2f} 英寸（{document.get('aspect_label') or ''}）",
        f"- 缺陷页：{'、'.join(f'P{index:02d}' for index in pages) or '无'}",
        "- 标注图目录：`annotated/`，文件名与页码对应（s0007.png = 第 7 页）",
        "- `source.pptx` 是原始文件，`fixed.pptx` 是系统自动修复后的副本（如果有）",
        "",
    ]
    for index in pages:
        brief = page_brief(payload, index, ops, hints, layout.get(index))
        prompt_parts += [f"## 第 {index} 页", "", "```text", brief["prompt"], "```", ""]
    (folder / "prompt.md").write_text("\n".join(prompt_parts), encoding="utf-8")

    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as bundle:
        source = Path(task_dir) / "source" / "source.pptx"
        if source.is_file():
            bundle.write(source, "source.pptx")
        fixed = folder / "fixed.pptx"
        if fixed.is_file():
            bundle.write(fixed, "fixed.pptx")
        for index, name in annots.items():
            bundle.write(folder / "annots" / name, f"annotated/{name}")
        bundle.write(folder / "prompt.md", "prompt.md")
        if (folder / "plan.json").is_file():
            bundle.write(folder / "plan.json", "plan.json")
    tmp.replace(target)
    return target


# ---------------------------------------------------------------------- plan
def _element_ref(issue: dict) -> dict:
    details = issue.get("details") or {}
    if isinstance(details.get("detector_claim"), dict):
        details = details["detector_claim"]
    return {
        "element_id": issue.get("element_id"),
        "name": details.get("element"),
        "covered_by": details.get("covered_by"),
        "box": details.get("box_in") or issue.get("bbox"),
    }


def _default_action(issue: dict) -> str:
    # 只有结构规则给出的条目带对象级坐标；视觉检测器的候选不做自动改写。
    if issue.get("evidence_source") != "structure_rule":
        return "manual"
    dtype = issue.get("defect_type")
    if dtype == "content_overflow_cutoff":
        return "wrap_and_grow" if issue.get("rule_id") == "R02" else "move_inside"
    return ALLOWED.get(dtype, ("manual",))[0]

def build_plan(payload: dict, round_no: int = 1) -> list[dict]:
    """One operation per visible finding, in a fixed, safe vocabulary.

    ``round_no`` only tags the operations so the result page can say which pass
    produced them; a second pass is fed the findings that survived the first.
    """
    ops: list[dict] = []
    for issue in payload.get("issues") or []:
        if not verdicts.is_visible(issue):
            continue
        defect = issue.get("defect_type")
        action = _default_action(issue)
        ref = _element_ref(issue)
        op = {
            "issue_id": issue.get("issue_id"),
            "slide_index": int(issue.get("slide_index") or 0),
            "defect_type": defect,
            "label": DEFECT_LABELS_ZH.get(defect, defect),
            "rule_id": issue.get("rule_id"),
            "title": issue.get("title"),
            "action": action,
            "target_pt": FONT_TARGET_PT if defect == "improper_font_sizing" else None,
            "ref": ref,
            "bbox": issue.get("bbox"),
            "note": "",
            "detail": "",
            "round": int(round_no),
            "status": "pending" if action != "manual" else "manual",
            "locked": issue.get("evidence_source") != "structure_rule",
        }
        if action == "manual":
            op["detail"] = (DETECTOR_REASON if op["locked"]
                            else MANUAL_REASON.get(defect, "该类问题没有安全的自动改法。"))
        ops.append(op)
    return ops


# ------------------------------------------------------------------- model
PLAN_SYSTEM = (
    "你是 PPT 版式修复的调度器。输入是审查系统已确认的缺陷清单、每条允许的修复动作，"
    "以及这一页的对象坐标与衬底卡片（card）。\n"
    "判断标准只有一条：改完之后这一页要像一份正常设计的稿子，而不只是「告警消失」。\n"
    "规则：\n"
    "1) 每条缺陷都必须给一个决定，不能漏。同一页有多条缺陷时全部处理，"
    "不允许只挑最显眼的一条。\n"
    "2) 每条只能从它自己的 allowed_actions 里选一个动作，不能发明新动作。\n"
    "3) 文字必须落在它所属的圆角矩形衬底内：不压边、不出卡片（距内缘至少 0.12 英寸），"
    "保持原来的对齐关系，不要把所有内容重新居中；衬底装不下时允许把衬底下延，"
    "但不能越出画布、不能压住下方内容。\n"
    "4) 同一列的块左边缘对齐，同一行的块上边缘对齐；相邻块间距保持一致（0.10–0.30 英寸）。\n"
    "5) 不允许用「删掉内容」「把字号缩到 9pt 以下」「把元素藏起来」来让规则通过。\n"
    "6) 只有必须改文案、换图或重画图标时，才选 manual，并在 note 里写清楚人工要做什么；"
    "其余一律给出可执行的自动动作。\n"
    "7) 带 round / previous 时，说明上一轮的动作没能让规则通过：对同一批条目换用另一种 "
    "allowed_actions 里的动作，不要重复上一轮的选择。\n"
    "8) 带 user_hint 时，在不超出 allowed_actions 的前提下向用户意图靠拢；"
    "做不到就在 note 里明确说明人工要做什么。\n"
    "9) 语气坚定，不写「可能/也许/建议考虑」这类措辞。\n"
    "只输出 JSON，不要解释。"
)

PLAN_SCHEMA = """{
 "decisions": [{"issue_id": "R05-s0005-01", "action": "bring_front", "target_pt": null,
                "note": "把覆盖在表头上的形状移到文字下层。"}],
 "summary": "一句话概括这次修复的重点"
}"""


def _plan_brief(payload: dict, ops: list[dict], prompt_excerpt: str,
                context: dict | None = None) -> dict:
    context = context or {}
    hints = context.get("hints") or {}
    layouts = context.get("layouts") or {}
    document = payload.get("document") or {}
    width = float(document.get("slide_width_in") or 13.333)
    height = float(document.get("slide_height_in") or 7.5)
    items = []
    for op in ops:
        layout = layouts.get(int(op["slide_index"])) or {}
        card = _card_of(layout, op["ref"].get("name"), op.get("bbox"))
        entry = {
            "issue_id": op["issue_id"],
            "slide": op["slide_index"],
            "defect": op["label"],
            "title": op["title"],
            "allowed_actions": list(ALLOWED.get(op["defect_type"], ("manual",))),
            "suggested_action": op["action"],
            "element": op["ref"].get("name") or op["ref"].get("element_id"),
            "covered_by": op["ref"].get("covered_by"),
            "box_in": op["bbox"],
        }
        if card:
            entry["card"] = {"name": card["name"], "box": card.get("box")}
        hint = hints.get(str(op["slide_index"]))
        if hint:
            entry["user_hint"] = hint
        items.append(entry)

    pages = []
    for index in sorted({int(op["slide_index"]) for op in ops}):
        layout = layouts.get(index)
        if not layout:
            continue
        pages.append({"slide": index,
                      "size": layout.get("size"),
                      "cards": layout.get("cards") or [],
                      "blocks": layout.get("blocks") or []})
    brief = {
        "deck": {"file": document.get("file_name"), "pages": document.get("slide_count"),
                 "aspect": document.get("aspect_label")},
        "canvas": {"w": round(width, 2), "h": round(height, 2), "margin_in": layout_ops.MARGIN_IN},
        "issues": items,
        "pages": pages,
        "layout_rules": LAYOUT_RULES.splitlines(),
        "repair_notes": prompt_excerpt,
    }
    if context.get("round"):
        brief["round"] = context["round"]
        brief["max_rounds"] = context.get("max_rounds")
    if context.get("previous"):
        brief["previous"] = context["previous"]
    return brief


def refine(ops: list[dict], payload: dict, settings: dict, api_key: str,
           backend: str = "api", context: dict | None = None) -> dict:
    """Ask the model to pick actions / write rationales.  Never fatal."""
    meta = {"source": "rule", "model": None, "degraded": "", "summary": "",
            "page_notes": {}}
    if backend != "api" or not api_key:
        meta["degraded"] = "未配置模型，本次修复由规则计划直接执行。"
        return meta
    try:
        from . import advice as advice_service
        from .mllm import _extract_json, _post_chat, completion_text, validate_base_url

        prompt = advice_service.build_prompt(payload)
        excerpt = prompt[:2600]
        base = validate_base_url(settings.get("api_base_url", ""),
                                 bool(settings.get("allow_private_base_url")),
                                 bool(settings.get("allow_insecure_base_url")))
        body = {
            "model": settings.get("api_model") or "",
            "messages": [
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": json.dumps(_plan_brief(payload, ops, excerpt, context),
                                                       ensure_ascii=False)
                 + "\n\n按这个结构输出：\n" + PLAN_SCHEMA},
            ],
            "temperature": 0.2,
            "max_tokens": 1600,
        }
        if settings.get("response_format_json", True):
            body["response_format"] = {"type": "json_object"}
        status, reply = _post_chat(base, api_key, body, float(settings.get("api_timeout_s") or 60))
        if status >= 400 or not isinstance(reply, dict):
            raise ValueError(f"接口返回 HTTP {status}")
        content, _meta = completion_text(reply)
        data = _extract_json(content)
        if not isinstance(data, dict):
            raise ValueError("模型输出不是 JSON 对象")

        by_id = {op["issue_id"]: op for op in ops}
        applied = 0
        for decision in data.get("decisions") or []:
            if not isinstance(decision, dict):
                continue
            op = by_id.get(str(decision.get("issue_id") or ""))
            if not op or op.get("locked"):
                continue
            action = str(decision.get("action") or "").strip()
            if action in ALLOWED.get(op["defect_type"], ()) and action != "manual":
                op["action"] = action
                applied += 1
            elif action == "manual" and op["action"] == "manual":
                applied += 1
            note = str(decision.get("note") or "").strip()
            if note:
                op["note"] = note[:120]
            if op["defect_type"] == "improper_font_sizing":
                try:
                    pt = float(decision.get("target_pt") or FONT_TARGET_PT)
                except (TypeError, ValueError):
                    pt = FONT_TARGET_PT
                op["target_pt"] = round(min(FONT_MAX_PT, max(FONT_TARGET_PT, pt)), 1)
        meta.update(source="llm", model=settings.get("api_model") or "", summary=str(data.get("summary") or "")[:160])
        if applied == 0:
            meta["degraded"] = "模型没有给出可执行的调整，按规则计划执行。"
        return meta
    except Exception as exc:  # network, auth, JSON — same deterministic fallback
        meta["degraded"] = f"模型调用失败（{type(exc).__name__}: {exc}），本次按规则计划修复。"
        return meta


# --------------------------------------------------------------------- apply
# 行高估算放在 layout_ops 里，衬底归位与撑高用的是同一把尺子
_need_height_in = layout_ops.need_height_in


def _text(shape) -> str:
    try:
        return shape.text_frame.text if shape.has_text_frame else ""
    except Exception:
        return ""


def _explicit_sizes(shape) -> list[float]:
    sizes = []
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.size is not None and run.text.strip():
                    sizes.append(round(run.font.size.pt, 1))
    except Exception:
        pass
    return sizes


def _set_font(shape, target: float) -> tuple[int, float]:
    changed, before = 0, None
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                size = run.font.size
                if size is None or not run.text.strip():
                    continue
                pt = round(size.pt, 1)
                before = pt if before is None else min(before, pt)
                if pt < target:
                    run.font.size = Pt(target)
                    changed += 1
    except Exception:
        return 0, before or 0.0
    return changed, before or 0.0


def _grow(shape, need_in: float, slide_h: float) -> tuple[float, float]:
    """Grow the box to ``need_in`` and lift it when the bottom would leave the canvas."""
    top, height = shape.top / EMU_IN, shape.height / EMU_IN
    new_h = min(max(height, need_in), max(height, slide_h - MARGIN_IN - top))
    if new_h > height:
        shape.height = Emu(int(new_h * EMU_IN))
    bottom = top + new_h
    shift = 0.0
    if bottom > slide_h - MARGIN_IN and top > MARGIN_IN:
        shift = min(top - MARGIN_IN, bottom - (slide_h - MARGIN_IN))
        shape.top = Emu(int((top - shift) * EMU_IN))
    return new_h, shift


def _move_inside(shape, slide_w: float, slide_h: float) -> tuple[str, str]:
    w, h = shape.width / EMU_IN, shape.height / EMU_IN
    left, top = shape.left / EMU_IN, shape.top / EMU_IN
    dx = dy = 0.0
    if left < MARGIN_IN:
        dx = MARGIN_IN - left
    elif left + w > slide_w - MARGIN_IN:
        dx = (slide_w - MARGIN_IN) - (left + w)
    if top < MARGIN_IN:
        dy = MARGIN_IN - top
    elif top + h > slide_h - MARGIN_IN:
        dy = (slide_h - MARGIN_IN) - (top + h)
    if abs(dx) < 1e-3 and abs(dy) < 1e-3:
        return "skipped", "对象位置已在画布内，无需移动。"
    if w > slide_w - 2 * MARGIN_IN:
        return "manual", "对象宽度超过画布，需要人工换行或改窄。"
    shape.left = Emu(int((left + dx) * EMU_IN))
    shape.top = Emu(int((top + dy) * EMU_IN))
    parts = []
    if abs(dx) > 1e-3:
        parts.append(f"水平 {dx:+.2f}in")
    if abs(dy) > 1e-3:
        parts.append(f"垂直 {dy:+.2f}in")
    return "applied", f"把「{shape.name}」移回画布：{'、'.join(parts)}。"


def _box_close(box: dict | None, leaf, tolerance: float = 1.2) -> bool:
    """Does the finding's rectangle describe this leaf?"""
    if not box:
        return True
    rect = layout_ops.box(leaf)
    if rect["w"] <= 0 or rect["h"] <= 0:
        return False
    ix, iy = layout_ops.overlap(rect, box)
    if ix * iy / max(1e-6, float(box["w"]) * float(box["h"])) >= 0.30:
        return True
    cx, cy = layout_ops.center(rect)
    return math.hypot(cx - (box["x"] + box["w"] / 2), cy - (box["y"] + box["h"] / 2)) <= tolerance


def _resolve(leaves, op: dict, slide_w: float | None = None,
             slide_h: float | None = None, exclude: set[int] | None = None):
    """Find the shape a finding points at, preferring identity over coordinates.

    ``element_id`` and the shape name are authoritative *only* when the parsed
    rectangle still agrees with the finding: a deck whose element order drifted
    would otherwise send the edit to the wrong text box.  When neither matches,
    the rectangle is used, and for an out-of-bounds finding the shape that
    really crosses the canvas is taken as the last resort — that is exactly what
    the rule measured, whatever its name is.
    """
    ref = op.get("ref") or {}
    box = op.get("bbox") or {}
    exclude = exclude or set()
    alive = [leaf for leaf in leaves if id(leaf.shape) not in exclude]

    named = []
    match = re.match(r"s(\d{4})-e(\d{3})", str(ref.get("element_id") or ""))
    if match:
        texts = [leaf for leaf in alive if _text(leaf.shape).strip()]
        ordinal = int(match.group(2))
        if 1 <= ordinal <= len(texts):
            named.append(texts[ordinal - 1])
    name = ref.get("name")
    if name:
        named.extend(leaf for leaf in alive if leaf.shape.name == name)

    for leaf in named:
        if _box_close(box, leaf):
            return leaf

    if box:
        best, best_score = None, 0.0
        for leaf in alive:
            ix, iy = layout_ops.overlap(layout_ops.box(leaf), box)
            score = (ix * iy) / max(1e-6, float(box["w"]) * float(box["h"]))
            if score > best_score:
                best, best_score = leaf, score
        if best is not None and best_score >= 0.35:
            return best

    if op.get("defect_type") == "content_overflow_cutoff" and slide_w and slide_h:
        # 只接受「离缺陷坐标不远」的出界对象：元素编号漂移时它能兜住，
        # 但不会把一个凭空出现的条目随手指到页面上任意一个越界框。
        reach = max(1.5, float(box.get("w") or 0), float(box.get("h") or 0))
        for leaf in alive:
            rect = layout_ops.box(leaf)
            outside = (rect["x"] < MARGIN_IN - 1e-3 or rect["y"] < MARGIN_IN - 1e-3
                       or rect["x"] + rect["w"] > slide_w - MARGIN_IN + 1e-3
                       or rect["y"] + rect["h"] > slide_h - MARGIN_IN + 1e-3)
            if not outside:
                continue
            if box:
                gap_x = max(0.0, max(rect["x"] - (box["x"] + box["w"]),
                                    box["x"] - (rect["x"] + rect["w"])))
                gap_y = max(0.0, max(rect["y"] - (box["y"] + box["h"]),
                                    box["y"] - (rect["y"] + rect["h"])))
                if math.hypot(gap_x, gap_y) > reach:
                    continue
            return leaf

    return named[0] if named else None


def _occlusions(leaves: list, slide_area: float, text_ratio: float = 0.45,
                cover_area: float = 0.03) -> list[tuple]:
    """Text shapes whose box is mostly painted over by a later, opaque shape.

    Mirrors the R05 geometry: a cover must be at least 3% of the page and hide
    at least 45% of the text box.  Kept local so the repair can iterate without
    re-parsing the whole deck after every edit.  The post-repair scan lowers
    both thresholds to *report* the borderline cases the repair leaves alone.
    """
    hits = []
    order = {id(leaf.shape._element): n for n, leaf in enumerate(leaves)}
    for text_leaf in leaves:
        if not _text(text_leaf.shape).strip():
            continue
        area = max(1e-6, text_leaf.width * text_leaf.height)
        for cover in leaves:
            if cover is text_leaf:
                continue
            if order.get(id(cover.shape._element), 0) <= order.get(id(text_leaf.shape._element), 0):
                continue
            if cover.width * cover.height < slide_area * cover_area:
                continue
            if not _is_opaque(cover.shape):
                continue
            ix = max(0.0, min(text_leaf.left + text_leaf.width, cover.left + cover.width)
                     - max(text_leaf.left, cover.left))
            iy = max(0.0, min(text_leaf.top + text_leaf.height, cover.top + cover.height)
                     - max(text_leaf.top, cover.top))
            if ix <= 0 or iy <= 0:
                continue
            if (ix * iy) / area >= text_ratio:
                hits.append((text_leaf, cover, (ix * iy) / area))
    return hits


def _lower(shape, tree) -> bool:
    try:
        element = shape._element
        tree.remove(element)
        tree.insert(2, element)  # 0/1 是 nvGrpSpPr / grpSpPr，插在后面即最底层
        return True
    except Exception:
        return False


def _raise(shape, tree) -> bool:
    try:
        element = shape._element
        tree.remove(element)
        tree.append(element)  # 追加到末尾即最上层
        return True
    except Exception:
        return False


def _lift_texts(slide, slide_area: float, tree, rounds: int) -> list[str]:
    lifted: list[str] = []
    for _ in range(rounds):
        hits = _occlusions(list(_iter_leaves(slide.shapes)), slide_area)
        if not hits:
            break
        moved = False
        for text_leaf, _cover, _ratio in hits:
            if text_leaf.group_path:
                continue
            if _raise(text_leaf.shape, tree):
                lifted.append(text_leaf.shape.name)
                moved = True
        if not moved:
            break
    return lifted


def _lower_covers(slide, slide_area: float, tree, rounds: int) -> list[str]:
    lowered: list[str] = []
    for _ in range(rounds):
        hits = _occlusions(list(_iter_leaves(slide.shapes)), slide_area)
        if not hits:
            break
        moved = False
        seen = set()
        for _text, cover, _ratio in hits:
            key = id(cover.shape._element)
            if key in seen or cover.group_path:
                continue
            seen.add(key)
            if _lower(cover.shape, tree):
                lowered.append(cover.shape.name)
                moved = True
        if not moved:
            break
    return lowered


def _clear_occlusions(slide, slide_area: float, rounds: int = 5,
                      strategy: str = "text_front") -> tuple[dict, int]:
    """Remove every occlusion on a page, one layer per round.

    ``text_front`` lifts only the text boxes that are actually painted over, so
    card fills and decorative panels keep their place; ``cover_back`` pushes the
    covering shapes behind the page and is used when the text itself cannot be
    reordered (locked inside a group).  A page can carry several stacked fills,
    so the check repeats after each round.
    """
    tree = slide.shapes._spTree
    lifted: list[str] = []
    lowered: list[str] = []
    if strategy == "cover_back":
        lowered = _lower_covers(slide, slide_area, tree, rounds)
    else:
        lifted = _lift_texts(slide, slide_area, tree, rounds)
    remaining = len(_occlusions(list(_iter_leaves(slide.shapes)), slide_area))
    if remaining:  # 换个方向再试一次，然后才承认需要人工
        if strategy == "cover_back":
            lifted = _lift_texts(slide, slide_area, tree, rounds)
        else:
            lowered = _lower_covers(slide, slide_area, tree, rounds)
        remaining = len(_occlusions(list(_iter_leaves(slide.shapes)), slide_area))
    return {"lowered": lowered, "lifted": lifted}, remaining


def _fingerprint(slide) -> tuple:
    """Geometry + z-order digest, used to prove whether a page really changed."""
    items = []
    for leaf in _iter_leaves(slide.shapes):
        shape = leaf.shape
        items.append((
            shape.name,
            round((shape.left or 0) / EMU_IN, 3),
            round((shape.top or 0) / EMU_IN, 3),
            round((shape.width or 0) / EMU_IN, 3),
            round((shape.height or 0) / EMU_IN, 3),
            tuple(_explicit_sizes(shape)),
        ))
    return tuple(items)


def _seat_one(leaves, leaf, slide_h: float, page_area: float) -> str:
    """Park the block on its card; empty string when there is nothing to do."""
    try:
        card = layout_ops.find_card(leaves, leaf, page_area=page_area)
        if card is None:
            return ""
        status, detail = layout_ops.seat_in_card(leaves, leaf, card, slide_h)
    except Exception:
        return ""
    return detail if status == "applied" else ""


def apply_plan(prs, ops: list[dict]) -> list[dict]:
    slide_w = prs.slide_width / EMU_IN
    slide_h = prs.slide_height / EMU_IN
    page_area = slide_w * slide_h
    by_slide: dict[int, list[dict]] = {}
    for op in ops:
        by_slide.setdefault(op["slide_index"], []).append(op)

    for index in sorted(by_slide):
        if not 1 <= index <= len(prs.slides):
            for op in by_slide[index]:
                op["status"] = "skipped"
                op["detail"] = "页码超出文稿范围。"
            continue
        slide = prs.slides[index - 1]
        page_ops = by_slide[index]
        before = _fingerprint(slide)
        layer_ops = [op for op in page_ops if op["action"] == "bring_front"]
        tidy_ops = [op for op in page_ops if op["action"] == "tidy_layout"]
        touched = False

        # 0) 先定位再动手：同页多条越界必须各改各的对象，不能都落到同一块文字上
        leaves = list(_iter_leaves(slide.shapes))
        targets: dict[int, object] = {}
        taken: dict[str, set[int]] = {}
        for op in page_ops:
            if op["status"] == "manual" or op["action"] not in PROVABLE:
                continue
            leaf = _resolve(leaves, op, slide_w, slide_h, taken.get(op["action"]))
            if leaf is None:
                continue
            targets[id(op)] = leaf
            taken.setdefault(op["action"], set()).add(id(leaf.shape))

        # 1) 几何位移：越界对象移回画布、文本框开换行后撑高；改完立刻收进衬底
        for op in page_ops:
            if op["status"] == "manual" or op["action"] not in ("move_inside", "wrap_and_grow"):
                continue
            leaf = targets.get(id(op))
            if leaf is None:
                op["status"] = "skipped"
                op["detail"] = "没有在源文件中定位到对应对象，保持原样。"
                continue
            try:
                if op["action"] == "move_inside":
                    if leaf.group_path:
                        op["status"] = "manual"
                        op["detail"] = "对象在组合内，需要人工移动整个组合。"
                    else:
                        status, detail = _move_inside(leaf.shape, slide_w, slide_h)
                        if status == "applied" and layout_ops.is_text_block(leaf):
                            seated = _seat_one(leaves, leaf, slide_h, page_area)
                            if seated:
                                detail = detail.rstrip("。") + "；" + seated
                        op["status"], op["detail"] = status, detail
                else:
                    shape = leaf.shape
                    if not getattr(shape, "has_text_frame", False):
                        op["status"], op["detail"] = "manual", "该对象没有文本框，需要人工调整排版。"
                        continue
                    was_wrap = getattr(shape.text_frame, "word_wrap", None)
                    shape.text_frame.word_wrap = True
                    need, lines = _need_height_in(_text(shape), max(_explicit_sizes(shape) or [12.0]),
                                                  shape.width / EMU_IN)
                    old_h = shape.height / EMU_IN
                    new_h, shift = _grow(shape, need, slide_h)
                    if new_h - old_h < 0.01 and was_wrap is True:
                        # 换行早就开着、高度也够：再撑一次不会有任何像素变化，如实说清楚
                        op["status"] = "skipped"
                        op["detail"] = (f"文本框已开启换行且高度足够（{old_h:.2f}in），"
                                        "自动撑高不会改变版面，需要人工减少文字或改版式。")
                    else:
                        op["status"] = "applied"
                        op["detail"] = (f"开启自动换行，文本框 {old_h:.2f} → {new_h:.2f}in"
                                        f"（估算 {lines} 行）" + (f"，上移 {shift:.2f}in" if shift else "")
                                        + "。")
                        seated = _seat_one(leaves, leaf, slide_h, page_area)
                        if seated:
                            op["detail"] = op["detail"].rstrip("。") + "；" + seated
                if op["status"] == "applied":
                    touched = True
            except Exception as exc:  # a single bad shape must not stop the deck
                op["status"] = "skipped"
                op["detail"] = f"处理失败（{type(exc).__name__}: {exc}），该条保持原样。"

        # 2) 字号放后面：放大后要按新字号把文本框撑高，否则重新解析仍判越界
        for op in page_ops:
            if op["status"] == "manual" or op["action"] != "raise_font":
                continue
            try:
                changed = 0
                before_min = None
                seated = 0
                for leaf in leaves:
                    shape = leaf.shape
                    sizes = _explicit_sizes(shape)
                    if not sizes or len(_text(shape).strip()) < 15:
                        continue
                    if min(sizes) >= structure_rules.MIN_FONT_PT:
                        continue
                    count, before = _set_font(shape, float(op["target_pt"] or FONT_TARGET_PT))
                    if count:
                        changed += count
                        need, _lines = _need_height_in(_text(shape), float(op["target_pt"] or FONT_TARGET_PT),
                                                       shape.width / EMU_IN)
                        _grow(shape, need, slide_h)
                        if _seat_one(leaves, leaf, slide_h, page_area):
                            seated += 1
                        before_min = before if before_min is None else min(before_min, before)
                if changed:
                    op["status"] = "applied"
                    op["detail"] = (f"{before_min:g}pt → {op['target_pt']:g}pt，"
                                    f"共调整 {changed} 段文字，并按新字号撑高文本框"
                                    + (f"，把 {seated} 段收进各自衬底。" if seated else "。"))
                    touched = True
                else:
                    op["status"] = "skipped"
                    op["detail"] = "该页没有可调整的显式字号（字号继承自母版）。"
            except Exception as exc:
                op["status"] = "skipped"
                op["detail"] = f"处理失败（{type(exc).__name__}: {exc}），该条保持原样。"

        # 3) 图层统一整理：位移和放大都可能制造新遮挡，所以即使没有 bring_front 条目也要再清一遍
        if layer_ops or touched:
            outcome, remaining = _clear_occlusions(slide, page_area, strategy=LAYER_STRATEGY)
            done = len(outcome["lowered"]) + len(outcome["lifted"])
            parts = []
            if outcome["lifted"]:
                parts.append(f"把 {len(outcome['lifted'])} 个被压文字提到最上层"
                             f"（{'、'.join(outcome['lifted'][:3])}"
                             f"{' 等' if len(outcome['lifted']) > 3 else ''}）")
            if outcome["lowered"]:
                parts.append(f"把 {len(outcome['lowered'])} 个覆盖对象移到文字下层")
            if done and not remaining:
                text = "；".join(parts) + "，本页遮挡已清除。"
            elif done and remaining:
                text = f"已调整 {done} 处图层，仍有 {remaining} 处遮挡需要人工处理。"
            elif remaining:
                text = "覆盖对象在组合内或图层被锁定，需要人工在「选择窗格」调整。"
            else:
                text = "复检时该页已无遮挡。"
            for op in layer_ops:
                op["status"] = ("applied" if done and not remaining
                                else "partial" if done else "manual" if remaining else "skipped")
                op["detail"] = text
            if touched and not layer_ops and done:
                for op in page_ops:
                    if op["status"] == "applied":
                        op["detail"] = op["detail"].rstrip("。") + f"，并顺带整理了 {done} 处图层遮挡。"
                        break

        # 4) 兜底整理：页面只剩「留待人工」的条目时，先把间距与对齐做掉，
        #    不能交一张修改前后一模一样的图。
        changed_page = _fingerprint(slide) != before
        if tidy_ops or (not changed_page and page_ops):
            # 拥挤页要「拉开/对齐」，稀疏页要「居中」，不能用同一套动作
            dense = tidy_ops or any(op["defect_type"] == "cluttered_layout" for op in page_ops)
            try:
                notes = layout_ops.tidy(leaves, slide_w, slide_h,
                                        mode="cluttered" if dense else "sparse")
            except Exception as exc:  # 整理出问题不能把整次修复带下去
                notes = []
                for op in tidy_ops:
                    op["status"] = "skipped"
                    op["detail"] = f"自动整理失败（{type(exc).__name__}），该页保持原样。"
            if notes:
                changed_page = True
                summary = "；".join(notes[:3]) + (" 等" if len(notes) > 3 else "")
                for op in page_ops:
                    op["tidy"] = notes
                for op in tidy_ops:
                    op["status"] = "partial"
                    op["detail"] = (f"已自动整理 {len(notes)} 处：{summary}。"
                                    "元素数量没有减少，密度指标仍需人工拆页或合并模块。")
                for op in page_ops:
                    if op["status"] in ("manual", "skipped") and op["action"] != "tidy_layout":
                        suffix = f"本页同时整理了 {len(notes)} 处间距/对齐"
                        if suffix not in op["detail"]:
                            op["detail"] = op["detail"].rstrip("。") + "；" + suffix + "。"
            elif tidy_ops:
                if not layout_ops.page_blocks(leaves, page_area):
                    reason = ("本页内容都排在卡片里，自动整理会破坏卡片排版，"
                              "需要人工合并模块或拆页。")
                else:
                    reason = ("页面级文字块之间没有互相压住、也没有明显错位，"
                              "再动就会破坏现有对齐，需要人工重排。")
                for op in tidy_ops:
                    op["status"] = "skipped"
                    op["detail"] = reason

        for op in page_ops:
            op["page_changed"] = bool(changed_page)
    return ops


# -------------------------------------------------------------------- verify
def verify(fixed_path: Path, before: list[dict]) -> dict:
    document, detail = parse_document(fixed_path)
    after, _risks = structure_rules.analyze(detail, document)
    before_struct = [i for i in before
                     if verdicts.is_visible(i) and i.get("evidence_source") == "structure_rule"]

    def key(item):
        return (int(item["slide_index"]), item["defect_type"])

    before_count = Counter(key(i) for i in before_struct)
    after_count = Counter(key(i) for i in after)
    fixed = sum(max(0, before_count[k] - after_count[k]) for k in before_count)
    introduced = sum(max(0, after_count[k] - before_count[k]) for k in after_count)
    remaining = sum(after_count.values())
    return {
        "before_structure": sum(before_count.values()),
        "after_structure": remaining,
        "fixed": fixed,
        "introduced": introduced,
        "by_type": {
            dtype: {
                "before": sum(v for (slide, d), v in before_count.items() if d == dtype),
                "after": sum(v for (slide, d), v in after_count.items() if d == dtype),
            } for dtype in STRUCTURE_TYPES
        },
        "pages": [
            {
                "index": int(slide["index"]),
                "before": sum(v for (s, _d), v in before_count.items() if s == int(slide["index"])),
                "after": sum(v for (s, _d), v in after_count.items() if s == int(slide["index"])),
            } for slide in document.get("slides", [])
        ],
        # 下一轮复核直接用这批残留条目重建计划；只给自动流程用，前端不展示原始明细
        "issues": after,
    }


# ----------------------------------------------------------------------- run
COLLISION_RATIO = 0.3      # 墨迹互相压住的比例
SCAN_TEXT_RATIO = 0.3      # 复扫用的宽松遮挡阈值（修复用的是 R05 的 0.45/3%）
SCAN_COVER_AREA = 0.01


def _overlap_ratio(a: dict, b: dict) -> float:
    ix = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0.0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    smaller = min(a["w"] * a["h"], b["w"] * b["h"])
    return (ix * iy) / max(1e-6, smaller)


def _ink_box(leaf) -> dict:
    """Estimate the area the glyphs actually paint, not the whole text box.

    Text boxes carry padding and often a taller frame than the line they hold,
    so two boxes can intersect while the visible text never touches.  Comparing
    estimated ink rectangles keeps the post-repair warning honest.
    """
    # Leaf 的坐标已经是英寸（_iter_leaves 里除过 EMU），不要再除一次
    box = {"x": leaf.left, "y": leaf.top, "w": leaf.width, "h": leaf.height}
    text = _text(leaf.shape).strip()
    sizes = _explicit_sizes(leaf.shape)
    size = max(sizes) if sizes else 12.0
    _need, lines = _need_height_in(text, size, box["w"])
    box["h"] = min(box["h"], lines * size * 1.32 / 72)
    return box


def visible_overlaps(fixed_path: Path) -> dict:
    """What still looks wrong on the repaired copy, without touching it.

    Two things are reported, both deliberately narrower than "everything that
    looks bad": text lying under an opaque shape (looser thresholds than the
    repair uses) and two text blocks whose glyphs paint over each other.  Pages
    listed here are *not* claimed as fixed; the UI marks them as still needing a
    human, because re-flowing a page is a design decision, not a safe edit.
    """
    prs = Presentation(str(fixed_path))
    width = prs.slide_width / EMU_IN
    height = prs.slide_height / EMU_IN
    page_area = width * height
    pages = []
    for index, slide in enumerate(prs.slides, 1):
        leaves = list(_iter_leaves(slide.shapes))
        items = []
        for text_leaf, cover, ratio in _occlusions(leaves, page_area, SCAN_TEXT_RATIO, SCAN_COVER_AREA):
            if _text(cover.shape).strip():
                continue  # 文字压文字交给下面的墨迹判定，别当成色块遮挡
            items.append({"kind": "cover", "a": text_leaf.shape.name, "b": cover.shape.name,
                          "ratio": round(ratio, 2)})
        texts = [leaf for leaf in leaves if _text(leaf.shape).strip()]
        for position, first in enumerate(texts):
            first_box = _ink_box(first)
            for second in texts[position + 1:]:
                ratio = _overlap_ratio(first_box, _ink_box(second))
                if ratio >= COLLISION_RATIO:
                    items.append({"kind": "text", "a": first.shape.name, "b": second.shape.name,
                                  "ratio": round(ratio, 2)})
        if items:
            pages.append({"index": index, "count": len(items), "items": items[:3]})
    return {"count": sum(page["count"] for page in pages), "pages": pages}


def _stage(status: dict, key: str, state: str, message: str = "") -> None:
    for stage in status["stages"]:
        if stage["key"] == key:
            stage["status"] = state
    if message:
        status["message"] = message
        status["log"].append({"at": _now(), "text": message})


def _save(task_dir: Path, repair_id: str, status: dict) -> None:
    status["updated_at"] = _now()
    _write_json(repairs_root(task_dir) / repair_id / "status.json", status)


def _public(status: dict) -> dict:
    return {k: v for k, v in status.items() if not k.startswith("_")}


def start(task_dir: Path, payload: dict, settings: dict, api_key: str,
          backend: str = "api", review: bool = False,
          max_rounds: int = REVIEW_MAX_ROUNDS) -> dict:
    """Create a repair record and run it in a background thread.

    ``review`` turns on the multi-pass mode: after each pass the repaired copy is
    re-analysed, whatever still fires is planned again, and the loop stops when
    the rules pass, the score stops improving, or ``max_rounds`` is reached.
    """
    root = repairs_root(task_dir)
    root.mkdir(parents=True, exist_ok=True)
    repair_id = _new_id()
    ops = build_plan(payload)
    rounds = max(1, min(int(max_rounds or REVIEW_MAX_ROUNDS), REVIEW_MAX_ROUNDS)) if review else 1
    status: dict[str, Any] = {
        "repair_id": repair_id,
        "task_id": payload.get("task", {}).get("id") or payload.get("id"),
        "status": "queued",
        "progress": 0.02,
        "message": "已接收修复请求，等待开始。",
        "plan_source": "rule",
        "model": None,
        "degraded": "",
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "review": {"enabled": bool(review), "max_rounds": rounds, "rounds": []},
        "summary": {"issues": len(ops),
                    "applied": 0, "manual": sum(1 for op in ops if op["action"] == "manual"),
                    "skipped": 0, "partial": 0, "rounds": 0, "unchanged": 0,
                    "hints": len(load_hints(task_dir))},
        "stages": [
            {"key": "plan", "label": "读取缺陷清单", "status": "pending"},
            {"key": "model", "label": "规划修复动作", "status": "pending"},
            {"key": "apply", "label": "写入修复副本", "status": "pending"},
            {"key": "render", "label": "渲染修复后页面", "status": "pending"},
            {"key": "verify", "label": "复检修复结果", "status": "pending"},
        ],
        "log": [],
        "ops": [],
        "pages": [],
        "output": None,
        "verify": None,
    }
    _save(task_dir, repair_id, status)
    thread = threading.Thread(target=_run,
                              args=(task_dir, repair_id, payload, settings, api_key, backend,
                                    bool(review), rounds),
                              name=f"repair-{repair_id}", daemon=True)
    thread.start()
    return status


def demote_invisible(ops: list[dict], visuals: dict[int, dict]) -> set[int]:
    """Pages whose render did not change must not report "已修复".

    A z-order shuffle that leaves every pixel intact is not a fix, whatever the
    rule says.  Returns the set of page indexes that were demoted so the caller
    can flag them in the UI.
    """
    invisible = {index for index, item in visuals.items() if not item.get("visible")}
    if not invisible:
        return set()
    demoted: set[int] = set()
    for op in ops:
        if int(op.get("slide_index") or 0) not in invisible:
            continue
        if op.get("status") != "applied":
            continue
        visual = visuals[int(op["slide_index"])]
        op["status"] = "partial"
        op["detail"] = (op.get("detail", "").rstrip("。")
                        + "；但像素级比对显示修复前后画面一致（差异 "
                        + f"{visual['ratio'] * 100:.2f}%、{visual['px']} 像素），"
                        + "这次动作只改了图层/几何顺序，肉眼看不到效果——这些文字在画面上本来就"
                        + "没有被真正遮住，是规则按文本框面积判定出来的误报，本页画面无需改动。")
        demoted.add(int(op["slide_index"]))
    return demoted


def _run(task_dir: Path, repair_id: str, payload: dict, settings: dict,
         api_key: str, backend: str, review: bool = False, max_rounds: int = 1) -> None:
    folder = repairs_root(task_dir) / repair_id
    status = load_status(task_dir, repair_id) or {}
    try:
        status["status"] = "running"
        _stage(status, "plan", "active", "读取复核后的缺陷清单…")
        _save(task_dir, repair_id, status)

        issues = payload.get("issues") or []
        hints = load_hints(task_dir)
        visible_all = [issue for issue in issues if verdicts.is_visible(issue)]
        first_plan = build_plan(payload)
        status["ops"] = deepcopy(first_plan)
        status["progress"] = 0.1
        _stage(status, "plan", "done",
               f"共 {len(first_plan)} 条可处理条目，其中 {sum(1 for op in first_plan if op['action'] == 'manual')} 条需要人工。"
               + (f" 已读取 {len(hints)} 页人工修改想法。" if hints else ""))
        _save(task_dir, repair_id, status)

        source = Path(task_dir) / "source" / "source.pptx"
        if not source.is_file():
            raise FileNotFoundError("源文件缺失，无法生成修复副本。")
        fixed = folder / "fixed.pptx"
        shutil.copy2(source, fixed)
        prs = Presentation(str(fixed))

        total = max(1, int(max_rounds or 1)) if review else 1
        current_issues = issues
        all_ops: list[dict] = []
        meta: dict = {"source": "rule", "model": None, "degraded": "", "summary": ""}
        result: dict | None = None
        last_after: int | None = None

        for round_no in range(1, total + 1):
            plan_input = (payload if round_no == 1
                          else {"issues": current_issues, "document": payload.get("document"),
                                "task": payload.get("task")})
            plan = build_plan(plan_input, round_no)
            if not plan and round_no > 1:
                break
            prefix = f"第 {round_no} 轮："
            _stage(status, "model", "active", f"{prefix}让模型在允许的动作里选择修复方式…")
            status["message"] = f"{prefix}规划修复动作"
            status["progress"] = min(0.30, 0.2 + 0.04 * round_no)
            _save(task_dir, repair_id, status)

            history = status["review"]["rounds"][-1] if status["review"]["rounds"] else None
            meta = refine(plan, payload, settings, api_key, backend, context={
                "round": round_no if total > 1 else 0,
                "max_rounds": total if total > 1 else 0,
                "hints": hints,
                "previous": history,
                "layouts": deck_layout(fixed),
            })
            if round_no == 1:
                status.update(plan_source=meta["source"], model=meta["model"],
                              degraded=meta["degraded"], plan_summary=meta.get("summary") or "")
            status["progress"] = min(0.36, 0.24 + 0.03 * round_no)
            _stage(status, "model", "done",
                   f"{prefix}模型已给出修复方案。" if meta["source"] == "llm" else f"{prefix}按规则计划执行。")
            if meta.get("degraded") and meta["degraded"] not in [line["text"] for line in status["log"]]:
                status["log"].append({"at": _now(), "text": meta["degraded"]})
            _save(task_dir, repair_id, status)

            _stage(status, "apply", "active", f"{prefix}在副本上应用 {len(plan)} 条修改…")
            apply_plan(prs, plan)
            prs.save(str(fixed))
            all_ops.extend(plan)
            status["ops"] = deepcopy(all_ops)
            counts = Counter(op["status"] for op in plan)
            status["progress"] = min(0.55, 0.38 + 0.14 * round_no)
            _stage(status, "apply", "done",
                   f"{prefix}写入 {counts.get('applied', 0)} 条修改，{counts.get('manual', 0) + counts.get('partial', 0)} 条留待人工。")
            _save(task_dir, repair_id, status)

            # 每轮都复检：结果页看到的是「累计改完之后还剩多少」，而不是最后一轮的局部数字
            result = verify(fixed, issues)
            round_record = {
                "round": round_no,
                "issues": len(plan),
                "applied": counts.get("applied", 0),
                "manual": counts.get("manual", 0),
                "before": result["before_structure"],
                "after": result["after_structure"],
                "introduced": result["introduced"],
                "note": meta.get("summary") or "",
            }
            status["review"]["rounds"].append(round_record)
            status["summary"]["rounds"] = round_no
            status["log"].append({
                "at": _now(),
                "text": (f"第 {round_no} 轮复检：结构问题 {result['before_structure']} → "
                         f"{result['after_structure']} 条，本轮新增 {result['introduced']} 条。"),
            })
            _save(task_dir, repair_id, status)

            if not review or round_no >= total:
                break
            if result["after_structure"] == 0:
                status["log"].append({"at": _now(), "text": "结构规则已全部通过，停止后续轮次。"})
                break
            # 只剩「人工重排」类的条目时，再调模型也不会改变结果，白白消耗 token
            auto_left = [item for item in result["issues"] if _default_action(item) in PROVABLE]
            if not auto_left:
                status["log"].append({
                    "at": _now(),
                    "text": f"剩余 {len(result['issues'])} 条都属于必须人工重排的版式问题，停止自动轮次。",
                })
                break
            # 与上一轮比：没有继续下降就收手，避免原地打转
            if last_after is not None and result["after_structure"] >= last_after:
                status["log"].append({
                    "at": _now(),
                    "text": f"第 {round_no} 轮没有进一步减少结构问题（仍为 {result['after_structure']} 条），停止继续消耗模型调用。",
                })
                break
            last_after = result["after_structure"]
            current_issues = result["issues"]
            status["log"].append({
                "at": _now(),
                "text": f"仍有 {result['after_structure']} 条结构问题，进入第 {round_no + 1} 轮针对性修复。",
            })
            _save(task_dir, repair_id, status)

        if result is None:
            result = verify(fixed, issues)

        _stage(status, "render", "active", "用 PowerPoint 渲染修复后的页面…")
        status["message"] = "渲染修复后页面"
        status["progress"] = 0.62
        _save(task_dir, repair_id, status)
        render = render_pptx(fixed, folder / "renders", RENDER_LONG_EDGE, RENDER_TIMEOUT_S)
        rendered = {int(item["index"]): item for item in render.get("slides", []) if item.get("status") == "rendered"}
        status["render"] = {"ok": bool(render.get("ok")), "error": render.get("error") or "",
                            "pages": len(rendered)}
        status["progress"] = 0.9
        _stage(status, "render", "done",
               f"{len(rendered)} 页渲染完成。" if rendered else "渲染未生成页面图，仍可下载修复文件。")
        _save(task_dir, repair_id, status)

        # 像素复核：几何指纹只能证明「属性动过」，证明不了「画面变过」。
        # 纯 z-order 调整会让规则满意、图一模一样，这里把它揭穿。
        visuals = visual_diff.compare_pages(task_dir / "renders", folder / "renders", set(rendered))
        invisible = demote_invisible(all_ops, visuals)
        if invisible:
            status["ops"] = deepcopy(all_ops)
            names = "、".join(f"P{index:02d}" for index in sorted(invisible))
            status["log"].append({
                "at": _now(),
                "text": (f"{len(invisible)} 页的改动只影响图层顺序，渲染图与修复前一致（{names}），"
                         f"已按「画面无可见变化」处理，不再计入已修复。"),
            })
            _save(task_dir, repair_id, status)

        _stage(status, "verify", "active", "用同一套结构规则复检修复后的文件，并标注原始缺陷位置…")
        _save(task_dir, repair_id, status)
        result["overlaps"] = visible_overlaps(fixed)
        status["verify"] = {key: value for key, value in result.items() if key != "issues"}

        document = payload.get("document") or {}
        try:
            annotate.annotate_pages(task_dir, folder / "annots", issues,
                                    float(document.get("slide_width_in") or 13.333),
                                    float(document.get("slide_height_in") or 7.5))
        except Exception as exc:  # 标注图只是辅助，失败不能让整次修复失败
            status["log"].append({"at": _now(), "text": f"标注图生成失败（{type(exc).__name__}），不影响修复文件。"})

        page_visible = Counter(int(issue.get("slide_index") or 0) for issue in visible_all)
        page_before = {int(page["index"]): int(page["before"]) for page in result["pages"]}
        page_after = {int(page["index"]): int(page["after"]) for page in result["pages"]}
        page_changed: dict[int, bool] = {}
        page_tidy: Counter = Counter()
        page_status: dict[int, Counter] = {}
        for op in all_ops:
            index = int(op.get("slide_index") or 0)
            page_changed[index] = page_changed.get(index, False) or bool(op.get("page_changed"))
            page_tidy[index] = max(page_tidy[index], len(op.get("tidy") or []))
            page_status.setdefault(index, Counter())[op["status"]] += 1
        pages = []
        for slide in document.get("slides", []):
            index = int(slide["index"])
            fixed_here = max(0, page_before.get(index, 0) - page_after.get(index, 0))
            counts = page_status.get(index, Counter())
            visual = visuals.get(index)
            # 画面没变就算没改：几何指纹只是必要条件，渲染像素才是证据
            changed = bool(page_changed.get(index)) and (visual["visible"] if visual else True)
            pages.append({
                "index": index,
                "before_url": f"/api/tasks/{status['task_id']}/slides/{index}/image?kind=render",
                "after_url": (f"/api/tasks/{status['task_id']}/repairs/{repair_id}/slides/{index}/image"
                              if index in rendered else None),
                "annot_url": (f"/api/tasks/{status['task_id']}/repairs/{repair_id}/slides/{index}/annot"
                              if (folder / "annots" / f"s{index:04d}.png").is_file() else None),
                "ops": page_visible.get(index, 0),
                # 逐条按动作结果计数：只在规则层面清零、画面没动的条目不再算「已修」
                "applied": counts.get("applied", 0),
                "partial": counts.get("partial", 0),
                "manual": counts.get("manual", 0),
                "before": page_before.get(index, 0),
                "after": page_after.get(index, 0),
                "merged": fixed_here,
                "overlaps": next((page["count"] for page in result["overlaps"]["pages"]
                                  if page["index"] == index), 0),
                "changed": changed,
                "tidy": int(page_tidy.get(index, 0)),
                "visual": visual,
                "visual_note": visual_diff.describe(visual) if visual else "",
            })
        status["pages"] = pages

        counts = Counter(op["status"] for op in all_ops)
        status["summary"].update(
            # 「已修」= 动手且画面真的变了的条目；规则清零数单独报，不混为一谈
            applied=counts.get("applied", 0),
            partial=counts.get("partial", 0),
            manual=counts.get("manual", 0),
            skipped=counts.get("skipped", 0),
            rule_resolved=max(0, result["before_structure"] - result["after_structure"]),
            issues=len(visible_all),
            rounds=status["review"]["rounds"].__len__(),
            # 画面确实变了的页：这是「修好了」的唯一凭证
            visible_pages=sum(1 for page in pages if page["changed"]),
            # 有缺陷但画面一像素都没动的页：必须报出来，不能当成「修好了」
            unchanged=sum(1 for page in pages if page["ops"] > 0 and not page["changed"]),
        )
        # 轮次记录跟 summary 用同一套口径，否则进度页显示「已修 12」而结果页显示「8」
        if status["review"]["rounds"]:
            status["review"]["rounds"][-1].update(
                applied=counts.get("applied", 0),
                manual=counts.get("manual", 0),
                partial=counts.get("partial", 0),
            )
        stalled = [page for page in pages if page["ops"] > 0 and not page["changed"]]
        if stalled:
            names = "、".join(f"P{page['index']:02d}" for page in stalled[:6])
            status["log"].append({
                "at": _now(),
                "text": (f"{len(stalled)} 页有缺陷但画面没有产生任何可见改动（{names}"
                         f"{' 等' if len(stalled) > 6 else ''}），原因见各页动作明细。"),
            })
        status["verify"] = dict(status["verify"] or {})
        status["verify"]["visual"] = {
            "checked": len(visuals),
            "visible_pages": sum(1 for page in pages if page["changed"]),
            "identical": sorted(index for index, item in visuals.items() if not item["visible"]),
        }
        status["output"] = {
            "file": fixed.name,
            "name": (Path((payload.get("document") or {}).get("file_name") or "slides.pptx").stem + "_已修复.pptx"),
            "size": fixed.stat().st_size,
            "url": f"/api/tasks/{status['task_id']}/repairs/{repair_id}/file",
            "bundle_url": f"/api/tasks/{status['task_id']}/repairs/{repair_id}/bundle",
        }
        status["progress"] = 1.0
        status["status"] = "completed"
        status["finished_at"] = _now()
        rounds_done = len(status["review"]["rounds"])
        _stage(status, "verify", "done",
               f"结构类问题 {result['before_structure']} → {result['after_structure']} 条"
               + (f"，新增 {result['introduced']} 条。" if result["introduced"] else "，未新增问题。")
               + (f" 画面有可见变化 {sum(1 for page in pages if page['changed'])} 页。"
                  if visuals else "")
               + (f" 共复核 {rounds_done} 轮。" if rounds_done > 1 else "")
               + (f" 另有 {len(result['overlaps']['pages'])} 页仍有可见重叠，需要人工确认。"
                  if result["overlaps"]["count"] else ""))
        _write_json(folder / "plan.json",
                    {"ops": all_ops, "meta": meta, "rounds": status["review"]["rounds"]})
        _save(task_dir, repair_id, status)
        _prune(task_dir)
    except Exception as exc:
        status["status"] = "failed"
        status["progress"] = 1.0
        status["finished_at"] = _now()
        status["message"] = f"修复失败：{type(exc).__name__}: {exc}"
        for stage in status.get("stages", []):
            if stage["status"] == "active":
                stage["status"] = "failed"
        status.setdefault("log", []).append({"at": _now(), "text": status["message"]})
        try:
            _save(task_dir, repair_id, status)
        except OSError:
            pass

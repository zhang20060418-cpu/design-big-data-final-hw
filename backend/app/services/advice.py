"""Overall revision advice and an exportable repair prompt.

Two artefacts are produced from the reviewed findings:

``compose``
    A deterministic action plan.  It only uses the recorded evidence, page
    numbers, coordinates and recommendations; it never introduces new facts and
    never hedges ("不确定" / "也许").  Every action carries an acceptance line so
    the result can be checked, not just read.

``build_prompt``
    A Markdown prompt that hands the confirmed findings to an external editing
    agent.  Dismissed findings never reach it.

An optional single LLM call can rewrite the wording (``generate`` with
``backend='api'``).  Its output goes through the same assertive filter and falls
back to the deterministic text whenever the model hedges, fails or is not
configured.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from ..config import DEFECT_LABELS_ZH
from .verdicts import is_dismissed, is_visible

ORDER = [
    "content_overflow_cutoff",
    "occluded_content",
    "improper_font_sizing",
    "cluttered_layout",
    "generative_design_anomaly",
    "excessive_whitespace",
]

ACTION_TITLE = {
    "content_overflow_cutoff": "把越界和裁切的对象收回画布",
    "occluded_content": "解除遮挡，恢复被压住的正文",
    "improper_font_sizing": "统一正文字号到 12pt 以上",
    "cluttered_layout": "拆开拥挤页的信息层级",
    "generative_design_anomaly": "重画语义不符的图标",
    "excessive_whitespace": "补满留白页的内容区",
}

DIRECTIVE = {
    "content_overflow_cutoff": "超出行边界的对象移回安全区；长文本改短或拆页，不留自动缩排和截断。",
    "occluded_content": "把压在上层的形状下移图层或移出正文区，被遮的文字整体露出。",
    "improper_font_sizing": "小于 9pt 的正文统一提到 12pt 起；同一层级收成一套字号，不再逐框微调。",
    "cluttered_layout": "按信息层级拆页或合并模块，同层并列元素压到 3 组以内。",
    "generative_design_anomaly": "按标签语义逐一重画图标，删掉重复符号，线宽与描边统一。",
    "excessive_whitespace": "在空白象限补内容模块，或把主体放大并移到页面中心区。",
}

OPERATION_STEPS = {
    "content_overflow_cutoff": [
        "打开「开始 → 选择窗格」，按下面的坐标定位对象，拖回画布内并留出边距。",
        "文字超出文本框时先删冗余字，再考虑缩小字号，保持段落结构不变。",
    ],
    "occluded_content": [
        "在「选择窗格」里把上层形状下移一层，或把它的填充改成透明。",
        "移动后切到放映视图，确认该段文字完整可见。",
    ],
    "improper_font_sizing": [
        "按层级批量设字号：正文 12pt 起，标题按现有层级等比放大。",
        "同一页内同层级文字统一字号与行距，避免逐框不同。",
    ],
    "cluttered_layout": [
        "把同层级并列块合并到 3 组以内，放不下就拆成两页。",
        "组与组之间留出一个空行的高度，删除贴边的装饰线。",
    ],
    "generative_design_anomaly": [
        "按标签语义列一张图标清单，一个语义只保留一个图标。",
        "统一线宽、圆角与配色，删除与内容无关的装饰图形。",
    ],
    "excessive_whitespace": [
        "把主体内容放大到占满页面宽度的 80%，或往空白象限补一个数据/要点模块。",
        "调整后检查页面四个象限都有内容分布。",
    ],
}

ACCEPTANCE = {
    "content_overflow_cutoff": "所有对象落在画布内（系统边界容差 0.04in），文本框没有截断和自动缩排。",
    "occluded_content": "页面上每个文本块的可视面积为 100%，与上层图形没有重叠。",
    "improper_font_sizing": "正文最小字号 ≥ 12pt，同一层级字号一致。",
    "cluttered_layout": "每页主要信息块不超过 3 组，组间距均匀。",
    "generative_design_anomaly": "同一组图标语义互不重复，线条与配色统一。",
    "excessive_whitespace": "页面四个象限均有内容分布，主体重心落在页面中部。",
}

PHASES = [
    ("第 1 轮 · 止血", ("content_overflow_cutoff", "occluded_content"),
     "把越界和遮挡的对象移回画布，先让被藏起来的正文重新可见。"),
    ("第 2 轮 · 可读", ("improper_font_sizing",),
     "统一字号与行距，正文不小于 12pt。"),
    ("第 3 轮 · 结构", ("cluttered_layout", "excessive_whitespace"),
     "拆页或合并模块，把密度和留白拉回正常区间。"),
    ("第 4 轮 · 一致", ("generative_design_anomaly",),
     "统一图标、装饰与模板结构，消除重复语义。"),
]

# 用户明确要求建议里不出现不确定推论；模型输出也要过这一层。
HEDGE_WORDS = (
    "不确定", "也许", "或许", "大概", "似乎", "看起来", "可能是", "可能",
    "视情况", "酌情", "建议考虑", "有待", "尚不明确", "疑似", "不妨", "需要结合", "需结合",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")


def has_hedge(text: str) -> bool:
    return any(word in (text or "") for word in HEDGE_WORDS)


def assertive(text: str, fallback: str = "") -> str:
    """Drop hedged sentences so the remaining text states what to do."""
    if not isinstance(text, str):
        return fallback
    kept = [part for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    kept = [part.strip() for part in kept if not has_hedge(part)]
    cleaned = "".join(kept).strip()
    return cleaned if len(cleaned) >= 6 else fallback


def _pages_label(pages: list[int], limit: int = 6) -> str:
    shown = "、".join(f"P{int(page):02d}" for page in pages[:limit])
    return shown + (f" 等 {len(pages)} 页" if len(pages) > limit else "")


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


_CHECK_PREFIX = re.compile(r"^(?:先)?(?:检查|核对|确认)[^，。；]*[，。；]?")
_CHECK_ONLY = ("核对", "检查", "确认", "判断是否", "若", "结合使用场景")


def _action_text(text: str) -> str:
    """Keep only sentences that state a change; drop "go and look at it" lines."""
    text = assertive(str(text or ""))
    if not text:
        return ""
    text = _CHECK_PREFIX.sub("", text).strip()
    if any(word in text for word in _CHECK_ONLY):
        return ""
    return text if len(text) >= 8 else ""


def _fingerprint(issues: list[dict]) -> str:
    ids = sorted(str(issue.get("issue_id")) for issue in issues)
    return hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]


def _verdict(types: set[str], page_count: int, total_pages: int, issues: list[dict] | None = None) -> str:
    content = {"content_overflow_cutoff", "occluded_content", "improper_font_sizing"} & types
    structural = {"cluttered_layout", "excessive_whitespace"} & types
    total = max(1, len(issues or []))
    content_share = sum(1 for issue in issues or [] if issue.get("defect_type") in content) / total
    if structural and content_share < 0.4 and page_count >= max(2, total_pages // 3):
        return "问题跨页出现、密度分布不均，按页面结构重排，不做局部微调。"
    if content and structural:
        return "先把内容收回画布、把字号提上来，再处理密度；版式骨架保留。"
    if content:
        return "问题集中在对象位置与字号，版式骨架不动，按清单逐项改即可。"
    if structural:
        return "对象位置正常，问题在页面密度与留白，按页面重排。"
    return "问题集中在素材与图标的一致性，版式骨架不动。"


def compose(payload: dict) -> dict:
    """Deterministic advice built only from recorded findings."""
    all_issues = payload.get("issues") or []
    issues = [issue for issue in all_issues if is_visible(issue)]
    summary = payload.get("summary") or {}
    document = payload.get("document") or {}
    total_pages = int(summary.get("slide_count") or document.get("slide_count") or 0)
    dismissed = int(summary.get("dismissed_count") or sum(1 for i in all_issues if is_dismissed(i)))
    rejected = int(summary.get("rejected_count") or 0)
    unreviewed = sum(1 for issue in issues if issue.get("review_status") == "unreviewed")

    by_type: dict[str, list[dict]] = {}
    by_page: dict[int, list[dict]] = {}
    for issue in issues:
        by_type.setdefault(issue["defect_type"], []).append(issue)
        by_page.setdefault(int(issue["slide_index"]), []).append(issue)

    pages = sorted(by_page)
    page_count = len(pages)
    high = sum(1 for issue in issues if issue.get("severity") == "high")
    fingerprint = _fingerprint(issues)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")

    if not issues:
        return {
            "generated_at": generated,
            "source": "rule",
            "model": None,
            "fingerprint": fingerprint,
            "stats": {"issues": 0, "pages": 0, "high": 0, "dismissed": dismissed,
                      "rejected": rejected, "unreviewed": 0,
                      "ai_reviewed_pages": int(summary.get("ai_reviewed_pages") or 0),
                      "ai_supported": int(summary.get("ai_supported") or 0)},
            "headline": "本次审查没有留下需要返工的条目。",
            "verdict": "没有可执行的修改动作；未覆盖范围按检测局限单独阅读。",
            "summary": [line for line in [
                f"已排除 {dismissed} 条误判，其余证据均已闭环。" if dismissed else "",
                "可判范围外仍未检查，这份结论不构成整份文稿的质量认证。",
            ] if line],
            "actions": [],
            "page_plan": [],
            "sequence": [],
            "note": "建议只使用本次任务已记录的证据与页码，不引入新事实。",
        }

    # ------------------------------------------------------------ overall view
    ranked_pages = sorted(by_page.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top_pages = [page for page, _ in ranked_pages[:3]]
    top_share = round(sum(len(by_page[page]) for page in top_pages) / len(issues) * 100)

    dominant_type = max(by_type.items(), key=lambda kv: (len(kv[1]), -ORDER.index(kv[0])))[0]
    dominant_pages = sorted({int(i["slide_index"]) for i in by_type[dominant_type]})

    action_types = [t for t in ORDER if t in by_type]
    headline = (f"这份稿子有 {len(issues)} 条待修证据，落在 {page_count} 页，"
                f"归成 {len(action_types)} 类动作。")

    lines = []
    if page_count >= 3:
        lines.append(f"证据集中在 {_pages_label(top_pages)}，这 {len(top_pages)} 页占全部证据的 {top_share}%，先改它们收益最大。")
    else:
        lines.append(f"只涉及 {_pages_label(pages)}，一次返工可以全部改完。")
    lines.append(f"{DEFECT_LABELS_ZH[dominant_type]}是本次最多的一类：{len(by_type[dominant_type])} 条，分布在 {_pages_label(dominant_pages)}。")
    ai_reviewed = int(summary.get("ai_reviewed_pages") or 0)
    ai_supported = int(summary.get("ai_supported") or 0)
    lines.append(
        f"AI 复核覆盖 {ai_reviewed} 页，支持 {ai_supported} 条、不成立 {rejected} 条；"
        f"其余 {unreviewed} 条按规则几何证据处理，同样进下面这张清单。")
    if dismissed:
        lines.append(f"你已核对渲染图并排除 {dismissed} 条误判，这些条目不进报告、建议和导出的 Prompt。")
    image_only = int(document.get("image_only_pages") or 0)
    if image_only:
        lines.append(f"{image_only} 页是整页位图，对象层没有可编辑文本，按图重做这几页，不进对象级修改清单。")
    missing_fonts = document.get("missing_fonts") or []
    if missing_fonts:
        preview = "、".join(list(missing_fonts)[:3])
        lines.append(f"文稿用到本机未安装的字体（{preview}），渲染会替换字形并改变换行；改稿前先装上或换掉。")

    # ---------------------------------------------------------------- actions
    actions = []
    for defect in action_types:
        items = by_type[defect]
        affected = sorted({int(item["slide_index"]) for item in items})
        steps = list(OPERATION_STEPS[defect])
        seen = {step for step in steps}
        for item in items:
            candidate = _action_text(str(item.get("recommendation") or ""))
            if candidate and candidate not in seen:
                steps.append(_short(candidate, 90))
                seen.add(candidate)
            if len(steps) >= 3:
                break
        actions.append({
            "defect_type": defect,
            "label": DEFECT_LABELS_ZH[defect],
            "title": ACTION_TITLE[defect],
            "directive": f"{_pages_label(affected)}：{DIRECTIVE[defect]}",
            "pages": affected,
            "pages_label": _pages_label(affected),
            "count": len(items),
            "high": any(item.get("severity") == "high" for item in items),
            "steps": steps[:4],
            "acceptance": ACCEPTANCE[defect],
        })

    # -------------------------------------------------------------- page plan
    page_plan = []
    for page in sorted(by_page, key=lambda p: (0 if any(i.get("severity") == "high" for i in by_page[p]) else 1, -len(by_page[p]), p)):
        counts: dict[str, int] = {}
        for issue in by_page[page]:
            counts[issue["defect_type"]] = counts.get(issue["defect_type"], 0) + 1
        focus = "、".join(f"{DEFECT_LABELS_ZH[key]} {value}" for key, value in
                         sorted(counts.items(), key=lambda kv: ORDER.index(kv[0])))
        page_plan.append({
            "slide_index": page,
            "label": f"P{page:02d}",
            "count": len(by_page[page]),
            "focus": focus,
            "high": any(issue.get("severity") == "high" for issue in by_page[page]),
        })

    sequence = []
    for title, types, detail in PHASES:
        present = [t for t in types if t in by_type]
        if not present:
            continue
        phase_pages = sorted({int(i["slide_index"]) for t in present for i in by_type[t]})
        sequence.append({
            "title": title,
            "detail": detail,
            "pages": phase_pages,
            "pages_label": _pages_label(phase_pages),
            "count": sum(len(by_type[t]) for t in present),
        })

    return {
        "generated_at": generated,
        "source": "rule",
        "model": None,
        "fingerprint": fingerprint,
        "stats": {
            "issues": len(issues), "pages": page_count, "high": high,
            "dismissed": dismissed, "rejected": rejected, "unreviewed": unreviewed,
            "ai_reviewed_pages": ai_reviewed, "ai_supported": ai_supported,
        },
        "headline": assertive(headline, headline),
        "verdict": _verdict(set(by_type), page_count, total_pages or page_count, issues),
        "summary": [assertive(line, line) for line in lines if line],
        "actions": actions,
        "page_plan": page_plan,
        "sequence": sequence,
        "note": ("建议只使用本次任务已记录的证据、页码与几何坐标，不引入新事实；"
                 "被标记为误判的条目不参与生成。"),
    }


# --------------------------------------------------------------------- model
SYSTEM_PROMPT = (
    "你是演示文稿版式修复顾问。输入是审查系统已经记录的缺陷证据，你负责把它写成整体修改意见和逐项动作。\n"
    "硬性规则：\n"
    "1) 只使用输入里出现的事实（页码、类型、证据、坐标、建议），不新增缺陷、不猜测原因、不承诺修改后的具体观感。\n"
    "2) 语气必须坚定：直接写要做什么、做到什么程度。禁止出现这些词：不确定、也许、或许、大概、似乎、"
    "看起来、可能、视情况、酌情、建议考虑、有待、尚不明确、疑似。\n"
    "3) 每条动作给出 1–3 个可执行步骤，每步是一个具体操作，不写泛泛的原则。\n"
    "4) 用简体中文，句子短，不用感叹号，不写客套话。\n"
    "5) 只输出 JSON，不要解释。"
)

OUTPUT_SCHEMA = (
    '{"headline":"一句话总体判断，含条数与页数","verdict":"一句整体结论，说明版式骨架保留还是重排",'
    '"summary":["3-5 条总体观察，每条一句话"],'
    '"actions":[{"defect_type":"六类键名之一","title":"动作标题","directive":"针对哪些页做什么",'
    '"steps":["步骤1","步骤2"]}],'
    '"page_plan":[{"slide_index":3,"focus":"这一页要改什么"}]}'
)


def _brief(payload: dict, advice: dict) -> dict:
    issues = [issue for issue in payload.get("issues") or [] if is_visible(issue)][:40]
    findings = []
    for issue in issues:
        box = issue.get("bbox") or {}
        findings.append({
            "page": issue.get("slide_index"),
            "type": DEFECT_LABELS_ZH.get(issue.get("defect_type"), issue.get("defect_type")),
            "issue": _short(issue.get("title"), 40),
            "evidence": _short(issue.get("evidence_summary"), 160),
            "recommendation": _short(issue.get("recommendation"), 120),
            "box": (f"x={box.get('x', 0):.2f},y={box.get('y', 0):.2f},"
                    f"w={box.get('w', 0):.2f},h={box.get('h', 0):.2f}") if box else None,
        })
    document = payload.get("document") or {}
    return {
        "document": {
            "pages": document.get("slide_count"),
            "aspect": document.get("aspect_label"),
            "image_only_pages": document.get("image_only_pages"),
        },
        "stats": advice.get("stats"),
        "current_plan": [{"title": a["title"], "pages": a["pages"], "acceptance": a["acceptance"]}
                         for a in advice.get("actions") or []],
        "findings": findings,
    }


def _ask_model(advice: dict, payload: dict, settings: dict, api_key: str) -> tuple[dict | None, str]:
    from .mllm import (_extract_json, _post_chat, completion_text, validate_base_url)

    base = validate_base_url(settings.get("api_base_url", ""),
                             bool(settings.get("allow_private_base_url")),
                             bool(settings.get("allow_insecure_base_url")))
    model = settings.get("api_model") or ""
    payload_out = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_brief(payload, advice), ensure_ascii=False)
             + "\n\n按下面的结构输出 JSON：\n" + OUTPUT_SCHEMA},
        ],
        "temperature": 0.3,
        "max_tokens": 1600,
    }
    if settings.get("response_format_json", True):
        payload_out["response_format"] = {"type": "json_object"}
    status, body = _post_chat(base, api_key, payload_out, float(settings.get("api_timeout_s") or 60))
    if status >= 400 or not isinstance(body, dict):
        raise ValueError(f"接口返回 HTTP {status}")
    content, _meta = completion_text(body)
    data = _extract_json(content)
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return data, model


def _apply_model(rule: dict, data: dict) -> dict:
    out = dict(rule)
    headline = assertive(data.get("headline") or "", "")
    if headline:
        out["headline"] = headline
    verdict = assertive(data.get("verdict") or "", "")
    if verdict:
        out["verdict"] = verdict
    lines = [assertive(item, "") for item in (data.get("summary") or []) if isinstance(item, str)]
    lines = [line for line in lines if line]
    if lines:
        out["summary"] = lines[:5]

    incoming = {}
    for item in data.get("actions") or []:
        if isinstance(item, dict) and item.get("defect_type") in ACCEPTANCE:
            incoming[item["defect_type"]] = item
    actions = []
    for action in rule.get("actions") or []:
        patch = incoming.get(action["defect_type"])
        if not patch:
            actions.append(action)
            continue
        merged = dict(action)
        title = assertive(patch.get("title") or "", "")
        if title:
            merged["title"] = _short(title, 30)
        directive = assertive(patch.get("directive") or "", "")
        if directive:
            merged["directive"] = _short(directive, 160)
        steps = [assertive(step, "") for step in (patch.get("steps") or []) if isinstance(step, str)]
        steps = [step for step in steps if step][:3]
        if steps:
            merged["steps"] = (steps + [s for s in action["steps"] if s not in steps])[:4]
        actions.append(merged)
    out["actions"] = actions

    focus = {}
    for item in data.get("page_plan") or []:
        if isinstance(item, dict):
            try:
                focus[int(item.get("slide_index"))] = assertive(item.get("focus") or "", "")
            except (TypeError, ValueError):
                continue
    out["page_plan"] = [{**page, "focus": focus.get(page["slide_index"]) or page["focus"]}
                        for page in rule.get("page_plan") or []]
    return out


# --------------------------------------------------------------------- cache
def _cache_path(directory: Path) -> Path:
    return Path(directory) / "reviews" / "advice.json"


def load_cache(directory: Path) -> dict | None:
    try:
        data = json.loads(_cache_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_cache(directory: Path, advice: dict) -> None:
    if advice.get("source") != "llm":
        return
    path = _cache_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "fingerprint": advice.get("fingerprint"),
        "generated_at": advice.get("generated_at"),
        "model": advice.get("model"),
        "advice": advice,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def overlay(advice: dict, directory: Path) -> dict:
    """Use a cached model-written version when it still matches the findings."""
    cached = load_cache(directory)
    if not cached or cached.get("fingerprint") != advice.get("fingerprint"):
        return advice
    stored = cached.get("advice")
    if not isinstance(stored, dict) or stored.get("source") != "llm":
        return advice
    merged = dict(stored)
    merged["stats"] = advice["stats"]
    merged["fingerprint"] = advice["fingerprint"]
    merged["stale"] = False
    return merged


def generate(task: dict, directory: Path, payload: dict, *, backend: str = "api",
             settings: dict | None = None, api_key: str = "") -> dict:
    """Compose the plan, then optionally let the configured model rewrite it."""
    rule = compose(payload)
    if backend != "api":
        rule["degraded"] = "未选择模型润色，本次建议由规则合成。"
        return rule
    settings = settings or {}
    if not (api_key and settings.get("api_base_url") and settings.get("api_model")):
        rule["degraded"] = "尚未配置模型服务，本次建议由规则合成。"
        return rule
    if not rule.get("actions"):
        rule["degraded"] = "本次没有待修条目，未调用模型。"
        return rule
    try:
        data, model = _ask_model(rule, payload, settings, api_key)
    except Exception as exc:  # network, auth, JSON, budget — same fallback
        rule["degraded"] = f"模型调用失败（{type(exc).__name__}: {exc}），本次建议由规则合成。"
        return rule
    refined = _apply_model(rule, data)
    refined["source"] = "llm"
    refined["model"] = model
    refined["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    refined["degraded"] = ""
    save_cache(directory, refined)
    return refined


# -------------------------------------------------------------------- prompt
def build_prompt(payload: dict) -> str:
    """Markdown prompt a repair agent can execute page by page."""
    issues = [issue for issue in payload.get("issues") or [] if is_visible(issue)]
    document = payload.get("document") or {}
    requirements = payload.get("user_requirements") or {}
    options = payload.get("options") or {}
    total_pages = int(document.get("slide_count") or 0)
    width = document.get("slide_width_in") or 13.333
    height = document.get("slide_height_in") or 7.5

    by_page: dict[int, list[dict]] = {}
    for issue in issues:
        by_page.setdefault(int(issue["slide_index"]), []).append(issue)

    lines: list[str] = []
    add = lines.append
    add("# PPTX 版式修复任务")
    add("")
    add("## 1. 角色")
    add("")
    add("你是 PowerPoint 版式修复执行者。下面的缺陷已由审查系统记录并确认，你按清单逐条修改，不重新判断缺陷是否存在。")
    add("")
    add("## 2. 文稿")
    add("")
    add(f"- 文件：{document.get('file_name') or '未命名.pptx'}")
    add(f"- 页数：{total_pages} 页；画布：{document.get('aspect_label') or '16:9'}（{width} × {height} 英寸）")
    add(f"- 页面构成：可编辑 {document.get('editable_pages', 0)} 页 / 混合 {document.get('mixed_pages', 0)} 页 / 整页位图 {document.get('image_only_pages', 0)} 页")
    add(f"- 待修条目：{len(issues)} 条，涉及 {len(by_page)} 页")
    if options.get("purpose") or options.get("style"):
        add(f"- 用途：{options.get('purpose') or '未指定'}；风格：{options.get('style') or '未指定'}")
    add("")
    add("## 3. 硬性约束")
    add("")
    add("- 文字内容只允许删冗余、断句和拆页，不改变原有语义与数据。")
    add("- 不新增页面、不删除页面、不改母版与主题色；同一份文稿的字体族保持一致。")
    add("- 只改清单里列出的页和对象，其他页保持原样。")
    add("- 每个对象改动后必须落在画布内，文本框不得出现自动缩排或截断。")
    if document.get("image_only_pages"):
        add(f"- 整页位图的页面无法在对象层修改，只输出该页的重做说明，不动其余页。")
    add("")
    add("## 4. 逐页修改清单")
    add("")
    for page in sorted(by_page):
        items = sorted(by_page[page], key=lambda i: ORDER.index(i["defect_type"]))
        add(f"### 第 {page} 页（P{page:02d}）")
        add("")
        for index, issue in enumerate(items, 1):
            defect = issue["defect_type"]
            add(f"{index}. **[{DEFECT_LABELS_ZH[defect]}] {issue.get('title') or ''}**")
            evidence = assertive(str(issue.get("evidence_summary") or ""), "")
            if evidence:
                add(f"   - 证据：{_short(evidence, 180)}")
            box = issue.get("bbox")
            if box:
                add(f"   - 位置：x={box['x']:.2f} / y={box['y']:.2f} / w={box['w']:.2f} / h={box['h']:.2f}（英寸）"
                    + (f"；元素 {issue.get('element_id')}" if issue.get("element_id") else ""))
            fix = _action_text(str(issue.get("recommendation") or "")) or DIRECTIVE[defect]
            add(f"   - 处理：{_short(fix, 160)}")
            add(f"   - 验收：{ACCEPTANCE[defect]}")
        add("")

    add("## 5. 执行顺序")
    add("")
    present = {issue["defect_type"] for issue in issues}
    step = 1
    for _title, types, detail in PHASES:
        if not any(t in present for t in types):
            continue
        pages = sorted({int(i["slide_index"]) for i in issues if i["defect_type"] in types})
        add(f"{step}. {detail} 涉及 {_pages_label(pages)}。")
        step += 1
    add("")

    add("## 6. 输出格式")
    add("")
    add("按页输出，每页一段：")
    add("")
    add("```")
    add("第 N 页")
    add("- 改动对象：<对象名 / 元素 ID>")
    add("- 改动内容：<调整前 → 调整后，写明字号、位置、层级等具体数值>")
    add("- 关闭的问题：<对应上面的条目编号>")
    add("```")
    add("")
    add("全部页面输出完后，给出 3 条以内的整体结论，说明还剩哪些条目没有关闭。")
    add("")

    dismissed = [issue for issue in payload.get("issues") or [] if is_dismissed(issue)]
    if dismissed:
        add("## 7. 说明")
        add("")
        add(f"本次已由人工核对渲染图，排除 {len(dismissed)} 条误判；这些条目不进修改清单，不要在对应页做任何改动。")
        add("")
    add("---")
    add(f"来源：SlideSentinel 审查任务 {payload.get('task', {}).get('id') or ''} · 生成于 {datetime.now().astimezone().isoformat(timespec='seconds')}")
    return "\n".join(lines) + "\n"

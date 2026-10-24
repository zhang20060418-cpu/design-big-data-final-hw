"""按需视觉复核客户端。

三种模式，行为与标记严格区分：
  api   真实调用 OpenAI 兼容的多模态接口（Key 只从环境变量读取）
  host  读取宿主多模态工具（WorkBuddy ViewImage）产出的 JSON，本程序不发 HTTP 请求
  mock  不读图、不联网，仅用于验证调度链路，结果强制标记 simulated=true

设计约束：
- 只对传入的页面调用一次，按页去重；
- 调用次数由调度器控制，本模块不自行重试；
- 任何情况下都不把 API Key 写入日志或返回值；
- 模型输出必须是 JSON，且 observations 不能为空；解析失败按错误记录，不编造结论。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

VERDICTS = {
    "visible_clip": "可见裁切",
    "visible_occlusion": "可见遮挡",
    "low_readability": "可读性不足",
    "no_visible_problem": "未见可见问题",
    "uncertain": "无法确定",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def build_prompt(cases: list[dict[str, Any]], page_w: float, page_h: float,
                 image_w: int | None, image_h: int | None) -> str:
    """把对象信息与结构证据写进提示词，要求结论必须引用可观察位置。"""
    lines = [
        "你是 PPT 版面视觉质量审查员。请只根据这张页面截图做判断。",
        f"页面尺寸 {page_w:g}×{page_h:g} pt"
        + (f"，截图像素 {image_w}×{image_h}" if image_w and image_h else "")
        + "。",
        "",
        "待核对对象：",
    ]
    for c in cases:
        geo = c.get("geometry_evidence") or {}
        lay = c.get("text_layout") or {}
        ov = c.get("page_overflow") or {}
        ink = (c.get("render_evidence") or {}).get("ink") or {}
        edge = ink.get("edge") or {}
        excerpt = " ".join((c.get("text_excerpt") or "").replace("\r", "\n").split())
        lines += [
            f"- shape_id={c['shape_id']}，名称“{c['name']}”，页码 {c['slide']}",
            f"  文本摘录：{excerpt[:90]}",
            f"  对象外框：{_box(geo.get('frame_bbox_pt'))}；COM 可见文字范围：{_box(lay.get('visible_bbox_pt'))}",
            f"  文字越出页面：{ov.get('max_pt', 0):g} pt；页边墨迹占比："
            + (", ".join(f"{k}={v.get('edge_ink_ratio')}" for k, v in edge.items()) or "无"),
        ]
        if c.get("agent_hint"):
            lines.append(f"  规则侧怀疑：{c['agent_hint']}")
    lines += [
        "",
        "判定要求：",
        "1. 只能描述截图中实际可见的内容；不得根据坐标推断被页面裁掉、或被其他形状盖住的内容。",
        "2. 每条 observation 必须给出可核对的位置（例如“页面底边 y≈1030-1080px，第二行文字下缘被截断”）。",
        "3. 若截图分辨率或遮挡导致无法判断，verdict 用 uncertain，不要猜。",
        "4. 不要评价内容对错，只评价可见性：是否被裁切、被覆盖、对比度是否足以阅读。",
        "",
        "只输出 JSON，不要额外文字：",
        '{"page_assessment":"一句话",'
        '"objects":[{"shape_id":13,"verdict":"visible_clip|visible_occlusion|low_readability|no_visible_problem|uncertain",'
        '"confidence":"high|medium|low","observations":["..."],'
        '"evidence_refs":["页面底边 y=1030-1080px"],"uncertainty":null}],'
        '"suggestion":"针对该页的修改建议"}',
    ]
    return "\n".join(lines)


def _box(box: dict[str, Any] | None) -> str:
    if not box:
        return "无"
    return (f"L={box.get('left_pt')} T={box.get('top_pt')} "
            f"R={box.get('right_pt')} B={box.get('bottom_pt')} pt")


def validate_response(data: Any, expected_ids: set[int]) -> tuple[dict[str, Any] | None, str | None]:
    """校验模型 JSON：必须覆盖全部对象、observations 非空、verdict 合法。"""
    if not isinstance(data, dict):
        return None, "返回不是 JSON 对象"
    objects = data.get("objects")
    if not isinstance(objects, list) or not objects:
        return None, "缺少 objects 数组"
    seen: set[int] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            return None, "objects 元素不是对象"
        try:
            sid = int(obj.get("shape_id"))
        except (TypeError, ValueError):
            return None, "objects 缺少合法 shape_id"
        if sid not in expected_ids:
            return None, f"返回了未请求的 shape_id={sid}"
        seen.add(sid)
        if obj.get("verdict") not in VERDICTS:
            return None, f"shape_id={sid} 的 verdict 非法：{obj.get('verdict')!r}"
        obs = obj.get("observations")
        if not isinstance(obs, list) or not [x for x in obs if str(x).strip()]:
            return None, f"shape_id={sid} 没有可观察证据（observations 为空）"
        if obj.get("confidence") not in ("high", "medium", "low"):
            return None, f"shape_id={sid} 的 confidence 非法"
    missing = expected_ids - seen
    if missing:
        return None, f"缺少对象：{sorted(missing)}"
    return data, None


@dataclass
class ReviewRecord:
    call_id: str
    page_key: str
    case_ids: list[str]
    mode: str
    model: str | None
    image: str | None
    prompt_sha256: str
    requested_at: str = field(default_factory=now_iso)
    latency_ms: int | None = None
    http_status: int | None = None
    usage: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: str | None = None
    simulated: bool = False
    note: str | None = None


class BaseReviewer:
    mode = "base"
    model: str | None = None
    simulated = False

    def __init__(self, max_calls: int = 4) -> None:
        self.max_calls = max_calls
        self.calls: list[ReviewRecord] = []
        self._seen_pages: set[str] = set()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def can_call(self) -> bool:
        return len(self.calls) < self.max_calls

    def review_page(self, page_key: str, cases: list[dict[str, Any]],
                    image_path: Path, prompt: str) -> ReviewRecord | None:
        if page_key in self._seen_pages:
            return None
        if not self.can_call():
            rec = ReviewRecord(
                call_id=f"C{len(self.calls) + 1:02d}", page_key=page_key,
                case_ids=[c["case_id"] for c in cases], mode=self.mode,
                model=self.model, image=str(image_path), prompt_sha256=_sha(prompt),
                error="超过最大复核次数，未调用",
            )
            self.calls.append(rec)
            return rec
        self._seen_pages.add(page_key)
        rec = self._do_review(page_key, cases, image_path, prompt)
        self.calls.append(rec)
        return rec

    def _do_review(self, page_key: str, cases: list[dict[str, Any]],
                   image_path: Path, prompt: str) -> ReviewRecord:
        raise NotImplementedError


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class MockReviewer(BaseReviewer):
    """只验证调度，不读图。所有结论标记为 simulated，不得当作复核结果。"""

    mode = "mock"
    model = None
    simulated = True

    def _do_review(self, page_key: str, cases: list[dict[str, Any]],
                   image_path: Path, prompt: str) -> ReviewRecord:
        objects = []
        for c in cases:
            objects.append({
                "shape_id": c["shape_id"],
                "verdict": "uncertain",
                "confidence": "low",
                "observations": ["模拟测试：本模式不读取图像，未产生任何视觉观察"],
                "evidence_refs": [],
                "uncertainty": "未配置 API Key / 未执行真实视觉复核",
            })
        return ReviewRecord(
            call_id=f"C{len(self.calls) + 1:02d}", page_key=page_key,
            case_ids=[c["case_id"] for c in cases], mode=self.mode, model=None,
            image=str(image_path), prompt_sha256=_sha(prompt), simulated=True,
            response={"page_assessment": "模拟测试，不构成复核结论", "objects": objects,
                      "suggestion": None},
            usage=None, cost=None,
            note="模拟测试：未调用任何模型，不能宣称完成真实 AI 复核",
        )


class HostEvidenceReviewer(BaseReviewer):
    """读取宿主多模态工具产出的复核 JSON。

    该模式不发 HTTP 请求，api_calls 恒为 0；证据来源标记为 host_vision_tool。
    """

    mode = "host"
    simulated = False

    def __init__(self, evidence_path: Path, model: str | None = None,
                 max_calls: int = 4) -> None:
        super().__init__(max_calls=max_calls)
        self.evidence_path = Path(evidence_path)
        self._meta = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        self.model = model or self._meta.get("model") or "host-vision-tool"
        self._items = {str(x["case_id"]): x for x in self._meta.get("items", [])}
        self._tool = self._meta.get("tool")

    def _do_review(self, page_key: str, cases: list[dict[str, Any]],
                   image_path: Path, prompt: str) -> ReviewRecord:
        objects, missing = [], []
        for c in cases:
            item = self._items.get(c["case_id"])
            if not item:
                missing.append(c["case_id"])
                continue
            objects.append({
                "shape_id": c["shape_id"],
                "verdict": item["verdict"],
                "confidence": item.get("confidence", "low"),
                "observations": item.get("observations") or [],
                "evidence_refs": item.get("evidence_refs") or [],
                "uncertainty": item.get("uncertainty"),
            })
        if missing:
            return ReviewRecord(
                call_id=f"C{len(self.calls) + 1:02d}", page_key=page_key,
                case_ids=[c["case_id"] for c in cases], mode=self.mode,
                model=self.model, image=str(image_path), prompt_sha256=_sha(prompt),
                error=f"宿主复核证据缺少：{missing}",
            )
        data, err = validate_response({"objects": objects}, {c["shape_id"] for c in cases})
        if data is not None:
            data["page_assessment"] = None
            data["suggestion"] = None
            for c in cases:
                item = self._items.get(c["case_id"], {})
                if item.get("page_assessment"):
                    data["page_assessment"] = item["page_assessment"]
                if item.get("suggestion"):
                    data["suggestion"] = item["suggestion"]
                if data["page_assessment"] and data["suggestion"]:
                    break
        return ReviewRecord(
            call_id=f"C{len(self.calls) + 1:02d}", page_key=page_key,
            case_ids=[c["case_id"] for c in cases], mode=self.mode, model=self.model,
            image=str(image_path), prompt_sha256=_sha(prompt),
            response=data, error=err,
            note=(f"宿主多模态工具看图（{self._tool or 'host tool'}）；"
                  "本程序未发起 HTTP 调用，Token/费用不可得"),
        )


class OpenAICompatReviewer(BaseReviewer):
    """OpenAI 兼容 /chat/completions 多模态调用。Key 仅从环境变量读取。"""

    mode = "api"

    def __init__(self, api_key: str, base_url: str, model: str,
                 max_calls: int = 4, timeout: float = 90.0,
                 price_in: float | None = None, price_out: float | None = None) -> None:
        super().__init__(max_calls=max_calls)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.price_in = price_in
        self.price_out = price_out

    @classmethod
    def from_env(cls, max_calls: int = 4) -> "OpenAICompatReviewer | None":
        key = os.environ.get("PPT_REVIEW_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        base = os.environ.get("PPT_REVIEW_BASE_URL", "https://api.openai.com/v1")
        model = os.environ.get("PPT_REVIEW_MODEL", "gpt-4o-mini")
        pin = _env_float("PPT_REVIEW_PRICE_IN_PER_MTOK")
        pout = _env_float("PPT_REVIEW_PRICE_OUT_PER_MTOK")
        timeout = _env_float("PPT_REVIEW_TIMEOUT") or 90.0
        return cls(key, base, model, max_calls=max_calls, timeout=timeout,
                   price_in=pin, price_out=pout)

    def _do_review(self, page_key: str, cases: list[dict[str, Any]],
                   image_path: Path, prompt: str) -> ReviewRecord:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 900,
            "messages": [
                {"role": "system",
                 "content": "你是严格的 PPT 版面审查员，只报告截图中可观察到的证据；"
                            "无法判断时必须输出 uncertain。"},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._data_url(image_path)}},
                ]},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"},
        )
        started = time.time()
        status, raw, err = None, None, None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}"
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        latency = int((time.time() - started) * 1000)

        usage, cost, parsed = None, None, None
        if raw:
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                err = err or "响应不是合法 JSON"
                envelope = None
            if envelope:
                usage = envelope.get("usage")
                content = (envelope.get("choices") or [{}])[0].get("message", {}).get("content")
                if isinstance(content, list):
                    content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                if content:
                    try:
                        candidate = json.loads(_strip_json(content))
                    except json.JSONDecodeError:
                        err = err or "模型输出不是 JSON"
                    else:
                        parsed, verr = validate_response(candidate, {c["shape_id"] for c in cases})
                        if verr:
                            err = err or verr
                            parsed = None
                else:
                    err = err or "响应没有 message.content"
                cost = self._cost(usage)

        return ReviewRecord(
            call_id=f"C{len(self.calls) + 1:02d}", page_key=page_key,
            case_ids=[c["case_id"] for c in cases], mode=self.mode, model=self.model,
            image=str(image_path), prompt_sha256=_sha(prompt), latency_ms=latency,
            http_status=status, usage=usage, cost=cost, response=parsed, error=err,
            note="真实 API 调用；Key 来自环境变量，未写入日志",
        )

    @staticmethod
    def _data_url(path: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    def _cost(self, usage: dict[str, Any] | None) -> dict[str, Any] | None:
        if not usage:
            return None
        pin, pout = self.price_in, self.price_out
        if pin is None or pout is None:
            return {"amount": None, "currency": "USD",
                    "note": "未配置单价（PPT_REVIEW_PRICE_IN_PER_MTOK / OUT），只记录 Token，不估算费用"}
        pt = usage.get("prompt_tokens") or 0
        ct = usage.get("completion_tokens") or 0
        amount = pt / 1_000_000 * pin + ct / 1_000_000 * pout
        return {"amount": round(amount, 6), "currency": "USD",
                "note": "按环境变量单价估算，非服务商账单"}


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def summarize_calls(calls: list[ReviewRecord], mode: str) -> dict[str, Any]:
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    have_usage = False
    for c in calls:
        if c.usage:
            have_usage = True
            for k in usage_total:
                if isinstance(c.usage.get(k), int):
                    usage_total[k] += c.usage[k]
    amounts = [c.cost["amount"] for c in calls
               if c.cost and isinstance(c.cost.get("amount"), (int, float))]
    modes = {}
    for c in calls:
        modes[c.mode] = modes.get(c.mode, 0) + 1
    return {
        "mode": mode,
        "simulated": mode == "mock",
        "calls_by_mode": modes,
        "api_http_calls": sum(1 for c in calls if c.mode == "api" and c.http_status is not None),
        "provider_api_calls": sum(1 for c in calls if c.mode == "api" and c.response and not c.error),
        "host_vision_calls": sum(1 for c in calls if c.mode == "host" and c.response and not c.error),
        "vision_reviews": sum(1 for c in calls if c.response and not c.error),
        "attempted": len(calls),
        "token_usage_available": have_usage,
        "usage": usage_total if have_usage else None,
        "cost": ({"amount": round(sum(amounts), 6), "currency": "USD",
                  "note": "按调用记录汇总，非服务商账单"} if amounts else None),
        "calls": [
            {
                "call_id": c.call_id, "page": c.page_key, "case_ids": c.case_ids,
                "mode": c.mode, "model": c.model, "image": c.image,
                "requested_at": c.requested_at, "latency_ms": c.latency_ms,
                "http_status": c.http_status, "usage": c.usage, "cost": c.cost,
                "error": c.error, "simulated": c.simulated, "note": c.note,
                "prompt_sha256": c.prompt_sha256,
                "response": c.response,
            }
            for c in calls
        ],
    }

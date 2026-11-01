"""Layer 3 — on-demand multimodal review through an OpenAI-compatible API.

Design rules
------------
* Only explicitly selected slides are sent, and only after the user enables
  cloud review in the task options.
* The API key never leaves the backend, never appears in logs or reports.
* Failures are recorded per page; already completed work is kept.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw

from ..config import DEFECT_TYPES, DEFECT_LABELS_ZH


class ApiConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


def validate_base_url(url: str, allow_private: bool = False,
                      allow_insecure_http: bool = False) -> str:
    """校验 Base URL。

    两个开关都由用户在设置页显式打开，默认关闭：

    * ``allow_private``        —— 允许解析到内网/回环地址（本地 Ollama、vLLM 等）。
    * ``allow_insecure_http``  —— 允许**非本机**地址使用明文 http。

    无论开关如何，链路本地地址（含 169.254.169.254 云元数据）始终拒绝；
    URL 内嵌凭据始终拒绝。这两条不是可配置项。
    """
    if not url.strip():
        raise ApiConfigError("empty_base_url", "请填写 API Base URL。")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ApiConfigError("invalid_scheme", "Base URL 必须以 http:// 或 https:// 开头。")
    if not parsed.hostname:
        raise ApiConfigError("invalid_host", "Base URL 缺少主机名。")
    host = parsed.hostname
    if parsed.scheme == "http" and not _is_loopback(host) and not allow_insecure_http:
        raise ApiConfigError(
            "insecure_scheme",
            "非本机地址必须使用 HTTPS。若该地址在你的可信网络内，"
            "请先在设置页打开“允许非本机 HTTP”，再保存。",
        )
    if parsed.username or parsed.password:
        raise ApiConfigError("credentials_in_url", "请不要把凭据写在 URL 中。")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ApiConfigError("dns_failed", f"无法解析主机名：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or str(ip) == "169.254.169.254":
            raise ApiConfigError("blocked_address", "该地址属于链路本地/云元数据地址，已拒绝访问。")
        if (ip.is_private or ip.is_loopback) and not _is_loopback(host) and not allow_private:
            raise ApiConfigError(
                "private_address",
                "目标解析到内网地址。若使用本地模型服务，请启用“允许内网 Base URL”。",
            )
    return url.strip().rstrip("/")


def _chat_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _headers(key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _sanitise(message: str, key: str) -> str:
    if key:
        message = message.replace(key, "***")
    return message[:600]


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("响应不是合法 JSON")


def _vision_probe_image() -> str:
    img = Image.new("RGB", (160, 160), (10, 16, 32))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 60, 140, 100], fill=(0, 194, 203))
    draw.text((52, 70), "OK", fill=(5, 10, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _page_image(path: Path, long_edge: int = 1400, quality: int = 82) -> str:
    with Image.open(path) as img:
        img = img.convert("RGB")
        ratio = long_edge / max(img.size)
        if ratio < 1:
            img = img.resize((max(1, round(img.size[0] * ratio)), max(1, round(img.size[1] * ratio))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def chat_payload(base: str, payload: dict) -> dict:
    """Provider-specific JSON review compatibility, without extra network calls.

    Official DeepSeek defaults to high-effort thinking, consuming max_tokens
    before final JSON. Review uses its documented non-thinking mode. Other
    OpenAI-compatible providers and an explicit caller preference are unchanged.
    """
    prepared = dict(payload)
    if (urlparse(base).hostname == 'api.deepseek.com'
            and str(prepared.get('model', '')).startswith('deepseek')
            and 'thinking' not in prepared and 'reasoning_effort' not in prepared):
        prepared['thinking'] = {'type': 'disabled'}
    return prepared


class ModelOutputError(ValueError):
    def __init__(self, code: str, message: str, metadata: dict):
        super().__init__(message)
        self.code, self.metadata = code, metadata


def completion_text(body: dict) -> tuple[str, dict]:
    """Validate final content; retain lengths/finish status, never reasoning text."""
    try:
        choice = body['choices'][0]
        message = choice['message']
        content = message.get('content') or ''
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError('响应缺少 choices[0].message.content') from exc
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        raise ValueError('响应 content 必须是文字或文本数组')
    reasoning = message.get('reasoning_content') or ''
    metadata = {'finish_reason': choice.get('finish_reason'),
        'content_chars': len(content), 'nonspace_content_chars': len(content.strip()),
        'reasoning_present': bool(reasoning),
        'reasoning_chars': len(reasoning) if isinstance(reasoning, str) else None}
    if metadata['finish_reason'] == 'length':
        raise ModelOutputError('output_truncated',
            '模型输出达到单次 token 上限，未生成完整 JSON；增加调用次数无效，需检查思考模式和单次输出预算。', metadata)
    if not content.strip():
        raise ModelOutputError('empty_final_response',
            '接口返回成功但没有最终 JSON 内容；可能仅返回思考内容或 JSON 模式空白，未形成复核结论。', metadata)
    return content, metadata


def http_error_message(status: int, body: dict | str, key: str) -> str:
    hints = {502: '上游网关错误', 503: '上游服务暂不可用', 504: '上游响应超时',
             429: '服务限流', 401: '鉴权失败', 403: '模型或服务无权限'}
    summary = f'接口返回 HTTP {status}' + (f'（{hints[status]}）' if status in hints else '')
    error = body.get('error') if isinstance(body, dict) else None
    detail = error.get('message') if isinstance(error, dict) else error
    if isinstance(detail, str):
        summary += '：' + _sanitise(' '.join(detail.split()), key)[:200]
    return summary


def _post_chat(base: str, key: str, payload: dict, timeout: float) -> tuple[int, dict | str]:
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.post(_chat_url(base), headers=_headers(key), json=chat_payload(base,payload))
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    return resp.status_code, body


def test_connection(base_url: str, key: str, model: str, timeout: float,
                    mode: str = "text", allow_private: bool = False,
                    allow_insecure_http: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        # 用与保存时同一组开关校验，否则"已保存成功却测不通"
        base = validate_base_url(base_url, allow_private, allow_insecure_http)
    except ApiConfigError as exc:
        return {"status": "invalid_config", "code": exc.code, "message": exc.message,
                "latency_ms": None, "http_status": None}
    if not model:
        return {"status": "invalid_config", "code": "missing_model", "message": "请填写模型名称。",
                "latency_ms": None, "http_status": None}

    content: Any = "Reply with the single word: pong"
    if mode == "vision":
        content = [
            {"type": "text", "text": "What two letters are shown in the image? Reply with only the letters."},
            {"type": "image_url", "image_url": {"url": _vision_probe_image()}},
        ]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 24,
        "temperature": 0,
    }
    try:
        status, body = _post_chat(base, key, payload, timeout)
    except httpx.TimeoutException:
        return {"status": "timeout", "code": "timeout", "message": f"请求超时（{timeout:g} 秒）。",
                "latency_ms": None, "http_status": None}
    except httpx.HTTPError as exc:
        return {"status": "network_error", "code": "network_error",
                "message": _sanitise(str(exc), key), "latency_ms": None, "http_status": None}
    latency = round((time.perf_counter() - started) * 1000, 1)

    if status in (401, 403):
        return {"status": "auth_failed", "code": "auth_failed", "message": "鉴权失败：API Key 无效或无权限。",
                "latency_ms": latency, "http_status": status}
    if status == 404:
        return {"status": "incompatible", "code": "not_found",
                "message": "接口不存在（404）：请检查 Base URL 是否为 …/v1 形式，以及模型名称。",
                "latency_ms": latency, "http_status": status}
    if status == 429:
        return {"status": "rate_limited", "code": "rate_limited", "message": "请求被限流（429），请稍后再试。",
                "latency_ms": latency, "http_status": status}
    if status >= 400:
        detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        if mode == "vision" and re.search(r"image|vision|multimodal|content.*type", detail, re.I):
            return {"status": "vision_unsupported", "code": "vision_unsupported",
                    "message": "接口可用，但当前模型/端点不接受图片输入。",
                    "latency_ms": latency, "http_status": status}
        return {"status": "api_error", "code": "api_error",
                "message": f"接口返回 {status}：" + _sanitise(detail, key)[:300],
                "latency_ms": latency, "http_status": status}

    text = ""
    try:
        if isinstance(body, dict):
            text = body["choices"][0]["message"].get("content") or ""
            if isinstance(text, list):
                text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
    except (KeyError, IndexError, TypeError):
        pass
    if not text:
        return {"status": "incompatible", "code": "unexpected_response",
                "message": "接口返回 200，但响应结构不符合 OpenAI Chat Completions 约定。",
                "latency_ms": latency, "http_status": status}
    return {"status": "ok", "code": "ok",
            "message": ("多模态图像请求成功。" if mode == "vision" else "文本请求成功。")
                       + f" 模型响应：{text.strip()[:60]}",
            "latency_ms": latency, "http_status": status, "sample": text.strip()[:120]}


SYSTEM_PROMPT = (
    "你是演示文稿视觉设计质量复核员。你只根据提供的幻灯片截图和结构证据判断，"
    "不臆测看不到的内容；证据不足时给出 uncertain。\n"
    "本次可判定的缺陷类型：\n"
    "1) improper_font_sizing 字号不合理：关键正文/说明文字在整页正常观看时明显过小、难以辨认；"
    "页脚、页码、装饰性微字不算。\n"
    "2) occluded_content 内容遮挡：本应可见的有意义文字、图表或图片被其他元素实质覆盖，信息读不到；"
    "装饰叠压、半透明色块、图片内部自然遮挡不算。\n"
    "3) cluttered_layout 布局拥挤：元素密度或间距导致明显阅读困难、难以区分分组；"
    "信息多但分区清楚不算。\n"
    "4) content_overflow_cutoff 内容溢出或裁切：文字或对象超出画布边缘，或被容器截断导致关键内容缺失；"
    "刻意出血的大字、图片内部构图裁切不算。\n"
    "只输出严格 JSON，不要 markdown，不要解释。"
)

RESPONSE_SCHEMA = {
    "verdicts": [
        {
            "defect_type": "improper_font_sizing | occluded_content | cluttered_layout | content_overflow_cutoff",
            "judgement": "supported | rejected | uncertain",
            "evidence": "具体位置与可见现象，40 字以内",
            "recommendation": "一句可执行的修改建议",
            "confidence": "low | medium | high",
        }
    ],
    "overall_note": "本页整体说明，可为空字符串",
}


def _build_messages(page: dict, image_uri: str) -> list[dict]:
    candidates = page.get("candidates") or []
    if candidates:
        lines = [
            f"- {c['defect_type']}（{DEFECT_LABELS_ZH.get(c['defect_type'], c['defect_type'])}）：{c.get('evidence','')}"
            for c in candidates
        ]
        candidate_text = "结构规则在本页给出的候选：\n" + "\n".join(lines)
    else:
        candidate_text = "结构规则在本页没有给出候选，请独立检查上述各类缺陷。"
    instruction = (
        f"这是第 {page['slide_index']} 页。请逐项判断下面列出的候选是否成立；"
        "若你在截图中发现候选之外的其他缺陷，也请补充。\n"
        f"{candidate_text}\n\n"
        "输出 JSON 结构：\n" + json.dumps(RESPONSE_SCHEMA, ensure_ascii=False) +
        "\njudgement 只能是 supported/rejected/uncertain；只判断确有可见证据的类别，"
        "不要为了凑数而给出 supported。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": image_uri}},
        ]},
    ]


def _parse_review(body: dict) -> dict:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("响应缺少 choices[0].message.content") from exc
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    data = _extract_json(str(content))
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("响应缺少 verdicts 数组")
    clean = []
    for item in verdicts[:8]:
        if not isinstance(item, dict):
            continue
        defect = item.get("defect_type")
        if defect not in DEFECT_TYPES:
            continue
        judgement = item.get("judgement")
        if judgement not in ("supported", "rejected", "uncertain"):
            continue
        confidence = item.get("confidence")
        clean.append({
            "defect_type": defect,
            "judgement": judgement,
            "evidence": str(item.get("evidence") or "")[:300],
            "recommendation": str(item.get("recommendation") or "")[:300],
            "confidence": confidence if confidence in ("low", "medium", "high") else "low",
        })
    if not clean:
        raise ValueError("响应中没有可用的 verdicts")
    return {"verdicts": clean, "overall_note": str(data.get("overall_note") or "")[:300]}


def review_page(page: dict, base_url: str, key: str, model: str, timeout: float,
                max_retries: int, temperature: float,
                response_format_json: bool = True,
                allow_private: bool = False,
                allow_insecure_http: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    result = {
        "slide_index": page["slide_index"],
        "status": "failed",
        "model": model,
        "verdicts": [],
        "overall_note": "",
        "error": None,
        "error_code": None,
        "latency_ms": None,
        "attempts": 0,
        "demo": False,
    }
    try:
        base = validate_base_url(base_url, allow_private, allow_insecure_http)
    except ApiConfigError as exc:
        result.update(status="invalid_config", error=exc.message, error_code=exc.code)
        return result
    if not key:
        result.update(status="invalid_config", error="未配置 API Key。", error_code="missing_key")
        return result
    if not model:
        result.update(status="invalid_config", error="未配置模型名称。", error_code="missing_model")
        return result

    try:
        image_uri = _page_image(Path(page["png_path"]))
    except Exception as exc:
        result.update(status="failed", error=f"页面图片读取失败：{exc}", error_code="image_read_failed")
        return result

    payload = {
        "model": model,
        "messages": _build_messages(page, image_uri),
        "max_tokens": 900,
        "temperature": temperature,
    }
    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    last_error = None
    for attempt in range(max_retries + 1):
        result["attempts"] = attempt + 1
        try:
            status, body = _post_chat(base, key, payload, timeout)
            if status == 400 and response_format_json and isinstance(body, (dict, str)):
                detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                if "response_format" in detail:
                    payload.pop("response_format", None)
                    status, body = _post_chat(base, key, payload, timeout)
            if status in (401, 403):
                result.update(status="auth_failed", error="鉴权失败：API Key 无效或无权限。",
                              error_code="auth_failed")
                return result
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                time.sleep(min(2 ** attempt, 6))
                continue
            if status >= 400:
                detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                if "image" in detail.lower() or "vision" in detail.lower():
                    result.update(status="vision_unsupported",
                                  error="当前模型/端点不接受图片输入。", error_code="vision_unsupported")
                else:
                    result.update(status="api_error",
                                  error=f"接口返回 {status}：{_sanitise(detail, key)[:240]}",
                                  error_code="api_error")
                return result
            parsed = _parse_review(body if isinstance(body, dict) else {})
            result.update(status="ok", verdicts=parsed["verdicts"],
                          overall_note=parsed["overall_note"], error=None, error_code=None)
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result
        except httpx.TimeoutException:
            last_error = f"请求超时（{timeout:g}s）"
            time.sleep(min(2 ** attempt, 4))
        except httpx.HTTPError as exc:
            last_error = _sanitise(str(exc), key)
            time.sleep(min(2 ** attempt, 4))
        except (ValueError, json.JSONDecodeError) as exc:
            result.update(status="invalid_response", error=f"响应格式不符合约定：{exc}",
                          error_code="invalid_response")
            return result

    result.update(status="timeout" if last_error and "超时" in str(last_error) else "network_error",
                  error=last_error or "调用失败", error_code="transport_error")
    return result


def review_pages(pages: list[dict], base_url: str, key: str, model: str, timeout: float,
                 max_retries: int, temperature: float, concurrency: int,
                 response_format_json: bool = True,
                 allow_private: bool = False,
                 allow_insecure_http: bool = False,
                 progress: Callable[[int, int, dict], None] | None = None) -> list[dict]:
    total = len(pages)
    results: list[dict] = []
    if not pages:
        return results
    if concurrency <= 1 or total == 1:
        for i, page in enumerate(pages, 1):
            res = review_page(page, base_url, key, model, timeout, max_retries,
                              temperature, response_format_json,
                              allow_private, allow_insecure_http)
            results.append(res)
            if progress:
                progress(i, total, res)
        return results

    with ThreadPoolExecutor(max_workers=min(concurrency, total)) as pool:
        futures = {
            pool.submit(review_page, page, base_url, key, model, timeout,
                        max_retries, temperature, response_format_json,
                        allow_private, allow_insecure_http): page
            for page in pages
        }
        for i, (future, page) in enumerate(futures.items(), 1):
            try:
                res = future.result()
            except Exception as exc:
                res = {"slide_index": page.get("slide_index"), "status": "failed",
                       "model": model, "verdicts": [], "error": str(exc),
                       "error_code": "worker_error", "latency_ms": None, "attempts": 0}
            results.append(res)
            if progress:
                progress(i, total, res)
    results.sort(key=lambda r: r.get("slide_index", 0))
    return results

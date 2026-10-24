"""API 复核客户端的本地桩测试（不是真实模型调用）。

目的：在没有 API Key 的环境里，验证 OpenAI 兼容请求的组装、图片编码、JSON 校验、
usage/费用字段与“Key 不落日志”约束。服务端是本地 HTTP 桩，返回固定 JSON；
结果单独写入 `outputs/agent/api_client_test.json`，不混入 agent_findings。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from review_client import OpenAICompatReviewer, build_prompt, summarize_calls  # noqa: E402

OUT = ROOT / "outputs" / "agent"
EVIDENCE = ROOT / "outputs" / "agent" / "stage3_evidence.json"
IMAGE = ROOT / "outputs" / "agent" / "render" / "stage3" / "slide03.png"
TEST_KEY = "local-stub-key-not-a-real-credential"

CANNED = {
    "page_assessment": "本地桩：正文被红色块覆盖。",
    "objects": [{
        "shape_id": 3,
        "verdict": "visible_occlusion",
        "confidence": "high",
        "observations": ["本地桩固定观察：红色块覆盖正文中部，x≈655-1160px。"],
        "evidence_refs": ["红色块 x≈655-1160px, y≈382-662px"],
        "uncertainty": None,
    }],
    "suggestion": "本地桩建议：调整叠放次序。",
}


class Handler(BaseHTTPRequestHandler):
    saw_image = False

    def log_message(self, *args):  # 静默
        pass

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        auth = self.headers.get("Authorization", "")
        ok_shape = any("image_url" in str(part) for msg in body["messages"]
                       for part in (msg["content"] if isinstance(msg["content"], list) else []))
        Handler.saw_image = ok_shape
        if auth != f"Bearer {TEST_KEY}" or not ok_shape:
            self.send_error(400)
            return
        payload = json.dumps({
            "id": "stub-1",
            "choices": [{"message": {"role": "assistant",
                                     "content": json.dumps(CANNED, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    os.environ["PPT_REVIEW_PRICE_IN_PER_MTOK"] = "0.4"
    os.environ["PPT_REVIEW_PRICE_OUT_PER_MTOK"] = "1.6"
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    case = next(c for c in ev["cases"] if c["case_id"] == "V3")
    case["agent_hint"] = "本地桩测试：遮挡规则触发"
    prompt = build_prompt([case], 960, 540, 1920, 1080)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    reviewer = OpenAICompatReviewer(
        api_key=TEST_KEY, base_url=f"http://127.0.0.1:{port}/v1",
        model="stub-vision", max_calls=1, price_in=0.4, price_out=1.6,
    )
    rec = reviewer.review_page("stage3_visual_cases.pptx#p3", [case], IMAGE, prompt)
    server.shutdown()

    summary = summarize_calls(reviewer.calls, reviewer.mode)
    blob = json.dumps(summary, ensure_ascii=False)
    checks = {
        "http_200": rec.http_status == 200,
        "verdict_parsed": rec.response["objects"][0]["verdict"] == "visible_occlusion",
        "usage_recorded": rec.usage["total_tokens"] == 1280,
        "cost_computed": bool(rec.cost and abs(rec.cost["amount"] - 0.000608) < 1e-9),
        "single_http_call": summary["api_http_calls"] == 1,
        "api_review_count": summary["provider_api_calls"] == 1,
        "key_not_in_record": TEST_KEY not in blob,
        "payload_has_image": Handler.saw_image,
    }
    passed = all(checks.values())
    out = {
        "test": "openai_compatible_client_local_stub",
        "real_model": False,
        "note": "本地 HTTP 桩返回固定 JSON；只验证客户端管线，不是真实多模态复核",
        "model": "stub-vision",
        "http_status": rec.http_status,
        "latency_ms": rec.latency_ms,
        "usage": rec.usage,
        "cost": rec.cost,
        "response": rec.response,
        "checks": checks,
        "passed": passed,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "api_client_test.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

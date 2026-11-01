"""End-to-end backend tests against a temporary data directory."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.visual_classifier import VisualClassifierService, VisualModelNotConfigured
from tests.conftest import upload, wait, wait_prepared


@pytest.fixture(autouse=True)
def unavailable_visual_plugin_for_legacy_scenarios(monkeypatch,tmp_path):
    """These baseline scenarios explicitly exercise the unconfigured plugin.

    Optional real model integration is tested separately; its installation on
    the workstation must not change what this legacy fixture represents.
    """
    from app.services import orchestrator
    monkeypatch.setattr(orchestrator,'_classifier',VisualClassifierService(tmp_path/'absent-model.json'))


def _issue_types(task: dict) -> set[str]:
    data = task.get("summary") or {}
    return {k for k, v in (data.get("by_type") or {}).items() if v}


def test_capabilities_report_visual_unavailable(client):
    caps = client.get("/api/system/capabilities").json()
    assert caps["modules"]["visual"]["state"] in ("not_configured", "error")
    assert caps["modules"]["visual"]["available"] is False
    assert caps["modules"]["structure"]["state"] == "ready"
    assert len(caps["labels"]) == 6
    assert {item["key"] for item in caps["labels"]} >= {
        "improper_font_sizing", "occluded_content", "cluttered_layout", "content_overflow_cutoff"}


def test_visual_service_never_fakes_predictions(tmp_path):
    service = VisualClassifierService(tmp_path/'absent-model.json')
    assert service.status()["state"] in ("not_configured", "error")
    with pytest.raises(VisualModelNotConfigured):
        service.predict_slide("does-not-matter.png", "doc", 1)


def test_rejects_invalid_files(client):
    response = client.post("/api/tasks", files={"file": ("bad.txt", b"hello")}, data={"options": "{}"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_format"

    response = client.post("/api/tasks", files={"file": ("fake.pptx", b"PK\x03\x04nope")}, data={"options": "{}"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("not_zip", "not_pptx")


def test_full_pipeline_without_visual_model(client, fixture_pptx):
    task = upload(client, fixture_pptx, name="真实闭环测试")
    final = wait(client, task["id"])
    assert final["status"] == "completed", final.get("error")
    stages = {s["key"]: s["status"] for s in final["stages"]}
    assert stages["parse"] == "done"
    assert stages["render"] == "done"
    assert stages["structure"] == "done"
    assert stages["visual"] == "skipped"
    assert stages["mllm"] == "skipped"
    assert stages["report"] == "done"

    doc = final["document"]
    assert doc["slide_count"] == 10
    assert doc["aspect_label"] == "16:9"
    assert doc["image_only_pages"] == 1
    assert final["summary"]["visible_issue_count"] >= 3
    assert "content_overflow_cutoff" in _issue_types(final)

    slides = client.get(f"/api/tasks/{task['id']}/slides").json()
    assert len(slides["slides"]) == 10
    assert slides["slides"][0]["render_status"] == "rendered"
    image = client.get(f"/api/tasks/{task['id']}/slides/1/image?kind=render")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")

    report = client.get(f"/api/tasks/{task['id']}/report?format=json")
    assert report.status_code == 200
    payload = report.json()
    assert payload["is_demo"] is False
    assert payload["task"]["status"] == "completed"
    assert any("CLIP未启用" in item for item in payload["limitations"])
    assert payload["modules"][1]["state_label"] == "已跳过"

    html = client.get(f"/api/tasks/{task['id']}/report?format=html")
    assert html.status_code == 200
    assert "检测能力与局限" in html.text
    assert "data:image/jpeg;base64" in html.text  # thumbnails embedded


def test_task_resume_after_prepare(client, fixture_pptx):
    task = upload(client, fixture_pptx, prepare_only=True, name="两阶段")
    prepared = wait_prepared(client, task["id"])
    assert prepared["status"] == "ready"
    assert prepared["document"]["slide_count"] == 10
    response = client.post(f"/api/tasks/{task['id']}/start", json={
        "options": {"enable_structure": True, "enable_visual": True, "enable_mllm": False,
                    "review_mode": "off", "max_review_pages": 0, "max_api_calls": 0},
        "name": "两阶段（已改名）",
    })
    assert response.status_code == 200
    final = wait(client, task["id"])
    assert final["status"] == "completed"
    assert final["name"] == "两阶段（已改名）"
    assert {s["key"]: s["status"] for s in final["stages"]}["render"] == "done"


def test_settings_validation_and_secret_masking(client):
    bad = client.put("/api/settings", json={"api_base_url": "http://10.0.0.5/v1", "api_model": "x"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] in ("private_address", "insecure_scheme")

    ok = client.put("/api/settings", json={
        "api_base_url": "http://127.0.0.1:8099/v1", "api_model": "mock-vl", "api_key": "sk-test-secret-1234",
    })
    assert ok.status_code == 200
    body = ok.json()
    assert body["api_key_set"] is True
    assert "sk-test-secret-1234" not in json.dumps(body)
    assert body["api_key_masked"].endswith("1234")
    client.delete("/api/settings/api-key")


def test_mllm_review_against_mock_endpoint(client, fixture_pptx):
    seen = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            seen["count"] += 1
            verdicts = [
                {"defect_type": "improper_font_sizing", "judgement": "supported",
                 "evidence": "mock: 参数段字号偏小", "recommendation": "提高字号", "confidence": "high"},
                {"defect_type": "content_overflow_cutoff", "judgement": "supported",
                 "evidence": "mock: 文本越出画布", "recommendation": "左移文本框", "confidence": "high"},
            ]
            content = json.dumps({"verdicts": verdicts, "overall_note": "mock"})
            payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    try:
        saved = client.put("/api/settings", json={
            "api_base_url": f"http://127.0.0.1:{port}/v1",
            "api_model": "mock-vl",
            "api_key": "sk-mock-key",
            "api_timeout_s": 20,
            "api_max_retries": 0,
            "max_calls_per_task": 4,
        })
        assert saved.status_code == 200, saved.text

        test = client.post("/api/settings/test-connection", json={"mode": "vision"})
        assert test.status_code == 200
        assert test.json()["result"]["status"] == "ok"

        task = upload(client, fixture_pptx, name="Mock API 复核", options={
            "enable_structure": True, "enable_visual": True, "enable_mllm": True,
            "review_mode": "all", "max_review_pages": 2, "max_api_calls": 2,
        })
        final = wait(client, task["id"])
        assert final["status"] == "completed", final.get("error")
        stages = {s["key"]: s["status"] for s in final["stages"]}
        assert stages["mllm"] == "done"
        assert final["summary"]["ai_reviewed_pages"] >= 1
        assert final["summary"]["ai_supported"] >= 1
        assert seen["count"] >= 1

        issues = client.get(f"/api/tasks/{task['id']}/issues").json()["items"]
        assert any(i["review_status"] == "supported" for i in issues)
        assert any(any(s["source"] == "mllm_review" for s in i["sources"]) for i in issues)

        payload = client.get(f"/api/tasks/{task['id']}/report?format=json").json()
        assert payload["summary"]["ai_reviewed_pages"] >= 1
        assert all("sk-mock-key" not in json.dumps(i) for i in payload["issues"])
    finally:
        server.shutdown()
        server.server_close()
        client.delete("/api/settings/api-key")
        client.put("/api/settings", json={"api_base_url": "", "api_model": ""})


def test_mllm_failure_keeps_other_results(client, fixture_pptx):
    saved = client.put("/api/settings", json={
        "api_base_url": "http://127.0.0.1:9/v1",
        "api_model": "unreachable",
        "api_key": "sk-should-not-leak",
        "api_timeout_s": 5,
        "api_max_retries": 0,
        "max_calls_per_task": 2,
    })
    assert saved.status_code == 200, saved.text
    try:
        task = upload(client, fixture_pptx, name="API 失败保留结果", options={
            "enable_structure": True, "enable_visual": True, "enable_mllm": True,
            "review_mode": "risk_only", "max_review_pages": 2, "max_api_calls": 2,
        })
        final = wait(client, task["id"])
        assert final["status"] == "completed_with_errors", final.get("error")
        stages = {s["key"]: s["status"] for s in final["stages"]}
        assert stages["structure"] == "done"
        assert stages["mllm"] == "failed"
        assert stages["report"] == "done"
        assert final["summary"]["visible_issue_count"] >= 3
        report = client.get(f"/api/tasks/{task['id']}/report?format=json")
        assert report.status_code == 200
        payload = report.json()
        assert payload["reviews"], "failed reviews must still be recorded"
        assert payload["reviews"][0]["status"] != "ok"
        assert "sk-should-not-leak" not in report.text
    finally:
        client.delete("/api/settings/api-key")
        client.put("/api/settings", json={"api_base_url": "", "api_model": ""})


def test_demo_cohort_is_isolated(client):
    created = client.post("/api/demo/tasks", json={"scenario": "full", "name": "pytest demo"})
    assert created.status_code == 201
    demo_id = created.json()["id"]
    final = wait(client, demo_id, mode="demo")
    assert final["status"] == "completed"
    assert final["mode"] == "demo"
    data = client.get(f"/api/tasks/{demo_id}/report?format=json&mode=demo").json()
    assert data["is_demo"] is True
    assert any("演示" in item for item in data["limitations"])
    assert data["modules"][1]["state_label"] == "已运行"

    real_ids = {t["id"] for t in client.get("/api/tasks").json()["items"]}
    assert demo_id not in real_ids
    demo_ids = {t["id"] for t in client.get("/api/tasks?mode=demo").json()["items"]}
    assert demo_id in demo_ids


def test_delete_task(client, fixture_pptx):
    task = upload(client, fixture_pptx, name="待删除")
    wait(client, task["id"])
    response = client.delete(f"/api/tasks/{task['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404

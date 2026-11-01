"""Report regressions using synthetic payloads and no fake PDF artifacts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.config import DEFECT_TYPES
from app.services import pdf_export, report
from app.services.merge import summarize_issues


def make_issue(*, mock=False):
    return dict(issue_id="six-fixture-s0001-D5", candidate_id="six-fixture-s0001-D5", document_id="fixture",
        slide_index=1, defect_type="excessive_whitespace", title="留白待核验候选",
        evidence_source="six_detector", sources=[dict(source="six_detector", summary="Synthetic native evidence")],
        evidence_summary="测量只支持候选，不表示已经确认缺陷。", bbox=dict(x=1., y=1., w=2., h=1.),
        coordinate_space="inches", severity="medium", confidence="candidate", recommendation="结合页面用途复核内容尺度。",
        review_status="supported" if mock else "unreviewed", review_result=(dict(judgement="supported", evidence="Mock fixture only",
            mock=True, model="explicit-mock") if mock else None), visual_probability=None)


def payload(tmp_path, *, mock=False, demo=False):
    issues = [make_issue(mock=mock)]
    task = dict(id="fixture-task", name="合成报告回归", status="completed", size_bytes=1234,
                source_sha256="a" * 64, mode="demo" if demo else "real", options={})
    document = dict(file_name="report-fixture.pptx", slide_count=1, aspect_label="4:3", slide_width_in=10.,
        slide_height_in=7.5, editable_pages=1, mixed_pages=0, image_only_pages=0, textboxes=2, pictures=0,
        tables=0, charts=0, fonts_used=[], missing_fonts=[], slides=[dict(index=1, content_mode="editable")])
    stages = [dict(key="structure", label="结构检测", status="done", message="Synthetic detector evidence"),
              dict(key="visual", label="视觉模型", status="unsupported", message="模型未接入"),
              dict(key="mllm", label="按需复核", status="done" if mock else "skipped", message="Mock only" if mock else "未执行")]
    reviews = [dict(status="ok", slide_index=1, model="explicit-mock", mock=True, actual_http_calls=0,
                    verdicts=[dict(candidate_id=issues[0]["candidate_id"], judgement="uncertain", evidence="Mock fixture only")])] if mock else []
    result = report.build_payload(task=task, document=document, stages=stages, issues=issues, reviews=reviews,
        risks=[], summary=summarize_issues(issues, 1, reviews, []), module_status={}, task_dir=tmp_path, is_demo=demo)
    result["mock_review"] = mock
    return result


@pytest.mark.parametrize("requirements", [{}, {"review_focus": None}, {"review_focus": []}])
def test_legacy_demo_without_review_focus_or_with_none_exports_html(tmp_path, requirements):
    data = payload(tmp_path, demo=True)
    data["user_requirements"] = {"purpose": "旧版演示", **requirements}
    paths = report.write_reports(data, tmp_path)
    html = paths["html"].read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html and "旧版演示" in html
    assert paths["json"].is_file()


def test_six_detector_only_issue_is_not_relabelled_visual_model(tmp_path):
    data = payload(tmp_path)
    prepared = data["pages"][0]["issues"][0]
    assert prepared["source_label"] == "六类专用检测"
    assert prepared["source_class"] == "rule"
    assert prepared["visual_probability"] is None
    html = report.write_reports(data, tmp_path)["html"].read_text(encoding="utf-8")
    assert '<span class="badge rule">六类专用检测</span>' in html
    assert '<span class="badge model">视觉模型</span>' not in html


def test_mock_banner_precedes_hero_and_badge_never_claims_real_llm_judgement(tmp_path):
    data = payload(tmp_path, mock=True)
    prepared = data["pages"][0]["issues"][0]
    assert "Mock" in prepared["review_badge"] and "非真实模型判断" in prepared["review_badge"]
    html = report.write_reports(data, tmp_path)["html"].read_text(encoding="utf-8")
    marker = '<div class="demo-banner">MOCK 模拟复核'
    assert html.index(marker) < html.index('<header class="hero">')
    assert "没有调用真实模型" in html
    assert '<span class="badge ai">AI 复核支持</span>' not in html
    assert '<span class="badge ai">Mock 模拟复核 · 非真实模型判断</span>' in html


def test_mock_json_records_remain_explicitly_mock_with_zero_http_calls(tmp_path):
    data = payload(tmp_path, mock=True)
    saved = report.write_reports(data, tmp_path)["json"]
    exported = json.loads(saved.read_text(encoding="utf-8"))
    assert exported["mock_review"] is True
    assert exported["reviews"][0]["mock"] is True
    assert exported["reviews"][0]["actual_http_calls"] == 0
    assert exported["issues"][0]["review_result"]["mock"] is True


def test_raw_unknown_and_a05_unsupported_survive_json_and_html_without_normalization(tmp_path):
    data = payload(tmp_path)
    data["claims"] = [dict(candidate_id="unknown-D1", slide_index=1, defect_type="improper_font_sizing",
        subtype=None, raw_status="unknown", score=None, detector_version="fixture", related_slides=[1],
        limitations=["有效字号和角色尚未确定"]),
        dict(candidate_id="unsupported-A05", slide_index=1, defect_type="generative_design_anomaly", subtype="A05",
            raw_status="unsupported", score=None, detector_version="fixture", related_slides=[1],
            limitations=["A05 语义检测未实现，ROI 仅供复核"])]
    original = deepcopy(data["claims"])
    paths = report.write_reports(data, tmp_path)
    exported = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["claims"] == original == exported["claims"]
    assert all(c["score"] is None for c in exported["claims"])
    html = paths["html"].read_text(encoding="utf-8")
    assert "unknown" in html and "unsupported" in html and "/ A05" in html
    assert "未知、不支持、未执行均不等于正常" in html
    assert "A05 语义检测未实现，ROI 仅供复核" in html


def test_generation_prompt_and_user_intent_are_html_escaped(tmp_path):
    data = payload(tmp_path)
    attack = '<script>window.untrustedPromptExecuted=true</script>'
    data["user_requirements"] = dict(purpose="报告审查", review_focus=None, original_prompt=attack)
    html = report.write_reports(data, tmp_path)["html"].read_text(encoding="utf-8")
    assert attack not in html
    assert "&lt;script&gt;window.untrustedPromptExecuted=true&lt;/script&gt;" in html


def test_pdf_export_without_browser_raises_and_never_creates_fake_pdf(tmp_path, monkeypatch):
    data = payload(tmp_path)
    paths = report.write_reports(data, tmp_path)
    original_html = paths["html"].read_bytes()
    real_is_file = Path.is_file
    def no_browser(self):
        if self.name.lower() in {"msedge.exe", "chrome.exe"}:
            return False
        return real_is_file(self)
    monkeypatch.setattr(Path, "is_file", no_browser)
    def no_process(*args, **kwargs):
        pytest.fail("No process should launch when no browser engine is available")
    monkeypatch.setattr(pdf_export.subprocess, "Popen", no_process)
    with pytest.raises(RuntimeError, match="未找到本机Edge/Chrome"):
        pdf_export.export_pdf(tmp_path)
    assert not (tmp_path / "report/report.pdf").exists()
    assert paths["html"].read_bytes() == original_html


def test_pdf_export_without_html_raises_instead_of_creating_placeholder(tmp_path):
    with pytest.raises(ValueError, match="HTML报告尚未生成"):
        pdf_export.export_pdf(tmp_path)
    assert not (tmp_path / "report/report.pdf").exists()

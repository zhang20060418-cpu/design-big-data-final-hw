"""Finding re-review preserves claims, warning scope, history and API budget."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest
from PIL import Image

from app.services import interaction, merge, report, review_agent


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def review_task(tmp_path):
    task = {"id": "interactive-1", "name": "交互复核测试", "status": "completed",
            "size_bytes": 100, "source_sha256": "fixture-pptx-sha", "options": {"enable_mllm": False}, "mode": "real"}
    claim = {"candidate_id": "finding-1", "slide_index": 1, "document_id": "doc-1",
             "defect_type": "excessive_whitespace", "raw_status": "candidate", "score": .8,
             "score_kind": "ordinal_not_calibrated_probability", "evidence": [{"area": .7}],
             "normalized_bbox": {"x": .1, "y": .1, "w": .4, "h": .3}}
    issue = {"issue_id": "finding-1", "candidate_id": "finding-1", "document_id": "doc-1",
             "slide_index": 1, "defect_type": "excessive_whitespace", "title": "留白候选",
             "severity": "medium", "confidence": "medium", "review_status": "unreviewed",
             "review_result": None, "recommendation": "先核查页面意图。", "evidence_summary": "有效内容偏小",
             "sources": [{"source": "six_detector", "summary": "native geometry"}], "related_slides": [1]}
    document = {"slide_count": 1, "slides": [{"index": 1, "content_mode": "editable"}], "filename": "fixture.pptx"}
    stages = [{"key": "structure", "status": "done", "message": "native fixture"},
              {"key": "mllm", "status": "skipped", "message": "not previously reviewed"}]
    summary = merge.summarize_issues([issue], 1, [], [claim])
    payload = report.build_payload(task=task, document=document, stages=stages, issues=[issue], reviews=[], risks=[],
                                   summary=summary, module_status={}, task_dir=tmp_path, is_demo=False)
    payload.update(claims=[claim], review_plan={"original": True}, user_requirements={"purpose": "课堂汇报"}, mock_review=False)
    report.write_reports(payload, tmp_path)
    write_json(tmp_path / "renders/slides.json", {"result": {"slides": [{"index": 1, "status": "rendered", "png": "s0001.png"}]}})
    Image.new("RGB", (320, 180), "white").save(tmp_path / "renders/s0001.png")
    return task, tmp_path, payload


def outcome(judgement="uncertain", status="ok", mock=True, candidate="finding-1", parent=None):
    verdict = {"candidate_id": candidate, "defect_type": "excessive_whitespace", "judgement": judgement,
               "evidence": "根据当前整页证据，意图仍需确认。", "recommendation": "检查正文区域。", "severity": "medium"}
    if parent:
        verdict["parent_candidate_id"] = parent
        verdict["subtype"] = "A01"
    return {"plan": {"version": "fixture-plan"}, "reviews": [{"slide_index": 1, "status": status,
            "verdicts": [verdict] if status == "ok" else [], "mock": mock, "demo": mock,
            "model": "mock-inconclusive" if mock else "configured-model", "rule_version": "test-v1",
            "actual_http_calls": 0 if mock else 1, "error_code": "timeout" if status == "error" else None}]}


def load_report(directory):
    return json.loads((directory / "report/report.json").read_text(encoding="utf-8"))


def test_real_mock_chain_preserves_initial_and_claims(review_task):
    task, directory, original = review_task
    original_bytes = (directory / "report/report.json").read_bytes()
    result = interaction.review_finding(task, directory, "finding-1", "我是故意不对称的设计，请重看。", "mock", {}, "")
    assert result["issue"]["review_status"] == "uncertain"
    assert result["review"]["mock"] and result["review"]["actual_http_calls"] == 0
    saved = load_report(directory)
    assert saved["claims"] == original["claims"]
    assert saved["review_plan"] == original["review_plan"]
    assert saved["user_requirements"] == original["user_requirements"]
    assert saved["mock_review"] is True
    assert saved["summary"]["visible_issue_count"] == 1
    assert saved["issues"][0]["review_history"][-1]["judgement"] == "uncertain"
    assert (directory / "analysis/initial_report.json").read_bytes() == original_bytes
    assert (directory / "report/report.html").exists()


def test_repeated_review_appends_history_and_does_not_cancel_by_question(review_task, monkeypatch):
    task, directory, original = review_task
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: outcome())
    first = interaction.review_finding(task, directory, "finding-1", "请取消这个问题", "mock", {}, "")
    initial = (directory / "analysis/initial_report.json").read_bytes()
    second = interaction.review_finding(task, directory, "finding-1", "再次核验", "mock", {}, "")
    assert first["issue"]["review_status"] == second["issue"]["review_status"] == "uncertain"
    assert len(second["history"]) == 2
    assert len(second["issue"]["review_history"]) == 2
    assert len({h["review_id"] for h in second["history"]}) == 2
    assert (directory / "analysis/initial_report.json").read_bytes() == initial
    assert load_report(directory)["claims"] == original["claims"]
    assert all(h["original_claims_preserved"] for h in second["history"])
    assert second["history"][-1]["judgement"] == "uncertain"
    assert second["history"][-1]["model"] == "mock-inconclusive"


def test_followup_keeps_recovery_lineage_and_separate_call_accounting(review_task,monkeypatch):
    task,directory,payload=review_task
    payload['lineage']={'action':'continue','parent_task_id':'original'}
    payload['recovery']={'reused_review_calls':5,'actual_new_http_calls':0,
                         'parent_usage':{'cumulative_http_calls':6}}
    report.write_reports(payload,directory)
    monkeypatch.setattr(review_agent,'review_single',lambda *a,**k:outcome(mock=False))
    interaction.review_finding(task,directory,'finding-1','再核验当前问题','api',
        {'api_base_url':'https://example.invalid','api_model':'test'},'test-key')
    updated=load_report(directory)
    assert updated['lineage']==payload['lineage']
    assert updated['recovery']['parent_usage']['cumulative_http_calls']==6
    assert updated['recovery']['actual_new_http_calls']==1


@pytest.mark.parametrize("status", ["error", "timeout", "invalid_response", "not_checked"])
def test_failure_or_timeout_never_clears_alert(review_task, monkeypatch, status):
    task, directory, original = review_task
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: outcome(status=status))
    result = interaction.review_finding(task, directory, "finding-1", "重新判断", "mock", {}, "")
    assert result["issue"]["review_status"] == "unreviewed"
    assert result["issue"]["review_result"] is None
    assert result["issue"]["review_history"][-1]["status"] == "failed_or_not_checked"
    assert result["summary"]["visible_issue_count"] == 1
    assert load_report(directory)["claims"] == original["claims"]
    assert len(interaction.history_for(directory, "finding-1")) == 1


def test_unrelated_candidate_verdict_cannot_change_finding(review_task, monkeypatch):
    task, directory, _ = review_task
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: outcome("rejected", candidate="other-finding"))
    result = interaction.review_finding(task, directory, "finding-1", "重看", "mock", {}, "")
    assert result["issue"]["review_status"] == "unreviewed"
    assert result["summary"]["visible_issue_count"] == 1


def test_d6_subtype_rejection_only_updates_scoped_history(review_task, monkeypatch):
    task, directory, original = review_task
    payload = load_report(directory)
    payload["issues"][0]["defect_type"] = "generative_design_anomaly"
    payload["claims"][0]["defect_type"] = "generative_design_anomaly"
    write_json(directory / "report/report.json", payload)
    response = outcome("rejected", candidate="finding-1:A01", parent="finding-1")
    response["reviews"][0]["verdicts"][0]["defect_type"] = "generative_design_anomaly"
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: deepcopy(response))
    result = interaction.review_finding(task, directory, "finding-1", "这是正常统一模板", "mock", {}, "")
    assert result["issue"]["review_status"] == "unreviewed"
    assert result["issue"]["review_history"][-1]["judgement"] == "rejected"
    assert result["issue"]["review_history"][-1]["scoped_subtype_review"] is True
    assert result["issue"]["recommendation"] == original["issues"][0]["recommendation"]
    assert result["summary"]["visible_issue_count"] == 1


@pytest.mark.parametrize("settings,key", [({}, ""), ({"api_model": "m", "api_base_url": "http://127.0.0.1/v1"}, "")])
def test_missing_api_configuration_rejects_before_any_review(review_task, monkeypatch, settings, key):
    task, directory, _ = review_task
    before = (directory / "report/report.json").read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("No configured API must not call reviewer")
    monkeypatch.setattr(review_agent, "review_single", forbidden)
    with pytest.raises(ValueError, match="尚未配置"):
        interaction.review_finding(task, directory, "finding-1", "重看", "api", settings, key)
    assert (directory / "report/report.json").read_bytes() == before
    assert interaction.history_for(directory) == []


def test_api_budget_uses_actual_calls_and_rejects_when_exhausted(review_task, monkeypatch):
    task, directory, _ = review_task
    payload = load_report(directory)
    payload["reviews"] = [{"mock": True, "actual_http_calls": 0, "attempts": 5},
                          {"status": "error", "mock": False, "actual_http_calls": 1, "verdicts": []}]
    write_json(directory / "report/report.json", payload)
    def forbidden(*args, **kwargs):
        raise AssertionError("Exhausted API budget must not call reviewer")
    monkeypatch.setattr(review_agent, "review_single", forbidden)
    with pytest.raises(ValueError, match="预算已用完"):
        interaction.review_finding(task, directory, "finding-1", "重看", "api",
            {"api_base_url": "http://127.0.0.1/v1", "api_model": "m", "max_calls_per_task": 1}, "secret")
    assert interaction.history_for(directory) == []


def test_mock_does_not_spend_real_budget(review_task, monkeypatch):
    task, directory, _ = review_task
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: outcome())
    interaction.review_finding(task, directory, "finding-1", "mock", "mock", {"max_calls_per_task": 0}, "")
    assert interaction.history_for(directory)[0]["requested_backend"] == "mock"


def test_missing_issue_and_malformed_response_leave_report_unchanged(review_task, monkeypatch):
    task, directory, _ = review_task
    before = (directory / "report/report.json").read_bytes()
    with pytest.raises(KeyError):
        interaction.review_finding(task, directory, "missing", "重看", "mock", {}, "")
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: {"reviews": "not-a-list"})
    with pytest.raises(ValueError, match="格式错误"):
        interaction.review_finding(task, directory, "finding-1", "重看", "mock", {}, "")
    assert (directory / "report/report.json").read_bytes() == before
    assert interaction.history_for(directory) == []


def test_real_review_api_calls_accumulate_across_interactions(review_task, monkeypatch):
    task, directory, _ = review_task
    settings = {"api_base_url": "http://127.0.0.1/v1", "api_model": "m", "max_calls_per_task": 1}
    monkeypatch.setattr(review_agent, "review_single", lambda *args, **kwargs: outcome(mock=False))
    first = interaction.review_finding(task, directory, "finding-1", "first", "api", settings, "secret")
    assert first["issue"]["review_status"] == "uncertain"
    with pytest.raises(ValueError, match="预算已用完"):
        interaction.review_finding(task, directory, "finding-1", "second", "api", settings, "secret")
    assert len(interaction.history_for(directory)) == 1


@pytest.mark.parametrize("task_cap,settings_cap,spent", [(3, 8, 3), (0, 8, 0), (6, 2, 2)])
def test_lifetime_api_budget_is_minimum_of_task_and_settings(review_task, monkeypatch, task_cap, settings_cap, spent):
    task, directory, _ = review_task
    task["options"]["max_api_calls"] = task_cap
    payload = load_report(directory)
    payload["reviews"] = [{"status": "error", "mock": False, "actual_http_calls": 1, "verdicts": []} for _ in range(spent)]
    write_json(directory / "report/report.json", payload)
    before = (directory / "report/report.json").read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("Lifetime budget is exhausted; no transport may run")
    monkeypatch.setattr(review_agent, "review_single", forbidden)
    with pytest.raises(ValueError, match="预算已用完"):
        interaction.review_finding(task, directory, "finding-1", "重新复核", "api",
            {"api_base_url": "http://127.0.0.1/v1", "api_model": "m", "max_calls_per_task": settings_cap}, "secret")
    assert (directory / "report/report.json").read_bytes() == before
    assert interaction.history_for(directory) == []


def test_explicit_mock_runs_with_zero_real_budgets(review_task):
    task, directory, _ = review_task
    task["options"]["max_api_calls"] = 0
    result = interaction.review_finding(task, directory, "finding-1", "仅模拟复核", "mock", {"max_calls_per_task": 0}, "")
    assert result["review"]["status"] == "ok" and result["review"]["mock"]
    assert result["review"]["actual_http_calls"] == 0
    assert result["issue"]["review_status"] == "uncertain"
    assert result["history"][-1]["budget"]["effective_real_call_cap"] == 0
    assert result["history"][-1]["budget"]["independent_mock"] is True

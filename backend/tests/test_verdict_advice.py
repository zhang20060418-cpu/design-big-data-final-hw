"""A dismissed false positive must disappear from counts, advice and prompt.

The raw detection record stays untouched; only the projection layer filters it
(``review_status != rejected`` and ``user_verdict != dismissed``).
"""
from __future__ import annotations

import json

from app.services import advice, merge, verdicts


def issue(issue_id: str, slide: int, defect: str, severity: str = "medium",
          status: str = "unreviewed", **extra) -> dict:
    data = {
        "issue_id": issue_id, "candidate_id": issue_id, "document_id": "doc-1",
        "slide_index": slide, "defect_type": defect, "title": "fixture finding",
        "severity": severity, "confidence": "medium", "review_status": status,
        "review_result": None, "recommendation": "按坐标把对象拖回画布安全区。",
        "evidence_summary": "对象越界 0.12in", "related_slides": [slide],
        "sources": [{"source": "six_detector", "summary": "native geometry"}],
    }
    data.update(extra)
    return data


def fixture_payload(tmp_path, dismissed: bool = True) -> dict:
    issues = [
        issue("f-overflow", 2, "content_overflow_cutoff", "high"),
        issue("f-font", 3, "improper_font_sizing"),
    ]
    verdicts.record(tmp_path, "f-font", "dismissed") if dismissed else None
    annotated = verdicts.annotate(issues, verdicts.load(tmp_path))
    summary = merge.summarize_issues(annotated, 5, [])
    document = {"slide_count": 5, "file_name": "fixture.pptx", "aspect_label": "16:9",
                "slide_width_in": 13.333, "slide_height_in": 7.5,
                "editable_pages": 5, "mixed_pages": 0, "image_only_pages": 0}
    return {"issues": annotated, "summary": summary, "document": document,
            "options": {"purpose": "内部评审"}, "user_requirements": {}}


def test_verdict_store_roundtrip(tmp_path):
    verdicts.record(tmp_path, "f-font", "dismissed", note="图标语义正常")
    stored = verdicts.load(tmp_path)
    assert stored["f-font"]["status"] == "dismissed"
    assert stored["f-font"]["note"] == "图标语义正常"

    projected = verdicts.annotate([issue("f-font", 3, "improper_font_sizing")], stored)[0]
    assert projected["user_verdict"] == "dismissed"
    assert verdicts.is_dismissed(projected) and not verdicts.is_visible(projected)

    verdicts.record(tmp_path, "f-font", "confirmed")
    assert verdicts.load(tmp_path) == {}
    restored = verdicts.annotate([issue("f-font", 3, "improper_font_sizing")], {})[0]
    assert "user_verdict" not in restored and verdicts.is_visible(restored)


def test_corrupt_verdict_file_is_treated_as_empty(tmp_path):
    (tmp_path / "verdicts.json").write_text("{not json", encoding="utf-8")
    assert verdicts.load(tmp_path) == {}


def test_summary_drops_dismissed_from_counts_and_pages(tmp_path):
    summary = fixture_payload(tmp_path)["summary"]
    assert summary["issue_count"] == 2          # raw records stay intact
    assert summary["visible_issue_count"] == 1
    assert summary["dismissed_count"] == 1
    assert summary["pages_with_issues"] == 1
    assert summary["by_type"]["improper_font_sizing"] == 0
    assert summary["by_type"]["content_overflow_cutoff"] == 1


def test_advice_skips_dismissed_findings(tmp_path):
    payload = fixture_payload(tmp_path)
    result = advice.compose(payload)
    assert result["stats"]["issues"] == 1
    assert result["stats"]["dismissed"] == 1
    assert [action["defect_type"] for action in result["actions"]] == ["content_overflow_cutoff"]
    assert [page["slide_index"] for page in result["page_plan"]] == [2]
    assert "排除 1 条误判" in " ".join(result["summary"])

    spoken = " ".join([result["headline"], result["verdict"], *result["summary"],
                       *(action["title"] + action["directive"] for action in result["actions"])])
    assert not advice.has_hedge(spoken)


def test_prompt_lists_only_visible_pages_and_mentions_exclusion(tmp_path):
    prompt = advice.build_prompt(fixture_payload(tmp_path))
    assert "### 第 2 页" in prompt
    assert "### 第 3 页" not in prompt
    assert "排除 1 条误判" in prompt
    assert "improper_font_sizing" not in prompt


def test_prompt_without_dismissal_keeps_both_pages(tmp_path):
    prompt = advice.build_prompt(fixture_payload(tmp_path, dismissed=False))
    assert "### 第 2 页" in prompt and "### 第 3 页" in prompt
    assert "排除 0 条误判" not in prompt


def test_assertive_filter_drops_hedged_sentences():
    assert advice.assertive("也许是字号问题。把正文统一到 12pt。") == "把正文统一到 12pt。"
    assert advice.assertive("需要结合用途判断。把这三块合成两页。") == "把这三块合成两页。"
    assert advice.assertive("也许不合适。", "兜底文本") == "兜底文本"
    assert advice.assertive("", "兜底文本") == "兜底文本"


def test_composed_advice_is_json_serialisable(tmp_path):
    result = advice.compose(fixture_payload(tmp_path))
    assert json.loads(json.dumps(result, ensure_ascii=False))["source"] == "rule"

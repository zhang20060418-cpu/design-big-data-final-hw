from __future__ import annotations

import unittest

from ppt_audit_tool.contract import (
    ContractError,
    normalize_report,
    profile_catalog,
    profile_options,
    visible_issues,
)


def sample_report() -> dict:
    return {
        "schema_version": "4.4BC-report-1",
        "task": {"id": "t1", "status": "completed", "mode": "real", "options": {"enable_structure": True}},
        "document": {
            "file_name": "demo.pptx", "slide_count": 3, "aspect_label": "16:9",
            "size_mb": 1.2, "sha256_short": "abc123", "textboxes": 10,
        },
        "summary": {
            "slide_count": 3, "issue_count": 3, "visible_issue_count": 1,
            "by_type": {"improper_font_sizing": 1}, "high_risk_pages": [2],
        },
        "module_status": {
            "six_detector": {"status": "available", "version": "r2"},
            "visual": {"status": "not_configured"},
        },
        "issues": [
            {"issue_id": "i1", "slide_index": 2, "defect_type": "improper_font_sizing",
             "title": "字号偏小", "severity": "high", "confidence": "high",
             "review_status": "unreviewed", "execution_status": "candidate",
             "evidence_summary": "最小字号 7.2pt", "recommendation": "提高到 12pt",
             "bbox": {"x": 1, "y": 2, "w": 3, "h": 1},
             "sources": [{"source": "structure_rule"}], "limitations": ["估算"]},
            {"issue_id": "i2", "slide_index": 3, "defect_type": "occluded_content",
             "review_status": "rejected", "sources": []},
            {"issue_id": "i3", "slide_index": 3, "defect_type": "cluttered_layout",
             "review_status": "unreviewed", "user_verdict": "dismissed", "sources": []},
        ],
        "pages": [{
            "index": 2, "issue_count": 1, "mode_label": "可编辑页",
            "issues": [{"issue_id": "i1", "slide_index": 2,
                        "defect_type": "improper_font_sizing", "sources": []}],
        }],
        "risks": [{"type": "structure_limited", "tag": "能力", "title": "估算", "detail": "需看图"}],
        "limitations": ["结构规则是候选，不是人工真值。"],
        "generated_at": "2026-11-01T00:00:00+08:00",
    }


class ContractTests(unittest.TestCase):
    def test_profiles_are_consistent(self):
        catalog = profile_catalog()
        self.assertEqual([item["name"] for item in catalog], ["quick", "standard", "deep"])
        quick = profile_options("quick")
        self.assertFalse(quick["enable_six_detectors"])
        deep = profile_options("deep")
        self.assertTrue(deep["enable_visual"])
        self.assertTrue(deep["enable_six_detectors"])

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ContractError):
            profile_options("turbo")

    def test_visibility_matches_backend_rule(self):
        report = sample_report()
        visible = visible_issues(report)
        self.assertEqual([item["issue_id"] for item in visible], ["i1"])

    def test_normalize_report_projects_compact_fields(self):
        result = normalize_report(sample_report(), issue_mode="summary")
        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], "t1")
        self.assertEqual(result["issues_total"], 1)
        self.assertEqual(result["issues"][0]["issue_id"], "i1")
        self.assertEqual(result["issues"][0]["defect_label"], "字号不合理")
        self.assertEqual(result["modules"]["six_detector"]["status"], "available")
        self.assertEqual(result["pages"][0]["issue_ids"], ["i1"])
        self.assertEqual(result["document"]["file_name"], "demo.pptx")

    def test_issue_modes(self):
        none = normalize_report(sample_report(), issue_mode="none")
        self.assertEqual(none["issues"], [])
        self.assertEqual(none["issues_total"], 1)
        all_items = normalize_report(sample_report(), issue_mode="all")
        self.assertEqual(len(all_items["issues"]), 1)
        with self.assertRaises(ContractError):
            normalize_report(sample_report(), issue_mode="everything")

    def test_truncation_is_explicit(self):
        result = normalize_report(sample_report(), issue_mode="summary", max_issues=0)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["issues_truncated"], 1)


if __name__ == "__main__":
    unittest.main()

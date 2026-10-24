"""结果融合（fuse）的自动测试：重点覆盖“本地低置信 + 视觉反向”的冲突场景。

用法：python test_fuse.py
"""
from __future__ import annotations

from typing import Any

from agent_router import fuse


def make_finding(status: str = "confirmed_clip", conf: str = "low",
                 decision: str = "recheck", coverage: str = "checked",
                 max_pt: float = 24.0, has_text: bool = True,
                 empty_graphics: bool = False) -> dict[str, Any]:
    case = {
        "second_pass": {
            "status": status, "confidence": conf,
            "reasons": ["合成测试用例"], "needs_multimodal_review": decision == "recheck",
        },
        "page_overflow": {"max_pt": max_pt},
        "visibility": {"has_text": has_text, "empty_graphics": empty_graphics},
        "geometry_evidence": {"geometry_severity": "medium",
                              "frame_overflow_sides": [{"distance_pt": max_pt}],
                              "note": "合成测试"},
        "text_layout": {"visible_bbox_pt": {"left_pt": 0, "top_pt": 0, "width_pt": 100,
                                            "height_pt": 50, "right_pt": 100, "bottom_pt": 50}},
        "render_evidence": {"ink": {}},
    }
    return {
        "_case": case,
        "route": {"decision": decision, "reason": "合成测试", "rule_id": "R4",
                  "api_call_avoided": decision != "recheck"},
        "coverage_status": coverage,
        "occlusion_evidence": {"max_overlap_ratio": 0.0, "checked": True},
        "picture_overlap_evidence": {"max_overlap_ratio": 0.0, "checked": True},
    }


def ai(verdict: str, conf: str = "high", uncertainty: str | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "confidence": conf, "observations": ["合成观察"],
            "evidence_refs": ["合成位置"], "uncertainty": uncertainty}


def run_checks() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), detail))

    # 任务 1 的核心回归：本地 confirmed_clip / low，视觉 no_visible_problem / high。
    r = fuse(make_finding("confirmed_clip", "low", "recheck"),
             ai("no_visible_problem", "high"), None)
    check("冲突-最终为待人工核实", r["status"] == "manual", r["status"])
    check("冲突-不确认问题", r["status"] != "confirmed", r["status"])
    check("冲突-带冲突标记", r.get("conflict") is True, str(r.get("conflict")))
    check("冲突-原因写明证据冲突", "证据冲突" in r["reason"], r["reason"])
    check("冲突-状态细节为证据冲突待人工核实",
          r.get("status_detail") == "证据冲突，待人工核实", str(r.get("status_detail")))
    check("冲突-可靠度低且严重度未知",
          r["reliability"] == "low" and r["severity"] == "unknown",
          f"{r['reliability']}/{r['severity']}")

    # 反向冲突：本地判“无裁切”，视觉判“可见裁切”。
    r = fuse(make_finding("no_visible_clip", "high", "recheck"),
             ai("visible_clip", "high"), None)
    check("反向冲突-待人工核实", r["status"] == "manual" and r.get("conflict") is True,
          f"{r['status']}/{r.get('conflict')}")

    # 同向：本地低置信但视觉确认裁切，可以确认。
    r = fuse(make_finding("confirmed_clip", "low", "recheck"),
             ai("visible_clip", "high"), None)
    check("同向-视觉确认裁切则确认问题", r["status"] == "confirmed", r["status"])

    # 本地不确定但无方向性判断，视觉高置信说没问题 -> 可判未发现问题。
    r = fuse(make_finding("uncertain", "low", "recheck"),
             ai("no_visible_problem", "high"), None)
    check("不确定-视觉高置信否定后可判未发现问题", r["status"] == "clean", r["status"])

    # 遮挡不是裁切方向冲突。
    r = fuse(make_finding("no_visible_clip", "high", "recheck"),
             ai("visible_occlusion", "high"), None)
    check("遮挡-不误判为裁切冲突", r["status"] == "confirmed" and not r.get("conflict"),
          f"{r['status']}/{r.get('conflict')}")

    # 空形状：不适用，而不是“未发现问题”。
    r = fuse(make_finding("not_applicable", "high", "skip", has_text=False,
                          empty_graphics=True), None, None)
    check("空形状-不适用", r["status"] == "not_applicable", r["status"])
    check("空形状-不写成未发现问题", r["status_cn"] != "未发现问题", r["status_cn"])
    check("空形状-说明不代表整页", "不代表整页" in r["reason"], r["reason"])

    # 覆盖缺口：未检查 -> 待人工核实。
    r = fuse(make_finding("no_visible_clip", "high", "report",
                          coverage="not_checked"), None, None)
    check("未检查-待人工核实", r["status"] == "manual", r["status"])

    # 进入复核但未调用 -> 待人工核实。
    r = fuse(make_finding("uncertain", "low", "recheck"), None, None)
    check("未复核-待人工核实", r["status"] == "manual", r["status"])

    # 规则闭环的两条路径。
    r = fuse(make_finding("confirmed_clip", "high", "report"), None, None)
    check("规则闭环-高置信裁切确认", r["status"] == "confirmed", r["status"])
    r = fuse(make_finding("no_visible_clip", "high", "report"), None, None)
    check("规则闭环-高置信无裁切判无问题", r["status"] == "clean", r["status"])

    return out


def main() -> int:
    checks = run_checks()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

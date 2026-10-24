"""第三阶段验收：覆盖统计、路由、复核记录与融合结果的可复跑断言。

用法：python verify_agent.py
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from test_fuse import run_checks as run_fuse_checks

ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "outputs" / "agent"
DECK_DIR = ROOT.parent / "python-ppt" / "downloaded_pptx"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    structure = load(ROOT / "outputs" / "stage1_structure.json")
    result = load(AGENT / "agent_findings.json")
    mock = load(AGENT / "mock_run" / "agent_findings.json")
    expected = load(ROOT / "test_samples" / "stage3" / "expected.json")
    log = load(AGENT / "agent_run_log.json")

    cov = result["coverage"]
    s2 = cov["stage2_text_visibility"]
    check("覆盖-文字对象总数=195", cov["scope"]["text_objects"] == 195,
          str(cov["scope"]["text_objects"]))
    check("覆盖-全局仅检查 2 个", s2["text_objects_checked_global"] == 2,
          str(s2["text_objects_checked_global"]))
    check("覆盖-未检查 193 个", s2["text_objects_not_checked_global"] == 193,
          str(s2["text_objects_not_checked_global"]))
    check("覆盖-候选页 4 页", s2["pages_inspected"] == 4, str(s2["pages_inspected"]))
    check("覆盖-候选页文字 19 个", s2["text_objects_on_inspected_pages"] == 19,
          str(s2["text_objects_on_inspected_pages"]))
    check("覆盖-候选页已检查 2 个", s2["text_objects_checked"] == 2,
          str(s2["text_objects_checked"]))
    check("覆盖-候选页跳过 17 个", s2["text_objects_skipped_on_inspected_pages"] == 17,
          str(s2["text_objects_skipped_on_inspected_pages"]))

    p4 = next(p for p in cov["pages"]
              if p["file"] == "CenterAlignTitles_TestA.pptx" and p["slide"] == 4)
    check("覆盖-第 4 页文字 10 个", p4["text_objects_total"] == 10, str(p4["text_objects_total"]))
    check("覆盖-第 4 页检查 0 个", p4["text_objects_checked"] == 0, str(p4["text_objects_checked"]))
    check("覆盖-第 4 页未检查 10 个", p4["text_objects_not_checked"] == 10,
          str(p4["text_objects_not_checked"]))

    nc = cov["not_checked_objects"]
    check("覆盖-not_checked 台账 193 条", len(nc) == 193, str(len(nc)))
    check("覆盖-台账全部标记 not_checked",
          all(x["coverage_status"] == "not_checked" for x in nc))
    check("覆盖-台账不含“无缺陷”结论",
          all("无缺陷" not in (x.get("reason") or "") for x in nc))

    total_text = sum(p["text_objects_total"] for p in cov["pages"])
    checked_text = sum(p["text_objects_checked"] for p in cov["pages"])
    check("覆盖-逐页合计=195", total_text == 195, str(total_text))
    check("覆盖-逐页检查合计=2", checked_text == 2, str(checked_text))

    psc = s2["page_status_counts"]
    check("覆盖-页面状态三分类", set(psc) == {"complete", "partial", "not_checked"}, str(psc))
    check("覆盖-页面状态合计=62", sum(psc.values()) == 62, str(sum(psc.values())))
    check("覆盖-逐页状态与汇总一致",
          sum(1 for p in cov["pages"] if p["coverage_status"] == "partial") == psc["partial"],
          str(psc))
    check("覆盖-partial 仅候选页",
          all(p["coverage_status"] != "partial" or p["stage2_candidate_objects"] > 0
              for p in cov["pages"]))
    check("覆盖-not_checked 页不能写成无缺陷",
          all("无缺陷" not in (p.get("note") or "")
              for p in cov["pages"] if p["coverage_status"] == "not_checked"))

    findings = {f["finding_id"]: f for f in result["findings"]}
    check("结果-共 8 条 finding", len(findings) == 8, str(len(findings)))
    required = {"file", "slide", "shape_id", "stage1_geometry", "stage2_visibility",
                "coverage_status", "route", "ai_review", "final_status",
                "anomaly_level", "evidence_reliability", "defect_severity"}
    check("结果-字段完整", all(required <= set(f) for f in findings.values()),
          str([k for k, f in findings.items() if not required <= set(f)]))
    check("结果-最终状态枚举合法",
          all(f["final_status"] in {"确认问题", "可能有问题", "待人工核实", "未发现问题", "不适用"}
              for f in findings.values()))
    check("结果-均带冲突标记字段",
          all("evidence_conflict" in f and isinstance(f["evidence_conflict"], bool)
              for f in findings.values()))
    check("结果-决策枚举合法",
          all(f["route"]["decision"] in {"skip", "report", "recheck"}
              for f in findings.values()))
    check("结果-决策均记录规则与触发条件",
          all(f["route"].get("rule_id") and f["route"].get("trigger")
              and f["route"].get("reason") for f in findings.values()))

    expect_routes = {"F-R1": ("report", "R7"), "F-R2": ("report", "R7"),
                     "F-R3": ("skip", "R1"), "F-R4": ("skip", "R1"),
                     "F-V1": ("report", "R7"), "F-V2": ("recheck", "R4"),
                     "F-V3": ("recheck", "R5"), "F-V4": ("recheck", "R6")}
    wrong = [k for k, (d, r) in expect_routes.items()
             if findings[k]["route"]["decision"] != d or findings[k]["route"]["rule_id"] != r]
    check("路由-8 条与规则表一致", not wrong, str(wrong))

    for item in expected["cases"]:
        fid = f"F-{item['case_id']}"
        check(f"路由-{fid} 与 expected 一致",
              findings[fid]["route"]["decision"] == item["expected_route"],
              f"{findings[fid]['route']['decision']} != {item['expected_route']}")

    check("结果-R1/R2 未被送入复核", all(not findings[k]["ai_review"]
                                        for k in ("F-R1", "F-R2")))
    check("结果-R3/R4 跳过且有理由",
          all(findings[k]["route"]["decision"] == "skip" for k in ("F-R3", "F-R4")))

    ai = result["ai_review"]
    check("复核-共 3 页", ai["pages_queued"] == 3, str(ai["pages_queued"]))
    check("复核-视觉复核 3 次", ai["vision_reviews"] == 3, str(ai["vision_reviews"]))
    check("复核-宿主复核 3 次", ai.get("host_vision_calls") == 3, str(ai.get("host_vision_calls")))
    check("复核-第三方 API 0 次", ai.get("provider_api_calls") == 0,
          str(ai.get("provider_api_calls")))
    check("复核-未超过上限", ai["attempted"] <= ai["max_calls"],
          f"{ai['attempted']} > {ai['max_calls']}")
    check("复核-页面去重", len({c["page"] for c in ai["calls"]}) == len(ai["calls"]))
    check("复核-模式为 host", ai["mode"] == "host", ai["mode"])
    check("复核-无第三方 HTTP 调用", ai["api_http_calls"] == 0, str(ai["api_http_calls"]))
    check("复核-未标记模拟", not ai["simulated"])
    check("复核-三条都有可观察证据",
          all(c["response"]["objects"][0]["observations"] for c in ai["calls"]))
    check("复核-三条都有图片路径",
          all(Path(c["image"]).exists() for c in ai["calls"]))
    check("复核-V2 判定可见裁切",
          findings["F-V2"]["ai_review"]["evidence"]["verdict"] == "visible_clip")
    check("复核-V3 判定可见遮挡",
          findings["F-V3"]["ai_review"]["evidence"]["verdict"] == "visible_occlusion")
    check("复核-V4 判定可读性不足",
          findings["F-V4"]["ai_review"]["evidence"]["verdict"] == "low_readability")

    for c in ai["calls"]:
        prompt_file = AGENT / "review_prompts" / (c["page"].replace(".pptx", "").replace("#", "_") + ".txt")
        ok = prompt_file.exists()
        if ok:
            digest = hashlib.sha256(
                prompt_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]
            ok = digest == c["prompt_sha256"]
        check(f"复核-{c['call_id']} prompt 指纹可核对", ok, str(prompt_file))

    check("融合-R1/R2 未发现问题", findings["F-R1"]["final_status"] == "未发现问题"
          and findings["F-R2"]["final_status"] == "未发现问题")
    check("融合-V2 确认问题", findings["F-V2"]["final_status"] == "确认问题")
    check("融合-V3 确认问题", findings["F-V3"]["final_status"] == "确认问题")
    check("融合-V4 可能有问题", findings["F-V4"]["final_status"] == "可能有问题")
    check("融合-R3/R4 为不适用",
          findings["F-R3"]["final_status"] == "不适用"
          and findings["F-R4"]["final_status"] == "不适用")
    check("融合-不适用不写成未发现问题",
          all(findings[k]["final_status"] != "未发现问题" for k in ("F-R3", "F-R4")))
    check("融合-当前运行无证据冲突",
          not any(f["evidence_conflict"] for f in findings.values()))
    check("融合-R1 异常高但缺陷为无",
          findings["F-R1"]["anomaly_level"] == "high"
          and findings["F-R1"]["defect_severity"] == "none")
    check("融合-V2 三项分级齐全",
          findings["F-V2"]["anomaly_level"] == "high"
          and findings["F-V2"]["evidence_reliability"] == "high"
          and findings["F-V2"]["defect_severity"] == "high")

    check("模拟-标记 simulated", mock["ai_review"]["simulated"] is True)
    check("模拟-模式 mock", mock["ai_review"]["mode"] == "mock")
    check("模拟-全部复核对象待人工核实",
          all(f["final_status"] == "待人工核实"
              for f in mock["findings"] if f["route"]["decision"] == "recheck"))
    check("模拟-有明确提示", "模拟" in (mock["ai_review"]["notice"] or ""))
    check("模拟-不计入成功复核页数",
          mock["summary"]["api_savings"]["review_pages_succeeded"] == 0
          and mock["summary"]["api_savings"]["review_pages_simulated"] == 3,
          str(mock["summary"]["api_savings"]))

    sv = result["summary"]["api_savings"]
    check("成本-候选对象 8 个", sv["candidate_objects_total"] == 8,
          str(sv["candidate_objects_total"]))
    check("成本-规则直接闭环 5 个", sv["rule_closed_candidates"] == 5,
          str(sv["rule_closed_candidates"]))
    check("成本-需要视觉复核 3 个", sv["recheck_candidates"] == 3,
          str(sv["recheck_candidates"]))
    check("成本-实际成功复核 3 页", sv["review_pages_succeeded"] == 3,
          str(sv["review_pages_succeeded"]))
    check("成本-候选口径自洽",
          sv["rule_closed_candidates"] + sv["recheck_candidates"]
          == sv["candidate_objects_total"])
    check("成本-未检查对象不折算成节省",
          sv["not_checked_counted_as_savings"] is False
          and sv["not_checked_objects"] == 193, str(sv["not_checked_objects"]))
    check("成本-不再输出 avoided_api_calls 合计", "avoided_api_calls" not in sv)

    real = ai["real_api_integration"]
    check("API-真实联调状态未完成", real["status"] == "not_completed", real["status"])
    check("API-明确说明尚未完成", "尚未完成" in real["reason"], real["reason"])
    check("API-第三方调用 0 次", real["provider_api_calls"] == 0,
          str(real["provider_api_calls"]))

    for name, ok, detail in run_fuse_checks():
        check(f"融合测试-{name}", ok, detail)

    check("安全-输出不含 Authorization", "Authorization" not in json.dumps(result))
    check("安全-输出不含 Bearer", "Bearer" not in json.dumps(result))
    check("安全-未读取到 Key", result["ai_review"]["api_key_present"] is False)
    check("安全-日志不含 Key", "sk-" not in json.dumps(log))

    api_test = load(AGENT / "api_client_test.json")
    check("API 客户端-本地桩通过", api_test["passed"] is True)
    check("API 客户端-明确非真实模型", api_test["real_model"] is False)
    check("API 客户端-usage 与费用字段可读",
          api_test["usage"]["total_tokens"] == 1280
          and api_test["cost"]["amount"] == 0.000608)

    for deck in structure["decks"]:
        path = Path(deck["path"])
        if not path.exists():
            check(f"原始文件存在-{path.name}", False)
            continue
        check(f"原始文件未修改-{path.name}", sha256_file(path) == deck["sha256"],
              "sha256 不一致")

    check("产物-报告存在", (AGENT / "agent_report.md").exists())
    check("产物-运行日志存在", (AGENT / "agent_run_log.json").exists())
    check("产物-宿主复核证据存在", (AGENT / "host_review.json").exists())
    host = load(AGENT / "host_review.json")
    hash_ok = True
    for cid, item in host.get("inputs", {}).items():
        for key in ("clean_png", "review_png"):
            path = ROOT / item[key]
            expected = item[key.replace("_png", "_sha256")]
            if not path.exists() or sha256_file(path) != expected:
                hash_ok = False
    check("产物-宿主复核图片哈希可核对", hash_ok)

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    print(f"\n{passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

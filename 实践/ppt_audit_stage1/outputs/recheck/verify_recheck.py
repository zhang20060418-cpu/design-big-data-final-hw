"""第三步验收：实际运行复检程序，并对结果做断言。

    python outputs/recheck/verify_recheck.py

覆盖的验收点
------------
1. 第一阶段结果未被改写（stage1_structure.json / stage1_violations.json 哈希不变）
2. 4 条真实记录的 geometry_evidence 与第一阶段逐字段一致，可追溯
3. 原两条"外框越界、文字正常"的案例不再被判为 confirmed_clip
4. 两条不可见空形状判为 not_applicable，且审查图默认不画其几何框（调试开关可显示）
5. 第 4 页覆盖台账如实区分“页内文字对象 / 已复检 / 跳过”，不把 not_applicable 当整页结论
6. 合成样本中的真实页面裁切能被发现（confirmed_clip + 页边墨迹）
7. 正常样本 / 框内溢出样本不被误报为裁切
8. 每条结果都带齐必填字段与证据图
9. 原始 PPTX 未被修改
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STAGE1 = ROOT / "outputs"

checks: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_color(path: Path, rgb: tuple[int, int, int]) -> int:
    """统计精确颜色像素数，用于确认审查图里是否真的画了几何虚线。"""
    im = Image.open(path).convert("RGB")
    data = im.tobytes()
    r, g, b = rgb
    return sum(1 for i in range(0, len(data), 3)
               if data[i] == r and data[i + 1] == g and data[i + 2] == b)


def main() -> int:
    s1_structure = STAGE1 / "stage1_structure.json"
    s1_violations = STAGE1 / "stage1_violations.json"
    before = {p.name: sha256(p) for p in (s1_structure, s1_violations)}

    # 1. 实际运行复检程序
    proc = subprocess.run([sys.executable, str(HERE / "text_visibility_recheck.py")],
                          capture_output=True, text=True, cwd=str(ROOT))
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip())
        return 1

    after = {p.name: sha256(p) for p in (s1_structure, s1_violations)}
    check("第一阶段 JSON 未被改写", before == after,
          "、".join(f"{k} {v[:8]}" for k, v in after.items()))

    data = json.loads((HERE / "text_recheck.json").read_text(encoding="utf-8"))
    v1 = json.loads(s1_violations.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in data["cases"]}
    real = [c for c in data["cases"] if c["group"] == "real_case"]

    # 2. 4 条真实记录 + 几何证据可追溯
    check("复检覆盖第一阶段全部 4 条记录", len(real) == len(v1["violations"]),
          f"real={len(real)} / stage1={len(v1['violations'])}")
    mismatch = []
    for rec in v1["violations"]:
        hit = [c for c in real if (c["file"], c["slide"], c["shape_id"]) ==
               (rec["file"], rec["slide"], rec["shape_id"])]
        if not hit:
            mismatch.append(f"缺 {rec['file']} p{rec['slide']} id={rec['shape_id']}")
            continue
        g = hit[0]["geometry_evidence"]
        if g["violations"] != rec["directions"] or g["geometry_severity"] != rec["severity"]:
            mismatch.append(f"{rec['file']} p{rec['slide']} 几何字段不一致")
    check("geometry_evidence 与第一阶段逐字段一致（方向/距离/级别）", not mismatch,
          "; ".join(mismatch) or "4/4 一致")

    # 3. 两条外框越界但文字正常的案例
    for cid, name in (("R1", "AddAgenda p3 id13"), ("R2", "Calibri p14 id4")):
        st = cases[cid]["second_pass"]["status"]
        check(f"{name} 未被判为已确认裁切", st != "confirmed_clip", f"status={st}")
    check("R2 的整段越界被归因于空段落（可见文字未越界）",
          cases["R2"]["page_overflow"].get("layout_only_note") is not None,
          cases["R2"]["page_overflow"].get("layout_only_note") or "")

    # 4. 两条不可见空形状
    for cid in ("R3", "R4"):
        c = cases[cid]
        check(f"{cid} 空形状判为 not_applicable 且保留结构记录",
              c["second_pass"]["status"] == "not_applicable"
              and c["visibility"]["empty_graphics"] is True
              and c["geometry_evidence"]["frame_bbox_pt"] is not None,
              f"status={c['second_pass']['status']} empty={c['visibility']['empty_graphics']}")

    # 4b. 空形状默认不画几何框；有文字的候选仍画
    dash_bad = []
    for cid in ("R3", "R4"):
        c = cases[cid]
        ann = c.get("annotation") or {}
        png = HERE / (c["render_evidence"].get("annotated_png") or "")
        n = count_color(png, (180, 140, 40)) if png.exists() else -1
        if not (ann.get("frame_suppressed") is True
                and ann.get("frame_drawn") is False
                and n == 0):
            dash_bad.append(f"{cid} suppressed={ann.get('frame_suppressed')} "
                            f"drawn={ann.get('frame_drawn')} dash_px={n}")
    check("not_applicable 空形状的审查图不绘制几何框（虚线像素=0）",
          not dash_bad, "; ".join(dash_bad) or "R3/R4 dash_px=0")
    r1_png = HERE / (cases["R1"]["render_evidence"].get("annotated_png") or "")
    check("有文字的候选仍绘制几何框（隐藏只针对空形状）",
          cases["R1"]["annotation"].get("frame_drawn") is True
          and count_color(r1_png, (180, 140, 40)) > 0,
          f"R1 dash_px={count_color(r1_png, (180, 140, 40)) if r1_png.exists() else -1}")

    # 4c. 第 4 页覆盖台账
    cov = data.get("page_coverage") or {}
    p4 = next((r for r in cov.get("pages", [])
               if r["file"] == "CenterAlignTitles_TestA.pptx" and r["slide"] == 4), None)
    check("第 4 页覆盖台账：10 个文字对象 / 复检 0 / 跳过 10，且注明不代表整页无问题",
          p4 is not None
          and p4["text_objects_total"] == 10
          and p4["text_objects_checked"] == 0
          and p4["text_objects_skipped"] == 10
          and bool(p4.get("interpretation_note")),
          (f"{p4['text_objects_total']}/{p4['text_objects_checked']}/{p4['text_objects_skipped']}"
           if p4 else "missing"))
    cov_real = cov.get("real_pages") or {}
    check("覆盖口径自洽：checked + skipped = 总数",
          cov_real.get("text_objects_checked", -1) + cov_real.get("text_objects_skipped", -1)
          == cov_real.get("text_objects_total"),
          f"{cov_real.get('text_objects_checked')}+{cov_real.get('text_objects_skipped')}="
          f"{cov_real.get('text_objects_total')}")

    # 5. 合成样本：真实页面裁切
    for label, cid in (("S2_RightClippedText", None), ("S2_BottomClippedText", None)):
        c = next((x for x in data["cases"] if x.get("sample_label") == label), None)
        if c is None:
            check(f"样本文本 {label} 存在", False)
            continue
        edge = (c["render_evidence"].get("ink") or {}).get("edge") or {}
        ratio = max([e.get("edge_ink_ratio") or 0 for e in edge.values()] or [0])
        check(f"{label} 被发现为 confirmed_clip（页边墨迹 {ratio:.1%}）",
              c["second_pass"]["status"] == "confirmed_clip" and ratio > 0,
              f"status={c['second_pass']['status']} edge_ink={ratio:.3f}")

    # 6. 正常样本 / 框内溢出样本不误报
    for label in ("S1_RightFrame_TextVisible", "S1_BottomFrame_TextVisible",
                  "S3_TightBox_NoAutofit", "S3_TightBox_ShrinkText"):
        c = next((x for x in data["cases"] if x.get("sample_label") == label), None)
        check(f"{label} 未被误报为裁切",
              c is not None and c["second_pass"]["status"] != "confirmed_clip",
              f"status={c['second_pass']['status'] if c else 'missing'}")

    # 7. 必填字段完整 + 证据图存在
    required = ("status", "confidence", "reasons", "evidence_sources",
                "defect_severity", "needs_multimodal_review")
    missing = []
    for c in data["cases"]:
        miss = [k for k in required if k not in c["second_pass"]]
        if miss:
            missing.append(f"{c['case_id']} 缺 {miss}")
        ann = c["render_evidence"].get("annotated_png")
        if not ann or not (HERE / ann).exists():
            missing.append(f"{c['case_id']} 缺标注图")
        for k in ("file", "slide", "shape_id", "geometry_evidence", "text_layout"):
            if k not in c:
                missing.append(f"{c['case_id']} 缺 {k}")
    check("每条结果字段齐备且证据图存在", not missing, "; ".join(missing[:5]) or f"{len(data['cases'])} 条")

    # 8. 状态口径
    allowed = {"confirmed_clip", "no_visible_clip", "uncertain", "not_applicable"}
    bad = [c["case_id"] for c in data["cases"] if c["second_pass"]["status"] not in allowed]
    check("状态取值均在约定集合内", not bad, ", ".join(bad) or "4 类枚举")

    # 9. 样本期望标签比对
    sc = data.get("sample_check") or {}
    check("对照样本期望标签全部一致", sc.get("total", 0) > 0 and sc.get("passed") == sc.get("total"),
          f"{sc.get('passed')}/{sc.get('total')}，失败 {len(sc.get('failed') or [])}")

    # 10. 源文件未被修改
    src_bad = [s["file"] for s in data["source_check"] if s["unchanged"] is False]
    check("原始 PPTX 哈希与第一阶段一致", not src_bad, ", ".join(src_bad) or
          f"{len(data['source_check'])} 份")

    # 11. 报告与链接
    report = HERE / "recheck_report.md"
    md = report.read_text(encoding="utf-8") if report.exists() else ""
    links = [p for p in __import__("re").findall(r"\]\(([^)]+\.png)\)", md)]
    missing_links = [p for p in links if not (HERE / p).exists()]
    check("报告存在且引用的截图路径均可解析", bool(md) and not missing_links,
          f"link={len(links)} missing={missing_links[:3]}")

    passed = sum(1 for c in checks if c["pass"])
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "passed": passed == len(checks),
        "check_count": len(checks),
        "failed_count": len(checks) - passed,
        "checks": checks,
        "summary": data["summary"],
    }
    (HERE / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(checks)} 通过 → {HERE / 'verification.json'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

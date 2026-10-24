"""第一阶段验收：真实运行检测程序，并对结果逐项断言。

运行：
    python verify_stage1.py

产出：
    outputs/stage1_verification.json   机器可读的断言结果
    outputs/测试总结.md                简短测试总结（四个问题的答案）
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "python-ppt" / "downloaded_pptx"
OUT = HERE / "outputs"
SELF_OUT = OUT / "selfcheck"
TOL_IN = 0.02


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(shapes) -> tuple[int, int]:
    """递归统计 (对象数, 含文字对象数)，与检测程序的统计口径一致。"""
    n_shapes = n_text = 0
    for sh in shapes:
        n_shapes += 1
        n_text += 1 if has_text(sh) else 0
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            child_shapes, child_text = walk(sh.shapes)
            n_shapes += child_shapes
            n_text += child_text
    return n_shapes, n_text


def has_text(sh) -> bool:
    try:
        if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
            return True
    except (AttributeError, ValueError):
        pass
    try:
        if getattr(sh, "has_table", False) and sh.has_table:
            return any(c.text.strip() for r in sh.table.rows for c in r.cells)
    except (AttributeError, ValueError):
        pass
    try:
        if getattr(sh, "has_chart", False) and sh.has_chart and sh.chart.has_title:
            return bool(sh.chart.chart_title.text_frame.text.strip())
    except (AttributeError, ValueError):
        pass
    return False


def run_audit(input_dir: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(HERE / "audit_stage1.py"),
            "--input",
            str(input_dir),
            "--out-dir",
            str(out_dir),
            "--tolerance-in",
            str(TOL_IN),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(HERE),
    )


def check(checks: list, name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    # 0. 记录原始文件哈希，用于证明未被修改
    originals = [p for p in sorted(SAMPLES.glob("*.pptx")) if not p.name.startswith("~$")]
    hashes_before = {p.name: sha256(p) for p in originals}
    mtimes_before = {p.name: p.stat().st_mtime_ns for p in originals}

    # 1. 独立统计基线（不复用被测代码）
    expected_slides = 0
    expected_shapes = 0
    expected_text = 0
    expected_decks = 0
    for p in originals:
        prs = Presentation(str(p))
        expected_decks += 1
        expected_slides += len(prs.slides)
        for slide in prs.slides:
            shape_n, text_n = walk(slide.shapes)
            expected_shapes += shape_n
            expected_text += text_n
    lock_files = [p.name for p in SAMPLES.glob("~$*")]

    # 2. 真实运行
    proc = run_audit(SAMPLES, OUT)
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    check(checks, "检测程序正常退出", proc.returncode == 0, f"returncode={proc.returncode}")

    structure = json.loads((OUT / "stage1_structure.json").read_text(encoding="utf-8"))
    violations_doc = json.loads((OUT / "stage1_violations.json").read_text(encoding="utf-8"))
    summary = structure["summary"]

    # 3. 读取数量
    check(checks, "读取的 PPT 份数", summary["deck_count"] == expected_decks == 6,
          f"{summary['deck_count']} 份（基线 {expected_decks}，锁文件 {len(lock_files)} 个已跳过）")
    check(checks, "读取的总页数", summary["slide_count"] == expected_slides == 62,
          f"{summary['slide_count']} 页（基线 {expected_slides}）")
    check(checks, "全部文件解析成功", summary["failed_deck_count"] == 0,
          f"失败 {summary['failed_deck_count']} 份")
    check(checks, "对象总数与基线一致", summary["shape_count"] == expected_shapes,
          f"{summary['shape_count']} 个对象（基线 {expected_shapes}，含组内子对象）")

    # 4. 结构字段完整性
    all_shapes = [sh for d in structure["decks"] for s in d["slides"] for sh in s["shapes"]]
    geom_ok = all(
        sh["geometry"] and all(sh["geometry"][k] is not None for k in ("left_emu", "top_emu", "width_emu", "height_emu"))
        for sh in all_shapes
    )
    check(checks, "每个对象都有四至坐标与尺寸", geom_ok, f"{len(all_shapes)}/{len(all_shapes)} 个对象几何完整")
    check(
        checks,
        "对象 ID 唯一且齐全",
        all(sh["shape_id"] is not None for sh in all_shapes)
        and len({(d["file"], s["index"], sh["shape_id"]) for d in structure["decks"] for s in d["slides"] for sh in s["shapes"]})
        == len(all_shapes),
        "同页内 shape_id 唯一",
    )
    text_ok = summary["text_shape_count"] == expected_text
    check(checks, "文字提取结果与基线一致", text_ok,
          f"{summary['text_shape_count']} 个对象含文字（基线 {expected_text}）")
    check(
        checks,
        "文字内容可回溯",
        all(sh["text"] for sh in all_shapes if sh["has_text"]),
        "含文字对象均有非空 text 字段",
    )

    # 5. 组坐标变换自洽性：对子对象均未旋转的组，组框应等于组内子对象映射后的并集
    groups_total = 0
    groups_checked = 0
    mismatches = []
    for d in structure["decks"]:
        for s in d["slides"]:
            for g in s["shapes"]:
                if g["shape_type"] != "GROUP" or not g["geometry"]:
                    continue
                groups_total += 1
                kids = [c for c in s["shapes"] if c["parent_group_id"] == g["shape_id"] and c["geometry"]]
                if not kids or any(c["rotation_deg"] for c in kids):
                    continue
                groups_checked += 1
                gb = g["geometry"]
                union = (
                    min(c["geometry"]["left_in"] for c in kids),
                    min(c["geometry"]["top_in"] for c in kids),
                    max(c["geometry"]["right_in"] for c in kids),
                    max(c["geometry"]["bottom_in"] for c in kids),
                )
                delta = max(
                    abs(union[0] - gb["left_in"]),
                    abs(union[1] - gb["top_in"]),
                    abs(union[2] - gb["right_in"]),
                    abs(union[3] - gb["bottom_in"]),
                )
                if delta > 0.02:
                    mismatches.append((d["file"], s["index"], g["shape_id"], round(delta, 3)))
    check(
        checks,
        "组形状坐标变换自洽",
        not mismatches and groups_checked >= 5,
        f"共 {groups_total} 个组，校验 {groups_checked} 个子对象无旋转的组，偏差均 < 0.02in",
    )

    # 6. 自检样本：真实运行
    build = subprocess.run([sys.executable, str(HERE / "make_test_decks.py")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    check(checks, "自检样本生成", build.returncode == 0, (build.stdout or build.stderr).strip().splitlines()[-1])

    self_proc = run_audit(HERE / "test_samples", SELF_OUT)
    check(checks, "自检样本检测运行", self_proc.returncode == 0, f"returncode={self_proc.returncode}")
    self_doc = json.loads((SELF_OUT / "stage1_violations.json").read_text(encoding="utf-8"))
    self_structure = json.loads((SELF_OUT / "stage1_structure.json").read_text(encoding="utf-8"))
    expected = json.loads((HERE / "test_samples" / "expected.json").read_text(encoding="utf-8"))

    slide1 = self_structure["decks"][0]["slides"][0]
    slide2 = self_structure["decks"][0]["slides"][1]
    check(checks, "正常页零误报", slide1["violation_count"] == 0,
          f"第 1 页 {slide1['shape_count']} 个对象，越界 {slide1['violation_count']} 条")

    found = {v["name"]: v for v in slide2["violations"]}
    expected_names = {e["name"] for e in expected["slide2_expected"]}
    check(checks, "越界页检出数量", len(found) == 4 and set(found) == expected_names,
          f"检出 {len(found)} 条，预期 {len(expected_names)} 条：{sorted(found)}")

    detail_ok = True
    for exp in expected["slide2_expected"]:
        v = found.get(exp["name"])
        if not v:
            detail_ok = False
            continue
        sides = {d["side"]: d["distance_in"] for d in v["directions"]}
        if exp["side"] not in sides or abs(sides[exp["side"]] - exp["distance_in"]) > 0.02:
            detail_ok = False
    check(checks, "越界方向与距离准确", detail_ok,
          "左 0.60in / 右 0.50in / 上 0.35in / 下 0.50in 全部命中（容差 ±0.02in）")

    control_ok = not any(name in found for name in expected["slide2_expected_clean_names"])
    check(checks, "同页正常对象不误报", control_ok,
          f"对照组均未出现：{expected['slide2_expected_clean_names']}")

    rotated = [sh for sh in slide2["shapes"] if sh["name"] == "对照-旋转但未越界"][0]
    check(checks, "旋转对象按外接框判定", rotated["rotation_deg"] == 30 and rotated["overflows"] == [],
          f"rotation={rotated['rotation_deg']}°，外接框在页内，未误报")

    # 6. 原始文件未被修改
    hashes_after = {p.name: sha256(p) for p in originals}
    mtimes_after = {p.name: p.stat().st_mtime_ns for p in originals}
    unchanged = hashes_before == hashes_after and mtimes_before == mtimes_after
    check(checks, "原始 PPT 未被修改", unchanged,
          f"{len(originals)} 份文件 SHA-256 与修改时间均未变化")

    # 7. 输出文件齐全
    required = ["stage1_structure.json", "stage1_violations.json"]
    check(checks, "JSON 输出齐全", all((OUT / f).exists() for f in required), ", ".join(required))

    # 8. 汇总
    passed = all(c["passed"] for c in checks)
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tolerance_in": TOL_IN,
        "passed": passed,
        "check_count": len(checks),
        "failed_count": sum(1 for c in checks if not c["passed"]),
        "checks": checks,
        "metrics": {
            "decks": summary["deck_count"],
            "slides": summary["slide_count"],
            "shapes": summary["shape_count"],
            "shapes_with_text": summary["text_shape_count"],
            "violations_in_samples": summary["violation_count"],
            "violations_in_selfcheck": self_doc["summary"]["violation_count"],
        },
    }
    (OUT / "stage1_verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'ALL PASS' if passed else 'HAS FAILURES'}  ({len(checks) - result['failed_count']}/{len(checks)})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

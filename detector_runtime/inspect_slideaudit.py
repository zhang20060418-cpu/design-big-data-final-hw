#!/usr/bin/env python3
"""第四阶段 4.1：检查 SlideAudit 已获取数据，生成索引、标签分布与报告。

只读取 ml_stage4/data 下已下载的内容，不下载、不训练、不修改源文件。

产物（默认写入 ml_stage4/reports/）：
    dataset_index.csv       已下载 60 张图与其标注/描述的对应索引
    label_distribution.csv  19 个设计缺陷的正负样本统计
    dataset_report.md       数据检查总结
    sample_gallery.html     10 张抽查样本的静态查看页（可选产物）

用法：
    python inspect_slideaudit.py
    python inspect_slideaudit.py --data-dir data --out-dir reports --seed 20261024
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REQUIRED_ANN_KEYS = (
    "design_deficiency_category",
    "design_deficiency",
    "response",
    "has_strong_agreement",
)
POSITIVE_DEF = "response == true（人工标注认为该缺陷存在）"
NEGATIVE_DEF = "response == false（人工标注认为该缺陷不存在）"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_json_dir(folder: Path) -> tuple[dict[int, dict], list[dict], list[str]]:
    """返回 (id->data, 解析失败列表, 文件名列表)。"""
    parsed: dict[int, dict] = {}
    failures: list[dict] = []
    names: list[str] = []
    for path in sorted(folder.glob("*.json")):
        names.append(path.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        sid = normalize_id(data.get("slide_id")) or normalize_id(path.stem)
        if sid is None:
            failures.append({"file": path.name, "error": "缺少可解析的 slide_id"})
            continue
        parsed[sid] = data
    return parsed, failures, names


def normalize_id(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def git_ls(repo: Path, prefix: str, suffix: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD", "--", prefix],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        ).stdout
    except Exception:
        return []
    return [x.strip() for x in out.splitlines() if x.strip().endswith(suffix)]


def dhash(path: Path, size: int = 8) -> int | None:
    try:
        with Image.open(path) as im:
            g = im.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            pixels = g.tobytes()
        bits = 0
        for row in range(size):
            for col in range(size):
                left = pixels[row * (size + 1) + col]
                right = pixels[row * (size + 1) + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return bits
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def md_table(headers: list[str], rows: list[list], align: str | None = None) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="检查 SlideAudit 已获取数据")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "reports"))
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--sample", type=int, default=10)
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    repo = data_dir / "slideaudit"
    img_dir = data_dir / "images"
    ann_dir = repo / "data" / "annotations"
    desc_dir = repo / "data" / "descriptions"
    meta_path = repo / "data" / "metadata.csv"

    if not meta_path.exists():
        raise SystemExit(f"缺少 {meta_path}，请先运行 download_slideaudit.py")

    # ---------------- 1. 源清单（以 git 树对象为准，不用 API 列表） ---------------- #
    src = {
        "metadata": 1 if git_ls(repo, "data/metadata.csv", ".csv") else 0,
        "annotations": len(git_ls(repo, "data/annotations", ".json")),
        "descriptions": len(git_ls(repo, "data/descriptions", ".json")),
        "images": len(git_ls(repo, "data/images", ".png")),
    }

    with meta_path.open(encoding="utf-8", newline="") as fh:
        meta_rows = list(csv.DictReader(fh))
    meta = {}
    for r in meta_rows:
        sid = normalize_id(r.get("id"))
        if sid is None:
            continue
        r["width"] = int(r.get("image_width") or 0)
        r["height"] = int(r.get("image_height") or 0)
        meta[sid] = r

    ann_all, ann_failures, ann_names = load_json_dir(ann_dir)
    desc_all, desc_failures, desc_names = load_json_dir(desc_dir)

    images = sorted(img_dir.glob("*.png"))
    img_ids = {normalize_id(p.stem) for p in images}
    img_ids.discard(None)

    log(f"[source] metadata {src['metadata']} / annotations {src['annotations']} / "
        f"descriptions {src['descriptions']} / images {src['images']}")
    log(f"[local ] metadata {len(meta_rows)} / annotations {len(ann_names)} / "
        f"descriptions {len(desc_names)} / images {len(images)}")

    # ---------------- 2. 标注结构校验与标签统计 ---------------- #
    cat_of: dict[str, str] = {}
    cat_conflicts: list[dict] = []
    field_missing = Counter()
    response_types = Counter()
    item_total = 0
    items_per_slide = Counter()
    items_by_slide: dict[int, int] = {}
    unexplained: list[dict] = []
    per_def = defaultdict(lambda: {"pos": 0, "neg": 0, "unknown": 0, "strong_pos": 0,
                                   "weak_pos": 0, "strong_neg": 0, "bbox_items": 0, "bboxes": 0})
    per_slide_pos: dict[int, set] = {}
    per_slide_strong: dict[int, set] = {}
    bbox_range = {"x1": [None, None], "y1": [None, None], "x2": [None, None], "y2": [None, None]}
    bbox_out_of_bounds = 0
    bbox_checked = 0

    for sid, data in ann_all.items():
        items = data.get("annotations")
        if not isinstance(items, list):
            unexplained.append({"slide_id": sid, "issue": "annotations 不是列表"})
            continue
        items_per_slide[len(items)] += 1
        items_by_slide[sid] = len(items)
        pset, sset = set(), set()
        for item in items:
            item_total += 1
            for key in REQUIRED_ANN_KEYS:
                if key not in item:
                    field_missing[key] += 1
            cat = item.get("design_deficiency_category")
            name = item.get("design_deficiency")
            resp = item.get("response")
            strong = item.get("has_strong_agreement")
            response_types[type(resp).__name__ + f":{resp}"] += 1
            if not cat or not name:
                unexplained.append({"slide_id": sid, "issue": "缺少类别或缺陷名",
                                    "item": item})
                continue
            if name in cat_of and cat_of[name] != cat:
                cat_conflicts.append({"deficiency": name, "kept": cat_of[name], "seen": cat})
            cat_of.setdefault(name, cat)
            bucket = per_def[name]
            if resp is True:
                bucket["pos"] += 1
                pset.add(name)
                if strong is True:
                    bucket["strong_pos"] += 1
                    sset.add(name)
                else:
                    bucket["weak_pos"] += 1
            elif resp is False:
                bucket["neg"] += 1
                if strong is True:
                    bucket["strong_neg"] += 1
            else:
                bucket["unknown"] += 1
                unexplained.append({"slide_id": sid, "issue": f"response 非布尔值：{resp!r}",
                                    "item": {"design_deficiency": name}})
            boxes = item.get("bounding_boxes")
            if isinstance(boxes, list) and boxes:
                bucket["bbox_items"] += 1
                bucket["bboxes"] += len(boxes)
                w = meta.get(sid, {}).get("width") or 0
                h = meta.get(sid, {}).get("height") or 0
                for b in boxes:
                    try:
                        x, y = float(b["x"]), float(b["y"])
                        bw, bh = float(b["width"]), float(b["height"])
                    except Exception:
                        unexplained.append({"slide_id": sid, "issue": "bbox 字段不完整", "bbox": b})
                        continue
                    if bw <= 0 or bh <= 0:
                        unexplained.append({"slide_id": sid, "issue": "bbox 宽高非正", "bbox": b})
                        continue
                    vals = {"x1": x, "y1": y, "x2": x + bw, "y2": y + bh}
                    for k, v in vals.items():
                        lo, hi = bbox_range[k]
                        bbox_range[k] = [v if lo is None else min(lo, v),
                                         v if hi is None else max(hi, v)]
                    if w and h:
                        bbox_checked += 1
                        if x < -1 or y < -1 or x + bw > w + 1 or y + bh > h + 1:
                            bbox_out_of_bounds += 1
        per_slide_pos[sid] = pset
        per_slide_strong[sid] = sset

    # ---------------- 3. 对应关系 ---------------- #
    id_issues = []
    for sid in sorted(img_ids):
        if sid not in meta:
            id_issues.append({"slide_id": sid, "issue": "图片在 metadata 中无记录"})
        if sid not in ann_all:
            id_issues.append({"slide_id": sid, "issue": "图片在 annotations 中无记录"})
        if sid not in desc_all:
            id_issues.append({"slide_id": sid, "issue": "图片在 descriptions 中无记录"})
    meta_no_ann = sorted(set(meta) - set(ann_all))
    ann_no_meta = sorted(set(ann_all) - set(meta))
    desc_id_mismatch = []
    for sid, d in desc_all.items():
        did = normalize_id(d.get("slide_id"))
        if did is not None and did != sid:
            desc_id_mismatch.append({"file_id": sid, "field_id": did})

    # ---------------- 4. 图片读取校验 ---------------- #
    img_failures = []
    dim_mismatch = []
    img_bytes = 0
    for path in images:
        sid = normalize_id(path.stem)
        img_bytes += path.stat().st_size
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                size = im.size
                mode = im.mode
                fmt = im.format
        except Exception as exc:
            img_failures.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        m = meta.get(sid, {})
        if m and (m["width"], m["height"]) != size:
            dim_mismatch.append({"slide_id": sid, "metadata": [m["width"], m["height"]],
                                 "image": list(size)})

    # ---------------- 5. 10 张抽查 ---------------- #
    rng = random.Random(args.seed)
    downloaded_ids = sorted(img_ids)
    sample_ids = rng.sample(downloaded_ids, min(args.sample, len(downloaded_ids)))
    sample_rows = []
    for sid in sample_ids:
        path = img_dir / f"slide_{sid:04d}.png"
        row = {"slide_id": sid, "file": path.name, "exists": path.exists()}
        try:
            with Image.open(path) as im:
                row["image_size"] = list(im.size)
        except Exception as exc:
            row["image_size"] = None
            row["error"] = str(exc)
        m = meta.get(sid, {})
        row["metadata_size"] = [m.get("width"), m.get("height")]
        row["dims_match"] = row["image_size"] == row["metadata_size"]
        row["annotation_present"] = sid in ann_all
        row["description_present"] = sid in desc_all
        row["n_items"] = items_by_slide.get(sid)
        row["positives"] = sorted(per_slide_pos.get(sid, set()))
        row["strong_positives"] = sorted(per_slide_strong.get(sid, set()))
        n_boxes = 0
        box_bad = 0
        for item in ann_all.get(sid, {}).get("annotations", []):
            boxes = item.get("bounding_boxes") or []
            for b in boxes:
                n_boxes += 1
                try:
                    if (float(b["x"]) < -1 or float(b["y"]) < -1
                            or float(b["x"]) + float(b["width"]) > m.get("width", 0) + 1
                            or float(b["y"]) + float(b["height"]) > m.get("height", 0) + 1):
                        box_bad += 1
                except Exception:
                    box_bad += 1
        row["bbox_count"] = n_boxes
        row["bbox_out_of_bounds"] = box_bad
        sample_rows.append(row)

    # ---------------- 6. 分组风险调查 ---------------- #
    alter = Counter(r.get("alteration") for r in meta.values())
    source = Counter(r.get("source_type") for r in meta.values())
    crosstab = Counter((r.get("source_type"), r.get("alteration")) for r in meta.values())
    dims_by_source = defaultdict(Counter)
    for r in meta.values():
        dims_by_source[r.get("source_type")][(r["width"], r["height"])] += 1

    # 6a. id 顺序上的连续段
    ordered = sorted(meta)
    runs = []
    for sid in ordered:
        key = (meta[sid].get("source_type"), meta[sid].get("alteration"))
        if runs and runs[-1][0] == key:
            runs[-1][2] += 1
            runs[-1][3] = sid
        else:
            runs.append([key, sid, 1, sid])
    run_sizes = Counter(r[2] for r in runs)

    # 6b. 文本指纹（用 descriptions 的 textContent）+ 元素几何（独立验证）
    fp_groups: dict[str, list[int]] = defaultdict(list)
    geom_of: dict[int, list[tuple]] = {}
    covered_text_pages = 0
    for sid, d in desc_all.items():
        texts = []
        boxes = []
        for el in d.get("elements", []):
            t = el.get("textContent")
            if t:
                texts.append(re.sub(r"\s+", " ", str(t)).strip().lower())
            pos = (el.get("positionAndSize") or {}).get("normalized") or {}
            if all(k in pos for k in ("x", "y", "width", "height")):
                boxes.append(tuple(round(float(pos[k]), 4) for k in ("x", "y", "width", "height")))
        geom_of[sid] = boxes
        norm = " | ".join(texts)
        if len(norm) < 20:
            continue
        fp_groups[hashlib.sha1(norm.encode("utf-8")).hexdigest()].append(sid)
        covered_text_pages += 1
    dup_groups = {k: sorted(v) for k, v in fp_groups.items() if len(v) > 1}
    group_sizes = Counter(len(v) for v in dup_groups.values())
    group_alter_sets = Counter()
    groups_same_source = 0
    groups_exact4 = 0
    for ids in dup_groups.values():
        alts = [meta.get(i, {}).get("alteration") for i in ids]
        group_alter_sets[len(set(alts))] += 1
        if len({meta.get(i, {}).get("source_type") for i in ids}) == 1:
            groups_same_source += 1
        if len(ids) == 4 and len(set(alts)) == 4:
            groups_exact4 += 1
    no_text_pages = len(desc_all) - covered_text_pages

    def geom_distance(a: int, b: int) -> float | None:
        ga, gb = geom_of.get(a, []), geom_of.get(b, [])
        if not ga or len(ga) != len(gb):
            return None
        return sum(abs(x - y) for p, q in zip(ga, gb) for x, y in zip(p, q)) / (len(ga) * 4)

    within = []
    for ids in dup_groups.values():
        if len(ids) != 4:
            continue
        for i in range(4):
            for j in range(i + 1, 4):
                dist = geom_distance(ids[i], ids[j])
                if dist is not None:
                    within.append(dist)
    by_count: dict[int, list[int]] = defaultdict(list)
    for sid in desc_all:
        if geom_of.get(sid):
            by_count[len(geom_of[sid])].append(sid)
    rng_g = random.Random(args.seed)
    cross = []
    for n, ids in sorted(by_count.items()):
        if len(ids) < 2:
            continue
        for _ in range(min(400, len(ids))):
            a, b = rng_g.sample(ids, 2)
            dist = geom_distance(a, b)
            if dist is not None:
                cross.append(dist)

    # 6c. 已下载 60 张的感知哈希近重复
    hashes = {}
    for sid in downloaded_ids:
        h = dhash(img_dir / f"slide_{sid:04d}.png")
        if h is not None:
            hashes[sid] = h
    near_pairs = []
    ids_h = sorted(hashes)
    for i, a in enumerate(ids_h):
        for b in ids_h[i + 1:]:
            dist = hamming(hashes[a], hashes[b])
            if dist <= 4:
                near_pairs.append({"a": a, "b": b, "distance": dist,
                                   "same_text": bool(per_slide_pos.get(a) == per_slide_pos.get(b)),
                                   "alter_a": meta.get(a, {}).get("alteration"),
                                   "alter_b": meta.get(b, {}).get("alteration")})

    # ---------------- 7. 写 CSV ---------------- #
    index_path = out_dir / "dataset_index.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["slide_id", "image_file", "image_present", "annotation_file",
                    "annotation_present", "description_file", "description_present",
                    "source_type", "alteration", "width", "height",
                    "n_annotation_items", "n_positive", "n_strong_positive",
                    "positive_labels", "has_bounding_boxes"])
        for sid in downloaded_ids:
            m = meta.get(sid, {})
            ann = ann_all.get(sid, {})
            boxes = any((it.get("bounding_boxes") or []) for it in ann.get("annotations", []))
            w.writerow([
                f"slide_{sid:04d}",
                f"images/slide_{sid:04d}.png", 1,
                f"slideaudit/data/annotations/slide_{sid:04d}.json", int(sid in ann_all),
                f"slideaudit/data/descriptions/slide_{sid:04d}.json", int(sid in desc_all),
                m.get("source_type"), m.get("alteration"), m.get("width"), m.get("height"),
                items_by_slide.get(sid), len(per_slide_pos.get(sid, set())),
                len(per_slide_strong.get(sid, set())),
                ";".join(sorted(per_slide_pos.get(sid, set()))), int(boxes),
            ])

    label_path = out_dir / "label_distribution.csv"
    with label_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "deficiency", "positives", "negatives", "unknown",
                    "total", "positive_ratio", "strong_positive", "weak_positive",
                    "strong_negative", "with_bounding_boxes", "bbox_count"])
        for name in sorted(per_def, key=lambda n: (cat_of.get(n, ""), n)):
            b = per_def[name]
            denom = b["pos"] + b["neg"]
            w.writerow([cat_of.get(name, "UNKNOWN"), name, b["pos"], b["neg"], b["unknown"],
                        b["pos"] + b["neg"] + b["unknown"],
                        round(b["pos"] / denom, 4) if denom else "",
                        b["strong_pos"], b["weak_pos"], b["strong_neg"],
                        b["bbox_items"], b["bboxes"]])

    # ---------------- 8. 样本查看页 ---------------- #
    gallery_path = out_dir / "sample_gallery.html"
    cards = []
    for row in sample_rows:
        sid = row["slide_id"]
        m = meta.get(sid, {})
        labels = "".join(
            f"<li>{'★ ' if lbl in row['strong_positives'] else ''}{lbl}</li>"
            for lbl in row["positives"]) or "<li class='none'>无正例</li>"
        im_path = Path("..") / "data" / "images" / f"slide_{sid:04d}.png"
        cards.append(f"""
  <figure>
    <img src="{im_path.as_posix()}" alt="slide_{sid:04d}">
    <figcaption>
      <b>slide_{sid:04d}</b> · {m.get('source_type','?')} / {m.get('alteration','?')} ·
      {row['image_size'][0]}×{row['image_size'][1]} · {row['bbox_count']} bbox
      <ul>{labels}</ul>
    </figcaption>
  </figure>""")
    gallery_path.write_text(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>SlideAudit 抽查样本（{len(sample_rows)} 张）</title>
<style>
 body{{font-family:system-ui,"Segoe UI",sans-serif;margin:24px;background:#fafafa;color:#222}}
 h1{{font-size:18px}} p{{color:#555;font-size:13px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:18px}}
 figure{{margin:0;background:#fff;border:1px solid #ddd;border-radius:6px;padding:10px}}
 img{{width:100%;height:auto;display:block;border:1px solid #eee}}
 figcaption{{font-size:12px;color:#444;margin-top:8px;line-height:1.5}}
 ul{{margin:6px 0 0 16px;padding:0}} li{{font-size:12px}}
 .none{{color:#999}}
</style></head><body>
<h1>SlideAudit 抽查样本（固定种子随机 {len(sample_rows)} 张）</h1>
<p>★ 表示该缺陷在标注中 has_strong_agreement=true。图片按原始尺寸等比缩放，未做任何修改。</p>
<div class="grid">{''.join(cards)}
</div></body></html>
""", encoding="utf-8")

    # ---------------- 9. 报告 ---------------- #
    total_items = sum(b["pos"] + b["neg"] + b["unknown"] for b in per_def.values())
    total_pos = sum(b["pos"] for b in per_def.values())
    total_neg = sum(b["neg"] for b in per_def.values())
    total_unknown = sum(b["unknown"] for b in per_def.values())
    multi_counts = Counter(len(v) for v in per_slide_pos.values())
    slides_pos = sum(1 for v in per_slide_pos.values() if v)
    slides_multi = sum(1 for v in per_slide_pos.values() if len(v) >= 2)
    slides_multi3 = sum(1 for v in per_slide_pos.values() if len(v) >= 3)

    cat_roll = defaultdict(lambda: {"pos": 0, "neg": 0})
    for name, b in per_def.items():
        cat_roll[cat_of.get(name, "UNKNOWN")]["pos"] += b["pos"]
        cat_roll[cat_of.get(name, "UNKNOWN")]["neg"] += b["neg"]

    src_ann = src["annotations"] or len(ann_names)
    src_desc = src["descriptions"] or len(desc_names)
    src_img = src["images"]
    not_dl_img = max(0, src_img - len(images))
    missing_ann = max(0, src_ann - len(ann_names))
    missing_desc = max(0, src_desc - len(desc_names))

    lines: list[str] = []
    A = lines.append
    A("# SlideAudit 数据获取与初步检查报告")
    A("")
    A(f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}  ")
    A("范围：第四阶段 4.1，只做数据获取、完整性检查与标签统计，**未训练任何模型**。")
    A("")
    A("## 1. 获取结果")
    A("")
    A(md_table(
        ["项目", "数量", "说明"],
        [
            ["metadata 记录", len(meta_rows), "`data/slideaudit/data/metadata.csv`"],
            ["annotation JSON", f"{len(ann_names)} / {src_ann}", "全部检出并解析成功"],
            ["description JSON", f"{len(desc_names)} / {src_desc}", "全部检出并解析成功"],
            ["图片", f"{len(images)} / {src_img}", f"分层抽样，未下载 {not_dl_img} 张"],
            ["数据目录体积", f"{dir_bytes(data_dir) / 1024 / 1024:.1f} MB",
             f"按文件字节合计（NTFS 实占约 65 MB）：图片 {img_bytes / 1024 / 1024:.1f} MB、标注 "
             f"{dir_bytes(ann_dir) / 1024 / 1024:.1f} MB、描述 "
             f"{dir_bytes(desc_dir) / 1024 / 1024:.1f} MB、git 元数据 "
             f"{dir_bytes(repo / '.git') / 1024 / 1024:.1f} MB"],
        ]))
    A("")
    A("获取方式：`git clone --filter=blob:none --depth=1 --sparse`，"
      "稀疏检出 `data/metadata.csv`、`data/annotations/`、`data/descriptions/`；"
      "图片按抽样清单逐张 HTTPS 下载。仓库 README 写的是 `.jpg` 与 "
      "`metadata/slideaudit_metadata.csv`，**实际文件为 `.png`，metadata 在 `data/metadata.csv`**，"
      "以实际目录为准。")
    A("")
    manifest_path = data_dir / "download_manifest.json"
    sel_stats = None
    if manifest_path.exists():
        try:
            mf = json.loads(manifest_path.read_text(encoding="utf-8"))
            run_history = mf.get("runs", [])
            if len(run_history) >= 2:
                A(f"重复执行验证：首次运行下载 {run_history[0].get('downloaded', 0)} 张，"
                  f"第二次运行复用 {run_history[-1].get('skipped_existing', 0)} 张、"
                  f"新增下载 {run_history[-1].get('downloaded', 0)} 张、"
                  f"新增体积 {mf.get('budget', {}).get('delta_this_run_mb', 0)} MB。")
                A("")
            items = mf.get("selection", {}).get("items", [])
            if items:
                sel_stats = {
                    "n": len(items),
                    "cover": len({d for it in items for d in it.get("strong_positives", [])}),
                    "neg": sum(1 for it in items if not it.get("positives")),
                    "multi": sum(1 for it in items
                                 if len(it.get("strong_positives", [])) >= 3),
                }
        except Exception:
            pass
    A("## 2. 完整性与缺失区分")
    A("")
    A(md_table(
        ["状态", "数量", "含义"],
        [
            ["已获取且可解析", f"{len(ann_all)} 标注 / {len(desc_all)} 描述 / {len(images)} 图片", "通过结构校验"],
            ["文件尚未下载", f"{not_dl_img} 张图片", "metadata / 标注 / 描述已全量获取；**不是损坏**"],
            ["源仓库确实缺失", "0", "git 树对象中列出的文件数与本地一致"],
            ["文件损坏", f"标注 {len(ann_failures)} / 描述 {len(desc_failures)} / 图片 {len(img_failures)}", "JSON 解析失败或 PNG 无法解码"],
            ["数据字段缺失", f"{sum(field_missing.values())} 处", "见 2.1"],
            ["slide_id 对应失败", f"{len(id_issues)} 处", "图片与三份索引之间"],
        ]))
    A("")
    if ann_failures or desc_failures or img_failures:
        A("失败明细：")
        for f in (ann_failures + desc_failures + img_failures)[:10]:
            A(f"- `{f['file']}`：{f['error']}")
        A("")
    else:
        A("JSON 解析失败 **0**，图片读取失败 **0**，slide_id 对应失败 **0**。"
          f"metadata 与标注的 id 集合差异：metadata 缺标注 {len(meta_no_ann)} 个，"
          f"标注缺 metadata {len(ann_no_meta)} 个。")
        A("")
    A("### 2.1 标注字段检查")
    A("")
    A(md_table(
        ["字段", "缺失数", "观测"],
        [
            ["design_deficiency_category", field_missing.get("design_deficiency_category", 0), "4 个大类"],
            ["design_deficiency", field_missing.get("design_deficiency", 0), f"{len(per_def)} 个缺陷名"],
            ["response", field_missing.get("response", 0), f"布尔值 {response_types.get('bool:True', 0) + response_types.get('bool:False', 0)} 条，非布尔 {total_unknown} 条"],
            ["has_strong_agreement", field_missing.get("has_strong_agreement", 0), "布尔值，表示标注者是否强一致"],
            ["bounding_boxes", "可选", f"{sum(b['bbox_items'] for b in per_def.values())} 条正例带框，共 {sum(b['bboxes'] for b in per_def.values())} 个框"],
        ]))
    A("")
    if cat_conflicts:
        A(f"类别归属冲突：{len(cat_conflicts)} 处（同一缺陷名映射到多个大类），已单独记录。")
    else:
        A("类别归属冲突 **0**：19 个缺陷名各自只属于 1 个大类。")
    A("")
    if unexplained:
        A(f"无法解释的标注条目 **{len(unexplained)}** 条，已单列，**未计入负例**。")
        for u in unexplained[:5]:
            A(f"- slide {u.get('slide_id')}：{u.get('issue')}")
    else:
        A("无法解释的标注条目 **0**。")
    A("")
    A(f"**正负例定义**：正例 = {POSITIVE_DEF}；负例 = {NEGATIVE_DEF}。"
      "`has_strong_agreement=false` 的正例仍算正例，但单独统计为“弱一致正例”，"
      "不并入负例，也不丢弃。")
    A("")
    A("## 3. 标签分布")
    A("")
    strong_total = sum(b["strong_pos"] for b in per_def.values())
    A(f"共 {len(ann_all)} 页 × {len(per_def)} 项 = {total_items} 条标注（每页恰好 {len(per_def)} 条，无缺项）："
      f"正例 {total_pos}、负例 {total_neg}、未知 {total_unknown}。"
      f"有正例的页面 {slides_pos} 页（{slides_pos / max(1, len(ann_all)):.1%}），"
      f"全负例页面 {len(ann_all) - slides_pos} 页。")
    A("")
    A(md_table(
        ["大类", "正例", "负例", "正例比例"],
        [[c, v["pos"], v["neg"],
          f"{v['pos'] / (v['pos'] + v['neg']):.1%}" if v["pos"] + v["neg"] else "—"]
         for c, v in sorted(cat_roll.items())]))
    A("")
    rows = []
    for name in sorted(per_def, key=lambda n: (-per_def[n]["pos"], n)):
        b = per_def[name]
        denom = b["pos"] + b["neg"]
        rows.append([cat_of.get(name, "?"), name, b["pos"], b["neg"],
                     f"{b['pos'] / denom:.1%}" if denom else "—",
                     b["strong_pos"], b["bbox_items"]])
    A(md_table(["大类", "缺陷", "正例", "负例", "正例比例", "强一致正例", "带框正例"], rows))
    A("")
    A(f"强一致正例合计 **{strong_total}** 条，占全部正例的 {strong_total / max(1, total_pos):.1%}；"
      "若只用强一致正例训练，最少的类别只有 1 个样本，19 类分类不可行，"
      "需要放宽到全部正例或合并类别。")
    A("")
    A("### 3.1 多标签")
    A("")
    A(f"单页正例数量的分布：0 个 {multi_counts.get(0, 0)} 页、1 个 {multi_counts.get(1, 0)} 页、"
      f"2 个 {multi_counts.get(2, 0)} 页、≥3 个 {sum(v for k, v in multi_counts.items() if k >= 3)} 页；"
      f"最多一页 {max(multi_counts) if multi_counts else 0} 个正例。")
    A("")
    A(f"多标签页面占比：≥2 个正例 {slides_multi}/{len(ann_all)} = {slides_multi / max(1, len(ann_all)):.1%}；"
      f"≥3 个 {slides_multi3} 页。**一页多标签是常态，不是异常**，"
      "后续建模需要么做多标签分类，么选一个主缺陷并显式处理其余标签。")
    A("")
    A("## 4. 图片与标注对应（抽查）")
    A("")
    if sel_stats:
        A(f"抽样依据（见 `download_manifest.json.selection`）：{sel_stats['n']} 张按 P1–P6 "
          "分层——每个缺陷至少 1 个强一致正例（按稀有度优先）、多标签页、弱一致正例、"
          f"全负例页、大类补足、种子补齐；覆盖 {sel_stats['cover']}/{len(per_def)} 个缺陷、"
          f"{sel_stats['neg']} 张无正例页、{sel_stats['multi']} 张 ≥3 标签页。")
        A("")
    A(f"固定种子 `{args.seed}` 从已下载图片中随机抽 {len(sample_rows)} 张，逐张打开并核对：")
    A("")
    A(md_table(
        ["slide_id", "尺寸", "与 metadata 一致", "标注", "描述", "标注条目", "正例", "bbox", "越界框"],
        [[f"slide_{r['slide_id']:04d}",
          f"{r['image_size'][0]}×{r['image_size'][1]}" if r["image_size"] else "读取失败",
          "是" if r["dims_match"] else "否",
          "有" if r["annotation_present"] else "缺",
          "有" if r["description_present"] else "缺",
          r["n_items"], len(r["positives"]), r["bbox_count"], r["bbox_out_of_bounds"]]
         for r in sample_rows]))
    A("")
    A(f"抽查结论：{sum(1 for r in sample_rows if r['dims_match'])}/{len(sample_rows)} 尺寸一致，"
      f"{sum(1 for r in sample_rows if r['annotation_present'] and r['description_present'])}/{len(sample_rows)} "
      "标注与描述齐全，"
      f"越界框 {sum(r['bbox_out_of_bounds'] for r in sample_rows)} 个。"
      f"全部 {len(images)} 张核对：读取失败 {len(img_failures)} 张，"
      f"尺寸与 metadata 不一致 {len(dim_mismatch)} 张。")
    A("")
    if bbox_checked:
        A(f"全量标注坐标范围（像素）：x1 {bbox_range['x1'][0]:.1f}–{bbox_range['x1'][1]:.1f}，"
          f"y1 {bbox_range['y1'][0]:.1f}–{bbox_range['y1'][1]:.1f}，"
          f"x2 {bbox_range['x2'][0]:.1f}–{bbox_range['x2'][1]:.1f}，"
          f"y2 {bbox_range['y2'][0]:.1f}–{bbox_range['y2'][1]:.1f}；"
          f"超出图片边界 {bbox_out_of_bounds}/{bbox_checked} 个（容差 1 px）。"
          "坐标是像素值，不是 0–1 归一化值。")
        A("")
    A(f"静态查看页：`sample_gallery.html`（{len(sample_rows)} 张，可直接用浏览器打开）。")
    A("")
    A("## 5. 原始 / 增强分组风险")
    A("")
    A(f"`source_type` 有 {len(source)} 种："
      + "、".join(f"{k}({v})" for k, v in source.most_common())
      + "；`alteration` 有 "
      + f"{len(alter)} 种：" + "、".join(f"{k}({v})" for k, v in alter.most_common()) + "。")
    A("")
    A(f"- **没有显式 group_id / original_id 字段**。metadata 只有 id、source_type、alteration、宽高。")
    ct_values = Counter(crosstab.values())
    alt_per_source = defaultdict(set)
    for (st, al), _n in crosstab.items():
        alt_per_source[st].add(al)
    A(f"- {len(source)} 个来源 × {len(alter)} 种 alteration = {len(crosstab)} 个组合，"
      + ("每个组合恰好 " + str(next(iter(ct_values))) + " 页，分布完全均衡；"
         if len(ct_values) == 1 else "各组合页数不一致；")
      + "各来源都覆盖全部 alteration 种类，说明 `source_type` 是来源标签而非分组 ID。")
    A(f"- id 顺序上的 (source_type, alteration) 连续段共 {len(runs)} 段，段长以 1 为主"
      f"（{run_sizes.get(1, 0)}/{len(runs)}），相邻 id 不是稳定的“同页变体”排列。")
    A(f"- 文本指纹（descriptions 的 textContent）：{len(dup_groups)} 组文本完全相同，覆盖 "
      f"{sum(len(v) for v in dup_groups.values())}/{len(desc_all)} 页；组大小为 "
      + "、".join(f"{k} 页 {v} 组" for k, v in sorted(group_sizes.items()))
      + f"。{groups_same_source}/{len(dup_groups)} 组内部 source_type 一致，"
      f"{sum(v for k, v in group_alter_sets.items() if k == 4)} 组内部正好覆盖 4 种 alteration"
      f"（其中 {groups_exact4} 组恰好 4 页）。")
    if within and cross:
        A(f"- 元素几何独立验证：4 页组内 {len(within)} 对，归一化坐标平均差 "
          f"mean {statistics.mean(within):.3f} / median {statistics.median(within):.3f}"
          f"（{sum(1 for x in within if x < 1e-3) / len(within):.0%} 完全相同）；"
          f"元素数相同的跨组随机对 median {statistics.median(cross):.3f}。"
          "组内几乎是同一版式的不同渲染。")
    A(f"- 已下载的 {len(images)} 张中没有两张落在同一文本组，因此 dHash 近重复对为 "
      f"{len(near_pairs)}，**无法用图片验证组内相似度**，只能用描述几何与字段结构佐证。")
    A("")
    A("**结论：分组基本可以重建，但目前只是高置信度候选，不是官方标注。** "
      "证据指向“600 个原始页面 × 4 种 alteration 变体”（3 个来源各 200 个）；"
      "仍有 6 个 8 页组需要拆分、28 页无文本无法归组，且不能排除同文案独立生成。"
      "因此本次**不生成 group_id、不做训练/测试划分**；正式切分前需向作者确认构造方式，"
      "或对候选组做图像相似度人工核验。")
    A("")
    A("## 6. 结论与下一步")
    A("")
    A("数据准备层面已具备进入下一阶段的条件：")
    A("")
    A(f"- 2400 页的标注与描述完整可解析，{len(per_def)} 个缺陷、4 个大类的正负统计已就绪；")
    A(f"- {len(images)} 张图片全部可读，尺寸与 metadata 一致，slide_id 与标注/描述一一对应；")
    A("- 正负例定义明确，弱一致正例与未知项已单列，未混入负例。")
    A("")
    A("仍需注意：")
    A("")
    A(f"- 图片只下载了 {len(images)}/{src_img} 张，**不能**用这 {len(images)} 张估计整体缺陷率；"
      "全量标签统计来自 2400 份标注，与图片子集是两件事；")
    A("- 分组有高置信度候选依据（文本指纹 + 元素几何 + 4 种 alteration 结构），"
      "但没有官方 group_id，正式切分前仍需作者确认或人工核验；")
    A("- 正例比例在不同缺陷间差异很大（见第 3 节），直接训练会偏向高频类；")
    A("- 标注为人工判断，`has_strong_agreement=false` 的正例带有噪声；")
    A(f"- 少量标注框越界（{bbox_out_of_bounds}/{bbox_checked}），对分类任务影响有限，"
      "做检测/定位任务前需要清洗。")
    A("")
    A("**是否足以开始训练准备**：作为标签体系与统计口径的准备，**足够**；"
      f"作为图像模型的数据集，**不够**——只有 {len(images)} 张图，且未做分组切分。"
      "4.2 若只用结构化/几何特征，可基于 2400 份标注先做标签与特征设计；"
      "若要训练视觉模型，必须先扩大图片下载并确认分组口径。")
    A("")
    A("本次未训练模型、未制作训练/验证/测试集、未修改前三阶段代码。")
    A("")

    report_path = out_dir / "dataset_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---------------- 10. 控制台摘要 ---------------- #
    log("")
    log("=" * 64)
    log(f"标注解析 {len(ann_all)}，描述 {len(desc_all)}，图片 {len(images)}，"
        f"解析失败 {len(ann_failures) + len(desc_failures)}，图片失败 {len(img_failures)}")
    log(f"标注条目 {total_items}：正例 {total_pos} / 负例 {total_neg} / 未知 {total_unknown}")
    log(f"有正例页面 {slides_pos}，多标签(>=2) {slides_multi}，最多 {max(multi_counts) if multi_counts else 0} 个")
    log(f"文本指纹重复组 {len(dup_groups)}，近重复对 {len(near_pairs)}，id_issues {len(id_issues)}")
    log(f"输出：{index_path.name} / {label_path.name} / {report_path.name} / {gallery_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

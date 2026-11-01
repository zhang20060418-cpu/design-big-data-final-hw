#!/usr/bin/env python3
"""第四阶段 4.1：获取 SlideAudit 结构化数据 + 分层抽样图片。

设计原则
--------
1. 结构化数据（metadata.csv / annotations / descriptions）用 **git 部分克隆**
   （--filter=blob:none --depth 1）+ sparse checkout 获取，
   不下载 data/images 下的 2400 张图，也不拉完整历史。
2. 图片只按分层抽样下载一个子集（默认 60 张，上限 80），
   单张走 HTTPS，逐张记账，失败不无限重试。
3. 可重复执行：已存在且校验通过的文件直接跳过。
4. 所有路径基于脚本自身位置，可用 --data-dir 覆盖。

用法
----
    python download_slideaudit.py                    # 结构化数据 + 60 张抽样图
    python download_slideaudit.py --limit-images 40  # 只下 40 张
    python download_slideaudit.py --skip-images      # 只取结构化数据
    python download_slideaudit.py --force-images     # 重新下载已有图片
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_URL = "https://github.com/zhuohaouw/SlideAudit.git"
RAW_BASE = "https://raw.githubusercontent.com/zhuohaouw/SlideAudit/main"

# 只检出这三类结构化数据；图片目录刻意排除
SPARSE_PATTERNS = [
    "/data/metadata.csv",
    "/data/annotations/",
    "/data/descriptions/",
]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_SEED = 20261024


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env_note = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    import os

    env = {**os.environ, **env_note}
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n{proc.stderr[-2000:]}")
    return proc


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def human_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


# --------------------------------------------------------------------------- #
# 1. 结构化数据：部分克隆 + 稀疏检出
# --------------------------------------------------------------------------- #
def git_tree_paths(repo: Path, prefix: str, suffix: str) -> list[str]:
    """用本仓库 HEAD 的树对象统计源文件，不依赖 GitHub API 的目录列表。"""
    out = run_git(["ls-tree", "-r", "--name-only", "HEAD", "--", prefix], cwd=repo).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip().endswith(suffix)]


def ensure_structured(repo: Path) -> dict:
    if not (repo / ".git").exists():
        if repo.exists() and any(repo.iterdir()):
            raise RuntimeError(f"{repo} 已存在但不是 git 仓库，请先人工确认")
        repo.parent.mkdir(parents=True, exist_ok=True)
        log(f"[git] 部分克隆（blob:none, depth=1）→ {repo}")
        run_git([
            "clone", "--filter=blob:none", "--depth", "1", "--no-tags",
            "--sparse", REPO_URL, str(repo),
        ])
    else:
        log(f"[git] 复用已有仓库：{repo}")

    # 稀疏模式：只保留结构化数据，图片目录不检出
    run_git(["sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS], cwd=repo)
    # 确保工作区与稀疏规则一致（已存在文件不会重复下载）
    run_git(["checkout", "-f", "HEAD"], cwd=repo)

    meta = repo / "data" / "metadata.csv"
    ann_dir = repo / "data" / "annotations"
    desc_dir = repo / "data" / "descriptions"
    for p in (meta, ann_dir, desc_dir):
        if not p.exists():
            raise RuntimeError(f"稀疏检出后缺少 {p}")

    src_ann = git_tree_paths(repo, "data/annotations", ".json")
    src_desc = git_tree_paths(repo, "data/descriptions", ".json")
    src_img = git_tree_paths(repo, "data/images", ".png")
    got_ann = sorted(p.name for p in ann_dir.glob("*.json"))
    got_desc = sorted(p.name for p in desc_dir.glob("*.json"))

    log(f"[git] 标注 {len(got_ann)}/{len(src_ann)}，描述 {len(got_desc)}/{len(src_desc)}，"
        f"图片源 {len(src_img)} 张（未检出）")

    return {
        "repo_dir": str(repo),
        "repo_url": REPO_URL,
        "clone_mode": "blob:none + depth=1 + sparse",
        "sparse_patterns": SPARSE_PATTERNS,
        "source_counts": {
            "annotations": len(src_ann),
            "descriptions": len(src_desc),
            "images": len(src_img),
        },
        "checked_out": {
            "metadata_csv": True,
            "annotations": len(got_ann),
            "descriptions": len(got_desc),
            "images": 0,
        },
    }


def read_metadata(meta_path: Path) -> list[dict]:
    with meta_path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["id"] = str(r.get("id", "")).strip()
    return rows


# --------------------------------------------------------------------------- #
# 2. 标签索引与分层抽样
# --------------------------------------------------------------------------- #
def build_label_index(ann_dir: Path) -> dict[int, dict]:
    idx: dict[int, dict] = {}
    for path in sorted(ann_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_id = data.get("slide_id")
        if raw_id is None:
            continue
        sid = int(str(raw_id).replace("slide_", "").lstrip("0") or 0)
        pos, strong = set(), set()
        for a in data.get("annotations", []):
            if a.get("response") is True:
                name = a.get("design_deficiency")
                if name:
                    pos.add(name)
                    if a.get("has_strong_agreement") is True:
                        strong.add(name)
        idx[sid] = {"positives": pos, "strong_positives": strong,
                    "n_items": len(data.get("annotations", []))}
    return idx


def select_images(idx: dict[int, dict], limit: int, seed: int) -> tuple[list[dict], list[str]]:
    """确定性分层抽样，返回 (选择列表, 规则说明)。

    优先级：
      P1 每个缺陷至少 1 个强一致正例（按缺陷稀有度升序，先保稀有类）
      P2 多标签页（>=3 个强一致正例）
      P3 弱一致正例（有正例但无强一致，用于观察标注噪声）
      P4 全负例页（19 项全为 false）
      P5 每个大类补足到 8 个强正例
      P6 固定种子随机补齐
    """
    picked: "OrderedDict[int, dict]" = OrderedDict()

    def add(sid: int, reason: str) -> bool:
        if sid in picked or len(picked) >= limit:
            return False
        info = idx.get(sid)
        if info is None:
            return False
        picked[sid] = {
            "slide_id": sid,
            "reason": reason,
            "positives": sorted(info["positives"]),
            "strong_positives": sorted(info["strong_positives"]),
            "n_positives": len(info["positives"]),
        }
        return True

    deficiency_pos = Counter()
    for v in idx.values():
        for d in v["strong_positives"]:
            deficiency_pos[d] += 1
    deficiencies_rare_first = [d for d, _ in sorted(deficiency_pos.items(),
                                                     key=lambda kv: (kv[1], kv[0]))]

    # P1 每个缺陷 1 个强正例
    for d in deficiencies_rare_first:
        cands = sorted(sid for sid, v in idx.items() if d in v["strong_positives"])
        for sid in cands[:1]:
            add(sid, f"P1 稀有缺陷覆盖：{d}（全局强正例 {deficiency_pos[d]} 个）")

    # P2 多标签页
    multi = sorted((sid for sid, v in idx.items() if len(v["strong_positives"]) >= 3),
                   key=lambda s: (-len(idx[s]["strong_positives"]), s))
    for sid in multi[: max(0, min(6, limit - len(picked)))]:
        add(sid, f"P2 多标签：{len(idx[sid]['strong_positives'])} 个强一致正例")

    # P3 弱一致正例（有正例但都不是强一致）
    weak = sorted(sid for sid, v in idx.items()
                  if v["positives"] and not v["strong_positives"])
    rng = random.Random(seed)
    rng.shuffle(weak)
    for sid in weak[: max(0, min(4, limit - len(picked)))]:
        add(sid, "P3 弱一致正例：有正例但无强一致，用于观察标注噪声")

    # P4 全负例页
    negative = sorted(sid for sid, v in idx.items() if not v["positives"] and v["n_items"] >= 10)
    rng.shuffle(negative)
    for sid in negative[: max(0, min(10, limit - len(picked)))]:
        add(sid, "P4 全负例：19 项标注全为 false")

    # P5 每个大类补足 8 个强正例（大类来自标注条目，运行时按索引反推较麻烦，
    #    这里用“缺陷名 → 大类”映射在 main 中补齐，避免重复读取 JSON）
    # 该步在 main() 中调用 top_up_by_category()

    # P6 随机补齐
    remaining = [sid for sid in idx if sid not in picked]
    rng.shuffle(remaining)
    for sid in remaining:
        if len(picked) >= limit:
            break
        add(sid, "P6 固定种子随机补齐")

    rules = [
        "P1 每个设计缺陷至少 1 个强一致正例，按全局稀有度排序",
        "P2 多标签页（>=3 个强一致正例）最多 6 张",
        "P3 弱一致正例最多 4 张（有正例但无强一致）",
        "P4 全负例页最多 10 张",
        "P5 每个大类补足到 8 个强正例（若未达上限）",
        "P6 固定随机种子补齐到目标数量",
    ]
    return list(picked.values()), rules


def deficiency_to_category(ann_dir: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in sorted(ann_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for a in data.get("annotations", []):
            c, d = a.get("design_deficiency_category"), a.get("design_deficiency")
            if c and d:
                mapping.setdefault(d, c)
    return mapping


def top_up_by_category(picked: list[dict], idx: dict[int, dict],
                       d2c: dict[str, str], limit: int) -> list[dict]:
    """P5：保证每个大类至少有 8 张强正例，若名额还够。"""
    out = OrderedDict((x["slide_id"], x) for x in picked)
    per_cat = Counter()
    for item in out.values():
        for d in item["strong_positives"]:
            per_cat[d2c.get(d, "unknown")] += 1

    for cat in sorted({d2c.get(d, "unknown") for d in d2c}):
        need = 8 - per_cat.get(cat, 0)
        if need <= 0 or len(out) >= limit:
            continue
        cands = sorted(sid for sid, v in idx.items()
                       if sid not in out and any(d2c.get(d) == cat for d in v["strong_positives"]))
        for sid in cands[:need]:
            if len(out) >= limit:
                break
            out[sid] = {
                "slide_id": sid,
                "reason": f"P5 大类补足：{cat} 强正例不足 8 个",
                "positives": sorted(idx[sid]["positives"]),
                "strong_positives": sorted(idx[sid]["strong_positives"]),
                "n_positives": len(idx[sid]["positives"]),
            }
            per_cat[cat] += 1
    return list(out.values())


# --------------------------------------------------------------------------- #
# 3. 图片下载
# --------------------------------------------------------------------------- #
def valid_png(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(8) == PNG_MAGIC and path.stat().st_size > 1024
    except OSError:
        return False


def download_image(sid: int, dest: Path, timeout: int = 60, retries: int = 2) -> dict:
    url = f"{RAW_BASE}/data/images/slide_{sid:04d}.png"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SlideAudit-stage4/1.0"})
            tmp = dest.with_suffix(".part")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data.startswith(PNG_MAGIC):
                raise ValueError(f"返回内容不是 PNG（{len(data)} bytes）")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return {"slide_id": sid, "ok": True, "bytes": len(data), "attempt": attempt}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return {"slide_id": sid, "ok": False, "error": last_err, "attempt": retries}


# --------------------------------------------------------------------------- #
# 4. 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="获取 SlideAudit 结构化数据与抽样图片")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--limit-images", type=int, default=60)
    ap.add_argument("--seeded-sample", action="store_true",
                    help="未使用；保留以说明抽样始终带固定种子")
    ap.add_argument("--max-mb", type=float, default=300.0, help="本次新增下载体积上限")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--skip-images", action="store_true")
    ap.add_argument("--force-images", action="store_true")
    args = ap.parse_args()

    limit = max(0, min(args.limit_images, 80))
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    repo = data_dir / "slideaudit"
    img_dir = data_dir / "images"
    img_dir.mkdir(exist_ok=True)

    started = time.time()
    before_bytes = dir_size_bytes(data_dir)

    # --- 结构化数据 ---
    repo_info = ensure_structured(repo)
    meta_rows = read_metadata(repo / "data" / "metadata.csv")
    ann_dir = repo / "data" / "annotations"
    desc_dir = repo / "data" / "descriptions"
    log(f"[meta] metadata 记录 {len(meta_rows)} 条")

    idx = build_label_index(ann_dir)
    log(f"[label] 可解析标注 {len(idx)} 页，"
        f"有正例 {sum(1 for v in idx.values() if v['positives'])} 页")

    # --- 抽样 ---
    picked, rules = select_images(idx, limit, args.seed)
    d2c = deficiency_to_category(ann_dir)
    picked = top_up_by_category(picked, idx, d2c, limit)
    log(f"[select] 选定 {len(picked)} 张，种子 {args.seed}")

    # --- 图片 ---
    results: list[dict] = []
    skipped_existing = 0
    if args.skip_images:
        log("[img] --skip-images：跳过图片下载")
    else:
        used = dir_size_bytes(data_dir)
        for i, item in enumerate(picked, 1):
            sid = item["slide_id"]
            dest = img_dir / f"slide_{sid:04d}.png"
            if dest.exists() and valid_png(dest) and not args.force_images:
                skipped_existing += 1
                results.append({"slide_id": sid, "ok": True, "bytes": dest.stat().st_size,
                                "skipped": True})
                continue
            if used > args.max_mb * 1024 * 1024:
                results.append({"slide_id": sid, "ok": False, "error": "超过体积上限，未下载"})
                continue
            res = download_image(sid, dest)
            results.append(res)
            if res["ok"]:
                used += res["bytes"]
            log(f"[img] {i:02d}/{len(picked)} slide_{sid:04d}.png "
                f"{'ok' if res['ok'] else 'FAIL'} "
                f"{res.get('bytes', 0) / 1024:.0f} KB {res.get('error', '')}")

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    img_bytes = sum(r.get("bytes", 0) for r in ok)
    if args.skip_images:
        img_bytes = dir_size_bytes(img_dir)

    # --- 清单 ---
    after_bytes = dir_size_bytes(data_dir)
    out = data_dir / "download_manifest.json"
    history: list[dict] = []
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            history = list(prev.get("runs", []))
            if not history and prev.get("generated_at"):
                history.append({
                    "at": prev["generated_at"],
                    "downloaded": prev.get("images", {}).get("downloaded", 0),
                    "skipped_existing": prev.get("images", {}).get("skipped_existing", 0),
                    "failed": prev.get("images", {}).get("failed_count", 0),
                })
        except Exception:
            history = []
    this_run = {
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "downloaded": len([r for r in ok if not r.get("skipped")]),
        "skipped_existing": skipped_existing,
        "failed": len(failed),
        "bytes": sum(r.get("bytes", 0) for r in ok if not r.get("skipped")),
    }
    history = (history + [this_run])[-5:]

    manifest = {
        "generated_at": this_run["at"],
        "runs": history,
        "repo": repo_info,
        "structured": {
            "metadata_rows": len(meta_rows),
            "metadata_csv": str(repo / "data" / "metadata.csv"),
            "annotations_present": len(list(ann_dir.glob("*.json"))),
            "descriptions_present": len(list(desc_dir.glob("*.json"))),
            "annotations_parsed": len(idx),
        },
        "images": {
            "requested": len(picked) if not args.skip_images else 0,
            "downloaded": len([r for r in ok if not r.get("skipped")]),
            "skipped_existing": skipped_existing,
            "failed": failed,
            "failed_count": len(failed),
            "bytes": img_bytes,
            "dir": str(img_dir),
        },
        "selection": {
            "seed": args.seed,
            "limit": limit,
            "rules": rules,
            "items": picked,
        },
        "budget": {
            "limit_mb": args.max_mb,
            "total_data_dir_mb": round(after_bytes / 1024 / 1024, 2),
            "delta_this_run_mb": round(max(0, after_bytes - before_bytes) / 1024 / 1024, 2),
        },
        "elapsed_sec": round(time.time() - started, 1),
    }
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log("")
    log("=" * 62)
    log(f"结构化：metadata {len(meta_rows)} 行，标注 {repo_info['checked_out']['annotations']} 份，"
        f"描述 {repo_info['checked_out']['descriptions']} 份")
    log(f"图片：成功 {len(ok)}（复用 {skipped_existing}），失败 {len(failed)}，"
        f"合计 {human_mb(img_bytes)}")
    log(f"data/ 总体积 {human_mb(after_bytes)}（本次新增 {human_mb(max(0, after_bytes - before_bytes))}）")
    log(f"清单：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

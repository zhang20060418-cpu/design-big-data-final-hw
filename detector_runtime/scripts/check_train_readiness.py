#!/usr/bin/env python3
"""4.3A: read-only data/environment checks; write only two readiness artifacts.

Run with the existing Python that already has Pillow:
    python scripts/check_train_readiness.py
    python scripts/check_train_readiness.py --offline

No package installation, image/model download, feature extraction, or training.
Online mode requests at most one small, pinned Git tree metadata response.
--offline reuses exact sizes from a previous, matching download plan when present.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
REPO = BASE / "data/slideaudit"
SPLIT = BASE / "reports/dataset_split.csv"
OUTPUT = BASE / "outputs"
REVISION = "642d490b7c1d2e78a50a631bfd359433397f3ecf"
IMAGE_TREE = "86c5f56654471758cc19060748af1694269d775b"
APPROVED_SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
LABELS = ("Improper Font Sizing", "Occluded Content", "Cluttered Layout", "Content Overflow/Cut-off")
SPLITS = ("train", "validation", "test", "excluded")
EXPECTED_COUNTS = dict(train=1512, validation=328, test=324, excluded=236)
LOW_POSITIVE_PAGES = 50
LOW_POSITIVE_GROUPS = 30
GIB = 1024 ** 3
PLAN_FIELDS = ("slide_id", "split", "target_image_path", "exists", "image_valid",
               "needs_download", "expected_size_bytes", "size_source",
               "expected_git_blob_sha", "source_commit", "download_url", "check_status")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, *, input_bytes=None, timeout=30, extra_env=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_NO_LAZY_FETCH="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", NO_COLOR="1")
    env.update(extra_env or {})
    return subprocess.run(command, input=input_bytes, capture_output=True,
                          timeout=timeout, env=env, cwd=str(BASE))


def git(*arguments: str, input_bytes=None) -> bytes:
    result = run(["git", "-C", str(REPO), *arguments], input_bytes=input_bytes)
    if result.returncode:
        raise RuntimeError("Local Git metadata check failed; no lazy fetch was allowed")
    return result.stdout


def load_fixed_data():
    if digest(SPLIT) != APPROVED_SPLIT_SHA256:
        raise ValueError("The accepted dataset_split.csv changed; refusing to regenerate or reinterpret it")
    with SPLIT.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with (REPO / "data/metadata.csv").open(encoding="utf-8-sig", newline="") as handle:
        metadata = {f"slide_{int(r['id']):04d}": r for r in csv.DictReader(handle)}
    ids = [r["slide_id"] for r in rows]
    if len(rows) != 2400 or len(set(ids)) != 2400 or set(ids) != set(metadata):
        raise ValueError("Split IDs are missing, duplicated, or inconsistent with metadata")
    if Counter(r["split"] for r in rows) != Counter(EXPECTED_COUNTS):
        raise ValueError("Accepted split counts do not match")
    groups = defaultdict(list)
    labels = {}
    for row in rows:
        sid, subset, gid = row["slide_id"], row["split"], row["group_id"]
        if (subset == "excluded") != (not gid):
            raise ValueError(f"Invalid trusted/excluded group_id: {sid}")
        if gid:
            groups[gid].append(row)
        path = REPO / f"data/annotations/{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        annotation_id = re.search(r"\d+", str(data.get("slide_id", "")))
        if not annotation_id or int(annotation_id.group()) != int(sid[6:]):
            raise ValueError(f"Annotation ID mismatch: {sid}")
        target_entries = defaultdict(list)
        for entry in data["annotations"]:
            if entry.get("design_deficiency") in LABELS:
                target_entries[entry["design_deficiency"]].append(entry.get("response"))
        values = []
        for label in LABELS:
            matches = target_entries[label]
            if len(matches) != 1 or type(matches[0]) is not bool:
                raise ValueError(f"Missing, duplicated, or non-boolean label: {sid} / {label}")
            values.append(matches[0])
        labels[sid] = values
        if not (REPO / f"data/descriptions/{sid}.json").is_file():
            raise ValueError(f"Missing description file: {sid}")
    for gid, members in groups.items():
        if len(members) != 4 or len({r["split"] for r in members}) != 1:
            raise ValueError(f"Non-atomic group in accepted split: {gid}")
        if {metadata[r["slide_id"]]["alteration"] for r in members} != {"nojitter", "texture", "layout", "alignment"}:
            raise ValueError(f"Augmentation coverage mismatch: {gid}")
    if len(groups) != 541:
        raise ValueError("Trusted group count mismatch")
    return rows, metadata, labels


def label_statistics(rows, labels):
    result = {}
    for subset in SPLITS:
        members = [r for r in rows if r["split"] == subset]
        n = len(members)
        targets = []
        for index, label in enumerate(LABELS):
            positive = [r for r in members if labels[r["slide_id"]][index] is True]
            ngroups = len({r["group_id"] for r in positive if r["group_id"]})
            flags = []
            if subset in ("validation", "test"):
                if len(positive) < LOW_POSITIVE_PAGES:
                    flags.append(f"正例页数 < {LOW_POSITIVE_PAGES}")
                if ngroups < LOW_POSITIVE_GROUPS:
                    flags.append(f"正例来源组 < {LOW_POSITIVE_GROUPS}")
            targets.append(dict(label=label, positive=len(positive), negative=n-len(positive),
                                rate=len(positive)/n, positive_groups=ngroups, risks=flags))
        any_positive = sum(any(labels[r["slide_id"]]) for r in members)
        result[subset] = dict(total=n, any_positive=any_positive, all_negative=n-any_positive,
                              labels=targets)
    return result


def pinned_image_objects():
    if git("rev-parse", "HEAD").decode().strip() != REVISION:
        raise ValueError("Upstream checkout differs from the pinned source commit")
    tree_line = git("ls-tree", "-d", REVISION, "--", "data/images").decode().split()
    if len(tree_line) < 3 or tree_line[2] != IMAGE_TREE:
        raise ValueError("Pinned image tree SHA mismatch")
    objects = {}
    for item in git("ls-tree", "-r", "-z", REVISION, "--", "data/images").split(b"\0"):
        if not item:
            continue
        fields, name = item.decode("utf-8").split("\t", 1)
        mode, kind, oid = fields.split()
        if kind == "blob" and name.endswith(".png"):
            objects[Path(name).stem] = oid
    if len(objects) != 2400:
        raise ValueError("Pinned tree does not contain the expected 2400 PNGs")
    # Object-size queries must never fetch missing promisor blobs.
    ordered = sorted(objects)
    payload = ("\n".join(objects[sid] for sid in ordered) + "\n").encode("ascii")
    sizes = {}
    lines = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                input_bytes=payload).decode().splitlines()
    if len(lines) != len(ordered):
        raise ValueError("Local object-size response is incomplete")
    for sid, line in zip(ordered, lines):
        parts = line.split()
        if len(parts) == 3 and parts[0] == objects[sid] and parts[1] == "blob":
            sizes[sid] = (int(parts[2]), "local_git_object")
    return objects, sizes


def github_token():
    # Credentials are used only in memory for api.github.com, never logged/saved.
    try:
        result = run(["git", "credential", "fill"],
                     input_bytes=b"protocol=https\nhost=github.com\n\n", timeout=10)
        if result.returncode == 0:
            values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
            if values.get("password"):
                return values["password"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    return None


def image_sizes(objects, local_sizes, offline):
    sizes = dict(local_sizes)
    notes = []
    cached = OUTPUT / "image_download_plan.csv"
    if cached.exists():
        with cached.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = row.get("slide_id")
                if (row.get("source_commit") == REVISION and objects.get(sid) == row.get("expected_git_blob_sha")
                        and row.get("size_source") in ("github_git_tree", "cached_pinned_tree", "local_git_object")
                        and row.get("expected_size_bytes", "").isdigit()):
                    sizes.setdefault(sid, (int(row["expected_size_bytes"]), "cached_pinned_tree"))
    if not offline:
        url = f"https://api.github.com/repos/zhuohaouw/SlideAudit/git/trees/{IMAGE_TREE}"
        headers = {"User-Agent": "SlideAudit-4.3A-Readiness", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = github_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                if not response.geturl().startswith("https://api.github.com/"):
                    raise ValueError("Unexpected tree metadata response host")
                body = response.read(8*1024*1024+1)
            if len(body) > 8*1024*1024:
                raise ValueError("Tree metadata response exceeded size limit")
            tree = json.loads(body)
            if tree.get("sha") != IMAGE_TREE or tree.get("truncated") is not False:
                raise ValueError("Tree metadata is mismatched or truncated")
            entries = {Path(e["path"]).stem: e for e in tree["tree"]
                       if e.get("type") == "blob" and e["path"].endswith(".png")}
            if set(entries) != set(objects):
                raise ValueError("Remote image names differ from the pinned local tree")
            for sid, entry in entries.items():
                if entry["sha"] != objects[sid] or type(entry.get("size")) is not int or entry["size"] < 1:
                    raise ValueError("Remote blob identity/size mismatch")
                sizes[sid] = (entry["size"], "github_git_tree")
            notes.append("完整、未截断的固定 Git 图片树返回了 2400 个 blob 的 size；只请求元数据，未获取 PNG 内容。")
        except urllib.error.HTTPError as exc:
            notes.append(f"GitHub 图片树元数据请求 HTTP {exc.code}；改用已有可靠大小或显式估算。")
        except (OSError, ValueError, TimeoutError) as exc:
            notes.append(f"GitHub 大小元数据未取得（{type(exc).__name__}）；使用本地/已有计划或估算。")
        finally:
            headers.clear()
            token = None
    else:
        notes.append("--offline：未请求网络；大小仅使用本地 Git 对象或此前匹配固定提交/blob 的计划记录。")
    return sizes, notes


def inspect_images(rows, metadata, objects, sizes):
    from PIL import Image
    local = {}
    for path in sorted((BASE / "data/images").glob("*.png")):
        sid = path.stem
        status, valid = "valid", False
        try:
            data = path.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                dimensions, fmt = image.size, image.format
            if sid not in metadata or sid not in objects:
                status = "unknown_slide_id"
            elif fmt != "PNG":
                status = "invalid_png_format"
            elif dimensions != (int(metadata[sid]["image_width"]), int(metadata[sid]["image_height"])):
                status = "dimension_mismatch"
            elif hashlib.sha1(f"blob {len(data)}\0".encode()+data).hexdigest() != objects[sid]:
                status = "pinned_git_blob_mismatch"
            else:
                valid = True
                sizes.setdefault(sid, (len(data), "verified_existing_blob"))
            local[sid] = dict(valid=valid, status=status, bytes=len(data))
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            local[sid] = dict(valid=False, status="unreadable_png", bytes=0)
    plan = []
    for row in rows:
        if row["split"] == "excluded":
            continue
        sid = row["slide_id"]
        if sid not in objects:
            raise ValueError(f"Formal image absent from pinned source tree: {sid}")
        existing = local.get(sid)
        size, size_source = sizes.get(sid, ("", "unknown"))
        if existing and existing["valid"] and type(size) is int and size != existing["bytes"]:
            raise ValueError(f"Pinned size metadata conflicts with a verified local blob: {sid}")
        plan.append(dict(slide_id=sid, split=row["split"], target_image_path=f"data/images/{sid}.png",
                         exists=int(existing is not None), image_valid=int(bool(existing and existing["valid"])),
                         needs_download=int(not existing or not existing["valid"]), expected_size_bytes=size,
                         size_source=size_source, expected_git_blob_sha=objects[sid], source_commit=REVISION,
                         download_url=f"https://raw.githubusercontent.com/zhuohaouw/SlideAudit/{REVISION}/data/images/{sid}.png",
                         check_status=existing["status"] if existing else "missing"))
    return plan, local


PROBE = r'''
import importlib, importlib.metadata, importlib.util, json, platform, sys
out = {"version": platform.python_version(), "executable": sys.executable, "packages": {}}
for module, dist in (("torch","torch"),("transformers","transformers"),("open_clip","open_clip_torch"),("sklearn","scikit-learn"),("numpy","numpy"),("PIL","Pillow")):
    entry = {"import_ok": False, "version": "not_installed"}
    try:
        if importlib.util.find_spec(module) is not None:
            imported = importlib.import_module(module)
            entry.update(import_ok=True, version=importlib.metadata.version(dist))
            if module == "torch":
                out["torch_cuda_build"] = imported.version.cuda
                out["torch_cuda_available"] = bool(imported.cuda.is_available())
                out["torch_gpu_names"] = [imported.cuda.get_device_name(i) for i in range(imported.cuda.device_count())] if out["torch_cuda_available"] else []
        else:
            entry["error"] = "module_missing"
    except Exception as exc:
        entry["error"] = type(exc).__name__ + ": " + str(exc)[:200]
    out["packages"][module] = entry
out.setdefault("torch_cuda_available", None)
if out["packages"]["transformers"]["import_ok"]:
    try:
        from transformers import CLIPModel, CLIPImageProcessor
        out["transformers_clip_import_ok"] = True
    except Exception as exc:
        out["transformers_clip_import_ok"] = False
        out["clip_import_error"] = type(exc).__name__ + ": " + str(exc)[:200]
print("READINESS_JSON=" + json.dumps(out))
'''


def environment_check():
    home = Path.home()
    candidates = [Path(sys.executable), BASE / ".venv/Scripts/python.exe",
                  home / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"]
    candidates += sorted((home / ".workbuddy/binaries/python/versions").glob("*/python.exe"))
    uv = shutil.which("uv") or str(home / ".local/bin/uv.exe")
    try:
        result = run([uv, "--offline", "python", "list", "--only-installed"], timeout=10)
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            match = re.search(r"([A-Za-z]:\\.*python[\d.]*\.exe)", line)
            if match:
                candidates.append(Path(match.group(1)))
    except (OSError, subprocess.TimeoutExpired):
        pass
    checked, paths = [], set()
    offline = dict(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    for candidate in candidates:
        if not candidate.is_file() or "WindowsApps" in str(candidate):
            continue
        resolved = str(candidate.resolve()).casefold()
        if resolved in paths:
            continue
        paths.add(resolved)
        try:
            result = run([str(candidate), "-B", "-c", PROBE], timeout=45, extra_env=offline)
            records = [line[15:] for line in result.stdout.decode("utf-8", "replace").splitlines() if line.startswith("READINESS_JSON=")]
            if not records:
                checked.append(dict(executable=str(candidate), version="probe_failed", packages={}, error="Import subprocess failed"))
                continue
            record = json.loads(records[-1])
            duplicate = False
            for previous in checked:
                try:
                    duplicate = duplicate or os.path.samefile(previous["executable"], record["executable"])
                except OSError:
                    duplicate = duplicate or previous["executable"].casefold() == record["executable"].casefold()
            if not duplicate:
                checked.append(record)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            checked.append(dict(executable=str(candidate), version="probe_failed", packages={}, error=type(exc).__name__))
    try:
        result = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.free", "--format=csv,noheader"], timeout=10)
        gpu = result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else "nvidia-smi unavailable"
    except (OSError, subprocess.TimeoutExpired):
        gpu = "nvidia-smi unavailable"
    cache_roots = [home / ".cache/huggingface/hub", home / ".cache/clip", home / ".cache/torch", BASE / "models"]
    for key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(key):
            cache_roots.append(Path(os.environ[key]))
    if os.environ.get("HF_HOME"):
        cache_roots.append(Path(os.environ["HF_HOME"]) / "hub")
    clip_weights = []
    for root in dict.fromkeys(cache_roots):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and "clip" in str(path).casefold() and path.suffix in (".bin", ".pt", ".safetensors"):
                    clip_weights.append(str(path))
    return checked, gpu, clip_weights


def md_table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] +
                     ["| " + " | ".join(str(cell).replace("|", "/").replace("\n", " ") for cell in row) + " |" for row in rows])


def make_report(rows, statistics, plan, local, environments, gpu, clip_weights, size_notes, offline):
    missing = [r for r in plan if r["needs_download"]]
    exact = all(type(r["expected_size_bytes"]) is int for r in missing)
    known_bytes = sum(r["expected_size_bytes"] for r in missing if type(r["expected_size_bytes"]) is int)
    formal_local = [local[r["slide_id"]]["bytes"] for r in plan if r["image_valid"]]
    estimate = known_bytes if exact else round(sum(formal_local)/len(formal_local)*len(missing)) if formal_local else None
    disk = shutil.disk_usage(BASE)
    c_disk = shutil.disk_usage(Path(sys.executable).anchor)
    feature_bytes = len(plan)*512*4
    image_budget = (estimate or 0) + GIB
    enough_disk = estimate is not None and disk.free >= image_budget + 10*GIB
    label_rows = []
    for index, label in enumerate(LABELS):
        label_rows.append([label] + [f"{statistics[s]['labels'][index]['positive']} / {statistics[s]['labels'][index]['negative']} / {statistics[s]['labels'][index]['rate']:.2%}" for s in SPLITS])
    page_rows = [[s, statistics[s]["total"], statistics[s]["any_positive"], statistics[s]["all_negative"]] for s in SPLITS]
    positive_group_rows = [[label] + [statistics[s]["labels"][j]["positive_groups"] for s in SPLITS[:3]] for j, label in enumerate(LABELS)]
    image_rows = []
    for subset in SPLITS[:3]:
        pp = [r for r in plan if r["split"] == subset]
        mm = [r for r in pp if r["needs_download"]]
        amount = sum(r["expected_size_bytes"] for r in mm) if all(type(r["expected_size_bytes"]) is int for r in mm) else "未知"
        image_rows.append([subset, len(pp), sum(r["image_valid"] for r in pp), len(mm), amount])
    image_rows.append(["合计", len(plan), len(plan)-len(missing), len(missing), known_bytes if exact else "仅估算"])
    env_rows = []
    for env in environments:
        packages = env["packages"]
        def state(module):
            p = packages.get(module, {})
            return p.get("version", "probe_failed") if p.get("import_ok") else "缺失/导入失败"
        cuda = env.get("torch_cuda_available")
        env_rows.append([env["version"], f'`{env["executable"]}`', state("torch"), state("transformers"), state("open_clip"), state("sklearn"), "未能验证" if cuda is None else str(cuda)])
    user_py312 = next((e["executable"] for e in environments if e["version"].startswith("3.12.") and "uv" in e["executable"].casefold()), "已安装的独立 Python 3.12")
    def clip_software_available(env):
        p = env["packages"]
        return (p.get("torch", {}).get("import_ok") and p.get("PIL", {}).get("import_ok")
                and (env.get("transformers_clip_import_ok") or p.get("open_clip", {}).get("import_ok")))
    software_ready = any(clip_software_available(e) for e in environments)
    combined_env_ready = any(clip_software_available(e) and e["packages"].get("sklearn", {}).get("import_ok") for e in environments)
    ready = software_ready and bool(clip_weights)
    blockers = []
    if missing:
        blockers.append(f"{len(missing):,} 张正式图片")
    if not combined_env_ready:
        blockers.append("同一独立环境中的 PyTorch、CLIP 图像库、Pillow 与 scikit-learn")
    if not clip_weights:
        blockers.append("冻结的 CLIP 权重与预处理配置")
    download_verdict = ("图片已齐备，无需新增下载" if not missing else
                        "计划和空间已具备条件，验收后可以启动；本次未下载" if enough_disk else
                        "磁盘不足或体积尚不能确认，需先解决空间/元数据问题")
    training_verdict = ("当前不能直接启动，还缺 " + "、".join(blockers) if blockers else
                        "数据与软件前置条件具备，仍须确认权重版本和小批量推理是否成功")
    train_rates = [item["rate"] for item in statistics["train"]["labels"]]
    small_page_combinations = sum(item["positive"] < LOW_POSITIVE_PAGES for s in ("validation", "test") for item in statistics[s]["labels"])
    low_group_labels = [label for j, label in enumerate(LABELS)
                        if any(statistics[s]["labels"][j]["positive_groups"] < LOW_POSITIVE_GROUPS for s in ("validation", "test"))]
    low_group_text = "、".join(low_group_labels) or "无"
    environment_blockers = ("缺少可完整导入依赖的独立环境" if not combined_env_ready else "已检测到依赖齐备的独立环境")
    environment_blockers += "；未检出 CLIP 权重" if not clip_weights else "；已检测到 CLIP 权重候选文件，尚未加载验证"
    excluded_local = sorted(r["slide_id"] for r in rows if r["split"] == "excluded" and r["slide_id"] in local)
    bad_local = sorted(sid for sid, item in local.items() if not item["valid"])
    input_hash = hashlib.sha256()
    for path in sorted((REPO / "data/annotations").glob("*.json")):
        input_hash.update(path.name.encode()+b"\0"+path.read_bytes()+b"\0")
    size_statement = (f"缺失 PNG 的 Git blob size 精确合计 **{known_bytes:,} 字节（{known_bytes/GIB:.3f} GiB）**，是压缩 PNG 文件本身的大小，不是解码内存或 NTFS 实占。" if exact else
                      f"缺失 PNG 大小未能全量确认。已知部分为 {known_bytes:,} 字节；按当前正式有效图片均值外推缺失图片约 {estimate:,} 字节（分层样本有选择偏差，非精确值、非上界）。" if estimate is not None else "无法可靠估算缺失图片体积，暂不能判断下载磁盘预算。")
    report = f'''# 第四阶段 4.3A：训练前检查

检查时间：{datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")}。只检查和生成计划，未安装依赖、未下载图片/模型、未提取特征、未训练。

## 1. 固定划分与四标签

固定使用已验收的 `reports/dataset_split.csv`：2,400 页、541 个可信四页组；train/validation/test = 1,512/328/324 页，excluded = 236 页。同组跨集合数 0。CSV 未重新分组或改写。

下表每格为 **正例 / 负例 / 正例比例**。只把严格布尔 `response=true` 计正例、`false` 计负例；每页四标签各恰好一条，缺失/重复/非布尔值均为 0。“有标注条目”不等于正例。

{md_table(['标签', 'train', 'validation', 'test', 'excluded'], label_rows)}

{md_table(['集合', '总目标图片数（页面数）', '至少一个目标正例', '四标签均负'], page_rows)}

**划分适合四标签基线实验，但样本支持有限。** 四标签在各正式集合均有正负例；训练正例率为 {min(train_rates):.2%}–{max(train_rates):.2%}，负例占主导。验证/测试共有 {small_page_combinations}/8 个“标签–集合”组合少于 50 个正例，标记“小样本评估风险”；验证或测试少于 30 个正例来源组的标签为：{low_group_text}，额外标记“有效独立来源较少”。50 页/30 组是本次预先声明的经验提醒阈值，不是训练资格判据，不能据此改已验收划分。

{md_table(['标签', 'train 正例组', 'validation 正例组', 'test 正例组'], positive_group_rows)}

同源增强不是独立原始页；后续报告分标签 P/R/F1、PR-AUC 与宏/微平均，保留全负页，采用按组重采样评估波动。不使用总体 accuracy 作为唯一结论，不为平衡类别重切分。

## 2. 确认的模型方案

**PNG → 冻结的预训练 CLIP 图像编码器 → 四个 scikit-learn 二分类逻辑回归。** 候选模型 `openai/clip-vit-base-patch32`；后续下载时冻结模型 revision 与预处理配置。使用对应 `CLIPImageProcessor` 的标准图像预处理，`eval()` + `torch.inference_mode()` 提取图像向量，不输入文本、不微调 CLIP；不使用图文相似度打分替代图像特征。

annotations 仅生成真实 y；标签名称、response、缺陷说明、bbox 均不拼入 X。descriptions 只检查文件存在，**不读取其答案性质内容作特征**；slide_id、split、source_type 和路径只用于索引/审计。图片向量可逐样本做固定 L2 归一化，避免引入全量数据拟合的预处理。

仅 train 拟合四个逻辑回归；`C`、是否 `class_weight='balanced'` 和每标签阈值只在 validation 上小范围选择，记录 0.5 阈值基线。test 最后只作一次正式评估，不用它调参、选标签或选模型。若以后增加标准化/PCA，必须只在 train 拟合。CLIP 的全局压缩表征可能丢失小字号、边缘裁切、细粒度遮挡，四类检测效果需要实测，不能先声称有效。

## 3. 本机环境实测

检查通过独立子进程实际 import，强制 Hugging Face/transformers 离线；未执行模型加载或依赖安装。

{md_table(['实际 Python', '解释器', 'PyTorch', 'transformers', 'open_clip', 'scikit-learn', 'torch.cuda.is_available()'], env_rows)}

- 运行本脚本的 Python：`{sys.version.split()[0]}`，`{sys.executable}`；目录名不能代替实际版本。Pillow 已在当前环境实际导入并用于完整解码检查。
- NVIDIA 实测：`{gpu}`（依次为名称、驱动、总显存、空闲显存）；这是硬件/驱动结果。PyTorch 未安装时 `torch.cuda.is_available()` 无法实测，不能把“有 NVIDIA 卡”写成“CUDA PyTorch 就绪”。
- 默认/环境指定 Hugging Face、CLIP、Torch 缓存及 `models/` 检出的 CLIP 权重文件：**{len(clip_weights)}**；CLIP 提取的软件/缓存前置条件：**{'具备候选条件，尚未做推理实测' if ready else '未具备'}**。
- 环境结论：{environment_blockers}；**没有实测证据说明 Python 3.13 必然不兼容**。最小隔离方案：若后续仍需准备环境，验收后用已安装的 `{user_py312}` 单独创建 `detector_runtime/.venv`，按官方 Windows 安装选择器安装兼容 PyTorch，加 transformers、scikit-learn 及其必要依赖；不改前三阶段解释器。复查也会检查该 `.venv`。4 GiB 显存宜从小批量推理开始，安装后必须验证 CUDA 可用性；CPU 可作备用。
- 磁盘：项目所在 {BASE.anchor} 可用 **{disk.free:,} 字节（{disk.free/GIB:.2f} GiB）**；当前解释器所在 {Path(sys.executable).anchor} 可用 **{c_disk.free/GIB:.2f} GiB**。数据与后续环境/权重缓存优先放 D 盘。

## 4. 图片下载计划

{md_table(['集合', '正式目标', '已有有效 PNG', '需要下载', '缺失 PNG 字节'], image_rows)}

本地 `data/images/` 共有 {len(local)} 张 PNG；检查包括 PNG 结构验证、完整解码、metadata 尺寸、固定提交 Git blob SHA-1。无效文件：{', '.join(bad_local) if bad_local else '0'}。其中 {len(excluded_local)} 张属 excluded：{', '.join(excluded_local)}，不进入正式清单。上一步画廊内嵌图片不属于本地 `data/images/` 文件，不将其误计为已就绪训练图片。

{size_statement}

大小来源：{' '.join(size_notes)} 本地只读 Git 查询强制 `GIT_NO_LAZY_FETCH=1`，不会为获取 size 自动拉取图片 blob。全部下载 URL 固定到 `{REVISION}`，不依赖 main。CSV 收录全部 2,164 个正式目标，筛选 `needs_download=1` 即得到增量缺失清单；excluded 没有任何行。目标路径相对 `detector_runtime/`，`exists=1` 仍须 `image_valid=1` 才跳过。

按 PNG 缺失体积再预留 1 GiB 下载/临时空间，并另预留 10 GiB 环境与模型缓存预算，磁盘判定：**{'足够' if enough_disk else '不足或尚不能确认'}**。后两项是保守规划预算，不是已下载体积；预计 512 维 float32 图像特征缓存仅 {feature_bytes:,} 字节（{feature_bytes/1024**2:.2f} MiB），实际维度以冻结模型配置为准。

**后续增量下载方案（本轮不执行）**：按清单每批 64–128 张、并发 2，已通过相同校验的文件跳过；先写 `.part`，仅当服务端返回匹配偏移的 206/Content-Range 时续传，若忽略 Range 返回 200 则重启该文件，不把完整响应追加到旧片段。429/5xx 最多重试 3 次、指数退避并遵守 Retry-After。完成后核对固定 blob size、Git blob SHA、PNG 解码与尺寸，再原子重命名。失败保留片段并记录失败项；重跑本检查脚本更新 needs_download，支持文件级断点恢复。本脚本没有下载/安装/训练开关。

## 5. 是否进入 4.3B

**图片下载：{download_verdict}。正式特征提取/分类器训练：{training_verdict}。** 开始训练前还需后续一张图/小批量推理验证及用户验收。当前四标签划分本身可用，不需要重分组或重切分。环境与图片齐备后，才进入 train 拟合、validation 选择、test 一次评估。

检查脚本可从任意目录重复执行：

```powershell
& "{sys.executable}" "{Path(__file__).resolve()}"
# 仅使用已记录的固定大小元数据，不请求网络：在同一命令末尾加 --offline
```

所有一致性检查通过：2,400 唯一 ID、541 原子组、四标签严格布尔、2,164 正式计划行、excluded 行数 0、固定 URL/blob 对应、原 split SHA-256 前后不变。脚本成功表示检查已完成，不表示训练环境自动就绪。

复现锚点：split SHA-256 `{APPROVED_SPLIT_SHA256}`；metadata SHA-256 `{digest(REPO/'data/metadata.csv')}`；全部 annotations 摘要 `{input_hash.hexdigest()}`（文件名排序，每项追加文件名、NUL、原始字节、NUL，再 SHA-256）；上游 `{REVISION}`；图片树 `{IMAGE_TREE}`。本次 {'离线' if offline else '在线仅元数据'} 检查。

官方依据：[CLIP 图像特征接口](https://huggingface.co/docs/transformers/model_doc/clip)、[逻辑回归与 class_weight](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html)、[PyTorch Windows 安装与 CUDA 验证](https://pytorch.org/get-started/locally/)、[GitHub 固定 Git 树的 blob size](https://docs.github.com/en/rest/git/trees?apiVersion=2022-11-28#get-a-tree)。安装版本与 CUDA 构建将在后续环境准备时按官方当时提供的兼容组合冻结，不在本轮安装。
'''
    return report, dict(missing_images=len(missing), existing_formal_images=len(plan)-len(missing),
                        exact_missing_bytes=known_bytes if exact else None, estimate_missing_bytes=estimate,
                        free_project_bytes=disk.free, disk_enough=enough_disk, clip_ready=ready,
                        label_statistics=statistics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="No network; reuse recorded pinned size metadata if available")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if importlib.util.find_spec("PIL") is None:
        raise SystemExit("Use the existing Python with Pillow; this script does not install dependencies")
    initial_split_hash = digest(SPLIT)
    rows, metadata, labels = load_fixed_data()
    stats = label_statistics(rows, labels)
    objects, local_sizes = pinned_image_objects()
    sizes, size_notes = image_sizes(objects, local_sizes, args.offline)
    plan, local = inspect_images(rows, metadata, objects, sizes)
    environments, gpu, weights = environment_check()
    report, summary = make_report(rows, stats, plan, local, environments, gpu, weights, size_notes, args.offline)
    if len(plan) != 2164 or len({r['slide_id'] for r in plan}) != 2164 or any(r['split']=='excluded' for r in plan):
        raise ValueError("Invalid download plan scope")
    if digest(SPLIT) != initial_split_hash or initial_split_hash != APPROVED_SPLIT_SHA256:
        raise ValueError("Accepted split changed during inspection")
    OUTPUT.mkdir(exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=PLAN_FIELDS, lineterminator="\n")
    writer.writeheader(); writer.writerows(plan)
    (OUTPUT / "image_download_plan.csv").write_text(buffer.getvalue(), encoding="utf-8", newline="")
    (OUTPUT / "train_readiness.md").write_text(report, encoding="utf-8", newline="")
    summary.update(plan_rows=len(plan), split_sha256=digest(SPLIT), source_revision=REVISION,
                   report=str(OUTPUT / "train_readiness.md"), plan=str(OUTPUT / "image_download_plan.csv"))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

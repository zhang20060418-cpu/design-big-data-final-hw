#!/usr/bin/env python3
"""4.3B: download only the accepted formal PNGs, without changing the plan/split.

Run with ml_stage4/.venv/Scripts/python.exe. Existing and downloaded files must
match the pinned Git blob, exact byte count, PNG decoding and metadata dimensions.
No package/model downloads, feature extraction, training, or Git lazy fetching.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import email.utils
import hashlib
import http.client
import io
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PLAN = BASE / "outputs/image_download_plan.csv"
SPLIT = BASE / "reports/dataset_split.csv"
METADATA = BASE / "data/slideaudit/data/metadata.csv"
IMAGES = BASE / "data/images"
REPORT = BASE / "outputs/image_download_report.md"
REVISION = "642d490b7c1d2e78a50a631bfd359433397f3ecf"
SPLIT_SHA = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
EXPECTED_COUNTS = {"train": 1512, "validation": 328, "test": 324}
CHUNK = 128 * 1024
MAX_ATTEMPTS = 3
MAX_FILE_BYTES = 64 * 1024 * 1024
TZ = timezone(timedelta(hours=8))
CONNECTIONS = threading.local()


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_image_path(sid):
    if not re.fullmatch(r"slide_\d{4}", sid):
        raise ValueError("Invalid slide identifier")
    if not IMAGES.resolve().is_relative_to(BASE.resolve()):
        raise ValueError("Image directory resolves outside ml_stage4")
    path = IMAGES / f"{sid}.png"
    if path.resolve() != path.absolute() or path.parent.resolve() != IMAGES.resolve():
        raise ValueError("Image target is redirected or outside the image directory")
    part = path.with_suffix(".png.part")
    if part.resolve() != part.absolute():
        raise ValueError("Partial file is redirected")
    return path, part


def load_inputs():
    if file_sha(SPLIT) != SPLIT_SHA:
        raise ValueError("Accepted split SHA-256 changed; download refused")
    plan_sha, metadata_sha = file_sha(PLAN), file_sha(METADATA)
    with SPLIT.open(encoding="utf-8-sig", newline="") as handle:
        split_rows = list(csv.DictReader(handle))
    with PLAN.open(encoding="utf-8-sig", newline="") as handle:
        plan_rows = list(csv.DictReader(handle))
    with METADATA.open(encoding="utf-8-sig", newline="") as handle:
        metadata = {f"slide_{int(r['id']):04d}": r for r in csv.DictReader(handle)}
    if len(split_rows) != 2400 or len({r["slide_id"] for r in split_rows}) != 2400:
        raise ValueError("Accepted split IDs are incomplete or duplicated")
    formal = {r["slide_id"]: r for r in split_rows if r["split"] in EXPECTED_COUNTS}
    if len(formal) != 2164 or Counter(r["split"] for r in formal.values()) != Counter(EXPECTED_COUNTS):
        raise ValueError("Formal split counts differ from the accepted scope")
    if len(plan_rows) != 2164 or len({r["slide_id"] for r in plan_rows}) != 2164:
        raise ValueError("Download plan IDs are incomplete or duplicated")
    if {r["slide_id"] for r in plan_rows} != set(formal):
        raise ValueError("Download plan includes excluded/unknown IDs or misses formal IDs")
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    output = subprocess.run(["git", "-C", str(BASE / "data/slideaudit"), "ls-tree", "-r", "-z", REVISION, "--", "data/images"],
                            env=env, capture_output=True, check=True, timeout=30).stdout
    objects = {}
    for item in output.split(b"\0"):
        if item:
            fields, name = item.decode("utf-8").split("\t", 1)
            mode, kind, oid = fields.split()
            if kind == "blob" and name.endswith(".png"):
                objects[Path(name).stem] = oid
    if len(objects) != 2400:
        raise ValueError("Pinned image tree is incomplete")
    items = []
    for row in plan_rows:
        sid = row["slide_id"]
        url = f"https://raw.githubusercontent.com/zhuohaouw/SlideAudit/{REVISION}/data/images/{sid}.png"
        if row["split"] != formal[sid]["split"] or not formal[sid]["group_id"]:
            raise ValueError(f"Plan split/group mismatch: {sid}")
        if row["target_image_path"] != f"data/images/{sid}.png" or row["download_url"] != url:
            raise ValueError(f"Unapproved download URL or target path: {sid}")
        if row["source_commit"] != REVISION or row["expected_git_blob_sha"] != objects[sid]:
            raise ValueError(f"Plan is not bound to the pinned Git blob: {sid}")
        if not row["expected_size_bytes"].isdigit() or not 0 < int(row["expected_size_bytes"]) <= MAX_FILE_BYTES:
            raise ValueError(f"Missing or unreasonable exact PNG size: {sid}")
        if row["size_source"] not in {"github_git_tree", "cached_pinned_tree", "local_git_object", "verified_existing_blob"}:
            raise ValueError(f"PNG size is not from an exact source: {sid}")
        target, part = safe_image_path(sid)
        items.append(dict(sid=sid, split=row["split"], url=url, target=target, part=part,
                          size=int(row["expected_size_bytes"]), blob=objects[sid],
                          dimensions=(int(metadata[sid]["image_width"]), int(metadata[sid]["image_height"]))))
    baseline_valid = {r["slide_id"] for r in plan_rows if r["image_valid"] == "1"}
    return items, plan_sha, metadata_sha, formal, metadata, objects, baseline_valid


def validate_png(path, item):
    from PIL import Image
    if not path.is_file():
        return False, "missing"
    try:
        if path.stat().st_size != item["size"]:
            return False, "byte_count_mismatch"
        with path.open("rb") as handle:
            data = handle.read(item["size"] + 1)
        if len(data) != item["size"]:
            return False, "byte_count_mismatch"
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return False, "invalid_png_signature"
        if hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() != item["blob"]:
            return False, "pinned_git_blob_mismatch"
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.size != item["dimensions"]:
                return False, "format_or_dimension_mismatch"
        return True, "valid"
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False, "unreadable_png"


def promote(item):
    # A valid destination always wins over a concurrent/previous partial download.
    if validate_png(item["target"], item)[0]:
        return "already_valid"
    valid, reason = validate_png(item["part"], item)
    if not valid:
        raise ValueError("Invalid completed partial PNG: " + reason)
    safe_image_path(item["sid"])
    os.replace(item["part"], item["target"])
    return "downloaded"


def retry_delay(headers, attempt):
    value = headers.get("Retry-After") if headers else None
    delay = 2 ** attempt
    if value:
        try:
            retry = float(value)
        except ValueError:
            try:
                retry = max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                retry = 0.0
        if retry > 60:
            return None  # Stop rather than violating a long server cooldown.
        delay = max(delay, retry)
    return min(60.0, delay)


class PersistentResponse:
    def __init__(self, response, url, connection):
        self.response, self.url, self.connection = response, url, connection
        self.status, self.headers = response.status, response.headers

    def geturl(self):
        return self.url

    def read(self, amount):
        return self.response.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.response.close()
        if exc_type is not None or self.response.will_close:
            self.connection.close()
            CONNECTIONS.connection = None


def open_png_response(url, headers):
    """Keep each worker's TLS tunnel alive; no cookies, redirects, or new dependencies."""
    connection = getattr(CONNECTIONS, "connection", None)
    proxy = urllib.request.getproxies().get("https")
    route = getattr(CONNECTIONS, "route", "direct")
    try:
        if connection is None:
            if proxy and route == "proxy":
                parsed_proxy = urllib.parse.urlsplit(proxy)
                if parsed_proxy.scheme != "http" or parsed_proxy.username or parsed_proxy.password:
                    raise ValueError("Only an unauthenticated HTTP CONNECT proxy is supported")
                connection = http.client.HTTPSConnection(parsed_proxy.hostname, parsed_proxy.port or 80,
                                                        timeout=30, context=ssl.create_default_context())
                connection.set_tunnel("raw.githubusercontent.com", 443)
            else:
                connection = http.client.HTTPSConnection("raw.githubusercontent.com", 443,
                                                        timeout=30, context=ssl.create_default_context())
            CONNECTIONS.connection = connection
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or parsed.query or parsed.fragment:
            raise ValueError("Unapproved raw image request")
        connection.request("GET", parsed.path, headers={**headers, "Connection": "keep-alive"})
        response = connection.getresponse()
        if response.status not in (200, 206):
            error = urllib.error.HTTPError(url, response.status, response.reason, response.headers, None)
            response.close()
            connection.close()
            CONNECTIONS.connection = None
            raise error
        return PersistentResponse(response, url, connection)
    except Exception:
        if connection is not None:
            connection.close()
        CONNECTIONS.connection = None
        if proxy and route == "direct":
            CONNECTIONS.route = "proxy"
        raise


def download_one(item):
    received, attempts, resumed = 0, 0, False
    if validate_png(item["target"], item)[0]:
        return dict(sid=item["sid"], status="already_valid", bytes=0, attempts=0, resumed=False, error="")
    last_error = "not_started"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        headers = None
        retryable = True
        try:
            safe_image_path(item["sid"])
            offset = item["part"].stat().st_size if item["part"].is_file() else 0
            if offset == item["size"]:
                if validate_png(item["part"], item)[0]:
                    return dict(sid=item["sid"], status=promote(item), bytes=received, attempts=attempt-1, resumed=True, error="")
                offset = 0
            if offset > item["size"]:
                offset = 0
            if offset == 0 and item["part"].exists():
                item["part"].write_bytes(b"")
            request_headers = {"User-Agent": "SlideAudit-4.3B-Pinned-PNG-Downloader", "Accept-Encoding": "identity"}
            if offset:
                request_headers["Range"] = f"bytes={offset}-"
            deadline = time.monotonic() + 120
            with open_png_response(item["url"], request_headers) as response:
                headers = response.headers
                if response.geturl() != item["url"]:
                    raise ValueError("Unexpected download redirect")
                if headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Unexpected encoded PNG response")
                if response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups())) != (offset, item["size"]-1, item["size"]):
                        raise ValueError("Unexpected Content-Range; partial response not appended")
                    mode, base = ("ab" if offset else "wb"), offset
                    resumed = resumed or bool(offset)
                elif response.status == 200:
                    # Range was ignored: restart, never append the full payload.
                    mode, base = "wb", 0
                else:
                    raise ValueError(f"Unexpected HTTP status {response.status}")
                length = headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != item["size"]-base):
                    raise ValueError("Response Content-Length differs from pinned byte count")
                count = base
                with item["part"].open(mode) as handle:
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError("PNG transfer exceeded finite attempt deadline")
                        chunk = response.read(min(CHUNK, item["size"]-count+1))
                        if not chunk:
                            break
                        received += len(chunk)
                        if count + len(chunk) > item["size"]:
                            raise ValueError("Response exceeded pinned PNG byte count")
                        handle.write(chunk)
                        count += len(chunk)
                if count != item["size"]:
                    raise ValueError("PNG response ended before the pinned byte count")
            valid, reason = validate_png(item["part"], item)
            if not valid:
                item["part"].write_bytes(b"")  # A corrupt completed payload cannot be resumed safely.
                raise ValueError("Downloaded PNG validation failed: " + reason)
            return dict(sid=item["sid"], status=promote(item), bytes=received, attempts=attempt, resumed=resumed, error="")
        except urllib.error.HTTPError as exc:
            headers = exc.headers
            last_error = f"HTTP {exc.code}"
            retryable = exc.code in {408, 416, 429, 500, 502, 503, 504}
            if exc.code == 416 and item["part"].is_file():
                item["part"].write_bytes(b"")
        except (OSError, ValueError, TimeoutError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__ + ": " + str(exc)[:220]
        if not retryable or attempt == MAX_ATTEMPTS:
            break
        delay = retry_delay(headers, attempt)
        if delay is None:
            last_error += "; server Retry-After >60s, deferred instead of retrying early"
            break
        time.sleep(delay)
    return dict(sid=item["sid"], status="failed", bytes=received, attempts=attempts, resumed=resumed, error=last_error)


def write_report(items, initial_valid, initial_invalid, results, started, workers, batch_size,
                 plan_sha, metadata_sha, original_names, baseline_valid, final=None, original_unchanged=None):
    counts = Counter(r["status"] for r in results)
    success = {r["sid"] for r in results if r["status"] != "failed"}
    current_valid = set(initial_valid) | success
    final_valid = final if final is not None else current_valid
    failed = [r for r in results if r["status"] == "failed"]
    transfer = sum(r["bytes"] for r in results)
    source_bytes = sum(item["size"] for item in items if item["sid"] in success-set(initial_valid))
    cumulative_new = final_valid - baseline_valid
    cumulative_bytes = sum(item["size"] for item in items if item["sid"] in cumulative_new)
    state = "完成（全部正式图片通过校验）" if final is not None and len(final_valid) == len(items) else "完成但仍有失败项，可重跑恢复" if final is not None else "下载进行中"
    split_rows = []
    for subset in EXPECTED_COUNTS:
        ii = [i for i in items if i["split"] == subset]
        nvalid = sum(i["sid"] in final_valid for i in ii)
        split_rows.append(f"| {subset} | {len(ii)} | {sum(i['sid'] in initial_valid for i in ii)} | {nvalid} | {len(ii)-nvalid} |")
    failures = "\n".join(f"| {r['sid']} | {r['attempts']} | {r['error'].replace('|','/').replace(chr(10),' ')} |" for r in failed) or "| — | — | 无 |"
    REPORT.write_text(f'''# 第四阶段 4.3B：正式图片增量下载记录

状态：**{state}**。开始：{started}；更新：{datetime.now(TZ).isoformat(timespec="seconds")}。

仅处理已验收清单中的 2,164 个正式目标。以 `ml_stage4/.venv/Scripts/python.exe` 执行，不重新分组、不修改 split 或下载计划、不下载 excluded、不提取特征或训练。

| 项目 | 数量 |
| --- | ---: |
| 原下载计划冻结时正式已有有效 PNG | {len(baseline_valid)} |
| 本次进程开始时 data/images 已有 PNG | {len(original_names)} |
| 本次进程开始时正式集合有效 PNG | {len(initial_valid)} |
| 开始时正式目标无效 PNG | {len(initial_invalid)} |
| 本次新完成有效 PNG | {len(success-set(initial_valid))} |
| 本阶段累计新增有效 PNG（含此前续跑成果） | {len(cumulative_new)} |
| 已处理但失败 | {len(failed)} |
| 当前正式有效 PNG | {len(final_valid)} |
| 正式集合仍缺/无效 | {len(items)-len(final_valid)} |
| 本次进程 HTTP 正文接收字节（含失败重试） | {transfer:,} |
| 本次新完成 PNG 的固定文件字节 | {source_bytes:,} |
| 本阶段累计新增 PNG 的固定文件字节 | {cumulative_bytes:,} |

| 集合 | 正式目标 | 开始时有效 | 当前有效 | 仍缺/无效 |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(split_rows)}

每批 {batch_size} 张，并发 {workers}，每文件最多 {MAX_ATTEMPTS} 次尝试；网络单次等待 30 秒、每次传输约 120 秒上限（阻塞读取最多再等待一次超时），指数退避；遵守最长 60 秒的 Retry-After，超过该等待期则记录延后处理，不提前重试。每线程复用持久 HTTPS 连接；优先直连，连接失败后在后续有限尝试中使用本机 HTTPS 代理；出错断开后重连，避免逐张重新握手。输出真实批次进度。

文件先写同目录 `.png.part`。只有 206 且 Content-Range 起点、终点、总长均匹配才追加；服务端返回 200 则从头覆盖片段。每次流量受固定字节数限制。完整片段须通过字节数、PNG 结构验证、完整解码、metadata 尺寸、固定 Git blob SHA-1，再原子移入目标。已有有效文件跳过；最终替换前再次检查目标，避免覆盖有效文件。失败片段保留供恢复；已确认完整但校验失败的内容清空后重新获取。

使用固定源提交 `{REVISION}` 和清单中的逐文件 blob SHA。Git 只读树查询始终设置 `GIT_NO_LAZY_FETCH=1`，没有借 Git 自动拉取图片。没有调用收费 API。

- 下载计划 SHA-256：`{plan_sha}`；当前一致：**{file_sha(PLAN) == plan_sha}**。
- 已验收 split SHA-256：`{SPLIT_SHA}`；当前一致：**{file_sha(SPLIT) == SPLIT_SHA}**。
- metadata SHA-256：`{metadata_sha}`；当前一致：**{file_sha(METADATA) == metadata_sha}**。
- 本轮下载目标含 excluded 或清单外路径：**0**（写入前逐行校验）。
- 初始已有有效 PNG 字节未变（原本无效的正式目标允许修复）：**{original_unchanged if original_unchanged is not None else '最终核验待完成'}**。
- 最终逐张重新校验：**{'已完成' if final is not None else '待完成'}**。

失败清单：

| slide_id | 尝试次数 | 原因 |
| --- | ---: | --- |
{failures}

恢复方式：使用同一解释器重跑 `scripts/download_training_images.py`；脚本读取原清单，但实际重新核验文件，不依赖其中旧 exists/needs_download 标记。已通过校验的图片会跳过。累计新增数与文件字节以原计划冻结时的有效样本为基线，包含此前进程落盘的成果；HTTP 接收字节和失败项只描述当前进程，不冒充全部进程的网络流量。stdout 保留批次进度；未改写原计划。
''', encoding="utf-8", newline="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(2, 4, 8), default=4)
    parser.add_argument("--batch-size", type=int, choices=(64, 128), default=128)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    expected_python = BASE / ".venv/Scripts/python.exe"
    if not expected_python.is_file() or not os.path.samefile(sys.executable, expected_python):
        raise SystemExit("Run this downloader only with ml_stage4/.venv/Scripts/python.exe")
    from PIL import Image  # Verify the project environment, never install implicitly.
    started = datetime.now(TZ).isoformat(timespec="seconds")
    items, plan_sha, metadata_sha, formal, metadata, objects, baseline_valid = load_inputs()
    original_names = {p.name: file_sha(p) for p in IMAGES.glob("*.png")}
    # Audit excluded existing PNGs too, but never place them in the download queue.
    excluded_existing_invalid = []
    for path in IMAGES.glob("*.png"):
        if path.stem in formal:
            continue
        sid = path.stem
        if sid not in metadata or sid not in objects:
            raise ValueError("Unknown existing PNG outside the pinned source: " + path.name)
        safe_image_path(sid)
        auxiliary = dict(size=path.stat().st_size, blob=objects[sid],
                         dimensions=(int(metadata[sid]["image_width"]), int(metadata[sid]["image_height"])))
        if not validate_png(path, auxiliary)[0]:
            excluded_existing_invalid.append(sid)
    if excluded_existing_invalid:
        raise ValueError("Invalid excluded existing PNGs left untouched: " + ", ".join(excluded_existing_invalid))
    initial_valid, initial_invalid = set(), {}
    pending = []
    for item in items:
        valid, reason = validate_png(item["target"], item)
        if valid:
            initial_valid.add(item["sid"])
        else:
            pending.append(item)
            if item["target"].exists():
                initial_invalid[item["sid"]] = reason
    print(json.dumps(dict(event="start", formal=len(items), existing_valid=len(initial_valid),
                          needs_download=len(pending), workers=args.workers, batch_size=args.batch_size), ensure_ascii=False), flush=True)
    results = []
    report_args = (items, initial_valid, initial_invalid, results, started, args.workers, args.batch_size,
                   plan_sha, metadata_sha, original_names, baseline_valid)
    write_report(*report_args)
    for start in range(0, len(pending), args.batch_size):
        if file_sha(PLAN) != plan_sha or file_sha(SPLIT) != SPLIT_SHA or file_sha(METADATA) != metadata_sha:
            raise ValueError("Immutable input changed during download")
        batch = pending[start:start+args.batch_size]
        batch_number = start // args.batch_size + 1
        before = len(results)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(download_one, item): item for item in batch}
            active = set(futures)
            last_update = time.monotonic()
            while active:
                done, active = concurrent.futures.wait(active, timeout=15, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    results.append(result)
                    if result["status"] == "failed":
                        print(json.dumps(dict(event="file_failed", **result), ensure_ascii=False), flush=True)
                if time.monotonic()-last_update >= 15 or not active:
                    print(json.dumps(dict(event="progress", batch=batch_number,
                                          processed=len(results), pending_total=len(pending),
                                          completed=sum(r["status"] != "failed" for r in results),
                                          failed=sum(r["status"] == "failed" for r in results),
                                          http_received_bytes=sum(r["bytes"] for r in results)), ensure_ascii=False), flush=True)
                    last_update = time.monotonic()
        write_report(*report_args)
        print(json.dumps(dict(event="batch_complete", batch=batch_number, files=len(results)-before,
                              processed=len(results), pending_total=len(pending)), ensure_ascii=False), flush=True)
    final = {item["sid"] for item in items if validate_png(item["target"], item)[0]}
    unchanged = all((IMAGES/name).is_file() and file_sha(IMAGES/name) == checksum
                    for name, checksum in original_names.items() if Path(name).stem not in initial_invalid)
    if set(p.name for p in IMAGES.glob("*.png")) - set(original_names) - {f"{item['sid']}.png" for item in items}:
        raise ValueError("Unexpected new image outside the formal plan")
    if not unchanged:
        raise ValueError("An initial PNG changed; inspection required")
    if file_sha(PLAN) != plan_sha or file_sha(SPLIT) != SPLIT_SHA or file_sha(METADATA) != metadata_sha:
        raise ValueError("Immutable input changed during download")
    write_report(*report_args, final=final, original_unchanged=unchanged)
    summary = dict(event="finished", formal=len(items), initial_valid=len(initial_valid),
                   new_valid=len(final-initial_valid), valid_now=len(final), remaining=len(items)-len(final),
                   stage_baseline_valid=len(baseline_valid), cumulative_new_valid=len(final-baseline_valid),
                   failed=sum(r["status"] == "failed" for r in results),
                   http_received_bytes=sum(r["bytes"] for r in results),
                   plan_unchanged=True, split_unchanged=True, existing_pngs_unchanged=unchanged)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if len(final) == len(items) else 2


if __name__ == "__main__":
    raise SystemExit(main())

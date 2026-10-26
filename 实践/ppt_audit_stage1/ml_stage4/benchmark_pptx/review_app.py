"""Offline human review server. CSV labels are authoritative; no model inference.

Only explicit user actions change labels. The companion journal stores action
provenance and inspected-unknown reasons, not an alternative label database.
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import struct
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs
import webbrowser


LABELS = ("improper_font_sizing", "occluded_content", "cluttered_layout", "content_overflow_cutoff")
LABEL_NAMES = dict(zip(LABELS, ("字号不合理", "内容遮挡", "布局拥挤", "内容溢出或裁切")))
FONT_OK = {"confirmed_no_visible_issue", "confirmed_substitution_accepted"}
RENDER_VALUES = {"pending_human_review", "confirmed", "needs_attention"}
FONT_VALUES = {"pending_human_review", *FONT_OK, "needs_attention"}
EDITABLE = {*LABELS, "review_notes", "render_review_status", "font_review_status",
            "annotation_status", "human_reviewed", "reviewer_id", "reviewed_at"}


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path, data):
    """Replace a complete file on the same volume, never truncate its target."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class ReviewError(Exception):
    def __init__(self, message, code="invalid_request", status=422, **detail):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


class ReviewStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = self.root / "benchmark_manifest.csv"
        self.plan_path = self.root / "source_plan.json"
        self.plan_raw = self.plan_path.read_bytes()
        self.plan_sha256 = digest(self.plan_raw)
        self.plan = json.loads(self.plan_raw.decode("utf-8-sig"))
        self.history_path = self.root / "review_history.json"
        self.wal_path = self.root / "review_transaction.json"
        self.backup = self.root / "review_backup" / "benchmark_manifest.before_review.csv"
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.health_cache = {}
        self.warning = ""
        self._recover()
        self.raw = self.manifest.read_bytes()
        self.revision = digest(self.raw)
        self.fields, self.rows, self.csv_bom, self.newline = self._parse(self.raw)
        self.metadata = self._read_history()
        self.revision = self._token(self.raw, self.metadata)
        self._validate_rows()
        self._helper = None

    @property
    def frozen(self):
        return (self.root / "freeze_record.json").exists() or (self.root / "frozen_labels.csv").exists()

    @staticmethod
    def _token(raw, metadata):
        actions = metadata.get("actions", [])
        tail = actions[-1].get("id", "") if actions else ""
        return digest(raw + b"\0" + str(tail).encode("utf-8"))

    def _read_history(self):
        if not self.history_path.exists():
            return {"schema_version": 1, "manifest_sha256": self.revision,
                    "progress": {}, "actions": [], "session": {"reviewer_id": "", "last_sample_id": ""}}
        value = json.loads(self.history_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1:
            raise ValueError("标注历史版本不支持，请保留文件并联系维护者。")
        if value.get("manifest_sha256") != digest(self.raw):
            self.warning = "CSV 在服务外被修改，原审核动作不再自动认定有效。请显式重新载入并核对。"
            self.external_conflict = True
        return value

    def _recover(self):
        """Recover at most one interrupted action, conditional on exact hashes."""
        if not self.wal_path.exists():
            return
        if self.frozen:
            raise ValueError("存在未完成保存记录且基准已冻结。只读检查事务后再启动，禁止自动改写冻结数据。")
        wal = json.loads(self.wal_path.read_text(encoding="utf-8"))
        current = digest(self.manifest.read_bytes())
        if wal.get("schema_version") != 1:
            raise ValueError("未完成保存事务格式异常。")
        if current == wal["after_sha256"]:
            meta = wal["metadata_after"]
            if meta.get("manifest_sha256") != current:
                raise ValueError("保存事务摘要不匹配，拒绝恢复。")
            atomic_write(self.history_path, json_bytes(meta))
            self.warning = "已恢复上次中断的审核历史；CSV 中已写入的真实操作得到保留。"
        elif current == wal["before_sha256"]:
            self.warning = "上次保存尚未写入 CSV，已取消该次未完成事务，请重新检查并操作。"
        else:
            raise ValueError("CSV 与未完成保存事务均不匹配，拒绝覆盖。请保留文件并人工核对。")
        self.wal_path.unlink()

    @staticmethod
    def _parse(raw):
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)):
            raise ValueError("CSV 列缺失或重复。")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("CSV 行列数量不一致。")
        return fields, rows, raw.startswith(b"\xef\xbb\xbf"), "\r\n" if "\r\n" in text else "\n"

    def _encode(self, rows):
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=self.fields, lineterminator=self.newline)
        writer.writeheader()
        writer.writerows(rows)
        return ("\ufeff" if self.csv_bom else "").encode("utf-8") + out.getvalue().encode("utf-8")

    def _validate_rows(self):
        required = {"sample_id", "group_id", "document_id", "png_path", "png_sha256", *EDITABLE,
                    "source_class", "provenance_status", "render_status", "reserved_split"}
        if not required.issubset(self.fields):
            raise ValueError("清单缺少必要字段：" + ", ".join(sorted(required - set(self.fields))))
        ids = [row["sample_id"] for row in self.rows]
        if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("样本 ID 缺失或重复。")
        self.by_id = {row["sample_id"]: row for row in self.rows}
        self.originals = {}
        for row in self.rows:
            if any(row[key] not in {"0", "1", "unknown"} for key in LABELS):
                raise ValueError("非法标签值：" + row["sample_id"])
            if row["variant_id"] == "original":
                if row["group_id"] in self.originals:
                    raise ValueError("同一 group_id 有多个原始页面。")
                self.originals[row["group_id"]] = row["sample_id"]
            self._artifact(row["png_path"])
        if any(row["group_id"] not in self.originals for row in self.rows):
            raise ValueError("存在没有对应原始页面的同源组。")

    def _artifact(self, value):
        if not value or "\\" in value or ":" in value:
            raise ValueError("工件路径非法。")
        path = Path(value)
        resolved = (self.root / path).resolve()
        if path.is_absolute() or ".." in path.parts or not resolved.is_relative_to(self.root):
            raise ValueError("禁止访问基准目录以外的文件。")
        return resolved

    def _check_plan(self):
        try:
            unchanged = digest(self.plan_path.read_bytes()) == self.plan_sha256
        except OSError:
            unchanged = False
        if not unchanged:
            raise ReviewError("来源计划已被其他程序修改或无法读取。请停止标注，核对来源证据后重启服务；本次不写入清单或审核进度。",
                              "source_plan_changed", 409)

    def _check_disk(self, revision=None):
        self._check_plan()
        current = digest(self.manifest.read_bytes())
        if current != digest(self.raw) or getattr(self, "external_conflict", False):
            raise ReviewError("清单已被其他程序修改。请重新载入，核对后再操作；本次未覆盖外部修改。",
                              "external_change", 409, revision=current)
        if revision is not None and revision != self.revision:
            raise ReviewError("页面版本已过期，请重新载入后操作。", "revision_conflict", 409, revision=self.revision)

    def reload(self):
        with self.lock:
            # Reject changed provenance before any WAL recovery or progress reset.
            self._check_plan()
            if self.wal_path.exists():
                self._recover()
                self.raw = self.manifest.read_bytes()
                self.fields, self.rows, self.csv_bom, self.newline = self._parse(self.raw)
                self.revision = digest(self.raw)
                self.external_conflict = False
                self.metadata = self._read_history()
                self.revision = self._token(self.raw, self.metadata)
                self._validate_rows()
            current = self.manifest.read_bytes()
            if digest(current) != digest(self.raw) or getattr(self, "external_conflict", False):
                self.fields, self.rows, self.csv_bom, self.newline = self._parse(current)
                self.raw, self.revision = current, digest(current)
                self._validate_rows()
                meta = copy.deepcopy(self.metadata)
                meta.setdefault("actions", []).append({"id": uuid.uuid4().hex, "type": "external_reload",
                    "at": timestamp(), "reviewer_id": "", "sample_id": "", "fields": [],
                    "note": "显式重新载入外部 CSV；不把外部标签变动冒充本界面人工动作。"})
                meta["progress"], meta["manifest_sha256"] = {}, self.revision
                if not self.frozen:
                    atomic_write(self.history_path, json_bytes(meta))
                self.metadata = meta
                self.revision = self._token(self.raw, self.metadata)
                self.external_conflict = False
                self.warning = "已重新载入磁盘清单。由于发生外部修改，动作核验进度已清空，请逐项复核；CSV 值没有被改写。"
            return self.state()

    def _image_health(self, row):
        try:
            path = self._artifact(row["png_path"])
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, row["png_sha256"])
            if key not in self.health_cache:
                data = path.read_bytes()
                if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
                    raise ValueError("文件不是有效的 PNG。")
                width, height = struct.unpack(">II", data[16:24])
                if digest(data) != row["png_sha256"]:
                    raise ValueError("PNG SHA-256 与清单不符。")
                if (width, height) != (int(row["png_width"]), int(row["png_height"])):
                    raise ValueError("PNG 像素尺寸与清单不符。")
                # Pillow is already present in the project's ML environment;
                # HTTP, persistence and security require only the standard library.
                try:
                    from PIL import Image
                except ImportError as exc:
                    raise ValueError("请使用项目现有 ML 虚拟环境；完整图片解码校验需要其中已有的 Pillow。") from exc
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != "PNG":
                        raise ValueError("图片解码格式不是 PNG。")
                    image.load()
                self.health_cache[key] = ""
            return self.health_cache[key]
        except (OSError, ValueError, TypeError) as exc:
            return str(exc)

    @staticmethod
    def _empty_progress():
        return {"labels": {}, "checks": {}, "completed": None}

    def _progress(self, row, metadata=None):
        metadata = self.metadata if metadata is None else metadata
        entry = copy.deepcopy(metadata.get("progress", {}).get(row["sample_id"], self._empty_progress()))
        for key in LABELS:
            item = entry.setdefault("labels", {}).setdefault(key, {"inspected": False, "reason": "", "reviewer_id": "", "at": ""})
            if item.get("value") != row[key]:
                item["inspected"] = False
        return entry

    def _blockers(self, row, progress, include_completion=True):
        reasons = []
        for key in LABELS:
            if not progress["labels"][key].get("inspected"):
                reasons.append("尚未主动检查标签：" + LABEL_NAMES[key])
            elif row[key] == "unknown":
                reasons.append("标签仍无法确定：" + LABEL_NAMES[key])
        image_error = self._image_health(row)
        if image_error:
            reasons.append("图片读取/校验失败：" + image_error)
        if row.get("render_status") != "ok":
            reasons.append("页面导出未成功。")
        if row.get("render_review_status") != "confirmed":
            reasons.append("渲染尚未人工确认无异常。")
        if row.get("font_review_status") not in FONT_OK:
            reasons.append("字体尚未人工确认可接受。")
        if row.get("font_review_status") == "confirmed_substitution_accepted" and not row.get("review_notes", "").strip():
            reasons.append("已接受字体替换但缺少说明。")
        if row.get("provenance_status") != "confirmed":
            reasons.append("来源尚未核实。")
        if row.get("source_class") not in {"A", "C"}:
            reasons.append("来源类别仍为 " + row.get("source_class", "unknown") + "，不能进入冻结。")
        if row.get("reserved_split") != "independent_test_reserved":
            reasons.append("独立测试保留状态不合法。")
        if include_completion and (row.get("human_reviewed") != "yes" or row.get("annotation_status") != "reviewed"
                                   or not row.get("reviewer_id", "").strip() or not row.get("reviewed_at", "")):
            reasons.append("尚未主动确认“完成本页审核”。")
        return reasons

    def state(self):
        with self.lock:
            self._check_disk()
            rows = []
            for source in self.rows:
                row = dict(source)
                progress = self._progress(row)
                inspected = sum(bool(progress["labels"][key].get("inspected")) for key in LABELS)
                labels_complete = inspected == 4 and all(row[key] in {"0", "1"} for key in LABELS)
                unknown_reviewed = any(row[key] == "unknown" and progress["labels"][key].get("inspected") for key in LABELS)
                blockers = self._blockers(row, progress)
                completed = row["human_reviewed"] == "yes" and row["annotation_status"] == "reviewed"
                status = "满足冻结前审核门槛" if not blockers else ("仍含无法确定项" if unknown_reviewed else
                    ("四标签已明确，其他审核待完成" if labels_complete else ("部分完成" if inspected else "未开始标注")))
                progress.update(inspected_count=inspected, labels_complete=labels_complete, unknown_reviewed=unknown_reviewed,
                                completed=completed, blockers=blockers, frozen_eligible=not blockers, current_status=status)
                row.update(review_progress=progress, image_url="/image/" + row["sample_id"],
                           original_sample_id=self.originals[row["group_id"]], image_error=self._image_health(row))
                rows.append(row)
            stats = {"total": len(rows), "total_cells": len(rows)*4,
                "labels_complete": sum(row["review_progress"]["labels_complete"] for row in rows),
                "partial": sum(row["review_progress"]["inspected_count"] > 0 and not row["review_progress"]["labels_complete"] for row in rows),
                "untouched": sum(row["review_progress"]["inspected_count"] == 0 for row in rows),
                "unknown_pages": sum(any(row[key] == "unknown" for key in LABELS) for row in rows),
                "unresolved_unknown_pages": sum(row["review_progress"]["unknown_reviewed"] for row in rows),
                "render_confirmed": sum(row["render_review_status"] == "confirmed" for row in rows),
                "font_confirmed": sum(row["font_review_status"] in FONT_OK for row in rows),
                "eligible": sum(row["review_progress"]["frozen_eligible"] for row in rows),
                "reviewed": sum(row["review_progress"]["completed"] for row in rows),
                "inspected_cells": sum(row["review_progress"]["inspected_count"] for row in rows)}
            return {"rows": rows, "stats": stats, "revision": self.revision, "csrf_token": self.csrf,
                    "session": self.metadata.get("session", {}), "documents": self.plan.get("documents", []),
                    "label_fields": LABELS, "frozen": self.frozen, "readonly": self.frozen,
                    "warning": self.warning, "can_undo": any(action.get("type") == "action" and not action.get("undone")
                                                            for action in self.metadata.get("actions", []))}

    @staticmethod
    def _reviewer(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100 or any(ord(ch) < 32 for ch in value):
            raise ReviewError("请先填写真实审核者编号（1—100 个字符）。", "reviewer_required")
        return value.strip()

    def _commit(self, rows, metadata):
        self._check_disk()
        if self.frozen:
            raise ReviewError("基准已经冻结，标注界面仅供只读查看。", "frozen_readonly", 423)
        data = self._encode(rows)
        after_sha = digest(data)
        metadata["manifest_sha256"] = after_sha
        # Preserve exact original bytes before the first label or note mutation.
        self.backup.parent.mkdir(exist_ok=True)
        if not self.backup.exists():
            atomic_write(self.backup, self.raw)
        wal = {"schema_version": 1, "before_sha256": digest(self.raw), "after_sha256": after_sha,
               "at": timestamp(), "metadata_after": metadata}
        atomic_write(self.wal_path, json_bytes(wal))
        self._check_disk()
        atomic_write(self.manifest, data)
        atomic_write(self.history_path, json_bytes(metadata))
        self.wal_path.unlink()
        self.raw, self.revision, self.rows, self.metadata = data, self._token(data, metadata), rows, metadata
        self._validate_rows()

    def action(self, payload):
        with self.lock:
            self._check_disk(payload.get("revision"))
            if not isinstance(payload.get("revision"), str):
                raise ReviewError("缺少清单版本，拒绝保存。", "revision_required")
            if self.frozen:
                raise ReviewError("基准已经冻结，只读查看。", "frozen_readonly", 423)
            reviewer = self._reviewer(payload.get("reviewer_id"))
            sid = payload.get("sample_id")
            if sid not in self.by_id:
                raise ReviewError("样本 ID 不存在。", "sample_not_found", 404)
            rows, meta = copy.deepcopy(self.rows), copy.deepcopy(self.metadata)
            row = next(row for row in rows if row["sample_id"] == sid)
            before = dict(row)
            progress = self._progress(row, meta)
            before_progress = copy.deepcopy(progress)
            kind, at = payload.get("type"), timestamp()
            reason = payload.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 10000:
                raise ReviewError("审核说明过长或格式错误。")
            reason = reason.strip()
            if kind in {"label", "all_clear", "checks", "complete"}:
                health = self._image_health(row)
                if health:
                    raise ReviewError("图片读取或校验失败，禁止把该页记为已检查：" + health, "image_invalid")
            if kind == "label":
                key, value = payload.get("label"), payload.get("value")
                if key not in LABELS or value not in {"0", "1", "unknown"}:
                    raise ReviewError("标签字段或取值非法。", "invalid_label")
                if value == "unknown" and not reason:
                    raise ReviewError("已检查但不确定时，请填写无法判断的原因。", "unknown_reason_required")
                row[key] = value
                progress["labels"][key] = {"inspected": True, "value": value, "reason": reason,
                                            "reviewer_id": reviewer, "at": at}
            elif kind == "all_clear":
                if payload.get("confirm") is not True:
                    raise ReviewError("必须明确确认亲自检查了当前页四类缺陷，均不存在问题。", "confirmation_required")
                for key in LABELS:
                    row[key] = "0"
                    progress["labels"][key] = {"inspected": True, "value": "0", "reason": "人工确认当前页四类均不存在缺陷",
                                                "reviewer_id": reviewer, "at": at}
            elif kind == "notes":
                notes = payload.get("notes")
                if not isinstance(notes, str) or len(notes) > 20000:
                    raise ReviewError("备注格式错误或超过 20000 字。")
                row["review_notes"] = notes
            elif kind == "checks":
                checks = {key: payload[key] for key in ("render_review_status", "font_review_status") if key in payload}
                if not checks:
                    raise ReviewError("请提供需要人工确认的渲染或字体状态。")
                for key, value in checks.items():
                    choices = RENDER_VALUES if key == "render_review_status" else FONT_VALUES
                    if value not in choices:
                        raise ReviewError("不支持的人工核验状态。")
                    if value in {"needs_attention", "confirmed_substitution_accepted"} and not reason:
                        raise ReviewError("发现异常或接受字体替换时必须填写原因。", "check_reason_required")
                    row[key] = value
                    progress.setdefault("checks", {})[key] = {"value": value, "reason": reason, "reviewer_id": reviewer, "at": at}
                if reason:
                    row["review_notes"] = (row["review_notes"].rstrip() + "\n" + "[渲染/字体核验] " + reason).strip()
            elif kind == "complete":
                if payload.get("confirm") is not True:
                    raise ReviewError("请主动确认完成本页人工审核。", "confirmation_required")
                blockers = self._blockers(row, progress, include_completion=False)
                if blockers:
                    raise ReviewError("本页尚不满足完整审核门槛。", "completion_blocked", blockers=blockers)
                row.update(human_reviewed="yes", annotation_status="reviewed", reviewer_id=reviewer, reviewed_at=at)
                progress["completed"] = {"reviewer_id": reviewer, "at": at}
            else:
                raise ReviewError("不支持该操作；来源字段只读，界面不提供冻结或模型入口。", "invalid_action")
            if kind != "complete":
                row.update(human_reviewed="no", annotation_status="pending_human_review", reviewer_id=reviewer, reviewed_at=at)
                progress["completed"] = None
            changes = [{"field": key, "old": before[key], "new": row[key]} for key in self.fields if before[key] != row[key]]
            if any(item["field"] not in EDITABLE for item in changes):
                raise ReviewError("禁止修改来源、ID、分组、路径及工件元数据。", "metadata_immutable")
            event = {"id": uuid.uuid4().hex, "type": "action", "operation": kind, "at": at,
                     "reviewer_id": reviewer, "sample_id": sid, "fields": changes,
                     "progress_before": before_progress, "progress_after": copy.deepcopy(progress), "undone": False}
            meta.setdefault("progress", {})[sid] = progress
            meta.setdefault("actions", []).append(event)
            meta["session"] = {"reviewer_id": reviewer, "last_sample_id": sid}
            self._commit(rows, meta)
            return self.state()

    def undo(self, payload):
        with self.lock:
            self._check_disk(payload.get("revision"))
            if not isinstance(payload.get("revision"), str):
                raise ReviewError("撤销操作缺少清单版本。", "revision_required")
            reviewer = self._reviewer(payload.get("reviewer_id"))
            rows, meta = copy.deepcopy(self.rows), copy.deepcopy(self.metadata)
            event = next((item for item in reversed(meta.get("actions", [])) if item.get("type") == "action" and not item.get("undone")), None)
            if not event:
                raise ReviewError("没有可撤销的标注操作。", "nothing_to_undo")
            sid = event["sample_id"]
            row = next(item for item in rows if item["sample_id"] == sid)
            reverse = []
            for change in event["fields"]:
                if row.get(change["field"]) != change["new"]:
                    raise ReviewError("当前字段与历史不一致，拒绝撤销以免覆盖其他修改。", "undo_conflict", 409)
                row[change["field"]] = change["old"]
                reverse.append({"field": change["field"], "old": change["new"], "new": change["old"]})
            event["undone"] = True
            meta.setdefault("progress", {})[sid] = event["progress_before"]
            meta["actions"].append({"id": uuid.uuid4().hex, "type": "undo", "at": timestamp(),
                "reviewer_id": reviewer, "sample_id": sid, "undoes": event["id"], "fields": reverse})
            self._commit(rows, meta)
            return self.state()

    def session(self, payload):
        with self.lock:
            self._check_disk()
            if self.frozen:
                return self.state()
            meta = copy.deepcopy(self.metadata)
            session = meta.setdefault("session", {})
            if "reviewer_id" in payload:
                session["reviewer_id"] = self._reviewer(payload["reviewer_id"]) if payload["reviewer_id"] else ""
            if "last_sample_id" in payload:
                if payload["last_sample_id"] not in self.by_id:
                    raise ReviewError("浏览位置的样本不存在。")
                session["last_sample_id"] = payload["last_sample_id"]
            atomic_write(self.history_path, json_bytes(meta))
            self.metadata = meta
            return self.state()

    def check(self):
        with self.lock:
            state = self.state()
            blockers = [{"sample_id": row["sample_id"], "reasons": row["review_progress"]["blockers"]}
                        for row in state["rows"] if row["review_progress"]["blockers"]]
            helper_errors = []
            try:
                if self._helper is None:
                    spec = importlib.util.spec_from_file_location("benchmark_review_validation", self.root / "prepare_benchmark.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self._helper = module
                self._helper.ROOT = self.root
                self._helper.ML = self.root.parent
            except Exception as exc:
                helper_errors.append("原检查模块加载失败：" + str(exc))
            if self._helper is not None:
                # Existing helpers are read-only. Never call freeze_benchmark.
                for name, arguments in (("validate_manifest", (self.rows, self.plan)),
                                        ("_review_check_rows", (self.root, self.rows))):
                    try:
                        getattr(self._helper, name)(*arguments)
                    except Exception as exc:
                        helper_errors.append(name + ": " + str(exc))
                if (self.root / "freeze_record.json").exists():
                    try:
                        self._helper.check_frozen_benchmark(self.root)
                    except Exception as exc:
                        helper_errors.append(str(exc))
            helper_error = "\n".join(helper_errors)
            self._check_disk()
            state["check"] = {"passed": not blockers and not helper_error, "blockers": blockers,
                              "helper_passed": not helper_error, "helper_error": helper_error,
                              "frozen": self.frozen, "message": "仅检查，未执行冻结、模型预测或训练。"}
            return state


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    @staticmethod
    def acquire_write_lock(root):
        handle = (Path(root).resolve() / "review_server.lock").open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except Exception:
            handle.close()
            raise

    def __init__(self, address, store, write_lock=None):
        self.store = store
        self._write_lock = write_lock or self.acquire_write_lock(store.root)
        try:
            super().__init__(address, ReviewHandler)
        except Exception:
            self._write_lock.close()
            raise
        self.trusted_origin = "http://127.0.0.1:" + str(self.server_address[1])

    def server_close(self):
        super().server_close()
        if not self._write_lock.closed:
            self._write_lock.close()


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "OfflinePPTReview/1.0"

    def log_message(self, fmt, *args):
        if args and ("/api/" in str(args[0]) or "/image/" in str(args[0])):
            return
        super().log_message(fmt, *args)

    def _trusted(self, mutation=False):
        expected_host = self.server.trusted_origin.removeprefix("http://")
        if self.headers.get("Host") != expected_host:
            raise ReviewError("仅允许通过本机 127.0.0.1 地址访问。", "untrusted_host", 403)
        origin = self.headers.get("Origin")
        if origin and origin != self.server.trusted_origin:
            raise ReviewError("拒绝来自其他网站的请求。", "untrusted_origin", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ReviewError("拒绝跨站访问。", "cross_site", 403)
        if mutation and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.store.csrf):
            raise ReviewError("本地会话已变更，请刷新页面后重试。", "csrf_invalid", 403)

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", '"' + self.server.store.revision + '"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, value):
        self._send(status, json_bytes(value))

    def _error(self, exc):
        if isinstance(exc, ReviewError):
            self._json(exc.status, {"error": str(exc), "code": exc.code, **exc.detail})
        elif isinstance(exc, (OSError, ValueError, KeyError, TypeError)):
            self._json(500, {"error": "读取或保存失败，本次未确认成功。请保留当前操作并重载检查：" + str(exc), "code": "storage_error"})
        else:
            self._json(500, {"error": "本地服务发生错误，本次未确认保存。请查看终端。", "code": "internal_error"})
            import traceback
            traceback.print_exc()

    def do_GET(self):
        try:
            self._trusted()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if parsed.path != path or "\\" in path or ".." in path.split("/"):
                raise ReviewError("禁止路径穿越。", "invalid_path", 403)
            if path == "/api/state":
                reload = parse_qs(parsed.query).get("reload") == ["1"]
                return self._json(200, self.server.store.reload() if reload else self.server.store.state())
            if path.startswith("/image/"):
                sid = path[len("/image/"):]
                store = self.server.store
                with store.lock:
                    store._check_disk()
                    if sid not in store.by_id:
                        raise ReviewError("图片未登记。", "image_not_found", 404)
                    row = store.by_id[sid]
                    error = store._image_health(row)
                    if error:
                        raise ReviewError("图片校验失败：" + error, "image_invalid", 422)
                    return self._send(200, store._artifact(row["png_path"]).read_bytes(), "image/png")
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/index.html": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in files:
                raise ReviewError("该地址不可访问。", "not_found", 404)
            filename, content_type = files[path]
            return self._send(200, (self.server.store.root / "review_ui" / filename).read_bytes(), content_type)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            self._trusted(mutation=True)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ReviewError("仅支持 JSON 请求。", "invalid_content_type", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ReviewError("请求大小不合法。", "request_size", 413)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ReviewError("请求必须为对象。")
            path = urlsplit(self.path).path
            store = self.server.store
            operations = {"/api/action": store.action, "/api/undo": store.undo, "/api/session": store.session}
            if path == "/api/check":
                if "revision" in payload:
                    store._check_disk(payload["revision"])
                result = store.check()
            elif path in operations:
                result = operations[path](payload)
            else:
                raise ReviewError("接口不存在；本地界面不提供冻结入口。", "not_found", 404)
            self._json(200, result)
        except Exception as exc:
            self._error(exc)

    def do_OPTIONS(self):
        self._json(403, {"error": "不允许跨站请求。", "code": "cross_site"})


def main():
    parser = argparse.ArgumentParser(description="离线 PPT 设计缺陷人工标注工作台，仅监听本机。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="基准目录，可指定临时复制目录用于测试")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，0 表示自动分配")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0—65535 之间。")
    # Acquire before loading/recovering a WAL: a second service must never
    # recover a transaction while the first service is still committing it.
    write_lock = ReviewHTTPServer.acquire_write_lock(args.root)
    try:
        store = ReviewStore(args.root)
        server = ReviewHTTPServer(("127.0.0.1", args.port), store, write_lock=write_lock)
    except Exception:
        write_lock.close()
        raise
    print("PPT 设计缺陷人工标注工作台：" + server.trusted_origin, flush=True)
    print("仅使用本地文件，不联网、不预测、不自动标注、不自动冻结。关闭终端可停止服务。", flush=True)
    if not args.no_browser:
        webbrowser.open(server.trusted_origin)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

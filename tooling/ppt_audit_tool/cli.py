"""``ppt-audit`` command line interface.

Design rules:
- stdout is reserved for one JSON document (or a short human summary);
- progress and service-start messages go to stderr;
- every command returns a stable ``ok`` field and a non-zero exit code on failure;
- no command writes into ``backend/data`` directly.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from . import TOOL_NAME, TOOL_VERSION
from .client import ApiError, Client, InputError, ServiceUnavailable, ToolError
from .contract import (
    DEFECT_LABELS_ZH,
    ISSUE_MODES,
    PROFILES,
    ContractError,
    human_summary,
    profile_catalog,
)
from .service import ensure_service, service_snapshot
from .workflows import (
    explain_issue,
    list_result,
    run_audit,
    run_demo,
    run_repair,
    status_result,
)

DEFAULT_OUTPUT_DIR = "slidesentinel-output"
DEMO_SCENARIOS = ("full", "risk", "api_failure", "module_missing")


def _env_base_url() -> str:
    return (os.environ.get("SLIDESENTINEL_BASE_URL")
            or os.environ.get("PPT_AGENT_BASE_URL")
            or "http://127.0.0.1:8000")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=_env_base_url(),
                        help="服务地址，默认 http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单次 HTTP 请求超时秒数")
    parser.add_argument("--no-auto-start", action="store_true",
                        help="服务未运行时不要自动调用 start_app.ps1")
    parser.add_argument("--format", choices=("json", "text"), default="json",
                        help="stdout 输出格式，默认 json")
    parser.add_argument("--quiet", action="store_true",
                        help="不向 stderr 打印进度")


def _add_issue_mode(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--issues", choices=ISSUE_MODES, default="summary",
                        help="summary=前 50 条；all=最多 500 条；none=不返回明细")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ppt-audit",
        description="SlideSentinel 工具外壳：把 PPT 审查 / 修复变成可脚本调用的单次命令。",
    )
    parser.add_argument("--version", action="version",
                        version=f"{TOOL_NAME} {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profiles", help="列出 quick / standard / deep 三档审查配置")
    p.add_argument("--format", choices=("json", "text"), default="json")

    p = sub.add_parser("capabilities", help="查看服务与各检测模块的当前能力")
    _add_common(p)
    p.add_argument("--full", action="store_true", help="附带服务返回的完整 capabilities")

    p = sub.add_parser("doctor", help="检查 Python、服务、模块与输出目录")
    _add_common(p)

    p = sub.add_parser("audit", help="上传 PPTX 并完成一次审查")
    _add_common(p)
    p.add_argument("file", help="本地 .pptx 路径")
    p.add_argument("--profile", choices=tuple(PROFILES), default="standard")
    p.add_argument("--name", default="", help="任务显示名，默认文件名")
    p.add_argument("--no-wait", action="store_true", help="只提交任务，立即返回 task_id")
    p.add_argument("--wait-timeout", type=float, default=900.0,
                   help="等待审查完成的秒数，默认 900")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                   help=f"报告与结果目录，默认 {DEFAULT_OUTPUT_DIR}")
    p.add_argument("--no-artifacts", action="store_true", help="不下载报告文件")
    _add_issue_mode(p)

    p = sub.add_parser("status", help="查询任务状态；已完成时返回报告摘要")
    _add_common(p)
    p.add_argument("task_id")
    p.add_argument("--mode", choices=("real", "demo"), default="real")
    p.add_argument("--output-dir", default="", help="可选：下载报告到该目录")
    _add_issue_mode(p)

    p = sub.add_parser("report", help="下载 JSON / HTML / PDF 报告")
    _add_common(p)
    p.add_argument("task_id")
    p.add_argument("--type", choices=("html", "json", "pdf"), default="html")
    p.add_argument("--out", default="", help="目标文件；默认写入 slidesentinel-output/<task_id>/")
    p.add_argument("--mode", choices=("real", "demo"), default="real")

    p = sub.add_parser("repair", help="对已完成任务运行 AI 修复")
    _add_common(p)
    p.add_argument("task_id")
    p.add_argument("--mode", choices=("real", "demo"), default="real")
    p.add_argument("--backend", choices=("api", "rule"), default="api",
                   help="api=模型调度；rule=纯规则计划")
    p.add_argument("--review", action="store_true", help="启用多轮复核")
    p.add_argument("--rounds", type=int, default=0, help="复核轮数上限，0 表示默认")
    p.add_argument("--no-wait", action="store_true", help="只发起修复，立即返回 repair_id")
    p.add_argument("--wait-timeout", type=float, default=900.0)
    p.add_argument("--download-fixed", action="store_true", help="下载 fixed.pptx")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--no-artifacts", action="store_true")

    p = sub.add_parser("explain", help="按 issue_id 返回完整证据与页面图地址")
    _add_common(p)
    p.add_argument("task_id")
    p.add_argument("issue_id")
    p.add_argument("--mode", choices=("real", "demo"), default="real")

    p = sub.add_parser("list", help="列出任务")
    _add_common(p)
    p.add_argument("--mode", choices=("real", "demo"), default="real")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)

    p = sub.add_parser("demo", help="创建预置演示任务（不读取本地文件）")
    _add_common(p)
    p.add_argument("--scenario", choices=DEMO_SCENARIOS, default="full")
    p.add_argument("--name", default="")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--wait-timeout", type=float, default=300.0)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--no-artifacts", action="store_true")
    _add_issue_mode(p)

    return parser


def _emit(result: dict[str, Any], fmt: str) -> None:
    if fmt == "text":
        print(human_summary(result))
        return
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _emit_capabilities(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        _emit(result, "json")
        return
    app = result.get("app") or {}
    print(f"{app.get('name') or 'SlideSentinel'} {app.get('version') or ''}".strip())
    print(f"服务：{result.get('base_url')}")
    print(f"任务数：{result.get('task_count')} · 演示任务：{(result.get('demo') or {}).get('task_count')}")
    for key, value in (result.get("modules") or {}).items():
        state = value.get("state") if isinstance(value, dict) else value
        print(f"- {key}: {state}")
    for profile in result.get("profiles") or []:
        print(f"- profile {profile['name']}: {profile.get('description')}")
    for note in result.get("notes") or []:
        print(f"注意：{note}")


def _emit_list(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        _emit(result, "json")
        return
    print(f"{result.get('mode')} 任务 {len(result.get('items') or [])} / {result.get('total')}")
    for item in result.get("items") or []:
        print(f"- {item.get('id')}  {item.get('status'):<22} {item.get('name')}")


def _emit_report(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        _emit(result, "json")
        return
    print(f"已下载 {result.get('format')}：{result.get('path')}")
    print(f"URL：{result.get('url')}")


def _emit_explain(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        _emit(result, "json")
        return
    if not result.get("ok"):
        error = result.get("error") or {}
        print(f"失败：{error.get('message') or error}")
        return
    issue = result.get("issue") or {}
    label = DEFECT_LABELS_ZH.get(issue.get("defect_type"), issue.get("defect_type"))
    print(f"{issue.get('issue_id')} · {label} · 第 {issue.get('slide_index')} 页")
    print(f"状态：{issue.get('review_status')} / {issue.get('execution_status')} · 可见：{result.get('visible')}")
    print(f"证据：{issue.get('evidence_summary') or '-'}")
    print(f"建议：{issue.get('recommendation') or '-'}")
    print(f"页面图：{result.get('page_image_url') or '-'}")


def _emit_repair(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        _emit(result, "json")
        return
    summary = result.get("summary") or {}
    verify = result.get("verify") or {}
    print(f"修复 {result.get('repair_id')} · {result.get('status')}")
    print(f"计划 {result.get('plan_source') or '-'} · "
          f"applied {summary.get('applied', 0)} / manual {summary.get('manual', 0)} / "
          f"skipped {summary.get('skipped', 0)} / partial {summary.get('partial', 0)}")
    if verify:
        print(f"结构复检：{verify.get('before_structure')} → {verify.get('after_structure')} · "
              f"新增 {verify.get('introduced')}")
    for label, path in (result.get("artifacts") or {}).items():
        if isinstance(path, str) and path:
            print(f"{label}: {path}")


def _progress_printer(quiet: bool):
    last: dict[str, Any] = {"key": None}

    def progress(detail: dict[str, Any], stage: dict[str, Any] | None) -> None:
        if quiet:
            return
        if stage:
            key = (stage.get("key"), stage.get("status"), stage.get("message"))
            if key == last["key"]:
                return
            last["key"] = key
            name = stage.get("name") or stage.get("key") or "stage"
            message = stage.get("message") or ""
            print(f"[{name}] {message}", file=sys.stderr, flush=True)
            return
        status = detail.get("status")
        message = detail.get("message")
        key = (status, message)
        if key == last["key"]:
            return
        last["key"] = key
        if status:
            suffix = f"：{message}" if message else ""
            print(f"[{status}]{suffix}", file=sys.stderr, flush=True)

    return progress


def _client(args: argparse.Namespace) -> Client:
    return ensure_service(
        args.base_url,
        auto_start=not bool(getattr(args, "no_auto_start", False)),
        quiet=bool(getattr(args, "quiet", False)),
    )


def _capabilities(args: argparse.Namespace) -> dict[str, Any]:
    client = _client(args)
    snapshot = service_snapshot(client)
    result = {
        "ok": True,
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        **snapshot,
        "profiles": profile_catalog(),
        "notes": [
            "渲染能力未在 capabilities 中单列；它依赖 Windows 桌面版 PowerPoint。",
            "six_detector.state=unsupported 表示隔离 ML 环境或冻结实现不可用，六类检查不会伪造结果。",
            "mllm.state=not_configured 表示未填写 API；此时请使用 standard 或 quick。",
        ],
    }
    if args.full:
        result["raw"] = client.capabilities()
    return result


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    client = _client(args)
    snapshot = service_snapshot(client)
    output = Path(DEFAULT_OUTPUT_DIR).expanduser()
    try:
        output.mkdir(parents=True, exist_ok=True)
        writable = os.access(output, os.W_OK)
    except OSError:
        writable = False
    com_spec = None
    try:
        com_spec = importlib.util.find_spec("win32com")
    except (ImportError, ValueError):
        com_spec = None
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "windows": sys.platform == "win32",
        "pywin32_importable": com_spec is not None,
        "base_url": client.base_url,
        "output_dir": str(output.resolve()),
        "output_writable": writable,
        "service": snapshot,
        "profiles": profile_catalog(),
        "notes": [
            "pywin32_importable 只说明 Python 侧可用；PowerPoint 是否安装仍需实际任务验证。",
            "若任务渲染失败，报告会保留 render 失败状态，不会用结构检查替代页面观察。",
        ],
    }


def _force_utf8() -> None:
    """Make redirected stdout/stderr UTF-8 on Chinese Windows consoles.

    Without this, ``python -m ppt_audit_tool ... > result.json`` can write
    GBK while an agent expects UTF-8.  ``reconfigure`` is best-effort because
    callers may replace the streams in tests.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "profiles":
        result = {"ok": True, "tool": TOOL_NAME, "version": TOOL_VERSION,
                  "profiles": profile_catalog()}
        if args.format == "json":
            _emit(result, "json")
        else:
            for item in result["profiles"]:
                print(f"{item['name']}: {item['label']} — {item['description']}")
                print("  " + json.dumps(item["options"], ensure_ascii=False))
        return 0

    fmt = getattr(args, "format", "json")
    try:
        if args.command == "capabilities":
            result = _capabilities(args)
            _emit_capabilities(result, fmt)
            return 0

        if args.command == "doctor":
            result = _doctor(args)
            if fmt == "json":
                _emit(result, "json")
            else:
                print(f"Python {result['python']} · {result['platform']}")
                print(f"服务 {result['base_url']} · 输出目录 {result['output_dir']}（可写：{result['output_writable']}）")
                for key, value in (result["service"].get("modules") or {}).items():
                    print(f"- {key}: {value.get('state') if isinstance(value, dict) else value}")
                for note in result["notes"]:
                    print(f"注意：{note}")
            return 0

        if args.command == "audit":
            client = _client(args)
            output_dir = None if args.no_artifacts else args.output_dir
            result = run_audit(
                client, args.file, profile=args.profile, name=args.name,
                wait=not args.no_wait, wait_timeout=args.wait_timeout,
                output_dir=output_dir, issue_mode=args.issues,
                progress=_progress_printer(args.quiet),
            )
            _emit(result, fmt)
            return 0 if result.get("ok") else 1

        if args.command == "status":
            client = _client(args)
            result = status_result(
                client, args.task_id, mode=args.mode, issue_mode=args.issues,
                output_dir=(args.output_dir or None),
            )
            _emit(result, fmt)
            return 0 if result.get("ok") else 1

        if args.command == "report":
            client = _client(args)
            ext = args.type
            target = Path(args.out).expanduser() if args.out else (
                Path(DEFAULT_OUTPUT_DIR).expanduser() / args.task_id / f"report.{ext}")
            path = client.download_report(args.task_id, ext, target, args.mode)
            result = {
                "ok": True,
                "task_id": args.task_id,
                "format": ext,
                "path": str(path.resolve()),
                "url": client.report_url(args.task_id, ext, args.mode),
            }
            _emit_report(result, fmt)
            return 0

        if args.command == "repair":
            client = _client(args)
            output_dir = None if args.no_artifacts else args.output_dir
            result = run_repair(
                client, args.task_id, mode=args.mode, backend=args.backend,
                review=args.review, rounds=args.rounds, wait=not args.no_wait,
                wait_timeout=args.wait_timeout, output_dir=output_dir,
                download_fixed=args.download_fixed,
                progress=_progress_printer(args.quiet),
            )
            _emit_repair(result, fmt)
            return 0 if result.get("ok") else 1

        if args.command == "explain":
            client = _client(args)
            result = explain_issue(client, args.task_id, args.issue_id, mode=args.mode)
            _emit_explain(result, fmt)
            return 0 if result.get("ok") else 1

        if args.command == "list":
            client = _client(args)
            result = list_result(client, mode=args.mode, limit=args.limit,
                                 offset=args.offset)
            _emit_list(result, fmt)
            return 0

        if args.command == "demo":
            client = _client(args)
            output_dir = None if args.no_artifacts else args.output_dir
            result = run_demo(
                client, scenario=args.scenario, name=args.name,
                wait=not args.no_wait, wait_timeout=args.wait_timeout,
                output_dir=output_dir, issue_mode=args.issues,
                progress=_progress_printer(args.quiet),
            )
            _emit(result, fmt)
            return 0 if result.get("ok") else 1

        parser.error(f"未知命令：{args.command}")
        return 2
    except ContractError as exc:
        _emit({"ok": False, "error": {"code": "invalid_profile", "message": str(exc)}}, fmt)
        return 2
    except InputError as exc:
        _emit({"ok": False, "error": exc.as_dict()}, fmt)
        return 3
    except ServiceUnavailable as exc:
        _emit({"ok": False, "error": exc.as_dict()}, fmt)
        return 2
    except ApiError as exc:
        _emit({"ok": False, "error": exc.as_dict()}, fmt)
        return 2
    except ToolError as exc:
        _emit({"ok": False, "error": exc.as_dict()}, fmt)
        return 2
    except KeyboardInterrupt:
        _emit({"ok": False, "error": {"code": "interrupted", "message": "已取消。"}}, fmt)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

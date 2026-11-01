"""Minimal MCP (Model Context Protocol) stdio server.

This is intentionally dependency-free: MCP stdio is newline-delimited
JSON-RPC 2.0, so a small dispatcher is enough.  Keeping it stdlib-only means
the tool works with the same ``backend/.venv`` and does not require an extra
``pip install mcp`` for the teacher.

Supported tools are high-level, not raw HTTP endpoints: an agent asks for
``ppt_audit`` and gets a compact report, not a five-step upload/poll sequence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import TOOL_NAME, TOOL_VERSION
from .client import ApiError, Client, InputError, ServiceUnavailable, ToolError
from .contract import PROFILES, ContractError, profile_catalog
from .service import ensure_service, service_snapshot
from .workflows import (
    explain_issue,
    list_result,
    run_audit,
    run_demo,
    run_repair,
    status_result,
)

SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_OUTPUT_DIR = "slidesentinel-output"

_ISSUE_SCHEMA = {
    "type": "string",
    "enum": ["none", "summary", "all"],
    "default": "summary",
    "description": "返回的缺陷明细数量：none 不返回，summary 前 50 条，all 最多 500 条。",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ppt_capabilities",
        "description": "检查本机 SlideSentinel 服务、六类检测、CLIP 与 LLM 复核的可用状态。调用审查前建议先执行。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_audit",
        "description": "审查一份本地 .pptx：上传、渲染、结构规则/六类检测（按 profile）、生成报告，返回结构化问题清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "本机 .pptx 绝对路径。"},
                "profile": {
                    "type": "string", "enum": list(PROFILES), "default": "standard",
                    "description": "quick=结构初筛；standard=结构+六类；deep=再加 CLIP 与 LLM 复核。",
                },
                "name": {"type": "string", "description": "任务显示名，默认文件名。"},
                "wait_seconds": {
                    "type": "number", "default": 900, "minimum": 0,
                    "description": "等待完成的秒数；0 表示只提交并返回 task_id。",
                },
                "output_dir": {
                    "type": "string", "default": DEFAULT_OUTPUT_DIR,
                    "description": "报告与 result.json 的输出目录；空字符串表示不下载。",
                },
                "issues": _ISSUE_SCHEMA,
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_status",
        "description": "查询任务状态；任务完成时直接返回报告摘要，未完成时返回阶段进度。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["real", "demo"], "default": "real"},
                "issues": _ISSUE_SCHEMA,
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_report",
        "description": "下载已完成任务的 JSON、HTML 或 PDF 报告，返回本地路径和访问 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "format": {"type": "string", "enum": ["html", "json", "pdf"], "default": "html"},
                "output_path": {"type": "string", "description": "目标文件路径；省略时写入 output_dir。"},
                "mode": {"type": "string", "enum": ["real", "demo"], "default": "real"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_repair",
        "description": "对已完成任务运行 AI 修复；默认只做安全的几何修复，视觉/拥挤/留白/异常设计仍留给人工。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["real", "demo"], "default": "real"},
                "backend": {"type": "string", "enum": ["api", "rule"], "default": "api"},
                "review": {"type": "boolean", "default": False, "description": "启用多轮复核。"},
                "rounds": {"type": "integer", "default": 0, "minimum": 0, "maximum": 3},
                "wait_seconds": {"type": "number", "default": 900, "minimum": 0},
                "download_fixed": {"type": "boolean", "default": False},
                "output_dir": {"type": "string", "default": DEFAULT_OUTPUT_DIR},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_explain",
        "description": "按 issue_id 返回完整检测证据、坐标、复核历史与对应页面渲染图 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "issue_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["real", "demo"], "default": "real"},
            },
            "required": ["task_id", "issue_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_list",
        "description": "列出最近的真实或演示任务。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["real", "demo"], "default": "real"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ppt_demo",
        "description": "创建系统预置的演示任务（不读取本地文件），用于演示完整、风险、API 失败、模块缺失四种场景。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scenario": {
                    "type": "string", "enum": ["full", "risk", "api_failure", "module_missing"],
                    "default": "full",
                },
                "wait_seconds": {"type": "number", "default": 300, "minimum": 0},
                "output_dir": {"type": "string", "default": DEFAULT_OUTPUT_DIR},
                "issues": _ISSUE_SCHEMA,
            },
            "additionalProperties": False,
        },
    },
]


class MCPServer:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        auto_start: bool = True,
        output_dir: str | Path = DEFAULT_OUTPUT_DIR,
        client_factory: Callable[[], Client] | None = None,
    ) -> None:
        self.base_url = base_url
        self.auto_start = auto_start
        self.output_dir = str(output_dir)
        self._client: Client | None = None
        self._client_factory = client_factory

    # ------------------------------------------------------------- transport
    def _get_client(self) -> Client:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = ensure_service(self.base_url, auto_start=self.auto_start)
        return self._client

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        is_notification = "id" not in message
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if not isinstance(method, str):
            return None if is_notification else _error(request_id, -32600, "缺少 method")
        if is_notification:
            # initialized / cancelled / exit: no response by JSON-RPC rules.
            return None

        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return _result(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": TOOL_NAME, "version": TOOL_VERSION},
                "instructions": (
                    "SlideSentinel 是本地 PPTX 质量审查与修复工具。"
                    "先调用 ppt_capabilities 检查 PowerPoint/六类环境；"
                    "再用 ppt_audit 审查，ppt_status 继续等待，ppt_explain 解释单条问题，"
                    "ppt_repair 只自动修可证明的几何问题。"
                ),
            })

        if method == "ping":
            return _result(request_id, {})

        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, -32602, "tools/call 需要 name 与 arguments")
            result = self._call_tool(name, arguments)
            text = json.dumps(result, ensure_ascii=False)
            return _result(request_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": result,
                "isError": not bool(result.get("ok")),
            })

        if method == "resources/list":
            return _result(request_id, {"resources": []})
        if method == "prompts/list":
            return _result(request_id, {"prompts": []})
        if method == "shutdown":
            return _result(request_id, {})

        return _error(request_id, -32601, f"不支持的方法：{method}")

    # ----------------------------------------------------------------- tools
    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "ppt_capabilities":
                return self._capabilities()
            if name == "ppt_audit":
                return self._audit(args)
            if name == "ppt_status":
                return status_result(
                    self._get_client(), str(args["task_id"]),
                    mode=str(args.get("mode") or "real"),
                    issue_mode=str(args.get("issues") or "summary"),
                    output_dir=None,
                )
            if name == "ppt_report":
                return self._report(args)
            if name == "ppt_repair":
                return self._repair(args)
            if name == "ppt_explain":
                return explain_issue(
                    self._get_client(), str(args["task_id"]), str(args["issue_id"]),
                    mode=str(args.get("mode") or "real"),
                )
            if name == "ppt_list":
                return list_result(
                    self._get_client(), mode=str(args.get("mode") or "real"),
                    limit=int(args.get("limit") or 20), offset=int(args.get("offset") or 0),
                )
            if name == "ppt_demo":
                return self._demo(args)
            return {
                "ok": False,
                "error": {"code": "unknown_tool", "message": f"未知工具：{name}"},
            }
        except KeyError as exc:
            return {
                "ok": False,
                "error": {"code": "missing_argument", "message": f"缺少参数：{exc}"},
            }
        except ContractError as exc:
            return {"ok": False, "error": {"code": "invalid_profile", "message": str(exc)}}
        except InputError as exc:
            return {"ok": False, "error": exc.as_dict()}
        except ServiceUnavailable as exc:
            return {"ok": False, "error": exc.as_dict()}
        except ApiError as exc:
            return {"ok": False, "error": exc.as_dict()}
        except ToolError as exc:
            return {"ok": False, "error": exc.as_dict()}
        except Exception as exc:  # keep the protocol alive; no traceback to the client
            return {
                "ok": False,
                "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
            }

    def _capabilities(self) -> dict[str, Any]:
        client = self._get_client()
        return {
            "ok": True,
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            **service_snapshot(client),
            "profiles": profile_catalog(),
            "notes": [
                "渲染能力未在 capabilities 中单列；它依赖 Windows 桌面版 PowerPoint。",
                "six_detector.state=unsupported 表示隔离 ML 环境或冻结实现不可用。",
                "mllm.state=not_configured 表示未填写 API；此时请使用 standard 或 quick。",
            ],
        }

    def _audit(self, args: dict[str, Any]) -> dict[str, Any]:
        wait_seconds = float(args.get("wait_seconds", 900))
        return run_audit(
            self._get_client(),
            str(args["file_path"]),
            profile=str(args.get("profile") or "standard"),
            name=str(args.get("name") or ""),
            wait=wait_seconds > 0,
            wait_timeout=max(1.0, wait_seconds),
            output_dir=str(args.get("output_dir", self.output_dir)),
            issue_mode=str(args.get("issues") or "summary"),
        )

    def _report(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._get_client()
        task_id = str(args["task_id"])
        fmt = str(args.get("format") or "html")
        target = args.get("output_path") or str(
            Path(self.output_dir) / task_id / f"report.{fmt}"
        )
        path = client.download_report(task_id, fmt, Path(target),
                                      str(args.get("mode") or "real"))
        return {
            "ok": True,
            "task_id": task_id,
            "format": fmt,
            "path": str(path.resolve()),
            "url": client.report_url(task_id, fmt, str(args.get("mode") or "real")),
        }

    def _repair(self, args: dict[str, Any]) -> dict[str, Any]:
        wait_seconds = float(args.get("wait_seconds", 900))
        return run_repair(
            self._get_client(),
            str(args["task_id"]),
            mode=str(args.get("mode") or "real"),
            backend=str(args.get("backend") or "api"),
            review=bool(args.get("review")),
            rounds=int(args.get("rounds") or 0),
            wait=wait_seconds > 0,
            wait_timeout=max(1.0, wait_seconds),
            output_dir=str(args.get("output_dir", self.output_dir)),
            download_fixed=bool(args.get("download_fixed")),
        )

    def _demo(self, args: dict[str, Any]) -> dict[str, Any]:
        wait_seconds = float(args.get("wait_seconds", 300))
        return run_demo(
            self._get_client(),
            scenario=str(args.get("scenario") or "full"),
            wait=wait_seconds > 0,
            wait_timeout=max(1.0, wait_seconds),
            output_dir=str(args.get("output_dir", self.output_dir)),
            issue_mode=str(args.get("issues") or "summary"),
        )

    # ----------------------------------------------------------------- serve
    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        debug = str(os.environ.get("SLIDESENTINEL_MCP_LOG", "")).lower() in ("1", "true", "yes")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: dict[str, Any] | None = _error(
                    None, -32700, f"JSON 解析失败：{exc.msg}")
            else:
                if debug:
                    print(f"mcp <- {message.get('method')}", file=sys.stderr, flush=True)
                response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> int:
    # MCP stdio is UTF-8 JSON-RPC in practice; never emit GBK on a Chinese
    # Windows console when a client pipes the process.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    auto_start = str(os.environ.get("SLIDESENTINEL_AUTO_START", "1")).lower() not in (
        "0", "false", "no", "off",
    )
    server = MCPServer(
        base_url=os.environ.get("SLIDESENTINEL_BASE_URL")
        or os.environ.get("PPT_AGENT_BASE_URL"),
        auto_start=auto_start,
        output_dir=os.environ.get("SLIDESENTINEL_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
    )
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Locate (and optionally start) the local SlideSentinel service.

The tool shell deliberately does not embed the FastAPI app.  It talks to the
same single-port service the web UI uses, so tasks, rendering, SQLite and the
PowerPoint COM worker have exactly one owner.

Auto-start is limited to loopback URLs and is implemented by calling the
existing ``scripts/start_app.ps1 -NoBrowser``.  Remote URLs are never started.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import Client, ServiceUnavailable, normalize_base_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_app.ps1"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback(base_url: str) -> bool:
    try:
        host = (urlparse(normalize_base_url(base_url)).hostname or "").lower()
    except Exception:
        return False
    return host in _LOOPBACK_HOSTS


def _log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, file=sys.stderr, flush=True)


def wait_for_health(client: Client, timeout: float = 30.0,
                    interval: float = 0.6) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(1.0, timeout)
    while True:
        try:
            return client.health(timeout=2.0)
        except ServiceUnavailable:
            pass
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _tail(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    return text[-limit:]


def _start_local(port: int, timeout: float, quiet: bool) -> None:
    if sys.platform != "win32":
        raise ServiceUnavailable(
            "", "当前不是 Windows，无法自动启动 PowerPoint 渲染服务。",
            "请在 Windows 上运行 SlideSentinel.cmd，或用 --base-url 指向已运行的服务。",
        )
    if not START_SCRIPT.is_file():
        raise ServiceUnavailable(
            "", f"找不到启动脚本：{START_SCRIPT}",
            "请在完整的 agent_app 目录中运行工具。",
        )
    _log(f"服务未运行，正在启动（端口 {port}）……首次启动可能需要安装依赖。", quiet)
    cmd = [
        "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(START_SCRIPT), "-Port", str(port), "-NoBrowser",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True,
            timeout=max(30.0, timeout),
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceUnavailable(
            f"http://127.0.0.1:{port}",
            "启动脚本超时，服务可能仍在初始化。",
            f"请稍后重试，或查看 {PROJECT_ROOT / '.run'} 下的日志。",
        ) from exc
    stdout = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
    stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
    # A non-zero exit can still mean "the port was already being opened"; the
    # health poll is authoritative, so the caller keeps waiting after this.
    if proc.returncode not in (0, 1):
        raise ServiceUnavailable(
            f"http://127.0.0.1:{port}",
            f"启动脚本失败（exit {proc.returncode}）。",
            _tail(stderr or stdout) or "请运行 scripts/start_backend.ps1 查看详细错误。",
        )
    if stdout:
        _log(_tail(stdout, 400), quiet)


def ensure_service(
    base_url: str | None = None,
    *,
    auto_start: bool = True,
    timeout: float = 300.0,
    quiet: bool = False,
) -> Client:
    """Return a healthy client or raise ``ServiceUnavailable`` with a hint."""
    url = normalize_base_url(base_url)
    client = Client(url)
    if wait_for_health(client, timeout=3.0):
        return client

    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 8000)
    if not auto_start or not is_loopback(url):
        raise ServiceUnavailable(
            url,
            "SlideSentinel 服务未运行。",
            "请先双击 SlideSentinel.cmd，或运行 scripts/start_app.ps1 -NoBrowser。",
        )

    _start_local(port, timeout=timeout, quiet=quiet)
    deadline = time.monotonic() + max(10.0, timeout)
    while time.monotonic() < deadline:
        if wait_for_health(client, timeout=2.0):
            _log(f"服务已就绪：{url}", quiet)
            return client
        time.sleep(1.0)

    raise ServiceUnavailable(
        url,
        "启动后仍未通过健康检查。",
        f"请查看 {PROJECT_ROOT / '.run' / 'backend.err.log'}，或运行 scripts/start_backend.ps1 前台排查。",
    )


def service_snapshot(client: Client) -> dict[str, Any]:
    """Compact service block used by ``ppt_capabilities`` and ``doctor``."""
    capabilities = client.capabilities()
    modules = capabilities.get("modules") or {}
    return {
        "base_url": client.base_url,
        "health": client.health(timeout=3.0),
        "app": capabilities.get("app") or {},
        "modules": {
            key: {
                "state": value.get("state") if isinstance(value, dict) else value,
                "name": value.get("name") if isinstance(value, dict) else None,
                "detail": value.get("detail") if isinstance(value, dict) else None,
                "requires": value.get("requires") if isinstance(value, dict) else None,
            }
            for key, value in modules.items()
        },
        "limits": capabilities.get("limits") or {},
        "labels": capabilities.get("labels") or [],
        "privacy": capabilities.get("privacy"),
        "task_count": capabilities.get("task_count"),
        "demo": capabilities.get("demo") or {},
    }

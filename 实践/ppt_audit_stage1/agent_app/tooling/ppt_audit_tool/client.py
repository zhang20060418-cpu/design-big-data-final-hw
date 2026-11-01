"""Small standard-library HTTP client for the local SlideSentinel service.

No third-party dependency on purpose: the tool must work with the same
``backend/.venv`` the application already requires, or with any Python 3.11+
on PATH.  The client never imports the FastAPI app and never touches
``backend/data`` directly.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class ToolError(RuntimeError):
    """Base class for errors the CLI/MCP can present as structured output."""

    code = "tool_error"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self)}


class ServiceUnavailable(ToolError):
    code = "service_unavailable"

    def __init__(self, base_url: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.base_url = base_url
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "base_url": self.base_url,
            "hint": self.hint,
        }


class ApiError(ToolError):
    code = "api_error"

    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.api_code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.api_code or self.code,
            "message": str(self),
            "http_status": self.status,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class InputError(ToolError):
    code = "invalid_input"


def normalize_base_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        text = DEFAULT_BASE_URL
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InputError(f"Base URL 不合法：{value!r}")
    return text


def _multipart(
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
    file_type: str,
) -> tuple[bytes, str]:
    boundary = "----SlideSentinel" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    safe_name = os.path.basename(file_name).replace('"', "_").replace("\r", "_").replace("\n", "_")
    chunks.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {file_type}\r\n\r\n".encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = float(timeout)

    # ------------------------------------------------------------- transport
    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            if clean:
                url += "?" + urlencode(clean, doseq=True)
        return url

    def _http_error(self, exc: HTTPError, url: str) -> ApiError:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        message = f"HTTP {exc.code}：{url}"
        code = "http_error"
        detail: Any = None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                message = str(detail.get("message") or message)
            elif isinstance(detail, str):
                message = detail
        elif raw:
            snippet = raw.decode("utf-8", "replace").strip()
            if snippet:
                message = f"{message}：{snippet[:300]}"
        return ApiError(exc.code, code, message, detail)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self._url(path, query)
        request = Request(url, data=data, method=method.upper(),
                          headers={"Accept": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(502, "invalid_json", f"服务返回了非 JSON 内容：{exc}") from exc
        except HTTPError as exc:
            raise self._http_error(exc, url) from exc
        except (URLError, socket.timeout, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServiceUnavailable(
                self.base_url,
                f"无法连接 SlideSentinel 服务（{reason}）。",
                "请先双击 SlideSentinel.cmd，或运行 scripts/start_app.ps1。",
            ) from exc

    def _download(
        self,
        path: str,
        destination: Path,
        *,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Path:
        url = self._url(path, query)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urlopen(url, timeout=timeout or self.timeout) as response:
                with destination.open("wb") as out:
                    shutil.copyfileobj(response, out)
        except HTTPError as exc:
            raise self._http_error(exc, url) from exc
        except (URLError, socket.timeout, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServiceUnavailable(
                self.base_url,
                f"下载失败：无法连接服务（{reason}）。",
                "请确认服务仍在运行。",
            ) from exc
        return destination

    # ---------------------------------------------------------------- system
    def health(self, timeout: float = 3.0) -> dict[str, Any]:
        return self._request("GET", "/api/system/health", timeout=timeout) or {}

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/system/capabilities") or {}

    # ----------------------------------------------------------------- tasks
    def create_task(
        self,
        file_path: str | Path,
        *,
        name: str = "",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise InputError(f"文件不存在：{path}")
        if path.suffix.lower() != ".pptx":
            raise InputError("只支持 .pptx 文件；旧版 .ppt 需要先另存为 .pptx。")
        payload = path.read_bytes()
        fields = {
            "name": str(name or path.stem)[:120],
            "options": json.dumps(options or {}, ensure_ascii=False),
        }
        body, content_type = _multipart(fields, "file", path.name, payload, PPTX_MIME)
        return self._request(
            "POST", "/api/tasks", data=body,
            headers={"Content-Type": content_type},
            timeout=max(self.timeout, 120.0),
        ) or {}

    def get_task(self, task_id: str, mode: str = "real") -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{quote(task_id)}",
                             query={"mode": mode}) or {}

    def list_tasks(self, mode: str = "real", limit: int = 30,
                   offset: int = 0) -> dict[str, Any]:
        return self._request("GET", "/api/tasks",
                             query={"mode": mode, "limit": limit, "offset": offset}) or {}

    def report_data(self, task_id: str, mode: str = "real") -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{quote(task_id)}/report/data",
                             query={"mode": mode}) or {}

    def report_url(self, task_id: str, fmt: str = "html", mode: str = "real") -> str:
        suffix = "&mode=demo" if mode == "demo" else ""
        return (f"{self.base_url}/api/tasks/{quote(task_id)}/report"
                f"?format={fmt}&download=1{suffix}")

    def download_report(self, task_id: str, fmt: str, destination: str | Path,
                        mode: str = "real") -> Path:
        return self._download(
            f"/api/tasks/{quote(task_id)}/report",
            Path(destination),
            query={"format": fmt, "download": 1, "mode": mode},
        )

    def image_url(self, task_id: str, slide_index: int, *,
                  mode: str = "real", kind: str = "render") -> str:
        suffix = "&mode=demo" if mode == "demo" else ""
        return (f"{self.base_url}/api/tasks/{quote(task_id)}/slides/{int(slide_index)}/image"
                f"?kind={quote(kind)}{suffix}")

    # --------------------------------------------------------------- repairs
    def start_repair(
        self,
        task_id: str,
        *,
        mode: str = "real",
        backend: str = "api",
        review: bool = False,
        rounds: int = 0,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/tasks/{quote(task_id)}/repairs",
            query={"mode": mode, "backend": backend, "review": 1 if review else 0,
                   "rounds": int(rounds or 0)},
            data=b"",
        ) or {}

    def get_repair(self, task_id: str, repair_id: str,
                   mode: str = "real") -> dict[str, Any]:
        return self._request(
            "GET", f"/api/tasks/{quote(task_id)}/repairs/{quote(repair_id)}",
            query={"mode": mode},
        ) or {}

    def download_repair_file(self, task_id: str, repair_id: str,
                             destination: str | Path, mode: str = "real") -> Path:
        return self._download(
            f"/api/tasks/{quote(task_id)}/repairs/{quote(repair_id)}/file",
            Path(destination),
            query={"mode": mode},
        )

    def repair_bundle_url(self, task_id: str, repair_id: str, mode: str = "real") -> str:
        suffix = "&mode=demo" if mode == "demo" else ""
        return (f"{self.base_url}/api/tasks/{quote(task_id)}/repairs/"
                f"{quote(repair_id)}/bundle?download=1{suffix}")

    # ------------------------------------------------------------------- demo
    def create_demo(self, scenario: str = "full", name: str = "") -> dict[str, Any]:
        body = json.dumps({"scenario": scenario, "name": name}, ensure_ascii=False).encode("utf-8")
        return self._request(
            "POST", "/api/demo/tasks", data=body,
            headers={"Content-Type": "application/json"},
        ) or {}

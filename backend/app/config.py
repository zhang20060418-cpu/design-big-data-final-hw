"""Application configuration and filesystem layout.

All runtime artefacts live under ``backend/data``.
Runtime artefacts never write to research datasets. The six-detector adapter
reads only versioned implementation code, never benchmark annotations.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # repository/backend
PROJECT_ROOT = APP_DIR.parent                              # repository root

try:  # optional: allow a local .env for limits and ports
    from dotenv import load_dotenv
    load_dotenv(APP_DIR / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

DATA_DIR = Path(os.environ.get("PPT_AGENT_DATA_DIR", APP_DIR / "data")).resolve()

TASKS_DIR = DATA_DIR / "tasks"
DEMO_TASKS_DIR = DATA_DIR / "demo_tasks"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "agent.db"
DEMO_DB_PATH = DATA_DIR / "demo.db"
SECRETS_PATH = DATA_DIR / "secrets.json"
TEMPLATE_DIR = APP_DIR / "app" / "templates"
FIXTURE_DIR = APP_DIR.parent / "fixtures"
FRONTEND_DIST = APP_DIR.parent / "frontend" / "dist"


def frontend_build_id() -> str:
    """``index.html`` 引用的入口 chunk 文件名，用作前端构建指纹。

    浏览器可能命中旧版 ``index.html`` 的磁盘缓存，导致界面停在上一次构建。
    前端把自己的 chunk 名与该指纹比对，不一致时提示刷新。
    """
    try:
        html = (FRONTEND_DIST / "index.html").read_text(encoding="utf-8")
    except OSError:
        return ""
    for token in html.replace('"', " ").replace("'", " ").split():
        if token.startswith("/assets/index-") and token.endswith(".js"):
            return token.rsplit("/", 1)[-1]
    return ""

MAX_UPLOAD_BYTES = int(os.environ.get("PPT_AGENT_MAX_UPLOAD_MB", "80")) * 1024 * 1024
MAX_SLIDES = int(os.environ.get("PPT_AGENT_MAX_SLIDES", "200"))
MAX_PAGES_PER_TASK = 200
RENDER_LONG_EDGE = int(os.environ.get("PPT_AGENT_RENDER_LONG_EDGE", "1920"))
THUMB_LONG_EDGE = 640
RENDER_TIMEOUT_S = int(os.environ.get("PPT_AGENT_RENDER_TIMEOUT", "420"))
MAX_CONCURRENT_TASKS = int(os.environ.get("PPT_AGENT_MAX_CONCURRENT_TASKS", "2"))

ALLOWED_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]

DEFECT_TYPES = [
    "improper_font_sizing",
    "occluded_content",
    "cluttered_layout",
    "content_overflow_cutoff",
    "excessive_whitespace",
    "generative_design_anomaly",
]

DEFECT_LABELS_ZH = {
    "improper_font_sizing": "字号不合理",
    "occluded_content": "内容遮挡",
    "cluttered_layout": "布局拥挤",
    "content_overflow_cutoff": "内容溢出或裁切",
    "excessive_whitespace": "过分留白",
    "generative_design_anomaly": "异常设计风险",
}

STAGE_DEFINITIONS = [
    ("validate", "文件检查"),
    ("parse", "PPTX 解析"),
    ("render", "页面渲染"),
    ("structure", "结构规则检查"),
    ("visual", "视觉模型检测"),
    ("mllm", "多模态按需复核"),
    ("merge", "结果整合"),
    ("report", "报告生成"),
]


@dataclass(frozen=True)
class Settings:
    app_name: str = "SlideSentinel"
    app_subtitle: str = "AI PPT 多层质量审查智能体"
    version: str = "4.4B–4.4C"
    data_dir: Path = field(default=DATA_DIR)
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_slides: int = MAX_SLIDES
    render_long_edge: int = RENDER_LONG_EDGE
    render_timeout_s: int = RENDER_TIMEOUT_S
    max_concurrent_tasks: int = MAX_CONCURRENT_TASKS


settings = Settings()


def ensure_dirs() -> None:
    for p in (DATA_DIR, TASKS_DIR, DEMO_TASKS_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)

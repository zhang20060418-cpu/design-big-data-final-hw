"""SlideSentinel 工具外壳。

这一层只做“调用契约”，不复制审查逻辑：
- CLI / MCP 通过本机 HTTP API 调用现有 FastAPI 服务；
- 不导入 ``backend.app``，不改前后端、数据库或任务目录；
- 输出稳定、紧凑的 JSON，供其他智能体或脚本消费。
"""
from __future__ import annotations

from .contract import CONTRACT_VERSION, TOOL_NAME, TOOL_VERSION

__all__ = ["CONTRACT_VERSION", "TOOL_NAME", "TOOL_VERSION"]

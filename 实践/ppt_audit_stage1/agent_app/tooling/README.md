# SlideSentinel 工具外壳（CLI / MCP）

这一层不是第二个应用，也没有重写审查逻辑。它把现有的 FastAPI 服务包装成
**一次调用就出结果**的工具，供命令行、脚本和其他智能体使用：

```text
CLI / MCP / 其他 Agent
        │
        ▼
tooling/ppt_audit_tool        # 上传、等待、取报告、结构化输出
        │  HTTP（同源 / 本机）
        ▼
现有 backend + frontend + PowerPoint COM + ml_stage4
```

## 边界

- **不改** `backend/`、`frontend/`、数据库、任务目录和现有报告；
- 不直接读写 `backend/data`，所有任务仍由现有服务执行；
- 不复制六类检测、不内置模型、不绕过 PowerPoint；
- 真实文件审查需要 **Windows + 桌面版 PowerPoint**；
- 六类检测额外依赖同项目的 `ml_stage4/.venv`，缺失时如实返回 `unsupported`；
- `deep` 档需要已配置的多模态 API，未配置时不会伪造复核结果。

## 目录

```text
agent_app/
├─ ppt-audit.cmd                 # CLI 双击/命令行入口
├─ ppt-audit-mcp.cmd             # MCP stdio 入口
└─ tooling/
   ├─ README.md                  # 本文件
   ├─ pyproject.toml             # 可选 pip install -e tooling
   ├─ ppt_audit_tool/
   │  ├─ cli.py                  # 参数、输出、退出码
   │  ├─ client.py               # 标准库 HTTP 客户端
   │  ├─ contract.py             # 三个 profile + 稳定 JSON 契约
   │  ├─ service.py              # 服务发现与自动启动
   │  ├─ workflows.py            # audit/repair/status 高层流程
   │  └─ mcp_server.py           # JSON-RPC over stdio
   ├─ examples/
   │  └─ mcp-config.example.json
   └─ tests/                     # 19 项标准库 unittest
```

## 快速开始

先启动应用（任选一种）：

```powershell
# 双击
SlideSentinel.cmd

# 或命令行
.\scripts\start_app.ps1
```

然后：

```powershell
# 1. 看当前机器能做什么
.\ppt-audit.cmd capabilities

# 2. 看三档审查配置
.\ppt-audit.cmd profiles

# 3. 审查一份 PPTX
.\ppt-audit.cmd audit "C:\path\to\deck.pptx" --profile standard
```

如果服务未运行，CLI 默认会调用现有 `scripts/start_app.ps1 -NoBrowser`
自动拉起；首次启动需要安装 Python 依赖，可能较慢。加 `--no-auto-start`
可以只检查、不启动。

## 三个 profile

| profile | 包含 | 跳过 | 适用 |
|---|---|---|---|
| `quick` | 结构规则（R01–R05） | 六类检测、CLIP、LLM 复核 | 快速初筛、无 ML 环境 |
| `standard` | 结构规则 + 六类专用检测 | CLIP、LLM 复核 | 常规完整跑通，不需要 API Key |
| `deep` | 结构 + 六类 + CLIP + LLM 按需复核 | — | 需要已配置多模态 API |

> `quick` 仍会渲染页面，因为现有流水线在结构检查前统一渲染；它只是跳过
> 六类检测和 LLM。这个限制在 `profiles` 输出里也写明了。

## 常用命令

```powershell
# 只提交，不等待
.\ppt-audit.cmd audit .\deck.pptx --profile standard --no-wait

# 继续查询
.\ppt-audit.cmd status <task_id>

# 下载报告（html / json / pdf）
.\ppt-audit.cmd report <task_id> --type html --out .\out\report.html

# 追问单条问题：完整证据、坐标、复核历史、页面图 URL
.\ppt-audit.cmd explain <task_id> <issue_id>

# AI 修复：默认只做安全的几何修复
.\ppt-audit.cmd repair <task_id> --review --rounds 2

# 下载 fixed.pptx
.\ppt-audit.cmd repair <task_id> --download-fixed

# 预置演示任务，不读取本地文件
.\ppt-audit.cmd demo --scenario full

# 最近任务
.\ppt-audit.cmd list --mode real --limit 10
```

### 输出与退出码

- **stdout 只输出一份 UTF-8 JSON**（`--format json`，默认）；
- 进度和服务启动信息只写 **stderr**，不污染管道；
- `--format text` 是给人看的短摘要，字段仍以 JSON 为准；
- 退出码：`0` 成功，`1` 任务/修复失败或超时，`2` 服务/接口错误，`3` 输入文件错误。

典型结果：

```json
{
  "ok": true,
  "contract": "1.0",
  "tool": "slidesentinel",
  "version": "1.0.0",
  "task_id": "32e98cfeed9a494b",
  "status": "completed",
  "profile": "standard",
  "document": {"file_name": "deck.pptx", "slide_count": 10},
  "summary": {"visible_issue_count": 4, "by_type": {"improper_font_sizing": 2}},
  "issues": [
    {
      "issue_id": "R03-s0004-01",
      "slide_index": 4,
      "defect_type": "improper_font_sizing",
      "severity": "high",
      "evidence": "最小显式字号为 8pt（低于 9pt 参考线）",
      "bbox": {"x": 0.9, "y": 1.7, "w": 3.2, "h": 1.1},
      "sources": ["structure_rule"]
    }
  ],
  "pages": [{"index": 4, "issue_count": 1, "issue_ids": ["R03-s0004-01"]}],
  "artifacts": {
    "report_json": "slidesentinel-output/<task_id>/report.json",
    "report_html": "slidesentinel-output/<task_id>/report.html"
  },
  "limitations": ["结构规则输出的是候选，不等同于已确认缺陷。"]
}
```

`--issues` 控制明细数量：`summary`（默认，前 50 条）、`all`（最多 500 条）、
`none`。`issues_total` 始终是可见问题总数，`issues_truncated` 表示被截断的数量。

## MCP 接入

MCP 服务使用标准 JSON-RPC over stdio，不依赖 `mcp` Python 包：

```powershell
.\ppt-audit-mcp.cmd
```

以 WorkBuddy / Claude Desktop 这类客户端为例，把下面内容合并到
`~/.workbuddy/mcp.json` 或对应客户端的 `mcpServers`：

```json
{
  "mcpServers": {
    "slidesentinel": {
      "command": "cmd",
      "args": ["/c", "D:\\path\\to\\agent_app\\ppt-audit-mcp.cmd"],
      "env": {
        "SLIDESENTINEL_BASE_URL": "http://127.0.0.1:8000",
        "SLIDESENTINEL_AUTO_START": "1",
        "SLIDESENTINEL_OUTPUT_DIR": "D:\\path\\to\\agent_app\\slidesentinel-output"
      }
    }
  }
}
```

> 不要直接把示例路径复制使用；把 `D:\path\to\agent_app` 换成实际目录。
> 修改 MCP 配置后需要重启客户端或重新加载 MCP。

### 暴露的 8 个工具

| 工具 | 作用 |
|---|---|
| `ppt_capabilities` | 服务、六类、CLIP、LLM 的可用状态；调用审查前先看 |
| `ppt_audit` | 上传并审查，返回结构化问题清单 |
| `ppt_status` | 查询进度；已完成时返回报告摘要 |
| `ppt_report` | 下载 html / json / pdf |
| `ppt_repair` | 对已完成任务做安全修复 |
| `ppt_explain` | 按 issue_id 返回完整证据与页面图 |
| `ppt_list` | 列出真实/演示任务 |
| `ppt_demo` | 创建预置演示任务 |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SLIDESENTINEL_BASE_URL` | `http://127.0.0.1:8000` | 服务地址 |
| `SLIDESENTINEL_AUTO_START` | `1` | 服务未运行时是否自动启动；`0` 关闭 |
| `SLIDESENTINEL_OUTPUT_DIR` | `slidesentinel-output` | 报告与 `result.json` 的输出目录 |
| `SLIDESENTINEL_MCP_LOG` | 空 | `1` 时把收到的 MCP 方法名写到 stderr |

## 典型智能体流程

```text
1. ppt_capabilities
   ├─ six_detector=unsupported → 用 standard 或 quick
   ├─ mllm=not_configured      → 用 standard 或 quick
   └─ 都 ready                 → 可用 deep

2. ppt_audit(file_path, profile="standard")
   └─ 返回 task_id + summary + issues

3. ppt_explain(task_id, issue_id)     # 解释单条证据
4. ppt_report(task_id, format="html") # 给人看的报告
5. ppt_repair(task_id, review=true)   # 只自动修可证明的几何问题
```

## 测试

```powershell
cd tooling
..\backend\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

当前 19 项，覆盖：

- 三个 profile 的一致性；
- 报告投影与人工否决/复核否决的可见性规则；
- `issues` 三种模式与截断；
- MCP initialize / tools/list / tools/call / 未知方法 / 通知；
- multipart 编码与 Base URL 规范化；
- CLI 参数解析。

## 实测（2026-11-01）

在独立数据目录、独立端口 8011 的临时实例上验证（不写入真实 `backend/data`，测完已停止）：

| 场景 | 结果 |
|---|---|
| `quick` 审查 `fixtures/demo_deck.pptx` | 10 页；`render_failed=0`；4 条结构问题、4 个问题页；六类/CLIP/LLM 均未运行；HTML 62 KB、JSON 127 KB |
| `repair --backend rule` | 结构 4 → 0；applied 4 / manual 0 / skipped 0；新增 0；生成 `fixed.pptx` |
| MCP | `initialize` 协商 2025-06-18；`tools/list` 返回 8 个工具；`ppt_capabilities` 的 `isError=false` |
| CLI 入口 | `ppt-audit.cmd` / `ppt-audit-mcp.cmd` 均可启动 |

临时实例使用 `PPT_AGENT_DATA_DIR=.local/tool_e2e_data`；主服务 8000 未重启、未写入测试任务。

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `service_unavailable` | 8000 未启动，或端口不对 | 先运行 `SlideSentinel.cmd`；或传 `--base-url` |
| `six_detector: unsupported` | `ml_stage4/.venv` 或冻结实现缺失 | 用 `standard` 仍可跑结构 + 六类降级说明；或用 `quick` |
| `render_failed` | 没有桌面版 PowerPoint / COM 被拦截 | 安装 PowerPoint；WPS 不能替代 COM 渲染 |
| `mllm: not_configured` | 未填写 API | 用 `standard`/`quick`，或在设置页配置 |
| 修复后仍有问题 | 视觉、拥挤、留白、异常设计按设计不自动改 | 用 `ppt_explain` 看证据，人工处理 |
| `file_too_large` | 超过 80 MB | 拆分文稿或调大 `PPT_AGENT_MAX_UPLOAD_MB` |
| `unsupported_format` | 上传了 `.ppt` | 先另存为 `.pptx` |

## Git 与运行数据

- `slidesentinel-output/` 是工具运行产物，已在根 `.gitignore` 中排除；
- `tooling/**/__pycache__/` 不会提交；
- 工具本身不创建数据库、不上传文件到外部服务；
- 真实任务仍保存在现有 `backend/data`，不会进入 Git。

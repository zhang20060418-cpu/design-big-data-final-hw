# CLI 与 MCP

CLI 和 MCP 都通过 HTTP 调用当前工作台，不直接修改任务数据库。真实审查沿用后端的 PowerPoint 渲染、检测与修复流程。

## 命令行

在仓库根目录启动工作台后运行：

```powershell
.\ppt_audit.cmd capabilities
.\ppt_audit.cmd profiles
.\ppt_audit.cmd audit .\fixtures\demo_deck.pptx --profile standard
.\ppt_audit.cmd status <task_id>
.\ppt_audit.cmd report <task_id> --type html
.\ppt_audit.cmd explain <task_id> <issue_id>
.\ppt_audit.cmd repair <task_id> --backend rule --download-fixed
```

| 档位 | 内容 |
| --- | --- |
| `quick` | 结构初筛，跳过六类专用检测和模型复核。 |
| `standard` | 结构规则与六类专用检测，不调用外部模型。 |
| `deep` | 加入已配置的 CLIP 辅助和多模态复核；所需模型与 API 应提前安装、配置。 |

`stdout` 输出 UTF-8 JSON，进度写入 `stderr`。加 `--format text` 可查看短摘要，`--no-wait` 可只提交任务。报告默认保存到根目录 `slidesentinel-output/`，该目录不进入 Git。

需要模型修复时使用 `--backend api`，并提前配置自己的 API；`--review` 可启用多轮复核。

服务未运行时工具可尝试启动本地服务。使用其他端口时，将参数放在子命令之后：

```powershell
.\ppt_audit.cmd capabilities --base-url http://127.0.0.1:8010 --no-auto-start
```

## MCP 配置

入口为根目录 `ppt_audit_mcp.cmd`，使用 stdio JSON-RPC。将 [配置示例](../tooling/examples/mcp_config.example.json) 中的路径改为本机仓库绝对路径，再填入 MCP 客户端：

```json
{
  "mcpServers": {
    "slidesentinel": {
      "command": "cmd",
      "args": ["/c", "C:\\projects\\slide_sentinel\\ppt_audit_mcp.cmd"],
      "env": {"SLIDESENTINEL_BASE_URL": "http://127.0.0.1:8000"}
    }
  }
}
```

| 工具 | 作用 |
| --- | --- |
| `ppt_capabilities` | 查询当前机器可用的审查能力。 |
| `ppt_audit` | 上传并审查 PPTX。 |
| `ppt_status` | 查询任务状态与结果摘要。 |
| `ppt_report` | 获取或下载报告。 |
| `ppt_repair` | 对已有任务执行修复。 |
| `ppt_explain` | 解释指定问题。 |
| `ppt_list` | 查询任务列表。 |
| `ppt_demo` | 创建明确标记的演示任务。 |

典型调用顺序为：`ppt_capabilities` 确认环境，`ppt_audit` 提交本机文件，`ppt_status` 查看进度，完成后用 `ppt_report`、`ppt_explain` 或 `ppt_repair` 处理结果。设置 `SLIDESENTINEL_AUTO_START=0` 可关闭自动启动。

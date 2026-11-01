# SlideSentinel

**PPTX 设计质量审查智能体**：上传演示文稿，定位问题页面与对象，查看证据、获取修改建议，并导出报告或尝试修复。网页工作台、命令行和 MCP 工具共用同一个本地服务。

## 快速开始

运行环境：**Windows 10/11、Python 3.12 或 3.13、桌面版 Microsoft PowerPoint**。首次安装需要联网；仓库已包含前端成品，普通使用不需要 Node.js。

```powershell
git clone --depth 1 https://github.com/zhang20060418-cpu/design-big-data-final-hw.git slide_sentinel
cd slide_sentinel
.\start_app.cmd
```

也可以下载仓库后，直接双击根目录的 **`start_app.cmd`**。首次启动自动安装后端及检测依赖，随后打开 <http://127.0.0.1:8000/>。停止服务使用 **`stop_app.cmd`**。

进入工作台后，可先查看内置演示，或上传 `fixtures/demo_deck.pptx` 运行真实审查。结构规则、六类检测和规则修复不需要 API Key；AI 复核、追问与 AI 修复需要在“设置”中配置自己的多模态模型服务。

## 主要功能

- **问题定位**：结合 PPTX 对象结构、PowerPoint 实际排版和渲染图，给出页面、对象与坐标证据。
- **六类检测**：检查字号、遮挡、拥挤、溢出、留白和异常设计风险，结合跨页结构判断。
- **智能复核与修复**：按需调用多模态模型，解释候选问题、生成建议、执行修复并比较前后结果。
- **报告与工具调用**：导出 JSON / HTML / PDF，通过 CLI 或 MCP 接入其他智能体。

## 目录

```text
slide_sentinel/
├── start_app.cmd          # 启动工作台
├── stop_app.cmd           # 停止工作台
├── ppt_audit.cmd          # 命令行入口
├── ppt_audit_mcp.cmd      # MCP 入口
├── backend/               # API、审查流程、报告与修复
├── frontend/              # 工作台源码与可直接使用的 dist
├── detector_runtime/      # 冻结检测实现、特征计算与模型资源
├── tooling/               # CLI / MCP 实现与测试
├── scripts/               # 安装、启动与模型下载脚本
├── fixtures/              # 合成演示样例
└── docs/                  # 使用、架构、模型与开发文档
```

说明文档统一放在 `docs/`：

- [工作台使用](docs/user_guide.md)
- [CLI 与 MCP 接入](docs/cli_mcp.md)
- [实现流程与架构](docs/system_architecture.md)
- [缺陷类型与结果解释](docs/defect_taxonomy.md)
- [模型资源与可选 CLIP 安装](docs/model_assets.md)
- [开发与测试](docs/development.md)

## 运行范围与数据

完整预览和排版测量需要桌面版 PowerPoint；PDF 导出需要 Edge 或 Chrome。复杂组合、旋转对象和纯图片页面存在检测范围限制，图片语义伪影检测尚未实现，结果会保留未知、未检查和不支持等状态。

上传文件、任务记录、设置和密钥只保存在本机 `backend/data/`，已从 Git 排除。启用 AI 复核或修复时，会向配置的模型服务发送对应页面内容。可选 CLIP 权重、个人文稿、报告和录屏均不随代码分发。

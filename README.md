# SlideSentinel：PPTX 设计质量审查智能体

SlideSentinel 是一个在本机运行的课程项目。上传可编辑 PPTX 后，工作台结合 OpenXML 结构、PowerPoint 实际排版与页面渲染图，定位问题页面和对象，汇总证据、审查建议与报告，并提供规则修复及按需 AI 复核、修复。网页、CLI 和 MCP 共用同一后端。

## 首次运行

推荐 **Windows 10/11 + Python 3.12 或 3.13 + 桌面版 Microsoft PowerPoint**。首次安装依赖需要联网。仓库附带前端成品，普通使用无需安装 Node.js；修改前端后才需要 Node.js 20+。

从 GitHub 下载完整仓库，或执行以下命令（私有仓库须先获得访问权限）：

```powershell
git clone --depth 1 https://github.com/zhang20060418-cpu/design-big-data-final-hw.git
cd design-big-data-final-hw\实践\ppt_audit_stage1\agent_app
.\SlideSentinel.cmd
```

也可以在资源管理器中双击 `实践/ppt_audit_stage1/agent_app/SlideSentinel.cmd`。首次启动会安装后端依赖及六类检测的 NumPy/SciPy/Pillow 环境，然后打开 <http://127.0.0.1:8000/>。请等安装完成；失败后可再次运行。停止应用使用同目录的 `SlideSentinel.Stop.cmd`。

1. 可先使用首页内置演示查看交互流程；演示模式有明确标记。
2. 真实运行可上传自己的 PPTX，或使用仓库中的合成样例 `agent_app/fixtures/demo_deck.pptx`。
3. 检查结果中查看页面预览、问题对象与证据，按需导出 JSON / HTML / PDF 报告，或尝试修复并查看前后差异。

没有桌面版 PowerPoint 时，部分结构检查仍可运行；页面预览、实际排版测量和依赖渲染的检测会显示不可用。PDF 导出需要本机 Edge 或 Chrome。当前实现以 Windows 环境为目标。

## 检测与智能体功能

| 能力 | 安装与配置 |
| --- | --- |
| 结构规则与六类专用检测 | 一键启动自动安装；不需要 API Key。六类覆盖字号、遮挡、拥挤、溢出、留白和异常设计风险，保留各项可判范围。 |
| 证据报告与规则修复 | 随应用提供；实际效果受 PPTX 对象结构影响。 |
| 多模态复核、追问与 AI 修复 | 在工作台“设置”中填入自己的兼容 API 地址、模型与 API Key。未配置时不执行真实模型调用。 |
| 冻结 CLIP512 + PNG10 辅助分类 | 默认关闭；按下文安装公开权重后手动启用。辅助四类页面风险，不替代六类专用检测。 |

异常设计中的图片语义伪影检测尚未实现；纯图片页面、复杂组合、旋转与继承属性等存在适用范围限制。结果会区分候选、未知、未检查与不支持，具体解释见[应用说明](实践/ppt_audit_stage1/agent_app/README.md)。

API Key 由使用者在本机设置，不随仓库提供。上传文件、任务数据库、设置、密钥和导出的结果均写入本机 `agent_app/backend/data/`，该目录已屏蔽；也可以用 `PPT_AGENT_DATA_DIR` 指向自己的数据目录。使用外部多模态服务时，所选页面内容会发送给配置的服务。

## CLI 与 MCP

先启动工作台，在 `agent_app` 目录运行：

```powershell
.\ppt-audit.cmd capabilities
.\ppt-audit.cmd audit .\fixtures\demo_deck.pptx --profile standard
.\ppt-audit.cmd profiles
```

`quick` 执行结构初筛；`standard` 加入六类检测；`deep` 按配置加入 CLIP 与模型复核。使用 `capabilities` 查看当前机器实际可用的模块。CLI 输出 UTF-8 JSON；MCP 入口 `ppt-audit-mcp.cmd` 提供 `ppt_capabilities`、`ppt_audit`、`ppt_status`、`ppt_report`、`ppt_repair`、`ppt_explain`、`ppt_list`、`ppt_demo` 八个工具。客户端配置、报告下载和修复命令见[工具说明](实践/ppt_audit_stage1/agent_app/tooling/README.md)。

## 可选 CLIP 模型

普通六类检测无需下载 CLIP 权重。需要体验辅助分类时，在 `agent_app` 目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_ml.ps1 -Clip
```

脚本会安装 PyTorch、Transformers 等依赖，从 Hugging Face 下载固定 revision 的公开 `openai/clip-vit-base-patch32` 资源，并按冻结清单校验 SHA256。下载量较大，需要可访问 Hugging Face。权重缓存留在本地，不放入 Git；下载完成后可离线推理，再在工作台中启用 CLIP。项目的小型冻结分类器和特征计算脚本已包含在仓库中。

## 代码与材料

| 位置 | 内容 |
| --- | --- |
| `实践/ppt_audit_stage1/agent_app/` | 当前应用、后端测试、前端源码及成品、合成演示样例、CLI/MCP。 |
| `实践/ppt_audit_stage1/ml_stage4/` | 当前检测实际使用的冻结实现、配置与小型模型；运行清单逐文件校验 SHA256。 |
| `01_标准/` 至 `04_产品/` | 项目设计记录；当前操作步骤以本 README 为准。 |
| `实践/ppt_audit_stage1/` 其余代码 | 早期原型与验证脚本，保留研究过程，不作为当前应用启动入口。 |

个人 PPT、答辩讲稿与报告、录屏及视频工程、上传记录、第三方数据快照、实验缓存、虚拟环境和大模型权重只在本地保存。Git 忽略规则对已入库文件不追溯生效；这次整理同时取消了这些材料在当前版本中的跟踪。旧提交属于历史快照，仍可能包含过去的报告和样本；`--depth 1` 可减少拉取旧快照。

## 开发验证

在 `agent_app` 目录执行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest .\backend\tests -q
Push-Location .\tooling
python -m unittest discover -s tests -q
Pop-Location
```

前端源码有修改时执行 `scripts/start_app.ps1 -Rebuild`；普通拉取使用仓库附带成品。检测依赖的研究实现保持原始字节，校验清单位于 `backend/models/six_runtime_manifest.json`，不要手动修改阈值或用实验结果缓存替代实时检测。

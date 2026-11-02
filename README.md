# SlideSentinel

SlideSentinel 是一个面向演示文稿设计质量的本地审查智能体。系统读取 PPTX 的原生对象、文字属性与空间关系，结合 PowerPoint 实际排版测量和页面渲染图，检查字号、遮挡、拥挤、溢出、留白及异常设计风险。用户可以逐页查看问题对象与证据、获得修改建议、执行规则或 AI 修复，并导出报告与修复后的文稿。

项目包含数据整理、同源页面分组、冻结 CLIP 视觉特征提取、整页与多区域表示比较、PNG 版式特征融合、四标签分类器训练、分组交叉验证、阈值工作点选择和独立文稿验证等研究工作。应用将这些方法与六类专用检测、多模态按需复核、任务恢复及修复验证串成完整流程，同时提供网页、CLI 和 MCP 三种使用方式。本仓库保留实现代码、公开 CLIP 基础权重、项目训练模型、实验协议，以及少量有来源记录的训练与验证样本。

## 快速开始

运行环境：**Windows 10/11、Python 3.12 或 3.13、桌面版 Microsoft PowerPoint**。首次安装 Python 依赖需要联网；仓库包含前端成品，普通使用不需要 Node.js。

CLIP 基础权重通过 Git LFS 管理，克隆时一并获取：

```powershell
git lfs install
git clone --depth 1 https://github.com/zhang20060418-cpu/design-big-data-final-hw.git slide_sentinel
cd slide_sentinel
.\start_app.cmd -WithClip
```

`-WithClip` 会安装 CLIP 推理依赖并校验模型资源，随后打开 <http://127.0.0.1:8000/>。只运行 `start_app.cmd` 时初始化后端及六类检测。停止使用根目录 `stop_app.cmd`。如果通过 ZIP 获取代码，或权重仍是 LFS 指针，`-WithClip` 也能从固定的公开来源补齐并校验权重。

进入工作台后，可上传自己的 PPTX 或 `fixtures/demo_deck.pptx`。在配置页开启 CLIP 辅助即可运行学习模型分支；AI 复核、追问和 AI 修复需要在“设置”中配置自己的多模态服务与 API Key。结构规则、六类专用检测和规则修复不需要 API Key。

## 项目内容与方法入口

| 工作 | 实现或资源 |
| --- | --- |
| PPTX 原生解析、实际排版测量、六类风险筛查 | `backend/app/services/`、`detector_runtime/experiments/` |
| 冻结 CLIP、中心裁剪 / 整页 / 多区域特征比较 | `detector_runtime/scripts/extract_clip_features.py`、`extract_full_slide_features.py`、`extract_multiregion_clip.py` |
| CLIP 基线、PNG10 融合、嵌套分组验证与阈值策略 | `detector_runtime/scripts/train_clip_lr.py`、`train_png_fusion_cv.py`、`optimize_fusion_routing.py` |
| 项目训练模型与公开基础权重 | `research/models/`、`detector_runtime/models/` |
| 精选训练 / 验证数据、来源与实验协议 | `research/sample_data/`、`research/configs/` |
| 按需复核、证据引用、任务恢复与修复前后比较 | `backend/app/services/review_agent.py`、`recovery.py`、`repair.py` |
| CLI / MCP 与结构化输出 | `tooling/`、根目录 `ppt_audit.cmd` / `ppt_audit_mcp.cmd` |

CLIP 视觉编码器使用公开预训练权重，项目训练的是下游分类器与特征融合方案；公开权重的来源、版本和许可均随资源保留。

## 目录

```text
slide_sentinel/
├── start_app.cmd          # 启动工作台，-WithClip 准备完整视觉环境
├── stop_app.cmd           # 停止工作台
├── ppt_audit.cmd          # CLI 入口
├── ppt_audit_mcp.cmd      # MCP 入口
├── backend/               # API、审查流程、报告与修复
├── frontend/              # 工作台源码与成品
├── detector_runtime/      # 冻结实现、特征与训练脚本、运行模型
│   └── models/clip_encoder/ # Git LFS 管理的公开 CLIP 编码器
├── research/              # 训练模型、实验协议与精选公开样本
│   ├── models/
│   ├── configs/
│   ├── sample_data/
│   └── scripts/
├── tooling/               # CLI / MCP 实现与测试
├── scripts/               # 安装、启动、模型验证与下载
├── fixtures/              # 可编辑的合成演示 PPTX
└── docs/                  # 统一的说明文档
```

## 研究样本与验证

精选数据包含 **32 页 train + 8 页 validation**，来自公开 SlideAudit 的 Gemini 来源页面。保留原图、原标注、对象描述、SHA256、项目源组与原有划分；未加入 test 页，同源组不跨集合。它们用于查看数据结构和做小规模运行检查，完整实验的数据规模、方法与结果见[研究方法说明](docs/research_methods.md)。

```powershell
python .\research\scripts\verify_sample_data.py
.\detector_runtime\.venv\Scripts\python.exe .\research\scripts\smoke_clip.py --limit 4
.\ppt_audit.cmd audit .\fixtures\demo_deck.pptx --profile standard
```

第一条验证样本与划分；第二条实际加载 CLIP 和项目分类器进行离线推理；第三条执行真实 PPTX 审查。检查产物写入已忽略的输出目录。

## 文档

- [工作台使用](docs/user_guide.md)
- [CLI 与 MCP 接入](docs/cli_mcp.md)
- [实现流程与架构](docs/system_architecture.md)
- [研究方法与实验资源](docs/research_methods.md)
- [模型资源与完整视觉环境](docs/model_assets.md)
- [公开数据、模型来源与许可](docs/third_party_assets.md)
- [缺陷类型与结果解释](docs/defect_taxonomy.md)
- [开发与测试](docs/development.md)

## 运行范围与数据

完整预览和排版测量需要桌面版 PowerPoint；PDF 导出需要 Edge 或 Chrome。复杂组合、旋转对象和纯图片页面存在检测范围限制，图片语义伪影检测尚未实现；结果保留候选、未知、未检查和不支持等状态。

上传文件、任务记录、设置和密钥只保存在本机 `backend/data/`，不进入 Git。开启外部模型服务后，选中的页面内容会发送给配置的服务。个人文稿、答辩材料、全量数据、特征缓存、预测明细、运行日志和虚拟环境均不随仓库分发。

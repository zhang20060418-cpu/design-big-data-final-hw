# 4.4B–4.4C 集成检查与交付记录

本轮目标是让现有应用形成六类检测、按需复核、追问和报告的可运行闭环。没有训练分类器、修改研究阈值或运行新的 G-2 评价。

## 修改前检查与保护

- 工程仍使用原 FastAPI、React/TypeScript、SQLite 和八阶段执行器。
- 实际业务对象为 Issue/Finding 字典；并不存在背景摘要所称的完整 Claim 类。本轮增加原始观察 Schema，挂接既有 Finding，没有创建第二套应用。
- 原有版本有 JSON/HTML 报告，未发现可运行的 PDF 导出入口；本轮在同一个报告模板上增加本机 Edge/Chrome 打印。
- 修改前后端 10 项 pytest、前端 typecheck/build 均通过。六类能力发布后，原标签数量断言由 4 更新为 6，并保留原四类包含关系检查。
- Git HEAD 为 `db2c569`。应用目录与很多研究工件尚未纳入 Git；已有工作没有 reset、clean、覆盖或批量提交。
- 安全检查点：项目根目录 `_local_checkpoints/4.4BC_20261030_215120/`。包含源码 ZIP、源码 SHA、Git 工作区记录及现有 SQLite 备份；秘密配置不放进源码 ZIP。
- 对保护清单中的 2,168 个研究文件重新计算 SHA：无缺失、无变化，见 `acceptance/research_preservation.json`。

## 各阶段实际交付

| 阶段 | 修改的主要文件 | 已实现功能与真实证据 | 限制与后续边界 |
|---|---|---|---|
| 0 仓库检查 | 本记录、安全检查点 | 完成基线测试、入口核对、研究 SHA 保护 | 没有把未跟踪大数据提交到普通 Git |
| 1 六类接入 | `services/six_detector.py`、`six_native.py`、`six_inference_worker.py`、`renderer.py`、`render_worker.py`、`orchestrator.py`、`schemas.py`、`merge.py` | 真实上传 PPTX；共享一次只读 PowerPoint 打开完成原比例 PNG 与段落测量；隔离调用原 R2 实现；保留五态、版本、原始证据、D6 关联页 | Windows PowerPoint、既有 ML 环境和研究实现须可用；组合、旋转、部分单元格或可见字形无法充分测量时保留未知 |
| 2 专用复核规则 | `llm_rules/` 12 份 JSON、`services/review_agent.py` | general、D1–D6、A01–A05 均带版本；动态注入；严格响应 Schema；无效 JSON、超时、伪造 ID/定位不形成裁决 | 本轮端到端演示用显式 Mock；未验证外部服务的真实视觉判断质量 |
| 3 按需调度 | `ReviewPlanner`、`orchestrator.py`、前端 `ReviewDetails.tsx` | full/selective 两种策略；候选、范围差异、未知补充；跨页和局部证据；调用/页面预算、选择与跳过原因、调用轨迹 | full_review 仍受预算限制；跳过项未被复核，不能保证零漏检；费用缺失显示未知，不臆测 |
| 4 交互与报告 | `services/interaction.py`、任务 API、Results/Report 页面、HTML 模板、`pdf_export.py` | Finding ID 级追问，原 Claim 与初始报告保留；复核历史、跨页跳转、归一化定位；HTML/JSON/PDF 实际导出 | 用户异议不会自动取消候选；不自动编辑 PPT；无实测支持不输出精确参数 |
| 5 验收 | 后端 `tests/`、前端 `e2e/`、`scripts/run_integration_acceptance.py` | 合成边界测试及真实 14 页上传、渲染、检测、Mock 复核、追问、浏览器与导出 | 只验证系统集成功能；功能样例和构造参考指标均不构成人工泛化成绩 |

## 六类检测的实际调用路径

`上传原稿 → 既有 OpenXML 校验/解析 → render_worker → 同次只读 COM 段落测量 → native_contexts.json → 原 ML Python 隔离推理 → 原 L/M/R2 专用函数 → SixDetectorAdapter → 原 Issue/Finding 合并 → 按需复核 → 原报告系统`。

`six_inference_worker.py` 只调用版本化特征/检测函数，不执行研究实验 runner，不读取人工标签、基准清单、历史预测 CSV 或 CLIP 冻结权重。执行时核查实际输入 SHA 与原检测器源文件版本。没有通过样本 ID 匹配历史结果。

版本核验边界：沿用候选 bundle 已登记的源码 SHA；`specialized.json`、`shared_features.json`、`roles.json` 三份配置不在该 bundle 的 expected 映射中。本轮记录其实际 SHA，并以修改前研究保护清单核实未变，不宣称已锁定全部传递依赖和配置。

旧 R01–R05 作为另一证据来源保留。同对象或可比坐标范围的候选可合并，不同位置不能仅凭同标签合并；原 Claim 始终保留。未检出、未知与旧规则告警不一致时记录范围冲突，交给复核核查，不擅自判定任一来源正确。

## 运行演示

- 原生可编辑功能文稿：`fixtures/integration_14_slides.pptx`，14 页；由原应用演示文稿扩展，绝非独立质量评价集。
- 首次真实任务：`8bf29916e0254aa6`，84 条 Claim；9 candidate、63 可判范围未检出、11 unknown、1 unsupported。
- 合并后 10 条 Finding；所有原始观测独立保留。第 6 页验证遮挡定位，第 11 页验证留白补充审查，第 12–14 页验证跨页布局，第 4 页 D5 候选验证用户设计意图追问。
- D2/D5/D6 及 A01 专用规则实际进入复核请求。A05 专项通过真实适配器形状的独立测试验证，语义状态始终 unsupported/unknown。
- 初次 3 次 Mock 复核，加上后续 Mock 追问；Mock 返回 uncertain，实际外部 LLM 调用为 0。浏览器测试也提交了追问，后续历史仍可追溯。
- 原 PPTX SHA 保持不变；实际原图为 1920×1080，完整页面导出，没有画检测框到模型输入。
- 真实报告及截图在 `acceptance/`，运行环境、测试结果、版本摘要见 `validation_results.md` 和 `acceptance/artifact_manifest.json`。

## 明确保留的未实现范围

1. 不把历史 CLIP C 强行启用为默认视觉裁决器；其插件仍可兼容原四类输入。
2. D6-A05 不具备完整伪文字、语义伪影判定。ROI 仅提供给进一步视觉核查。
3. 未配置真实 LLM 时，可用显式 Mock 验证工程链路；Mock 不支持“确认缺陷”或“确认无缺陷”。
4. 不输出 AI 生成率，不保证零漏检，不把 ordinal 风险强度称为校准概率。
5. 没有自动修改 PPT，没有接入新训练流程，没有更改历史基准和人工标签。

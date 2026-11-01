# 可选冻结 CLIP 四类辅助复核接入

日期：2026-10-31。用户在 R2 主检测已接入后，授权把旧 CLIP 实际接入，默认关闭、可手动开启；开启顺序为 R2 六类首筛 → CLIP 四类辅助复核 → LLM 按需复检。本记录是系统接入验收，不是新的分类器性能评价。

## 实际采用的模型

- R2 继续作为六类首筛，不变更实现和冻结阈值。
- CLIP 辅助层沿用原 G-2 预注册冻结 **C**：CLIP 图像向量 512 维 + 原 PNG10 10 维，经原训练集拟合的变换形成 522 维输入，再使用四个冻结逻辑回归分类器。
- 分类器 bundle：`ml_stage4/models/fusion_routing_candidate.joblib`，SHA-256 `53a56f3bb1d776cca5e4a33926b6db57f74d3e982d16cf73ff3a097dc592ff63`。
- CLIP `openai/clip-vit-base-patch32`，revision `3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268`。
- 默认 F1 工作点的四标签阈值依次为 `0.13 / 0.16 / 0.57 / 0.14`。原有高召回、低误报阈值保留在冻结资产记录中，本轮不重新选择或向 UI 新增阈值调参功能。
- 四标签顺序：字号不合理、内容遮挡、布局拥挤、内容溢出或裁切。没有 D5/D6 的 CLIP 概率。

复用原图像预处理和 PNG10 配方，包括 CLIP 中心裁剪。中心裁剪会省略宽屏两侧，仍是已知限制；为了训练/推理一致，没有在应用里悄悄更改输入。模型输出未经新任务场景重新校准，不等于缺陷发生率或确认真值。

## 用户操作与真实顺序

新任务默认开启 R2、关闭 CLIP。进入任务配置后，可手动开启“CLIP 四标签辅助复核”。开启 CLIP 同时启用 R2；关闭 R2 时 CLIP 也关闭，后端拒绝绕过 R2 单独启用此辅助模式。

LLM 仍需用户显式启用并配置服务，避免开启本地 CLIP 就悄悄发送页面或产生付费调用。使用真实 LLM 时，调度沿用既有候选/未知补查、专用规则和预算；关闭、未配置、预算不足、超时、Mock 或 uncertain 都会明确保留未完成范围，不称为完整最终质量确认。

执行过程：

1. 原有只读解析、原比例 PNG、COM 排版测量后，运行 R2 六类首筛。
2. 手动启用时，对全部成功渲染页面执行真实 CLIP 四类辅助复核，每份文稿一次模型加载、batch_size=1。渲染/推理失败页留错误记录。
3. 在 LLM 调度之前，将 CLIP 的正负概率、冻结阈值、版本、工作点附到派生候选与六类补查范围。原 R2 Claim 不改写。
4. CLIP 阴性不能删除或判定 R2 候选为 rejected；分歧单独记录。CLIP 独有阳性形成待复核的页级候选，不伪造对象坐标。
5. LLM 查看 R2、CLIP、整页/局部/跨页证据和用户意图后，返回 supported/rejected/uncertain；最终建议与原始证据分开保存。

## 实现与接口

| 位置 | 功能 |
|---|---|
| `backend/models/visual_model.json` | 实际可用的应用插件清单，默认不启用 |
| `backend/models/frozen_clip_manifest.json` | 固定资产 SHA、标签顺序、阈值及预处理版本 |
| `backend/models/frozen_clip_impl.py` | 不加载重模型的能力检查；按任务启动既有 ML Python |
| `services/clip_inference_worker.py` | 本地固定权重、原配方、推理模式；禁止网络、拟合、基准读取和研究写入 |
| `services/visual_classifier.py` | 一次任务批量调用、页面身份验证、有限概率与元数据校验 |
| `services/merge.py` | 正负 CLIP 证据与分歧保留，不否决 R2，避免重复附加 |
| `services/review_agent.py` | 把真实 CLIP 证据送入专用 Prompt 与未知补查，不污染 D5/D6 |
| `services/report.py` 与前端 | 各层状态、逐页四标签证据、概率、阈值、版本、剩余复检范围 |

新增 GET `/api/tasks/{id}/visual-results`：返回逐页 CLIP 状态、四类概率、阈值、告警及版本。原单页 GET 增加 `visual_result`，报告增加 `visual_results` 与 `verification_pipeline`。追问继续携带原任务 CLIP 证据，调用预算与历史保留规则不变。

真实任务下保存 `analysis/visual_results.json` 和 `analysis/clip/` 的请求、结果、必要特征与 worker 日志。权重没有复制到应用目录，没有重新下载数据或模型。

## 实际验收

| 检查 | 结果 |
|---|---|
| 后端完整测试 | **149 passed，0 failed，0 skipped** |
| 前端 typecheck / build | 通过 |
| 原前端契约 / 新 CLIP 组件契约 | **8 / 7 项通过** |
| 真实本地 CLIP 烟测 | 原应用 8 张 PNG 全部成功，CUDA，模型只加载一次 |
| 新 14 页默认关闭任务 | `4a40b2a37b184d1b`；CLIP 结果 0 条，无 CLIP 推理目录/模型加载 |
| 新 14 页手动开启任务 | `4b9f59bfc64c46f2`；14 页四标签全部成功，CLIP 为真实推理 |
| 三级顺序 | R2 完成时间 ≤ CLIP 开始，CLIP 完成时间 ≤ LLM 开始 |
| R2 不受 CLIP 改写 | 两任务 84 条原始 Claim 的页面/标签/状态/分数一致 |
| LLM 演示 | 显式 Mock，全部 uncertain，实际外部调用 0 |
| 浏览器 | 6 项只读核验通过；默认关闭、手动开关与 R2 依赖、四类证据、未复核页、报告导出均正常；无页面错误、无写请求 |
| 报告 | JSON、HTML、PDF 实际导出，含 CLIP 概率/冻结阈值和 Mock/未完成范围 |

开启任务保留 14 页成功推理的原 PNG SHA、固定权重与配方摘要，模型加载一次，未新增拟合、下载或基准读取。没有使用 SlideAudit test 开发，没有改 G-2 工件、原标签或冻结模型。

证据位于 `clip_optional_checks/`；底层 worker 的 12 项测试与 8 图烟测另存 `clip_integration_checks/`。开启报告为 `clip_optional_checks/on_report.html/json/pdf`。

本轮证明可选三级链路真实可执行，**没有证明加入 CLIP 提高了自然页面的准确率或消除了漏检**。用户可选择增加这层辅助证据，其默认关闭状态和既有质量限制均保留。

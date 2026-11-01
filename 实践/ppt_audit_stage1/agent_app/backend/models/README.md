# 视觉分类模型接入目录

已接入冻结 C 四标签辅助模型。默认关闭，用户在任务配置页手动开启后才进行推理。未复制大模型权重，复用项目原有 ML 虚拟环境与 Hugging Face 本地缓存。

当前文件：

1. `frozen_clip_impl.py`：轻量可用性检查与隔离批量推理桥接；每份文稿仅加载一次模型。
2. `visual_model.json`：应用接口清单，默认工作点 F1。
3. `frozen_clip_manifest.json`：模型、预处理、PNG10、源码摘要与既有三组阈值。

`VisualClassifierService.status()` 的 ready 表示依赖入口可用，不表示已运行模型。默认关闭时视觉阶段为 skipped；显式开启后使用原始 PNG 执行真实 CLIP + 冻结 C 推理，失败保留错误，不产生占位概率。

执行顺序为 R2 六类首筛 → CLIP 四类辅助复核 → LLM 按需复检。CLIP 低分不取消 R2 候选，正负概率和冲突都会保留。LLM 必须显式启用；关闭、预算不足、失败或 Mock 均不会标记为真实最终确认。

注意：
- 标签顺序必须是 `improper_font_sizing, occluded_content, cluttered_layout, content_overflow_cutoff`；
- 本次沿用 G-2 预注册的冻结 C、CLIP revision `3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268` 与 F1 阈值 `[0.13, 0.16, 0.57, 0.14]`，没有据 G-2 成绩重新选择或调参；
- 模型输出只是页面级概率，不提供元素坐标，系统不会伪造检测框。

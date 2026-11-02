# 研究方法与实验资源

## 数据整理与分组

项目以公开 SlideAudit 为主要图像研究来源。原始数据包含 2,400 页；项目结合文字、对象序列、几何和来源信息识别同源页面，形成 541 个可信四页组、共 2,164 页。以组为单位形成 train 1,512 页、validation 328 页、test 324 页，其余 236 页排除；组内原页与受控变体放在同一个集合，降低同源页面泄漏。

对应实现为 `detector_runtime/inspect_slideaudit.py`、`scripts/check_train_readiness.py` 和数据下载脚本。`research/sample_data/` 从既有 train / validation 中精选 40 页公共 Gemini 来源图像，保留原字节及来源记录。四个学习标签来自官方标注的布尔 response，不用是否“强一致”改写标签。

## 表示学习与特征比较

CLIP ViT-B/32 编码器冻结使用，输出 512 维图像投影；本项目未对基础 CLIP 做微调。中心裁剪特征、保持整页的缩放留白方案和多区域特征分别由 `extract_clip_features.py`、`extract_full_slide_features.py`、`extract_multiregion_clip.py` 实现。

为补充全局语义向量对版式细节的不敏感，项目实现 PNG10：宽高比、边缘密度、平坦区域比例、灰度标准差、网格边缘变异、边缘带分布、水平 / 垂直投影离散度、平均灰度和色彩范围。方案、顺序与预处理写入 `features/png_layout_recipe.json`，实现为 `extract_png_layout_features.py`。

| 表示 | 维度 | 内容 |
| --- | --- | --- |
| A | 512 | 固定逐行 L2 归一化的 CLIP。 |
| B | 10 | 仅使用训练折拟合的标准化 PNG10。 |
| B9 | 9 | 移除宽高比的版式特征。 |
| C | 522 | CLIP512 与标准化后除以 √10 的 PNG10 拼接。 |
| C9 | 521 | 使用 PNG9 的融合消融。 |
| H | 按标签选择 A / C | 由训练期内层 OOF 表现确定的路由。 |

## 分类器、验证与阈值

四标签学习任务为字号、遮挡、拥挤和溢出。首次 CLIP-only 实验每标签比较 C∈{0.1,1,10} 与 class_weight∈{None,balanced}，共完成 24 次逻辑回归拟合，并使用 validation 选择参数及阈值。冻结后在 324 页 test 上进行一次评估：选定阈值下 Macro F1 为 0.3253，Macro AP 为 0.2987。该结果属于首次基线，不是完整智能体或后续融合模型的总评分。

后续 PNG 融合与路由实验使用分组交叉验证、训练折内拟合标准化器和嵌套 OOF 选择，比较不同表示，形成 F1、高召回、低误报三种工作点；跨来源压力检查与组级配对 bootstrap 用于分析稳定性。协议见 `research/configs/experiment_protocols.json`；核心实现为 `train_png_fusion_cv.py`、`optimize_fusion_routing.py`、`pptx_g2_metrics.py`。独立 PPTX 验证入口为 `pptx_g2_preflight.py`、`evaluate_pptx_g2.py`、`analyze_pptx_g2.py`。

`research/models/` 保留训练后的基线和消融参数；应用使用的融合模型位于 `detector_runtime/models/fusion_routing_candidate.joblib`。没有将全量特征矩阵、逐页预测明细或实验运行日志放入仓库。

## 从页面概率到证据审查

全局图像表示对小字号、边缘裁切和细粒度遮挡仍可能误诊。因此应用进一步结合原生对象、实际文字排版、图像区域、空间占用、角色和跨页关系，形成六类专用风险证据。实现保留在 `detector_runtime/experiments/`，运行清单逐文件校验。

智能体围绕候选证据进行调度与复核，保留问题来源、未知范围、失败与继续审查状态；规则或 AI 修复生成新文稿，并提供修改记录和前后对比。研究中的四标签辅助概率与应用的六类证据分别保留，用户能查看每项建议的依据。

## 样本检查

```powershell
python .\research\scripts\verify_sample_data.py
.\detector_runtime\.venv\Scripts\python.exe .\research\scripts\smoke_clip.py --limit 4
```

第一条校验图像、标注、四标签映射、原始划分与组隔离；第二条实际加载 CLIP 与冻结融合分类器。样本来源和许可见 [第三方资源说明](third_party_assets.md)。

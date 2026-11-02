# 模型与检测资源

## 运行模型

| 资源 | 位置 | 使用方式 |
| --- | --- | --- |
| 公开 CLIP ViT-B/32 编码器 | `detector_runtime/models/clip_encoder/` | Git LFS 保存 PyTorch 权重，配置、来源、许可和 SHA256 随仓库保留。 |
| 项目融合分类器 | `detector_runtime/models/fusion_routing_candidate.joblib` | 应用使用冻结 C：CLIP512 + PNG10，输出四标签辅助概率。 |
| CLIP-only 基线 | `research/models/clip_lr_4labels.joblib` | 保留四个已训练逻辑回归、阈值及实验元数据。 |
| PNG / CLIP 融合消融模型 | `research/models/png_fusion_candidates.joblib` | 保留 A/B/B9/C/C9 表示对应的分类器和训练期标准化器，供研究比较。 |
| 六类专用实现与配置 | `detector_runtime/experiments/` | 应用实时调用；`six_runtime_manifest.json` 校验 36 个工件。 |

`research/models/registry.json` 记录项目模型路径、摘要和用途。基线与消融模型作为研究资源保留；应用运行分支使用冻结 C 与六类专用实现。CLIP 编码器来自公开预训练模型，下游分类器为本项目训练；模型参数、特征顺序和工作阈值均保持冻结。

## 完整安装

在根目录运行：

```powershell
git lfs pull
.\start_app.cmd -WithClip
```

Git LFS 展开约 577 MiB 的 `pytorch_model.bin`。`-WithClip` 安装 PyTorch、Transformers、scikit-learn 等依赖，并逐文件校验 `backend/models/frozen_clip_manifest.json`。普通启动仍只准备后端与六类检测依赖。设置好视觉环境后，在工作台开启 CLIP，或通过 CLI 选择需要该分支的配置。

如果没有展开 LFS 文件，或通过 ZIP 获取了代码，`scripts/download_clip.py` 会按固定模型 ID、revision 和 SHA256，从 Hugging Face 补齐需要的文件。下载缓存写入 `detector_runtime/.cache/`，不进入 Git；后续推理使用本地资源并关闭网络下载。

六类专用检测使用 NumPy、SciPy、Pillow，不需要外部 API 或 CLIP 权重。AI 复核与 AI 修复使用个人配置的模型服务，API Key 不随项目提供。

## 离线模型检查

```powershell
.\detector_runtime\.venv\Scripts\python.exe .\research\scripts\smoke_clip.py --limit 4
```

此命令直接运行应用的真实 CLIP + PNG10 工作者，输出页面概率、阈值和实际推理统计，不训练分类器，不读取 benchmark，也不请求外部模型服务。结果位于 `research/outputs/clip_sample_smoke/`，已屏蔽。

## 研究复现

原特征提取、训练、分组交叉验证和独立评估脚本仍在 `detector_runtime/scripts/`，实验协议摘录位于 `research/configs/experiment_protocols.json`。完整历史实验需要按公开来源准备全量数据和相应输入清单；精选样本用于理解流程与验证安装，不替代正式实验的数据规模。

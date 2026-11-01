# 检测依赖与模型资源

## 常规六类检测

一键启动会创建 `backend/.venv` 和 `detector_runtime/.venv`，分别安装后端与纯计算依赖。六类检测使用 NumPy、SciPy、Pillow，不需要外部 API，也不需要下载 CLIP 权重。依赖版本分别见 `backend/requirements.txt` 和 `detector_runtime/requirements-runtime.txt`。

冻结源文件与配置位于 `detector_runtime/experiments/`，保持原目录关系以复用既有实现。`backend/models/six_runtime_manifest.json` 记录 36 个运行工件的相对路径及 SHA256。唯一随代码保留的 `coordinate_transform_proof.json` 是合成表格坐标校准记录，用于限定实时测量的适用范围。

## 可选 CLIP 辅助

需要体验辅助分类时，在根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_ml.ps1 -Clip
```

脚本安装 `detector_runtime/requirements-clip.txt`，再从 Hugging Face 下载固定 revision 的公开 `openai/clip-vit-base-patch32` 配置与权重，并按 `backend/models/frozen_clip_manifest.json` 校验 SHA256。下载量较大，需要能够访问 Hugging Face；缓存放在 `detector_runtime/models/huggingface/`，不进入 Git。下载完成后可离线推理，再在工作台启用该分支。

仓库保留项目训练的小型 `fusion_routing_candidate.joblib`、冻结 PNG10 特征方案及计算脚本。CLIP 仅提供辅助结果，常规六类检测可以独立运行。

## 研究源码

`detector_runtime/scripts/` 保留特征提取、训练、交叉验证和评估代码，便于检查研究方法。研究脚本需要另行准备相应的数据与目录，不属于启动应用的步骤；原始数据集、benchmark、标注、历史预测及大型模型缓存不随仓库分发。请勿通过修改冻结阈值或读取历史预测替代新文件的实时检测。

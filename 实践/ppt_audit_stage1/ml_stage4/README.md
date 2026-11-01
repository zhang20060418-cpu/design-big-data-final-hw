# 检测运行依赖与研究代码

当前 Agent 的六类专用检测复用本目录中已冻结的实现。`experiments/` 保持原相对目录，以保证动态导入与原算法一致；应用不会执行这些模块的研究批处理入口，也不读取历史预测或 benchmark 标注。所有运行文件由 `../agent_app/backend/models/six_runtime_manifest.json` 逐项记录 SHA256。

- `requirements-runtime.txt`：常规六类检测的 NumPy、SciPy、Pillow。
- `requirements-clip.txt`：可选 CLIP 辅助分类依赖。
- `models/fusion_routing_candidate.joblib`：项目训练得到的小型冻结分类器。
- `models/huggingface/`：可选公开 CLIP 权重缓存，不上传 Git。
- `features/png_layout_recipe.json` 与指定脚本：冻结特征计算方案。
- `scripts/`：保留特征提取、降维与分析、分类器训练、交叉验证及评估代码，便于查看项目方法；研究脚本需要另行准备对应数据，不属于应用启动步骤。
- 唯一保留的 `results/coordinate_transform_proof.json` 是合成表格坐标校准记录，供实时测量判断适用范围，不是用户数据或检测结果缓存。

数据下载脚本仅作为研究来源说明保留；日常运行无需下载研究数据集。个人 PPT、benchmark、SlideAudit 数据快照、渲染图、标注、实验输出和虚拟环境均仅保存在本地。完整启动步骤见[仓库说明](../../../README.md)。

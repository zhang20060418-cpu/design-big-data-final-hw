# 公开模型与数据来源

## CLIP 编码器

- 来源：[OpenAI CLIP ViT-B/32 模型](https://huggingface.co/openai/clip-vit-base-patch32)。
- 固定 revision：`3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268`。
- 位置：`detector_runtime/models/clip_encoder/`。
- 许可：MIT，版权声明随模型放在 `license.txt`；来源说明见同目录 `source.json`，原许可见 [OpenAI CLIP LICENSE](https://github.com/openai/CLIP/blob/main/LICENSE)。
- 基础权重未修改，由 Git LFS 保存。项目训练的下游分类器与公开预训练编码器分别注明。

## SlideAudit 精选样本

- 来源：[SlideAudit 官方仓库](https://github.com/zhuohaouw/SlideAudit)，固定提交 `642d490b7c1d2e78a50a631bfd359433397f3ecf`。
- 作者：Zhuohao (Jerry) Zhang、Ruiqi Chen、Mingyuan Zhong、Jacob O. Wobbrock。
- 论文：[SlideAudit: A Dataset and Taxonomy for Automated Evaluation of Presentation Slides](https://doi.org/10.1145/3746059.3747736)，UIST 2025。
- 许可：[Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/)，上游声明见 `research/sample_data/license.txt`。

精选内容为 40 页公开 Gemini 来源图像：32 页 train、8 页 validation。原图、原标注与对象描述字节保持不变；项目新增选择清单、四标签映射、SHA256、源组与划分信息。源组编号为本项目推断编号，不是数据集官方原文稿 ID。没有发布个人课件或把本项目的任务历史作为训练样本。

`manifest.csv` 可追溯每个文件，`source.json` 记录选择种子、来源提交、许可和统计。样本用于说明数据与运行流程，不代表完整数据分布。全量数据按上游许可获取。

## Git LFS 获取

大权重通过 [Git LFS](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage) 保存；常规克隆配合 `git lfs install` / `git lfs pull` 获取实际文件。若下载得到的是指针文件，可使用项目的固定版本下载与校验脚本补齐。运行环境、下载缓存及推理产物保持本地保存。

# SlideAudit 数据获取与初步检查报告

生成时间：2026-10-24T18:01:51+08:00  
范围：第四阶段 4.1，只做数据获取、完整性检查与标签统计，**未训练任何模型**。

## 1. 获取结果

| 项目 | 数量 | 说明 |
| --- | --- | --- |
| metadata 记录 | 2400 | `data/slideaudit/data/metadata.csv` |
| annotation JSON | 2400 / 2400 | 全部检出并解析成功 |
| description JSON | 2400 / 2400 | 全部检出并解析成功 |
| 图片 | 60 / 2400 | 分层抽样，未下载 2340 张 |
| 数据目录体积 | 54.1 MB | 按文件字节合计（NTFS 实占约 65 MB）：图片 35.8 MB、标注 9.8 MB、描述 5.6 MB、git 元数据 2.8 MB |

获取方式：`git clone --filter=blob:none --depth=1 --sparse`，稀疏检出 `data/metadata.csv`、`data/annotations/`、`data/descriptions/`；图片按抽样清单逐张 HTTPS 下载。仓库 README 写的是 `.jpg` 与 `metadata/slideaudit_metadata.csv`，**实际文件为 `.png`，metadata 在 `data/metadata.csv`**，以实际目录为准。

重复执行验证：首次运行下载 60 张，第二次运行复用 60 张、新增下载 0 张、新增体积 0.0 MB。

## 2. 完整性与缺失区分

| 状态 | 数量 | 含义 |
| --- | --- | --- |
| 已获取且可解析 | 2400 标注 / 2400 描述 / 60 图片 | 通过结构校验 |
| 文件尚未下载 | 2340 张图片 | metadata / 标注 / 描述已全量获取；**不是损坏** |
| 源仓库确实缺失 | 0 | git 树对象中列出的文件数与本地一致 |
| 文件损坏 | 标注 0 / 描述 0 / 图片 0 | JSON 解析失败或 PNG 无法解码 |
| 数据字段缺失 | 0 处 | 见 2.1 |
| slide_id 对应失败 | 0 处 | 图片与三份索引之间 |

JSON 解析失败 **0**，图片读取失败 **0**，slide_id 对应失败 **0**。metadata 与标注的 id 集合差异：metadata 缺标注 0 个，标注缺 metadata 0 个。

### 2.1 标注字段检查

| 字段 | 缺失数 | 观测 |
| --- | --- | --- |
| design_deficiency_category | 0 | 4 个大类 |
| design_deficiency | 0 | 19 个缺陷名 |
| response | 0 | 布尔值 45600 条，非布尔 0 条 |
| has_strong_agreement | 0 | 布尔值，表示标注者是否强一致 |
| bounding_boxes | 可选 | 1491 条正例带框，共 2761 个框 |

类别归属冲突 **0**：19 个缺陷名各自只属于 1 个大类。

无法解释的标注条目 **0**。

**正负例定义**：正例 = response == true（人工标注认为该缺陷存在）；负例 = response == false（人工标注认为该缺陷不存在）。`has_strong_agreement=false` 的正例仍算正例，但单独统计为“弱一致正例”，不并入负例，也不丢弃。

## 3. 标签分布

共 2400 页 × 19 项 = 45600 条标注（每页恰好 19 条，无缺项）：正例 3379、负例 42221、未知 0。有正例的页面 1748 页（72.8%），全负例页面 652 页。

| 大类 | 正例 | 负例 | 正例比例 |
| --- | --- | --- | --- |
| Color | 343 | 6857 | 4.8% |
| Composition & Layout | 1734 | 12666 | 12.0% |
| Imagery & Visualizations | 245 | 9355 | 2.6% |
| Typography | 1057 | 13343 | 7.3% |

| 大类 | 缺陷 | 正例 | 负例 | 正例比例 | 强一致正例 | 带框正例 |
| --- | --- | --- | --- | --- | --- | --- |
| Typography | Improper Font Sizing | 374 | 2026 | 15.6% | 72 | 302 |
| Composition & Layout | Occluded Content | 372 | 2028 | 15.5% | 105 | 353 |
| Composition & Layout | Unbalanced Space Distribution | 320 | 2080 | 13.3% | 55 | 0 |
| Composition & Layout | Cluttered Layout | 278 | 2122 | 11.6% | 51 | 0 |
| Composition & Layout | Content Overflow/Cut-off | 273 | 2127 | 11.4% | 82 | 223 |
| Composition & Layout | Poor Visual Hierarchy | 265 | 2135 | 11.0% | 29 | 0 |
| Composition & Layout | Content Alignment Issues | 226 | 2174 | 9.4% | 21 | 190 |
| Typography | Poor Text Hierarchy | 209 | 2191 | 8.7% | 19 | 0 |
| Typography | Excessive Text Volume | 189 | 2211 | 7.9% | 40 | 0 |
| Typography | Improper Text Styling | 149 | 2251 | 6.2% | 13 | 120 |
| Color | Excessive or Inconsistent Color Usage | 133 | 2267 | 5.5% | 25 | 0 |
| Color | Insufficient Color Contrast for Readability | 113 | 2287 | 4.7% | 20 | 97 |
| Color | Inappropriate or Mismatched Color Combinations | 97 | 2303 | 4.0% | 8 | 0 |
| Typography | Improper Line/Character Spacing | 95 | 2305 | 4.0% | 7 | 70 |
| Imagery & Visualizations | Improper Image Sizing | 84 | 2316 | 3.5% | 3 | 56 |
| Imagery & Visualizations | Irrelevant Visual Content | 78 | 2322 | 3.2% | 2 | 0 |
| Imagery & Visualizations | Poor Image Quality/Editing | 64 | 2336 | 2.7% | 5 | 53 |
| Typography | Illegible Typeface Selection or Usage | 41 | 2359 | 1.7% | 1 | 27 |
| Imagery & Visualizations | Inconsistent Visual Style Usage | 19 | 2381 | 0.8% | 2 | 0 |

强一致正例合计 **560** 条，占全部正例的 16.6%；若只用强一致正例训练，最少的类别只有 1 个样本，19 类分类不可行，需要放宽到全部正例或合并类别。

### 3.1 多标签

单页正例数量的分布：0 个 652 页、1 个 864 页、2 个 499 页、≥3 个 385 页；最多一页 9 个正例。

多标签页面占比：≥2 个正例 884/2400 = 36.8%；≥3 个 385 页。**一页多标签是常态，不是异常**，后续建模需要么做多标签分类，么选一个主缺陷并显式处理其余标签。

## 4. 图片与标注对应（抽查）

抽样依据（见 `download_manifest.json.selection`）：60 张按 P1–P6 分层——每个缺陷至少 1 个强一致正例（按稀有度优先）、多标签页、弱一致正例、全负例页、大类补足、种子补齐；覆盖 19/19 个缺陷、18 张无正例页、6 张 ≥3 标签页。

固定种子 `20261024` 从已下载图片中随机抽 10 张，逐张打开并核对：

| slide_id | 尺寸 | 与 metadata 一致 | 标注 | 描述 | 标注条目 | 正例 | bbox | 越界框 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| slide_1464 | 1600×900 | 是 | 有 | 有 | 19 | 2 | 2 | 0 |
| slide_0061 | 1600×1200 | 是 | 有 | 有 | 19 | 1 | 0 | 0 |
| slide_1887 | 960×720 | 是 | 有 | 有 | 19 | 1 | 2 | 0 |
| slide_0011 | 960×720 | 是 | 有 | 有 | 19 | 2 | 3 | 0 |
| slide_0702 | 1600×900 | 是 | 有 | 有 | 19 | 5 | 3 | 1 |
| slide_1748 | 1600×900 | 是 | 有 | 有 | 19 | 0 | 0 | 0 |
| slide_0003 | 1600×900 | 是 | 有 | 有 | 19 | 3 | 3 | 0 |
| slide_0159 | 1600×1200 | 是 | 有 | 有 | 19 | 2 | 2 | 0 |
| slide_1311 | 1600×900 | 是 | 有 | 有 | 19 | 1 | 0 | 0 |
| slide_1938 | 1600×1200 | 是 | 有 | 有 | 19 | 0 | 0 | 0 |

抽查结论：10/10 尺寸一致，10/10 标注与描述齐全，越界框 1 个。全部 60 张核对：读取失败 0 张，尺寸与 metadata 不一致 0 张。

全量标注坐标范围（像素）：x1 -3.5–1527.2，y1 -1.7–1148.9，x2 46.4–1599.9，y2 29.0–1198.3；超出图片边界 29/2761 个（容差 1 px）。坐标是像素值，不是 0–1 归一化值。

静态查看页：`sample_gallery.html`（10 张，可直接用浏览器打开）。

## 5. 原始 / 增强分组风险

`source_type` 有 3 种：Gemini(800)、gdcdataset(800)、Google(800)；`alteration` 有 4 种：nojitter(600)、texture(600)、layout(600)、alignment(600)。

- **没有显式 group_id / original_id 字段**。metadata 只有 id、source_type、alteration、宽高。
- 3 个来源 × 4 种 alteration = 12 个组合，每个组合恰好 200 页，分布完全均衡；各来源都覆盖全部 alteration 种类，说明 `source_type` 是来源标签而非分组 ID。
- id 顺序上的 (source_type, alteration) 连续段共 2172 段，段长以 1 为主（1966/2172），相邻 id 不是稳定的“同页变体”排列。
- 文本指纹（descriptions 的 textContent）：587 组文本完全相同，覆盖 2372/2400 页；组大小为 4 页 581 组、8 页 6 组。587/587 组内部 source_type 一致，587 组内部正好覆盖 4 种 alteration（其中 581 组恰好 4 页）。
- 元素几何独立验证：4 页组内 3486 对，归一化坐标平均差 mean 0.017 / median 0.003（45% 完全相同）；元素数相同的跨组随机对 median 0.247。组内几乎是同一版式的不同渲染。
- 已下载的 60 张中没有两张落在同一文本组，因此 dHash 近重复对为 0，**无法用图片验证组内相似度**，只能用描述几何与字段结构佐证。

**结论：分组基本可以重建，但目前只是高置信度候选，不是官方标注。** 证据指向“600 个原始页面 × 4 种 alteration 变体”（3 个来源各 200 个）；仍有 6 个 8 页组需要拆分、28 页无文本无法归组，且不能排除同文案独立生成。因此本次**不生成 group_id、不做训练/测试划分**；正式切分前需向作者确认构造方式，或对候选组做图像相似度人工核验。

## 6. 结论与下一步

数据准备层面已具备进入下一阶段的条件：

- 2400 页的标注与描述完整可解析，19 个缺陷、4 个大类的正负统计已就绪；
- 60 张图片全部可读，尺寸与 metadata 一致，slide_id 与标注/描述一一对应；
- 正负例定义明确，弱一致正例与未知项已单列，未混入负例。

仍需注意：

- 图片只下载了 60/2400 张，**不能**用这 60 张估计整体缺陷率；全量标签统计来自 2400 份标注，与图片子集是两件事；
- 分组有高置信度候选依据（文本指纹 + 元素几何 + 4 种 alteration 结构），但没有官方 group_id，正式切分前仍需作者确认或人工核验；
- 正例比例在不同缺陷间差异很大（见第 3 节），直接训练会偏向高频类；
- 标注为人工判断，`has_strong_agreement=false` 的正例带有噪声；
- 少量标注框越界（29/2761），对分类任务影响有限，做检测/定位任务前需要清洗。

**是否足以开始训练准备**：作为标签体系与统计口径的准备，**足够**；作为图像模型的数据集，**不够**——只有 60 张图，且未做分组切分。4.2 若只用结构化/几何特征，可基于 2400 份标注先做标签与特征设计；若要训练视觉模型，必须先扩大图片下载并确认分组口径。

本次未训练模型、未制作训练/验证/测试集、未修改前三阶段代码。

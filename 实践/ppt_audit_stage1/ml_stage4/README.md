# ml_stage4 — 第四阶段：SlideAudit 设计缺陷数据

> 状态：**4.1 数据获取与初步分析已完成**；未训练模型、未制作训练/验证/测试集。

## 一、本阶段在做什么

用传统机器学习方法对幻灯片版面质量做可复现的分类，替代继续堆规则。
4.1 只解决"数据从哪来、标签是什么、能不能用"，不碰模型。

数据来源：<https://github.com/zhuohaouw/SlideAudit>（UIST 2025，CC BY 4.0）。

## 二、目录

```text
ml_stage4/
├── download_slideaudit.py     数据获取（部分克隆 + 分层抽样图片）
├── inspect_slideaudit.py      完整性检查、标签统计、索引生成
├── README.md                  本文件
├── data/
│   ├── slideaudit/            部分克隆：metadata + 2400 annotation + 2400 description
│   ├── images/                抽样的 60 张 PNG
│   └── download_manifest.json 下载记录（含历次运行与选图依据）
├── reports/
│   ├── dataset_index.csv      60 张图的图片/标注/描述对应索引
│   ├── label_distribution.csv 19 个缺陷的正负统计
│   ├── dataset_report.md      数据检查总结
│   └── sample_gallery.html    10 张抽查样本的静态查看页
├── features/                  预留（空）
└── models/                    预留（空）
```

## 三、怎么跑

```bash
cd ml_stage4
python download_slideaudit.py              # 结构化数据 + 60 张图；已存在则跳过
python inspect_slideaudit.py               # 生成 reports/ 下四个产物
```

可选参数：`--limit-images N`（上限 80）、`--skip-images`、`--force-images`、
`--max-mb`、`--seed`、`--data-dir`、`--out-dir`。脚本路径基于 `__file__`，
从任意工作目录运行都可以。重复执行不会重复下载（第二次实测新增 0 MB）。

## 四、数据事实（截至 4.1）

- metadata 2400 行；annotation 2400 份；description 2400 份；图片 60 张（35.8 MB）。
- 标注 19 个缺陷、4 个大类，共 45600 条：正例 3379、负例 42221、未知 0。
- 正例定义 `response == true`；`has_strong_agreement` 单独统计，不并入负例。
- 一页多标签常见：≥2 个正例 884 页（36.8%），最多 9 个。
- 未下载的 2340 张图是**未获取**，不是损坏。

## 五、进入建模前必须知道

1. **分组未官方确认**：metadata 无 group_id。文本指纹 + 元素几何 + 4 种 alteration
   的结构高度一致（581 组 4 页、组内几何中位差 0.003、跨组 0.247），
   指向"600 原始页 × 4 变体"，但仍是反推，不能直接当官方切分依据。
2. **图片只有 60/2400**：标签统计覆盖全量标注，不代表图片子集；不要用 60 张估计缺陷率。
3. **类别极不均衡**：从 15.6% 到 0.8%，且强一致正例更少（最少的只有 1 个）。
4. **不做的事**：不训练 CLIP/ResNet/逻辑回归/SVM，不划分数据集，不接多模态 API。

## 六、下一步（4.2 再开始）

- 确认分组口径，按组切分避免同源泄漏；
- 决定任务形式：19 类多标签，还是先做单一主缺陷；
- 特征先用传统可解释特征（版面几何、字号、颜色、文本密度），再谈模型增益。

# ppt_audit_stage1 — PPT 版面质量审查（三阶段原型）

## 1. 项目目标

验证一条最小可跑通的 PPT 版面质量审查链路：

**解析结构 → 找几何异常 → 复检文字是否真的被裁切 → 用可解释规则决定要不要看图 → 输出带证据的结论。**

边界先说清楚：

- 不训练模型、不做前端、不修改原始 PPT；
- 第三阶段是**规则调度 + 按需复核的原型**，不是自主规划器，也不是机器学习模型；
- 所有结论都要能回溯到「文件 + 页码 + `shape_id` + 证据图 / JSON 字段」；
- 没有检查过的对象标为 `not_checked`，不写成“无缺陷”。

## 2. 已完成的三个阶段

| 阶段 | 做了什么 | 主要产物 | 验收 |
|---|---|---|---|
| 一：结构解析与几何越界 | python-pptx 只读解析 6 份 PPT / 62 页，提取对象坐标、文字、组变换，按外框判越界 | `outputs/stage1_structure.json`、`stage1_violations.json` | 19/19 |
| 二：文字可见性复检 | PowerPoint COM 读 `TextFrame2` 实际排版范围 + 渲染图像素墨迹，判断**可见文字**是否越出页面 | `outputs/recheck/text_recheck.json`、报告、标注图 | 23/23 |
| 三：规则调度与按需复核 | R1~R8 规则输出 `skip / report / recheck`；只对疑点页做一次视觉复核；融合本地与视觉证据 | `outputs/agent/agent_findings.json`、报告、运行日志 | 108/108（含融合单测 17） |

关键结论：第一阶段的 4 条“外框越界”里，**2 条实际文字仍在页内**，2 条是空形状；
真正需要看图的是第三阶段补充的 3 个合成案例（页底裁切、遮挡、低对比度）。

## 3. 目录与核心程序

```text
ppt_audit_stage1/
├── README.md                 本文件
├── project_inventory.md      整理前的文件盘点
├── cleanup_report.md         本次整理记录与测试结果
├── docs/                     文档索引与数据流说明
├── _archive/                 归档区（1 个失效缓存 + 整理前测试日志）
├── ml_stage4/                第四阶段：SlideAudit 数据获取（4.1 已完成）
├── audit_stage1.py           阶段一主程序
├── make_test_decks.py        阶段一自检样本生成
├── verify_stage1.py          阶段一验收
├── annotate_visual.py        阶段二标注
├── check_annotations.py      阶段二标注自检
├── agent_router.py           阶段三调度与融合
├── review_client.py          阶段三 api / host / mock 复核客户端
├── stage3_probe.py           阶段三测试页证据生成
├── verify_agent.py           阶段三验收
├── test_fuse.py              融合逻辑单测
├── test_api_client.py        API 客户端本地桩测试（非真实模型）
├── outputs/                  三个阶段的结构化结果与证据
├── test_samples/             三套测试样本
└── visual_validation/        阶段二渲染页、标注图、人工核对表
```

| 程序 | 用途 |
|---|---|
| `audit_stage1.py` | 解析 PPTX，输出页/对象/坐标/文字与越界清单 |
| `verify_stage1.py` | 真实调用主程序，断言 19 项，并生成自检结果 |
| `make_test_decks.py` | 生成“正常页 + 四方向越界页”自检样本 |
| `annotate_visual.py` | 在渲染图上按坐标画框，页外部分用虚线 + 斜纹重建 |
| `check_annotations.py` | 核对标注位置、墨迹与源文件哈希 |
| `outputs/recheck/text_visibility_recheck.py` | 阶段二主程序：COM 读实际排版范围 + 像素复检 |
| `outputs/recheck/verify_recheck.py` | 阶段二验收，23 项断言 |
| `stage3_probe.py` | 复用阶段二探针，为 4 张测试页生成同口径证据 |
| `agent_router.py` | R1~R8 路由、按页去重复核、`fuse()` 融合、报告与日志 |
| `review_client.py` | `api` / `host` / `mock` 三种复核模式，Key 只读环境变量 |
| `verify_agent.py` | 阶段三验收，108 项断言（含调用 `test_fuse.py`） |
| `test_fuse.py` | 覆盖“本地低置信 + 视觉反向 → 证据冲突”等 17 项 |
| `test_api_client.py` | 本地 HTTP 桩验证请求组装、usage/费用、Key 不落日志 |

## 4. 如何运行现有测试

依赖：Python 3.13、`python-pptx`、`Pillow`、`pywin32`；
第二阶段与阶段三证据生成需要本机安装 **Microsoft PowerPoint**（COM）。

```bash
# 阶段一：解析 + 自检 + 19 项断言（会重写 outputs/stage1_*.json）
python verify_stage1.py

# 阶段二：实际运行复检 + 23 项断言（需要 PowerPoint，耗时较长）
python outputs/recheck/verify_recheck.py

# 阶段三：用已记录的宿主复核证据重新融合，并跑 108 项验收
python agent_router.py --reviewer host --max-calls 4 --out-dir outputs/agent
python agent_router.py --reviewer mock --max-calls 4 --out-dir outputs/agent/mock_run
python verify_agent.py
python test_fuse.py

# 可选：API 客户端本地桩（不联网、不是真实模型）
python test_api_client.py
```

单独重跑阶段三测试页证据（需要 PowerPoint）：

```bash
python test_samples/stage3/make_stage3_cases.py
python stage3_probe.py
```

`api` 模式（**当前未联调**，没有 Key 时会直接退出、不会发请求）：

```bash
PPT_REVIEW_API_KEY=... python agent_router.py --reviewer api --max-calls 2 --out-dir outputs/agent/api_run
```

## 5. 主要结果保存在哪里

| 内容 | 位置 |
|---|---|
| 6 份 PPT 的结构与对象清单 | `outputs/stage1_structure.json` |
| 4 条几何越界记录 | `outputs/stage1_violations.json` |
| 文字可见性复检（含覆盖台账） | `outputs/recheck/text_recheck.json` |
| 阶段二报告与标注图 | `outputs/recheck/recheck_report.md`、`evidence/` |
| 阶段二人工核对表（分类字段留空） | `visual_validation/visual_validation.md` |
| 8 条融合结论 | `outputs/agent/agent_findings.json` |
| 阶段三报告 / 运行日志 | `outputs/agent/agent_report.md`、`agent_run_log.json` |
| 复核证据 | `outputs/agent/evidence/`、`host_review.json`、`review_prompts/` |
| 模拟对照 | `outputs/agent/mock_run/`（`simulated=true`） |
| 各阶段验收记录 | `outputs/stage1_verification.json`、`outputs/recheck/verification.json`、`outputs/agent/verify_output.txt` |

## 6. 第四阶段进展

`ml_stage4/`：**4.1 数据获取与初步分析已完成**，尚未训练任何模型。

- `download_slideaudit.py`：部分克隆（`blob:none` + 稀疏检出）获取 metadata、
  2400 份 annotation 与 2400 份 description；图片只按分层抽样下载 60 张（35.8 MB）；
- `inspect_slideaudit.py`：完整性检查、标签统计、10 张抽查与分组调查；
- 产物：`reports/dataset_index.csv`、`label_distribution.csv`、`dataset_report.md`；
- 数据：19 个缺陷 / 4 个大类 / 45600 条标注（正例 3379、负例 42221）；
- **进入训练前仍需解决**：原始/增强分组只有高置信度候选（无官方 group_id），
  且 2400 张图只下载了 60 张。
- 详细说明见 `ml_stage4/README.md` 与 `ml_stage4/reports/dataset_report.md`。

## 7. 当前已知限制

1. **第二阶段没有做全页文字检测。** 只复检第一阶段 4 个越界候选，涉及 4 页、19 个含文字对象：
   实际检查 2 个、跳过 17 个；全局 195 个文字对象中 **193 个是 `not_checked`**，覆盖率 1.0%。
   第 4 页 10 个文字对象一个都没查；该页唯一候选是空形状，它的“不适用”**不能**推断整页无裁切风险。
2. **第三阶段没有完成真实第三方 API 联调。** 本机未配置 `PPT_REVIEW_API_KEY`，
   `provider_api_calls = 0`、HTTP 请求 0 次，`real_api_integration.status = not_completed`。
   现有 3 次复核是宿主多模态图像工具实际看图（`mode=host`），Token 与费用不可得，**不等同于 API 复核**。
3. **第三阶段是规则调度原型。** `skip / report / recheck` 由固定规则表决定，第一条命中即停止；
   没有学习、没有自主规划，也没有对全部页面做质量评分。
4. **样本很小。** 合成测试页只有 4 页（V1 正常、V2 页底裁切、V3 遮挡、V4 低对比度），
   不代表真实语料的缺陷分布；可读性判断（V4）仍带主观性。
5. **证据冲突路径只有单元测试。** 现有 8 条 finding 没有真实命中“本地与视觉方向相反”的情况。
6. **阶段二的框内裁切样本未复现。** 小框 + 长文本时 PowerPoint 会把文字画到框外而不是裁掉，
   该场景标为未解决，未作为正例使用。
7. 原始 PPT 全程只读；三阶段验证都会比对 SHA-256。
8. 页面覆盖状态当前为 `complete / partial / not_checked = 1 / 2 / 59` 页（共 62 页）；
   `not_checked` 是覆盖缺口，不是“没有问题”。

## 8. 文档导航

- 文件盘点：`project_inventory.md`
- 整理记录：`cleanup_report.md`
- 文档索引：`docs/README.md`
- 数据流与路径约定：`docs/data_flow.md`
- 阶段报告：`outputs/测试总结.md`、`outputs/recheck/recheck_report.md`、`outputs/agent/agent_report.md`

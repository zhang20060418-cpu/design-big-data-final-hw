# 项目文件盘点（整理前）

- 盘点时间：2026-10-24
- 盘点范围：`实践/ppt_audit_stage1`（不含 `../python-ppt/downloaded_pptx` 的 6 份原始 PPTX）
- 盘点方法：`find` 列目录 + `sha256sum` 分组查重 + `grep` 扫描 Python 导入、`__file__` 路径、文档与 JSON 中的引用
- 规模：**102 个文件（不含 `__pycache__` 下的 `.pyc`），约 9.3 MB**
- 结论：**绝大多数 `.py` 与 `outputs/` 不能移动**。它们用 `Path(__file__).resolve().parent` 定位根目录，
  或者被验证脚本按固定相对路径读取；本次只归档 1 个失效缓存，其余保持原位。

---

## 一、目录总览

```text
ppt_audit_stage1/
├── README.md                     项目总说明
├── audit_stage1.py               阶段一主程序
├── make_test_decks.py            阶段一自检样本生成器
├── verify_stage1.py              阶段一验收（19 项断言）
├── annotate_visual.py            阶段二标注工具
├── check_annotations.py          阶段二标注自检
├── outputs/                      三个阶段的结构化结果与证据
│   ├── recheck/                  阶段二：文字可见性复检
│   │   ├── text_visibility_recheck.py
│   │   ├── verify_recheck.py
│   │   ├── text_recheck.json
│   │   ├── recheck_report.md
│   │   ├── verification.json
│   │   ├── evidence/             12 张标注图
│   │   ├── probe/                6 份 COM 探针缓存
│   │   └── render/               9 张页面渲染图
│   ├── agent/                    阶段三：路由、复核与融合
│   ├── selfcheck/                阶段一自检输出
│   └── stage1_*.json             阶段一真实语料输出
├── agent_router.py               阶段三调度与融合
├── review_client.py              阶段三 api / host / mock 客户端
├── stage3_probe.py               阶段三证据生成
├── verify_agent.py               阶段三验收（108 项断言）
├── test_fuse.py                  融合逻辑单测（17 项）
├── test_api_client.py            API 客户端本地桩测试
├── test_samples/                 三套测试样本与期望值
└── visual_validation/            阶段二渲染页、标注图与人工核对表
```

---

## 二、核心 Python 程序

| 文件 | 用途 | 被谁依赖 | 移动风险 |
|---|---|---|---|
| `audit_stage1.py` | 阶段一主程序：只读解析 PPTX，提取页/对象/坐标/文字，按几何规则判越界，输出 JSON | `verify_stage1.py` 以子进程调用 | **高**：验收脚本按 `HERE / "audit_stage1.py"` 调用 |
| `agent_router.py` | 阶段三主程序：读取阶段一/二证据 → R1~R8 规则路由 → 按页去重复核 → `fuse()` 融合 → 输出 findings/报告/日志 | `test_fuse.py`、`verify_agent.py` 直接 `import` | **高**：根目录 `import review_client`，且 `ROOT` 由 `__file__` 推出 |
| `review_client.py` | 三种复核客户端：`api`（OpenAI 兼容）/ `host`（读取宿主看图结果）/ `mock`（模拟） | `agent_router.py`、`test_api_client.py` | **高**：同目录导入 |
| `stage3_probe.py` | 用 `importlib` 加载阶段二复检模块，为 4 张合成测试页生成同口径证据 | 手动运行；输出 `outputs/agent/stage3_evidence.json` | **高**：硬编码 `outputs/recheck/text_visibility_recheck.py` 与 `test_samples/stage3/*` |
| `annotate_visual.py` | 阶段二：在 1920×1080 渲染图上按坐标画框，页外部分用虚线 + 斜纹重建 | 手动运行；读取 `outputs/stage1_*.json` | **高**：`ROOT = __file__.parent`，输出写 `visual_validation/` |
| `check_annotations.py` | 阶段二：核对渲染尺寸、标注框像素位置、墨迹与原始 PPT 哈希 | 手动运行；读取 `annotation_meta.json` | **高**：同上，依赖 `visual_validation/` 相对位置 |
| `make_test_decks.py` | 生成阶段一自检样本（1 页正常 + 1 页四方向越界）与 `expected.json` | 手动运行；`OUT_DIR = __file__.parent / "test_samples"` | **高**：移动会写错样本目录 |
| `outputs/recheck/text_visibility_recheck.py` | 阶段二主程序：PowerPoint COM 读 `TextRange` 实际排版范围 + 像素墨迹复检，生成 JSON/报告/证据图 | `verify_recheck.py` 子进程调用；`stage3_probe.py` 动态加载 | **极高**：`ROOT = HERE.parents[1]`，且被两个脚本按固定路径引用 |
| `outputs/recheck/verify_recheck.py` | 阶段二验收（23 项断言），并实际运行上面的复检程序 | 手动运行 | **极高**：按 `HERE` 定位 JSON、证据图与报告 |

> 结论：以上 9 个脚本**全部保持原路径**，不做任何移动或重命名。

---

## 三、测试脚本与测试样本

| 路径 | 用途 | 备注 |
|---|---|---|
| `verify_stage1.py` | 阶段一验收：真实调用 `audit_stage1.py`，逐项断言 19 项，写 `outputs/stage1_verification.json` | 依赖 `test_samples/expected.json`、`test_samples/selfcheck_stage1.pptx` |
| `verify_agent.py` | 阶段三验收：读 `outputs/agent/*.json` 与 `outputs/stage1_structure.json`，断言 108 项 | 同时调用 `test_fuse.run_checks()` |
| `test_fuse.py` | `fuse()` 的单元测试：本地/视觉冲突、不适用、未检查等 17 项 | 直接 `from agent_router import fuse` |
| `test_api_client.py` | 用本地 HTTP 桩验证 API 请求组装、图片编码、usage/费用解析、Key 不落日志 | **不是真实模型**；输出 `outputs/agent/api_client_test.json` |
| `test_samples/expected.json` | 阶段一自检页的期望越界方向与距离 | 由 `make_test_decks.py` 生成，被 `verify_stage1.py` 读取 |
| `test_samples/selfcheck_stage1.pptx` | 阶段一自检样本：1 页正常 + 1 页四方向越界 + 旋转/正常对照 | 2 页；源文件 |
| `test_samples/recheck/make_recheck_samples.py` | 生成阶段二 5 页对照样本 | 写入同目录 |
| `test_samples/recheck/recheck_samples.pptx` | 阶段二对照样本：正常、右裁切、下裁切、框内溢出、自动缩放 | 5 页；`text_visibility_recheck.py` 固定读取 |
| `test_samples/recheck/expected.json` | 阶段二对照样本的期望标签 | 由 `verify_recheck.py` 读取 |
| `test_samples/stage3/make_stage3_cases.py` | 生成阶段三 4 页测试案例（正常 / 页底裁切 / 遮挡 / 低对比度） | 写入同目录；另复制 1 px 裁边复核图 |
| `test_samples/stage3/stage3_visual_cases.pptx` | 阶段三测试页 | 4 页；`stage3_probe.py` 固定读取 |
| `test_samples/stage3/expected.json` | 阶段三每页的期望路由（skip/report/recheck） | 被 `verify_agent.py` 读取 |
| `test_samples/stage3/assets/bg_confetti.png` | V4 低对比度页的彩色背景素材 | 由 `make_stage3_cases.py` 嵌入 |
| `test_samples/stage3/assets/bg_nebula.png` | V2 复杂背景页的星云背景素材 | 同上 |

---

## 四、各阶段输出文件

### 4.1 阶段一（`outputs/`）

| 文件 | 用途 |
|---|---|
| `outputs/stage1_structure.json`（约 497 KB） | 6 份 PPT / 62 页 / 全部对象的 ID、类型、名称、文字、字号、四至坐标、组归属、几何来源、越界标记 |
| `outputs/stage1_violations.json` | 4 条真实越界记录的扁平清单 + 汇总 |
| `outputs/stage1_verification.json` | 19 项断言结果、指标与运行时间 |
| `outputs/测试总结.md` | 阶段一结论、四个问题的回答与已知限制 |
| `outputs/selfcheck/stage1_structure.json` | 自检样本的结构输出（由 `verify_stage1.py` 重新生成） |
| `outputs/selfcheck/stage1_violations.json` | 自检样本的越界输出（期望 4 条） |

### 4.2 阶段二（`outputs/recheck/`）

| 路径 | 用途 |
|---|---|
| `text_recheck.json`（约 72 KB） | 8 条 case（4 真实 + 4 合成）、`page_coverage` 台账、`source_check`、探针错误 |
| `recheck_report.md` | 阶段二报告：覆盖范围、逐条证据、未解决项 |
| `verification.json` | 23 项断言结果 |
| `evidence/`（12 张） | 8 张标注图 + 2 张页边放大图 + 2 张对照样本标注图；JSON 里以相对文件名引用 |
| `probe/`（6 份） | PowerPoint COM 探针缓存，键名含 `deck_sha256`；**删掉后需重新开 PowerPoint 才能恢复** |
| `render/`（9 张） | 真实 4 页 + 对照 5 页的 1920×1080 渲染图；`text_recheck.json` 记录其路径 |

### 4.3 阶段三（`outputs/agent/`）

| 路径 | 用途 |
|---|---|
| `agent_findings.json`（约 158 KB） | 8 条 finding：结构证据、可见性证据、覆盖状态、路由决策、复核证据、三级分级、修改建议 |
| `agent_report.md` | 覆盖统计、规则表、逐条决策、复核记录、成本口径、限制 |
| `agent_run_log.json` | 运行起止时间、模式、调用次数、决策计数、输入哈希、输出清单 |
| `stage3_evidence.json` | 4 张合成测试页的 COM 排版范围、像素墨迹、遮挡/图片重叠证据 |
| `host_review.json` | 宿主多模态工具对 V2/V3/V4 的看图结论，含输入图片 SHA-256 |
| `host_review_inputs/`（3 张） | 实际送审的 1 px 裁边图；哈希被 `verify_agent.py` 核对 |
| `review_prompts/`（3 份） | 实际发送的复核提示词；SHA-256 记录在 `agent_findings.json` |
| `evidence/`（4 张） | V1~V4 的标注审查图 |
| `render/stage3/`（4 张） | V1~V4 的干净页面图 |
| `api_client_test.json` | API 客户端桩测试结果（`real_model=false`） |
| `api_mode_guard.txt` | 无 Key 时 `api` 模式退出码 2、0 次调用 |
| `fuse_test_output.txt` | 融合回归测试输出（17/17） |
| `verify_output.txt` | 阶段三验收输出（108/108） |
| `测试总结.md` | 阶段三简短测试总结 |
| `mock_run/` | 模拟对照：`agent_findings.json`、`agent_report.md`、`agent_run_log.json`、3 份提示词；全部 `simulated=true` |

### 4.4 阶段二人工核对材料（`visual_validation/`）

| 路径 | 用途 |
|---|---|
| `visual_validation.md` | 4 条越界记录的渲染/标注核对表，**人工分类字段留空**；文中用 `pages/...`、`annotated/...` 相对路径引用图片 |
| `pages/`（4 张） | 原始渲染页，1920×1080 |
| `annotated/`（4 张） | 叠加对象框的标注图 |
| `annotation_meta.json` | 标注坐标、画布尺寸、源文件哈希 |
| `annotation_check.json` | 标注自检结果（尺寸、位置、墨迹、源文件未变） |
| `render_pages.ps1` | PowerPoint COM 导出脚本 |

---

## 五、运行日志与验收记录

| 文件 | 说明 |
|---|---|
| `outputs/agent/agent_run_log.json` | 阶段三唯一运行日志（时间、模式、计数、输入/输出） |
| `outputs/stage1_verification.json` | 阶段一验收记录 |
| `outputs/recheck/verification.json` | 阶段二验收记录 |
| `outputs/agent/fuse_test_output.txt` | 融合单测的原始输出 |
| `outputs/agent/verify_output.txt` | 阶段三验收的原始输出 |
| `outputs/agent/api_mode_guard.txt` | 无 Key 保护的原始输出 |

> 阶段一、阶段二没有单独的文本日志，运行信息分别写入上述 `verification.json`。

---

## 六、重复文件与临时文件

### 6.1 内容完全相同的文件（SHA-256 一致）

| 文件 A | 文件 B | 处理 |
|---|---|---|
| `outputs/agent/review_prompts/stage3_visual_cases_p2.txt` | `outputs/agent/mock_run/review_prompts/stage3_visual_cases_p2.txt` | **保留**：分别属于 host 结果与 mock 对照，各自报告会引用 |
| 同上 `p3.txt` | 同上 `p3.txt` | 保留 |
| 同上 `p4.txt` | 同上 `p4.txt` | 保留 |
| `outputs/recheck/render/AddAgendaSlide_TestA/slide03.png` | `visual_validation/pages/AddAgendaSlide_TestA_p3.png` | **保留**：前者被 `text_recheck.json` 引用，后者被 `visual_validation.md` 引用 |
| `outputs/recheck/render/CalibriToTimesTestA/slide14.png` | `visual_validation/pages/CalibriToTimesTestA_p14.png` | 保留，同上 |
| `outputs/recheck/render/CenterAlignTitles_TestA/slide04.png` | `visual_validation/pages/CenterAlignTitles_TestA_p4.png` | 保留，同上 |
| `outputs/recheck/render/CenterAlignTitles_TestA/slide05.png` | `visual_validation/pages/CenterAlignTitles_TestA_p5.png` | 保留，同上 |

### 6.2 近似重复（像素略有差异，不是副本）

- `outputs/agent/host_review_inputs/review_p{2,3,4}.png` 是 `render/stage3/slide0{2,3,4}.png` 去掉 1 px 边后的送审版本。
  两者哈希不同，且 `host_review.json` 记录了这两组哈希，**均不可删**。

### 6.3 临时文件 / 生成物

| 路径 | 性质 | 处理 |
|---|---|---|
| `__pycache__/` | Python 自动编译缓存（`.pyc` 数量随运行变化） | 保留原位；删掉也会自动重建，移动无收益 |
| `test_samples/stage3/__pycache__/` | 同上 | 保留 |
| `outputs/recheck/probe/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | **失效缓存**：记录的是旧版测试页（`deck_sha256=e5ea25fd…`），当前测试页为 `230b2335…`，缓存键按哈希生成，永远不会再命中 | **归档到 `_archive/`**（唯一移动的文件） |

---

## 七、用途待确认的文件

| 路径 | 不确定点 | 本次处理 |
|---|---|---|
| `outputs/recheck/probe/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | 初次盘点时无法确定它是否仍被读取；已通过 `deck_sha256` 与当前样本比对确认是旧版缓存 | 已确认失效，归档保留，不删除 |
| `visual_validation/render_pages.ps1` | 只在阶段二渲染时手动使用，未被任何 Python 调用 | 保留原位；若后续不再重渲染，可再议归档 |

> 除上述两项外，其余文件均能确认用途。没有对任何文件做删除操作。

---

## 八、第四阶段（SlideAudit / 传统机器学习）预留

现有结构中**没有**机器学习代码、数据集或模型文件。建议第四阶段使用独立目录，避免污染已验证的三阶段产物：

```text
ml_stage4/
├── README.md          阶段四目标、输入契约与边界
├── data/              后续放置数据集（本次不下载）
├── features/          特征抽取脚本（后续）
├── models/            模型与超参数（后续）
└── reports/           评估结果（后续）
```

可复用的输入（只读）：

- `outputs/stage1_structure.json`：对象级几何与文字特征；
- `outputs/stage1_violations.json`：阶段一越界候选；
- `outputs/recheck/text_recheck.json`：真实文字可见性标签；
- `outputs/agent/agent_findings.json`：规则/复核融合结论，可作为弱标签或对照基线。

**注意**：上述 JSON 内的路径是绝对路径，第四阶段若做数据集切分，应复制特征而不是移动原文件。

---

## 九、统计汇总

| 类别 | 数量 |
|---|---:|
| 根目录 Python 程序 | 11 |
| 子目录 Python 程序（阶段二复检 + 样本生成） | 4 |
| Markdown 文档（整理前，含 `mock_run/agent_report.md`） | 7 |
| 测试样本 PPTX | 3 |
| 阶段一 JSON（含 `selfcheck/` 2 份） | 5 |
| 阶段二 JSON（含 6 份探针缓存） | 8 |
| 阶段三 JSON（含 `mock_run/` 2 份） | 7 |
| 样本期望值 JSON（`test_samples/` 3 份） | 3 |
| 标注元数据 JSON（`annotation_meta` / `annotation_check`） | 2 |
| 渲染 / 标注 / 背景 PNG | 42 |
| 提示词与测试日志 TXT | 9 |
| PowerShell 脚本 | 1 |
| 合计（不含 `.pyc`） | **102** |

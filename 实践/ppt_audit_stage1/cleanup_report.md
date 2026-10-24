# 目录整理报告

- 整理时间：2026-10-24
- 工作目录：`实践/ppt_audit_stage1`
- 整理范围：只调整本目录内的文档与失效缓存；**未修改任何核心 Python 代码，未移动 outputs / test_samples / visual_validation，未删除任何文件**

---

## 一、结论摘要

| 项目 | 结果 |
|---|---|
| 移动的文件 | **1 个**（失效的 COM 探针缓存，移入 `_archive/`） |
| 新增的目录 | `docs/`、`_archive/`、`ml_stage4/`（含 4 个空子目录） |
| 新增的文档 | 6 份（盘点、整理报告、文档索引、数据流、归档说明、阶段四说明） |
| 删除的文件 | **0 个** |
| 核心 `.py` 是否移动 | **否，15 个全部原位** |
| 原始 PPTX 是否修改 | **否**，三次验收均比对 SHA-256 |
| 整理后验收 | 19/19、23/23、108/108、17/17、API 桩通过，与整理前一致 |

一句话：**这次是“加目录、加索引、归档一个失效缓存”，不是重构。**

---

## 二、变更清单

### 2.1 移动的文件（可恢复）

| 原路径 | 新路径 | SHA-256 | 理由 |
|---|---|---|---|
| `outputs/recheck/probe/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | `_archive/stale_probe_cache/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | `f9c894db099d21a187af73ff5b85d29eac4a5c72e6edded00dbdf3ec7dc943b0` | 探针缓存按 `deck_sha256` 命名；该文件对应旧版测试页 `e5ea25fd…`，当前测试页是 `230b2335…`，缓存永远不会再命中 |

恢复命令：

```bash
mv _archive/stale_probe_cache/stage3_visual_cases__all__e5ea25fd32c0__v2.json \
   outputs/recheck/probe/
```

### 2.2 新增的目录

| 目录 | 用途 |
|---|---|
| `docs/` | 文档导航与数据流说明；正式报告仍留在各阶段输出目录，避免拆散“报告 + 证据图” |
| `_archive/` | 归档区：1 个失效缓存 + 整理前后的 14 份测试日志 |
| `_archive/logs/` | `before_*.txt` / `after_*.txt`，用于对照整理前后结果 |
| `_archive/stale_probe_cache/` | 失效探针缓存 |
| `ml_stage4/` | 第四阶段预留，目前只有 README，无代码/数据/模型 |
| `ml_stage4/data/`、`features/`、`models/`、`reports/` | 空目录，仅占位 |

### 2.3 新增的文档

| 文件 | 内容 |
|---|---|
| `project_inventory.md` | 整理前的 102 个文件盘点：核心程序、测试、输出、日志、报告、重复文件、待确认项 |
| `cleanup_report.md` | 本文件 |
| `docs/README.md` | 文档索引：按“想看什么”导航到各阶段报告与验收记录 |
| `docs/data_flow.md` | 三阶段数据流、程序读写契约、路径约定、证据分级、阶段四接口 |
| `_archive/README.md` | 归档清单、原路径、哈希、恢复方法、归档约定 |
| `ml_stage4/README.md` | 第四阶段目标、目录约定、只读输入、进入条件与限制 |

### 2.4 更新的文档

| 文件 | 改动 |
|---|---|
| `README.md` | 重写为 8 节：项目目标、三阶段、核心程序、运行测试、结果位置、阶段四预留、已知限制、文档导航；明确写出“第二阶段未做全页检测”“第三阶段未完成真实 API 联调”“当前是规则调度原型” |

### 2.5 未移动的文件与原因

| 对象 | 数量 | 不移动的原因 |
|---|---:|---|
| 根目录 `.py` | 11 | 全部用 `Path(__file__).resolve().parent` 定位根目录；`agent_router.py` 还直接 `import review_client`，`test_fuse.py` 直接 `import agent_router` |
| `outputs/recheck/*.py` | 2 | `text_visibility_recheck.py` 用 `HERE.parents[1]` 反推根目录，并被 `verify_recheck.py`（子进程）和 `stage3_probe.py`（`importlib`）按固定路径加载 |
| `test_samples/*.py` | 2 | `make_test_decks.py` 用 `__file__.parent / "test_samples"`；样本生成器与期望值同目录 |
| `outputs/**`（JSON / PNG / 报告） | 67 | JSON 记录证据图绝对路径；`verify_agent.py` 按固定路径读取 prompts、报告、host 复核图并核对哈希 |
| `test_samples/**` | 10 | 三套样本被验证脚本硬编码读取；移动会改变 `deck_sha256` 与探针缓存键 |
| `visual_validation/**` | 12 | `visual_validation.md` 用 `pages/…`、`annotated/…` 相对路径引用图片；`annotate_visual.py` / `check_annotations.py` 按 `ROOT/visual_validation` 读写 |
| `__pycache__/` | 2 个目录 | Python 自动生成；移动无意义，删除也会重建 |

---

## 三、引用检查结果

整理前逐项检查了以下五类引用，确认“只有归档那个缓存”不会破坏程序：

| 检查项 | 结果 |
|---|---|
| Python 模块导入 | `agent_router → review_client`、`test_fuse → agent_router`、`verify_agent → test_fuse`、`test_api_client → review_client`，均要求同目录，已保持 |
| 基于 `__file__` 的相对路径 | **12 个脚本**使用 `Path(__file__).resolve().parent` 或 `.parents[1]`（含两个样本生成器）；全部原位，路径推导不变 |
| 代码中写死的路径 | `annotate_visual.py` 与 `check_annotations.py` 写死 `../python-ppt/downloaded_pptx` 与 `C:/Windows/Fonts`；`stage3_probe.py` 写死 `outputs/recheck/text_visibility_recheck.py`。这些脚本全部原位，未改代码 |
| README 中的运行命令 | 所有命令中的路径（`outputs/recheck/verify_recheck.py`、`test_samples/stage3/...`）在整理后仍然有效，已实际执行验证 |
| JSON 中记录的证据图片路径 | `stage1_structure.json`、`stage3_evidence.json`、`agent_findings.json` 中的图片路径均为绝对路径，图片未移动；`text_recheck.json` 中为相对 `outputs/recheck/` 的路径，目录未变 |

另外检查了 Markdown 相对链接：`README.md`、`docs/README.md`、`docs/data_flow.md`、`ml_stage4/README.md`、`_archive/README.md`、本报告共 10 条 Markdown 相对链接，整理后全部可解析；文档中以反引号标注的代码与数据路径也已逐项对照实际文件。

---

## 四、整理前后测试对照

所有命令均在项目根目录执行；`before_*` 与 `after_*` 原始输出保存在 `_archive/logs/`。

| 命令 | 整理前 | 整理后 | 说明 |
|---|---|---|---|
| `python verify_stage1.py` | 19/19 通过 | 19/19 通过 | 真实解析 6 份 PPT / 62 页；原始文件 SHA-256 与 mtime 未变 |
| `python outputs/recheck/verify_recheck.py` | 23/23 通过 | 23/23 通过 | 需要 PowerPoint COM；实际运行复检程序，生成 8 条 case |
| `python agent_router.py --reviewer host --max-calls 4` | 8 条 finding，3 页复核 | 8 条 finding，3 页复核 | 结果与整理前一致：2 确认、1 可能、3 无问题、2 不适用 |
| `python agent_router.py --reviewer mock ...` | 3 页模拟 | 3 页模拟 | 全部 `simulated=true` |
| `python verify_agent.py` | 108/108 通过 | 108/108 通过 | 含覆盖统计、路由、融合、安全断言 |
| `python test_fuse.py` | 17/17 通过 | 17/17 通过 | 含“本地低置信 + 视觉反向 → 证据冲突”用例 |
| `python test_api_client.py` | `passed=true`，`real_model=false` | 同左 | 本地 HTTP 桩，不是真实模型 |

关键统计在整理前后一致：

- 全局文字对象 **195** 个，已检查 **2** 个，`not_checked` **193** 个；
- 页面覆盖 `complete / partial / not_checked = 1 / 2 / 59`；
- 最终状态：确认问题 2、可能有问题 1、未发现问题 3、不适用 2；
- 第三方 API 调用 **0** 次，`real_api_integration.status = not_completed`。

> 说明：`verify_stage1.py`、`verify_recheck.py` 和 `agent_router.py` 会重写 `outputs/` 中的 JSON（更新 `generated_at`、哈希与运行日志）。
> 这是脚本的正常行为，不是整理造成的结构变化；重跑后证据链仍然自洽（`verify_agent.py` 的哈希核对全部通过）。

---

## 五、未执行的项目及原因

| 未执行 | 原因 |
|---|---|
| `python test_samples/stage3/make_stage3_cases.py` | 会重新生成测试页 PPTX，改变 `deck_sha256`，使现有 `stage3_evidence.json`、探针缓存与 `host_review.json` 的图片哈希失效；本次不需要重建样本 |
| `python stage3_probe.py` | 依赖上一条生成的 PPTX；样本未变，证据无需重算 |
| `python outputs/recheck/text_visibility_recheck.py --refresh` | `verify_recheck.py` 内部已经实际运行了复检程序（未加 `--refresh`，使用现有探针缓存），无需再跑一遍 |
| 真实 `api` 模式 | 本机无 `PPT_REVIEW_API_KEY`；按约束不购买服务、不调用付费接口 |

没有出现“测试无法运行”的情况。第二阶段的 COM 调用在本机 PowerPoint 16.0 下正常完成。

---

## 六、回滚方法

本次只有一处文件移动，回滚只需一步：

```bash
mv _archive/stale_probe_cache/stage3_visual_cases__all__e5ea25fd32c0__v2.json \
   outputs/recheck/probe/
```

新增目录不影响运行，可直接保留；如需完全回到整理前状态，删除 `docs/`、`_archive/`、`ml_stage4/`、
`project_inventory.md`、`cleanup_report.md`，并恢复旧版 `README.md` 即可。
`outputs/` 中的 JSON 因验收重跑已刷新时间戳，但字段与结论未变。

---

## 七、剩余问题

1. `visual_validation/render_pages.ps1` 是阶段二手动渲染脚本，没有 Python 调用它；用途基本明确，但后续是否还需要重渲染待定，本次保留原位。
2. `outputs/recheck/probe/` 下仍有 5 份有效缓存，删掉后需要重新打开 PowerPoint 才能重建；不建议当普通临时文件清理。
3. `outputs/agent/mock_run/review_prompts/` 与主目录 3 份提示词内容相同，但分属两套运行记录，未合并。
4. 项目没有 Git 仓库，本次整理无法用提交历史回溯；已用 `_archive/README.md` 和本报告记录移动明细。

---

## 八、整理后目录树（省略证据文件名）

```text
ppt_audit_stage1/
├── README.md                     项目入口（已更新）
├── project_inventory.md          整理前文件盘点（新增）
├── cleanup_report.md             本报告（新增）
├── audit_stage1.py               阶段一主程序
├── make_test_decks.py            阶段一自检样本
├── verify_stage1.py              阶段一验收
├── annotate_visual.py            阶段二标注
├── check_annotations.py          阶段二标注自检
├── agent_router.py               阶段三调度与融合
├── review_client.py              阶段三复核客户端
├── stage3_probe.py               阶段三证据生成
├── verify_agent.py               阶段三验收
├── test_fuse.py                  融合单测
├── test_api_client.py            API 客户端本地桩
├── docs/
│   ├── README.md                 文档索引（新增）
│   └── data_flow.md              数据流与路径约定（新增）
├── _archive/
│   ├── README.md                 归档清单（新增）
│   ├── logs/                     before/after 测试日志 14 份（新增）
│   └── stale_probe_cache/        1 份失效探针缓存（移入）
├── ml_stage4/
│   ├── README.md                 阶段四说明（新增）
│   ├── data/  features/  models/  reports/   （空目录）
├── outputs/
│   ├── stage1_structure.json
│   ├── stage1_violations.json
│   ├── stage1_verification.json
│   ├── 测试总结.md
│   ├── selfcheck/                阶段一自检输出
│   ├── recheck/                  阶段二：程序、JSON、报告、证据、探针、渲染
│   └── agent/                    阶段三：findings、报告、日志、复核证据、mock_run
├── test_samples/
│   ├── expected.json
│   ├── selfcheck_stage1.pptx
│   ├── recheck/                  对照样本 + 生成器 + 期望值
│   └── stage3/                   测试页 + 生成器 + 期望值 + 背景素材
└── visual_validation/
    ├── visual_validation.md      人工核对表（分类字段留空）
    ├── pages/  annotated/        渲染页与标注图
    ├── annotation_meta.json
    ├── annotation_check.json
    └── render_pages.ps1
```

文件总数：整理前 102 → 整理后 122（新增 6 份文档 + 14 份测试日志，另有 1 份缓存从 `outputs/` 移入 `_archive/`，未增减）。

---

## 九、是否影响后续开发

- 三阶段代码、数据与证据路径**完全不变**，现有测试可继续复跑；
- 第四阶段可以直接从 `ml_stage4/` 开始，按 `docs/data_flow.md` 的只读接口读取三阶段产物；
- 本次未进入第四阶段，未下载 SlideAudit、未训练模型、未接入新 API。

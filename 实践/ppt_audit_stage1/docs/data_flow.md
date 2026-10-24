# 三阶段数据流与依赖

本图只描述**现状**，不代表系统已经覆盖全部文字检测。虚线框表示“需要外部程序”：PowerPoint COM 或多模态看图。

```text
原始语料（项目外，只读）
  ../python-ppt/downloaded_pptx/*.pptx  (6 份 / 62 页 / 195 个含文字对象)
            │
            │  audit_stage1.py（只读解析 + 几何规则）
            ▼
  outputs/stage1_structure.json ──────────────┐
  outputs/stage1_violations.json（4 条越界）   │
            │                                 │
            │  text_visibility_recheck.py     │  stage3_probe.py
            │  （PowerPoint COM 读实际排版）   │  （复用第二阶段探针）
            ▼                                 ▼
  outputs/recheck/text_recheck.json      outputs/agent/stage3_evidence.json
  outputs/recheck/evidence/*.png         outputs/agent/evidence/*.png
  outputs/recheck/recheck_report.md      outputs/agent/render/stage3/*.png
            │                                 │
            └──────────────┬──────────────────┘
                           │  agent_router.py
                           │  R1~R8 规则 → skip / report / recheck
                           ▼
              ┌────────────┴─────────────┐
              │ recheck 才调用 review_client │
              │ api / host / mock 三选一     │
              └────────────┬─────────────┘
                           ▼
  outputs/agent/agent_findings.json（8 条 finding）
  outputs/agent/agent_report.md
  outputs/agent/agent_run_log.json
  outputs/agent/review_prompts/*.txt
```

## 一、各程序的读写契约

| 程序 | 读取 | 写出 |
|---|---|---|
| `audit_stage1.py` | `--input` 指定的 PPTX 目录 | `--out-dir` 下的 `stage1_structure.json`、`stage1_violations.json` |
| `verify_stage1.py` | `test_samples/selfcheck_stage1.pptx`、`test_samples/expected.json`、原始 6 份 PPTX | `outputs/stage1_verification.json`、`outputs/selfcheck/` |
| `text_visibility_recheck.py` | `outputs/stage1_*.json`、原始 PPTX、`test_samples/recheck/recheck_samples.pptx` | `outputs/recheck/text_recheck.json`、`recheck_report.md`、`evidence/`、`probe/`、`render/` |
| `verify_recheck.py` | 上述全部、`test_samples/recheck/expected.json` | `outputs/recheck/verification.json` |
| `stage3_probe.py` | `test_samples/stage3/stage3_visual_cases.pptx`、`expected.json`、第二阶段复检模块 | `outputs/agent/stage3_evidence.json`、`evidence/`、`render/stage3/` |
| `agent_router.py` | `outputs/stage1_*.json`、`outputs/recheck/text_recheck.json`、`outputs/agent/stage3_evidence.json`、`host_review.json`（host 模式） | `outputs/agent/agent_findings.json`、`agent_report.md`、`agent_run_log.json`、`review_prompts/` |
| `review_client.py` | 页面 PNG、环境变量（api 模式） | 复核记录（由 `agent_router.py` 汇总） |
| `verify_agent.py` | `outputs/agent/*.json`、`test_samples/stage3/expected.json`、原始 6 份 PPTX | 终端断言结果 |
| `annotate_visual.py` / `check_annotations.py` | `outputs/stage1_*.json`、`visual_validation/pages/` | `visual_validation/annotated/`、`annotation_meta.json`、`annotation_check.json` |

## 二、路径约定（整理时必须遵守）

1. **根目录脚本一律用 `Path(__file__).resolve().parent` 定位项目根目录**。
   移动脚本会连带改变 `ROOT`，所以 11 个根目录 `.py` 全部保持原位。
2. `outputs/recheck/text_visibility_recheck.py` 用 `HERE.parents[1]` 反推根目录，
   同时被 `verify_recheck.py` 以子进程、`stage3_probe.py` 以 `importlib` 按固定路径加载。
   移动它会同时破坏两处，**禁止移动**。
3. `outputs/` 下的 JSON 记录了大量证据图的绝对路径（例如 `page_png`、`annotated_png`），
   `visual_validation.md` 使用 `pages/…`、`annotated/…` 相对路径。
   移动这些图片会让证据链断开，**保持原位**。
4. `outputs/recheck/probe/` 是 COM 探针缓存，键名含 `deck_sha256`。
   缓存可在重跑时重建，但需要本机 PowerPoint；不要随意清理。
5. 原始 PPTX 只读。三个阶段的验证脚本都会比对 SHA-256，任何写回都会导致验收失败。

## 三、证据分级（阶段三）

| 字段 | 含义 | 取值 |
|---|---|---|
| `anomaly_level` | 异常程度，仅描述几何/版面信号强弱 | none / low / medium / high |
| `evidence_reliability` | 证据可靠性，取决于本地判定与复核置信度 | low / medium / high |
| `defect_severity` | 最终缺陷严重度，只有确认问题才给具体等级 | none / low / medium / high / unknown |

三者分开记录，避免把“越界很多”直接等同于“缺陷很严重”（例如 R1：`anomaly_level=high`，但 `defect_severity=none`）。

## 四、给第四阶段的接口

第四阶段（SlideAudit）可以直接读取以下**只读**产物做特征或弱标签：

- `outputs/stage1_structure.json`：对象级几何、文字、字号、组归属；
- `outputs/stage1_violations.json`：越界候选与方向/距离；
- `outputs/recheck/text_recheck.json`：真实文字可见性标签与覆盖台账；
- `outputs/agent/agent_findings.json`：规则融合结论与证据可靠性。

约束：**不要移动或覆盖**上述文件；需要新字段时写到 `ml_stage4/` 下，保持三阶段可复跑。

# 文档索引

本目录只放**导航性文档**。各阶段的正式报告仍留在其输出目录中，避免拆散“报告 + 证据图”的相对引用。

## 从哪里开始

| 想看什么 | 打开 |
|---|---|
| 项目目标、运行方式、已知限制 | [`../README.md`](../README.md) |
| 整理前的完整文件盘点 | [`../project_inventory.md`](../project_inventory.md) |
| 本次整理做了什么、测试结果 | [`../cleanup_report.md`](../cleanup_report.md) |
| 三个阶段的数据如何流转 | [`data_flow.md`](data_flow.md) |
| 第四阶段预留目录与输入契约 | [`../ml_stage4/README.md`](../ml_stage4/README.md) |

## 各阶段正式报告

| 阶段 | 报告 | 配套证据 |
|---|---|---|
| 一：结构解析与几何越界 | [`../outputs/测试总结.md`](../outputs/测试总结.md) | `outputs/stage1_structure.json`、`stage1_violations.json` |
| 二：文字可见性复检 | [`../outputs/recheck/recheck_report.md`](../outputs/recheck/recheck_report.md) | `outputs/recheck/evidence/`、`text_recheck.json` |
| 二：渲染与人工核对表 | [`../visual_validation/visual_validation.md`](../visual_validation/visual_validation.md) | `visual_validation/pages/`、`annotated/` |
| 三：规则调度与按需复核 | [`../outputs/agent/agent_report.md`](../outputs/agent/agent_report.md) | `outputs/agent/agent_findings.json`、`evidence/`、`host_review.json` |
| 三：简短测试总结 | [`../outputs/agent/测试总结.md`](../outputs/agent/测试总结.md) | `fuse_test_output.txt`、`verify_output.txt` |

## 验收记录

| 命令 | 结果文件 |
|---|---|
| `python verify_stage1.py` | `outputs/stage1_verification.json`（19 项） |
| `python outputs/recheck/verify_recheck.py` | `outputs/recheck/verification.json`（23 项） |
| `python verify_agent.py` | 终端输出；同时调用 `test_fuse.py` 的 17 项（共 108 项） |
| `python test_fuse.py` | 终端输出（17 项） |
| `python test_api_client.py` | `outputs/agent/api_client_test.json`（本地桩，非真实模型） |

## 阅读顺序建议

1. 先看根 `README.md` 的“当前已知限制”，确认系统边界；
2. 再看 `docs/data_flow.md`，理解三个阶段的证据如何串起来；
3. 需要复核某条结论时，按报告里的页码 / `shape_id` 到对应 JSON 和证据图核对。

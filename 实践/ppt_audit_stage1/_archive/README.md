# _archive — 归档区

本目录只存放**已确认不再被程序读取**的文件，便于追溯，不参与任何运行流程。
本轮整理仅归档 1 个文件，另有整理前的测试日志 7 份。

## 一、归档清单

| 归档后路径 | 原路径 | SHA-256 | 归档理由 |
|---|---|---|---|
| `stale_probe_cache/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | `outputs/recheck/probe/stage3_visual_cases__all__e5ea25fd32c0__v2.json` | `f9c894db099d21a187af73ff5b85d29eac4a5c72e6edded00dbdf3ec7dc943b0` | 阶段二 COM 探针缓存按 `deck_sha256` 命名。该文件记录旧版测试页 `e5ea25fd…`，当前 `test_samples/stage3/stage3_visual_cases.pptx` 为 `230b2335…`，缓存键不再匹配，永远不会被读取 |

## 二、整理前的基线测试日志

`logs/before_*.txt` 是 2026-10-24 整理前运行现有验收脚本的原始输出，用于对比整理前后是否一致：

| 文件 | 对应命令 | 结果 |
|---|---|---|
| `logs/before_verify_stage1.txt` | `python verify_stage1.py` | 19/19 通过 |
| `logs/before_verify_recheck.txt` | `python outputs/recheck/verify_recheck.py` | 23/23 通过 |
| `logs/before_agent_host.txt` | `python agent_router.py --reviewer host --max-calls 4` | 生成 8 条 finding，3 页复核 |
| `logs/before_agent_mock.txt` | `python agent_router.py --reviewer mock --out-dir outputs/agent/mock_run` | 3 页模拟，`simulated=true` |
| `logs/before_verify_agent.txt` | `python verify_agent.py` | 108/108 通过 |
| `logs/before_test_fuse.txt` | `python test_fuse.py` | 17/17 通过 |
| `logs/before_test_api.txt` | `python test_api_client.py` | 本地桩通过（非真实模型） |

## 三、恢复方法

需要恢复归档文件时，把文件移回原路径即可：

```bash
mv _archive/stale_probe_cache/stage3_visual_cases__all__e5ea25fd32c0__v2.json \
   outputs/recheck/probe/
```

该缓存即使放回也不会被当前测试页命中；如需旧版证据，应同时恢复对应版本的 `stage3_visual_cases.pptx`。

## 四、约定

- 归档文件**不删除**，只移动，并在本文件登记原路径与哈希。
- 归档区不放置 `outputs/` 中仍被 JSON 或验证脚本引用的文件。
- 后续如需清理，先在本表新增一行，再移动文件。

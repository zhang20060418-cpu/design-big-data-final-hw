# LLM 复核失败、默认开关与提前跳过修复

日期：2026-10-31。没有修改冻结检测模型、阈值、人工标签、PPTX 或历史研究工件。

## 实际原因

旧任务 `91b84b83454c4798` 通过原网关的 6 次复核全部收到 HTTP 502。用户切换 HTTPS 官方接口后，任务 `297fe2b0ba4e4f59` 的 6 次请求已经 HTTP 200，但均未通过 JSON 解析。因此最新失败不能再归因为 HTTPS 或调用次数不足。

对同一失败任务的第一个请求做两次受限对照（同样 17 张证据图、8 个候选），仅保存输出长度、usage、结束原因，不保存或展示模型思考文本：

| 请求 | 单次 max_tokens | 思考模式 | finish_reason | reasoning tokens | 最终内容 | 8 项严格校验 |
|---|---:|---|---|---:|---|---|
| 原参数 | 2800 | 服务默认 | length | 2800 | 0 字符 | 失败 |
| 控制对照 | 2800 | disabled | stop | 0 | 5468 字符 | 全部通过 |

这确认原请求的单次输出预算全部消耗在思考部分，最终没有 JSON。把「调用次数」设成 30 只改变请求数量，不会增加每个请求的输出 token 预算。

官方当前默认开启高思考模式，使用 `thinking:{type:disabled}` 可以关闭；JSON 文档也要求为完整 JSON 留足输出预算，并说明可能出现空白内容或 length 截断。[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

## 程序修复

- 对官方 `api.deepseek.com` 的 DeepSeek 结构化复核请求，默认采用非思考模式；其他服务保持原参数，调用方显式提供的 thinking/reasoning 偏好不会被覆盖。不添加偷偷重试或额外请求。
- 六类复核按输出合同增加单次 JSON 预算：`min(16000,max(4096,候选数×700))`。这与任务调用次数上限是不同设置；全局与任务调用预算仍取较小值。
- 记录 finish_reason、最终内容长度与是否存在思考内容；不保存模型思考正文。截断报 `output_truncated`，空内容报 `empty_final_response`，不会误称检查完成。
- HTTP 错误显示状态码和已脱敏的服务错误摘要；失败阶段显示真实页号、原因和未形成结论，不再统一写「执行完成」。旧失败任务通过只读投影显示已有错误，不改旧报告或 DB。
- 新增 `mllm_user_choice`：null 为随 API 配置自动，布尔值为用户明确选择。旧待审任务的默认 false 不再被当作主动关闭；用户实际关闭后会持久保留选择。
- 「跳过复核」放在多模态阶段旁。任务已开始、LLM 尚未开始时即可提前登记跳过；不会停止解析、渲染或首轮筛查。首轮完成后统一输出，并保留风险提示。已经发送复核请求时禁止假装取消。

## 真实验收

1. 真实官方 API 完整应用任务 `08041fc0de2f4e04`：复用用户这份 T07 PPTX，在隔离服务仅选第 3 页、限制一次真实调用。R2 → 5 秒窗口 → 真实 `deepseek-flash` → JSON 校验 → 报告全部成功，8 个候选全部返回、`finish_reason=stop`，没有思考 token。其余页因本次 QA 的一次调用限制保持未复检，最终范围状态为 partial，不宣称整份文稿完整复检。
2. 本次 provider 诊断和真实验收合计 **3 次真实调用**：原参数失败对照一次、非思考成功对照一次、完整应用验证一次。没有自动重发用户的六页失败任务。隔离服务临时配置已恢复、QA Key 已清除，日常服务 HTTPS 配置和密钥未改写。
3. 真实浏览器任务 `43e8a3b9d552446f`：在 render 阶段 26%、LLM pending 时点击阶段旁按钮；提前跳过持久化，随后 14 页、84 条 Claim 正常完成，自动进入结果。`reviews=[]`、真实调用 0，误诊/漏检提示可见。
4. 只读验证用户旧待审任务 `e2a1c3e5887c467f` 默认勾选；前端手动关闭不会被轮询打开，未保存或启动用户任务。
5. 后端 **218 项通过，0 失败、0 错误、0 跳过**；前端生产构建通过，原合同 8 项、新默认/跳过合同 9 项、浏览器响应 fixture 5 项通过。构建 `index-BHEB9UXq.js`。

完整证据位于 `llm_gate_fix_checks/`：`provider_original.json`、`provider_non_thinking.json`、`live_pipeline_check.json`、`live_pipeline_report.json`、`browser-live-early-skip.json`、`backend-tests.xml`。脚本位于 `backend/scripts/diagnose_review_api.py`、`check_live_llm_fix.py` 及 `frontend/scripts/check_early_skip_browser_live.mjs`；真实 API 脚本必须显式 `--live`，不会由测试套件自动执行。

应用服务已经重启加载修复。刷新页面后开始新的审查任务即可使用；旧失败记录保持历史事实，不会被自动改成成功，也不会自动重发已执行请求。

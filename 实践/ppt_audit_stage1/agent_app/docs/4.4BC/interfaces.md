# 六类检测与按需复核接口

## 业务对象

原 `Issue` 继续作为可展示的 Finding，增加 `candidate_id`、`claims`、`locations`、`source_conflicts`、`initial_conclusion`、`review_history`、`related_slides`。原结构规则、可选视觉插件、LLM 复核使用同一个 Finding 流程。

`DetectorClaim` 是原始观察，不能因复核而改写：

| 字段 | 定义 |
|---|---|
| document_id / slide_id / slide_index / candidate_id | 上传任务、稳定页面与候选身份 |
| defect_type / subtype | 六类英文标识；D6 可细分 A01–A05 |
| detector_version / input_version | 原检测器版本、实际原稿/PNG/上下文摘要 |
| raw_status | candidate / no_candidate_in_available_scope / unknown / unsupported / not_checked |
| score / score_kind | 可为空的有序强度；不是缺陷概率 |
| object_ids / native_bbox | 原生对象定位；应用外框统一为英寸，原证据保留测量单位 |
| normalized_bbox | 供前端定位的页面内 [0,1] 坐标；显示外框裁到页面内，原越界包络仍保留 |
| related_slides | 本文稿关联页号，D6-A01 可多页 |
| evidence / coverage / limitations | 几何、排版、像素或跨页证据，以及未知范围 |

部分范围未知、但已有局部证据达到冻结筛查规则的观察，允许生成“待核验风险” Finding；仍保留原始 unknown，不将其改成阳性。

## REST 增量接口

| 方法与路径 | 用途 |
|---|---|
| POST `/api/tasks` | 原上传入口；multipart file/options/prepare_only |
| POST `/api/tasks/{id}/start` | 原准备后启动入口；原八阶段流水线 |
| GET `/api/tasks/{id}/claims` | 原始六类 Claim 与能力状态；旧任务无记录时为未检查范围 |
| GET `/api/tasks/{id}/review-plan` | 选择/跳过原因、预算、工具、证据需求、执行 outcome |
| GET `/api/tasks/{id}/issues` | 原问题列表；现增加六类、定位与关联页信息 |
| GET `/api/tasks/{id}/issues/{issue_id}/reviews` | 该 Finding 的追问历史 |
| POST `/api/tasks/{id}/issues/{issue_id}/review` | `{ "question": "补充设计意图", "llm_backend": "mock" }`；api 模式需真实配置和剩余预算 |
| GET `/api/tasks/{id}/report?format=html\|json\|pdf&download=true` | 既有报告格式扩展，PDF 由同一 HTML 打印 |
| GET `/api/system/capabilities` | 六类配置、依赖要求、A05 语义限制、旧插件和服务状态 |

所有原 `mode=real/demo` 隔离、旧上传/查询/演示接口保留。新客户端显式传 review_strategy；旧客户端未传时保持旧四类页面复核兼容路径。新增任务建议使用新的候选级复核。

`ReviewOptions` 新增：`enable_six_detectors`、`purpose`、`style`、`review_focus`、`original_prompt`、`review_strategy=full_review|selective_review`、`llm_backend=api|mock`。预算继续用原 `max_api_calls`、`max_review_pages`。Mock 需显式选择；真实 API 仅在任务显式启用且配置完整时发送选中的页面和证据。

## 调度、Prompt 与响应合同

- `ReviewPlanner.plan(issues, claims, rendered, options, settings)` 输出 items/calls/budget；每项保留 selection_reason、selected_tool、evidence_requested、skip_reason、outcome、execution_trace。
- 选中的页面增加六类 `sixscope` 待查范围，避免仅确认已报警项目。它们不是阳性标签；预算或渲染失败后的范围仍未知。
- 跨页图片计入页面预算，真实 HTTP 尝试计入累计调用预算。首轮与后续追问共同受任务 max_api_calls、全局 max_calls_per_task 两者较小值约束；Mock 的实际 HTTP 为 0，模拟调度另行标记。新流程不隐藏重试；超时/错误不会形成 supported/rejected。
- D6 子候选 ID 为 `父candidate_id:A01` 等，保留 parent_candidate_id。子项复核记录独立保留，不能因 A05 uncertain 或 A01 rejected 覆盖整个 D6 裁决。
- 可信系统消息加载版本化规则。用户用途、页面文本、原生成 Prompt、用户追问和候选内容均为不可信审查数据。任务符合性独立输出，不混入六类缺陷指标。
- LLM 必须对输入 ID 完整且一一对应地返回 `supported|rejected|uncertain`，并给出实际 evidence_refs、object_ids、severity、recommendation、recommendation_level。服务端补充模型、规则版本、耗时、usage、cost、调用与失败轨迹。
- 未提供的 ID、坐标和证据引用被拒绝。L2 数值建议必须引用实际测量。单页证据不能支持 A01 跨页结论。JSON 错误或超时返回 error，verdicts 为空。
- Mock 固定为不确定，`mock=true`、`model=mock-inconclusive`、`actual_http_calls=0`。未知 usage/cost 为 null，不能当作真实服务的零费用。

## 任务工件

默认位于 `backend/data/tasks/{id}/`，可用 `PPT_AGENT_DATA_DIR` 隔离；不向研究数据目录写入。

| 位置 | 内容 |
|---|---|
| source/source.pptx | 原始上传副本，检测不覆盖 |
| renders/slides.json、原 PNG、native_contexts.json | 同次 COM 渲染及原生段落测量 |
| six_detector/raw_detection.json、adapter_result.json | 实际六类推理与适配结果、SHA |
| analysis/claims.json、observations.json、review_plan.json | 不可变观察与可核查调度 |
| analysis/initial_report.json | 首次交互前报告快照 |
| reviews/history.jsonl | 用户问题、前后结果与复核历史，逐次追加 |
| report/report.json、report.html、report.pdf | 最终报告；PDF 缓存随 HTML 更新时间失效 |

真实 LLM 设置复用本机原设置入口和密钥存储。配置和密钥不会写入交付摘要；验收服务使用独立数据目录，无外部 API Key。

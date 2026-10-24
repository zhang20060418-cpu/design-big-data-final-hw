# PPT 质量审查智能体 · 最小闭环报告

生成时间：2026-10-24T17:40:48+08:00　规则版本：1.0　复核模式：`host`

## 一、结论摘要

- 融合结论：候选对象 8 条（真实语料 4 条 + 合成用例 4 条）：2 条确认问题、1 条可能有问题、0 条待人工核实、3 条未发现问题、2 条不适用（无可见文字）；真实语料 195 个文字对象中只检查了 2 个，另有 193 个未检查对象保持 not_checked / 待人工核实，不能据此宣称整套 PPT 无文字缺陷。
- 复核调用：3 次视觉复核（宿主 3 次 / 第三方 API 0 次），HTTP 请求 0 次；队列 3 页，成功复核 3 页，上限 4 次。
- 真实 API 联调：`not_completed`——本机未配置 PPT_REVIEW_API_KEY / OPENAI_API_KEY，未发起任何第三方 API 调用；真实 API 联调尚未完成
- 说明：宿主视觉工具复核：未配置 API Key，程序未发起 HTTP 请求；Token/费用不可得，不等同于第三方 API 复核

| 最终状态 | 数量 |
| --- | ---: |
| 确认问题 | 2 |
| 可能有问题 | 1 |
| 待人工核实 | 0 |
| 未发现问题 | 3 |
| 不适用 | 2 |

## 二、第二轮到底覆盖了多少对象

- 语料：6 份 PPT、62 页、195 个含文字对象。
- 第一阶段：对 195 个文字对象做了对象外框越界检测（只看外框）。
- 第二阶段：只复检第一阶段 4 个越界候选，涉及 4 页；这 4 页共有 19 个含文字对象，实际检查 2 个、跳过 17 个。
- 全局文字可见性覆盖率：2/195 = 1.0%；其余 193 个标记为 `not_checked`。
- 页面覆盖状态：complete 1 页 / partial 2 页 / not_checked 59 页（共 62 页）。`complete` 只表示本页待查文字都查过，不表示整份 PPT 查完。

| 候选页 | 含文字对象 | 已检查 | 未检查 | 第二阶段候选对象 | 页面覆盖状态 |
| --- | ---: | ---: | ---: | ---: | --- |
| AddAgendaSlide_TestA.pptx p3 | 3 | 1 | 2 | 1 | partial |
| CalibriToTimesTestA.pptx p14 | 3 | 1 | 2 | 1 | partial |
| CenterAlignTitles_TestA.pptx p4 | 10 | 0 | 10 | 1 | not_checked |
| CenterAlignTitles_TestA.pptx p5 | 3 | 0 | 3 | 1 | not_checked |

> 第 4 页有 10 个含文字对象，实际检查 0 个、未检查 10 个；该页唯一的第二阶段候选是空形状（AutoShape 2），其 `not_applicable` 只说明“该对象没有文字”，不能推断整页文字没有裁切风险。

> `not_checked` 只表示没有做文字可见性检查，不等于没有缺陷；本报告不会把未检查对象计入“未发现问题”。

## 三、路由规则（按顺序命中，第一条命中即停止）

| 规则 | 决策 | 触发条件 | 依据 | 是否省掉调用 |
| --- | --- | --- | --- | --- |
| R1 空形状跳过 | 跳过 | has_text=false 且 empty_graphics=true（无填充、无边框、无效果） | 对象在渲染中不可见，文字可见性检查不适用；保留结构记录即可 | 是 |
| R2 无文字对象跳过 | 跳过 | has_text=false，但存在可见填充或边框 | 对象可见但不承载文字，本轮只审文字可见性，不产生文字缺陷结论 | 是 |
| R3 已确认裁切直接记录 | 直接记录 | second_pass=confirmed_clip 且 confidence=high | COM 文字范围越界 + 页边像素墨迹两项独立证据一致，无需再问模型 | 是 |
| R4 不确定进入视觉复核 | 视觉复核 | second_pass=uncertain，或 confidence=low，或 needs_multimodal_review=true | 规则证据不足或相互矛盾，交由视觉模型补充可观察证据 | 否 |
| R5 上层不透明对象遮挡 | 视觉复核 | 更高 z 序、填充可见的对象覆盖文字范围 ≥15% | 页边裁切规则看不到遮挡；这是结构性可见性风险，需要看图确认 | 否 |
| R6 图片背景可读性 | 视觉复核 | 文字范围与图片重叠 ≥50%，且无页边裁切 | 无裁切不等于可读；文字压在复杂图片上属于人工判断不确定的情况 | 否 |
| R7 无可见裁切直接记录 | 直接记录 | second_pass=no_visible_clip、confidence=high，且无质量信号/遮挡/图片背景风险 | COM 排版范围完整在页内，且墨迹不贴边，足以否定“页面裁切” | 是 |
| R8 未检查记录覆盖缺口 | 直接记录 | coverage_status=not_checked（第二轮未检查该对象） | 没有可见性证据，只能记录“待人工核实”，不得写成“未发现问题” | 是 |

## 四、逐条决策

| 案例 | 文件/页/对象 | 第一阶段 | 第二阶段 | 覆盖 | 决策 | 规则 | 最终状态 | 严重度 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-R1 | AddAgendaSlide_TestA.pptx p3 id=13 | high | no_visible_clip | checked | 直接记录 | R7 | 未发现问题 | none |
| F-R2 | CalibriToTimesTestA.pptx p14 id=4 | medium | no_visible_clip | checked | 直接记录 | R7 | 未发现问题 | none |
| F-R3 | CenterAlignTitles_TestA.pptx p4 id=4 | low | not_applicable | not_applicable | 跳过 | R1 | 不适用 | none |
| F-R4 | CenterAlignTitles_TestA.pptx p5 id=4 | low | not_applicable | not_applicable | 跳过 | R1 | 不适用 | none |
| F-V1 | stage3_visual_cases.pptx p1 id=3 | - | no_visible_clip | checked | 直接记录 | R7 | 未发现问题 | none |
| F-V2 | stage3_visual_cases.pptx p2 id=3 | high | uncertain | checked | 视觉复核 | R4 | 确认问题 | high |
| F-V3 | stage3_visual_cases.pptx p3 id=3 | - | no_visible_clip | checked | 视觉复核 | R5 | 确认问题 | high |
| F-V4 | stage3_visual_cases.pptx p4 id=3 | - | no_visible_clip | checked | 视觉复核 | R6 | 可能有问题 | low |

## 五、视觉复核记录

### C01 · stage3_visual_cases.pptx#p2

- 模式：`host`；模型：`host-multimodal (current session image understanding)`；HTTP：—；耗时：— ms
- 图片：`D:\Codex_Project\Homework\Design_big_data\Final_Hw_修改\实践\ppt_audit_stage1\outputs\agent\render\stage3\slide02.png`；prompt 指纹：`c51f49c52783c702`
- Token：不可得（该模式不返回 usage）
- 费用：不可得
- 页面判断：正文最后一行被页面下边界横向切断，确认可见裁切。
  - shape_id=3 → `visible_clip`（high）：页面底部只看到两行白色正文：第一行完整，第二行在 y≈1020-1080px 处被页面下边界横向切断，字形的下半部分缺失。 “and reduce permeability in the invaded zone; the last lines of this”这句的下缘没有收笔，说明不是自然换行，而是被裁掉。 第三行完全没有出现在画面内；按 COM 记录，文字框底部越出页面 48.0 pt，与截图一致。 背景为深色星云照片，页边墨迹占比 47.4%，但主背景占比只有 12.4%，所以规则侧仍将像素证据标为不可靠。

### C02 · stage3_visual_cases.pptx#p3

- 模式：`host`；模型：`host-multimodal (current session image understanding)`；HTTP：—；耗时：— ms
- 图片：`D:\Codex_Project\Homework\Design_big_data\Final_Hw_修改\实践\ppt_audit_stage1\outputs\agent\render\stage3\slide03.png`；prompt 指纹：`403b7fa427595674`
- Token：不可得（该模式不返回 usage）
- 费用：不可得
- 页面判断：正文被上层红色矩形覆盖，中间语义断裂，确认遮挡。
  - shape_id=3 → `visible_occlusion`（high）：红色矩形位于页面中部 x≈655-1160px、y≈382-662px，直接压住正文第 1-3 行的中间部分。 被覆盖区域的文字完全不可见：第一行只剩“The conclusion of this slid”，第三行右侧只剩“fect.”，中间文字被整块遮住。 文字本身没有越出页面边界，四边均有余量；问题是叠放次序导致的遮挡。

### C03 · stage3_visual_cases.pptx#p4

- 模式：`host`；模型：`host-multimodal (current session image understanding)`；HTTP：—；耗时：— ms
- 图片：`D:\Codex_Project\Homework\Design_big_data\Final_Hw_修改\实践\ppt_audit_stage1\outputs\agent\render\stage3\slide04.png`；prompt 指纹：`1e56e8fc76004b71`
- Token：不可得（该模式不返回 usage）
- 费用：不可得
- 页面判断：无裁切、无遮挡，但文字与花哨背景对比度不足，可读性存疑。
  - shape_id=3 → `low_readability`（medium）：浅黄色文字位于米黄、金色纸屑背景上，x≈130-1360px、y≈440-570px，字色与背景亮度非常接近。 文字没有被页面四边裁切，也没有被其他对象覆盖；主要问题是局部对比度不足，阅读费力。 背景纸屑的高频纹理进一步干扰字形边缘，属于设计判断而非确定性缺陷。

## 六、融合结论与修改建议

### F-R1 · AddAgendaSlide_TestA.pptx p3 id=13

- 异常程度：`high`；证据可靠性：`high`；最终缺陷严重度：`none`
- 路由：report（R7）——COM 可见文字范围完整落在页内，且渲染墨迹不贴边；按页面裁切定义不成立
- 最终状态：**未发现问题**——COM 可见文字范围完整落在页内，墨迹不贴边

### F-R2 · CalibriToTimesTestA.pptx p14 id=4

- 异常程度：`medium`；证据可靠性：`high`；最终缺陷严重度：`none`
- 路由：report（R7）——COM 可见文字范围完整落在页内，且渲染墨迹不贴边；按页面裁切定义不成立
- 最终状态：**未发现问题**——COM 可见文字范围完整落在页内，墨迹不贴边

### F-R3 · CenterAlignTitles_TestA.pptx p4 id=4

- 异常程度：`low`；证据可靠性：`high`；最终缺陷严重度：`none`
- 路由：skip（R1）——对象无文字、无填充、无边框、无可见效果，渲染不可见；文字可见性检查不适用，保留结构记录即可
- 最终状态：**不适用**——不可见空形状（无文字、无填充、无边框、无可见效果），文字可见性检查不适用；不代表整页没有文字缺陷

### F-R4 · CenterAlignTitles_TestA.pptx p5 id=4

- 异常程度：`low`；证据可靠性：`high`；最终缺陷严重度：`none`
- 路由：skip（R1）——对象无文字、无填充、无边框、无可见效果，渲染不可见；文字可见性检查不适用，保留结构记录即可
- 最终状态：**不适用**——不可见空形状（无文字、无填充、无边框、无可见效果），文字可见性检查不适用；不代表整页没有文字缺陷

### F-V1 · stage3_visual_cases.pptx p1 id=3

- 异常程度：`none`；证据可靠性：`high`；最终缺陷严重度：`none`
- 路由：report（R7）——COM 可见文字范围完整落在页内，且渲染墨迹不贴边；按页面裁切定义不成立
- 最终状态：**未发现问题**——COM 可见文字范围完整落在页内，墨迹不贴边

### F-V2 · stage3_visual_cases.pptx p2 id=3

- 异常程度：`high`；证据可靠性：`high`；最终缺陷严重度：`high`
- 路由：recheck（R4）——规则证据不足或相互矛盾：页边背景复杂，无法可靠区分文字墨迹与背景
- 视觉证据：`visible_clip`（high）：页面底部只看到两行白色正文：第一行完整，第二行在 y≈1020-1080px 处被页面下边界横向切断，字形的下半部分缺失。 “and reduce permeability in the invaded zone; the last lines of this”这句的下缘没有收笔，说明不是自然换行，而是被裁掉。 第三行完全没有出现在画面内；按 COM 记录，文字框底部越出页面 48.0 pt，与截图一致。 背景为深色星云照片，页边墨迹占比 47.4%，但主背景占比只有 12.4%，所以规则侧仍将像素证据标为不可靠。
- 证据位置：页面底边 y≈1020-1080px，第二行文字下缘被切断; 文字块 x≈144-1636px，y≈940-1080px
- 最终状态：**确认问题**——视觉模型在截图中观察到文字被页面边界切断；与 COM 越界 48.0 pt 一致
- 建议：缩短文字或减小字号，或上移/缩小文本框，使可见文字距页面边缘至少 6 pt；同时检查自动缩放是否真正生效

### F-V3 · stage3_visual_cases.pptx p3 id=3

- 异常程度：`high`；证据可靠性：`high`；最终缺陷严重度：`high`
- 路由：recheck（R5）——文字虽未越出页面，但被更高 z 序的不透明对象覆盖；页边裁切规则天然看不到这类问题
- 视觉证据：`visible_occlusion`（high）：红色矩形位于页面中部 x≈655-1160px、y≈382-662px，直接压住正文第 1-3 行的中间部分。 被覆盖区域的文字完全不可见：第一行只剩“The conclusion of this slid”，第三行右侧只剩“fect.”，中间文字被整块遮住。 文字本身没有越出页面边界，四边均有余量；问题是叠放次序导致的遮挡。
- 证据位置：红色遮挡块 x≈655-1160px，y≈382-662px; 被截断的第一行 x≈160-655px 与 x≈1160-1360px
- 最终状态：**确认问题**——视觉模型确认文字被上层对象覆盖；结构侧重叠比例 46.4%
- 建议：调整叠放次序，把正文置于顶层；或为文字让出空间，避免不透明形状压住正文

### F-V4 · stage3_visual_cases.pptx p4 id=3

- 异常程度：`low`；证据可靠性：`medium`；最终缺陷严重度：`low`
- 路由：recheck（R6）——无裁切不等于可读；文字整体压在图片上，需要看图判断对比度
- 视觉证据：`low_readability`（medium）：浅黄色文字位于米黄、金色纸屑背景上，x≈130-1360px、y≈440-570px，字色与背景亮度非常接近。 文字没有被页面四边裁切，也没有被其他对象覆盖；主要问题是局部对比度不足，阅读费力。 背景纸屑的高频纹理进一步干扰字形边缘，属于设计判断而非确定性缺陷。
- 证据位置：正文区域 x≈130-1360px，y≈440-570px; 页面四边：文字距最近边缘 >20pt，无裁切
- 最终状态：**可能有问题**——文字未被裁切/遮挡，但视觉模型判断对比度或背景干扰影响阅读；属于设计判断，需人工确认
- 建议：提高文字与背景对比度（加深字色、加半透明底板或弱化背景图），并请设计者确认

## 七、调度成本与未覆盖项

| 指标 | 数量 | 说明 |
| --- | ---: | --- |
| 候选对象 | 8 | 4 条真实候选 + 4 条合成用例 |
| 规则直接闭环 | 5 | skip + report，未请求视觉复核 |
| 需要视觉复核 | 3 | 进入复核队列的候选对象 |
| 复核队列页数 | 3 | 按页去重 |
| 实际成功复核页数 | 3 | 有响应且无错误 |
| 未检查文字对象 | 193 | 覆盖缺口，**不计入节省的调用** |

- 候选对象 8 个：规则直接闭环 5 个，进入视觉复核 3 个。
- 复核队列 3 页，实际成功复核 3 页；按页去重，最多 4 次。
- 193 个未检查文字对象不计入“节省的调用”：它们是覆盖缺口，状态保持 not_checked / 待人工核实，不能用“省了调用”来解释。

## 八、局限

- 本次运行未配置 PPT_REVIEW_API_KEY，第三方 API 调用为 0；3 次复核由宿主多模态图像工具完成（mode=host），Token 与费用不可得，不等同于第三方 API 复核。
- 第二轮只覆盖第一阶段越界候选，不是全页文字扫描；193 个文字对象为 not_checked，不能按“无缺陷”处理。
- 真实第三方 API 联调尚未完成：本机没有配置 PPT_REVIEW_API_KEY / OPENAI_API_KEY。host 模式是宿主看图，不是第三方 API 调用。
- 页面裁切只回答“文字是否被页面边界切断”，不回答内容正确性、语义或版式美观。
- 遮挡规则基于 COM z 序、Fill.Visible 与轴对齐外接框，旋转/组合对象可能低估重叠。
- 图片背景可读性规则只做区域重叠判断，是否真的难读必须由视觉复核或人工确认。
- 视觉模型只看到导出的静态 PNG；动画、切页效果、字体替换后的差异不在检查范围。
- 合成测试页用于验证调度与复核能力，不代表真实语料的缺陷分布。

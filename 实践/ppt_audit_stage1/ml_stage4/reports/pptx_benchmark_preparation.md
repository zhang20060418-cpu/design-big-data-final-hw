# 4.3G-1：独立PPTX基准准备与人工核验

**当前状态：待人工核验或门槛待补齐；frozen=false，ready_for_final_test=false。本轮没有模型训练、特征提取或新基准预测。**

实际使用3份用户原稿，共84页未修改原始页面与24个可控修改样本。标签生成时均为unknown；以下统计依据当前manifest及人工审核字段，不根据修改操作或原稿身份自动赋值。

## 1. 来源与独立性

用户于2026-10-26声明：工具包括Codex/Kimi，逐份具体工具不清楚；约近一个月生成；均未导入本PPT审查项目测试。声明和本地生成链证据分别记录；导出器名称不能单独证明AI平台。

| 文稿ID | 当前源文件 | 页数/真实页面比例 | 来源机制与当前分类 | 当前原始SHA-256 |
| --- | --- | --- | --- | --- |
| D01_96b385d0 | D:\Codex_Project\Homework\User_experience\竞品分析\输出\竞品分析_Google_Marine_Layer_v1.0.pptx | 18 / 16:9 | ai_assisted_script_authored，C | `96b385d06c4ac1a62ebe987bae4ddf983249930f99bc571f700eef12bc5e812e` |
| D02_7d908509 | D:\Codex_Project\Homework\Game_design\张家乐_AIGC实践作业\ppt.pptx | 28 / 16:9 | ai_assisted_editable_reconstruction，C | `7d9085096ad473c5606699f6a06cc8a6a138af73a322434bb16211429b6a06da` |
| D03_e079826d | D:\Codex_Project\Homework\Game_design\计算机游戏程序设计_前三次作业说明与方法指导_助教版_修订.pptx | 38 / 16:9 | ai_generation_user_attested_tool_unknown，A_candidate | `e079826dc96f7f3f0d4453330ea432dada66f32f094ecd7996d733d4cdefb6b0` |

当前文稿来源分类：A_candidate=1、C=2。A=来源已核实的自然AI文稿；B=旧工程调试样本；C=人工或脚本构建，包括AI辅助脚本及可编辑转译；A_candidate=用户声明AI生成、生成链待核实。natural仅表示未修改原页，不把C自动变为自然AI来源。

本地旧PPTX摘要/名称调查与用户未使用声明均保留在source_inventory.csv、source_plan.json；扫描不能证明所有历史会话绝无接触。未读取SlideAudit test，未使用旧validation成功/失败页选择新稿。

D02与D03同属Game_design课程，主题和素材可能相关；3份文件不等于3个独立生成平台或完全独立主题群体，页面数不能掩盖文稿相关性。D03包内2026-09-23时间可能继承模板，不能代替用户口述的实际生成日期。

source_inventory.csv调查48个路径、30个不同SHA；复制、历史版本及同主题重制不计独立文稿。未选文件只做ZIP/XML检查，不宣称均已COM渲染。

- D01_96b385d0：本地COM编排脚本可查；成稿有后续PowerPoint修改，历史交付SHA与现稿不同，不能声称完整逐字节生成链；来源状态以manifest当前provenance_status及实际证据为准。
- D02_7d908509：可编辑转译/字体与讲稿修复；上传文件校验与现稿SHA一致；不冒充未加工的一次平台生成；来源状态以manifest当前provenance_status及实际证据为准。
- D03_e079826d：用户声明AI生成；具体工具和生成机制待核实；包内时间2026-09-23可能继承模板，与近一个月口述略异，不能作为实际生成日期；来源状态以manifest当前provenance_status及实际证据为准。

## 2. 子集、修改与同源关系

未修改原页84张、可控修改尝试24张。每稿固定原页2与5各制作font_small、occlusion、clutter、overflow四版，规则先于任何新模型预测确定。每版保留文稿，仅修改登记页；原文件不覆盖。

字号干预设8pt并关闭AutoSize；遮挡添加不透明对象；拥挤复制正文对象并压缩间距；裁切将内容移到边界外。具体操作、shape ID和几何见operations_controlled.json。操作意图不等于缺陷成立，允许原页已有缺陷、多标签或无效干预。

首次controlled尝试因目标选择的≥30字符限制过严、子进程Get-FileHash不可用而失败；已按最小范围修复后重跑。首次日志及12份尝试PPTX保留在ml_stage4/.cache/benchmark_build/failed_attempt_01，不纳入有效基准或成功统计；下列最终样本状态只来自当前正式operations日志和manifest实际检查。

original_slide_id使用真实native SlideID，另列1基页序；同原页各版继承group_id，document_id保留文稿相关性。全部预留independent_test_reserved，不供调参；旧几何探针只在calibration，排除评估。

目前干预只覆盖每稿第2与第5页，且每类只有固定操作强度，不能代表全部版式与缺陷程度。4:3仅有渲染兼容探针，尚无真实独立4:3自然来源。

| 来源 × 页面子集 | 样本数 |
| --- | ---: |
| A_candidate/controlled | 8 |
| A_candidate/natural | 38 |
| C/controlled | 16 |
| C/natural | 46 |

## 3. 渲染、字体与工件

PowerPoint COM按PageSetup真实比例、长边1920导出原始PNG。可控版保存关闭后再只读重开；清单记录原PNG像素尺寸与实际页尺寸，校验解码、SHA及比例。未使用旧固定1920×1080方式强制导出4:3。

清单渲染/操作失败0个。几何探针16:9→1920×1080、4:3→1920×1440，圆/矩形、四边标记与包围框映射通过=True。探针证明入口几何，不冒充新增自然4:3稿；实际来源比例见上表。

Codex已查看84原页的缩略总览、6组原页/修改版对照及6张选定原页大图，并抽看字号、遮挡和越界原图；这仅是渲染初查，不是人工缺陷核验。D01第2页的字号/拥挤干预集中在底部窄带，可能不足以形成目标缺陷，已在review_notes提醒，保持unknown。

字体显式/主题/嵌入名称与COM诊断见font_and_source_diagnostics.json。名称缺失、别名、继承及字形fallback不能仅凭安装列表判定；render_review_status及font_review_status由人工逐页确认。原模型输入没有诊断框或标注；.cache中的缩略图只供核验。

## 4. 人工标签与冻结

当前有记录的人工核验者0人：尚无；四标签及人工身份时间字段完整的页面0张，尚未完成108张。人数不等于独立多人一致性评估；当前未记录多人一致性或kappa。

0也需要人工确认不存在，unknown不作阴性；正负数量仅统计annotation_status=reviewed、human_reviewed=yes且reviewer_id与ISO时间有效的人工记录。未审核的数字标签不计确认数量。

| 标签 | 人工确认正例 | 人工确认负例 | unknown单元格 |
| --- | ---: | ---: | ---: |
| improper_font_sizing | 0 | 0 | 108 |
| occluded_content | 0 | 0 | 108 |
| cluttered_layout | 0 | 0 | 108 |
| content_overflow_cutoff | 0 | 0 | 108 |

未核验的数字标签单元格：0；有unknown样本108个、unknown单元格432个。

流程：待生成→待人工核验→核验完成→数据冻结。review_protocol.md给出判定说明，review_gallery.html并排显示原页与修改版。冻结检查所有拟冻结行，不自动筛掉unknown；A/C来源及natural/controlled必须独立报告。

当前冻结阻塞项：
- human four-label review incomplete: 108 samples
- render_review_status gate incomplete/invalid: 108 samples
- font_review_status gate incomplete/invalid: 108 samples
- provenance_status gate incomplete/invalid: 46 samples
- source_class gate incomplete/invalid: 46 samples

## 5. 产物与复现

benchmark_manifest.csv、source_inventory.csv、source_plan.json、原/修改PPTX、原始renders、画廊、核验协议及两份operations日志位于benchmark_pptx。prepare_benchmark.py和modify_and_render.ps1不修改前三阶段、旧划分、模型、阈值或旧特征。

```powershell
.\.venv\Scripts\python.exe -B benchmark_pptx\prepare_benchmark.py --check
# 人工编辑清单后刷新画廊和报告，不自动赋标签
.\.venv\Scripts\python.exe -B benchmark_pptx\prepare_benchmark.py --refresh-review
# 全部门槛通过后执行一次冻结
.\.venv\Scripts\python.exe -B benchmark_pptx\prepare_benchmark.py --freeze
```

当前manifest SHA-256：`174635971bbf3f4e16406f6b971a79ae47f3148bd5da67be0779ecf67d2fa720`；source_plan原字节SHA-256：`160331b783d9af88067aa7002526c31905248731fa25222f32c5d53e3f1b81db`。未生成freeze_record前，这些摘要不构成人工标签冻结。

冻结后check拒绝清单、标签、source_plan或工件变化。不得因A/C/H/C9预测改标签、筛页或调整阈值；错误须另建有版本说明的修订记录。冻结门槛通过不等于独立文稿数或统计效力充分。

## 6. 十项直接回答

1. 使用3份用户原稿；当前来源文稿数：A_candidate=1、C=2，不同cohort不能冒充全新自然AI样本。
2. 原始页面84张。
3. 可控修改尝试24张；操作/渲染失败0个。
4. 未修改原页/可控页构成为84/24；它不等于自然缺陷/可控缺陷成立比例，真实标签见人工统计。
5. 四标签人工确认正例依次为[0, 0, 0, 0]，负例为[0, 0, 0, 0]（顺序同上表）。
6. 有unknown页面108张、unknown标签单元格432个；失败0个。
7. 实际原稿比例见来源表；16:9/4:3专用探针检查通过=True，不把探针计作独立自然来源。
8. 本地未发现旧审查文稿SHA/名称重合；来源不确定项按计划与清单保留，历史未接触结论受本地证据和用户声明范围限制。
9. frozen=false；只有验证通过的freeze_record才表示冻结。
10. ready_for_final_test=false；尚不具备4.3G-2评估条件：先补齐阻塞项并成功冻结。本轮未运行A/C/H/C9或查看新基准预测。

完成后停止，等待人工核验与验收，不自动执行下一阶段。

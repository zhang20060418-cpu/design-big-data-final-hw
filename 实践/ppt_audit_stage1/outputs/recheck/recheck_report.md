# 第三步 · 文字可见性复检报告

生成时间：2026-10-24T17:40:41+08:00　|　程序：`outputs/recheck/text_visibility_recheck.py`　|　渲染：PowerPoint 16.0，1920x1080 PNG via PowerPoint Slide.Export

> 本报告只回答“**实际可见文字**是否越出页面或被页面裁切”。第一阶段的外框越界结果以 `geometry_evidence` 原样保留，**不作为文字裁切的证据**。

## 一、结论摘要

| 组 | 条目 | 已确认裁切 | 未发现可见裁切 | 不确定 | 不适用 |
|---|---:|---:|---:|---:|---:|
| 第一阶段 4 条真实记录 | 4 | 0 | 2 | 0 | 2 |
| 合成对照样本 | 6 | 2 | 4 | 0 | 0 |

建议交给后续多模态复核：**2 条**。

## 二、复检覆盖范围（真实样本不是全页扫描）

第二轮的真实候选**只来自第一阶段 4 条越界记录**，没有遍历每页所有含文字对象；合成样本才是全页扫描。已探测页面的台账：

| 文件 | 页 | 含文字对象 | 已复检 | 未复检 | 本轮候选 | 候选中的空形状 |
|---|---:|---:|---:|---:|---:|---:|
| AddAgendaSlide_TestA.pptx | 3 | 3 | 1 | 2 | 1 | 0 |
| CalibriToTimesTestA.pptx | 14 | 3 | 1 | 2 | 1 | 0 |
| CenterAlignTitles_TestA.pptx | 4 | 10 | 0 | 10 | 1 | 1 |
| CenterAlignTitles_TestA.pptx | 5 | 3 | 0 | 3 | 1 | 1 |

**第 4 页的具体数字**：页内识别到 **10** 个含文字对象，本轮成功检查 **0** 个，跳过 **10** 个。唯一候选是 id=4 的空形状（无文字）：对象读取成功 1 个，但文字可见性检查为 0 个。因此 `not_applicable` 只说明该对象不适用，**不能推断整页没有文字裁切问题**。

合成样本：5 页、含文字对象 16 个，全部参与候选筛选，6 个触发条件并完成复检。

计数口径：真实页取第一阶段结构 JSON 的叶子文字对象（组容器不重复计数）；合成样本取 COM 全页扫描结果。

### 判定口径

每个对象走三层证据，**后一层只用于确认，不用于推翻前一层的事实**：

1. **COM 排版层**：PowerPoint `TextFrame2.TextRange` 给出每段文字的 `Bound*`，取**非空段落**的并集作为 `visible_bbox`；整段范围另存为 `layout_bbox` 作对照。这样既拿到真实排版范围，又不会把末尾空段落算成文字。
2. **像素层**：用 PowerPoint 导出 144 dpi 页面图，在 `visible_bbox` 内统计墨迹范围，并在页面边缘 3 px 带内统计墨迹占比；文字越界且页边有墨迹才判 `confirmed_clip`。
3. **局部放大层**：对 `confirmed_clip` 生成页边 2–4 倍放大图，人工可核。

状态含义：`confirmed_clip` = 排版越界且页边确有字迹被切断；`no_visible_clip` = **本次观察范围内**未发现可见文字裁切（不等于证明所有文字都完整）；`uncertain` = 有越界迹象但证据不足；`not_applicable` = 无文字或渲染不可见。

未使用 OCR：字符是否被切由“排版范围 + 页边墨迹 + 放大图”三重证据判断。OCR 缺字可能来自识别误差，本阶段不把它作为裁切证据。

## 三、第一次 vs 第二次（真实记录）

| # | 文件 / 页 / 对象 | 第一次（外框几何） | 第二次（可见文字） | 变化 |
|---|---|---|---|---|
| R1 | AddAgendaSlide_TestA.pptx p3 id=13 | S-OVERSHOOT（下41.6pt；几何级别 high） | **no_visible_clip**（缺陷级别 none） | 维持（非文字裁切） |
| R2 | CalibriToTimesTestA.pptx p14 id=4 | S-OVERSHOOT（右7.3pt、下30.9pt；几何级别 medium） | **no_visible_clip**（缺陷级别 none） | 维持（非文字裁切） |
| R3 | CenterAlignTitles_TestA.pptx p4 id=4 | S-OVERSHOOT（上5.7pt；几何级别 low） | **not_applicable**（缺陷级别 none） | 降级为不适用（渲染不可见） |
| R4 | CenterAlignTitles_TestA.pptx p5 id=4 | S-OVERSHOOT（上5.7pt；几何级别 low） | **not_applicable**（缺陷级别 none） | 降级为不适用（渲染不可见） |

## 四、逐条证据

### R1 · AddAgendaSlide_TestA.pptx · 第 3 页 · id=13 （Content Placeholder 2）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `none`
- **对象**：PLACEHOLDER；文字：“Some additive can cause any residue after it’s added to base fracturin…”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：外框 (73.7, 155.6)–(885.8, 581.6) pt；越界 下 41.6pt（0.578 in）（几何级别 high）
- **第二次文字范围**：可见文字 (80.9, 159.2)–(883.9, 370.8) pt；整段（含空段落）(80.9, 159.2)–(883.9, 370.8) pt
- **页面**：960×540 pt；文字越界 无
- **像素核对**：页面主背景亮度 254（占比 86%）；文字区内墨迹 10.8%；墨迹范围 (81.5, 175.0)–(878.0, 368.5) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 81.5 pt，无字迹贴近边界
- **是否建议多模态复核**：否
- **截图**：[标注图](evidence/R1_AddAgendaSlide_TestA_p3_id13_annotated.png) · [原始渲染](render/AddAgendaSlide_TestA/slide03.png)

### R2 · CalibriToTimesTestA.pptx · 第 14 页 · id=4 （TextBox 3）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `none`
- **对象**：TEXT_BOX；文字：“*Momper JD, Mulugeta Y, Green DJ, Karesh A, Krudys KM, Sachs HC, Yao L…”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：外框 (17.8, 495.7)–(967.3, 570.9) pt；越界 右 7.3pt（0.101 in）、下 30.9pt（0.429 in）（几何级别 medium）
- **第二次文字范围**：可见文字 (25.0, 499.3)–(945.9, 530.5) pt；整段（含空段落）(25.0, 499.3)–(945.9, 566.5) pt
- **页面**：960×540 pt；文字越界 无
- **空段落影响**：整段 Bound* 越界但可见段落未越界；差额来自末尾空段落（XML 末尾空段落 2 个）
- **像素核对**：页面主背景亮度 254（占比 83%）；文字区内墨迹 11.6%；墨迹范围 (25.0, 502.5)–(943.5, 530.0) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 10.0 pt，无字迹贴近边界
- **是否建议多模态复核**：否
- **截图**：[标注图](evidence/R2_CalibriToTimesTestA_p14_id4_annotated.png) · [原始渲染](render/CalibriToTimesTestA/slide14.png)

### R3 · CenterAlignTitles_TestA.pptx · 第 4 页 · id=4 （AutoShape 2）

- **状态**：`not_applicable`（不适用），置信度 high，缺陷级别 `none`
- **对象**：AUTO_SHAPE；文字：（空）
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **审查图边界框**：已隐藏 —— 空形状（无文字、无填充、无边框、无可见效果）；坐标与结构仍保留在 `geometry_evidence`
- **第一次几何证据**：外框 (128.4, -5.7)–(140.4, 6.3) pt；越界 上 5.7pt（0.079 in）（几何级别 low）
- **第二次文字范围**：可见文字 —；整段（含空段落）—
- **页面**：960×540 pt；文字越界 无
- **判定依据**：对象无文字，且无填充、无边框、无效果；渲染不可见
- **判定依据**：保留第一阶段几何记录，但不构成可见文字缺陷
- **是否建议多模态复核**：否
- **截图**：[标注图](evidence/R3_CenterAlignTitles_TestA_p4_id4_annotated.png) · [原始渲染](render/CenterAlignTitles_TestA/slide04.png)

### R4 · CenterAlignTitles_TestA.pptx · 第 5 页 · id=4 （AutoShape 2）

- **状态**：`not_applicable`（不适用），置信度 high，缺陷级别 `none`
- **对象**：AUTO_SHAPE；文字：（空）
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **审查图边界框**：已隐藏 —— 空形状（无文字、无填充、无边框、无可见效果）；坐标与结构仍保留在 `geometry_evidence`
- **第一次几何证据**：外框 (128.4, -5.7)–(140.4, 6.3) pt；越界 上 5.7pt（0.079 in）（几何级别 low）
- **第二次文字范围**：可见文字 —；整段（含空段落）—
- **页面**：960×540 pt；文字越界 无
- **判定依据**：对象无文字，且无填充、无边框、无效果；渲染不可见
- **判定依据**：保留第一阶段几何记录，但不构成可见文字缺陷
- **是否建议多模态复核**：否
- **截图**：[标注图](evidence/R4_CenterAlignTitles_TestA_p5_id4_annotated.png) · [原始渲染](render/CenterAlignTitles_TestA/slide05.png)

### S1 · recheck_samples.pptx · 第 1 页 · id=3 （S1_RightFrame_TextVisible）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `none`
- **对象**：TEXT_BOX；文字：“ALL VISIBLE”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (619.2, 115.2)–(1051.2, 201.6) pt，本次补算外框越界 右 91.2pt（1.267 in）
- **第二次文字范围**：可见文字 (626.4, 118.8)–(796.9, 152.4) pt；整段（含空段落）(626.4, 118.8)–(804.6, 152.4) pt
- **页面**：960×540 pt；文字越界 无
- **像素核对**：页面主背景亮度 254（占比 96%）；文字区内墨迹 26.5%；墨迹范围 (626.5, 125.5)–(795.5, 146.5) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 125.5 pt，无字迹贴近边界
- **是否建议多模态复核**：否
- **样本身份**：`S1_RightFrame_TextVisible`
- **截图**：[标注图](evidence/S1_recheck_samples_p1_id3_annotated.png) · [原始渲染](render/recheck_samples/slide01.png)

### S2 · recheck_samples.pptx · 第 1 页 · id=4 （S1_BottomFrame_TextVisible）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `none`
- **对象**：TEXT_BOX；文字：“BOTTOM FRAME OVERFLOW, TEXT INSIDE”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (64.8, 496.8)–(496.8, 640.8) pt，本次补算外框越界 下 100.8pt（1.400 in）
- **第二次文字范围**：可见文字 (72.0, 500.4)–(444.0, 522.0) pt；整段（含空段落）(72.0, 500.4)–(449.0, 522.0) pt
- **页面**：960×540 pt；文字越界 无
- **像素核对**：页面主背景亮度 254（占比 96%）；文字区内墨迹 21.4%；墨迹范围 (73.0, 504.5)–(442.0, 520.5) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 19.5 pt，无字迹贴近边界
- **是否建议多模态复核**：否
- **样本身份**：`S1_BottomFrame_TextVisible`
- **截图**：[标注图](evidence/S2_recheck_samples_p1_id4_annotated.png) · [原始渲染](render/recheck_samples/slide01.png)

### S3 · recheck_samples.pptx · 第 2 页 · id=3 （S2_RightClippedText）

- **状态**：`confirmed_clip`（已确认裁切），置信度 high，缺陷级别 `high`
- **对象**：TEXT_BOX；文字：“RIGHT EDGE CLIPPED TEXT”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (648.0, 216.0)–(936.0, 288.0) pt，本次补算外框越界 无
- **第二次文字范围**：可见文字 (655.2, 219.6)–(1201.3, 267.6) pt；整段（含空段落）(655.2, 219.6)–(1212.5, 267.6) pt
- **页面**：960×540 pt；文字越界 右 241.3pt（3.352 in）
- **框内压力**：文字超出文本框内部区域 右 272.5pt（3.785 in）（PowerPoint 不裁切框内文字，溢出仍可见）
- **像素核对**：页面主背景亮度 254（占比 97%）；文字区内墨迹 26.7%；墨迹范围 (658.0, 229.5)–(960.0, 259.0) pt
  - 右边 3px 带墨迹占比 9.3%（27/291 px）；墨迹距该边 0.0 pt
- **判定依据**：实际文字排版范围越出页面
- **判定依据**：右边墨迹占比 9.3%，字形在页面边界处被切断
- **判定依据**：文字排版范围超出文本框内部区域（PowerPoint 不裁切文本框内文字，溢出部分仍可见）
- **附带信号**：text_spills_outside_frame
- **是否建议多模态复核**：否
- **样本身份**：`S2_RightClippedText`
- **截图**：[标注图](evidence/S3_recheck_samples_p2_id3_annotated.png) · [页边放大](evidence/S3_recheck_samples_p2_id3_zoom.png) · [原始渲染](render/recheck_samples/slide02.png)

### S4 · recheck_samples.pptx · 第 3 页 · id=3 （S2_BottomClippedText）

- **状态**：`confirmed_clip`（已确认裁切），置信度 high，缺陷级别 `high`
- **对象**：TEXT_BOX；文字：“BOTTOM EDGE CLIPPED TEXT”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (86.4, 500.4)–(806.4, 572.4) pt，本次补算外框越界 下 32.4pt（0.450 in）
- **第二次文字范围**：可见文字 (93.6, 504.0)–(896.6, 568.8) pt；整段（含空段落）(93.6, 504.0)–(911.6, 568.8) pt
- **页面**：960×540 pt；文字越界 下 28.8pt（0.400 in）
- **框内压力**：文字超出文本框内部区域 右 97.4pt（1.353 in）（PowerPoint 不裁切框内文字，溢出仍可见）
- **像素核对**：页面主背景亮度 254（占比 96%）；文字区内墨迹 29.5%；墨迹范围 (97.5, 517.0)–(895.5, 540.0) pt
  - 下边 3px 带墨迹占比 44.4%（2139/4821 px）；墨迹距该边 0.0 pt
- **判定依据**：实际文字排版范围越出页面
- **判定依据**：下边墨迹占比 44.4%，字形在页面边界处被切断
- **判定依据**：文字排版范围超出文本框内部区域（PowerPoint 不裁切文本框内文字，溢出部分仍可见）
- **附带信号**：text_spills_outside_frame
- **是否建议多模态复核**：否
- **样本身份**：`S2_BottomClippedText`
- **截图**：[标注图](evidence/S4_recheck_samples_p3_id3_annotated.png) · [页边放大](evidence/S4_recheck_samples_p3_id3_zoom.png) · [原始渲染](render/recheck_samples/slide03.png)

### S5 · recheck_samples.pptx · 第 4 页 · id=3 （S3_TightBox_NoAutofit）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `low`
- **对象**：TEXT_BOX；文字：“This sentence is far too long for a 1.6 by 0.5 inch box.”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (72.0, 129.6)–(187.2, 165.6) pt，本次补算外框越界 无
- **第二次文字范围**：可见文字 (79.2, 133.2)–(175.2, 262.8) pt；整段（含空段落）(79.2, 133.2)–(175.2, 262.8) pt
- **页面**：960×540 pt；文字越界 无
- **框内压力**：文字超出文本框内部区域 下 100.8pt（1.400 in）（PowerPoint 不裁切框内文字，溢出仍可见）
- **像素核对**：页面主背景亮度 254（占比 96%）；文字区内墨迹 13.4%；墨迹范围 (79.0, 137.5)–(169.0, 259.0) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 79.0 pt，无字迹贴近边界
- **判定依据**：文字排版范围超出文本框内部区域（PowerPoint 不裁切文本框内文字，溢出部分仍可见）
- **附带信号**：text_spills_outside_frame
- **是否建议多模态复核**：是 —— 文字溢出文本框后与相邻内容重叠，是否影响可读性？
- **样本身份**：`S3_TightBox_NoAutofit`
- **截图**：[标注图](evidence/S5_recheck_samples_p4_id3_annotated.png) · [原始渲染](render/recheck_samples/slide04.png)

### S6 · recheck_samples.pptx · 第 5 页 · id=3 （S3_TightBox_ShrinkText）

- **状态**：`no_visible_clip`（未发现可见裁切），置信度 high，缺陷级别 `low`
- **对象**：TEXT_BOX；文字：“This sentence is far too long for a 1.6 by 0.5 inch box.”
- **外观属性**：填充 visible=0、边框 visible=0、形状 visible=True、XML 效果=False
- **第一次几何证据**：未进入第一阶段结果（外框在页内或本页未参与检测）；外框 (72.0, 129.6)–(187.2, 165.6) pt，本次补算外框越界 无
- **第二次文字范围**：可见文字 (79.2, 133.2)–(175.2, 262.8) pt；整段（含空段落）(79.2, 133.2)–(175.2, 262.8) pt
- **页面**：960×540 pt；文字越界 无
- **框内压力**：文字超出文本框内部区域 下 100.8pt（1.400 in）（PowerPoint 不裁切框内文字，溢出仍可见）
- **像素核对**：页面主背景亮度 254（占比 97%）；文字区内墨迹 13.4%；墨迹范围 (79.0, 137.5)–(169.0, 259.0) pt
- **判定依据**：实际文字排版范围完整落在页面内，按页面裁切定义不成立
- **判定依据**：渲染图墨迹距最近页面边缘 79.0 pt，无字迹贴近边界
- **判定依据**：文字排版范围超出文本框内部区域（PowerPoint 不裁切文本框内文字，溢出部分仍可见）
- **判定依据**：自动缩放（溢出时缩小文字）已开启，但实际字号仍为 18 pt，与显式字号一致；PowerPoint 未重算 fontScale，本次渲染没有缩小
- **附带信号**：text_spills_outside_frame, autofit_shrink_inactive
- **是否建议多模态复核**：是 —— 文字溢出文本框后与相邻内容重叠，是否影响可读性？
- **样本身份**：`S3_TightBox_ShrinkText`
- **截图**：[标注图](evidence/S6_recheck_samples_p5_id3_annotated.png) · [原始渲染](render/recheck_samples/slide05.png)

## 五、合成样本的实测结论

| 样本 | 设计意图 | 预期标签 | 程序判定 | 验收 | 渲染实测 |
|---|---|---|---|---|---|
| S1_RightFrame_TextVisible | 外框越界、文字完整 | `no_visible_clip` | `no_visible_clip` | ✅ | 字符完整可见 |
| S1_BottomFrame_TextVisible | 外框越界、文字完整 | `no_visible_clip` | `no_visible_clip` | ✅ | 字符完整可见 |
| S2_RightClippedText | 文字伸出页面右边缘 | `confirmed_clip` | `confirmed_clip` | ✅ | 字符在页边被切断（见放大图） |
| S2_BottomClippedText | 文字伸出页面下边缘 | `confirmed_clip` | `confirmed_clip` | ✅ | 字符在页边被切断（见放大图） |
| S3_TightBox_NoAutofit | 小框+长文，观察是否裁切 | `no_visible_clip` | `no_visible_clip` | ✅ | 字符完整可见 |
| S3_TightBox_ShrinkText | 小框+长文+自动缩放 | `no_visible_clip` | `no_visible_clip` | ✅ | 字符完整可见 |

验收比对：**6/6 通过**（期望标签见 `test_samples/recheck/expected.json`，只用于比对，不参与判定）。

样本 3 的关键实测：**PowerPoint 不会把溢出文本框的文字裁掉**，而是继续画在框外。因此“文本框内部文字裁切”在普通文本框上不可复现，本阶段不能提供该类型的正例，相应检测标记为未解决（见第八节）。

## 六、本次能回答与不能回答的问题

| 验收项 | 结果 | 证据 |
|---|---|---|
| 原来两条“外框越界但文字正常”的案例不再被判为已确认裁切 | 通过 | R1 `no_visible_clip`、R2 `no_visible_clip` |
| 两条不可见空形状不被报告为可见文字缺陷 | 通过 | R3/R4 `not_applicable` |
| 真实存在文字被页面裁切的样本能被发现 | 通过 | 2/2：S2_RightClippedText、S2_BottomClippedText |
| 文本框内部裁切能否可靠检测 | 未解决 | 实测 PowerPoint 不裁切溢出文本，无正例 |
| 第一阶段结果是否保留、可追溯 | 通过 | `geometry_evidence` 原样保留；源文件 SHA-256 与第一阶段一致 |

## 七、本地已能处理 / 仍需多模态判断

**本地可处理（确定性证据）**

1. 对象外框与页面边界的关系（第一阶段）。
2. 实际文字排版范围是否越出页面（COM `TextRange`，按非空段落取并集）。
3. 文字范围是否溢出文本框内部区域（排版压力，不等于裁切）。
4. 空形状过滤：无填充、无边框、无文字的对象不再进入视觉告警。
5. 页边墨迹核对：文字越界且页边确有墨迹 → 确认裁切。

**仍需后续多模态智能体判断**

1. 文字越界但被上层对象遮挡、或文字与背景同色导致无墨迹时的真实可读性。
2. 文字溢出文本框后与相邻元素重叠是否构成可读性问题（本阶段只报几何信号）。
3. 艺术字、3D 变换、图片内文字（本阶段 COM/像素均无法取得排版）。
4. 字体替换导致的重新排版（本机字体与原稿作者环境不同）。

## 八、技术限制

1. **COM `Bound*` 不是“可见文字”边界**：它包含末尾空段落。Calibri p14 的整段范围到 566.5 pt（越界 26.5 pt），但越界部分是两个空段落，可见文字止于 530.5 pt。本模块因此改用非空段落并集，并保留整段范围作对照。
2. **文本框内部裁切不可复现**：实测（样本 3）PowerPoint 对溢出文字继续绘制在框外，不裁切到框内；只有页面边界会真正切掉字形。
3. **墨迹统计按矩形区域**：区域内其它对象（页码、分隔线、背景图）会被一并计入；背景复杂（主背景占比 < 55%）时判定降级为 `uncertain`。
4. **只覆盖候选对象，不是全页扫描**：真实样本只复检第一阶段的 4 条记录；这些页共 19 个含文字对象，实际复检 2 个，跳过 17 个。其余 58 页完全未做文字范围扫描。
5. **字体与版本相关**：排版范围由本机 PowerPoint + 本地字体计算，与原稿作者机器可能存在差异。
6. **旋转/组合内文字**未纳入本次复检（4 条记录旋转均为 0°，且均为顶层对象）。

## 九、人工分类（请填写，未预填）

| # | 文件 / 页 / 对象 | 复检状态 | 真实缺陷 | 合理设计 | 不确定 | 备注 |
|---|---|---|---:|---:|---:|---|
| R1 | AddAgendaSlide_TestA.pptx p3 id=13 | `no_visible_clip` | ☐ | ☐ | ☐ | |
| R2 | CalibriToTimesTestA.pptx p14 id=4 | `no_visible_clip` | ☐ | ☐ | ☐ | |
| R3 | CenterAlignTitles_TestA.pptx p4 id=4 | `not_applicable` | ☐ | ☐ | ☐ | |
| R4 | CenterAlignTitles_TestA.pptx p5 id=4 | `not_applicable` | ☐ | ☐ | ☐ | |

## 十、复现

```bash
# 依赖：python-pptx、Pillow、pywin32、本机 Microsoft PowerPoint
pip install python-pptx pillow pywin32

# 1) 重建对照样本（可选；仓库内已含成品）
python test_samples/recheck/make_recheck_samples.py

# 2) 运行复检（--refresh 忽略 COM 探针缓存）
python outputs/recheck/text_visibility_recheck.py --refresh

# 3) 运行验收断言
python outputs/recheck/verify_recheck.py
```

产物：`text_recheck.json`（结构化结果）、`recheck_report.md`（本文件）、`evidence/`（标注图与页边放大图）、`render/`（原始页面渲染）、`verification.json`（验收断言）。

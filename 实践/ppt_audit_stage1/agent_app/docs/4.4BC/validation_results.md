# 4.4B–4.4C 实际验收结果

验收日期：2026-10-30～2026-10-31。结论：六类检测到按需复核、用户追问和报告导出的最小闭环已完成；真实检测和模拟 LLM 明确分开。

## 自动化测试

| 检查 | 实际结果 | 记录 |
|---|---|---|
| 修改前基线 | 后端 10 项、前端 typecheck/build 通过 | 安全检查点和集成检查记录 |
| 后端完整 pytest | **118 passed，0 failed，0 skipped** | `acceptance/backend-tests.xml`、`backend-tests.log` |
| 报告最终布局后的定向回归 | **24 passed** | `acceptance/report-final-check.log` |
| TypeScript | 通过 | `acceptance/frontend-checks.log` |
| React 生产构建 | 通过 | 同上 |
| 前端契约测试 | **8 passed** | 同上 |
| Playwright 完整流程 | **3 passed，0 skipped，0 flaky** | `acceptance/playwright-results.json` |
| 最新服务只读 UI 复查 | 通过，无页面错误、无写操作 | `acceptance/final-ui-smoke.json`、三张 final 截图 |
| 受保护研究文件 SHA | **2,168 个一致，0 改变、0 缺失** | `acceptance/research_preservation.json` |

后端测试构成：旧流水线 10；实时六类适配 21；规则/调度/响应校验 29；合并与 Schema 30；交互复核 18；报告 10。旧演示接口与旧四类视觉插件继续通过测试。首次浏览器检查发现 CSS 百分比序列化引起的过严精度断言，保留首轮记录，改用小于 0.01 像素的容差后通过。

10 月 31 日补测曾因工作站代理转发本地模拟 API 而出现 502，记录保留为 `backend-proxy-failure.log/xml`。测试进程现明确将 loopback 加入 NO_PROXY，避免把本地 fixture 请求送给代理；未修改用户系统代理或应用外部服务配置。

本轮还通过回归测试发现并修复实际问题：旧演示任务缺少 review_focus 导致报告失败；六类独有 Finding 的来源被误显示为视觉模型；`.html.j2` 后缀未触发自动转义导致用户文本能进入 HTML；交互追问累计真实调用仅受全局上限约束。后者已修为任务与全局调用上限两者较小值，Mock 单独标记且不花真实 HTTP 预算。修复没有改变任何研究检测阈值。

## 真实上传与演示证据

原生 14 页功能文稿：`fixtures/integration_14_slides.pptx`。

真实任务 `8bf29916e0254aa6` 使用本机 PowerPoint COM、原 PNG 与真实排版数据，源文件 SHA：

`c4427509a9b061bb60a07b99134c7e9669791ff6b76cdbef84f2f7eeb89379a6`

| 输出 | 实际数量 |
|---|---:|
| 原稿页面 / 成功渲染页面 | 14 / 14 |
| 原始 Claim | 84 |
| candidate | 9 |
| no_candidate_in_available_scope | 63 |
| unknown | 11 |
| unsupported | 1 |
| 合并后的 Finding | 10 |
| 有 Finding 的页面 | 8 |
| 原稿哈希改变 | 0 |
| 外部真实 LLM 调用 | 0 |

六类 Claim 数量不等于六类阳性数量。第 11 页留白范围保持 unknown，仍通过明确选择的补充复核进入 D5 审查，未为了演示而把它强改为候选。第 12–14 页真实布局特征形成关联页；这只说明需要比较模板与内容，不能证明正常统一模板有缺陷。第 6 页有可定位遮挡，第 5 页有越界候选。

初次按演示配置指定的第 6、11、12 页形成 3 次 Mock 调度，跨页图实际覆盖 5 个唯一页面。D2/D5/D6/A01 规则实际载入；A05 原生 ROI 专项用独立适配器形状测试验证。用户追问 D5 的原 Claim、初始报告和历次结果均保留。后续导出和浏览器验收产生的 Mock 追问追加在历史，不覆写原记录。

最终导出 `acceptance/demo_report.html`、`demo_report.json`、`demo_report.pdf`。PDF 由同一 HTML 用本机 Edge 打印，A4，已检查文件签名、文本、首页 Mock 标识、六类信息及若干页面渲染；原始输入 PNG 没有检测框。摘要与最终 SHA 在 `acceptance/integration_run.json` 和 `artifact_manifest.json`。

## 环境与复现

本机实际环境：

- 应用 Python **3.13.14**；FastAPI 0.162.0、Pydantic 2.14.1、python-pptx 1.0.2、Pillow 13.0.0、pywin32 312、httpx 0.28.1、pytest 9.2.0。
- 既有 ML Python **3.12.15**；numpy 2.5.4、scipy 1.18.1。只借用其推理运行时，没有安装/升级 ML 依赖、加载 CLIP 或训练分类器。
- 前端复用 React/TS/Vite；新增 Playwright 开发测试依赖，使用已安装的 Edge，没有另下整套浏览器。

日常仍使用 `agent_app/scripts/start_app.ps1`。验收服务独立运行于 `http://127.0.0.1:8011/`，数据目录为 `docs/4.4BC/acceptance/runtime/`，不覆盖日常任务数据库；没有使用原人工标注工作台的 8766 端口。

在 `agent_app/backend` 执行后端测试：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -B -m pytest -q
```

在 `agent_app/frontend` 执行前端检查：

```powershell
npm run typecheck
npm run test:review-contract
npm run build
$env:E2E_BASE_URL='http://127.0.0.1:8011'
$env:E2E_TASK_ID='8bf29916e0254aa6'
$env:E2E_PPTX_PATH=(Resolve-Path ..\fixtures\integration_14_slides.pptx).Path
npm run test:e2e
```

独立验收服务运行后，在 backend 运行 `scripts/run_integration_acceptance.py` 会真实上传并执行新的功能任务；`--task-id 8bf29916e0254aa6` 重用已检测任务，追加一次显式 Mock 追问并重新导出，不重跑检测。它不是独立模型性能评分脚本。

## 已完成与尚存边界

已完成真实六类接入、五态、不可变原始证据、范围冲突、跨页关系、12 份版本化规则、PromptBuilder、严格响应校验、full/selective 调度、预算和日志、候选级交互、原风格前端和三种报告导出。

本轮没有真实外部 LLM 视觉质量成绩；Mock 全部 uncertain。D6-A05 语义检测仍 unsupported。缺少 PowerPoint、ML 运行时或有效测量时相应范围明确为未知/不支持。页面未报警或未选中不代表正常，预算内审查不保证零漏检。没有自动编辑 PPT，也没有把候选或模型复核写成人工真值。

下一轮可在当前接口上开展固定样例的真实服务验证和 full/selective 成本覆盖实验；本轮到此停止，不启动新训练或改动研究模型。

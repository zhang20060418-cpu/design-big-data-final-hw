import { test, expect } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import type { Issue } from '../src/lib/types'

const completedTask = process.env.E2E_TASK_ID
const pptx = process.env.E2E_PPTX_PATH
const labels = ['字号不合理', '内容遮挡', '布局拥挤', '溢出或裁切', '过分留白', '异常设计风险']

test('已完成真实检测任务：六类、未决范围、定位、跨页、Mock追问和报告导出', async ({ page, request }, testInfo) => {
  test.skip(!completedTask, 'Set E2E_TASK_ID to an actual completed isolated acceptance task.')
  const id = completedTask!
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const claimsResponse = await request.get(`/api/tasks/${id}/claims`)
  expect(claimsResponse.ok()).toBeTruthy()
  const { claims } = await claimsResponse.json()
  expect(claims.length).toBeGreaterThanOrEqual(6)
  expect(claims.some((claim: { raw_status: string }) => ['unknown', 'unsupported', 'not_checked'].includes(claim.raw_status))).toBeTruthy()
  const issueResponse = await request.get(`/api/tasks/${id}/issues`)
  const { items: issues } = await issueResponse.json() as { items: Issue[] }
  expect(issues.length).toBeGreaterThan(0)
  await page.goto(`/tasks/${id}/results`)
  await expect(page.getByRole('region', { name: '六类检查范围' })).toBeVisible()
  for (const label of labels) {
    const filter = page.getByRole('button', { name: new RegExp(`^${label} [0-9]+$`) })
    await expect(filter).toBeVisible()
    await filter.click()
    await expect(filter).toHaveAttribute('aria-pressed', 'true')
    await filter.click()
  }
  await expect(page.getByText('异常设计风险不代表“AI生成率”；素材伪文字等语义伪影仍需进一步视觉核实。')).toBeVisible()
  const localized = issues.find((issue) => issue.normalized_bbox && !Array.isArray(issue.normalized_bbox) && issue.normalized_bbox.x >= 0 && issue.normalized_bbox.y >= 0 && issue.normalized_bbox.x + issue.normalized_bbox.w <= 1 && issue.normalized_bbox.y + issue.normalized_bbox.h <= 1 && issue.review_status !== 'rejected')
  expect(localized, 'Acceptance deck must produce a measured localized candidate.').toBeTruthy()
  await page.goto(`/tasks/${id}/results?page=${localized!.slide_index}`)
  await page.locator('.issue-card').filter({ has: page.locator('.t', { hasText: localized!.title }) }).first().locator('.t').click()
  const marker = page.locator('.marker.sel')
  await expect(marker).toBeVisible()
  const rendered = await marker.evaluate((element) => ({ left: parseFloat((element as HTMLElement).style.left), top: parseFloat((element as HTMLElement).style.top), width: parseFloat((element as HTMLElement).style.width), height: parseFloat((element as HTMLElement).style.height) }))
  const normalized = localized!.normalized_bbox as { x: number; y: number; w: number; h: number }
  // Chromium serializes percentage CSS to roughly six significant digits.
  // A 0.0005 percentage-point tolerance is less than 0.01 px at 1920 px.
  expect(rendered.left).toBeCloseTo(normalized.x * 100, 3)
  expect(rendered.top).toBeCloseTo(normalized.y * 100, 3)
  expect(rendered.width).toBeCloseTo(normalized.w * 100, 3)
  expect(rendered.height).toBeCloseTo(normalized.h * 100, 3)
  const related = issues.find((issue) => (issue.related_slides?.length ?? 0) > 1)
  expect(related, 'Acceptance deck must produce a real cross-page candidate.').toBeTruthy()
  await page.goto(`/tasks/${id}/results?page=${related!.slide_index}`)
  const relatedCard = page.locator('.issue-card').filter({ has: page.locator('.t', { hasText: related!.title }) }).first()
  await relatedCard.locator('.t').click()
  const target = related!.related_slides!.map(Number).find((index) => index !== related!.slide_index)!
  await relatedCard.getByRole('button', { name: `P${String(target).padStart(2, '0')}`, exact: true }).click()
  await expect(page.locator('.viewer-head .title')).toHaveText(`第 ${target} 页`)
  const issue = issues.find((item) => item.defect_type === 'excessive_whitespace') ?? issues[0]
  await page.goto(`/tasks/${id}/results?page=${issue.slide_index}`)
  const card = page.locator('.issue-card').filter({ has: page.locator('.t', { hasText: issue.title }) }).first()
  await card.locator('.t').click()
  await expect(page.getByRole('region', { name: '问题追问与复核' })).toBeVisible()
  await page.getByLabel('你的问题或补充说明').fill('这里的留白是有意的非对称构图，请根据实际证据复核，证据不足时保留不确定结论。')
  await page.getByLabel('本次复核方式').selectOption('mock')
  const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().includes(`/issues/${encodeURIComponent(issue.issue_id)}/review`))
  await page.getByRole('button', { name: '提交问题并复核' }).click()
  const reviewResponse = await responsePromise
  expect(reviewResponse.ok(), await reviewResponse.text()).toBeTruthy()
  const reviewData = await reviewResponse.json()
  expect(reviewData.history.length).toBeGreaterThan(0)
  expect(reviewData.review.mock || reviewData.review.backend === 'mock' || reviewData.review.demo).toBeTruthy()
  await expect(page.getByText('Mock 模拟 · 非真实模型判断').first()).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('six-results-and-followup.png'), fullPage: true })
  await page.goto(`/tasks/${id}/report`)
  await expect(page.getByText('六类缺陷分布', { exact: true })).toBeVisible()
  await expect(page.getByText('智能体复核调度', { exact: true })).toBeVisible()
  await expect(page.getByText('六类检测范围与未决项', { exact: true })).toBeVisible()
  for (const kind of ['html', 'json', 'pdf']) {
    const response = await request.get(`/api/tasks/${id}/report?format=${kind}&download=true`, { timeout: 60_000 })
    expect(response.ok(), `${kind} export: ${response.status()}`).toBeTruthy()
    if (kind === 'pdf') expect((await response.body()).subarray(0, 5).toString()).toBe('%PDF-')
  }
  await page.screenshot({ path: testInfo.outputPath('six-report.png'), fullPage: true })
  expect(errors).toEqual([])
})

test('真实上传14页：准备、中文配置、六类检测与显式Mock复核闭环', async ({ page, request }, testInfo) => {
  test.skip(!pptx || !existsSync(pptx), 'Set E2E_PPTX_PATH to the generated 10–15 slide acceptance deck.')
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await page.locator('input[type=file]').setInputFiles(pptx!)
  await page.waitForURL(/\/tasks\/[^/]+\/configure/, { timeout: 60_000 })
  await expect(page.getByRole('button', { name: '开始审查' })).toBeEnabled({ timeout: 180_000 })
  const id = page.url().match(/\/tasks\/([^/]+)\/configure/)![1]
  await page.getByLabel('使用场景').fill('投影汇报与课堂演示')
  await page.getByLabel('期望风格').fill('保持统一模板，允许合理极简和非对称设计')
  await expect(page.getByRole('switch', { name: '六类专用检测' })).toHaveAttribute('aria-checked', 'true')
  await page.getByLabel('复核方式', { exact: true }).selectOption('mock')
  await page.getByRole('switch', { name: '多模态复核' }).click()
  await page.getByLabel('候选调度策略').selectOption('selective_review')
  await page.getByLabel('API 调用预算（次）').fill('3')
  await page.getByRole('button', { name: '开始审查' }).click()
  await page.waitForURL(/\/progress/)
  await expect(page.getByRole('heading', { name: '审查完成', exact: true })).toBeVisible({ timeout: 240_000 })
  const taskResponse = await request.get(`/api/tasks/${id}`)
  const task = await taskResponse.json()
  expect(task.document.slide_count).toBeGreaterThanOrEqual(10)
  expect(task.document.slide_count).toBeLessThanOrEqual(15)
  expect(task.options.enable_six_detectors).toBeTruthy()
  expect(task.options.llm_backend).toBe('mock')
  const response = await request.get(`/api/tasks/${id}/claims`)
  const { claims } = await response.json()
  for (const key of ['improper_font_sizing', 'occluded_content', 'cluttered_layout', 'content_overflow_cutoff', 'excessive_whitespace', 'generative_design_anomaly']) expect(claims.some((claim: { defect_type: string }) => claim.defect_type === key)).toBeTruthy()
  await page.getByRole('button', { name: '查看逐页结果' }).click()
  await expect(page.getByRole('region', { name: '六类检查范围' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('uploaded-real-deck-results.png'), fullPage: true })
  await testInfo.attach('created-task', { body: JSON.stringify({ task_id: id, source: pptx, source_bytes: readFileSync(pptx!).length, status: task.status, slides: task.document.slide_count, options: task.options }, null, 2), contentType: 'application/json' })
  expect(errors).toEqual([])
})

test('旧任务或缺失新端点时保留未检查状态，结果页不崩溃', async ({ page }) => {
  test.skip(!completedTask, 'Set E2E_TASK_ID to an actual isolated task.')
  await page.route('**/api/tasks/*/claims?*', (route) => route.fulfill({ status: 404, json: { error: { message: 'No six-class record' } } }))
  await page.route('**/api/tasks/*/review-plan?*', (route) => route.fulfill({ status: 404, json: { error: { message: 'No review plan' } } }))
  await page.goto(`/tasks/${completedTask}/results`)
  await expect(page.getByRole('region', { name: '六类检查范围' })).toBeVisible()
  await expect(page.getByRole('button', { name: /^异常设计风险 \d+$/ })).toBeVisible()
  await expect(page.locator('.viewer-head .title')).toBeVisible()
})

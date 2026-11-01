import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const baseURL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8011'
const taskId = process.env.E2E_TASK_ID ?? '8bf29916e0254aa6'
const output = resolve('../docs/4.4BC/acceptance')
mkdirSync(output, { recursive: true })
const result = { task_id: taskId, base_url: baseURL, checked_at: new Date().toISOString(), checks: [], page_errors: [], mutation_requests: [], screenshots: [] }
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EDGE_PATH ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
try {
  const context = await browser.newContext({ baseURL, viewport: { width: 1600, height: 1000 } })
  const page = await context.newPage()
  page.on('pageerror', (error) => result.page_errors.push(error.message))
  page.on('request', (request) => { if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) result.mutation_requests.push({ method: request.method(), url: request.url() }) })
  const response = await context.request.get(`/api/tasks/${taskId}/issues`)
  assert.equal(response.status(), 200)
  const { items: issues } = await response.json()
  const d5 = issues.find((issue) => issue.defect_type === 'excessive_whitespace') ?? issues[0]
  await page.goto(`/tasks/${taskId}/results?page=${d5.slide_index}`)
  await page.getByRole('region', { name: '六类检查范围' }).waitFor({ state: 'visible' })
  for (const label of ['字号不合理', '内容遮挡', '布局拥挤', '溢出或裁切', '过分留白', '异常设计风险']) {
    assert.equal(await page.getByRole('button', { name: new RegExp(`^${label} [0-9]+$`) }).count(), 1)
  }
  result.checks.push('Six filters and per-page six-category scope are visible.')
  const disclosure = await page.locator('.scope-panel details').evaluateAll((nodes) => nodes.map((node) => node.open))
  assert.ok(disclosure.length > 0)
  assert.ok(disclosure.every((open) => open === false))
  result.checks.push(`All ${disclosure.length} scope limitation disclosures start collapsed; status labels remain visible.`)
  await page.getByText('Mock 模拟复核', { exact: true }).waitFor({ state: 'visible' })
  result.checks.push('Mock review is explicitly labeled in the result module strip.')
  await page.screenshot({ path: resolve(output, 'final-results.png'), fullPage: true })
  result.screenshots.push('final-results.png')

  const related = issues.find((issue) => (issue.related_slides?.length ?? 0) > 1)
  assert.ok(related)
  await page.goto(`/tasks/${taskId}/results?page=${related.slide_index}`)
  const card = page.locator('.issue-card').filter({ has: page.locator('.t', { hasText: related.title }) }).first()
  await card.locator('.t').click()
  const target = related.related_slides.map(Number).find((index) => index !== related.slide_index)
  await card.getByRole('button', { name: `P${String(target).padStart(2, '0')}`, exact: true }).click()
  await page.waitForFunction((expected) => document.querySelector('.viewer-head .title')?.textContent === `第 ${expected} 页`, target)
  result.checks.push(`Real cross-page issue navigation works: ${related.slide_index} -> ${target}.`)

  await page.goto(`/tasks/${taskId}/results?page=${d5.slide_index}`)
  const d5card = page.locator('.issue-card').filter({ has: page.locator('.t', { hasText: d5.title }) }).first()
  await d5card.locator('.t').click()
  await page.getByRole('region', { name: '问题追问与复核' }).scrollIntoViewIfNeeded()
  assert.ok(await page.getByText('Mock 模拟 · 非真实模型判断', { exact: true }).count() > 0)
  await page.getByRole('region', { name: '问题追问与复核' }).evaluate((element) => {
    const panel = element.closest('.detail-panel')
    const header = panel?.querySelector('.detail-head')
    if (panel) panel.scrollTop += element.getBoundingClientRect().top - panel.getBoundingClientRect().top - (header?.getBoundingClientRect().height ?? 0) - 12
  })
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))))
  await page.screenshot({ path: resolve(output, 'final-review-history.png'), fullPage: false })
  result.screenshots.push('final-review-history.png')
  result.checks.push('Existing mock review history remains readable; no follow-up was submitted.')

  await page.goto(`/tasks/${taskId}/report`)
  await page.getByText('六类缺陷分布', { exact: true }).waitFor({ state: 'visible' })
  await page.getByText('六类检测范围与未决项', { exact: true }).waitFor({ state: 'visible' })
  await page.getByText('智能体复核调度', { exact: true }).waitFor({ state: 'visible' })
  assert.ok(await page.getByText('Mock 模拟复核报告', { exact: false }).count() > 0)
  for (const format of ['html', 'json', 'pdf']) assert.ok(await page.locator(`a[href*="format=${format}"]`).count() > 0)
  await page.screenshot({ path: resolve(output, 'final-report.png'), fullPage: true })
  result.screenshots.push('final-report.png')
  result.checks.push('Regenerated report displays six-category distribution, scope, planner, explicit Mock banner, and HTML/JSON/PDF links.')
  assert.deepEqual(result.page_errors, [])
  assert.deepEqual(result.mutation_requests, [])
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'
  result.error = error.stack ?? String(error)
  process.exitCode = 1
} finally {
  await browser.close()
  writeFileSync(resolve(output, 'final-ui-smoke.json'), JSON.stringify(result, null, 2) + '\n')
  console.log(JSON.stringify(result, null, 2))
}

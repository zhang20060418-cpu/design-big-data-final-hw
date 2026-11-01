// Real isolated PPTX + R2. Explicit Mock, early skip before the LLM stage.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const out = fileURLToPath(new URL('../../docs/4.4BC/llm_gate_fix_checks/', import.meta.url))
const fixture = fileURLToPath(new URL('../../fixtures/integration_14_slides.pptx', import.meta.url))
const base = 'http://127.0.0.1:8011'
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
const page = await context.newPage(), errors = [], pageMutations = [], findings = []
let taskId = null
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => {
  if (request.url().includes('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) pageMutations.push({ method: request.method(), url: request.url() })
})
const get = async url => {
  const result = await context.request.get(url); assert.equal(result.ok(), true, `${result.status()} ${url}`); return result.json()
}
try {
  // Existing real ready task: verify projection + local manual choice only.
  const oldId = 'e2a1c3e5887c467f', daily = 'http://127.0.0.1:8000'
  const before = await get(`${daily}/api/tasks/${oldId}`)
  await page.goto(`${daily}/tasks/${oldId}/configure`)
  const toggle = page.getByRole('switch', { name: '多模态复核', exact: true })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await toggle.click(); await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.locator('#review-purpose').fill('只读界面检查，不开始任务')
  await page.waitForTimeout(1800)
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  const after = await get(`${daily}/api/tasks/${oldId}`)
  assert.deepEqual(after.options, before.options); assert.equal(after.status, before.status)
  await page.screenshot({ path: path.join(out, 'browser-live-default-optout.png'), fullPage: true })
  findings.push('Existing legacy ready task opens with LLM checked under configured API. Local opt-out stays off across rerender; no task options or user files were saved.')

  const options = { enable_mllm: true, mllm_user_choice: true, llm_backend: 'mock',
    review_strategy: 'full_review', review_mode: 'manual', manual_pages: [4],
    max_api_calls: 1, max_review_pages: 14, enable_visual: false }
  const upload = await context.request.post(`${base}/api/tasks`, { multipart: {
    file: { name: 'integration_14_slides.pptx', mimeType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', buffer: readFileSync(fixture) },
    name: 'QA 真实首轮前提前跳过 Mock', options: JSON.stringify(options), prepare_only: 'false' } })
  assert.equal(upload.status(), 201)
  taskId = (await upload.json()).id
  writeFileSync(path.join(out, 'browser-live-early-skip-task.json'), JSON.stringify({ task_id: taskId, options, uploaded_at: new Date().toISOString() }, null, 2))
  await page.goto(`${base}/tasks/${taskId}/progress`)
  const phase = page.locator('.stage-item').filter({ has: page.getByText('多模态按需复核', { exact: true }) })
  const skip = phase.getByRole('button', { name: '跳过复核', exact: true })
  await expect(skip).toBeEnabled({ timeout: 30000 })
  const pending = await get(`${base}/api/tasks/${taskId}`)
  assert.equal(pending.review_gate.state, 'not_ready')
  assert.equal(pending.review_gate.can_skip, true)
  assert.equal(pending.review_gate.skip_available_before_stage, true)
  assert.equal(pending.report_ready, false)
  assert.equal(pending.stages.find(stage => stage.key === 'mllm').status, 'pending')
  await page.screenshot({ path: path.join(out, 'browser-live-early-skip-button.png'), fullPage: true })
  const skipResponse = page.waitForResponse(response => response.request().method() === 'POST' && response.url().includes(`/api/tasks/${taskId}/skip-review`))
  await skip.click()
  const response = await skipResponse
  assert.equal(response.ok(), true)
  const acknowledged = await response.json()
  assert.equal(acknowledged.state, 'skipped')
  assert.equal(acknowledged.skipped_before_review, true)
  assert.equal(acknowledged.skip_reason, 'user_skipped_before_review')
  const continuing = await get(`${base}/api/tasks/${taskId}`)
  assert.ok(['queued', 'running'].includes(continuing.status))
  assert.equal(continuing.report_ready, false)
  assert.ok(continuing.stages.some(stage => ['validate', 'parse', 'render', 'structure', 'visual'].includes(stage.key) && ['pending', 'running'].includes(stage.status)))
  assert.match(page.url(), /\/progress$/)
  findings.push('Real task is skipped while gate is not_ready and LLM stage pending. Skip is persisted before the five-second phase, without interrupting local screening or producing an early report.')

  await page.waitForURL(`**/tasks/${taskId}/results`, { timeout: 420000 })
  await expect(page.getByText(/跳过.*(误诊|漏检)/).first()).toBeVisible()
  const completed = await get(`${base}/api/tasks/${taskId}`)
  const report = await get(`${base}/api/tasks/${taskId}/report?format=json`)
  assert.equal(completed.status, 'completed', completed.error)
  assert.equal(completed.review_gate.state, 'skipped')
  assert.equal(completed.review_gate.skipped_before_review, true)
  assert.equal(report.verification_pipeline.final_review_status, 'user_skipped')
  assert.equal(report.verification_pipeline.llm_actual_http_calls, 0)
  assert.deepEqual(report.reviews, [])
  assert.equal(report.claims.length, 84)
  assert.equal(completed.document.slide_count, 14)
  await page.waitForTimeout(400)
  await page.screenshot({ path: path.join(out, 'browser-live-early-skip-result.png'), fullPage: true })
  findings.push('All 14 pages finish real R2 screening (84 claims), then navigate automatically to results. Final report records user_skipped with no LLM review records and zero actual calls.')
  assert.deepEqual(errors, [])
  assert.deepEqual(pageMutations, [{ method: 'POST', url: `${base}/api/tasks/${taskId}/skip-review?mode=real` }])
  const evidence = { status: 'passed', checked_at: new Date().toISOString(), fixture_only: false,
    task_id: taskId, findings, initial_task: pending, acknowledged_gate: acknowledged,
    continued_status: continuing.status, final_status: completed.status,
    final_gate: completed.review_gate, verification_pipeline: report.verification_pipeline,
    claim_count: report.claims.length, review_records: report.reviews.length,
    actual_external_llm_calls: 0, uploads: 1, page_mutation_requests: pageMutations,
    user_ready_task_unchanged: true, page_errors: errors }
  writeFileSync(path.join(out, 'browser-live-early-skip.json'), JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({ status: 'passed', task_id: taskId, checks: findings.length, actual_external_llm_calls: 0 }))
} catch (error) {
  writeFileSync(path.join(out, 'browser-live-early-skip-failure.json'), JSON.stringify({ task_id: taskId, error: String(error), page_errors: errors, page_mutations: pageMutations }, null, 2))
  throw error
} finally { await browser.close() }

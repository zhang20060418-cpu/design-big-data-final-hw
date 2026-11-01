// Explicit Mock-only browser QA. A run creates exactly one isolated task and
// sends one skip request; it never changes settings or calls a real LLM.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const base = 'http://127.0.0.1:8011'
const output = fileURLToPath(new URL('../../docs/4.4BC/llm_gate_checks/', import.meta.url))
const fixture = fileURLToPath(new URL('../../fixtures/integration_14_slides.pptx', import.meta.url))
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
const page = await context.newPage()
const errors = [], mutations = [], externalRequests = []
let taskId = process.env.GATE_QA_EXISTING_TASK || null
if (taskId) mutations.push(...JSON.parse(readFileSync(path.join(output, 'browser-failure.json'), 'utf8')).mutation_requests)
const findings = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => {
  if (!request.url().startsWith(base)) externalRequests.push({ method: request.method(), url: request.url() })
  if (request.url().includes('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
    mutations.push({ method: request.method(), url: request.url() })
  }
})

const readJson = async url => {
  const response = await context.request.get(url)
  assert.equal(response.ok(), true, `GET failed: ${url}: ${response.status()}`)
  return response.json()
}

try {
  await page.goto(base)
  const options = {
    enable_structure: true, enable_six_detectors: true, enable_visual: false,
    enable_mllm: true, llm_backend: 'mock', review_strategy: 'full_review',
    review_mode: 'manual', manual_pages: [4], max_api_calls: 1, max_review_pages: 14,
  }
  const upload = taskId ? null : await page.evaluate(async ({ bytesBase64, options }) => {
    const bytes = Uint8Array.from(atob(bytesBase64), char => char.charCodeAt(0))
    const body = new FormData()
    body.append('file', new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' }), 'integration_14_slides.pptx')
    body.append('name', `QA Mock gate browser skip ${new Date().toISOString()}`)
    body.append('options', JSON.stringify(options))
    body.append('prepare_only', 'false')
    const response = await fetch('/api/tasks', { method: 'POST', body })
    return { status: response.status, body: await response.json() }
  }, { bytesBase64: readFileSync(fixture).toString('base64'), options })
  if (upload) {
    assert.equal([200, 201].includes(upload.status), true)
    taskId = upload.body.id
  }
  writeFileSync(path.join(output, 'browser-task.json'), JSON.stringify({ task_id: taskId, options, uploaded_at: new Date().toISOString() }, null, 2))
  await page.goto(`${base}/tasks/${taskId}/progress`)
  const waitingPanel = page.getByRole('region', { name: 'LLM复检等待窗口' })
  await expect(waitingPanel).toBeVisible({ timeout: 300000 })
  const first = await readJson(`${base}/api/tasks/${taskId}`)
  assert.equal(first.review_gate.state, 'waiting')
  assert.equal(first.review_gate.countdown_seconds, 5)
  assert.equal(first.report_ready, false)
  const premature = await context.request.get(`${base}/api/tasks/${taskId}/report?format=json`)
  assert.equal(premature.status(), 409)
  await page.screenshot({ path: path.join(output, 'browser-countdown.png'), fullPage: true })
  findings.push('Real five-second backend waiting gate appeared after local R2 screening; report was not ready and report endpoint returned 409.')

  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(waitingPanel).toBeVisible()
  const refreshed = await readJson(`${base}/api/tasks/${taskId}`)
  assert.equal(refreshed.review_gate.deadline_at, first.review_gate.deadline_at)
  assert.equal(refreshed.review_gate.gate_id, first.review_gate.gate_id)
  assert.equal(refreshed.report_ready, false)
  const skipButton = page.getByRole('button', { name: '跳过 LLM 复检，查看结果', exact: true })
  await expect(skipButton).toBeEnabled()
  const skipResponsePromise = page.waitForResponse(response => response.url().includes(`/api/tasks/${taskId}/skip-review`) && response.request().method() === 'POST')
  await skipButton.click()
  const skipResponse = await skipResponsePromise
  assert.equal(skipResponse.ok(), true)
  const skipped = await skipResponse.json()
  assert.equal(skipped.state, 'skipped')
  findings.push('Reload preserved gate ID and original deadline; exactly one explicit button click skipped review within the original window.')

  await page.waitForURL(`**/tasks/${taskId}/results`, { timeout: 60000 })
  const warning = page.getByText(/跳过.*(误诊|漏检)/).first()
  await expect(warning).toBeVisible()
  const completed = await readJson(`${base}/api/tasks/${taskId}`)
  assert.equal(['completed', 'completed_with_errors'].includes(completed.status), true)
  assert.equal(completed.review_gate.state, 'skipped')
  assert.equal(completed.review_gate.skipped_by_user, true)
  const report = await readJson(`${base}/api/tasks/${taskId}/report?format=json`)
  assert.equal(report.review_gate.state, 'skipped')
  assert.equal(report.verification_pipeline.final_review_status, 'user_skipped')
  assert.deepEqual(report.reviews, [])
  await page.screenshot({ path: path.join(output, 'browser-skipped-result.png'), fullPage: true })
  findings.push('Backend finalized local results before automatic result navigation; visible warning and report explicitly say user_skipped, with zero review records.')
  assert.deepEqual(errors, [])
  assert.deepEqual(externalRequests, [])
  assert.equal(mutations.length, 2)
  assert.equal(mutations.filter(item => item.url === `${base}/api/tasks` && item.method === 'POST').length, 1)
  assert.equal(mutations.filter(item => item.url.includes(`/api/tasks/${taskId}/skip-review`) && item.method === 'POST').length, 1)
  const evidence = {
    status: 'passed', checked_at: new Date().toISOString(), base_url: base,
    task_id: taskId, options, findings, page_errors: errors, mutation_requests: mutations,
    external_browser_requests: externalRequests, actual_external_llm_calls: 0,
    initial_gate: first.review_gate, refreshed_gate: refreshed.review_gate,
    skip_response: skipped, completed_status: completed.status,
    report_review_gate: report.review_gate, verification_pipeline: report.verification_pipeline,
    review_records: report.reviews.length,
  }
  writeFileSync(path.join(output, 'browser-checks.json'), JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({ status: 'passed', task_id: taskId, checks: findings.length, mutation_requests: mutations.length }))
} catch (error) {
  writeFileSync(path.join(output, 'browser-failure.json'), JSON.stringify({ status: 'failed', task_id: taskId, error: String(error), page_errors: errors, mutation_requests: mutations, external_browser_requests: externalRequests }, null, 2))
  throw error
} finally { await browser.close() }

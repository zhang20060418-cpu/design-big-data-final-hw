// Deterministic browser response fixtures. No uploaded task or provider call.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const out = fileURLToPath(new URL('../../docs/4.4BC/llm_gate_fix_checks/', import.meta.url))
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
const page = await context.newPage(), errors = [], browserMutations = [], findings = []
page.on('pageerror', e => errors.push(e.message))
const base = 'http://127.0.0.1:8000', id = 'ui-review-gate-fixture'
const source = await (await context.request.get(`${base}/api/tasks/91b84b83454c4798`)).json()
const caps = await (await context.request.get(`${base}/api/system/capabilities`)).json()
caps.modules.mllm.state = 'ready'
let task = { ...source, id, status: 'ready', images_available: false, report_ready: false,
  options: { ...source.options, enable_mllm: false, mllm_user_choice: null, review_mode: 'risk_only', llm_backend: 'api' } }
const emptyGate = { state: 'not_ready', deadline_at: null, remaining_ms: 0, can_skip: false,
  countdown_seconds: null, eligible: false, skip_reason: null, skipped_by_user: false,
  execution_started_at: null, completed_at: null, outcome: null, duplicate_execution_guard: true, warning: null }
task.review_gate = { ...emptyGate }
const render = route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(task) })
await page.route('**/api/system/capabilities', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(caps) }))
await page.route(`**/api/tasks/${id}**`, async route => {
  const request = route.request(), url = new URL(request.url())
  if (request.method() === 'POST') {
    browserMutations.push({ path: url.pathname, body: request.postDataJSON?.() })
    if (url.pathname.endsWith('/start')) {
      const submitted = request.postDataJSON(); task.options = submitted.options; task.status = 'running'
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(task) })
    }
    if (url.pathname.endsWith('/skip-review')) {
      task.review_gate = { ...emptyGate, state: 'skipped', can_skip: false, skipped_by_user: true,
        skipped_before_review: true, skip_reason: 'user_skipped_before_review', warning: '跳过复核可能导致误诊或漏检。' }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(task.review_gate) })
    }
    throw new Error(`Unexpected fixture mutation ${request.method()} ${url.pathname}`)
  }
  if (url.pathname.endsWith('/slides')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slides: [] }) })
  return render(route)
})
try {
  await page.goto(`${base}/tasks/${id}/configure`)
  const toggle = page.getByRole('switch', { name: '多模态复核', exact: true })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  findings.push('Auto-choice null plus a configured API checks LLM by default even if an older saved display value was false.')
  await toggle.click(); await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.locator('#review-purpose').fill('UI fixture verifies explicit opt-out')
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('button', { name: '开始审查', exact: true }).click()
  await page.waitForURL(`**/tasks/${id}/progress`)
  const submitted = browserMutations.find(m => m.path.endsWith('/start')).body.options
  assert.equal(submitted.enable_mllm, false); assert.equal(submitted.mllm_user_choice, false)
  findings.push('Explicit close survives UI changes and is sent as mllm_user_choice=false at task start.')

  task.options = { ...task.options, enable_mllm: true, mllm_user_choice: null }
  task.status = 'running'; task.progress = .4
  task.review_gate = { ...emptyGate, can_skip: true, skip_available_before_stage: true }
  task.stages = task.stages.map(s => ({ ...s, status: s.key === 'structure' ? 'running' : ['visual', 'mllm', 'merge', 'report'].includes(s.key) ? 'pending' : 'done', message: '', detail: {} }))
  await page.reload()
  const phase = page.locator('.stage-item').filter({ has: page.getByText('多模态按需复核', { exact: true }) })
  const skip = phase.getByRole('button', { name: '跳过复核', exact: true })
  await expect(skip).toBeEnabled(); await skip.click()
  await expect(phase.getByText('已预约跳过', { exact: true })).toBeVisible()
  await expect(page.getByText(/首轮检测仍在进行，完成后自动展示结果/)).toBeVisible()
  assert.match(page.url(), /\/progress$/)
  await page.screenshot({ path: path.join(out, 'browser-early-skip-fixture.png'), fullPage: true })
  findings.push('Pending LLM stage has an adjacent enabled skip action. Early skip keeps local screening running and does not show results prematurely.')

  task.review_gate = { ...emptyGate, state: 'executing', can_skip: false }
  task.stages = task.stages.map(s => s.key === 'mllm' ? { ...s, status: 'running' } : s)
  await page.reload()
  await expect(page.getByRole('button', { name: '跳过复核', exact: true })).toBeDisabled()
  await expect(page.getByText('已发送请求，无法跳过', { exact: true })).toBeVisible()
  findings.push('Executing review disables the action and states that sent requests cannot be cancelled by skipping.')

  task.status = 'completed_with_errors'; task.report_ready = true
  task.review_gate = { ...emptyGate, state: 'completed', outcome: 'failed' }
  task.stages = task.stages.map(s => s.key === 'mllm' ? { ...s, status: 'failed', message: '按需复核执行完成。', detail: { review_errors: [{ slide_index: 3, error: '接口返回HTTP 502', error_code: 'http_502' }] } } : { ...s, status: s.key === 'visual' ? 'skipped' : 'done' })
  await page.reload()
  await expect(page.getByRole('alert').filter({ hasText: 'HTTP 502' })).toBeVisible()
  await expect(page.getByText('LLM 复核失败，未取得有效复核结论；首轮候选已保留。', { exact: true })).toBeVisible()
  await expect(page.getByText('按需复核执行完成。', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: path.join(out, 'browser-api-error-fixture.png'), fullPage: true })
  findings.push('Failed stage shows real HTTP code and page number instead of the misleading generic execution-complete message.')

  assert.deepEqual(errors, [])
  assert.equal(browserMutations.length, 2)
  writeFileSync(path.join(out, 'browser-ui-fixtures.json'), JSON.stringify({ status: 'passed', checked_at: new Date().toISOString(),
    fixture_only: true, findings, browser_intercepted_mutations: browserMutations, backend_mutations: 0,
    actual_external_llm_calls: 0, page_errors: errors }, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: findings.length, backend_mutations: 0, actual_external_llm_calls: 0 }))
} finally { await browser.close() }

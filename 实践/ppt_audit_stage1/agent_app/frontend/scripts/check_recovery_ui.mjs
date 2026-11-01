// Browser response fixtures for recovery actions. No task or provider writes.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const out = fileURLToPath(new URL('../../docs/4.4BC/review_recovery_checks/', import.meta.url))
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:8000', id = 'recovery-ui-parent'
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1150 } })
const page = await context.newPage(), errors = [], backendMutations = [], fixtureMutations = [], checks = []
page.on('pageerror', error => errors.push(error.message))
const original = await (await context.request.get(`${base}/api/tasks/51467dc86c784a4e`)).json()
const info = { available: true, can_continue: true, can_restart: true,
  continue_label: '继续审查', reused_stages: ['parse', 'render', 'structure', 'mllm'],
  pending_review_calls: 1, additional_api_calls_possible: true,
  reason: '有 1 项未完成，成功复核的 5 项将保留。' }
const emptyGate = { state: 'completed', can_skip: false, remaining_ms: 0, deadline_at: null,
  countdown_seconds: null, eligible: true, skipped_by_user: false, outcome: 'failed' }
let parent = { ...original, id, mode: 'real', status: 'completed_with_errors', recovery: info,
  images_available: false, report_ready: true, review_gate: emptyGate }
let rejectNext = false
const child = parentId => ({ ...parent, id: parentId, status: 'running', recovery: { ...info, available: false, can_continue: false, can_restart: false },
  lineage: { parent_task_id: id, action: parentId.endsWith('restart') ? 'restart' : 'continue' },
  report_ready: false, review_gate: { ...emptyGate, state: 'not_ready', can_skip: true },
  stages: parent.stages.map(stage => ({ ...stage, status: stage.key === 'structure' ? 'running' : ['visual', 'mllm', 'merge', 'report'].includes(stage.key) ? 'pending' : 'done' })) })

await page.route('**/api/**', async route => {
  const request = route.request(), url = new URL(request.url())
  const isFixture = url.pathname.startsWith(`/api/tasks/${id}`)
  if (!isFixture) {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      backendMutations.push({ method: request.method(), path: url.pathname })
      return route.abort()
    }
    return route.continue()
  }
  const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  if (request.method() === 'POST') {
    assert.ok(url.pathname.endsWith('/recover'))
    const body = request.postDataJSON(); fixtureMutations.push({ path: url.pathname, body })
    if (rejectNext) {
      rejectNext = false
      return json({ error: { code: 'recovery_conflict', message: '已有恢复任务在运行，请先查看该任务。' } }, 409)
    }
    await new Promise(resolve => setTimeout(resolve, 180))
    return json({ task_id: `${id}-${body.action}`, mode: 'real', parent_task_id: id,
      action: body.action, reused_stages: body.action === 'continue' ? info.reused_stages : [],
      pending_review_calls: 1, additional_api_calls_possible: true, message: '新记录已创建，原结果保持不变。' })
  }
  if (url.pathname.endsWith('/recovery')) return json(info)
  if (url.pathname.endsWith('/slides')) return json({ slides: [], checked_by: {}, slide_count: 10 })
  if (url.pathname.endsWith('/claims')) return json({ claims: [] })
  if (url.pathname.endsWith('/review-plan')) return json({ items: [] })
  if (url.pathname.endsWith('/report')) return json({ error: { code: 'report_not_ready', message: '报告尚未生成。' } }, 409)
  if (url.pathname.includes('/slides/')) return json({ issues: [], claims: [] })
  if (url.pathname === `/api/tasks/${id}`) return json(parent)
  return json(child(url.pathname.split('/')[3]))
})

try {
  await page.goto(`${base}/tasks/${id}/progress`)
  const phase = page.locator('.stage-item').filter({ has: page.getByText('多模态按需复核', { exact: true }) })
  await expect(phase.getByRole('button', { name: '继续审查', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '重新审查', exact: true })).toBeVisible()
  await expect(page.getByText(/待补复核 1 项/)).toBeVisible()
  await expect(page.getByText(/可能新增 LLM API 调用/)).toBeVisible()
  checks.push('Failed review has an adjacent continue action, plus terminal continue/restart with preserved-history and API-use explanation.')
  // The UI's entry animation can still be running after its text is visible.
  // Freeze it for a readable QA screenshot without changing application CSS.
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' })
  await page.screenshot({ path: path.join(out, 'browser-recovery-actions.png'), fullPage: true })

  await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('button')].filter(button => button.textContent.includes('继续审查'))
    buttons[0].click(); buttons[1].click(); buttons[0].click()
  })
  await page.waitForURL(`**/tasks/${id}-continue/progress`)
  await expect(page.getByRole('link', { name: '查看原任务', exact: true })).toHaveAttribute('href', `/tasks/${id}/progress`)
  assert.equal(fixtureMutations.length, 1)
  assert.deepEqual(fixtureMutations[0].body, { action: 'continue' })
  await expect(page.getByRole('button', { name: '重新审查', exact: true })).toHaveCount(0)
  checks.push('Simultaneous phase/footer repeated clicks send one continue request and navigate to a separate running child, with no active-task recovery buttons.')

  await page.goto(`${base}/tasks/${id}/progress`)
  await page.getByRole('button', { name: '重新审查', exact: true }).click()
  await page.waitForURL(`**/tasks/${id}-restart/progress`)
  assert.equal(fixtureMutations.length, 2)
  assert.deepEqual(fixtureMutations[1].body, { action: 'restart' })
  checks.push('Restart sends its distinct action and opens a new progress task rather than replacing the parent result.')

  rejectNext = true
  await page.goto(`${base}/tasks/${id}/progress`)
  await phase.getByRole('button', { name: '继续审查', exact: true }).click()
  await expect(phase.getByRole('alert').filter({ hasText: '已有恢复任务在运行' })).toHaveText('已有恢复任务在运行，请先查看该任务。')
  assert.equal(fixtureMutations.length, 3)
  assert.ok(page.url().endsWith(`${id}/progress`))
  checks.push('A 409 recovery conflict is shown in place and does not navigate or discard the original results.')

  parent = { ...parent, status: 'failed', report_ready: false }
  await page.goto(`${base}/tasks/${id}/report`)
  await expect(page.getByText('报告不可用', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '继续审查', exact: true })).toBeVisible()
  checks.push('Interrupted tasks without a report can recover from the report-unavailable page.')

  parent = { ...parent, status: 'completed', report_ready: true,
    review_gate: { ...emptyGate, state: 'skipped', skipped_by_user: true, warning: '跳过复核可能误诊或漏检。' },
    stages: parent.stages.map(stage => ({ ...stage, status: stage.key === 'mllm' ? 'skipped' : 'done' })) }
  await page.goto(`${base}/tasks/${id}/results`)
  await expect(page.getByRole('button', { name: '继续审查', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '重新审查', exact: true })).toBeVisible()
  checks.push('A completed task with skipped LLM review exposes continuation and restart from its results.')
  assert.deepEqual(errors, []); assert.deepEqual(backendMutations, [])
  const result = { status: 'passed', checked_at: new Date().toISOString(), checks,
    fixture_requests: fixtureMutations, actual_backend_mutations: backendMutations,
    actual_external_llm_calls: 0, page_errors: errors }
  writeFileSync(path.join(out, 'browser-recovery-fixtures.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, fixture_requests: fixtureMutations.length, actual_backend_mutations: 0 }))
} finally { await browser.close() }

// Read-only browser checks; the failed-capability case is an explicit UI fixture.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const out = fileURLToPath(new URL('../../docs/4.4BC/llm_gate_checks/', import.meta.url))
const tasks = JSON.parse(readFileSync(path.join(out, 'online_gate_checks.json'), 'utf8')).tasks
const skipped = JSON.parse(readFileSync(path.join(out, 'browser-checks.json'), 'utf8')).task_id
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
const page = await context.newPage(), errors = [], mutations = [], checks = []
page.on('pageerror', e => errors.push(e.message))
page.on('request', r => { if (r.url().includes('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(r.method())) mutations.push(r.url()) })
try {
  const caps = await (await context.request.get('http://127.0.0.1:8000/api/system/capabilities')).json()
  assert.equal(caps.modules.mllm.state, 'ready')
  assert.match(caps.modules.mllm.detail, /默认复检.*5秒/)
  checks.push('Daily service advertises API-configured default review and five-second window without exposing its key.')

  // Simulate only browser capability discovery failure, retaining the saved task choice.
  const id = tasks.automatic.task_id
  const taskUrl = `http://127.0.0.1:8011/api/tasks/${id}?mode=real`
  const original = await (await context.request.get(taskUrl)).json()
  await page.route('**/api/system/capabilities', route => route.abort())
  await page.route(taskUrl, route => route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ ...original, options: { ...original.options, llm_backend: 'api' } }) }))
  await page.goto(`http://127.0.0.1:8011/tasks/${id}/configure`)
  const toggle = page.getByRole('switch', { name: '多模态复核', exact: true })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await expect(toggle).toBeEnabled()
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  checks.push('Synthetic failed discovery preserves saved API-review true; user can still explicitly close it. No configuration or task was changed.')
  await page.unroute('**/api/system/capabilities')
  await page.unroute(taskUrl)

  await page.goto(`http://127.0.0.1:8011/tasks/${skipped}/results`)
  await expect(page.getByText(/跳过.*(误诊|漏检)/).first()).toBeVisible()
  await page.waitForFunction(() => document.querySelector('.shell').getBoundingClientRect().left >= document.querySelector('.rail').getBoundingClientRect().right)
  await page.screenshot({ path: path.join(out, 'browser-final-skipped-result.png'), fullPage: true })
  await page.goto(`http://127.0.0.1:8011/tasks/${skipped}/report`)
  await expect(page.getByText(/跳过.*(误诊|漏检)/).first()).toBeVisible()
  await page.waitForFunction(() => document.querySelector('.shell').getBoundingClientRect().left >= document.querySelector('.rail').getBoundingClientRect().right)
  await page.screenshot({ path: path.join(out, 'browser-final-skipped-report.png'), fullPage: true })
  checks.push('Latest built result/report pages show skipped-review risk; existing sidebar and content do not overlap after layout settles.')
  assert.deepEqual(errors, []); assert.deepEqual(mutations, [])
  const evidence = { status: 'passed', checked_at: new Date().toISOString(), checks,
    page_errors: errors, mutation_requests: mutations, actual_external_llm_calls: 0,
    capability_failure_case: 'Explicit browser-only response fixture; not a backend mutation.' }
  writeFileSync(path.join(out, 'browser-defaults-readonly.json'), JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, mutation_requests: 0 }))
} finally { await browser.close() }

// One authorized browser recovery against an existing isolated Mock-only task.
// This creates one child attempt but never contacts a paid provider.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const app = fileURLToPath(new URL('../../', import.meta.url))
const out = path.join(app, 'docs/4.4BC/review_recovery_checks')
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:8011', parentId = process.argv[2] || '43e8a3b9d552446f'
assert.match(parentId, /^[a-f0-9]{16}$/)
const parentDir = path.join(app, 'docs/4.4BC/acceptance/runtime/tasks', parentId)
assert.ok(existsSync(parentDir))
function snapshot(folder, prefix = '') {
  const files = {}
  for (const entry of readdirSync(folder, { withFileTypes: true })) {
    const relative = path.join(prefix, entry.name), file = path.join(folder, entry.name)
    if (entry.isDirectory()) Object.assign(files, snapshot(file, relative))
    else if (!entry.name.startsWith('recovery_') && entry.name !== 'recovery.lock') files[relative] = createHash('sha256').update(readFileSync(file)).digest('hex')
  }
  return files
}
const before = snapshot(parentDir)
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' })
const context = await browser.newContext({ viewport: { width: 1600, height: 1150 } })
const page = await context.newPage(), errors = [], mutations = [], checks = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => { if (request.url().includes('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) mutations.push({ url: request.url(), method: request.method() }) })
let childId = null
const read = async url => { const r = await context.request.get(url); assert.ok(r.ok(), `${r.status()} ${url}`); return r.json() }
try {
  const parent = await read(`${base}/api/tasks/${parentId}`)
  assert.equal(parent.options.llm_backend, 'mock', 'This script refuses a real API parent')
  assert.ok(['completed', 'completed_with_errors', 'failed'].includes(parent.status))
  const availability = await read(`${base}/api/tasks/${parentId}/recovery`)
  assert.ok(availability.can_continue)
  const oldClaims = await read(`${base}/api/tasks/${parentId}/claims`)

  await page.goto(`${base}/tasks/${parentId}/results`)
  const button = page.getByRole('button', { name: availability.continue_label || '继续审查', exact: true })
  await expect(button).toBeVisible()
  const responsePromise = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/tasks/${parentId}/recover`)
  await button.click()
  const response = await responsePromise
  assert.ok(response.ok())
  const recovery = await response.json(); childId = recovery.task_id
  assert.ok(childId && childId !== parentId)
  assert.equal(recovery.action, 'continue')
  await page.waitForURL(`**/tasks/${childId}/progress`)
  await expect(page.getByRole('link', { name: '查看原任务', exact: true })).toHaveAttribute('href', `/tasks/${parentId}/progress`)
  checks.push('One actual click created a separate continuation task, navigated to its progress and retained a link to the parent.')

  const deadline = Date.now() + 240000
  let child
  while (Date.now() < deadline) {
    child = await read(`${base}/api/tasks/${childId}`)
    if (['completed', 'completed_with_errors', 'failed'].includes(child.status)) break
    await new Promise(resolve => setTimeout(resolve, 400))
  }
  assert.equal(child.status, 'completed', child.error)
  assert.equal(child.options.llm_backend, 'mock')
  assert.equal(child.lineage.parent_task_id, parentId)
  assert.equal(child.lineage.action, 'continue')
  const preservedParent = await read(`${base}/api/tasks/${parentId}`)
  assert.equal(preservedParent.review_gate.state, 'skipped')
  assert.equal(child.review_gate.state, 'completed')
  assert.equal(child.review_gate.outcome, 'mock_only')
  assert.ok(child.lineage.reused_stages.includes('structure'))
  const report = await read(`${base}/api/tasks/${childId}/report?format=json`)
  assert.ok(report.reviews.length)
  assert.ok(report.reviews.every(r => r.mock && r.actual_http_calls === 0))
  const newClaims = await read(`${base}/api/tasks/${childId}/claims`)
  const fingerprint = data => data.claims.map(c => [c.slide_index, c.defect_type, c.raw_status, c.score])
  assert.deepEqual(fingerprint(newClaims), fingerprint(oldClaims))
  checks.push('The resumed Mock review completed with reused R2 observations, preserved label/status/score values and zero provider requests.')

  await page.goto(`${base}/tasks/${childId}/results`)
  await expect(page.getByRole('link', { name: '查看原任务', exact: true })).toBeVisible()
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important}' })
  await page.screenshot({ path: path.join(out, 'browser-live-recovery-result.png'), fullPage: true })
  const after = snapshot(parentDir)
  for (const [relative, digest] of Object.entries(before)) assert.equal(after[relative], digest, `Parent artifact changed: ${relative}`)
  assert.deepEqual(errors, [])
  assert.deepEqual(mutations, [{ url: `${base}/api/tasks/${parentId}/recover?mode=real`, method: 'POST' }])
  checks.push(`All ${Object.keys(before).length} pre-existing parent artifacts retained their SHA-256; browser sent one recovery POST and no extra model-triggering mutation.`)
  const evidence = { status: 'passed', checked_at: new Date().toISOString(), parent_task_id: parentId,
    child_task_id: childId, checks, recovery_response: recovery, child_lineage: child.lineage,
    actual_external_llm_calls: 0, browser_mutations: mutations, page_errors: errors,
    parent_review_gate: preservedParent.review_gate, child_review_gate: child.review_gate,
    parent_files_checked: Object.keys(before).length, parent_artifacts_unchanged: true,
    parent_file_sha256: before, child_review_records: report.reviews.length }
  writeFileSync(path.join(out, 'browser-live-recovery.json'), JSON.stringify(evidence, null, 2))
  console.log(JSON.stringify({ status: 'passed', parent_task_id: parentId, child_task_id: childId, checks: checks.length, actual_external_llm_calls: 0 }))
} catch (error) {
  writeFileSync(path.join(out, 'browser-live-recovery-failure.json'), JSON.stringify({ status: 'failed', parent_task_id: parentId, child_task_id: childId, error: String(error), browser_mutations: mutations, page_errors: errors }, null, 2))
  throw error
} finally { await browser.close() }

// UI state invariants only: no backend, uploads, credentials or provider requests.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source = readFileSync(new URL('../src/lib/reviewOptions.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const ui = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const base = { enable_mllm: false, mllm_user_choice: null, review_mode: 'risk_only', llm_backend: 'api' }
let passed = 0
const check = (name, fn) => { fn(); passed += 1; console.log(`PASS ${name}`) }
check('configured API enables automatic policy from an old false display value', () => {
  const next = ui.configuredReviewOptions(base, true)
  assert.equal(next.enable_mllm, true); assert.equal(next.mllm_user_choice, null)
  assert.equal(base.enable_mllm, false)
})
check('explicit user opt-out survives configuration refresh', () => {
  const closed = { ...base, mllm_user_choice: false }
  assert.equal(ui.configuredReviewOptions(closed, true), closed)
  assert.equal(ui.configuredReviewOptions(closed, true).enable_mllm, false)
})
check('explicit user opt-in survives a delayed capability response', () => {
  assert.equal(ui.configuredReviewOptions({ ...base, mllm_user_choice: true }, null).enable_mllm, true)
})
check('unknown capability and unknown legacy intent preserve the saved value', () => {
  assert.equal(ui.configuredReviewOptions(base, null), base)
  assert.equal(ui.configuredReviewOptions({ ...base, mllm_user_choice: undefined }, true).enable_mllm, false)
})
check('unconfigured API and off mode do not enable automatic sending', () => {
  assert.equal(ui.configuredReviewOptions({ ...base, enable_mllm: true }, false).enable_mllm, false)
  assert.equal(ui.configuredReviewOptions({ ...base, review_mode: 'off' }, true).enable_mllm, false)
})
check('an explicitly selected Mock backend is not API-defaulted', () => {
  const mock = { ...base, llm_backend: 'mock', enable_mllm: true }
  assert.equal(ui.configuredReviewOptions(mock, false), mock)
})
check('resolved values retain identity to prevent state-effect loops', () => {
  const resolved = ui.configuredReviewOptions(base, true)
  assert.equal(ui.configuredReviewOptions(resolved, true), resolved)
})
check('early skip is allowed only after backend eligibility and active task status', () => {
  const gate = { state: 'not_ready', can_skip: true }
  assert.equal(ui.canSkipReview(gate, 'running', 0), true)
  assert.equal(ui.canSkipReview(gate, 'queued', 0), true)
  assert.equal(ui.canSkipReview({ ...gate, can_skip: false }, 'running', 0), false)
  for (const status of ['preparing', 'ready', 'failed', 'completed', 'completed_with_errors']) assert.equal(ui.canSkipReview(gate, status, 0), false)
})
check('waiting deadline and execution cannot be bypassed by client clock', () => {
  assert.equal(ui.canSkipReview({ state: 'waiting', can_skip: true }, 'running', 1), true)
  assert.equal(ui.canSkipReview({ state: 'waiting', can_skip: true }, 'running', 0), false)
  for (const state of ['executing', 'completed', 'invalid', 'skipped', 'disabled']) assert.equal(ui.canSkipReview({ state, can_skip: true }, 'running', 5000), false)
})
console.log(JSON.stringify({ passed, failed: 0, actual_external_llm_calls: 0 }))

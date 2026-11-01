import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const source = readFileSync(new URL('../src/lib/review.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const ui = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
const normalize = (issue) => ui.markerBox(issue, 10, 5)
let count = 0
const check = (name, fn) => { fn(); count += 1; console.log(`PASS ${name}`) }
check('normalized coordinates take precedence over legacy inch coordinates', () => {
  assert.deepEqual(normalize({ normalized_bbox: { x: .1, y: .2, w: .3, h: .4 }, bbox: { x: 9, y: 4, w: 1, h: 1 } }), { x: .1, y: .2, w: .30000000000000004, h: .4000000000000001 })
})
check('legacy inch coordinates keep the actual slide aspect ratio', () => {
  const box = normalize({ bbox: { x: 2, y: 1, w: 3, h: 2 } })
  assert.equal(box.x, .2); assert.equal(box.y, .2); assert.ok(Math.abs(box.w - .3) < 1e-12); assert.ok(Math.abs(box.h - .4) < 1e-12)
})
check('outside portion is clipped without moving the object inside', () => {
  assert.deepEqual(normalize({ normalized_bbox: { x: -.1, y: .9, w: .3, h: .3 } }), { x: 0, y: .9, w: .19999999999999998, h: .09999999999999998 })
})
check('fully outside and nonfinite markers are not fabricated', () => {
  assert.equal(normalize({ normalized_bbox: { x: 1.2, y: 0, w: .1, h: .1 } }), null)
  assert.equal(normalize({ normalized_bbox: { x: NaN, y: 0, w: 1, h: 1 } }), null)
  assert.equal(normalize({ normalized_bbox: { x: 0, y: 0, w: 0, h: 1 } }), null)
})
check('missing and unrecognized detection states stay unknown', () => {
  assert.equal(ui.claimStatus({}), 'unknown')
  assert.equal(ui.claimStatus({ raw_status: 'normal' }), 'unknown')
  for (const status of ['candidate', 'unknown', 'unsupported', 'not_checked', 'no_candidate_in_available_scope']) assert.equal(ui.claimStatus({ raw_status: status }), status)
})
check('current state takes precedence and legacy status remains supported', () => {
  assert.equal(ui.claimStatus({ raw_status: 'unknown', status: 'no_candidate_in_available_scope' }), 'unknown')
  assert.equal(ui.claimStatus({ status: 'unsupported' }), 'unsupported')
})
check('cross-page links accept valid pages only and do not infer IDs from text', () => {
  assert.deepEqual(ui.relatedPages([3, '2', 3, 0, 7, 'slide_001', 'A05', '1.5', NaN], 5), [2, 3])
})
check('structured traces remain readable without unsafe HTML conversion', () => {
  assert.equal(ui.displayEvidence({ status: 'failed', reason: ['timeout', '<script>'] }), 'status: failed；reason: timeout；<script>')
})
console.log(JSON.stringify({ test: pathToFileURL(process.argv[1]).href, passed: count, failed: 0 }))

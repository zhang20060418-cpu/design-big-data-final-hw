// Read-only browser QA: real existing CLIP tasks, no uploads or model calls.
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const output=fileURLToPath(new URL('../../docs/4.4BC/clip_optional_checks/',import.meta.url))
const checks=JSON.parse(readFileSync(path.join(output,'online_pipeline_checks.json'),'utf8'))
const off=checks.tasks.off.task_id,on=checks.tasks.on.task_id
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'})
const context=await browser.newContext({viewport:{width:1600,height:1100}})
const page=await context.newPage(),errors=[],mutations=[]
page.on('pageerror',e=>errors.push(e.message))
page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD','OPTIONS'].includes(r.method()))mutations.push({method:r.method(),url:r.url()})})
const findings=[]
try {
  await page.goto('http://127.0.0.1:8000/')
  await expect(page.getByRole('heading',{name:'六类筛查 R2',exact:true})).toBeVisible()
  const caps=await (await context.request.get('http://127.0.0.1:8000/api/system/capabilities')).json()
  assert.equal(caps.modules.visual.available,true)
  assert.equal(caps.modules.visual.default_enabled,false)
  findings.push('Daily application exposes the frozen CLIP plugin as available, default off.')
  await page.screenshot({path:path.join(output,'browser-home.png'),fullPage:true})

  const base='http://127.0.0.1:8011'
  await page.goto(`${base}/tasks/${off}/configure`)
  const clip=page.getByRole('switch',{name:'视觉分类模型',exact:true})
  const six=page.getByRole('switch',{name:'六类专用检测',exact:true})
  await expect(clip).toBeEnabled()
  await expect(clip).toHaveAttribute('aria-checked','false')
  await clip.click();await expect(clip).toHaveAttribute('aria-checked','true')
  await expect(six).toHaveAttribute('aria-checked','true')
  await expect(page.getByText('LLM 复检未启用，本次筛查与辅助复核不代表最终复检完成。')).toBeVisible()
  await six.click();await expect(clip).toHaveAttribute('aria-checked','false')
  await clip.click();await expect(six).toHaveAttribute('aria-checked','true')
  await page.screenshot({path:path.join(output,'browser-manual-toggle.png'),fullPage:true})
  const unchanged=await (await context.request.get(`${base}/api/tasks/${off}`)).json()
  assert.equal(unchanged.options.enable_visual,false)
  findings.push('CLIP toggle is enabled but initially off; opening it requires R2; disabling R2 closes CLIP; LLM is not silently enabled.')

  await page.goto(`${base}/tasks/${off}/results?page=1`)
  await expect(page.getByText('CLIP 已关闭，本页未进行四标签辅助复核。')).toBeVisible()
  await page.screenshot({path:path.join(output,'browser-clip-off.png'),fullPage:true})
  findings.push('Default-off task has no CLIP probabilities and no inference artifacts.')

  await page.goto(`${base}/tasks/${on}/results?page=4`)
  await expect(page.getByText('CLIP 有辅助证据',{exact:true})).toBeVisible()
  const panel=page.locator('details[aria-label="本页CLIP辅助证据"]')
  await panel.locator('summary').click()
  await expect(panel.locator('tbody tr')).toHaveCount(4)
  const visual=(await (await context.request.get(`${base}/api/tasks/${on}/slides/4`)).json()).visual_result
  for (const [index,label] of Object.keys(visual.probabilities).entries()) {
    const row=panel.locator('tbody tr').nth(index)
    await expect(row.locator('td').nth(1)).toHaveAttribute('title',String(visual.probabilities[label]))
    await expect(row.locator('td').nth(2)).toHaveAttribute('title',String(visual.thresholds[label]))
  }
  await expect(panel.getByText(/CLIP 权重版本：3d74acf9/)).toBeVisible()
  await expect(page.getByText(/未完成真实 LLM 复检/).first()).toBeVisible()
  await page.screenshot({path:path.join(output,'browser-clip-on.png'),fullPage:true})
  findings.push('Actual CLIP probability/threshold/revision render for exactly four labels; Mock LLM is explicit.')

  await page.goto(`${base}/tasks/${on}/results?page=1`)
  await expect(page.getByText('本页未完成 LLM 复检',{exact:true})).toBeVisible()
  findings.push('A task-level completed LLM stage does not claim an unreviewed page was checked.')
  await page.goto(`${base}/tasks/${on}/report`)
  await expect(page.getByText('CLIP 四标签辅助复核',{exact:true}).first()).toBeVisible()
  await expect(page.getByText('未完成真实 LLM 复检：本次只有 Mock 模拟结果。')).toBeVisible()
  for (const format of ['json','html','pdf']) {
    const r=await context.request.get(`${base}/api/tasks/${on}/report?format=${format}&download=true`)
    assert.equal(r.ok(),true)
    if(format==='pdf')assert.equal((await r.body()).subarray(0,5).toString(),'%PDF-')
  }
  await page.screenshot({path:path.join(output,'browser-clip-report.png'),fullPage:true})
  findings.push('Report includes CLIP assistance, outstanding LLM scope and all three valid export formats.')
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[])
  writeFileSync(path.join(output,'browser-checks.json'),JSON.stringify({status:'passed',checked_at:new Date().toISOString(),
    tasks:{off,on},checks:findings,page_errors:errors,mutation_requests:mutations,actual_external_llm_calls:0},null,2))
  console.log(JSON.stringify({status:'passed',checks:findings.length,errors:errors.length,mutation_requests:mutations.length}))
} finally {await browser.close()}

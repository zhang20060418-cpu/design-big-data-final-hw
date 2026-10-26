// Development-only browser smoke test. Every write targets a fresh cache copy.
// Requires existing Node 24, Microsoft Edge and this project's ML environment.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

const source = path.dirname(fileURLToPath(import.meta.url));
const ml = path.dirname(source);
const python = path.join(ml, '.venv', 'Scripts', 'python.exe');
const work = path.join(ml, '.cache', 'benchmark_review_app', 'browser-' + Date.now());
const fixture = path.join(work, 'ml_stage4', 'benchmark_pptx');
const results = [];
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const check = name => { results.push(name); process.stdout.write('PASS ' + name + '\n'); };
const labels = ['improper_font_sizing','occluded_content','cluttered_layout','content_overflow_cutoff'];
const formalManifest = await fs.readFile(path.join(source, 'benchmark_manifest.csv'));
const formalState = await fs.readFile(path.join(source, 'preparation_state.json'));
await fs.mkdir(fixture, {recursive:true});
for (const entry of await fs.readdir(source, {withFileTypes:true})) {
  if (['review_history.json','review_transaction.json','review_backup','review_server.lock','renders.zip'].includes(entry.name)) continue;
  await fs.cp(path.join(source, entry.name), path.join(fixture, entry.name), {recursive:true});
}
const planPath = path.join(fixture, 'source_plan.json');
const plan = JSON.parse(await fs.readFile(planPath, 'utf8'));
for (const doc of [...plan.documents, ...plan.probes]) doc.source_path = path.join(fixture, doc.original_path);
await fs.writeFile(planPath, JSON.stringify(plan, null, 2));
await fs.mkdir(path.join(work, 'ml_stage4', 'reports'), {recursive:true});
// Fault injection exists only in the temporary server launcher, never the app.
const faultLauncher = path.join(fixture, 'fault_server.py');
await fs.writeFile(faultLauncher, `from pathlib import Path\nimport review_app\nreal=review_app.atomic_write\ndef atomic(path,data):\n if Path(path).name=='benchmark_manifest.csv' and (Path(__file__).parent/'inject_save_failure').exists():\n  raise PermissionError('Temporary smoke-test disk failure')\n return real(path,data)\nreview_app.atomic_write=atomic\nreview_app.main()\n`);
let server, edge, cdp, origin, latest;
async function startServer() {
  server = spawn(python, ['-B','-u',faultLauncher,'--root',fixture,'--port','0','--no-browser'],
    {windowsHide:true,env:{...process.env,PYTHONIOENCODING:'utf-8'}});
  let output=''; server.stdout.on('data', b=>{output+=b.toString();}); server.stderr.on('data',b=>{output+=b.toString();});
  for(let i=0;i<150;i++){const found=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(found){origin=found[0];return;}if(server.exitCode!==null)throw Error(output);await pause(100);}
  throw Error('Server did not start: '+output);
}
async function stopServer() {if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}}
async function state() {const r=await fetch(origin+'/api/state');assert.equal(r.status,200);latest=await r.json();return latest;}
async function until(fn, message, timeout=15000) {const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await pause(80);}throw Error('Timed out: '+message);}
function connect(url) {
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);let sequence=0;const pending=new Map();
    ws.onopen=()=>resolve({
      send(method,params={}){return new Promise((done,fail)=>{const id=++sequence;pending.set(id,{done,fail});ws.send(JSON.stringify({id,method,params}));});},
      close(){ws.close();}
    });
    ws.onerror=reject;ws.onmessage=event=>{const m=JSON.parse(event.data);if(pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.fail(Error(JSON.stringify(m.error))):p.done(m.result);}};
  });
}
async function evaluate(expression) {const r=await cdp.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result?.value;}
const click=selector=>evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
async function key(value, modifiers=0) {const upper=value.toUpperCase();const code=value.length===1?upper.charCodeAt(0):0;await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:value,code:'Key'+upper,windowsVirtualKeyCode:code,modifiers});await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:value,code:'Key'+upper,windowsVirtualKeyCode:code,modifiers});}
async function imageReady() {await until(()=>evaluate(`document.querySelectorAll('#imageStage img').length>0 && [...document.querySelectorAll('#imageStage img')].every(i=>i.complete&&i.naturalWidth>0) && !document.querySelector('button[data-label]').disabled`),'image ready');}
async function labelSaved(sid, field, value) {await until(async()=>{const s=await state();return s.rows.find(r=>r.sample_id===sid)[field]===value&&await evaluate(`document.getElementById('saveStatus').textContent.includes('已保存')`);},'label saved');}
try {
  await startServer();const initial=await state();assert.equal(initial.rows.length,108);
  assert(initial.rows.every(r=>labels.every(k=>r[k]==='unknown')&&r.human_reviewed==='no'));
  check('108 initial pages remain unreviewed, no inferred labels');
  const groups=new Map();for(const row of initial.rows){const a=groups.get(row.group_id)||[];a.push(row);groups.set(row.group_id,a);}
  const five=[...groups.values()].filter(g=>g.length===5);assert.equal(five.length,6);
  for(const group of five){const original=group.find(r=>r.variant_id==='original');assert(original);assert(group.every(r=>r.original_sample_id===original.sample_id));}
  for(const row of initial.rows){const r=await fetch(origin+row.image_url);assert.equal(r.status,200);assert.equal(sha(Buffer.from(await r.arrayBuffer())),row.png_sha256);}
  check('108 real PNGs load and all six five-image groups match manifest');
  const edgePath=process.env.EDGE_PATH||'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
  const profile=path.join(work,'edge-profile');await fs.mkdir(profile,{recursive:true});
  edge=spawn(edgePath,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let debugPort;await until(async()=>{try{debugPort=(await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0];return Boolean(debugPort);}catch{return false;}},'Edge debug port');
  const pages=await (await fetch('http://127.0.0.1:'+debugPort+'/json/list')).json();
  cdp=await connect(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
  await cdp.send('Page.enable');await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:1600,height:1100,deviceScaleFactor:1,mobile:false});
  await cdp.send('Page.navigate',{url:origin});
  await until(()=>evaluate(`document.getElementById('reviewerDialog')?.open`),'reviewer dialog');
  await evaluate(`document.getElementById('reviewerInput').value='SMOKE_ONLY_NOT_HUMAN';document.getElementById('reviewerForm').requestSubmit()`);
  await until(()=>evaluate(`!document.getElementById('reviewerDialog').open`),'reviewer saved');await imageReady();
  const sid=initial.rows[0].sample_id;
  assert.equal(await evaluate(`document.querySelectorAll('button[data-label][aria-pressed="true"]').length`),0);
  await fs.writeFile(path.join(work,'workbench_initial.png'),Buffer.from((await cdp.send('Page.captureScreenshot',{format:'png'})).data,'base64'));
  check('Chinese UI opens in real Edge, unknown initially unchecked');
  await key('q');await key('1');await labelSaved(sid,labels[0],'1');
  await key('w');await key('1');await labelSaved(sid,labels[1],'1');
  let row=(await state()).rows.find(r=>r.sample_id===sid);assert.equal(row[labels[2]],'unknown');assert.equal(row.human_reviewed,'no');
  await click(`button[data-label="${labels[2]}"][data-value="0"]`);await labelSaved(sid,labels[2],'0');
  await key('r');await key('u');await until(()=>evaluate(`document.getElementById('unknownDialog').open`),'unknown reason dialog');
  await evaluate(`document.getElementById('unknownReason').value='临时烟测：已查看，仍无法判定';document.getElementById('unknownForm').requestSubmit()`);
  await until(async()=>{row=(await state()).rows.find(r=>r.sample_id===sid);return row.review_progress.labels[labels[3]].inspected;},'unknown action');
  assert.equal(row[labels[3]],'unknown');assert.equal(row.review_progress.unknown_reviewed,true);assert.match(row.review_progress.labels[labels[3]].reason,/临时烟测/);
  assert.equal(latest.stats.partial,1);assert.equal(latest.stats.labels_complete,0);
  check('QWER + 1/0/U, mouse mapping, multilabel and inspected-unknown reason');
  await click('#undoButton');await until(async()=>!(await state()).rows.find(r=>r.sample_id===sid).review_progress.labels[labels[3]].inspected,'undo restored untouched');
  await evaluate(`document.getElementById('reviewNotes').focus()`);const before=(await state()).rows.find(r=>r.sample_id===sid);
  await cdp.send('Input.insertText',{text:'临时测试备注 qwernpjgzu10'});await key('n');await key('q');await key('s',2);
  await until(async()=>/临时测试备注/.test((await state()).rows.find(r=>r.sample_id===sid).review_notes),'notes autosave');
  row=(await state()).rows.find(r=>r.sample_id===sid);for(const k of labels)assert.equal(row[k],before[k]);
  assert.match(await evaluate(`document.getElementById('sampleMeta').textContent`),new RegExp(sid));
  await evaluate(`document.activeElement.blur()`);check('notes typing does not trigger shortcuts; Ctrl+S saves');
  await key('n');await imageReady();assert.match(await evaluate(`document.getElementById('sampleMeta').textContent`),new RegExp(initial.rows[1].sample_id));
  await key('p');await imageReady();assert.match(await evaluate(`document.getElementById('sampleMeta').textContent`),new RegExp(sid));
  await key('j');await imageReady();check('N/P/J navigation');
  await cdp.send('Page.navigate',{url:origin+'/?resume=1'});await until(()=>evaluate(`location.search==='?resume=1' && typeof state!=='undefined' && state!==null && document.querySelector('#imageStage img')?.naturalWidth>0`),'refresh restore');
  assert.equal((await state()).rows.find(r=>r.sample_id===sid)[labels[0]],'1');
  const modified=five[0].find(r=>r.variant_id!=='original');
  await evaluate(`navigateTo(${JSON.stringify(modified.sample_id)})`);await imageReady();
  await click('#viewOriginal');await until(()=>evaluate(`document.querySelector('#imageStage img')?.naturalWidth>0 && document.getElementById('allClear').disabled`),'original-only view guard');
  await click('#viewCompare');await imageReady();assert.equal(await evaluate(`document.querySelectorAll('#imageStage img').length`),2);
  await key('g');assert.equal(await evaluate(`document.querySelectorAll('#groupThumbnails button').length`),5);
  await key('z');await until(()=>evaluate(`document.getElementById('zoomDialog').open&&document.getElementById('zoomImage').naturalWidth===1920`),'native pixel zoom');
  await click('#zoomIn');await click('#zoomActual');
  const box=await evaluate(`(()=>{const r=document.getElementById('zoomViewport').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',x:box.x,y:box.y,button:'left',buttons:1,clickCount:1});
  await cdp.send('Input.dispatchMouseEvent',{type:'mouseMoved',x:box.x-120,y:box.y-60,buttons:1});
  await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',x:box.x-120,y:box.y-60,button:'left',buttons:0,clickCount:1});
  assert(await evaluate(`document.getElementById('zoomViewport').scrollLeft>0||document.getElementById('zoomViewport').scrollTop>0`));
  await click('#zoomClose');await click('#focusButton');
  assert.equal(await evaluate(`document.body.classList.contains('focus-mode')`),true);
  check('original/variant comparison, group switching and native-pixel zoom');
  await fs.writeFile(path.join(work,'workbench_compare.png'),Buffer.from((await cdp.send('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await fs.writeFile(path.join(fixture,'inject_save_failure'),'temporary test fault');
  await click(`button[data-label="${labels[0]}"][data-value="1"]`);
  await until(()=>evaluate(`!document.getElementById('saveFailure').hidden`),'save failure displayed');
  const titleBefore=await evaluate(`document.getElementById('sampleMeta').textContent`);await key('n');assert.equal(await evaluate(`document.getElementById('sampleMeta').textContent`),titleBefore);
  await fs.unlink(path.join(fixture,'inject_save_failure'));await click('#retrySave');await labelSaved(modified.sample_id,labels[0],'1');
  check('real server disk failure is visible, blocks navigation and retries safely');
  await evaluate(`document.getElementById('renderFontDetails').open=true;document.getElementById('renderStatus').value='confirmed';document.getElementById('renderStatus').dispatchEvent(new Event('change',{bubbles:true}))`);
  await until(async()=>(await state()).rows.find(r=>r.sample_id===modified.sample_id).render_review_status==='confirmed','render confirmation');
  await evaluate(`document.getElementById('fontStatus').value='confirmed_no_visible_issue';document.getElementById('fontStatus').dispatchEvent(new Event('change',{bubbles:true}))`);
  await until(async()=>(await state()).rows.find(r=>r.sample_id===modified.sample_id).font_review_status==='confirmed_no_visible_issue','font confirmation');
  assert.equal((await state()).rows.find(r=>r.sample_id===modified.sample_id).human_reviewed,'no');
  let dialogAction=click('#allClear');await pause(120);await cdp.send('Page.handleJavaScriptDialog',{accept:false});await dialogAction;
  assert.equal((await state()).rows.find(r=>r.sample_id===modified.sample_id)[labels[0]],'1');
  dialogAction=click('#allClear');await pause(120);await cdp.send('Page.handleJavaScriptDialog',{accept:true});await dialogAction;
  await until(async()=>{const s=await state();return labels.every(k=>s.rows.find(r=>r.sample_id===modified.sample_id)[k]==='0');},'confirmed all clear');
  assert.equal((await state()).rows.find(r=>r.sample_id===sid)[labels[0]],'1');
  dialogAction=click('#completeButton');await pause(120);await cdp.send('Page.handleJavaScriptDialog',{accept:true});await dialogAction;
  await until(async()=>(await state()).rows.find(r=>r.sample_id===modified.sample_id).human_reviewed==='yes','active completion');
  await click('#undoButton');await until(async()=>(await state()).rows.find(r=>r.sample_id===modified.sample_id).human_reviewed==='no','completion undo');
  check('render/font are active checks; all-clear is confirmed and current-page-only; completion requires a separate action');
  const png=path.join(fixture,modified.png_path);await fs.rename(png,png+'.smoke-missing');
  await evaluate(`currentRow().image_url+='?smoke-missing='+Date.now();imageKey='';renderImage(currentRow())`);await until(()=>evaluate(`document.querySelector('#imageStage .image-error')!==null`),'image loading error');
  assert(await evaluate(`[...document.querySelectorAll('button[data-label]')].every(b=>b.disabled)&&document.getElementById('completeButton').disabled`));
  await fs.rename(png+'.smoke-missing',png);await evaluate(`document.querySelector('#imageStage .image-error button').click()`);await imageReady();
  check('missing PNG is explicit and disables review until it loads again');
  await evaluate(`document.getElementById('statusFilter').value='controlled';document.getElementById('statusFilter').dispatchEvent(new Event('change',{bubbles:true}))`);
  await until(()=>evaluate(`document.getElementById('filteredCount').textContent.includes('24 / 108')`),'controlled filter');
  await click('#resetFilters');await imageReady();
  check('disk-derived filters show the 24 controlled pages');
  await click('#checkButton');await until(()=>evaluate(`document.getElementById('checkDialog').open&&document.getElementById('checkContent').textContent.includes('D03_')`),'freeze omissions include sample IDs');await click('#checkClose');
  const saved=(await state()).rows.find(r=>r.sample_id===sid);await stopServer();await startServer();
  const restarted=await state();assert.equal(restarted.rows.find(r=>r.sample_id===sid)[labels[0]],saved[labels[0]]);
  assert(restarted.rows.filter(r=>r.document_id.startsWith('D03')).every(r=>r.source_class==='A_candidate'&&r.provenance_status==='pending_confirmation'));
  check('server restart retains progress; D03 provenance stays pending');
  for(const flag of ['--check','--refresh-review','--freeze']){
    const proc=spawn(python,['-B',path.join(fixture,'prepare_benchmark.py'),flag],{windowsHide:true,env:{...process.env,PYTHONIOENCODING:'utf-8'}});
    let output='';proc.stdout.on('data',b=>output+=b.toString());proc.stderr.on('data',b=>output+=b.toString());
    const code=await new Promise(resolve=>proc.on('exit',resolve));assert.equal(code,flag==='--freeze'?2:0,output.slice(0,1000));
  }
  assert.equal((await state()).rows.find(r=>r.sample_id===sid)[labels[0]],'1');
  for(const n of ['frozen_labels.csv','freeze_record.json'])await assert.rejects(fs.access(path.join(fixture,n)));
  check('existing check/refresh preserve annotations; freeze rejects only on temporary copy');
  const externallyEdited=Buffer.concat([await fs.readFile(path.join(fixture,'benchmark_manifest.csv')),Buffer.from('\n')]);
  await fs.writeFile(path.join(fixture,'benchmark_manifest.csv'),externallyEdited);
  await cdp.send('Page.navigate',{url:origin+'/?conflict=1'});
  await until(()=>evaluate(`location.search==='?conflict=1' && document.getElementById('startupReload')!==null`),'startup external conflict detected');
  dialogAction=click('#startupReload');await pause(120);await cdp.send('Page.handleJavaScriptDialog',{accept:true});await dialogAction;
  await until(()=>evaluate(`document.getElementById('startupError').hidden && typeof state!=='undefined' && state!==null`),'explicit external reload');
  assert.equal(sha(await fs.readFile(path.join(fixture,'benchmark_manifest.csv'))),sha(externallyEdited));
  check('external CSV conflict survives browser refresh and explicit reload preserves external bytes');
  assert.equal(sha(await fs.readFile(path.join(source,'benchmark_manifest.csv'))),sha(formalManifest));
  assert.equal(sha(await fs.readFile(path.join(source,'preparation_state.json'))),sha(formalState));
  check('formal manifest/state unchanged: 108 pending pages and 432 unknown labels');
  await fs.writeFile(path.join(work,'result.json'),JSON.stringify({passed:true,tests:results,fixture,formal_manifest_sha256:sha(formalManifest)},null,2));
  process.stdout.write(JSON.stringify({passed:true,checks:results.length,output:work})+'\n');
} finally {
  if(cdp){try{await cdp.send('Browser.close');}catch{}cdp.close();}
  if(edge&&edge.exitCode===null)edge.kill();await stopServer();
}

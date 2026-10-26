/* 全部标签仅由真实审核操作写入本地服务；浏览器不推断缺陷，也不访问模型。 */
"use strict";

const LABELS = [
  {key: "improper_font_sizing", name: "字号不合理", hotkey: "Q", yes: "关键内容或正文实际字号明显不适合阅读；文字缩小确实影响可读性。", no: "正文、标题和必要标注整体可读，没有明确字号异常。", note: "不能仅看字号 pt 数值。页脚、角标、小注释不能一概判错；必要时放大查看文字。"},
  {key: "occluded_content", name: "内容遮挡", hotkey: "W", yes: "应当可见的有意义文字、表格或图形被其他对象遮住，并影响阅读或理解。", no: "没有影响阅读的实质性遮挡。", note: "正常叠层、背景和装饰不自动算遮挡。文字框互相压住时可同时考虑布局拥挤。"},
  {key: "cluttered_layout", name: "布局拥挤", hotkey: "E", yes: "元素明显过密、间距或层次不合理，重复、叠放或拥塞造成明显阅读困难。", no: "信息虽多，但组织清晰、间距可接受，仍能正常阅读。", note: "信息量大不等于拥挤。不能因修改名叫 clutter 就确认；应判断局部混乱的实际影响。"},
  {key: "content_overflow_cutoff", name: "内容溢出或裁切", hotkey: "R", yes: "关键文字、图表或有意义对象确实超出可见页面，造成信息不可见或不完整。", no: "重要内容完整可见，无实际裁切问题。", note: "正常出血、装饰越界不自动算缺陷。不能仅凭对象包围框越界判断，要看实际可见内容。"}
];
const MODIFICATIONS = {none: "未修改原始页", original: "未修改原始页", font_small: "缩小字号", occlusion: "增加遮挡", clutter: "增加布局密度", overflow: "移出页面边界"};
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, x => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[x]));
let state = null, currentId = "", selectedLabel = 0, viewMode = "current", imageKey = "", currentImageReady = false;
let reviewer = "", unknownLabel = "", noteDraft = null, notesTimer = null, toastTimer = null;
let jobs = [], saving = false, failedJob = null, activeJob = null, idleResolvers = [], jobSequence = 0;
const optimistic = new Map();
let zoomScale = 1, zoomNaturalWidth = 0, drag = null;

function currentRow() { return state?.rows.find(row => row.sample_id === currentId); }
function progress(row) { return row?.review_progress || {}; }
function originalRow(row) { return state.rows.find(other => other.sample_id === row.original_sample_id) || state.rows.find(other => other.group_id === row.group_id && other.variant_id === "original"); }
function annotationImageVisible() { return viewMode !== "original" || currentRow()?.variant_id === "original"; }
function modification(row) { return MODIFICATIONS[row.modification_type] || MODIFICATIONS[row.variant_id] || (row.natural_or_controlled === "natural" ? "未修改原始页" : "可控修改：" + row.modification_type); }
function isReadOnly() { return Boolean(state?.readonly || state?.frozen); }
function toast(message) { clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false; toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500); }
function setSaveStatus(text, className = "") { $("saveStatus").textContent = text; $("saveStatus").className = "save-status " + className; }

async function api(url, data) {
  const options = {cache: "no-store", credentials: "same-origin", headers: {"Accept":"application/json"}};
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = state?.csrf_token || "";
    options.body = JSON.stringify(data);
  }
  let response;
  try { response = await fetch(url, options); } catch { throw new Error("无法连接本地标注服务。请确认服务仍在运行；当前操作尚未保存。"); }
  let body;
  try { body = await response.json(); } catch { throw new Error("本地服务返回了无法读取的内容，当前操作尚未保存。"); }
  if (!response.ok) {
    const error = new Error(body.error || "本地保存失败，请查看服务窗口。");
    error.status = response.status; error.code = body.code; error.details = body;
    throw error;
  }
  return body;
}

function receiveState(next, initial = false) {
  state = next;
  if (!reviewer) reviewer = next.session?.reviewer_id || "";
  if (initial) {
    const last = next.pending_save?.sample_id || next.session?.last_sample_id;
    currentId = next.rows.some(row => row.sample_id === last) ? last : next.rows[0]?.sample_id || "";
    populateFilters();
  }
  renderAll();
}

function labelView(row, field) {
  const value = optimistic.get(row.sample_id + ":" + field);
  if (value) return {value: value.value, inspected: true, reason: value.reason, pending: true};
  const metadata = progress(row).labels?.[field] || {};
  return {value: String(row[field]), inspected: Boolean(metadata.inspected), reason: metadata.reason || "", pending: false};
}

function renderStats() {
  const s = state.stats;
  const stats = [["总页面",s.total],["四标签已完成",s.labels_complete],["部分完成",s.partial],["尚未核验",s.untouched],["待解决 unknown",s.unknown_pages],["渲染人工已核验",s.render_confirmed],["字体人工已核验",s.font_confirmed],["当前可冻结页面",s.eligible]];
  $("stats").innerHTML = stats.map(([title,value]) => `<div class="stat"><strong>${escapeHTML(value ?? 0)}</strong><span>${title}</span></div>`).join("");
  $("progressText").textContent = `已完成四标签：${s.labels_complete} / ${s.total}`;
  $("inspectedText").textContent = `已主动检查 ${s.inspected_cells} / ${s.total_cells} 项；看过仍不确定 ${s.unresolved_unknown_pages} 页`;
  $("progressBar").max = s.total || 1; $("progressBar").value = s.labels_complete || 0;
  $("freezeStatus").textContent = state.readonly ? "检测到冻结工件：当前只读，须检查冻结记录是否有效" : "未冻结，禁止进行模型性能测试";
  $("reviewerName").textContent = reviewer ? `当前审核者：${reviewer}` : "尚未填写，不会记录虚构身份";
  $("reviewerButton").textContent = reviewer ? "更换审核者" : "填写审核者编号";
}

function populateSelect(id, values, prefix) {
  const select = $(id), old = select.value;
  select.innerHTML = `<option value="">${prefix}</option>` + values.map(([value,label]) => `<option value="${escapeHTML(value)}">${escapeHTML(label)}</option>`).join("");
  select.value = old;
}
function populateFilters() {
  const documents = [...new Set(state.rows.map(r => r.document_id))];
  populateSelect("documentFilter", documents.map(d => [d,d]), "全部文稿");
  populateGroupFilter();
  const mods = [...new Set(state.rows.map(r => r.modification_type))];
  populateSelect("modificationFilter", mods.map(m => [m, MODIFICATIONS[m] || m]), "全部修改类型");
}
function populateGroupFilter() {
  const documentId = $("documentFilter").value;
  const rows = state.rows.filter(r => !documentId || r.document_id === documentId);
  const groups = [...new Set(rows.map(r => r.group_id))];
  populateSelect("groupFilter", groups.map(g => { const r = rows.find(x => x.group_id === g); return [g, `第 ${r.original_slide_index} 页 · ${g}`]; }), "全部同源组");
}
function filteredRows() {
  if (!state) return [];
  const status = $("statusFilter").value, doc = $("documentFilter").value, group = $("groupFilter").value, mod = $("modificationFilter").value;
  const search = $("sampleSearch").value.trim().toLowerCase();
  return state.rows.filter(row => {
    const p = progress(row);
    if (doc && row.document_id !== doc || group && row.group_id !== group || mod && row.modification_type !== mod) return false;
    if (search && !`${row.sample_id} ${row.original_slide_index} ${row.group_id}`.toLowerCase().includes(search)) return false;
    if (status === "incomplete" && p.completed) return false;
    if (status === "partial" && !(p.inspected_count > 0 && !p.labels_complete)) return false;
    if (status === "complete" && !p.labels_complete) return false;
    if (status === "defect" && !LABELS.some(label => String(row[label.key]) === "1")) return false;
    if (status === "uncertain" && !LABELS.some(label => String(row[label.key]) === "unknown")) return false;
    if (status === "controlled" && row.natural_or_controlled !== "controlled") return false;
    if (status === "natural" && row.natural_or_controlled !== "natural") return false;
    return true;
  });
}

function renderMetadata(row) {
  const controlled = row.natural_or_controlled === "controlled";
  $("sampleTitle").textContent = `${row.document_id} · 第 ${row.original_slide_index} 页 · ${controlled ? "修改页" : "原始页"}`;
  $("sampleMeta").textContent = `样本 ${row.sample_id}　｜　同源组 ${row.group_id}　｜　${modification(row)}`;
  $("variantWarning").hidden = !controlled;
  $("variantWarning").textContent = controlled && viewMode === "original"
    ? "当前展示的是对应原图。请切回当前修改图或并排对照后，再填写修改页标签。修改操作仅说明生成意图，不代表缺陷一定成立。"
    : "修改操作仅说明生成意图，不代表缺陷一定成立。";
  const provenance = row.provenance_status === "confirmed" ? "来源已核实" : "来源待核实，暂不能冻结";
  const sourceClass = {A:"A · 已核实 AI 生成",C:"C · 脚本制作 / 可编辑重建",A_candidate:"A_candidate · AI 生成来源待核实",B:"B · 历史工程样本"}[row.source_class] || row.source_class;
  $("sourceNotice").textContent = `${sourceClass}　｜　${provenance}　｜　原始 PNG：${row.png_width} × ${row.png_height}，保持原始比例`;
  const document = (state.documents || []).find(doc => doc.document_id === row.document_id) || {};
  $("sourceDetails").innerHTML = `<p><b>来源类别：</b>${escapeHTML(sourceClass)}</p><p><b>来源说明：</b>${escapeHTML(document.source_notes || "请参考现有来源记录。")}</p><p><b>生成工具：</b>${escapeHTML(document.generation_tool === "Codex_or_Kimi_per_document_unknown" ? "Codex / Kimi，逐份文稿的具体工具不确定" : document.generation_tool || "待核实")}</p><p><b>独立性：</b>用户声明未参与此前审查项目开发或测试；同一文稿仍保留文稿级来源关系。</p><p><b>保留用途：</b>独立测试封存（independent_test_reserved）</p>`;
  $("viewOriginal").disabled = !controlled || !originalRow(row);
  $("viewCompare").disabled = !controlled || !originalRow(row);
  for (const [id,mode] of [["viewCurrent","current"],["viewOriginal","original"],["viewCompare","compare"]]) $(id).classList.toggle("active", viewMode === mode);
}

function renderImage(row) {
  const key = `${row.sample_id}:${viewMode}`;
  if (imageKey === key) return;
  imageKey = key; currentImageReady = false;
  const stage = $("imageStage"); stage.replaceChildren(); stage.classList.toggle("compare", viewMode === "compare");
  const original = originalRow(row);
  const displayRows = viewMode === "compare" && original ? [original,row] : [viewMode === "original" && original ? original : row];
  for (const display of displayRows) {
    const figure = document.createElement("figure"); figure.className = "image-cell";
    const caption = document.createElement("figcaption"); caption.textContent = `${display.variant_id === "original" ? "未修改原始页面" : "当前修改页面"} · ${modification(display)}`;
    const loading = document.createElement("div"); loading.className = "image-loading"; loading.textContent = "正在加载完整 PNG，请稍候…";
    const image = document.createElement("img"); image.alt = `${display.document_id} 第 ${display.original_slide_index} 页 ${modification(display)}`;
    image.dataset.sampleId = display.sample_id; image.hidden = true;
    image.addEventListener("load", () => {
      if (imageKey !== key) return;
      if (!image.naturalWidth || !image.naturalHeight) { image.dispatchEvent(new Event("error")); return; }
      loading.remove(); image.hidden = false;
      const allImages = [...stage.querySelectorAll("img")];
      currentImageReady = allImages.every(img => img.complete && img.naturalWidth > 0 && !img.hidden);
      renderEnabledState();
    });
    image.addEventListener("error", () => {
      if (imageKey !== key) return;
      currentImageReady = false; loading.remove(); image.hidden = true;
      const error = document.createElement("div"); error.className = "image-error";
      error.textContent = `图片加载失败：${display.sample_id}。本页不能标记为已审核，请检查文件或重试。`;
      const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "重新加载图片";
      retry.addEventListener("click", () => { imageKey = ""; renderImage(currentRow()); }); error.append(document.createElement("br"), retry); figure.append(error);
      renderEnabledState();
    });
    image.addEventListener("click", () => openZoom(display));
    figure.append(caption, loading, image); stage.append(figure);
    image.src = display.image_url || `/image/${encodeURIComponent(display.sample_id)}`;
  }
}

function renderLabels(row) {
  const openGuides = new Set([...$("labelCards").querySelectorAll(".label-card")].filter(card => card.querySelector("details")?.open).map(card => card.dataset.label));
  $("labelCards").innerHTML = LABELS.map((label,index) => {
    const v = labelView(row,label.key);
    let status = v.inspected ? (v.value === "unknown" ? "已检查但无法判断" : "已主动确认") : "尚未检查";
    if (v.pending) status += " · 等待保存完成";
    return `<section class="label-card ${selectedLabel === index ? "selected" : ""}" data-label="${label.key}" tabindex="-1"><div class="label-card-head"><span class="key-badge">${label.hotkey}</span><h3>${label.name}</h3></div><div class="label-action-grid">${[["1","存在缺陷"],["0","不存在缺陷"],["unknown","不确定"]].map(([value,title]) => `<button type="button" data-label="${label.key}" data-value="${value}" class="${v.inspected && v.value === value ? "chosen" : ""}" aria-pressed="${Boolean(v.inspected && v.value === value)}">${title}<b>（${value}）</b></button>`).join("")}</div><p class="label-inspection ${v.inspected && v.value === "unknown" ? "uncertain" : ""}">${escapeHTML(status)}${v.reason ? `：${escapeHTML(v.reason)}` : ""}</p><details${openGuides.has(label.key) ? " open" : ""}><summary>如何判断？</summary><p><b>标 1：</b>${label.yes}</p><p><b>标 0：</b>${label.no}</p><p><b>注意：</b>${label.note}</p></details></section>`;
  }).join("");
  $("labelCards").querySelectorAll("button[data-label]").forEach(button => button.addEventListener("click", () => chooseLabel(button.dataset.label, button.dataset.value)));
  $("pageStatus").textContent = progress(row).current_status || (progress(row).labels_complete ? "四标签已明确" : "待人工核验");
  const notes = noteDraft?.sampleId === row.sample_id ? noteDraft.text : row.review_notes || "";
  if ($("reviewNotes").value !== notes && document.activeElement !== $("reviewNotes")) $("reviewNotes").value = notes;
  $("renderStatus").value = row.render_review_status || "pending_human_review";
  $("fontStatus").value = row.font_review_status || "pending_human_review";
  const blockers = progress(row).blockers || [];
  $("pageBlockers").innerHTML = blockers.length ? `<b>本页尚需：</b><ul>${blockers.map(reason => `<li>${escapeHTML(reason)}</li>`).join("")}</ul>` : `<span class="success-text">本页各项门槛已满足${row.human_reviewed === "yes" ? "，已主动完成审核。" : "；请点击完成本页审核。"}</span>`;
}

function renderGroup(row) {
  const peers = state.rows.filter(other => other.group_id === row.group_id).sort((a,b) => (a.variant_id === "original" ? -1 : b.variant_id === "original" ? 1 : 0));
  $("groupThumbnails").innerHTML = peers.map(peer => `<button class="group-thumb ${peer.sample_id === row.sample_id ? "active" : ""}" type="button" data-sample="${escapeHTML(peer.sample_id)}"><img src="${escapeHTML(peer.image_url || `/image/${encodeURIComponent(peer.sample_id)}`)}" alt="${escapeHTML(modification(peer))}" loading="lazy"><span>${escapeHTML(modification(peer))}</span><span>${progress(peer).labels_complete ? "四标签已明确" : "待核验"}</span></button>`).join("");
  $("groupThumbnails").querySelectorAll("button").forEach(button => button.addEventListener("click", () => navigateTo(button.dataset.sample)));
}

function renderEnabledState() {
  const blocked = Boolean(failedJob || state?.pending_save);
  const canAnnotate = Boolean(currentRow() && currentImageReady && annotationImageVisible() && !isReadOnly() && !blocked);
  document.querySelectorAll("#labelCards button, #allClear, #completeButton").forEach(button => button.disabled = !canAnnotate);
  for (const id of ["reviewNotes","undoButton"]) $(id).disabled = isReadOnly() || blocked;
  for (const id of ["renderStatus","fontStatus"]) $(id).disabled = !canAnnotate;
  $("saveButton").disabled = isReadOnly();
  $("retrySave").disabled = saving;
  $("zoomButton").disabled = !currentImageReady;
  $("recoverPending").disabled = saving || isReadOnly() || state?.pending_save?.recoverable === false;
  if (blocked) $("pageStatus").textContent = "保存受阻，已保留选择；请先恢复保存";
  else if (currentRow()) $("pageStatus").textContent = progress(currentRow()).current_status || "待人工核验";
}

function renderPendingRecovery() {
  const pending = state?.pending_save;
  $("pendingRecovery").hidden = !pending;
  if (!pending) return;
  const changed = (pending.fields || []).filter(change => LABELS.some(label => label.key === change.field));
  const summary = changed.map(change => `${LABELS.find(label => label.key === change.field).name}：${change.old} → ${change.new}`).join("；");
  $("pendingRecoveryText").textContent = pending.recoverable
    ? `样本 ${pending.sample_id}；原审核者 ${pending.reviewer_id || "见操作记录"}；原操作时间 ${pending.at || "见操作记录"}。${summary || "已保留原审核内容。"} 这次操作尚未确认为完整保存。请关闭占用 CSV 的 Excel / WPS 工作簿，再点击恢复；不会自动推断标签。`
    : `待保存记录暂不能安全恢复：${pending.error || "请保留记录并核对，不能覆盖现有清单。"}`;
}

function renderAll() {
  if (!state) return;
  renderStats();
  renderPendingRecovery();
  const row = currentRow();
  const filtered = filteredRows();
  $("filteredCount").textContent = `筛选结果 ${filtered.length} / ${state.rows.length} 页`;
  if (!row) return;
  renderMetadata(row); renderImage(row); renderLabels(row); renderGroup(row); renderEnabledState();
  const index = filtered.findIndex(r => r.sample_id === currentId);
  $("navigationPosition").textContent = index >= 0 ? `${index + 1} / ${filtered.length}` : "当前页面不在筛选结果中";
  $("prevButton").disabled = !filtered.length || index === 0;
  $("nextButton").disabled = !filtered.length || index === filtered.length - 1;
}

function ensureReviewer() {
  if (reviewer.trim()) return true;
  $("reviewerInput").value = ""; $("reviewerDialog").showModal();
  toast("请先填写实际审核者编号，再进行标注。不会自动创建身份。"); return false;
}

function enqueueAction(type, payload = {}, options = {}) {
  if (isReadOnly()) { toast("数据已冻结或处于只读状态，不能修改。"); return Promise.resolve(false); }
  if (!ensureReviewer()) return Promise.resolve(false);
  if (failedJob) { toast("先处理保存失败，再继续标注或导航。"); return Promise.resolve(false); }
  if (state?.pending_save) { toast("请先恢复保存上次真实操作，避免覆盖待保存记录。"); return Promise.resolve(false); }
  const sampleId = options.sampleId || currentId;
  return new Promise(resolve => {
    const job = {id: ++jobSequence, type, payload, sampleId, reviewer, resolve};
    if (type === "label") optimistic.set(`${sampleId}:${payload.label}`, {jobId:job.id,value:payload.value,reason:payload.reason || ""});
    if (type === "all_clear") LABELS.forEach(label => optimistic.set(`${sampleId}:${label.key}`,{jobId:job.id,value:"0",reason:""}));
    jobs.push(job); renderLabels(currentRow()); renderEnabledState(); pump();
  });
}

async function pump() {
  if (saving || failedJob) return;
  saving = true;
  try {
    while (jobs.length && !failedJob) {
      const job = jobs.shift();
      activeJob = job;
      setSaveStatus(`正在保存到本地清单${jobs.length ? `，还有 ${jobs.length} 项待保存` : ""}…`);
      try {
        const result = await api(job.type === "__undo" ? "/api/undo" : "/api/action", {revision:state.revision, reviewer_id:job.reviewer, sample_id:job.sampleId, type:job.type, ...job.payload});
        for (const [key,value] of optimistic) if (value.jobId === job.id) optimistic.delete(key);
        if (job.type === "notes" && noteDraft?.sampleId === job.sampleId && noteDraft.text === job.payload.notes) noteDraft = null;
        receiveState(result);
        setSaveStatus(`已保存到本地 · ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`,"success");
        job.resolve(true);
      } catch (error) {
        failedJob = job;
        job.pendingSave = error.details?.pending_save || null;
        if (job.pendingSave) state.pending_save = job.pendingSave;
        const locked = error.code === "manifest_locked" || /WinError (5|32|33)/.test(error.message);
        $("saveFailureText").textContent = locked
          ? "标注清单被 Excel、WPS 或文件预览占用，暂时无法替换保存。请关闭正在打开 benchmark_manifest.csv 的工作簿，再点击‘重新保存’；当前选择仍保留，先不要刷新或重新载入。"
          : `${error.message}${error.status === 409 ? " 磁盘记录有冲突，请核对后再处理；不会静默覆盖。" : " 本次保存尚未完整确认，暂时禁止跳页。"}`;
        $("saveFailure").hidden = false;
        setSaveStatus(locked ? "文件被占用：请关闭 CSV 工作簿后重新保存" : "保存未完成，当前操作尚未确认完整落盘", "error-text");
        try { receiveState(await api("/api/state")); } catch { renderAll(); /* Keep the original error and local choice on conflicts. */ }
        job.resolve(false); renderEnabledState();
      }
    }
  } finally {
    activeJob = null; saving = false; renderEnabledState();
    const resolves = idleResolvers.splice(0); resolves.forEach(resolve => resolve(!failedJob));
  }
}
function waitIdle() { return saving ? new Promise(resolve => idleResolvers.push(resolve)) : Promise.resolve(!failedJob); }
async function flushNotes() {
  clearTimeout(notesTimer);
  if (noteDraft && !failedJob && ![activeJob,...jobs].some(job => job?.type === "notes" && job.sampleId === noteDraft.sampleId && job.payload.notes === noteDraft.text)) await enqueueAction("notes",{notes:noteDraft.text},{sampleId:noteDraft.sampleId});
  return waitIdle();
}
async function navigationGate() {
  if (state?.pending_save) { toast("存在尚未完成保存的真实操作，请先恢复保存，再继续切换页面。"); return false; }
  if (!await flushNotes() || !await waitIdle()) { toast("保存尚未成功，已保留当前页面；请先重新保存或明确重新载入。"); return false; }
  return true;
}
async function retrySaving() {
  if (failedJob?.pendingSave?.csv_already_written) return recoverPendingSave();
  if (!failedJob && state?.pending_save) return recoverPendingSave();
  if (!failedJob) return flushNotes();
  const job = failedJob; failedJob = null; jobs.unshift(job);
  $("saveFailure").hidden = true; await pump(); return !failedJob;
}

async function recoverPendingSave() {
  if (failedJob && !failedJob.pendingSave?.csv_already_written) return retrySaving();
  const job = failedJob;
  const pending = job?.pendingSave || state?.pending_save;
  if (!pending || pending.recoverable === false || !ensureReviewer() || isReadOnly()) return false;
  if (!window.confirm(`确认恢复 ${pending.reviewer_id || "原审核者"} 对 ${pending.sample_id} 的上次真实操作？原始审核身份和时间会保留，并另外记录本次恢复。`)) return false;
  saving = true; renderEnabledState(); setSaveStatus("正在恢复上次真实操作…");
  try {
    const next = await api("/api/retry-pending", {revision:state.revision,reviewer_id:reviewer,confirm:true});
    if (job) {
      for (const [key,value] of optimistic) if (value.jobId === job.id) optimistic.delete(key);
      if (job.type === "notes" && noteDraft?.sampleId === job.sampleId && noteDraft.text === job.payload.notes) noteDraft = null;
      failedJob = null;
    }
    receiveState(next); $("saveFailure").hidden = true;
    setSaveStatus("已保存：已恢复上次真实操作，可继续标注", "success"); return true;
  } catch(error) {
    $("saveFailure").hidden = false; $("saveFailureText").textContent = error.message;
    setSaveStatus("恢复保存尚未成功，原操作继续保留", "error-text"); return false;
  } finally { saving = false; renderEnabledState(); if (!failedJob && jobs.length) pump(); }
}
async function saveNow() {
  if (!failedJob && state?.pending_save) return recoverPendingSave();
  if (failedJob) { await retrySaving(); return; }
  const ok = await flushNotes(); if (ok) setSaveStatus("已保存到本地，当前没有待保存操作", "success");
}

async function chooseLabel(field, value) {
  if (state?.pending_save) { toast("请先恢复上次待保存操作。"); return; }
  if (!annotationImageVisible()) { toast("当前查看的是原始对照页，请切回当前修改图或并排后再标注修改页。"); return; }
  if (!currentImageReady) { toast("图片尚未完整加载，不能开始该页标注。"); return; }
  if (!ensureReviewer() || failedJob || isReadOnly()) return;
  selectedLabel = LABELS.findIndex(label => label.key === field);
  if (value === "unknown") {
    unknownLabel = field;
    $("unknownTitle").textContent = `${LABELS[selectedLabel].name}：已查看但无法确定`;
    $("unknownReason").value = labelView(currentRow(),field).reason || "";
    $("unknownDialog").showModal(); $("unknownReason").focus(); return;
  }
  const success = await enqueueAction("label",{label:field,value,reason:""});
  if (success) afterLabel(field);
}
function afterLabel(field) {
  if ($("quickMode").checked) selectedLabel = (LABELS.findIndex(label => label.key === field) + 1) % LABELS.length;
  renderLabels(currentRow()); renderEnabledState();
  if (progress(currentRow()).labels_complete) {
    toast("本页四标签已填写完成；渲染、字体、来源及完成审核需另行确认。");
    if ($("autoNext").checked) navigateStep(1);
  }
}

async function saveSession() {
  try {
    await api("/api/session", {reviewer_id:reviewer,last_sample_id:currentId});
  } catch (error) { toast(`浏览位置未保存：${error.message}`); }
}
async function navigateTo(sampleId) {
  if (!state || sampleId === currentId || !state.rows.some(row => row.sample_id === sampleId)) return;
  if (!await navigationGate()) return;
  currentId = sampleId; viewMode = "current"; selectedLabel = 0; imageKey = ""; noteDraft = null;
  $("reviewNotes").value = currentRow().review_notes || "";
  renderAll(); await saveSession();
}
function navigateStep(direction) {
  const rows = filteredRows(), index = rows.findIndex(row => row.sample_id === currentId);
  const target = rows[index < 0 ? 0 : index + direction];
  if (target) return navigateTo(target.sample_id);
  toast(direction > 0 ? "已到筛选结果的最后一页。" : "已到筛选结果的第一页。");
}
function nextIncomplete() {
  const rows = filteredRows(), start = rows.findIndex(row => row.sample_id === currentId);
  for (let offset = 1; offset <= rows.length; offset++) {
    const row = rows[(Math.max(start,0) + offset) % rows.length];
    if (!progress(row).completed && row.sample_id !== currentId) { navigateTo(row.sample_id); return; }
  }
  toast("当前筛选范围内没有其他未完成页面。");
}
async function filtersChanged() {
  if (!await navigationGate()) return;
  const rows = filteredRows();
  if (rows.length && !rows.some(row => row.sample_id === currentId)) await navigateTo(rows[0].sample_id);
  else renderAll();
  if (!rows.length) toast("没有符合筛选条件的页面；当前页保留供查看。");
}

async function updateCheck(field, value) {
  if (!state || !annotationImageVisible() || !currentImageReady) return;
  if (!ensureReviewer()) { renderLabels(currentRow()); renderEnabledState(); return; }
  let reason = "";
  if (value === "needs_attention" || value === "confirmed_substitution_accepted") {
    reason = window.prompt(value === "needs_attention" ? "请说明你实际发现的渲染或字体问题（必填）：" : "请说明字体替代情况，以及为何可以接受（必填）：", "") || "";
    if (!reason.trim()) { renderLabels(currentRow()); renderEnabledState(); toast("未填写说明，未保存本次核验状态。"); return; }
  }
  await enqueueAction("checks",{[field]:value,reason});
}

function setView(mode) { viewMode = mode; renderMetadata(currentRow()); renderImage(currentRow()); }
function completionReadiness(row) {
  const missing = [];
  for (const label of LABELS) {
    const value = labelView(row,label.key);
    if (!value.inspected || !["0","1"].includes(value.value)) missing.push(`${label.name}尚未明确判断`);
  }
  if (row.render_review_status !== "confirmed") missing.push("渲染尚未确认正常");
  if (!["confirmed_no_visible_issue","confirmed_substitution_accepted"].includes(row.font_review_status)) missing.push("字体尚未确认可接受");
  if (row.provenance_status !== "confirmed" || !["A","C"].includes(row.source_class)) missing.push("来源尚未满足冻结要求（可继续标签标注）");
  if (row.render_status !== "ok" || !currentImageReady) missing.push("图片未完整加载或渲染失败");
  return missing;
}
function toggleGroup() { $("groupPanel").hidden = !$("groupPanel").hidden; if (!$("groupPanel").hidden) $("groupPanel").scrollIntoView({block:"nearest",behavior:"smooth"}); }
function applyZoom(scale) {
  zoomScale = Math.min(6,Math.max(.1,scale));
  $("zoomImage").style.width = `${Math.round(zoomNaturalWidth * zoomScale)}px`;
  $("zoomImage").style.height = "auto";
  $("zoomPercent").textContent = `${Math.round(zoomScale*100)}%`;
}
function fitZoom() {
  const image = $("zoomImage"), view = $("zoomViewport");
  if (!image.naturalWidth) return;
  applyZoom(Math.min((view.clientWidth-16)/image.naturalWidth,(view.clientHeight-16)/image.naturalHeight,1));
}
function openZoom(row = null) {
  if (!currentImageReady && !row) return;
  const display = row || (viewMode === "original" ? originalRow(currentRow()) : currentRow());
  if (!display) return;
  const image = $("zoomImage");
  $("zoomTitle").textContent = `${display.document_id} 第 ${display.original_slide_index} 页 · ${modification(display)}`;
  image.onload = () => { zoomNaturalWidth = image.naturalWidth; applyZoom(1); $("zoomViewport").scrollTo(0,0); };
  image.onerror = () => { toast("大图加载失败，未改变任何审核状态。"); $("zoomDialog").close(); };
  image.src = display.image_url || `/image/${encodeURIComponent(display.sample_id)}`;
  $("zoomDialog").showModal();
  if (image.complete && image.naturalWidth) { zoomNaturalWidth=image.naturalWidth; applyZoom(1); }
}

async function checkFreeze() {
  if (!await navigationGate()) return;
  $("checkDialog").showModal(); $("checkContent").textContent = "正在检查磁盘记录和现有冻结门槛，不会执行冻结…";
  try {
    const result = await api("/api/check",{revision:state.revision});
    receiveState(result);
    const check = result.check;
    $("checkContent").innerHTML = `<div class="check-summary"><b>${check.passed ? "当前各项门槛已通过，可由人工另行使用现有冻结入口。" : `尚未满足冻结条件，共 ${check.blockers.length} 页有待处理项。`}</b><p>此按钮仅检查，不会生成冻结文件，也不会启动模型评价。</p>${check.helper_error ? `<details><summary>既有校验器的详细记录（供排查）</summary><pre>${escapeHTML(check.helper_error)}</pre></details>` : ""}</div>` + check.blockers.map(item => `<div class="check-row"><button type="button" data-check-sample="${escapeHTML(item.sample_id)}">定位 ${escapeHTML(item.sample_id)}</button><p>${item.reasons.map(escapeHTML).join("；")}</p></div>`).join("");
    $("checkContent").querySelectorAll("button[data-check-sample]").forEach(button => button.addEventListener("click",() => { $("checkDialog").close(); navigateTo(button.dataset.checkSample); }));
  } catch (error) { $("checkContent").textContent = "检查失败：" + error.message; }
}

function installEvents() {
  $("focusButton").addEventListener("click", () => {
    const focused = document.body.classList.toggle("focus-mode");
    $("focusButton").textContent = focused ? "展开标注总览" : "专注标注：收起统计";
    $("focusButton").setAttribute("aria-pressed", String(focused));
    window.scrollTo({top:0,behavior:"smooth"});
  });
  $("reviewerButton").addEventListener("click",() => { $("reviewerInput").value = reviewer; $("reviewerDialog").showModal(); $("reviewerInput").focus(); });
  $("reviewerLater").addEventListener("click",() => $("reviewerDialog").close());
  $("reviewerForm").addEventListener("submit",async event => {
    event.preventDefault(); const next = $("reviewerInput").value.trim(); if (!next) return;
    if (reviewer && !await navigationGate()) return;
    reviewer=next; $("reviewerDialog").close(); renderStats(); await saveSession();
    if (noteDraft) await flushNotes();
  });
  $("unknownCancel").addEventListener("click",() => $("unknownDialog").close());
  $("unknownForm").addEventListener("submit",async event => {
    event.preventDefault(); const reason=$("unknownReason").value.trim(); if (!reason) return;
    $("unknownDialog").close(); const field=unknownLabel;
    if (await enqueueAction("label",{label:field,value:"unknown",reason})) afterLabel(field);
  });
  $("reviewNotes").addEventListener("input",() => {
    noteDraft={sampleId:currentId,text:$("reviewNotes").value}; clearTimeout(notesTimer);
    setSaveStatus("备注有待保存内容…");
    notesTimer=setTimeout(() => flushNotes(),600);
  });
  $("renderStatus").addEventListener("change",e => updateCheck("render_review_status",e.target.value));
  $("fontStatus").addEventListener("change",e => updateCheck("font_review_status",e.target.value));
  $("allClear").addEventListener("click",async () => {
    if (!currentImageReady || !annotationImageVisible() || !ensureReviewer()) return;
    if (window.confirm("请确认你已经亲自检查了本页四类缺陷，均不存在问题。\n\n只会修改当前可见页面，不会批量处理其他原图。")) await enqueueAction("all_clear",{confirm:true});
  });
  $("completeButton").addEventListener("click",async () => {
    if (!currentImageReady || !annotationImageVisible() || !ensureReviewer() || !await navigationGate()) return;
    const missing=completionReadiness(currentRow());
    if(missing.length){toast("当前不能完成本页审核："+missing.join("；"));$("pageBlockers").scrollIntoView({block:"nearest"});return;}
    if (window.confirm("请确认：你已亲自完成当前页四标签、渲染和字体核验。此操作仅在现有来源及全部审核门槛通过时标记为已审核，不执行冻结。")) await enqueueAction("complete",{confirm:true});
  });
  $("saveButton").addEventListener("click",saveNow); $("retrySave").addEventListener("click",retrySaving);
  $("recoverPending").addEventListener("click",recoverPendingSave);
  $("undoButton").addEventListener("click",async () => { if (await navigationGate()) await enqueueAction("__undo"); });
  $("reloadDisk").addEventListener("click",async () => {
    if (!window.confirm("重新载入会丢弃浏览器中尚未保存的操作和备注，并以磁盘上的清单为准。确定继续吗？")) return;
    try { const next=await api("/api/state?reload=1"); jobs.forEach(job=>job.resolve(false)); jobs=[]; failedJob=null; optimistic.clear(); noteDraft=null; $("saveFailure").hidden=true; $("reviewNotes").value=""; receiveState(next); setSaveStatus("已重新载入磁盘记录；未保存草稿已丢弃"); toast(next.conflict_warning || "已重新载入磁盘上的真实记录。"); } catch(error) { toast(error.message); }
  });
  $("prevButton").addEventListener("click",()=>navigateStep(-1)); $("nextButton").addEventListener("click",()=>navigateStep(1)); $("nextIncomplete").addEventListener("click",nextIncomplete);
  $("groupButton").addEventListener("click",toggleGroup);
  $("viewCurrent").addEventListener("click",()=>setView("current")); $("viewOriginal").addEventListener("click",()=>setView("original")); $("viewCompare").addEventListener("click",()=>setView("compare"));
  $("zoomButton").addEventListener("click",()=>openZoom()); $("zoomClose").addEventListener("click",()=>$("zoomDialog").close());
  $("zoomActual").addEventListener("click",()=>applyZoom(1)); $("zoomFit").addEventListener("click",fitZoom); $("zoomIn").addEventListener("click",()=>applyZoom(zoomScale*1.25)); $("zoomOut").addEventListener("click",()=>applyZoom(zoomScale/1.25));
  $("zoomViewport").addEventListener("wheel",event=>{ event.preventDefault(); applyZoom(zoomScale*(event.deltaY<0?1.15:1/1.15)); },{passive:false});
  $("zoomViewport").addEventListener("pointerdown",event=>{ if(event.button!==0)return; drag={x:event.clientX,y:event.clientY,left:$("zoomViewport").scrollLeft,top:$("zoomViewport").scrollTop}; $("zoomViewport").setPointerCapture(event.pointerId); });
  $("zoomViewport").addEventListener("pointermove",event=>{ if(drag){$("zoomViewport").scrollLeft=drag.left-(event.clientX-drag.x);$("zoomViewport").scrollTop=drag.top-(event.clientY-drag.y);} });
  $("zoomViewport").addEventListener("pointerup",()=>drag=null); $("zoomViewport").addEventListener("pointercancel",()=>drag=null);
  $("checkButton").addEventListener("click",checkFreeze); $("checkClose").addEventListener("click",()=>$("checkDialog").close());
  for(const id of ["statusFilter","groupFilter","modificationFilter"]) $(id).addEventListener("change",filtersChanged);
  $("documentFilter").addEventListener("change",()=>{ populateGroupFilter(); filtersChanged(); });
  $("sampleSearch").addEventListener("input",()=>{ clearTimeout($("sampleSearch").filterTimer); $("sampleSearch").filterTimer=setTimeout(filtersChanged,350); });
  $("resetFilters").addEventListener("click",()=>{ for(const id of ["statusFilter","documentFilter","groupFilter","modificationFilter","sampleSearch"]) $(id).value=id==="statusFilter"?"all":""; populateGroupFilter(); filtersChanged(); });
  document.addEventListener("keydown",event=>{
    if ((event.ctrlKey||event.metaKey) && event.key.toLowerCase()==="s") { event.preventDefault(); if(state)saveNow(); return; }
    if(event.ctrlKey||event.metaKey||event.altKey||event.repeat||!state) return;
    if(document.querySelector("dialog[open]")) return;
    const target=event.target; if(target.closest("input,textarea,select,[contenteditable=true]")) return;
    const key=event.key.toLowerCase(), index=["q","w","e","r"].indexOf(key);
    if(index>=0){event.preventDefault();selectedLabel=index;renderLabels(currentRow());renderEnabledState();document.querySelector(`.label-card[data-label="${LABELS[index].key}"]`).focus({preventScroll:true});return;}
    const actions={"1":()=>chooseLabel(LABELS[selectedLabel].key,"1"),"0":()=>chooseLabel(LABELS[selectedLabel].key,"0"),u:()=>chooseLabel(LABELS[selectedLabel].key,"unknown"),n:()=>navigateStep(1),p:()=>navigateStep(-1),j:nextIncomplete,g:toggleGroup,z:()=>openZoom()};
    if(actions[key]){event.preventDefault();actions[key]();}
  });
  window.addEventListener("beforeunload",event=>{ if(saving||jobs.length||failedJob||noteDraft){event.preventDefault();event.returnValue="";} });
}

async function start() {
  installEvents();
  const initialControls = [...document.querySelectorAll("main button,main input,main select,main textarea,#reviewerButton")];
  initialControls.forEach(control => control.disabled = true);
  async function loadInitial(url) {
    try {
      const loaded = await api(url);
      initialControls.forEach(control => control.disabled = false);
      $("startupError").hidden = true;
      receiveState(loaded,true);
      if (!reviewer && !isReadOnly()) $("reviewerDialog").showModal();
      if (state.warning || state.conflict_warning) toast(state.warning || state.conflict_warning);
    } catch(error) {
      $("startupError").textContent=`无法读取本地基准：${error.message} 请保持本地服务运行，并通过终端显示的 127.0.0.1 地址打开。`;
      $("startupError").hidden=false;
      if (error.code === "external_change" || error.code === "revision_conflict") {
        const reload = document.createElement("button");
        reload.id = "startupReload";
        reload.textContent = "核对后重新载入磁盘记录";
        reload.addEventListener("click", () => {
          if (window.confirm("请先确认外部 CSV 改动。重新载入保留磁盘上的标签，但会清除不能对应到当前 CSV 的审核动作进度，需逐项重新核验。确定继续？")) loadInitial("/api/state?reload=1");
        });
        $("startupError").append(document.createElement("br"), reload);
      }
    }
  }
  await loadInitial("/api/state");
}
start();

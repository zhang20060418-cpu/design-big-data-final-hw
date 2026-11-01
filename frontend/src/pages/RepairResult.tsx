import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { DEFECT_LABEL, type RepairOp, type RepairRecord, type TaskDetail } from '../lib/types'
import { Icon } from '../components/Icon'
import { Skeleton } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'

const OP_STATUS: Record<string, { text: string; tone: string }> = {
  applied: { text: '已修复', tone: 'ok' },
  partial: { text: '部分修复', tone: 'warn' },
  manual: { text: '需人工', tone: 'warn' },
  skipped: { text: '已跳过', tone: 'idle' },
  pending: { text: '待处理', tone: 'idle' },
  'pending-layer': { text: '待处理', tone: 'idle' },
}

const ACTION_TEXT: Record<string, string> = {
  move_inside: '移回画布',
  wrap_and_grow: '开启换行',
  raise_font: '放大字号',
  bring_front: '图层置顶',
  manual: '人工处理',
}

export default function RepairResult() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const repairId = params.get('rid') || ''
  const navigate = useNavigate()
  const [record, setRecord] = useState<RepairRecord | null>(null)
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [view, setView] = useState<'after' | 'before' | 'split' | 'annot'>('after')
  const [busy, setBusy] = useState(false)
  const [hints, setHints] = useState<Record<string, string>>({})
  const [hint, setHint] = useState('')
  const [hintBusy, setHintBusy] = useState(false)
  const [brief, setBrief] = useState<{ prompt: string } | null>(null)
  const [briefBusy, setBriefBusy] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const rid = repairId || (await api.repairs(taskId, mode)).items?.[0]?.repair_id
        if (!rid) throw new Error('还没有修复记录。')
        const [detail, current] = await Promise.all([api.getTask(taskId, mode), api.repair(taskId, rid, mode)])
        if (!alive) return
        setTask(detail); setRecord(current)
        const first = current.pages.find((item) => item.ops > 0)?.index ?? current.pages[0]?.index ?? 1
        setPage(first)
      } catch (err) { if (alive) setError((err as Error).message) }
    }
    void load()
    return () => { alive = false }
  }, [taskId, repairId, mode])

  useEffect(() => {
    let alive = true
    api.repairHints(taskId, mode)
      .then((res) => { if (alive) setHints(res.hints ?? {}) })
      .catch(() => { if (alive) setHints({}) })
    return () => { alive = false }
  }, [taskId, mode])

  // 切换页码时把该页已有的人工想法带进来，避免改了另一页的内容
  useEffect(() => { setHint(hints[String(page)] ?? '') }, [hints, page])

  useEffect(() => {
    if (!record?.repair_id) return
    let alive = true
    setBriefBusy(true)
    api.repairBrief(taskId, record.repair_id, page, mode)
      .then((res) => { if (alive) setBrief({ prompt: res.prompt }) })
      .catch(() => { if (alive) setBrief(null) })
      .finally(() => { if (alive) setBriefBusy(false) })
    return () => { alive = false }
  }, [taskId, record?.repair_id, page, mode])

  const current = useMemo(() => record?.pages.find((item) => item.index === page) ?? null, [record, page])
  const pageOps = useMemo(
    () => (record?.ops ?? []).filter((op) => op.slide_index === page),
    [record, page],
  )

  const again = async () => {
    setBusy(true)
    try {
      const next = await api.startRepair(taskId, 'api', mode)
      navigate(`/repair/${taskId}/progress?rid=${next.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const saveHint = async () => {
    setHintBusy(true)
    try {
      const res = await api.saveRepairHint(taskId, page, hint, mode)
      setHints(res.hints ?? {})
      toast('本页的人工修改想法已保存，会随下一次修复交给模型。', 'success')
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setHintBusy(false)
    }
  }

  const copyPrompt = async () => {
    if (!brief?.prompt) return
    try {
      await navigator.clipboard.writeText(brief.prompt)
      toast('本页修改 prompt 已复制。', 'success')
    } catch {
      toast('浏览器拒绝了剪贴板访问，请手动选中文本复制。', 'error')
    }
  }

  if (error) {
    return (
      <div className="card" style={{ padding: 24 }}>
        <div className="row" style={{ fontWeight: 700, color: 'var(--orange)' }}><Icon name="alert" size={18} /> 结果不可用</div>
        <p className="small muted" style={{ marginTop: 6 }}>{error}</p>
        <button className="btn btn-sm" style={{ marginTop: 12 }} onClick={() => navigate(`/repair/${taskId}`)}>返回修复页</button>
      </div>
    )
  }

  if (!record || !task) {
    return <div className="stack"><Skeleton height={110} /><Skeleton height={420} /></div>
  }

  if (record.status !== 'completed') {
    return (
      <div className="card" style={{ padding: 24 }}>
        <div className="row" style={{ fontWeight: 700 }}><Icon name="spinner" size={18} /> 这次修复还没有结束</div>
        <p className="small muted" style={{ marginTop: 6 }}>{record.message}</p>
        <button className="btn btn-sm" style={{ marginTop: 12 }}
          onClick={() => navigate(`/repair/${taskId}/progress?rid=${record.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)}>
          查看进度
        </button>
      </div>
    )
  }

  const verify = record.verify
  const applied = record.summary.applied
  const manual = record.summary.manual
  const partial = record.summary.partial ?? 0
  const overlapPages = verify?.overlaps?.pages ?? []
  const stalledPages = (record.pages ?? []).filter((item) => item.ops > 0 && item.changed === false)

  return (
    <div className="fade-up">
      <div className="repair-hero">
        <div className="repair-hero-main">
          <button className="btn btn-ghost btn-sm" style={{ marginLeft: -8, marginBottom: 6 }} onClick={() => navigate('/repair')}>
            <Icon name="left" size={15} /> AI 修复
          </button>
          <h1>修复结果 · {task.name}</h1>
          <div className="lead">
            生成于 {formatDate(record.finished_at ?? record.created_at)} · 方案来源
            {record.plan_source === 'llm' ? ` ${record.model || '模型'}` : ' 规则计划'}
            {record.review?.enabled ? ` · 复核 ${Math.max(1, record.review.rounds.length)} 轮` : ''}
            {' · '}文件 {record.output ? (record.output.size / 1024 / 1024).toFixed(2) : '—'} MB
            {record.summary.visible_pages != null
              ? ` · 画面可见变化 ${record.summary.visible_pages} 页`
              : ''}
          </div>
        </div>
        <div className="repair-hero-act">
          <button className="btn btn-ghost btn-sm" onClick={() => navigate(`/repair/${taskId}`)}>修复页</button>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate(`/tasks/${taskId}/report${mode === 'demo' ? '?mode=demo' : ''}`)}>
            <Icon name="file" size={15} /> 审查报告
          </button>
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void again()}>
            <Icon name="refresh" size={15} /> 重新修复
          </button>
          <a className="btn btn-primary" href={api.repairFileUrl(taskId, record.repair_id, mode)}>
            <Icon name="download" size={16} /> 下载修复文件
          </a>
        </div>
      </div>

      <div className="repair-kpis">
        <div className="repair-kpi"><b>{applied}</b><span>条已修复（画面已变）</span></div>
        <div className="repair-kpi" title={partial > 0 ? `其中 ${partial} 条已写入文件但画面没有变化，不计入已修复` : undefined}>
          <b>{manual + partial}</b><span>条待人工{partial > 0 ? ' / 未生效' : ''}</span>
        </div>
        <div className="repair-kpi"><b>{verify ? `${verify.before_structure} → ${verify.after_structure}` : '—'}</b><span>结构问题复检</span></div>
        <div className="repair-kpi"><b>{verify?.introduced ?? 0}</b><span>条新增问题</span></div>
      </div>

      {stalledPages.length > 0 && (
        <div className="repair-overlap is-stalled">
          <Icon name="alert" size={16} />
          <div>
            <b>{stalledPages.length} 页有缺陷但画面上没有可见改动</b>
            <p>
              判定标准是修复前/后两张渲染图逐像素比对——只要画面没变，就不算修好，哪怕规则层面已经清掉。
              这些页的条目要么定位不到可安全修改的对象（对象在组合里、字号继承自母版），
              要么改动只动了图层顺序（规则按文本框面积判遮挡，文字实际没被挡住），
              要么本身只剩「必须改文案、换图或重画图标」的问题。原因逐条写在每页的动作明细里：
              需要改文案的请填写人工修改想法后重新修复，或导出任务包交给外部模型。
              涉及页：{stalledPages.map((item) => ` P${String(item.index).padStart(2, '0')}`).join('、')}。
            </p>
          </div>
        </div>
      )}

      {overlapPages.length > 0 && (
        <div className="repair-overlap">
          <Icon name="alert" size={16} />
          <div>
            <b>还有 {overlapPages.length} 页存在可见重叠</b>
            <p>
              自动修复只处理越界、遮挡、字号、拥挤这四类可证明的几何问题；
              文字块互相压住、或文字仍被色块盖住的页需要重排版式，系统只标注、不改写。以下页在结果里标为「部分修复」：
              {overlapPages.map((item) => ` P${String(item.index).padStart(2, '0')}（${item.count} 处）`).join('、')}。
            </p>
          </div>
        </div>
      )}

      <div className="card repair-viewer">
        <div className="repair-viewer-head">
          <div className="repair-tabs">
            {record.pages.filter((item) => item.ops > 0).map((item) => (
              <button key={item.index}
                className={`${item.index === page ? 'on' : ''}${item.overlaps ? ' overlaps' : ''}${item.changed === false ? ' is-ghost' : ''}`.trim()}
                title={`第 ${item.index} 页：${item.ops} 条待修，已修复 ${item.applied} 条，${item.manual} 条需人工`
                  + (item.overlaps ? `；修复后仍有 ${item.overlaps} 处可见重叠` : '')
                  + (item.changed === false ? '；画面与修复前一致，未产生可见改动' : '')}
                onClick={() => setPage(item.index)}>
                P{String(item.index).padStart(2, '0')}
                {item.manual > 0 && <em className="manual-dot" title={`本页还有 ${item.manual} 条需要人工处理`} />}
                <i className={item.applied ? '' : 'zero'}>{item.applied || '人工'}</i>
              </button>
            ))}
            {record.pages.every((item) => item.ops === 0) && <span className="tiny muted">这份文稿没有需要修改的页</span>}
          </div>
          <div className="spacer" />
          <div className="repair-seg">
            {(['after', 'before', 'annot', 'split'] as const).map((key) => (
              <button key={key} className={view === key ? 'on' : ''} onClick={() => setView(key)}>
                {key === 'after' ? '修复后' : key === 'before' ? '修复前' : key === 'annot' ? '缺陷标注' : '对比'}
              </button>
            ))}
          </div>
        </div>

        <div className={`repair-stage${view === 'split' ? ' is-split' : ''}`}>
          {view === 'split' ? (
            <div className="repair-split">
              <figure>
                <figcaption>修复前</figcaption>
                <img src={current?.before_url} alt="修复前页面" />
              </figure>
              <figure>
                <figcaption>修复后</figcaption>
                {current?.after_url
                  ? <img src={current.after_url} alt="修复后页面" />
                  : <div className="repair-missing">该页没有生成修复后的渲染图</div>}
              </figure>
            </div>
          ) : (
            <figure className="repair-single">
              <figcaption>
                {view === 'after' ? '修复后' : view === 'annot' ? '修复前 · 缺陷标注' : '修复前'}
                {current ? ` · P${String(current.index).padStart(2, '0')}` : ''}
                {current?.visual && !current.visual.visible
                  ? <em className="repair-flag warn">画面无可见变化</em>
                  : current?.changed === false && <em className="repair-flag">本页未产生改动</em>}
                {current?.visual?.visible && <em className="repair-flag ok">画面已改变</em>}
                {(current?.tidy ?? 0) > 0 && <em className="repair-flag ok">整理 {current?.tidy} 处</em>}
                {current?.visual_note && <em className="repair-visual">{current.visual_note}</em>}
              </figcaption>
              {view === 'after' ? (
                current?.after_url
                  ? <img src={current.after_url} alt={`第 ${current.index} 页修复后`} />
                  : <div className="repair-missing">该页没有生成修复后的渲染图</div>
              ) : view === 'annot' ? (
                current?.annot_url
                  ? <img src={current.annot_url} alt={`第 ${current.index} 页缺陷标注`} />
                  : <div className="repair-missing">该页没有生成标注图</div>
              ) : (
                current && <img src={current.before_url} alt={`第 ${current.index} 页修复前`} />
              )}
            </figure>
          )}

          <aside className="repair-ops">
            <div className="repair-ops-head">
              <b>本页动作</b>
              <span>
                {current
                  ? `${current.ops} 条缺陷 · 已修 ${current.applied}`
                    + ((current.partial ?? 0) > 0 ? ` · 部分 ${current.partial}` : '')
                    + ` · 人工 ${current.manual}`
                    + (current.before + current.after > 0
                      ? ` · 结构问题 ${current.before} → ${current.after}`
                      : ' · 无结构类问题')
                    + ((current.tidy ?? 0) > 0 ? ` · 顺带整理 ${current.tidy} 处` : '')
                    + (current.visual
                      ? current.visual.visible ? '' : ' · 画面无可见变化'
                      : current.changed === false ? ' · 未改动' : '')
                  : ''}
              </span>
            </div>
            {(current?.overlaps ?? 0) > 0 && (
              <p className="repair-ops-warn">
                这一页修复后仍有 {current?.overlaps} 处可见重叠（文字互相压住或文字被色块盖住），自动改写不重排版式，需要人工挪开。
              </p>
            )}
            {current?.visual && !current.visual.visible && (
              <p className="repair-ops-warn">
                这一页的条目在规则层面已处理，但修复前/后渲染图逐像素比对几乎完全一致（差异
                {' '}{(current.visual.ratio * 100).toFixed(2)}%、{current.visual.px} 像素）。说明被判成「遮挡」的文字在画面上本来就没被挡住——
                规则按文本框面积判定，而文字通常只占框内一小块。画面不需要改；如果仍想调整，请写人工想法或导出本页 prompt 交给外部模型。
              </p>
            )}
            {pageOps.length === 0 && <p className="tiny muted">这一页没有检测到需要修改的条目。</p>}
            {pageOps.map((op) => (
              <article className="repair-op" key={op.issue_id}>
                <header>
                  <span className={`repair-dot ${OP_STATUS[op.status]?.tone ?? 'idle'}`} />
                  <b>{DEFECT_LABEL[op.defect_type] || op.defect_type}</b>
                  <em>{ACTION_TEXT[op.action] ?? op.action}</em>
                </header>
                <p>{op.detail}</p>
                {op.note && <p className="note">AI 说明：{op.note}</p>}
                {op.round && op.round > 1 && <p className="tiny muted">第 {op.round} 轮复核时追加</p>}
              </article>
            ))}

            <div className="repair-hint">
              <div className="repair-hint-head">
                <b>人工修改想法</b>
                <span>这一页系统改不了的部分，写清你想怎么改；保存后会交给下一次修复，也会写进任务包 prompt。</span>
              </div>
              <textarea
                value={hint}
                maxLength={600}
                rows={3}
                placeholder="例如：表格太挤，允许缩小字号，但保留三列结构；左下角照片移到说明文字右侧"
                onChange={(event) => setHint(event.target.value)}
              />
              <div className="repair-hint-act">
                <span className="tiny muted">{hint.length}/600</span>
                <button className="btn btn-sm" disabled={hintBusy} onClick={() => void saveHint()}>
                  {hintBusy ? '保存中…' : '保存想法'}
                </button>
              </div>
            </div>
          </aside>
        </div>
      </div>

      <section className="card repair-table-card">
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
          <div className="h3">全部修改记录</div>
          <span className="tiny muted">{record.ops.length} 条 · 已修复 {applied} · 需人工 {manual}</span>
        </div>
        <div className="repair-table">
          <div className="repair-tr head">
            <span>页</span><span>类型</span><span>动作</span><span>结果</span><span>说明</span>
          </div>
          {record.ops.map((op: RepairOp) => (
            <div className="repair-tr" key={op.issue_id}>
              <span className="mono">P{String(op.slide_index).padStart(2, '0')}</span>
              <span>
                {DEFECT_LABEL[op.defect_type] || op.defect_type}
                {op.round && op.round > 1 ? <em className="repair-round-tag">R{op.round}</em> : null}
              </span>
              <span>{ACTION_TEXT[op.action] ?? op.action}</span>
              <span><em className={`repair-chip ${OP_STATUS[op.status]?.tone ?? 'idle'}`}>{OP_STATUS[op.status]?.text ?? op.status}</em></span>
              <span className="detail">{op.detail}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="card repair-bundle">
        <div className="repair-bundle-main">
          <div className="repair-bundle-kicker">FOR EXTERNAL LLM</div>
          <b>导出修改任务包</b>
          <p>
            包含原始 PPT、每页缺陷标注图（红框编号对应清单）和逐页修改 prompt。
            能自动修的页可以直接把 prompt + 标注图交给支持看图的模型继续改；人工页也能照着改。
          </p>
        </div>
        <div className="repair-bundle-act">
          <a className="btn btn-sm" href={api.repairBundleUrl(taskId, record.repair_id, mode)}>
            <Icon name="download" size={15} /> 下载任务包（zip）
          </a>
          <button className="btn btn-ghost btn-sm" disabled={briefBusy || !brief} onClick={() => void copyPrompt()}>
            <Icon name="file" size={15} /> {briefBusy ? '生成 prompt…' : '复制本页 prompt'}
          </button>
        </div>
      </section>

      <div className="repair-notes">
        <div>
          <b>复检口径</b>
          <p>
            修复后的文件用同一套结构规则重新解析与判定，只比较结构类问题（越界、遮挡、字号、拥挤）。
            {verify ? ` 本次 ${verify.before_structure} → ${verify.after_structure} 条，新增 ${verify.introduced} 条。` : ''}
            {(record.summary.rule_resolved ?? 0) > record.summary.applied
              ? `其中 ${(record.summary.rule_resolved ?? 0) - record.summary.applied} 条只是把图层顺序或几何属性改了一遍、画面并没有变（规则按文本框面积判遮挡产生的误报），保守起见不算作已修复。`
              : ''}
            视觉检测器给出的拥挤、留白与图标语义问题不参与自动改写，仍需人工处理。
            {verify?.overlaps?.count
              ? ` 另外复扫出 ${verify.overlaps.count} 处可见重叠（文字互相压住，或文字仍被色块盖住），这类需要重排版式，系统只标注不自动改。`
              : ' 复扫未发现文字互压或残留遮挡。'}
            {verify?.visual
              ? ` 渲染图逐像素比对：${verify.visual.checked} 页里 ${verify.visual.visible_pages} 页画面确实变了`
                + (verify.visual.identical.length
                  ? `，${verify.visual.identical.map((index) => `P${String(index).padStart(2, '0')}`).join('、')} 画面与修复前一致（不计入已修复）。`
                  : '。')
              : ''}
          </p>
        </div>
        <div>
          <b>文件说明</b>
          <p>
            下载的是副本 <b>{record.output?.name}</b>，原始 PPT 与审查报告都保持原样。
            {record.render?.ok === false ? ' 本次渲染失败，预览图缺失，但修复文件可以正常下载。' : ''}
            {record.degraded ? ` ${record.degraded}` : ''}
          </p>
        </div>
      </div>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { Claim, Issue, ReviewPlanItem, SlideDetail, SlideSummary, TaskDetail } from '../lib/types'
import { DEFECTS, DEFECT_COLOR, DEFECT_LABEL, DETECTION_STATUS_LABEL } from '../lib/types'
import { Icon } from '../components/Icon'
import { EmptyState, Skeleton, StatusChip } from '../components/Ui'
import { toast } from '../components/Toasts'
import { clamp } from '../lib/format'
import { displayEvidence, isDismissed, isVisible, markerBox } from '../lib/review'
import { ClipIssueEvidence, ClipResultPanel, InteractiveReview, PlanDetails, RelatedSlides, ReviewFlowNotice, ScopePanel } from '../components/ReviewDetails'
import { RecoveryActions, RecoveryLineageNotice } from '../components/RecoveryActions'

const SOURCE_LABEL: Record<string, string> = {
  structure_rule: '结构规则',
  visual_model: '视觉模型',
  mllm_review: 'AI 复核',
  six_detector: '专用检测',
  six_detectors: '专用检测',
}
const SOURCE_CLASS: Record<string, string> = {
  structure_rule: 'rule', visual_model: 'model', mllm_review: 'ai',
}
const REVIEW_BADGE: Record<string, string> = {
  supported: 'AI 支持', rejected: 'AI 不成立', uncertain: 'AI 不确定',
}

export default function Results() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const requestedPage = Number(params.get('page'))
  const navigate = useNavigate()

  const [task, setTask] = useState<TaskDetail | null>(null)
  const [slides, setSlides] = useState<SlideSummary[]>([])
  const [current, setCurrent] = useState(1)
  const [detail, setDetail] = useState<SlideDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<string | null>(null)
  const [onlyIssues, setOnlyIssues] = useState(false)
  const [markers, setMarkers] = useState(true)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const [selectedIssue, setSelectedIssue] = useState<string | null>(null)
  const [busyIssue, setBusyIssue] = useState<string | null>(null)
  const [claims, setClaims] = useState<Claim[]>([])
  const [claimsAvailable, setClaimsAvailable] = useState(false)
  const [planItems, setPlanItems] = useState<ReviewPlanItem[]>([])
  const dragRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const pendingResults = !!task && (['waiting', 'executing', 'invalid'].includes(task.review_gate?.state ?? '') || ['draft', 'preparing', 'ready', 'queued', 'running'].includes(task.status))

  useEffect(() => {
    if (pendingResults) navigate(`/tasks/${taskId}/progress${mode === 'demo' ? '?mode=demo' : ''}`, { replace: true })
  }, [pendingResults, navigate, taskId, mode])

  useEffect(() => {
    let alive = true
    Promise.all([api.getTask(taskId, mode), api.slides(taskId, mode)])
      .then(([t, s]) => {
        if (!alive) return
        setTask(t)
        setSlides(s.slides)
        const first = Number.isInteger(requestedPage) && s.slides.some((slide) => slide.index === requestedPage) ? requestedPage : s.slides.find((x) => x.issue_count > 0)?.index ?? 1
        setCurrent(first)
      })
      .catch((err) => toast(err.message, 'error'))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [taskId, mode, requestedPage])

  useEffect(() => {
    let alive = true
    setClaims([]); setClaimsAvailable(false); setPlanItems([])
    api.claims(taskId, mode).then((result) => { if (alive) { setClaims(Array.isArray(result.claims) ? result.claims : []); setClaimsAvailable(true) } }).catch(() => undefined)
    api.reviewPlan(taskId, mode).then((result) => { if (alive) setPlanItems(Array.isArray(result.items) ? result.items : []) }).catch(() => undefined)
    return () => { alive = false }
  }, [taskId, mode])

  useEffect(() => {
    let alive = true
    setDetail(null)
    api.slide(taskId, current, mode)
      .then((d) => alive && setDetail(d))
      .catch(() => alive && setDetail(null))
    return () => { alive = false }
  }, [taskId, current, mode])

  const stageSlide = useCallback((delta: number) => {
    setCurrent((prev) => clamp(prev + delta, 1, Math.max(1, slides.length)))
    setSelectedIssue(null)
    setZoom(1)
    setPan({ x: 0, y: 0 })
  }, [slides.length])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      if (event.key === 'ArrowRight') stageSlide(1)
      if (event.key === 'ArrowLeft') stageSlide(-1)
      if (event.key === '+' || event.key === '=') setZoom((z) => clamp(z + .25, 1, 4))
      if (event.key === '-') setZoom((z) => clamp(z - .25, 1, 4))
      if (event.key === '0') { setZoom(1); setPan({ x: 0, y: 0 }) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [stageSlide])

  const doc = task?.document
  const issues = useMemo(() => {
    const list = detail?.issues ?? []
    return filter ? list.filter((i) => i.defect_type === filter) : list
  }, [detail, filter])

  const counts = useMemo(() => {
    const global = task?.summary?.by_type
    if (global) return global as Record<string, number>
    const base: Record<string, number> = {}
    for (const issue of detail?.issues ?? []) {
      if (issue.review_status === 'rejected') continue
      base[issue.defect_type] = (base[issue.defect_type] || 0) + 1
    }
    return base
  }, [task, detail])

  const visibleSlides = onlyIssues ? slides.filter((s) => s.issue_count > 0) : slides
  const currentSlide = slides.find((s) => s.index === current)
  const aspect = doc ? doc.slide_width_in / doc.slide_height_in : 16 / 9
  const currentClaims = detail?.claims ?? claims.filter((claim) => claim.slide_index === current)
  const pagePlans = planItems.filter((item) => item.slide_index === current || item.related_slides?.includes(current))
  const pageHasReviewRecord = currentSlide?.ai_reviewed || pagePlans.some((item) => item.outcome === 'reviewed')

  const goPage = (index: number) => { setCurrent(index); setSelectedIssue(null); setZoom(1); setPan({ x: 0, y: 0 }) }
  const issueUpdated = (issue: Issue) => {
    setDetail((previous) => previous ? { ...previous, issues: previous.issues.map((existing) => existing.issue_id === issue.issue_id ? issue : existing) } : previous)
    api.getTask(taskId, mode).then(setTask).catch(() => undefined)
    api.slides(taskId, mode).then((result) => setSlides(result.slides)).catch(() => undefined)
    api.reviewPlan(taskId, mode).then((result) => setPlanItems(result.items ?? [])).catch(() => undefined)
  }

  // 人工复核：确认是系统误判后排除这条，报告、建议与导出 Prompt 同步去掉它。
  // 勾选状态先本地生效（乐观更新），接口失败再回滚，避免点击后有明显延迟。
  const setVerdict = async (issue: Issue, dismissed: boolean) => {
    const previous = issue
    const optimistic: Issue = { ...issue, user_verdict: dismissed ? 'dismissed' : undefined }
    setBusyIssue(issue.issue_id)
    setDetail((current) => current
      ? { ...current, issues: current.issues.map((item) => item.issue_id === issue.issue_id ? optimistic : item) }
      : current)
    try {
      const result = await api.setVerdict(taskId, issue.issue_id, dismissed ? 'dismissed' : 'confirmed', '', mode)
      issueUpdated(result.issue)
      toast(dismissed ? '已排除这条缺陷，报告与建议同步更新' : '已恢复这条缺陷', dismissed ? 'success' : 'info')
    } catch (err) {
      setDetail((current) => current
        ? { ...current, issues: current.issues.map((item) => item.issue_id === issue.issue_id ? previous : item) }
        : current)
      toast((err as Error).message, 'error')
    } finally {
      setBusyIssue(null)
    }
  }

  const onPointerDown = (e: React.PointerEvent) => {
    if (zoom <= 1) return
    dragRef.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }
    setDragging(true)
    ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragRef.current) return
    const dx = e.clientX - dragRef.current.x
    const dy = e.clientY - dragRef.current.y
    setPan({ x: dragRef.current.px + dx, y: dragRef.current.py + dy })
  }
  const endDrag = () => {
    dragRef.current = null
    setDragging(false)
  }

  if (pendingResults) return <div className="card" role="status" style={{ padding: 24 }}>本次审查流程尚未结束，正在返回进度页。等待复检或跳过请求后的结果整理完成后，才能查看输出。</div>

  return (
    <div className="fade-up">
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div>
          <button className="btn btn-ghost btn-sm" style={{ marginBottom: 6, marginLeft: -8 }} onClick={() => navigate('/')}>
            <Icon name="left" size={15} /> 工作台
          </button>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <h1 style={{ fontSize: 23 }}>{task?.name || '逐页审查结果'}</h1>
            {task && <StatusChip status={task.status} />}
            {mode === 'demo' && <span className="chip demo">演示数据</span>}
          </div>
          <div className="lead">{doc?.file_name} · {doc?.slide_count ?? '—'} 页 · {doc?.aspect_label ?? '—'}</div>
        </div>
        <div className="row">
          {task?.summary && (
            <div className="row small" style={{ gap: 14, marginRight: 6 }}>
              <span className="muted">问题 <b style={{ color: 'var(--ink)' }}>{task.summary.visible_issue_count}</b></span>
              <span className="muted">涉及 <b style={{ color: 'var(--ink)' }}>{task.summary.pages_with_issues}</b> 页</span>
            </div>
          )}
          <button className="btn btn-sm" onClick={() => navigate(`/tasks/${taskId}/report${mode === 'demo' ? '?mode=demo' : ''}`)}>
            <Icon name="chart" size={15} /> 报告
          </button>
        </div>
      </div>

      <RecoveryLineageNotice lineage={task?.lineage} mode={mode} />
      {task && <div className="card" style={{ padding: '0 16px', marginBottom: 12 }}>
        <RecoveryActions taskId={taskId} status={task.status} mode={mode} info={task.recovery} />
      </div>}

      <div className="results-bar">
        <div className="filter-group">
          <button className="filter-chip" aria-pressed={filter === null} onClick={() => setFilter(null)}>
            全部 <span className="n">{task?.summary?.visible_issue_count ?? detail?.issues.filter((i) => i.review_status !== 'rejected').length ?? 0}</span>
          </button>
          {DEFECTS.map((d) => (
            <button key={d.key} className="filter-chip" aria-pressed={filter === d.key} onClick={() => setFilter(filter === d.key ? null : d.key)}>
              <i className="swatch" style={{ background: d.color }} />
              {d.label} <span className="n">{counts[d.key] || 0}</span>
            </button>
          ))}
        </div>
        <div className="spacer" />
        {(task?.summary?.dismissed_count ?? 0) > 0 && (
          <span className="filter-chip static" title="人工核对渲染图后排除的误判，不计入报告与建议">
            <Icon name="check" size={13} /> 已排除 {(task?.summary?.dismissed_count ?? 0)}
          </span>
        )}
        <button className="filter-chip" aria-pressed={onlyIssues} onClick={() => setOnlyIssues((v) => !v)}>
          <Icon name="alert" size={13} /> 只看有问题
        </button>
        <button className="filter-chip" aria-pressed={markers} onClick={() => setMarkers((v) => !v)}>
          <Icon name={markers ? 'eye' : 'eyeOff'} size={13} /> 检测标记
        </button>
      </div>

      {loading ? (
        <div className="card" style={{ padding: 20 }}><Skeleton height={420} /></div>
      ) : slides.length === 0 ? (
        <div className="card"><EmptyState icon="image" title="没有可展示的页面" hint="该任务可能未完成解析或渲染。" /></div>
      ) : (
        <div className="results-grid">
          <div className="card slide-nav">
            {visibleSlides.length === 0 && <div className="empty small">没有命中筛选的页面</div>}
            {visibleSlides.map((slide) => (
              <button
                key={slide.index}
                className={`slide-nav-item${slide.index === current ? ' active' : ''}`}
                onClick={() => { setCurrent(slide.index); setSelectedIssue(null); setZoom(1); setPan({ x: 0, y: 0 }) }}
              >
                <div className="pic">
                  {slide.thumb_url ? <img src={slide.thumb_url} alt={`第 ${slide.index} 页缩略图`} loading="lazy" /> : null}
                  {slide.issue_count > 0 && <span className="badge-count">{slide.issue_count}</span>}
                </div>
                <div style={{ minWidth: 0 }}>
                  <div className="num">P{String(slide.index).padStart(2, '0')}</div>
                  <div className="flag">{slide.content_mode === 'image_only' ? '整页位图' : slide.render_status === 'rendered' ? '已渲染' : '渲染失败'}</div>
                  <div className="issues">
                    {slide.issue_types.filter((t) => !filter || t === filter).map((t) => (
                      <i key={t} style={{ background: DEFECT_COLOR[t] }} title={DEFECT_LABEL[t]} />
                    ))}
                  </div>
                </div>
              </button>
            ))}
          </div>

          <div className="card viewer-panel">
            <div className="viewer-head">
              <span className="title">第 {current} 页</span>
              <span className="chip gray">{currentSlide?.content_mode === 'image_only' ? '整页位图' : currentSlide?.content_mode === 'mixed' ? '混合页' : '可编辑页'}</span>
              <div className="spacer" />
              <button className="icon-btn" aria-label="上一页" onClick={() => stageSlide(-1)} disabled={current <= 1}><Icon name="left" size={16} /></button>
              <button className="icon-btn" aria-label="下一页" onClick={() => stageSlide(1)} disabled={current >= slides.length}><Icon name="right" size={16} /></button>
            </div>

            <div
              ref={stageRef}
              className={`viewer-stage${dragging ? ' dragging' : ''}`}
              style={{ aspectRatio: String(aspect) }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerLeave={endDrag}
            >
              {currentSlide?.render_status === 'rendered' && currentSlide.image_url ? (
                <div
                  className="viewer-canvas"
                  style={{
                    width: `min(100%, calc((100vh - 300px) * ${aspect}))`,
                    transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
                  }}
                >
                  <img src={currentSlide.image_url} alt={`第 ${current} 页渲染图`} draggable={false} />
                  {markers && issues.filter(isVisible).map((issue, idx) => {
                    const b = markerBox(issue, doc?.slide_width_in, doc?.slide_height_in)
                    if (!b) return null
                    return (
                      <div
                        key={issue.issue_id}
                        className={`marker${issue.severity === 'high' ? ' high' : ''}${selectedIssue === issue.issue_id ? ' sel' : ''}`}
                        data-n={idx + 1}
                        title={`${DEFECT_LABEL[issue.defect_type]}：${issue.evidence_summary}`}
                        style={{
                          left: `${b.x * 100}%`,
                          top: `${b.y * 100}%`,
                          width: `${b.w * 100}%`,
                          height: `${b.h * 100}%`,
                        }}
                        onClick={(e) => { e.stopPropagation(); setSelectedIssue(issue.issue_id) }}
                      />
                    )
                  })}
                </div>
              ) : (
                <div style={{ color: '#8fa0bd', textAlign: 'center', padding: 30 }}>
                  <Icon name="image" size={30} />
                  <div style={{ marginTop: 8, fontSize: 13 }}>该页渲染图不可用</div>
                  <div style={{ fontSize: 11, marginTop: 4 }}>{currentSlide?.render_error || 'PowerPoint 渲染失败，仍可查看结构候选。'}</div>
                </div>
              )}
            </div>

            <div className="viewer-foot">
              <button className="icon-btn" aria-label="缩小" onClick={() => setZoom((z) => clamp(z - .25, 1, 4))}><Icon name="zoomOut" size={16} /></button>
              <span className="zoom-val">{Math.round(zoom * 100)}%</span>
              <button className="icon-btn" aria-label="放大" onClick={() => setZoom((z) => clamp(z + .25, 1, 4))}><Icon name="zoomIn" size={16} /></button>
              <button className="btn btn-ghost btn-sm" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }) }}>
                <Icon name="fit" size={14} /> 适配
              </button>
              <div className="spacer" />
              <span>{currentSlide?.width ? `${currentSlide.width} × ${currentSlide.height}px` : '—'}</span>
              <span>·</span>
              <span>{doc?.aspect_label}</span>
            </div>
          </div>

          <div className="card detail-panel">
            <div className="detail-head">
              <div className="idx">PAGE {String(current).padStart(2, '0')} / {slides.length}</div>
              <div className="h">
                {issues.filter(isVisible).length > 0
                  ? `${issues.filter(isVisible).length} 条问题证据`
                  : issues.some(isDismissed) ? '本页缺陷已全部排除' : '本页无问题候选'}
              </div>
              <div className="small muted" style={{ marginTop: 3 }}>
                {currentSlide?.text_chars ?? 0} 字符 · {currentSlide?.textboxes ?? 0} 文本框 · {currentSlide?.pictures ?? 0} 图片
              </div>
            </div>

            <div className="module-strip">
              {task?.stages.find((s) => s.key === 'structure')?.status === 'done'
                ? <span className="chip green"><i className="dot" />结构已运行</span>
                : <span className="chip gray"><i className="dot" />结构未完成</span>}
              {detail?.visual_result?.status === 'ok'
                ? <span className="chip cyan"><i className="dot" />CLIP 有辅助证据</span>
                : <span className="chip gray"><i className="dot" />{task?.options.enable_visual ? '本页 CLIP 未取得有效输出' : 'CLIP 关闭'}</span>}
              {pageHasReviewRecord
                ? <span className="chip orange"><i className="dot" />{task?.options.llm_backend === 'mock' ? 'Mock 模拟复核' : 'LLM 有复核记录'}</span>
                : <span className="chip gray"><i className="dot" />本页未完成 LLM 复检</span>}
            </div>

            <ReviewFlowNotice options={task?.options} stages={task?.stages} planItems={pagePlans} gate={task?.review_gate} />
            <ScopePanel claims={currentClaims} available={claimsAvailable || !!detail?.claims} />
            <div style={{ padding: '0 14px' }}><ClipResultPanel result={detail?.visual_result} enabled={task?.options.enable_visual} /></div>
            <div style={{ padding: '0 14px' }}><PlanDetails items={pagePlans} /></div>

            {!detail ? (
              <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <Skeleton height={90} /><Skeleton height={90} />
              </div>
            ) : issues.length === 0 ? (
              <div className="issue-list">
                <EmptyState icon="file" title="没有命中筛选的问题" hint={filter ? '切换缺陷类型或查看全部。' : '未发现候选不代表视觉质量合格，请结合六类检查范围与未决项判断。'} />
              </div>
            ) : (
              <div className="issue-list">
                {issues.map((issue) => (
                  <IssueCard
                    key={issue.issue_id}
                    issue={issue}
                    selected={selectedIssue === issue.issue_id}
                    busy={busyIssue === issue.issue_id}
                    onSelect={() => setSelectedIssue(issue.issue_id)}
                    onVerdict={(dismissed) => void setVerdict(issue, dismissed)}
                    onPage={goPage}
                    pageCount={slides.length}
                    review={selectedIssue === issue.issue_id && isVisible(issue) ? <InteractiveReview taskId={taskId} issue={issue} mode={mode} backend={task?.options.llm_backend ?? 'api'} onUpdated={issueUpdated} /> : undefined}
                  />
                ))}
              </div>
            )}

            {detail && (
              <div style={{ padding: '0 14px 16px' }}>
                <div className="small muted" style={{ lineHeight: 1.8 }}>
                  {detail.issues_confirmed > 0 && <div>{task?.options.llm_backend === 'mock' ? '模拟复核' : 'AI 复核'}支持 {detail.issues_confirmed} 条</div>}
                  {detail.issues_rejected > 0 && <div>{task?.options.llm_backend === 'mock' ? '模拟复核' : 'AI 复核'}不成立 {detail.issues_rejected} 条</div>}
                  <div>页面来源：{detail.slide?.content_mode === 'image_only' ? '整页位图（对象层无文本）' : 'PowerPoint 原生对象'}</div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="small muted" style={{ marginTop: 16, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <span>← → 切换页面</span><span>·</span><span>+ / − 缩放</span><span>·</span><span>0 复位</span>
        <span>·</span>
        <span>标记框来自结构规则的元素坐标；视觉模型仅提供页面级风险，不虚构定位框。</span>
      </div>
    </div>
  )
}

function IssueCard({ issue, selected, busy, onSelect, onVerdict, onPage, pageCount, review }: { issue: Issue; selected: boolean; busy: boolean; onSelect: () => void; onVerdict: (dismissed: boolean) => void; onPage: (index: number) => void; pageCount: number; review?: React.ReactNode }) {
  const color = DEFECT_COLOR[issue.defect_type]
  const sources = Array.from(new Set((issue.sources ?? []).map((s) => s.source)))
  const dismissed = isDismissed(issue)
  return (
    <div className={`issue-card${selected ? ' selected' : ''}${dismissed ? ' dismissed' : ''}`} onClick={onSelect} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && onSelect()}>
      <div className="issue-top">
        <i style={{ width: 8, height: 8, borderRadius: 2, background: color, display: 'block' }} />
        <span className="t">{issue.title}</span>
        <div className="spacer" />
        {dismissed && <span className="badge ok">人工排除</span>}
        <span className={`badge ${issue.severity === 'high' ? 'high' : issue.severity === 'medium' ? 'mid' : 'low'}`}>
          {issue.severity === 'high' ? '高风险' : issue.severity === 'medium' ? '中风险' : '低风险'}
        </span>
      </div>
      <div className="issue-ev">{issue.evidence_summary}</div>
      {issue.raw_status && <div className="tiny muted" style={{ marginTop: 5 }}>{DETECTION_STATUS_LABEL[issue.raw_status] ?? issue.raw_status}{issue.subtype ? ` · ${issue.subtype}` : ''}</div>}
      <RelatedSlides issue={issue} count={pageCount} onPage={onPage} />
      {issue.bbox && (
        <div className="issue-meta">
          坐标 x={issue.bbox.x.toFixed(2)} y={issue.bbox.y.toFixed(2)} w={issue.bbox.w.toFixed(2)} h={issue.bbox.h.toFixed(2)} in
        </div>
      )}
      {issue.clip_review && <ClipIssueEvidence result={issue.clip_review} defectType={issue.defect_type} />}
      {issue.evidence_conflict ? <div className="tiny" style={{ color: 'var(--orange)', marginTop: 7 }}>R2 与辅助证据存在分歧，原始候选保留，需结合 LLM 复检判断。</div> : null}
      {issue.review_status !== 'unreviewed' && (
        <div className={`badge ${issue.review_status === 'supported' ? 'ai' : issue.review_status === 'rejected' ? 'ok' : 'low'}`} style={{ marginTop: 8 }}>
          {issue.review_result?.mock || issue.review_result?.demo || issue.review_result?.backend === 'mock' ? `Mock 模拟 · ${issue.review_status === 'supported' ? '支持候选' : issue.review_status === 'rejected' ? '不支持候选' : '待核实'}` : REVIEW_BADGE[issue.review_status] || issue.review_status}
        </div>
      )}
      {issue.review_result?.evidence && (
        <div className="issue-ai">
          <b>{issue.review_result.demo || issue.review_result.mock || issue.review_result.backend === 'mock' ? 'Mock 模拟复核（非真实模型判断）：' : 'AI 复核：'}</b>{displayEvidence(issue.review_result.evidence)}
          {issue.review_result.model ? <div style={{ opacity: .7, marginTop: 3 }}>{issue.review_result.model}</div> : null}
        </div>
      )}
      <div className="issue-rec"><b>建议：</b>{issue.recommendation}</div>
      <div className="issue-src">
        {sources.map((s) => (
          <span key={s} className={`badge ${SOURCE_CLASS[s] ?? 'rule'}`}>{SOURCE_LABEL[s] ?? '专用检测'}</span>
        ))}
        {issue.rule_id && <span className="badge low">{issue.rule_id}</span>}
        {issue.visual_probability != null && !issue.clip_review && <span className="badge low">视觉模型分数 {issue.visual_probability.toFixed(2)}</span>}
        {issue.demo && <span className="badge ai">演示</span>}
      </div>
      <div className={`issue-verdict${dismissed ? ' on' : ''}`} onClick={(e) => e.stopPropagation()}>
        <label>
          <input
            type="checkbox"
            checked={dismissed}
            disabled={busy}
            onChange={(event) => onVerdict(event.target.checked)}
          />
          <span>{busy ? '正在更新…' : '系统误判，排除这条'}</span>
        </label>
        <span className="vx-note">
          {dismissed ? '已排除 · 报告、建议与 Prompt 不再包含' : '核对右侧渲染图与证据后勾选'}
        </span>
      </div>
      {selected && !dismissed && <>
        {issue.detector_version && <div className="tiny muted" style={{ marginTop: 8 }}>检测版本：{issue.detector_version}</div>}
        {issue.limitations?.length ? <div className="tiny muted" style={{ marginTop: 6 }}>可判范围限制：{issue.limitations.join('；')}</div> : null}
        {(issue.available_scope ?? issue.source_scope ?? issue.scope) != null && <details className="tiny muted"><summary>本项证据范围</summary>{displayEvidence(issue.available_scope ?? issue.source_scope ?? issue.scope)}</details>}
        {issue.claims?.length ? <details className="tiny muted"><summary>原始检测记录（{issue.claims.length}）</summary>{issue.claims.map((claim, index) => <div key={claim.claim_id ?? index} style={{ margin: '7px 0' }}><div>{DETECTION_STATUS_LABEL[claim.raw_status ?? claim.status ?? 'unknown']} · {claim.detector_version ?? ''} {claim.summary ?? claim.evidence_summary ?? ''}</div>{typeof claim.score === 'number' && Number.isFinite(claim.score) && <div>异常评分 {claim.score.toFixed(3)}（排序参考，非缺陷概率）</div>}{claim.object_ids?.length ? <div>相关对象：{claim.object_ids.join('、')}</div> : null}{claim.evidence?.length ? <div>原始证据：{displayEvidence(claim.evidence)}</div> : null}</div>)}</details> : null}
        {issue.source_conflicts?.length ? <details className="tiny muted"><summary>不同来源证据需合并判断</summary>{displayEvidence(issue.source_conflicts)}</details> : null}
        {review}
      </>}
    </div>
  )
}

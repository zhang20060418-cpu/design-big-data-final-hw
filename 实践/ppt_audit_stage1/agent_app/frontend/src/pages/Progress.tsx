import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { ReviewGate, SlideSummary, TaskDetail } from '../lib/types'
import { Icon } from '../components/Icon'
import { Skeleton, StatusChip } from '../components/Ui'
import { toast } from '../components/Toasts'
import { canSkipReview } from '../lib/reviewOptions'
import { RecoveryActions, RecoveryLineageNotice } from '../components/RecoveryActions'

const STAGE_ICON = {
  pending: 'dot', running: 'refresh', done: 'check',
  skipped: 'left', unsupported: 'info', failed: 'alert',
} as const

const STATE_TEXT = {
  pending: '等待中', running: '运行中', done: '已完成',
  skipped: '已跳过', unsupported: '未接入', failed: '失败',
} as const

export default function Progress() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const navigate = useNavigate()
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [slides, setSlides] = useState<SlideSummary[]>([])
  const [fatal, setFatal] = useState('')
  const [skipAck, setSkipAck] = useState<ReviewGate | null>(null)
  const [skipping, setSkipping] = useState(false)
  const [skipError, setSkipError] = useState('')
  const [clockNow, setClockNow] = useState(() => performance.now())
  const gateObservedAt = useRef(performance.now())
  const skipInFlight = useRef(false)
  const timer = useRef<number>()
  const activeRequestScope = useRef(`${taskId}:${mode}`)
  activeRequestScope.current = `${taskId}:${mode}`
  const gate = skipAck ?? task?.review_gate

  useEffect(() => {
    let stopped = false
    let thumbsLoaded = 0
    setTask(null); setSlides([]); setSkipAck(null); setSkipping(false); setSkipError(''); setFatal('')
    skipInFlight.current = false
    const tick = async () => {
      try {
        const next = await api.getTask(taskId, mode)
        if (stopped) return
        const receivedAt = performance.now()
        gateObservedAt.current = receivedAt
        setClockNow(receivedAt)
        setTask(next)
        if (next.document && next.images_available && Date.now() - thumbsLoaded > 2500) {
          thumbsLoaded = Date.now()
          api.slides(taskId, mode).then((r) => !stopped && setSlides(r.slides)).catch(() => undefined)
        }
        if (next.status !== 'failed' && (['queued', 'running', 'preparing', 'draft'].includes(next.status) || ['waiting', 'executing'].includes(next.review_gate?.state ?? ''))) {
          timer.current = window.setTimeout(tick, next.review_gate?.state === 'waiting' ? 300 : 1100)
        }
      } catch (err) {
        if (!stopped) setFatal((err as Error).message)
      }
    }
    void tick()
    return () => {
      stopped = true
      window.clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, mode])

  useEffect(() => {
    if (gate?.state !== 'waiting' || task?.status === 'failed') return
    const clock = window.setInterval(() => setClockNow(performance.now()), 200)
    return () => window.clearInterval(clock)
  }, [gate?.state, task?.status])

  const gateBlocksResults = ['waiting', 'executing', 'invalid'].includes(gate?.state ?? '')
  const terminal = !!task && task.id === taskId && ['completed', 'completed_with_errors'].includes(task.status)
  const done = terminal && !gateBlocksResults && !!task?.report_ready
  const failed = task?.status === 'failed'
  const pct = Math.round((task?.progress ?? 0) * 100)
  const rendered = slides.filter((s) => s.render_status === 'rendered').length

  const stageState = (key: string) => task?.stages.find((s) => s.key === key)?.status ?? 'pending'
  const remainingMs = gate?.state === 'waiting' && Number.isFinite(gate.remaining_ms) ? Math.max(0, gate.remaining_ms - Math.max(0, clockNow - gateObservedAt.current)) : 0
  const canSkip = canSkipReview(gate, task?.status, remainingMs) && !skipping
  const localWorkPending = !!task?.stages.some(stage => ['validate', 'parse', 'render', 'structure', 'visual'].includes(stage.key) && ['pending', 'running'].includes(stage.status))

  useEffect(() => {
    if (gate?.skipped_by_user && done) navigate(`/tasks/${taskId}/results${mode === 'demo' ? '?mode=demo' : ''}`, { replace: true })
  }, [gate?.skipped_by_user, done, taskId, mode, navigate])

  const skipReview = async () => {
    if (!canSkip || skipInFlight.current) return
    const requestScope = `${taskId}:${mode}`
    skipInFlight.current = true
    setSkipping(true); setSkipError('')
    try {
      const acknowledged = await api.skipReview(taskId, mode)
      if (activeRequestScope.current !== requestScope) return
      setSkipAck(acknowledged)
    } catch (err) {
      if (activeRequestScope.current !== requestScope) return
      setSkipError((err as Error).message)
      skipInFlight.current = false
      const refreshed = await api.getTask(taskId, mode).catch(() => null)
      if (refreshed) {
        const receivedAt = performance.now()
        gateObservedAt.current = receivedAt; setClockNow(receivedAt); setTask(refreshed)
      }
    } finally { if (activeRequestScope.current === requestScope) setSkipping(false) }
  }

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <button className="btn btn-ghost btn-sm" style={{ marginBottom: 8, marginLeft: -8 }}
            onClick={() => navigate(done ? `/tasks/${taskId}/results${mode === 'demo' ? '?mode=demo' : ''}` : '/')}>
            <Icon name="left" size={15} /> {done ? '查看结果' : '返回工作台'}
          </button>
          <div className="row" style={{ gap: 10 }}>
            <h1>{done ? '审查完成' : failed ? '审查中断' : gate?.state === 'waiting' ? '首轮完成，等待 LLM 复检' : gate?.state === 'executing' ? 'LLM 复检进行中' : terminal ? '审查结束，正在确认结果' : '智能审查进行中'}</h1>
            {task && <StatusChip status={task.status} />}
            {mode === 'demo' && <span className="chip demo">演示数据</span>}
          </div>
          <div className="lead">{task?.source_name || '正在加载任务…'}</div>
        </div>
        {task?.document && (
          <div className="row small muted">
            <span>{task.document.slide_count} 页</span>
            <span>·</span>
            <span>{task.document.aspect_label}</span>
            {rendered > 0 && <><span>·</span><span>{rendered} 页已渲染</span></>}
          </div>
        )}
      </div>

      <RecoveryLineageNotice lineage={task?.lineage} mode={mode} />
      {fatal && <div className="card" style={{ padding: 18, color: 'var(--red)' }}>{fatal}</div>}

      {gate?.state === 'waiting' && !failed && <section className="card" aria-label="LLM复检等待窗口" style={{ marginBottom: 16, padding: 18, borderColor: '#ffe0ad', background: '#fffaf1' }}>
        <div className="row" style={{ flexWrap: 'wrap', justifyContent: 'space-between' }}>
          <div><div className="h3">首轮筛查已完成</div><p className="small" aria-live="polite" style={{ marginTop: 4 }}>{remainingMs > 0 ? `${Math.ceil(remainingMs / 1000)} 秒后自动开始 LLM 复检` : '等待窗口已结束，正在进入 LLM 复检'}</p></div>
          <span className="small muted">可在下方「多模态按需复核」阶段旁跳过</span>
        </div>
        <p className="small" style={{ color: '#885616', marginTop: 10 }}>{gate.warning || '跳过 LLM 复检可能导致误诊或漏检；未经复检的首轮候选不等于最终确认。'}</p>
        {skipError && <p className="small" role="alert" style={{ color: 'var(--red)', marginTop: 8 }}>{skipError}</p>}
      </section>}
      {gate?.state === 'executing' && <div className="card" role="status" style={{ padding: 16, marginBottom: 16 }}>{failed ? '复检未正常结束，任务已中断。不会自动重新调用，请查看错误记录。' : 'LLM 复检已经开始，无法再跳过。完成本次复检及结果整理后才能查看最终输出。'}</div>}
      {gate?.skipped_by_user && <div className="card" role="status" style={{ padding: 16, marginBottom: 16, borderColor: '#ffe0ad' }}><b>用户已跳过 LLM 复检</b><p className="small muted" style={{ marginTop: 6 }}>{gate.warning || '跳过复检可能导致误诊或漏检。'}{!done && (localWorkPending ? ' 首轮检测仍在进行，完成后自动展示结果。' : ' 正在整理首轮结果，请稍候。')}</p></div>}
      {gate?.state === 'invalid' && <div className="card" role="alert" style={{ padding: 16, marginBottom: 16, color: 'var(--red)' }}>{gate.warning || '复检状态记录异常，已阻止提前展示结果；请查看任务错误记录。'}</div>}

      <div className="progress-layout">
        <div className="card">
          <div className="progress-hero">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-end' }}>
              <div className="big">{pct}<span>%</span></div>
              <div className="small muted" style={{ textAlign: 'right' }}>
                {task?.stages.filter((s) => s.status === 'done').length ?? 0} / 8 阶段完成
                <br />
                未接入模块会如实标记
              </div>
            </div>
            <div className="progress-bar"><i style={{ width: `${Math.max(2, pct)}%` }} /></div>
            {task?.error && (
              <div style={{ marginTop: 12, padding: '10px 12px', background: '#fff4f4', border: '1px solid #f6c9cb', borderRadius: 10, fontSize: 12, color: '#a01f26', whiteSpace: 'pre-wrap' }}>
                {task.error.split('\n')[0]}
              </div>
            )}
          </div>

          <div className="stage-list">
            {task?.stages.map((stage) => (
              <div key={stage.key} className={`stage-item ${stage.status}`}>
                <div className="stage-node">
                  <Icon name={STAGE_ICON[stage.status]} size={15} />
                </div>
                <div>
                  <div className="label">{stage.label}</div>
                  <div className="msg">{stage.key === 'mllm' && stage.status === 'failed' && (!stage.message || stage.message.includes('执行完成')) ? 'LLM 复核失败，未取得有效复核结论；首轮候选已保留。' : stage.message || '等待前序阶段完成…'}</div>
                  {stage.key === 'mllm' && stage.status === 'failed' && Array.isArray(stage.detail.review_errors) && (
                    <ul className="small" role="alert" style={{ color: 'var(--red)', margin: '8px 0 0', paddingLeft: 16 }}>
                      {(stage.detail.review_errors as { slide_index?: number; error?: string; error_code?: string }[]).map((error, index) => (
                        <li key={index}>{error.slide_index ? `第 ${error.slide_index} 页：` : ''}{error.error || error.error_code || '接口未返回有效结果'}{error.error_code && error.error && !error.error.includes(error.error_code) ? `（${error.error_code}）` : ''}</li>
                      ))}
                    </ul>
                  )}
                  {stage.key === 'mllm' && gate?.skipped_by_user && <div className="msg">已记录跳过；首轮检测不会中止。跳过复核可能导致误诊或漏检。</div>}
                  {stage.key === 'mllm' && skipError && <div className="small" role="alert" style={{ color: 'var(--red)', marginTop: 6 }}>{skipError}</div>}
                </div>
                <div className="state" style={stage.key === 'mllm' ? { display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6, maxWidth: 180 } : undefined}>
                  <span>{stage.key === 'mllm' && gate?.skipped_by_user && ['pending', 'running'].includes(stage.status) ? '已预约跳过' : STATE_TEXT[stage.status]}</span>
                  {stage.key === 'mllm' && task?.options.enable_mllm && task.options.review_mode !== 'off' && !terminal && !failed && !['done', 'failed', 'skipped', 'unsupported'].includes(stage.status) && (
                    <>
                      <button className="btn btn-sm" title={gate?.state === 'executing' ? '复核已开始，不能撤回已发送请求。' : '跳过 LLM；首轮检测继续，结束后查看结果。'}
                        onClick={() => void skipReview()} disabled={!canSkip} aria-label="跳过复核">
                        {skipping ? '正在记录跳过…' : gate?.skipped_by_user ? '已跳过复核' : gate?.state === 'executing' ? '复核已开始' : '跳过复核'}
                      </button>
                      {gate?.state === 'not_ready' && <span className="tiny muted">可提前跳过，首轮继续</span>}
                      {gate?.state === 'waiting' && <span className="tiny muted">{remainingMs > 0 ? `${Math.ceil(remainingMs / 1000)} 秒后开始` : '即将开始，无法再跳过'}</span>}
                      {gate?.state === 'executing' && <span className="tiny muted">已发送请求，无法跳过</span>}
                    </>
                  )}
                  {stage.key === 'mllm' && task && (terminal || failed) && ['failed', 'skipped', 'pending'].includes(stage.status) && (
                    <RecoveryActions taskId={taskId} status={task.status} mode={mode} compact info={task.recovery} />
                  )}
                </div>
              </div>
            )) ?? (
              <div style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
                {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} height={44} />)}
              </div>
            )}
          </div>

          {done && (
            <div style={{ padding: '0 22px 20px', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button className="btn btn-primary" onClick={() => navigate(`/tasks/${taskId}/results${mode === 'demo' ? '?mode=demo' : ''}`)}>
                查看逐页结果 <Icon name="arrow" size={16} />
              </button>
              <button className="btn" onClick={() => navigate(`/tasks/${taskId}/report${mode === 'demo' ? '?mode=demo' : ''}`)}>
                <Icon name="chart" size={15} /> 汇总报告
              </button>
            </div>
          )}
          {failed && (
            <div style={{ padding: '0 22px 20px', display: 'flex', gap: 10 }}>
              <button className="btn" onClick={() => navigate('/')}>重新上传</button>
              {task?.document && !gateBlocksResults && (
                <button className="btn btn-soft" onClick={() => navigate(`/tasks/${taskId}/results${mode === 'demo' ? '?mode=demo' : ''}`)}>
                  查看已产出内容
                </button>
              )}
            </div>
          )}
          {task && (terminal || failed) && <div style={{ padding: '0 22px 12px' }}>
            <RecoveryActions taskId={taskId} status={task.status} mode={mode} info={task.recovery} />
          </div>}
        </div>

        <div className="stack">
          <div className="panel-dark" style={{ padding: 18 }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <span style={{ fontSize: 11, letterSpacing: '.16em', color: '#7f8ca6', fontWeight: 700 }}>LIVE RENDERS</span>
              <span className="tiny" style={{ color: '#8fa0bd' }}>{rendered} / {task?.document?.slide_count ?? '—'}</span>
            </div>
            <div className="live-thumbs" style={{ marginTop: 12 }}>
              {slides.length === 0 ? (
                <div style={{ gridColumn: '1 / -1', color: '#8fa0bd', fontSize: 12, padding: '18px 0', textAlign: 'center' }}>
                  {stageState('render') === 'done' ? '渲染图加载中…' : '等待页面渲染…'}
                </div>
              ) : slides.slice(0, 12).map((slide) => (
                <div className="live-thumb" key={slide.index}>
                  {slide.thumb_url ? <img src={slide.thumb_url} alt={`第 ${slide.index} 页`} /> : <div style={{ width: '100%', height: '100%', background: '#101a33' }} />}
                  <span className="n">P{String(slide.index).padStart(2, '0')}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="card" style={{ padding: 18 }}>
            <div className="h3" style={{ marginBottom: 10 }}>本任务配置</div>
            <div className="stack small" style={{ gap: 8 }}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className="muted">结构规则</span>
                <span className="badge rule">已启用</span>
              </div>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className="muted">视觉模型</span>
                <span className={`badge ${stageState('visual') === 'done' ? 'model' : 'off'}`}>
                  {stageState('visual') === 'done' ? '已运行' : stageState('visual') === 'unsupported' ? '未接入' : stageState('visual') === 'skipped' ? '已关闭' : stageState('visual') === 'failed' ? '失败' : '等待'}
                </span>
              </div>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className="muted">多模态复核</span>
                <span className={`badge ${stageState('mllm') === 'done' ? 'ai' : 'off'}`}>
                  {gate?.skipped_by_user ? '用户跳过，未复检' : gate?.state === 'waiting' ? '等待确认窗口' : gate?.state === 'executing' ? '执行中' : stageState('mllm') === 'done' ? '已返回复核记录' : stageState('mllm') === 'failed' ? '失败' : stageState('mllm') === 'unsupported' ? '未配置/不可用' : stageState('mllm') === 'skipped' ? '未执行' : '等待'}
                </span>
              </div>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className="muted">任务模式</span>
                <span className="badge low">{mode === 'demo' ? '演示数据' : '真实运行'}</span>
              </div>
            </div>
          </div>

          {task?.document && (
            <div className="card" style={{ padding: 18 }}>
              <div className="h3" style={{ marginBottom: 8 }}>文稿</div>
              <div className="small muted" style={{ lineHeight: 1.8 }}>
                {task.document.file_name}<br />
                {task.document.slide_width_in} × {task.document.slide_height_in} in · {task.document.aspect_label}<br />
                可编辑 {task.document.editable_pages} 页 · 混合 {task.document.mixed_pages} 页 · 位图 {task.document.image_only_pages} 页
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

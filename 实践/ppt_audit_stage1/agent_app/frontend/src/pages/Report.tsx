import { useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { DEFECTS, DEFECT_LABEL, DETECTION_STATUS_LABEL } from '../lib/types'
import type { Advice, Claim, ReviewGate, ReviewOptions, ReviewPlan, ReviewRecord, Summary, TaskDetail, VerificationPipeline, VisualResult } from '../lib/types'
import { claimStatus, displayEvidence, isVisible, relatedPages } from '../lib/review'
import { ClipResultPanel, PlanDetails, ReviewFlowNotice } from '../components/ReviewDetails'
import { Icon } from '../components/Icon'
import { Skeleton, StatusChip } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'
import { RecoveryActions, RecoveryLineageNotice } from '../components/RecoveryActions'

interface ReportIssue {
  issue_id: string
  slide_index: number
  defect_type: string
  title: string
  evidence_summary: string
  severity: string
  review_status: string
  rule_id?: string | null
  bbox?: { x: number; y: number; w: number; h: number } | null
  related_slides?: (number | string)[]
  detector_version?: string
  review_result?: ReviewRecord | null
  raw_status?: string
  user_verdict?: 'dismissed' | null
  user_verdict_note?: string
}

interface ReportData {
  is_demo: boolean
  generated_at: string
  task: { id: string; name: string; status: string; started_at: string | null; finished_at: string | null; status_label: string }
  document: {
    file_name: string
    slide_count: number
    aspect_label: string
    editable_pages: number
    mixed_pages: number
    image_only_pages: number
    textboxes: number
    pictures: number
    tables: number
    charts: number
    missing_fonts: string[]
    size_mb: number
  }
  summary: Summary
  modules: { name: string; state_class: string; state_label: string; detail: string }[]
  issues: ReportIssue[]
  advice?: Advice
  limitations: string[]
  stages: { key: string; label: string; status: string; status_label: string; message: string; finished_at: string | null }[]
  claims?: Claim[]
  review_plan?: ReviewPlan | null
  user_requirements?: { purpose?: string; style?: string; review_focus?: string[]; original_prompt?: string }
  mock_review?: boolean
  schema_version?: string
  options?: ReviewOptions
  visual_results?: VisualResult[]
  verification_pipeline?: VerificationPipeline
  review_gate?: ReviewGate
  recovery?: { reused_review_calls: number; actual_new_http_calls: number }
}

// 同一任务 + 同一结果指纹只自动请求一次模型建议，避免每次打开报告都消耗调用。
const adviceRequests = new Set<string>()

export default function ReportPage() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const navigate = useNavigate()
  const [data, setData] = useState<ReportData | null>(null)
  const [error, setError] = useState('')
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [adviceBusy, setAdviceBusy] = useState<'api' | 'rule' | null>(null)
  const [repairBusy, setRepairBusy] = useState(false)

  useEffect(() => {
    let alive = true
    setTask(null); setData(null); setError('')
    const load = async () => {
      try {
        const current = await api.getTask(taskId, mode)
        if (!alive) return
        setTask(current)
        if (['waiting', 'executing', 'invalid'].includes(current.review_gate?.state ?? '') || ['draft', 'preparing', 'ready', 'queued', 'running'].includes(current.status)) {
          navigate(`/tasks/${taskId}/progress${mode === 'demo' ? '?mode=demo' : ''}`, { replace: true })
          return
        }
        const res = await fetch(api.reportUrl(taskId, 'json', false, mode))
        if (!res.ok) {
          const body = await res.json().catch(() => null)
          throw new Error(body?.error?.message || '报告尚未生成。')
        }
        const report = await res.json()
        if (alive) setData(report)
      } catch (err) { if (alive) setError((err as Error).message) }
    }
    void load()
    return () => { alive = false }
  }, [taskId, mode, navigate])

  // 打开报告时如果只有规则版建议，后台静默换一版模型措辞；失败即保留规则版，不打断阅读。
  useEffect(() => {
    const current = data?.advice
    if (!current || current.source !== 'rule' || current.degraded || current.stats.issues <= 0) return
    const key = `${mode}:${taskId}:${current.fingerprint}`
    if (adviceRequests.has(key)) return
    adviceRequests.add(key)
    let alive = true
    setAdviceBusy('api')
    api.refreshAdvice(taskId, 'api', mode)
      .then((result) => {
        if (alive) setData((previous) => previous ? { ...previous, advice: result.advice, summary: result.summary } : previous)
      })
      .catch(() => undefined)
      .finally(() => { if (alive) setAdviceBusy(null) })
    return () => { alive = false }
  }, [data, taskId, mode])

  if (error) {
    return (
      <div className="card" style={{ padding: 26 }}>
        <div className="row" style={{ color: 'var(--orange)', fontWeight: 700 }}><Icon name="alert" size={18} /> 报告不可用</div>
        <p className="small muted" style={{ marginTop: 6 }}>{error}</p>
        <div className="row" style={{ marginTop: 14 }}>
          <button className="btn btn-sm" onClick={() => navigate(`/tasks/${taskId}/progress${mode === 'demo' ? '?mode=demo' : ''}`)}>查看任务进度</button>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/')}>返回工作台</button>
        </div>
        <RecoveryLineageNotice lineage={task?.lineage} mode={mode} />
        {task && <RecoveryActions taskId={taskId} status={task.status} mode={mode} info={task.recovery} />}
      </div>
    )
  }

  if (!data) {
    return <div className="stack"><Skeleton height={180} /><Skeleton height={100} /><Skeleton height={280} /></div>
  }

  const s = data.summary
  const maxCount = Math.max(1, ...Object.values(s.by_type))
  const visibleIssues = data.issues.filter((i) => i.review_status !== 'rejected' && i.user_verdict !== 'dismissed')
  const dismissedCount = data.issues.filter((i) => i.user_verdict === 'dismissed').length
  const highRisk = visibleIssues.filter((i) => s.high_risk_pages.includes(i.slide_index))
  const modeLabel = { editable: '可编辑', mixed: '混合', image_only: '整位图' }
  const options = data.options ?? task?.options
  const visualResults = data.visual_results ?? []

  // 一键修复：从报告直接进入修复流程，进度页挂在「AI 修复」入口下。
  const startRepair = async () => {
    setRepairBusy(true)
    try {
      const record = await api.startRepair(taskId, 'api', mode)
      navigate(`/repair/${taskId}/progress?rid=${record.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setRepairBusy(false)
    }
  }

  // 建议由复核后的结果生成；模型可用时重写措辞，否则退回规则合成版本。
  const refreshAdvice = async (backend: 'api' | 'rule') => {
    setAdviceBusy(backend)
    try {
      const result = await api.refreshAdvice(taskId, backend, mode)
      setData((previous) => previous ? { ...previous, advice: result.advice, summary: result.summary } : previous)
      const note = result.advice.degraded || (result.advice.source === 'llm' ? '已由模型重写建议' : '已按本次结果重新合成建议')
      toast(note, result.advice.degraded ? 'info' : 'success')
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setAdviceBusy(null)
    }
  }

  return (
    <div className="fade-up">
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="btn btn-ghost btn-sm" style={{ marginLeft: -8 }} onClick={() => navigate(`/tasks/${taskId}/results${mode === 'demo' ? '?mode=demo' : ''}`)}>
          <Icon name="left" size={15} /> 逐页结果
        </button>
        <div className="spacer" />
        {dismissedCount > 0 && (
          <span className="chip gray" title="人工核对渲染图后排除的误判，已从计数、逐页证据、建议与 Prompt 中移除">
            已排除 {dismissedCount} 条误判
          </span>
        )}
        <button className="btn btn-primary btn-sm" disabled={repairBusy || visibleIssues.length === 0}
          title={visibleIssues.length === 0 ? '这份报告没有待修条目，无需生成修复副本' : '按缺陷清单生成修复副本，并在新副本上复检'}
          onClick={() => void startRepair()}>
          <Icon name="spark" size={14} /> {repairBusy ? '正在启动…' : visibleIssues.length === 0 ? '无需修复' : 'AI 在线修复'}
        </button>
        <a className="btn btn-sm" href={api.promptUrl(taskId, true, mode)}>
          <Icon name="download" size={14} /> AI 修改 Prompt
        </a>
        <a className="btn btn-sm" href={api.reportUrl(taskId, 'html', false, mode)} target="_blank" rel="noreferrer">
          <Icon name="external" size={14} /> 打开 HTML 报告
        </a>
        <a className="btn btn-sm" href={api.reportUrl(taskId, 'html', true, mode)}>
          <Icon name="download" size={14} /> 下载 HTML
        </a>
        <a className="btn btn-sm" href={api.reportUrl(taskId, 'json', true, mode)}>
          <Icon name="download" size={14} /> JSON
        </a>
        <a className="btn btn-sm" href={api.reportUrl(taskId, 'pdf', true, mode)}><Icon name="download" size={14} /> PDF</a>
      </div>

      <RecoveryLineageNotice lineage={task?.lineage} mode={mode} />
      {task && <div className="card" style={{ padding: '0 16px', marginBottom: 12 }}>
        <RecoveryActions taskId={taskId} status={task.status} mode={mode} info={task.recovery} />
      </div>}

      {data.recovery && <p className="small muted" role="status" style={{ marginBottom: 12 }}>
        复用 {data.recovery.reused_review_calls} 项成功复核，本次实际新增 {data.recovery.actual_new_http_calls} 次 API 调用；原任务结果保留。
      </p>}

      {data.is_demo && (
        <div className="chip demo" style={{ marginBottom: 12, height: 30 }}>
          <Icon name="info" size={14} /> 演示报告 · 数据隔离，不代表真实模型评估
        </div>
      )}
      {data.mock_review && <div className="chip demo" style={{ marginBottom: 12 }}>Mock 模拟复核报告 · 检测证据来自实际执行，复核结论不代表真实模型判断</div>}

      <div className="panel-dark report-hero">
        <div>
          <div className="eyebrow" style={{ color: '#7de7eb' }}>Design quality report</div>
          <h1>{data.task.name}</h1>
          <div className="file">{data.document.file_name} · {formatDate(data.generated_at)}</div>
          <div className="row" style={{ marginTop: 16, gap: 8, flexWrap: 'wrap' }}>
            <span className="chip" style={{ background: 'rgba(255,255,255,.08)', borderColor: 'rgba(255,255,255,.16)', color: '#dfe8ff' }}>
              {data.document.slide_count} 页 · {data.document.aspect_label}
            </span>
            <span className="chip" style={{ background: 'rgba(255,255,255,.08)', borderColor: 'rgba(255,255,255,.16)', color: '#dfe8ff' }}>
              可编辑 {data.document.editable_pages} · 混合 {data.document.mixed_pages} · 位图 {data.document.image_only_pages}
            </span>
            {data.task.status === 'completed' || data.task.status === 'completed_with_errors' ? null : (
              <StatusChip status={data.task.status as never} />
            )}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 11, color: '#8fa0bd', letterSpacing: '.16em' }}>FINDINGS</div>
          <div style={{ fontSize: 56, fontWeight: 750, lineHeight: 1.05, letterSpacing: '-.04em' }}>{s.visible_issue_count}</div>
          <div style={{ fontSize: 11.5, color: '#9fb0cc' }}>涉及 {s.pages_with_issues} / {s.slide_count} 页</div>
        </div>
      </div>

      <div className="stat-grid" style={{ marginTop: 14 }}>
        <div className="stat"><div className="k">文稿页面</div><div className="v">{s.slide_count}</div><div className="u">具体覆盖范围见检查记录</div></div>
        <div className="stat warn"><div className="k">高风险页面</div><div className="v">{s.high_risk_pages.length}</div><div className="u">含 high 级证据</div></div>
        <div className="stat"><div className="k">{data.mock_review ? '模拟复核页面' : 'AI 已复核'}</div><div className="v">{s.ai_reviewed_pages}</div><div className="u">支持 {s.ai_supported} 条 · 不成立 {s.ai_rejected} 条</div></div>
        <div className="stat"><div className="k">未复核问题</div><div className="v">{s.unreviewed_count}</div><div className="u">等待人工或 AI 判断</div></div>
      </div>

      {data.advice && (
        <section className="advice-card">
          <div className="advice-bar">
            <div>
              <div className="advice-kicker">AI REVISION PLAN</div>
              <h2>AI 修改建议</h2>
            </div>
            <div className="spacer" />
            <span className={`chip ${data.advice.source === 'llm' ? 'ai' : 'gray'}`}>
              {adviceBusy === 'api' && data.advice.source !== 'llm'
                ? '模型生成中…'
                : data.advice.source === 'llm' ? `模型生成 · ${data.advice.model}` : '规则合成'}
            </span>
            <button className="btn btn-sm" disabled={!!adviceBusy} onClick={() => void refreshAdvice('api')}>
              {adviceBusy === 'api' ? '生成中…' : '重新生成'}
            </button>
            <a className="btn btn-sm" href={api.promptUrl(taskId, true, mode)}>
              <Icon name="download" size={14} /> 导出 Prompt
            </a>
          </div>

          <p className="advice-lead">{data.advice.headline}</p>
          <p className="advice-verdict">{data.advice.verdict}</p>

          {data.advice.summary.length > 0 && (
            <ul className="advice-points">
              {data.advice.summary.map((line, index) => <li key={index}>{line}</li>)}
            </ul>
          )}

          {data.advice.actions.length > 0 && (
            <div className="advice-grid">
              {data.advice.actions.map((action, index) => (
                <article className="advice-act" key={action.defect_type}>
                  <header>
                    <span className="no">{String(index + 1).padStart(2, '0')}</span>
                    <b>{action.title}</b>
                    {action.high && <span className="pill high">高优先</span>}
                    <span className="pill">{action.count} 条 · {action.pages_label}</span>
                  </header>
                  <p className="dir">{action.directive}</p>
                  <ol>{action.steps.map((step, i) => <li key={i}>{step}</li>)}</ol>
                  <p className="acc"><Icon name="check" size={12} /> 验收：{action.acceptance}</p>
                </article>
              ))}
            </div>
          )}

          {data.advice.sequence.length > 0 && (
            <div className="advice-flow">
              {data.advice.sequence.map((phase) => (
                <div className="advice-phase" key={phase.title}>
                  <b>{phase.title}</b>
                  <span>{phase.detail}</span>
                  <i>{phase.pages_label} · {phase.count} 条</i>
                </div>
              ))}
            </div>
          )}

          {data.advice.page_plan.length > 0 && (
            <div className="advice-pages">
              <div className="advice-pages-head">逐页待办</div>
              {data.advice.page_plan.map((page) => (
                <button key={page.slide_index} className="advice-page"
                  onClick={() => navigate(`/tasks/${taskId}/results?page=${page.slide_index}${mode === 'demo' ? '&mode=demo' : ''}`)}>
                  <b>{page.label}</b>
                  <span>{page.focus}</span>
                  {page.high && <i>高</i>}
                </button>
              ))}
            </div>
          )}

          <p className="advice-foot">
            {data.advice.note}
            {data.advice.stats.dismissed > 0 && ` 已排除 ${data.advice.stats.dismissed} 条人工判定的误判。`}
            {data.advice.degraded ? ` ${data.advice.degraded}` : ''}
          </p>
        </section>
      )}

      <div className="card" style={{ marginTop: 14, overflow: 'hidden' }}><ReviewFlowNotice options={options} stages={data.stages} planItems={data.review_plan?.items} verification={data.verification_pipeline} gate={data.review_gate ?? data.verification_pipeline?.review_gate ?? task?.review_gate} /></div>

      <div className="report-grid" style={{ marginTop: 16 }}>
        <div className="stack">
          <div className="card" style={{ padding: 20 }}>
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
              <div className="h3">六类缺陷分布</div>
              <span className="tiny muted">按候选问题计数，已排除 AI 判定不成立项</span>
            </div>
            {DEFECTS.map((d) => {
              const count = s.by_type[d.key] || 0
              return (
                <div className="dist-row" key={d.key}>
                  <span className="small">{d.label}</span>
                  <span className="bar"><i style={{ width: `${(count / maxCount) * 100}%`, background: d.color }} /></span>
                  <span className="n">{count}</span>
                </div>
              )
            })}
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>六类检测范围与未决项</div>
            <p className="tiny muted">可判范围未发现候选不代表整页正常；异常设计风险不表示 AI 生成概率。</p>
            {DEFECTS.map((defect) => {
              const entries = (data.claims ?? []).filter((claim) => claim.defect_type === defect.key)
              const counts: Record<string, number> = {}
              entries.forEach((entry) => { const status = claimStatus(entry); counts[status] = (counts[status] ?? 0) + 1 })
              return <div key={defect.key} className="scope-row"><span className="small">{defect.label}</span><span className="scope-statuses">{entries.length ? Object.entries(counts).map(([status, count]) => <span className={`badge ${status === 'candidate' ? 'mid' : 'off'}`} key={status}>{DETECTION_STATUS_LABEL[status as keyof typeof DETECTION_STATUS_LABEL]} {count}</span>) : <span className="badge off">未提供范围记录</span>}</span></div>
            })}
            <details className="tiny muted" style={{ marginTop: 8 }}><summary>检测工具与版本</summary>{[...new Set((data.claims ?? []).map((claim) => claim.detector_version).filter(Boolean))].join('；') || '本任务未提供版本记录'}</details>
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>CLIP 四标签辅助复核</div>
            <p className="small muted">{options?.enable_visual ? `已取得 ${visualResults.filter((result) => result.status === 'ok').length} / ${s.slide_count} 页有效模型输出。` : options ? '本次未启用 CLIP，不会用缺少输出表示正常。' : '尚未取得该任务的 CLIP 启用记录。'}CLIP 不判断 D5/D6，不取消 R2 候选，也不代替最终 LLM 复检。</p>
            {visualResults.length > 0 ? <div style={{ maxHeight: 420, overflowY: 'auto', marginTop: 8 }}>{visualResults.map((result) => <ClipResultPanel key={result.slide_index} result={result} enabled={options?.enable_visual} title={`第 ${result.slide_index} 页`} />)}</div> : options?.enable_visual ? <p className="tiny muted" style={{ marginTop: 8 }}>本报告没有可读取的逐页 CLIP 输出，请保留该范围未完成状态。</p> : null}
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
              <div className="h3">高风险页面</div>
              <span className="tiny muted">{highRisk.length} 条证据</span>
            </div>
            {highRisk.length === 0 ? (
              <div className="small muted" style={{ padding: '12px 0' }}>没有 high 级证据。未接入模块仍可能遗漏问题。</div>
            ) : (
              highRisk.slice(0, 8).map((issue) => (
                <div className="risk-page" key={issue.issue_id}>
                  <img src={`/api/tasks/${taskId}/slides/${issue.slide_index}/image?kind=thumb${mode === 'demo' ? '&mode=demo' : ''}`} alt={`第 ${issue.slide_index} 页`} loading="lazy" />
                  <div style={{ minWidth: 0 }}>
                    <div className="small" style={{ fontWeight: 650 }}>P{String(issue.slide_index).padStart(2, '0')} · {issue.title}</div>
                    <div className="tiny muted" style={{ marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                      {issue.evidence_summary}
                    </div>
                  </div>
                  <span className={`badge ${issue.severity === 'high' ? 'high' : 'mid'}`}>{DEFECT_LABEL[issue.defect_type]}</span>
                </div>
              ))
            )}
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>模块运行状态</div>
            {data.modules.map((m) => (
              <div key={m.name} className="row" style={{ alignItems: 'flex-start', padding: '10px 0', borderTop: '1px solid var(--line)' }}>
                <span className={`dot ${m.state_class === 'done' ? '' : ''}`} style={{
                  width: 8, height: 8, borderRadius: '50%', marginTop: 6, flex: 'none',
                  background: m.state_class === 'done' ? 'var(--green)' : m.state_class === 'fail' ? 'var(--red)' : '#a7b0c2',
                }} />
                <div style={{ flex: 1 }}>
                  <div className="small" style={{ fontWeight: 650 }}>{m.name}</div>
                  <div className="tiny muted" style={{ marginTop: 2 }}>{m.detail}</div>
                </div>
                <span className={`badge ${m.state_class === 'done' ? 'ok' : m.state_class === 'fail' ? 'high' : 'off'}`}>
                  {m.state_label || (m.state_class === 'done' ? '已运行' : '未运行')}
                </span>
              </div>
            ))}
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>逐页问题索引</div>
            <div style={{ maxHeight: 330, overflow: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                    <th style={{ padding: '6px 4px', fontWeight: 600 }}>页</th>
                    <th style={{ padding: '6px 4px', fontWeight: 600 }}>问题</th>
                    <th style={{ padding: '6px 4px', fontWeight: 600 }}>来源</th>
                    <th style={{ padding: '6px 4px', fontWeight: 600 }}>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleIssues.map((issue) => (
                    <tr key={issue.issue_id} style={{ borderTop: '1px solid var(--line)' }}>
                      <td style={{ padding: '7px 4px', fontVariantNumeric: 'tabular-nums' }}><button className="btn btn-ghost btn-sm" onClick={() => navigate(`/tasks/${taskId}/results?page=${issue.slide_index}${mode === 'demo' ? '&mode=demo' : ''}`)}>P{String(issue.slide_index).padStart(2, '0')}</button>{relatedPages(issue.related_slides, s.slide_count).filter((page) => page !== issue.slide_index).map((page) => <button key={page} className="btn btn-ghost btn-sm" onClick={() => navigate(`/tasks/${taskId}/results?page=${page}${mode === 'demo' ? '&mode=demo' : ''}`)}>P{String(page).padStart(2, '0')}</button>)}</td>
                      <td style={{ padding: '7px 4px' }}>{issue.title}</td>
                      <td style={{ padding: '7px 4px' }}>{issue.rule_id || (issue.bbox ? '规则' : '模型/AI')}</td>
                      <td style={{ padding: '7px 4px' }}>
                        <span className={`badge ${issue.review_status === 'supported' ? 'ai' : 'low'}`}>
                          {issue.review_result?.mock || issue.review_result?.demo || issue.review_result?.backend === 'mock' ? 'Mock 模拟 · ' : ''}{issue.review_status === 'supported' ? '复核支持' : issue.review_status === 'uncertain' ? '待核实' : '未复核'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="stack">
          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>用户审查要求</div>
            <div className="small">用途：{data.user_requirements?.purpose || '未指定'}</div>
            <div className="small">风格：{data.user_requirements?.style || '未指定'}</div>
            <div className="small">重点：{data.user_requirements?.review_focus?.map((key) => DEFECT_LABEL[key] ?? key).join('、') || '未额外指定'}</div>
            {data.user_requirements?.original_prompt && <details style={{ marginTop: 8 }}><summary className="small">原始生成要求</summary><p className="tiny muted" style={{ whiteSpace: 'pre-wrap' }}>{data.user_requirements.original_prompt}</p></details>}
          </div>
          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>智能体复核调度</div>
            {data.review_plan ? <><div className="tiny muted">策略：{data.review_plan.strategy === 'full_review' ? '完整复核' : data.review_plan.strategy === 'selective_review' ? '按需复核' : data.review_plan.strategy ?? '未记录'}</div>{data.review_plan.budget != null && <div className="tiny muted">预算：{displayEvidence(data.review_plan.budget)}</div>}{data.review_plan.usage != null && <div className="tiny muted">使用情况：{displayEvidence(data.review_plan.usage)}</div>}<PlanDetails items={data.review_plan.items ?? []} /></> : <p className="small muted">本任务没有可读取的调度记录。</p>}
          </div>
          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>检测局限</div>
            <ul className="small" style={{ margin: 0, paddingLeft: 17, color: 'var(--muted)', lineHeight: 1.75 }}>
              {data.limitations.map((item, i) => <li key={i} style={{ marginBottom: 6 }}>{item}</li>)}
            </ul>
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 10 }}>执行轨迹</div>
            {data.stages.map((stage) => (
              <div key={stage.key} className="row" style={{ alignItems: 'flex-start', padding: '7px 0', borderTop: '1px solid var(--line)' }}>
                <span style={{
                  width: 7, height: 7, borderRadius: '50%', marginTop: 6, flex: 'none',
                  background: stage.status === 'done' ? 'var(--green)' : stage.status === 'failed' ? 'var(--red)' : '#b8c1d3',
                }} />
                <div style={{ flex: 1 }}>
                  <div className="small" style={{ fontWeight: 600 }}>{stage.label}</div>
                  <div className="tiny muted">{stage.message || '—'}</div>
                </div>
                <span className="tiny muted">{stage.status_label}</span>
              </div>
            ))}
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="h3" style={{ marginBottom: 8 }}>下载</div>
            <div className="stack" style={{ gap: 8 }}>
              <a className="btn btn-sm" href={api.promptUrl(taskId, true, mode)}><Icon name="download" size={14} /> AI 修改 Prompt（.md）</a>
              <a className="btn btn-sm" href={api.reportUrl(taskId, 'html', true, mode)}><Icon name="download" size={14} /> HTML 可视化报告</a>
              <a className="btn btn-sm" href={api.reportUrl(taskId, 'json', true, mode)}><Icon name="download" size={14} /> JSON 结构化报告</a>
              <a className="btn btn-sm" href={api.reportUrl(taskId, 'pdf', true, mode)}><Icon name="download" size={14} /> PDF 报告</a>
            </div>
            <p className="tiny muted" style={{ marginTop: 10, lineHeight: 1.6 }}>
              HTML 报告为自包含文件，缩略图已内嵌，可直接用于答辩展示。
            </p>
          </div>
        </div>
      </div>

      <div className="small muted" style={{ marginTop: 18 }}>
        生成于 {formatDate(data.generated_at)} · {data.mock_review || data.is_demo ? '模拟复核已单独标记，不能作为真实模型判断' : '所有数值来自本次任务执行记录'}
      </div>
    </div>
  )
}

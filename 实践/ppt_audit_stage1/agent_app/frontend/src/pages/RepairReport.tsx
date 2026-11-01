import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { DEFECTS, DEFECT_COLOR, DEFECT_LABEL, type RepairRecord } from '../lib/types'
import { Icon } from '../components/Icon'
import { Skeleton } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'

interface ReviewIssue {
  issue_id: string
  slide_index: number
  defect_type: string
  title: string
  evidence_summary: string
  severity: string
  review_status: string
  evidence_source?: string
  rule_id?: string | null
  user_verdict?: 'dismissed' | null
}

interface ReportShape {
  task: { id: string; name: string }
  document: { file_name: string; slide_count: number; aspect_label: string; editable_pages: number; mixed_pages: number; image_only_pages: number }
  summary: { visible_issue_count: number; pages_with_issues: number; dismissed_count?: number; high_risk_pages: number[]; by_type: Record<string, number> }
  issues: ReviewIssue[]
  advice?: { headline: string; actions: { title: string; directive: string; count: number; pages_label: string }[] }
}

const SEVERITY: Record<string, string> = { high: '高', medium: '中', low: '低' }

export default function RepairReport() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const navigate = useNavigate()
  const [data, setData] = useState<ReportShape | null>(null)
  const [history, setHistory] = useState<RepairRecord[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [review, setReview] = useState(false)

  useEffect(() => {
    let alive = true
    setData(null); setError('')
    const load = async () => {
      try {
        const res = await fetch(api.reportUrl(taskId, 'json', false, mode))
        if (!res.ok) throw new Error('这份任务还没有生成报告。')
        const report = await res.json()
        if (!alive) return
        setData(report)
        const list = await api.repairs(taskId, mode).catch(() => ({ items: [] }))
        if (alive) setHistory(list.items ?? [])
      } catch (err) { if (alive) setError((err as Error).message) }
    }
    void load()
    return () => { alive = false }
  }, [taskId, mode])

  const visible = useMemo(
    () => (data?.issues ?? []).filter((issue) => issue.review_status !== 'rejected' && issue.user_verdict !== 'dismissed'),
    [data],
  )
  const autoEligible = useMemo(() => visible.filter((issue) => issue.evidence_source === 'structure_rule'), [visible])
  const pages = useMemo(() => {
    const map = new Map<number, ReviewIssue[]>()
    for (const issue of visible) {
      const list = map.get(issue.slide_index) ?? []
      list.push(issue)
      map.set(issue.slide_index, list)
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0])
  }, [visible])

  const latest = history[0]
  const running = latest && ['queued', 'running'].includes(latest.status)
  const noAuto = autoEligible.length === 0

  const startRepair = async () => {
    setBusy(true)
    try {
      const record = await api.startRepair(taskId, 'api', mode, review)
      navigate(`/repair/${taskId}/progress?rid=${record.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  if (error) {
    return (
      <div className="card" style={{ padding: 26 }}>
        <div className="row" style={{ color: 'var(--orange)', fontWeight: 700 }}><Icon name="alert" size={18} /> 报告不可用</div>
        <p className="small muted" style={{ marginTop: 6 }}>{error}</p>
        <button className="btn btn-sm" style={{ marginTop: 12 }} onClick={() => navigate('/repair')}>返回 AI 修复</button>
      </div>
    )
  }

  if (!data) {
    return <div className="stack"><Skeleton height={120} /><Skeleton height={220} /></div>
  }

  const doc = data.document
  const summary = data.summary
  const maxCount = Math.max(1, ...Object.values(summary.by_type))
  const top = DEFECTS
    .map((defect) => ({ ...defect, count: summary.by_type[defect.key] ?? 0 }))
    .filter((item) => item.count > 0)
    .sort((a, b) => b.count - a.count)

  return (
    <div className="fade-up">
      <div className="repair-hero">
        <div className="repair-hero-main">
          <button className="btn btn-ghost btn-sm" style={{ marginLeft: -8, marginBottom: 6 }} onClick={() => navigate('/repair')}>
            <Icon name="left" size={15} /> AI 修复
          </button>
          <h1>{data.task.name}</h1>
          <div className="lead">
            {doc.file_name} · {doc.slide_count} 页 · {doc.aspect_label} · 可编辑 {doc.editable_pages} / 混合 {doc.mixed_pages} / 位图 {doc.image_only_pages}
          </div>
        </div>
        <div className="repair-hero-act">
          <button className="btn btn-ghost btn-sm" onClick={() => navigate(`/tasks/${taskId}/report${mode === 'demo' ? '?mode=demo' : ''}`)}>
            <Icon name="file" size={15} /> 完整报告
          </button>
          {latest?.status === 'completed' ? (
            <button className="btn btn-sm" onClick={() => navigate(`/repair/${taskId}/result?rid=${latest.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)}>
              <Icon name="eye" size={15} /> 查看修复结果
            </button>
          ) : running ? (
            <button className="btn btn-sm" onClick={() => navigate(`/repair/${taskId}/progress?rid=${latest.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)}>
              <Icon name="spinner" size={15} /> 修复进行中
            </button>
          ) : null}
          <button className="btn btn-primary" disabled={busy || !!running || noAuto}
            title={noAuto ? '这份报告没有带对象级坐标的缺陷，只能人工处理' : '按缺陷清单在副本上生成修复'}
            onClick={() => void startRepair()}>
            <Icon name="spark" size={16} /> {busy ? '正在启动…' : noAuto ? '没有可自动修的条目' : 'AI 在线修复'}
          </button>
          <label className={`repair-toggle${review ? ' on' : ''}`}
            title="每轮改完再复检，没修净的继续让模型换动作；最多 3 轮，token 消耗更高">
            <input type="checkbox" checked={review} onChange={(event) => setReview(event.target.checked)} />
            <span>LLM 复核（多轮）</span>
            <i>每轮复检，未修净继续改</i>
          </label>
        </div>
      </div>

      <div className="repair-kpis">
        <div className="repair-kpi"><b>{summary.visible_issue_count}</b><span>条可见缺陷</span></div>
        <div className="repair-kpi"><b>{summary.pages_with_issues}</b><span>页涉及修改</span></div>
        <div className="repair-kpi"><b>{autoEligible.length}</b><span>条可自动修复</span></div>
        <div className="repair-kpi"><b>{summary.high_risk_pages.length}</b><span>页高优先</span></div>
      </div>

      <div className="repair-layout">
        <div className="stack">
          {data.advice && (
            <section className="card repair-advice">
              <div className="advice-kicker">AI REVISION PLAN</div>
              <p className="advice-lead">{data.advice.headline}</p>
              <div className="repair-advice-list">
                {data.advice.actions.slice(0, 4).map((action) => (
                  <div key={action.title}>
                    <b>{action.title}</b>
                    <span>{action.directive}</span>
                    <i>{action.count} 条 · {action.pages_label}</i>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="card" style={{ padding: 18 }}>
            <div className="h3" style={{ marginBottom: 10 }}>缺陷分布</div>
            {top.length === 0 ? (
              <p className="small muted">这份文稿没有可见缺陷，不需要修复。</p>
            ) : (
              <div className="repair-dist">
                {top.map((item) => (
                  <div className="repair-dist-row" key={item.key}>
                    <span className="nm"><i style={{ background: item.color }} />{item.label}</span>
                    <span className="bar"><i style={{ width: `${(item.count / maxCount) * 100}%`, background: item.color }} /></span>
                    <b>{item.count}</b>
                    <span className="tiny muted">
                      可自动 {autoEligible.filter((issue) => issue.defect_type === item.key).length}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card" style={{ padding: 18 }}>
            <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10 }}>
              <div className="h3">逐页待修清单</div>
              <span className="tiny muted">「可自动」的条目会在修复副本里直接改，其余留待人工</span>
            </div>
            <div className="repair-pages">
              {pages.map(([index, list]) => (
                <div className="repair-page" key={index}>
                  <div className="repair-page-head">
                    <b>P{String(index).padStart(2, '0')}</b>
                    <span>{list.length} 条</span>
                    <i>{list.filter((issue) => issue.evidence_source === 'structure_rule').length} 条可自动</i>
                  </div>
                  <ul>
                    {list.map((issue) => (
                      <li key={issue.issue_id}>
                        <i className="dot" style={{ background: DEFECT_COLOR[issue.defect_type] || '#888' }} />
                        <b>{DEFECT_LABEL[issue.defect_type] || issue.defect_type}</b>
                        <span>{issue.title}</span>
                        <em className={`sev ${issue.severity}`}>{SEVERITY[issue.severity] ?? issue.severity}</em>
                        <em className={issue.evidence_source === 'structure_rule' ? 'auto' : 'human'}>
                          {issue.evidence_source === 'structure_rule' ? '可自动' : '人工'}
                        </em>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        </div>

        <aside className="stack">
          <section className="card repair-side">
            <div className="h3">这次会改什么</div>
            <ul className="repair-scope">
              <li><Icon name="check" size={14} /><div><b>越界对象</b><span>按坐标移回画布内，保留原始尺寸</span></div></li>
              <li><Icon name="check" size={14} /><div><b>被遮文字</b><span>只把被压住的文字提到上层，卡片底色不动</span></div></li>
              <li><Icon name="check" size={14} /><div><b>过小字号</b><span>提到 12pt，并按新字号撑高文本框</span></div></li>
              <li className="off"><Icon name="x" size={14} /><div><b>拥挤 / 留白 / 图标语义</b><span>需要重排版面或重画素材，交给人工</span></div></li>
            </ul>
            <p className="tiny muted" style={{ marginTop: 10 }}>
              修复只写副本：<b style={{ color: 'var(--ink)' }}>{doc.file_name.replace(/\.pptx$/i, '')}_已修复.pptx</b>，原始文件、报告与复核记录都不改动。
            </p>
          </section>

          {latest && (
            <section className="card repair-side">
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div className="h3">最近一次修复</div>
                <span className={`repair-state ${latest.status}`}><i />{latest.status === 'completed' ? '已完成' : latest.status === 'failed' ? '失败' : '进行中'}</span>
              </div>
              <div className="repair-last">
                <div><span>时间</span><b>{formatDate(latest.finished_at ?? latest.created_at)}</b></div>
                <div><span>自动修复</span><b>{latest.summary.applied} / {latest.summary.issues} 条</b></div>
                {latest.verify && <div><span>结构问题</span><b>{latest.verify.before_structure} → {latest.verify.after_structure}</b></div>}
              </div>
              {latest.status === 'completed' && (
                <button className="btn btn-sm" style={{ width: '100%', marginTop: 10 }}
                  onClick={() => navigate(`/repair/${taskId}/result?rid=${latest.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`)}>
                  <Icon name="eye" size={14} /> 打开修复结果
                </button>
              )}
            </section>
          )}

          <section className="card repair-cta">
            <div className="repair-cta-kicker">准备好后</div>
            <b>一键生成修复副本</b>
            <p>系统会按缺陷清单规划动作、在副本上写入修改、渲染预览，并用同一套结构规则复检，最后给出可下载的文件。</p>
            <button className="btn btn-primary" disabled={busy || !!running || noAuto}
              title={noAuto ? '这份报告没有带对象级坐标的缺陷，只能人工处理' : '按缺陷清单在副本上生成修复'}
              onClick={() => void startRepair()}>
              <Icon name="spark" size={16} /> {busy ? '正在启动…' : running ? '修复进行中' : noAuto ? '没有可自动修的条目' : 'AI 在线修复'}
            </button>
            <label className={`repair-toggle${review ? ' on' : ''}`}
              title="每轮改完再复检，没修净的继续让模型换动作；最多 3 轮，token 消耗更高">
              <input type="checkbox" checked={review} onChange={(event) => setReview(event.target.checked)} />
              <span>LLM 复核（多轮）</span>
              <i>每轮复检，未修净继续改</i>
            </label>
          </section>
        </aside>
      </div>
    </div>
  )
}

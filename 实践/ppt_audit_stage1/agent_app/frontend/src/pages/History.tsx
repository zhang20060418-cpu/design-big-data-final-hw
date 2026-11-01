import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import type { TaskSummary } from '../lib/types'
import { EmptyState, Skeleton, StatusChip } from '../components/Ui'
import { Icon } from '../components/Icon'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'
import { RecoveryActions } from '../components/RecoveryActions'

export default function History() {
  const navigate = useNavigate()
  const [items, setItems] = useState<TaskSummary[] | null>(null)
  const [demos, setDemos] = useState<TaskSummary[]>([])
  const [filter, setFilter] = useState<'all' | 'active' | 'done' | 'failed'>('all')

  const load = async () => {
    try {
      const [real, demo] = await Promise.all([api.listTasks('real'), api.listTasks('demo')])
      setItems(real.items)
      setDemos(demo.items)
    } catch (err) {
      toast((err as Error).message, 'error')
    }
  }

  useEffect(() => { void load() }, [])

  const open = (task: TaskSummary) => {
    const suffix = task.mode === 'demo' ? '?mode=demo' : ''
    if (['draft', 'preparing', 'ready'].includes(task.status)) navigate(`/tasks/${task.id}/configure${suffix}`)
    else if (['queued', 'running', 'failed'].includes(task.status)) navigate(`/tasks/${task.id}/progress${suffix}`)
    else navigate(`/tasks/${task.id}/results${suffix}`)
  }

  const remove = async (task: TaskSummary) => {
    if (!window.confirm(`删除任务「${task.name}」及其临时文件？`)) return
    try {
      await api.deleteTask(task.id, task.mode)
      toast('任务已删除。', 'success')
      void load()
    } catch (err) {
      toast((err as Error).message, 'error')
    }
  }

  const filtered = (items ?? []).filter((task) => {
    if (filter === 'all') return true
    if (filter === 'active') return ['draft', 'preparing', 'ready', 'queued', 'running'].includes(task.status)
    if (filter === 'done') return ['completed', 'completed_with_errors'].includes(task.status)
    return task.status === 'failed'
  })

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <span className="eyebrow">Audit archive</span>
          <h1 style={{ marginTop: 10 }}>审查任务</h1>
          <div className="lead">真实任务与演示任务分开存放；删除任务会清理其上传与渲染文件。</div>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/')}><Icon name="upload" size={16} /> 新建审查</button>
      </div>

      <div className="results-bar">
        <div className="filter-group">
          {([
            ['all', '全部'], ['active', '进行中'], ['done', '已完成'], ['failed', '失败'],
          ] as const).map(([key, label]) => (
            <button key={key} className="filter-chip" aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>
          ))}
        </div>
        <div className="spacer" />
        <span className="tiny muted">共 {items?.length ?? 0} 个真实任务 · {demos.length} 个演示任务</span>
      </div>

      <div className="card">
        {items === null ? (
          <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Skeleton height={46} /><Skeleton height={46} /><Skeleton height={46} />
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState icon="history" title="没有符合条件的任务" hint="换一个筛选条件，或新建一次审查。" />
        ) : filtered.map((task) => (
          <div key={task.id} className="task-row" role="button" tabIndex={0}
            onClick={() => open(task)} onKeyDown={(e) => e.key === 'Enter' && open(task)}>
            <div style={{ minWidth: 0 }}>
              <div className="task-name">{task.name}</div>
              <div className="task-sub">{task.source_name} · {formatDate(task.created_at)}</div>
            </div>
            <StatusChip status={task.status} />
            <div className="task-metric pages">{task.slide_count ? `${task.slide_count} 页` : '—'}</div>
            <div className="task-metric">{task.summary ? `${task.summary.visible_issue_count} 条问题` : '—'}</div>
            <div className="row" style={{ justifyContent: 'flex-end', gap: 2 }}>
              <RecoveryActions taskId={task.id} status={task.status} mode={task.mode} compact />
              {task.summary && (
                <button className="icon-btn" aria-label="报告" onClick={(e) => { e.stopPropagation(); navigate(`/tasks/${task.id}/report${task.mode === 'demo' ? '?mode=demo' : ''}`) }}>
                  <Icon name="chart" size={15} />
                </button>
              )}
              <button className="icon-btn" aria-label="删除任务" onClick={(e) => { e.stopPropagation(); void remove(task) }}>
                <Icon name="trash" size={15} />
              </button>
            </div>
          </div>
        ))}
      </div>

      {demos.length > 0 && (
        <>
          <div className="section-head">
            <div><div className="kicker">demo cohort</div><div className="h2">演示任务</div></div>
            <span className="chip demo">独立数据库</span>
          </div>
          <div className="card">
            {demos.map((task) => (
              <div key={task.id} className="task-row" role="button" tabIndex={0}
                onClick={() => open(task)} onKeyDown={(e) => e.key === 'Enter' && open(task)}>
                <div style={{ minWidth: 0 }}>
                  <div className="task-name">{task.name} <span className="badge ai">DEMO</span></div>
                  <div className="task-sub">{task.source_name} · {formatDate(task.created_at)}</div>
                </div>
                <StatusChip status={task.status} />
                <div className="task-metric pages">{task.slide_count ? `${task.slide_count} 页` : '—'}</div>
                <div className="task-metric">{task.summary ? `${task.summary.visible_issue_count} 条` : '—'}</div>
                <div className="row" style={{ justifyContent: 'flex-end' }}>
                  <button className="icon-btn" aria-label="删除演示任务" onClick={(e) => { e.stopPropagation(); void remove(task) }}>
                    <Icon name="trash" size={15} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

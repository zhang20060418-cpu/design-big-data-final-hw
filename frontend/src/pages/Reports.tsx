import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { DEFECTS, type TaskSummary } from '../lib/types'
import { EmptyState, Skeleton } from '../components/Ui'
import { Icon } from '../components/Icon'
import { formatDate } from '../lib/format'

const DONE = new Set(['completed', 'completed_with_errors'])

export default function Reports() {
  const navigate = useNavigate()
  const [items, setItems] = useState<TaskSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api.listTasks('real')
      .then((res) => { if (alive) setItems(res.items ?? []) })
      .catch((err) => {
        if (!alive) return
        setError((err as Error).message)
        setItems([])
      })
    return () => { alive = false }
  }, [])

  const done = useMemo(
    () => (items ?? [])
      .filter((task) => DONE.has(task.status))
      .sort((a, b) => (b.finished_at ?? b.created_at).localeCompare(a.finished_at ?? a.created_at)),
    [items],
  )

  const stat = useMemo(() => {
    const risk = done.reduce((sum, task) => sum + (task.summary?.visible_issue_count ?? 0), 0)
    const pages = done.reduce((sum, task) => sum + (task.slide_count ?? 0), 0)
    const reviewed = done.reduce((sum, task) => sum + (task.summary?.ai_reviewed_pages ?? 0), 0)
    return { risk, pages, reviewed, coverage: pages > 0 ? Math.round((reviewed / pages) * 100) : 0 }
  }, [done])

  const go = (task: TaskSummary, target: 'results' | 'report') => {
    const suffix = task.mode === 'demo' ? '?mode=demo' : ''
    navigate(`/tasks/${task.id}/${target}${suffix}`)
  }

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <span className="eyebrow">Report hub</span>
          <h1 style={{ marginTop: 10 }}>报告中心</h1>
          <div className="lead">汇总本机已完成任务的报告入口；内容由任务产出，这里只做索引，不做二次判定。</div>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/')}><Icon name="upload" size={16} /> 新建审查</button>
      </div>

      <div className="rp-stats">
        <div className="rp-stat"><b>{done.length}</b><span>份已完成报告</span></div>
        <div className="rp-stat"><b>{stat.risk}</b><span>处可见风险</span></div>
        <div className="rp-stat"><b>{stat.pages}</b><span>页已渲染</span></div>
        <div className="rp-stat"><b>{stat.coverage}%</b><span>多模态复核覆盖率</span></div>
      </div>

      {error && <div className="rp-note"><Icon name="alert" size={14} /> 读取任务失败：{error}</div>}

      {items === null ? (
        <div className="stack" style={{ marginTop: 16 }}><Skeleton height={86} /><Skeleton height={86} /></div>
      ) : done.length === 0 ? (
        <div style={{ marginTop: 16 }}>
          <EmptyState icon="file" title="还没有已完成的报告" hint="回到工作台上传一份 PPT，跑完审查后这里会按时间倒序列出报告入口。" />
        </div>
      ) : (
        <div className="rp-list">
          {done.map((task) => {
            const byType = task.summary?.by_type ?? {}
            const total = DEFECTS.reduce((sum, defect) => sum + (byType[defect.key] ?? 0), 0)
            const top = DEFECTS
              .map((defect) => ({ ...defect, count: byType[defect.key] ?? 0 }))
              .filter((item) => item.count > 0)
              .sort((a, b) => b.count - a.count)
            return (
              <article className="rp-row" key={task.id}>
                <div className="rp-main">
                  <b>{task.name}</b>
                  <span>
                    {formatDate(task.finished_at ?? task.created_at)} · {task.slide_count ?? '—'} 页
                    {task.aspect_label ? ` · ${task.aspect_label}` : ''}
                    {task.mode === 'demo' ? ' · 演示任务' : ''}
                  </span>
                </div>

                <div className="rp-mix">
                  {total > 0 ? (
                    <>
                      <div className="rp-bar" role="img" aria-label="风险类型分布">
                        {top.map((item) => (
                          <i key={item.key} style={{ flexGrow: item.count, background: item.color }} title={`${item.label} ${item.count}`} />
                        ))}
                      </div>
                      <div className="rp-legend">
                        {top.slice(0, 3).map((item) => (
                          <span key={item.key}><i style={{ background: item.color }} />{item.label} {item.count}</span>
                        ))}
                        {top.length > 3 && <span className="rp-legend-more">+{top.length - 3} 类</span>}
                      </div>
                    </>
                  ) : (
                    <span className="rp-clean"><Icon name="check" size={13} /> 未发现可见风险</span>
                  )}
                </div>

                <div className="rp-num">
                  <b>{task.summary?.visible_issue_count ?? 0}</b>
                  <i>处风险</i>
                </div>

                <div className="rp-act">
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => go(task, 'results')}>
                    <Icon name="eye" size={14} /> 页面结果
                  </button>
                  <button type="button" className="btn btn-sm" onClick={() => go(task, 'report')}>
                    <Icon name="file" size={14} /> 打开报告
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { DEFECTS, type RepairProject } from '../lib/types'
import { EmptyState, Skeleton } from '../components/Ui'
import { Icon } from '../components/Icon'
import { formatDate } from '../lib/format'

const STATUS_TEXT: Record<string, string> = {
  queued: '排队中', running: '修复中', completed: '已修复', failed: '修复失败',
}

export default function Repair() {
  const navigate = useNavigate()
  const [items, setItems] = useState<RepairProject[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api.repairProjects('real')
      .then((res) => { if (alive) setItems(res.items ?? []) })
      .catch((err) => { if (alive) { setError((err as Error).message); setItems([]) } })
    return () => { alive = false }
  }, [])

  const stat = useMemo(() => {
    const list = items ?? []
    const done = list.filter((item) => item.repair?.status === 'completed')
    return {
      projects: list.length,
      done: done.length,
      applied: done.reduce((sum, item) => sum + (item.repair?.summary.applied ?? 0), 0),
      manual: done.reduce((sum, item) => sum + (item.repair?.summary.manual ?? 0), 0),
    }
  }, [items])

  const enter = (project: RepairProject) => {
    navigate(`/repair/${project.task.id}`)
  }

  const openResult = (project: RepairProject) => {
    if (!project.repair) return
    navigate(`/repair/${project.task.id}/result?rid=${project.repair.repair_id}`)
  }

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <span className="eyebrow">AI repair</span>
          <h1 style={{ marginTop: 10 }}>AI 修复</h1>
          <div className="lead">
            选一份已完成的审查，确认报告后一键生成修复副本。修改在副本上执行，原始文件与审查记录都不改动。
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => navigate('/')}><Icon name="upload" size={16} /> 新建审查</button>
      </div>

      <div className="rp-stats repair-stats">
        <div className="rp-stat"><b>{stat.projects}</b><span>份可修复文稿</span></div>
        <div className="rp-stat"><b>{stat.done}</b><span>次已完成修复</span></div>
        <div className="rp-stat"><b>{stat.applied}</b><span>条自动修复</span></div>
        <div className="rp-stat"><b>{stat.manual}</b><span>条需人工处理</span></div>
      </div>

      {error && <div className="rp-note"><Icon name="alert" size={14} /> 读取修复列表失败：{error}</div>}

      {items === null ? (
        <div className="stack" style={{ marginTop: 16 }}><Skeleton height={92} /><Skeleton height={92} /></div>
      ) : items.length === 0 ? (
        <div style={{ marginTop: 16 }}>
          <EmptyState icon="file" title="还没有可修复的文稿" hint="先上传一份 PPT 跑完审查，这里会列出可以一键修复的项目。" />
        </div>
      ) : (
        <div className="rp-list">
          {items.map((project) => {
            const task = project.task
            const repair = project.repair
            const byType = task.summary?.by_type ?? {}
            const total = DEFECTS.reduce((sum, defect) => sum + (byType[defect.key] ?? 0), 0)
            const top = DEFECTS
              .map((defect) => ({ ...defect, count: byType[defect.key] ?? 0 }))
              .filter((item) => item.count > 0)
              .sort((a, b) => b.count - a.count)
            const running = repair && ['queued', 'running'].includes(repair.status)
            return (
              <article className="rp-row repair-row" key={task.id}>
                <div className="rp-main">
                  <b>{task.name}</b>
                  <span>
                    {formatDate(task.finished_at ?? task.created_at)} · {task.slide_count ?? '—'} 页
                    {task.mode === 'demo' ? ' · 演示任务' : ''}
                  </span>
                  <span className={`repair-state ${repair?.status ?? 'idle'}`}>
                    <i />
                    {repair ? STATUS_TEXT[repair.status] ?? repair.status : '未修复'}
                    {repair?.status === 'completed' && repair.verify
                      ? ` · 结构问题 ${repair.verify.before_structure}→${repair.verify.after_structure}`
                      : ''}
                  </span>
                </div>

                <div className="rp-mix">
                  {total > 0 ? (
                    <>
                      <div className="rp-bar" role="img" aria-label="缺陷类型分布">
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
                    <span className="rp-clean"><Icon name="check" size={13} /> 未发现可见缺陷</span>
                  )}
                </div>

                <div className="rp-num">
                  <b>{task.summary?.visible_issue_count ?? 0}</b>
                  <i>处缺陷</i>
                </div>

                <div className="rp-act">
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate(`/tasks/${task.id}/report`)}>
                    <Icon name="file" size={14} /> 报告
                  </button>
                  {repair?.status === 'completed' ? (
                    <button type="button" className="btn btn-sm" onClick={() => openResult(project)}>
                      <Icon name="eye" size={14} /> 查看结果
                    </button>
                  ) : running ? (
                    <button type="button" className="btn btn-sm" onClick={() => repair && navigate(`/repair/${task.id}/progress?rid=${repair.repair_id}`)}>
                      <Icon name="spinner" size={14} /> 查看进度
                    </button>
                  ) : (
                    <button type="button" className="btn btn-primary btn-sm" onClick={() => enter(project)}>
                      <Icon name="spark" size={14} /> 进入修复
                    </button>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

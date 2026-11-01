import { useEffect, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { formatDate } from '../lib/format'
import type { TaskSummary } from '../lib/types'
import { Icon } from './Icon'

const NAV = [
  { to: '/', label: '工作台', icon: 'home' as const, end: true },
  { to: '/tasks', label: '审查任务', icon: 'folder' as const, end: false },
  { to: '/reports', label: '报告中心', icon: 'file' as const, end: false },
  { to: '/repair', label: 'AI 修复', icon: 'spark' as const, end: false },
  { to: '/settings', label: '系统设置', icon: 'settings' as const, end: false },
]

const DONE = new Set(['completed', 'completed_with_errors'])

function dotTone(status: string) {
  if (status === 'completed') return 'ok'
  if (status === 'completed_with_errors') return 'warn'
  if (status === 'failed') return 'bad'
  if (status === 'running' || status === 'preparing' || status === 'queued') return 'live'
  return 'idle'
}

type Props = {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: Props) {
  const navigate = useNavigate()
  const location = useLocation()
  const [tasks, setTasks] = useState<TaskSummary[]>([])

  // 跟随路由刷新，任务跑完后侧栏的「最近任务 / 本机数据」能立刻更新
  useEffect(() => {
    let alive = true
    api.listTasks('real')
      .then((res) => { if (alive) setTasks(res.items ?? []) })
      .catch(() => { if (alive) setTasks([]) })
    return () => { alive = false }
  }, [location.pathname])

  // 侧栏放三条：再多会和底部「本机数据 / 插画 / 本地工作区」抢高度，
  // 窗口矮的时候直接叠在一起。完整列表去「审查任务」页看。
  // 空间不足时由 CSS 先隐藏第三行，不靠 JS 猜窗口高度。
  const recent = tasks.slice(0, 3)
  const done = tasks.filter((task) => DONE.has(task.status))
  const riskTotal = done.reduce((sum, task) => sum + (task.summary?.visible_issue_count ?? 0), 0)
  const lastDone = done[0]?.finished_at ?? done[0]?.created_at ?? null

  const openTask = (task: TaskSummary) => {
    const view = DONE.has(task.status) ? 'results'
      : ['failed', 'queued', 'running'].includes(task.status) ? 'progress' : 'configure'
    navigate(`/tasks/${task.id}/${view}`)
  }

  return (
    <aside className="rail" aria-label="主导航">
      <div className="rail-head">
        <div className="rail-brand">
          <span className="rail-mark"><Icon name="logo" size={19} /></span>
          <span className="rail-brand-txt">
            <b>SlideSentinel</b>
            <i>AI PPT 审查智能体</i>
          </span>
        </div>
        <button
          type="button"
          className="rail-toggle"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
          title={collapsed ? '展开侧栏' : '收起侧栏'}
        >
          <Icon name="chevron" size={15} />
        </button>
      </div>

      <nav className="rail-nav">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            title={collapsed ? item.label : undefined}
            className={({ isActive }) => `rail-link${isActive ? ' active' : ''}`}
          >
            <Icon name={item.icon} size={18} />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>

      {recent.length > 0 && (
        <section className="rail-sec">
          <div className="rail-sec-head">
            <span>最近任务</span>
            <NavLink to="/tasks" className="rail-sec-more" title="全部任务">全部</NavLink>
          </div>
          <div className="rail-task-list">
            {recent.map((task) => (
              <button key={task.id} type="button" className="rail-task" onClick={() => openTask(task)}
                title={`${task.name}${task.slide_count ? ` · ${task.slide_count} 页` : ''}`}>
                <i className={`rail-dot ${dotTone(task.status)}`} />
                <b>{task.name}</b>
              </button>
            ))}
          </div>
        </section>
      )}

      {tasks.length > 0 && (
        <section className="rail-sec rail-sec-meta">
          <div className="rail-sec-head"><span>本机数据</span></div>
          <div className="rail-meta">
            <div><b>{tasks.length}</b><i>个任务</i></div>
            <div><b>{riskTotal}</b><i>处风险</i></div>
          </div>
          <div className="rail-since">最近完成 · {formatDate(lastDone)}</div>
        </section>
      )}

      {/* 两段弹性留白按 2:1 分配剩余高度，插画因此略靠下、贴近底部工作区，
          而不是悬在空白正中间显得上面空一截 */}
      <span className="rail-gap rail-gap-top" aria-hidden="true" />

      <div className="rail-art" aria-hidden="true">
        <div className="la-stage">
          <span className="la-card la-c1" />
          <span className="la-card la-c2" />
          <span className="la-card la-c3"><i>P</i><b>PPTX</b></span>
        </div>
      </div>

      <span className="rail-gap rail-gap-bottom" aria-hidden="true" />

      <div className="rail-user">
        <span className="rail-ava">U</span>
        <span className="rail-user-txt">
          <b>本地工作区</b>
          <i>数据保存在本机</i>
        </span>
      </div>
    </aside>
  )
}

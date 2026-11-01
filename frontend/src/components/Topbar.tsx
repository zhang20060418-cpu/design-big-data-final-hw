import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import type { TaskSummary } from '../lib/types'
import { STATUS_LABEL } from '../lib/types'
import { formatDate } from '../lib/format'
import { Icon } from './Icon'

const PAGES = [
  { to: '/', label: '工作台', hint: '上传、能力总览与最近任务' },
  { to: '/tasks', label: '审查任务', hint: '全部任务、演示任务与删除' },
  { to: '/reports', label: '报告中心', hint: '已完成任务的报告入口汇总' },
  { to: '/settings', label: '系统设置', hint: '模型接入、接口连通性与隐私开关' },
]

export function Topbar() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [query, setQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [bellOpen, setBellOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    api.listTasks().then((res) => setTasks(res.items)).catch(() => setTasks([]))
  }, [])

  useEffect(() => {
    setSearchOpen(false)
    setBellOpen(false)
    setMenuOpen(false)
  }, [pathname])

  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setSearchOpen(false)
        setBellOpen(false)
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        // 顶栏可能已经随滚动收起，先让它滑回来再聚焦
        window.dispatchEvent(new Event('slidesentinel:topbar-show'))
        inputRef.current?.focus()
        setSearchOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const keyword = query.trim().toLowerCase()
  const pageHits = PAGES.filter((page) => !keyword || page.label.toLowerCase().includes(keyword))
  const taskHits = useMemo(() => {
    const matched = keyword
      ? tasks.filter((task) => `${task.name} ${task.source_name}`.toLowerCase().includes(keyword))
      : tasks
    return matched.slice(0, 4)
  }, [tasks, keyword])

  const goFirst = () => {
    if (pageHits.length > 0) {
      navigate(pageHits[0].to)
      setQuery('')
      setSearchOpen(false)
      return
    }
    if (taskHits.length > 0) {
      navigate(`/tasks/${taskHits[0].id}/results`)
      setQuery('')
      setSearchOpen(false)
    }
  }

  const finished = tasks.filter((task) => task.status === 'completed' || task.status === 'completed_with_errors').slice(0, 3)
  const finishedToday = finished.some((task) => {
    if (!task.finished_at) return false
    return Date.now() - new Date(task.finished_at).getTime() < 86_400_000
  })

  return (
    <header className="topbar" ref={rootRef}>
      <div className="tb-search">
        <div className={`tb-search-box${searchOpen ? ' open' : ''}`}>
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            value={query}
            aria-label="搜索任务、报告或规则"
            placeholder="搜索任务、报告或规则…"
            onFocus={() => { setSearchOpen(true); setBellOpen(false); setMenuOpen(false) }}
            onChange={(event) => { setQuery(event.target.value); setSearchOpen(true) }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') goFirst()
              if (event.key === 'Escape') { setSearchOpen(false); event.currentTarget.blur() }
            }}
          />
          {query ? (
            <button type="button" className="tb-clear" aria-label="清空搜索" onClick={() => setQuery('')}>
              <Icon name="x" size={13} />
            </button>
          ) : <kbd>Ctrl K</kbd>}
        </div>

        {searchOpen && (
          <div className="tb-pop tb-results">
            {pageHits.length > 0 && <div className="tb-group">页面</div>}
            {pageHits.map((page) => (
              <button key={page.to} type="button" className="tb-hit"
                onClick={() => { navigate(page.to); setQuery(''); setSearchOpen(false) }}>
                <Icon name="arrow" size={14} />
                <span><b>{page.label}</b><i>{page.hint}</i></span>
              </button>
            ))}
            {taskHits.length > 0 && <div className="tb-group">最近任务</div>}
            {taskHits.map((task) => (
              <button key={task.id} type="button" className="tb-hit"
                onClick={() => {
                  navigate(task.status === 'ready' || task.status === 'draft' || task.status === 'preparing'
                    ? `/tasks/${task.id}/configure`
                    : task.status === 'queued' || task.status === 'running'
                      ? `/tasks/${task.id}/progress`
                      : `/tasks/${task.id}/results`)
                  setSearchOpen(false)
                }}>
                <Icon name="file" size={14} />
                <span><b>{task.name}</b><i>{STATUS_LABEL[task.status]} · {task.slide_count ?? '—'} 页</i></span>
              </button>
            ))}
            {pageHits.length === 0 && taskHits.length === 0 && (
              <div className="tb-empty">没有匹配的任务或页面。试试任务名、文件名或「设置」。</div>
            )}
            <div className="tb-foot">Enter 打开第一项 · Esc 关闭</div>
          </div>
        )}
      </div>

      <div className="tb-right">
        <div className="tb-slot">
          <button type="button" className={`tb-icon${bellOpen ? ' on' : ''}`} aria-label="通知" aria-expanded={bellOpen}
            onClick={() => { setBellOpen((v) => !v); setMenuOpen(false); setSearchOpen(false) }}>
            <Icon name="bell" size={18} />
            {finishedToday && <i className="tb-dot" />}
          </button>

          {bellOpen && (
            <div className="tb-pop tb-bell">
              <div className="tb-pop-head">通知</div>
              {finished.length === 0
                ? <div className="tb-empty">还没有完成的任务。审查结束后，这里会显示结果入口。</div>
                : finished.map((task) => (
                  <button key={task.id} type="button" className="tb-hit"
                    onClick={() => { navigate(`/tasks/${task.id}/results`); setBellOpen(false) }}>
                    <Icon name="check" size={14} />
                    <span><b>{task.name}</b><i>{STATUS_LABEL[task.status]} · {formatDate(task.finished_at)}</i></span>
                  </button>
                ))}
              <div className="tb-foot">通知只来自本机已完成的任务</div>
            </div>
          )}
        </div>

        <div className="tb-slot">
          <button type="button" className={`tb-user${menuOpen ? ' on' : ''}`} aria-expanded={menuOpen}
            onClick={() => { setMenuOpen((v) => !v); setBellOpen(false); setSearchOpen(false) }}>
            <span className="tb-ava">U</span>
            <span className="tb-user-txt">本地工作区</span>
            <Icon name="chevron" size={14} />
          </button>

          {menuOpen && (
            <div className="tb-pop tb-menu">
              <div className="tb-pop-head">本地工作区</div>
              <div className="tb-menu-note">任务、渲染图与报告都保存在 <code>backend/data</code>；启用 AI 复核时会向所配置的模型服务发送选中的页面内容。</div>
              <button type="button" className="tb-hit" onClick={() => { navigate('/tasks'); setMenuOpen(false) }}>
                <Icon name="folder" size={14} /><span><b>任务记录</b><i>查看已上传的文稿</i></span>
              </button>
              <button type="button" className="tb-hit" onClick={() => { navigate('/settings'); setMenuOpen(false) }}>
                <Icon name="settings" size={14} /><span><b>系统设置</b><i>模型接入与隐私开关</i></span>
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}

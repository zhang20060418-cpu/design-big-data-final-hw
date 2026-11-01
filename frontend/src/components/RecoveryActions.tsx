import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import type { RecoveryInfo, RecoveryLineage, TaskStatus } from '../lib/types'
import { Icon } from './Icon'
import { toast } from './Toasts'

// The phase and footer may both expose recovery. A shared guard prevents their
// two buttons from creating separate attempts while the same request is pending.
const pending = new Set<string>()
const listeners = new Set<() => void>()
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
const notify = () => listeners.forEach(listener => listener())
const ACTIVE = new Set<TaskStatus>(['draft', 'preparing', 'ready', 'queued', 'running'])
const STAGE_LABELS: Record<string, string> = {
  validate: '文件检查', parse: '解析', render: '页面渲染', structure: '首轮检测', visual: 'CLIP', mllm: '已成功复核',
}

type Props = {
  taskId: string
  status: TaskStatus
  mode?: 'real' | 'demo'
  compact?: boolean
  info?: RecoveryInfo
}

export function RecoveryLineageNotice({ lineage, mode = 'real' }: { lineage?: RecoveryLineage | null; mode?: 'real' | 'demo' }) {
  if (!lineage?.parent_task_id) return null
  return <div className="small muted" role="status" style={{ padding: '10px 14px', marginBottom: 12, border: '1px solid var(--line)', borderRadius: 10 }}>
    这是{lineage.action === 'restart' ? '重新审查' : '继续审查'}创建的新记录，原任务结果与历史保持保留。{' '}
    <Link to={`/tasks/${encodeURIComponent(lineage.parent_task_id)}/progress${mode === 'demo' ? '?mode=demo' : ''}`}>查看原任务</Link>
  </div>
}

export function RecoveryActions({ taskId, status, mode = 'real', compact = false, info: supplied }: Props) {
  const navigate = useNavigate()
  const [info, setInfo] = useState<RecoveryInfo | null>(supplied ?? null)
  const [error, setError] = useState('')
  const scope = `${mode}:${taskId}`
  const mountedScope = useRef<string | null>(scope)
  const busy = useSyncExternalStore(subscribe, () => pending.has(scope))
  const active = ACTIVE.has(status)

  useEffect(() => {
    mountedScope.current = scope
    return () => { mountedScope.current = null }
  }, [scope])

  useEffect(() => {
    let alive = true
    setError(''); setInfo(supplied ?? null)
    if (!active && !supplied) api.recovery(taskId, mode)
      .then(next => { if (alive) setInfo(next) })
      .catch(err => { if (alive) setError((err as Error).message) })
    return () => { alive = false }
  }, [taskId, mode, status, active, supplied])

  const recover = async (action: 'continue' | 'restart') => {
    if (pending.has(scope) || active) return
    pending.add(scope); notify(); setError('')
    try {
      const result = await api.recover(taskId, action, mode)
      if (!result.task_id || result.task_id === taskId) throw new Error('恢复接口未返回新的审查任务，原结果未改写。')
      if (mountedScope.current !== scope) return
      toast(result.message || '已创建新的审查记录，原结果保持不变。', 'success')
      navigate(`/tasks/${result.task_id}/progress${result.mode === 'demo' ? '?mode=demo' : ''}`)
    } catch (err) {
      if (mountedScope.current === scope) setError((err as Error).message)
    } finally { pending.delete(scope); notify() }
  }

  if (active) return null
  if (!info) return compact ? null : <div className="small muted" role="status">{error ? `暂时无法读取可恢复范围：${error}` : '正在检查可恢复范围…'}</div>
  if (!info.available || (!info.can_continue && !info.can_restart)) return compact ? null : (
    <div className="small muted" role="status">{info.reason || '当前任务没有可恢复的步骤。'}</div>
  )

  const showRestart = info.can_restart && (!compact || !info.can_continue)
  return (
    <div aria-label="审查恢复操作" onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}
      style={compact ? { display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'flex-end' } : { padding: '12px 0', width: '100%' }}>
      {!compact && <p className="small muted" style={{ margin: '0 0 9px' }}>
        继续审查会复用已验证步骤，只补原计划中失败、跳过或中断的范围；重新审查会从头运行。两者均创建新记录，保留原结果。
        {info.additional_api_calls_possible && <span> 可能新增 LLM API 调用，仍受新任务预算限制。</span>}
      </p>}
      {!compact && info.reason && <p className="small muted" style={{ margin: '0 0 9px' }}>{info.reason}</p>}
      {!compact && !!info.reused_stages?.length && <p className="tiny muted" style={{ margin: '0 0 9px' }}>
        可复用：{info.reused_stages.map(stage => STAGE_LABELS[stage] || stage).join('、')}
        {typeof info.pending_review_calls === 'number' ? `；待补复核 ${info.pending_review_calls} 项` : ''}。
      </p>}
      <div className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: compact ? 'flex-end' : undefined }}>
        {info.can_continue && <button className="btn btn-sm" disabled={busy}
          title="创建新的审查记录，只补未完成范围；可能新增API调用，不改写原结果。"
          onClick={() => void recover('continue')}>
          <Icon name={busy ? 'spinner' : 'play'} size={14} />{busy ? '正在创建…' : info.continue_label || '继续审查'}
        </button>}
        {showRestart && <button className="btn btn-ghost btn-sm" disabled={busy}
          title="创建新记录并按任务配置从头检查，可能再次调用API。"
          onClick={() => void recover('restart')}>
          <Icon name="refresh" size={14} />重新审查
        </button>}
      </div>
      {!compact && info.warnings?.map((warning, i) => <p className="tiny" key={i} style={{ color: 'var(--orange)', marginTop: 7 }}>{warning}</p>)}
      {error && <div className="small" role="alert" style={{ marginTop: 7, color: 'var(--red)' }}>{error}</div>}
    </div>
  )
}

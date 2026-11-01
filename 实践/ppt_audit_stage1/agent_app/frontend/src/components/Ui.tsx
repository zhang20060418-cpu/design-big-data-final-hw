import type { ReactNode } from 'react'
import { Icon } from './Icon'
import { STATUS_LABEL, type StageStatus, type TaskStatus } from '../lib/types'

export function StatusChip({ status }: { status: TaskStatus }) {
  const cls =
    status === 'completed' ? 'green'
      : status === 'completed_with_errors' ? 'orange'
        : status === 'failed' ? 'red'
          : status === 'running' || status === 'queued' || status === 'preparing' ? 'indigo'
            : 'gray'
  return (
    <span className={`chip ${cls}`}>
      <i className="dot" />
      {STATUS_LABEL[status] || status}
    </span>
  )
}

export function StageState({ status }: { status: StageStatus }) {
  const map: Record<StageStatus, string> = {
    pending: 'gray', running: 'indigo', done: 'green',
    skipped: 'gray', unsupported: 'gray', failed: 'red',
  }
  const label: Record<StageStatus, string> = {
    pending: '等待', running: '运行中', done: '完成',
    skipped: '跳过', unsupported: '未接入', failed: '失败',
  }
  return <span className={`badge ${map[status]}`}>{label[status]}</span>
}

export function EmptyState({ icon = 'file', title, hint }: { icon?: 'file' | 'image' | 'history'; title: string; hint?: string }) {
  return (
    <div className="empty">
      <div className="icon"><Icon name={icon} size={30} /></div>
      <div style={{ fontWeight: 600, color: 'var(--ink-2)' }}>{title}</div>
      {hint && <div style={{ marginTop: 4 }}>{hint}</div>}
    </div>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Icon name="spinner" size={size} style={{ animation: 'spin 1.1s linear infinite' }} />
}

export function Skeleton({ height = 16, width = '100%', radius = 6 }: { height?: number | string; width?: number | string; radius?: number }) {
  return <div className="skeleton" style={{ height, width, borderRadius: radius }} />
}

export function SectionHead({ kicker, title, aside }: { kicker?: string; title: string; aside?: ReactNode }) {
  return (
    <div className="section-head">
      <div>
        {kicker && <div className="kicker">{kicker}</div>}
        <div className="h2">{title}</div>
      </div>
      {aside}
    </div>
  )
}

export function Toggle({ checked, onChange, disabled, label }: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  )
}

import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { RepairRecord } from '../lib/types'
import { Icon } from '../components/Icon'
import { toast } from '../components/Toasts'

const STAGE_ICON: Record<string, 'check' | 'spinner' | 'dot' | 'x'> = {
  done: 'check', active: 'spinner', pending: 'dot', failed: 'x',
}

export default function RepairProgress() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  let repairId = params.get('rid') || ''
  const navigate = useNavigate()
  const [record, setRecord] = useState<RepairRecord | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(false)
  const redirected = useRef(false)
  const latestReview = useRef(false)

  useEffect(() => {
    let alive = true
    let timer = 0
    const tick = async () => {
      try {
        if (!repairId) {
          const list = await api.repairs(taskId, mode)
          repairId = list.items?.[0]?.repair_id ?? ''
        }
        if (!repairId) throw new Error('没有找到修复记录。')
        const current = await api.repair(taskId, repairId, mode)
        if (!alive) return
        setRecord(current)
        latestReview.current = !!current.review?.enabled
        if (current.status === 'completed' && !redirected.current) {
          redirected.current = true
          timer = window.setTimeout(() => {
            navigate(`/repair/${taskId}/result?rid=${current.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`, { replace: true })
          }, 900)
          return
        }
        if (current.status === 'failed') return
        timer = window.setTimeout(tick, 1100)
      } catch (err) {
        if (alive) setError((err as Error).message)
      }
    }
    void tick()
    return () => { alive = false; window.clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, mode, retry])

  const again = async () => {
    try {
      const record = await api.startRepair(taskId, 'api', mode, latestReview.current)
      redirected.current = false
      setRetry((v) => !v)
      navigate(`/repair/${taskId}/progress?rid=${record.repair_id}${mode === 'demo' ? '&mode=demo' : ''}`, { replace: true })
    } catch (err) {
      toast((err as Error).message, 'error')
    }
  }

  if (error) {
    return (
      <div className="card" style={{ padding: 24 }}>
        <div className="row" style={{ fontWeight: 700, color: 'var(--orange)' }}><Icon name="alert" size={18} /> 修复记录读取失败</div>
        <p className="small muted" style={{ marginTop: 6 }}>{error}</p>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn btn-sm" onClick={() => navigate(`/repair/${taskId}`)}>返回修复页</button>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/repair')}>AI 修复列表</button>
        </div>
      </div>
    )
  }

  if (!record) {
    return (
      <div className="card repair-progress-card">
        <div className="row" style={{ gap: 10 }}><Icon name="spinner" size={18} /> <b>正在连接修复进程…</b></div>
      </div>
    )
  }

  const percent = Math.round((record.progress || 0) * 100)
  const failed = record.status === 'failed'
  const activeStage = record.stages.find((stage) => stage.status === 'active')

  return (
    <div className="fade-up">
      <div className="page-head" style={{ marginBottom: 12 }}>
        <div>
          <span className="eyebrow">AI repair · running</span>
          <h1 style={{ marginTop: 10 }}>{failed ? '修复中断' : record.status === 'completed' ? '修复完成' : '正在修复这份文稿'}</h1>
          <div className="lead">{record.message}</div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => navigate(`/repair/${taskId}`)}>
          <Icon name="left" size={15} /> 返回修复页
        </button>
      </div>

      <div className={`card repair-progress-card${failed ? ' failed' : ''}`}>
        <div className="repair-progress-head">
          <div>
            <span>当前阶段</span>
            <b>{failed ? '已停止' : activeStage?.label ?? (record.status === 'completed' ? '全部完成' : '准备中')}</b>
          </div>
          <div className="repair-progress-num">{percent}<i>%</i></div>
        </div>

        <div className="repair-bar" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
          <i style={{ width: `${Math.max(3, percent)}%` }} />
        </div>

        <div className="repair-steps">
          {record.stages.map((stage) => (
            <div className={`repair-step ${stage.status}`} key={stage.key}>
              <span className="mark"><Icon name={STAGE_ICON[stage.status]} size={13} /></span>
              <b>{stage.label}</b>
            </div>
          ))}
        </div>

        {record.review?.enabled && (
          <div className="repair-rounds">
            <div className="repair-rounds-head">
              <b>LLM 复核</b>
              <span>
                {record.review.rounds.length > 0
                  ? `已复核 ${record.review.rounds.length} / 最多 ${record.review.max_rounds} 轮`
                  : `最多 ${record.review.max_rounds} 轮，正在执行第 1 轮`}
              </span>
            </div>
            <div className="repair-rounds-list">
              {record.review.rounds.map((item) => (
                <div className="repair-round" key={item.round}>
                  <b>第 {item.round} 轮</b>
                  <span>结构问题 {item.before} → {item.after}</span>
                  <i>{item.applied} 条改动</i>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="repair-log">
          {record.log.slice(-7).map((line, index) => (
            <div key={`${line.at}-${index}`}>
              <span>{line.at.slice(11, 19)}</span>
              <p>{line.text}</p>
            </div>
          ))}
          {record.log.length === 0 && <div className="tiny muted">等待第一条进度…</div>}
        </div>

        {failed && (
          <div className="repair-fail">
            <Icon name="alert" size={16} />
            <div>
              <b>这次修复没有完成</b>
              <p>{record.message}</p>
            </div>
            <button className="btn btn-sm" onClick={() => void again()}>重新修复</button>
          </div>
        )}

        <p className="tiny muted repair-progress-foot">
          修复在副本上执行，原始 PPT、审查报告与复核记录都不会被改动。离开本页不会中断进程，可从「AI 修复」列表回到这里。
        </p>
      </div>
    </div>
  )
}

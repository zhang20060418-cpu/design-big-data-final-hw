import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { Claim, ClipReview, Issue, ReviewGate, ReviewOptions, ReviewPlanItem, ReviewRecord, VerificationPipeline, VisualResult } from '../lib/types'
import { DEFECTS, DEFECT_LABEL, DETECTION_STATUS_LABEL } from '../lib/types'
import { claimStatus, displayEvidence, relatedPages } from '../lib/review'
import { formatDate } from '../lib/format'

const clipAlert = (value: boolean | number | null | undefined) => value === true || value === 1 ? '达到冻结阈值' : value === false || value === 0 ? '未达到阈值' : '未取得判断'
const clipValue = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : '未取得'

export function ClipResultPanel({ result, enabled, title = '本页' }: { result?: VisualResult | null; enabled?: boolean; title?: string }) {
  if (enabled === false) return <p className="tiny muted" style={{ margin: '8px 0' }}>CLIP 已关闭，本页未进行四标签辅助复核。</p>
  const available = result?.status === 'ok'
  return <details className="review-detail" aria-label={`${title}CLIP辅助证据`}>
    <summary>{title} CLIP 四标签辅助证据 · {available ? '已取得模型输出' : result ? '未取得有效输出' : '未取得执行记录'}</summary>
    <p className="tiny muted" style={{ margin: '7px 0', lineHeight: 1.65 }}>仅补充前四类，不判断留白或异常设计风险；低分不取消 R2 候选，高分也不等于最终确认。</p>
    {available ? <>
      <div className="tiny muted">模型：{result.model_version || '未记录'} · 冻结工作点：{result.work_point || '未记录'}</div>
      <div style={{ overflowX: 'auto', marginTop: 6 }}>
        <table style={{ width: '100%', fontSize: 10.5, borderCollapse: 'collapse', textAlign: 'left' }}>
          <thead><tr><th>标签</th><th>模型正类概率</th><th>冻结阈值</th><th>辅助告警</th></tr></thead>
          <tbody>{DEFECTS.slice(0, 4).map((defect) => <tr key={defect.key} style={{ borderTop: '1px solid var(--line)' }}><td style={{ padding: '6px 2px' }}>{defect.short}</td><td title={String(result.probabilities?.[defect.key] ?? '')}>{clipValue(result.probabilities?.[defect.key])}</td><td title={String(result.thresholds?.[defect.key] ?? '')}>{clipValue(result.thresholds?.[defect.key])}</td><td>{clipAlert(result.alerts?.[defect.key])}</td></tr>)}</tbody>
        </table>
      </div>
      <p className="tiny muted" style={{ marginTop: 5 }}>模型概率未经本任务场景重新校准，不代表实测缺陷发生率。</p>
    </> : <p className="small muted">{result?.error || '该页缺少可验证的 CLIP 输出，不能据此视为低风险。'}</p>}
    {result?.model_revision && <div className="tiny muted" style={{ overflowWrap: 'anywhere' }}>CLIP 权重版本：{result.model_revision}</div>}
    {result?.limitations?.length ? <p className="tiny muted">适用范围：{result.limitations.join('；')}</p> : null}
  </details>
}

export function ClipIssueEvidence({ result, defectType }: { result: ClipReview; defectType: string }) {
  return <div className="issue-ai" aria-label="CLIP问题辅助复核">
    <b>CLIP 辅助复核：</b>{DEFECT_LABEL[defectType] ?? defectType} · {clipAlert(result.alert)}
    <div className="tiny" title={`概率 ${result.probability ?? '未取得'}；阈值 ${result.threshold ?? '未取得'}`}>模型正类概率 {clipValue(result.probability)} / 冻结阈值 {clipValue(result.threshold)}</div>
    <div className="tiny">工作点：{result.work_point || '未记录'} · {result.model_version || '版本未记录'}</div>
    <div className="tiny">仅为页面级补充，不否决 R2 候选，也不代替 LLM 复检。</div>
    {result.status !== 'ok' && <div className="tiny">该项执行状态：{result.status}，不能作为有效低风险结论。</div>}
  </div>
}

export function ReviewFlowNotice({ options, stages, planItems = [], verification, gate }: { options?: ReviewOptions; stages?: { key: string; status: string }[]; planItems?: ReviewPlanItem[]; verification?: VerificationPipeline; gate?: ReviewGate }) {
  const llmStage = stages?.find((stage) => stage.key === 'mllm')?.status
  const skipped = planItems.filter((item) => !item.selected || ['not_checked', 'error', 'failed'].includes(String(item.outcome))).length
  const fallbackNotice = !options || !options.enable_mllm || options.review_mode === 'off'
    ? '未完成最终 LLM 复检：本任务未启用 LLM 复检，或未保存启用记录。'
    : options.llm_backend === 'mock'
      ? '未完成真实 LLM 复检：本任务使用 Mock 模拟链路，模拟结果不是真实模型判断。'
      : ['failed', 'unsupported', 'skipped'].includes(llmStage ?? '')
        ? '未完成最终 LLM 复检：服务不可用、调用失败或本阶段未执行；原始候选继续保留。'
        : !options.max_review_pages || !options.max_api_calls || skipped > 0
          ? `未完成全部范围的最终 LLM 复检：${skipped > 0 ? `有 ${skipped} 项未选择、跳过或失败。` : '复核预算未允许调用。'}`
          : 'LLM 仅检查本次实际选中并成功执行的范围；任务结束不代表所有缺陷和未决范围都已确认。'
  const finalLabels: Record<VerificationPipeline['final_review_status'], string> = {
    not_requested: '未完成最终 LLM 复检：本任务未请求 LLM 复检。',
    not_checked: '未完成最终 LLM 复检：尚未取得有效复检记录。',
    mock_only: '未完成真实 LLM 复检：本次只有 Mock 模拟结果。',
    partial: '仅完成部分 LLM 复检；预算外、跳过或失败范围尚未完成。',
    reviewed_with_uncertainty: '预定范围已有复核记录，但仍有不确定结论，不能视为最终质量确认。',
    planned_scope_reviewed: '已复核本次预定范围，不等于全部页面和全部缺陷都已确认。',
    user_skipped: '用户跳过 LLM 复检：未完成最终复检，可能误诊或漏检。',
  }
  const notice = gate?.skipped_by_user ? finalLabels.user_skipped : verification ? finalLabels[verification.final_review_status] ?? fallbackNotice : fallbackNotice
  const stageLabels: Record<string, string> = { done: '已运行', complete: '已执行', partial: '部分完成', failed: '执行失败', unsupported: '能力不支持', not_checked: '未检查', skipped: '未执行', disabled: '未启用', pending: '待执行', running: '运行中' }
  return <section aria-label="本次复核流程" style={{ padding: '10px 14px', borderBottom: '1px solid var(--line)', background: 'var(--surface-2)' }}>
    <div className="small" style={{ fontWeight: 650 }}>{options?.enable_six_detectors ? 'R2 六类首筛' : 'R2 未启用或未记录（非完整三级流程）'} → {verification ? verification.clip_enabled ? 'CLIP 四标签辅助复核' : 'CLIP 关闭' : options?.enable_visual ? 'CLIP 四标签辅助复核' : 'CLIP 关闭'} → LLM 按需复检</div>
    <p className="tiny muted" style={{ margin: '5px 0 0', lineHeight: 1.65 }}>{notice}</p>
    {gate?.skipped_by_user && <p className="small" style={{ margin: '6px 0', color: 'var(--orange)' }}>{gate.warning || '跳过 LLM 复检可能导致误诊或漏检；当前结果仅为首轮检测候选，请谨慎使用。'}</p>}
    {verification && <>
      <div className="tiny muted" style={{ marginTop: 5 }}>CLIP 有效输出 {verification.clip_pages_scored} 页 / 失败 {verification.clip_pages_failed} 页；LLM 复核记录 {verification.llm_call_records} 条 / 真实模型调用 {verification.llm_actual_http_calls} 次；未复核 {verification.llm_unreviewed_items} 项 / 仍待核实 {verification.llm_uncertain_verdicts} 项。</div>
      {verification.note && <p className="tiny muted" style={{ margin: '5px 0' }}>{verification.note}</p>}
      <details className="tiny muted"><summary>实际执行顺序与范围</summary><ol style={{ paddingLeft: 18, margin: '5px 0' }}>{verification.ordered_layers.map((layer) => <li key={layer.key}>{layer.label}：{stageLabels[layer.status] ?? layer.status} · {displayEvidence(layer.scope)}</li>)}</ol></details>
    </>}
  </section>
}

export function ScopePanel({ claims, available }: { claims: Claim[]; available: boolean }) {
  return <section aria-label="六类检查范围" className="scope-panel">
    <div className="h3">本页六类检查范围</div>
    <p className="tiny muted">未发现候选仅适用于已取得的证据，不代表整页已确认正常。</p>
    {DEFECTS.map((defect) => {
      const entries = claims.filter((claim) => claim.defect_type === defect.key)
      const statuses = entries.length ? [...new Set(entries.map(claimStatus))] : ['not_checked' as const]
      return <div key={defect.key} className="scope-row">
        <span className="small">{defect.label}</span>
        <span className="scope-statuses">{statuses.map((status) => <span key={status} className={`badge ${status === 'candidate' ? 'mid' : status === 'no_candidate_in_available_scope' ? 'low' : 'off'}`}>{DETECTION_STATUS_LABEL[status]}</span>)}</span>
        {entries.some((entry) => entry.limitations?.length) && <details className="tiny muted scope-notes"><summary>可判范围与限制</summary>{[...new Set(entries.flatMap((entry) => entry.limitations ?? []))].join('；')}</details>}
      </div>
    })}
    {!available && <p className="tiny muted">当前任务没有可读取的六类范围记录；请结合已有问题和模块状态判断。</p>}
    <p className="tiny muted">异常设计风险不代表“AI生成率”；素材伪文字等语义伪影仍需进一步视觉核实。</p>
  </section>
}

export function PlanDetails({ items }: { items: ReviewPlanItem[] }) {
  if (!items.length) return null
  const tools: Record<string, string> = { multimodal_multi_page: '多模态跨页对照', multimodal_page_and_roi: '整页与局部区域复核', multimodal_full_page: '多模态整页复核' }
  return <details className="review-detail"><summary>为什么复核或跳过（{items.length}项）</summary>
    {items.map((item, index) => <div className="review-history-entry" key={item.candidate_id ?? index}>
      <div className="small"><b>{item.selected ? '已纳入复核计划' : '未纳入本次复核'}</b></div>
      <div className="tiny">{displayEvidence(item.selection_reason) || displayEvidence(item.skip_reason) || '未提供调度理由'}</div>
      {item.selected_tool && <div className="tiny muted">工具：{tools[item.selected_tool] ?? item.selected_tool}</div>}
      {item.evidence_requested != null && <div className="tiny muted">所需证据：{displayEvidence(item.evidence_requested)}</div>}
      {item.budget != null && <div className="tiny muted">预算：{displayEvidence(item.budget)}</div>}
      {item.execution_trace != null && <div className="tiny muted">执行记录：{displayEvidence(item.execution_trace)}</div>}
      {item.outcome != null && <div className="tiny muted">结果：{displayEvidence(item.outcome)}</div>}
    </div>)}
  </details>
}

export function RelatedSlides({ issue, count, onPage }: { issue: Issue; count: number; onPage: (index: number) => void }) {
  const pages = relatedPages(issue.related_slides, count)
  if (!pages.length) return null
  return <div className="row" style={{ flexWrap: 'wrap', gap: 5, marginTop: 8 }} aria-label="关联页面">
    <span className="tiny muted">关联页面：</span>
    {pages.map((page) => <button key={page} className="btn btn-sm" onClick={(event) => { event.stopPropagation(); onPage(page) }}>P{String(page).padStart(2, '0')}</button>)}
  </div>
}

function ReviewEntry({ record }: { record: ReviewRecord }) {
  const mock = record.mock || record.demo || record.backend === 'mock'
  const labels = { supported: '支持候选', rejected: '不支持候选', uncertain: '仍待核实' }
  return <div className="review-history-entry">
    <div className="row" style={{ flexWrap: 'wrap' }}><b className="small">{labels[record.judgement ?? 'uncertain']}</b>{mock && <span className="badge ai">Mock 模拟 · 非真实模型判断</span>}<span className="tiny muted">{formatDate(record.reviewed_at ?? record.created_at)}</span></div>
    {(record.question || record.user_question) && <div className="tiny">追问：{record.question ?? record.user_question}</div>}
    <div className="small" style={{ marginTop: 5 }}>{displayEvidence(record.evidence ?? record.rationale ?? record.reason) || '未提供判断依据'}</div>
    {record.recommendation && <div className="small">建议：{record.recommendation}</div>}
    <div className="tiny muted">{record.model ?? record.llm_model ?? record.model_name ?? '模型未记录'}{(record.rule_version || record.rule_versions) && ` · 规则 ${displayEvidence(record.rule_version ?? record.rule_versions)}`}</div>
    {record.error && <div className="tiny" style={{ color: 'var(--red)' }}>复核失败：{record.error}</div>}
    {record.execution_trace != null && <details><summary className="tiny muted">复核执行记录</summary><p className="tiny muted">{displayEvidence(record.execution_trace)}</p></details>}
  </div>
}

export function InteractiveReview({ taskId, issue, mode, backend, onUpdated }: {
  taskId: string; issue: Issue; mode: 'real' | 'demo'; backend: 'mock' | 'api'; onUpdated: (issue: Issue) => void
}) {
  const [question, setQuestion] = useState('')
  const [selectedBackend, setSelectedBackend] = useState(backend)
  const [history, setHistory] = useState<ReviewRecord[]>(issue.review_history ?? [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true
    setHistory(issue.review_history ?? [])
    api.issueReviews(taskId, issue.issue_id, mode).then((result) => { if (alive && Array.isArray(result.history)) setHistory(result.history) }).catch(() => undefined)
    return () => { alive = false }
  }, [taskId, issue.issue_id, mode, issue.review_history])
  const submit = async () => {
    if (!question.trim() || busy) return
    setBusy(true); setError('')
    try {
      const result = await api.reviewIssue(taskId, issue.issue_id, question.trim(), selectedBackend, mode)
      setHistory(Array.isArray(result.history) ? result.history : [...history, result.review])
      onUpdated(result.issue)
      setQuestion('')
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="interactive-review" aria-label="问题追问与复核" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
    <div className="h3">问题追问与复核</div>
    <p className="tiny muted">补充用途或设计意图后重新核实。原始证据及历史结论会保留，也可能继续待核实。</p>
    <label className="tiny" htmlFor={`question-${issue.issue_id}`}>你的问题或补充说明</label>
    <textarea id={`question-${issue.issue_id}`} className="input" rows={3} maxLength={4000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：这里的留白是有意的非对称构图，为什么仍被标记？" />
    <label className="tiny" htmlFor={`backend-${issue.issue_id}`}>本次复核方式</label>
    <select id={`backend-${issue.issue_id}`} className="input" value={selectedBackend} onChange={(event) => setSelectedBackend(event.target.value as 'mock' | 'api')} disabled={busy}><option value="api">真实模型服务（需已配置）</option><option value="mock">Mock 模拟复核</option></select>
    {selectedBackend === 'mock' && <p className="tiny" style={{ color: 'var(--orange)' }}>Mock 仅验证复核流程，不调用真实模型。</p>}
    {error && <p className="small" role="alert" style={{ color: 'var(--red)' }}>{error} 原有结论已保留。</p>}
    <button className="btn btn-primary btn-sm" style={{ marginTop: 8 }} onClick={() => void submit()} disabled={busy || !question.trim()}>{busy ? '正在复核…' : '提交问题并复核'}</button>
    <details style={{ marginTop: 10 }} open={history.length > 0}><summary className="small">复核历史（{history.length}）</summary>{history.map((record, index) => <ReviewEntry key={index} record={record} />)}</details>
  </section>
}

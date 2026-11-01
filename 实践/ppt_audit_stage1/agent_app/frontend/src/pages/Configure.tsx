import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import type { Capabilities, ReviewOptions, SlideSummary, TaskDetail } from '../lib/types'
import { DEFECTS } from '../lib/types'
import { Icon } from '../components/Icon'
import { EmptyState, Skeleton, StatusChip, Toggle } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatMb } from '../lib/format'
import { configuredReviewOptions } from '../lib/reviewOptions'

export default function Configure() {
  const { taskId = '' } = useParams()
  const [params] = useSearchParams()
  const mode = params.get('mode') === 'demo' ? 'demo' : 'real'
  const navigate = useNavigate()

  const [task, setTask] = useState<TaskDetail | null>(null)
  const [slides, setSlides] = useState<SlideSummary[] | null>(null)
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [options, setOptions] = useState<ReviewOptions | null>(null)
  const [name, setName] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const timer = useRef<number>()
  const initializedOptionsFor = useRef<string | null>(null)
  const activeTaskId = useRef(taskId)
  activeTaskId.current = taskId

  const poll = async () => {
    try {
      const next = await api.getTask(taskId, mode)
      if (activeTaskId.current !== taskId) return
      setTask(next)
      if (initializedOptionsFor.current !== taskId) {
        initializedOptionsFor.current = taskId
        setName(next.name)
        setOptions(next.options)
      } else {
        setName((prev) => prev || next.name)
        // Initialize once per task; polling never resets a user's switch choice.
        setOptions((prev) => prev ?? next.options)
      }
      if (next.document) {
        api.slides(taskId, mode).then((res) => setSlides(res.slides)).catch(() => undefined)
      }
      if (['draft', 'preparing', 'queued', 'running'].includes(next.status)) {
        timer.current = window.setTimeout(poll, 1500)
      }
    } catch (err) {
      setError((err as Error).message)
    }
  }

  useEffect(() => {
    void poll()
    api.capabilities().then(setCaps).catch(() => undefined)
    return () => window.clearTimeout(timer.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  const preparing = !task || ['draft', 'preparing'].includes(task.status)
  const parseFailed = task?.status === 'failed'
  const sixDetector = caps?.modules.six_detector
  const sixRelease = sixDetector?.version?.match(/(?:^|[-_])r(\d+)(?:$|[-_])/i)?.[1]
  const sixStateLabel = !caps ? '正在探测' : sixDetector?.state === 'ready' ? '入口可用' : sixDetector?.state === 'unsupported' ? '当前不支持' : sixDetector?.state === 'error' ? '暂不可用' : '状态未确认'
  const visualReady = caps?.modules.visual.available ?? false
  const mllmReady = caps?.modules.mllm.state === 'ready'
  const reviewAvailable = options?.llm_backend === 'mock' || mllmReady

  useEffect(() => {
    setOptions(previous => previous ? configuredReviewOptions(previous, caps ? caps.modules.mllm.state === 'ready' : null) : previous)
  }, [caps, options])

  const effective = useMemo<ReviewOptions | null>(() => {
    if (!options) return null
    return {
      ...options,
      enable_visual: options.enable_visual && (!caps || visualReady),
      // A delayed capability probe must not overwrite a saved/user choice.
      // The worker checks actual API configuration again before sending anything.
      enable_mllm: options.enable_mllm,
    }
  }, [options, visualReady, caps])

  const setOpt = <K extends keyof ReviewOptions>(key: K, value: ReviewOptions[K]) => {
    setOptions((prev) => {
      if (!prev) return prev
      if (key === 'enable_mllm') return { ...prev, enable_mllm: Boolean(value), mllm_user_choice: Boolean(value),
        ...(value && prev.review_mode === 'off' ? { review_mode: 'risk_only' as const } : {}) }
      if (key === 'review_mode' && value === 'off') return { ...prev, review_mode: 'off', enable_mllm: false, mllm_user_choice: false }
      return { ...prev, [key]: value }
    })
  }

  const start = async () => {
    if (!effective) return
    setStarting(true)
    try {
      await api.startTask(taskId, { ...effective, manual_pages: effective.review_mode === 'manual' ? effective.manual_pages : [] }, name)
      navigate(`/tasks/${taskId}/progress${mode === 'demo' ? '?mode=demo' : ''}`)
    } catch (err) {
      toast(err instanceof ApiError ? err.message : '无法开始审查。', 'error')
      setStarting(false)
    }
  }

  const discard = async () => {
    if (!window.confirm('放弃这次上传并删除临时文件？')) return
    try {
      await api.deleteTask(taskId, mode)
      navigate('/')
    } catch (err) {
      toast((err as Error).message, 'error')
    }
  }

  const toggleManual = (index: number) => {
    if (!options) return
    const pages = options.manual_pages.includes(index)
      ? options.manual_pages.filter((p) => p !== index)
      : [...options.manual_pages, index].sort((a, b) => a - b)
    setOpt('manual_pages', pages)
  }

  const modeLabel = { editable: '可编辑', mixed: '混合', image_only: '整页位图' } as const
  const doc = task?.document

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/')} style={{ marginBottom: 8, marginLeft: -8 }}>
            <Icon name="left" size={15} /> 返回工作台
          </button>
          <div className="row" style={{ gap: 10 }}>
            <h1>{preparing ? '正在准备预览' : task?.name || '审查配置'}</h1>
            {task && <StatusChip status={task.status} />}
            {mode === 'demo' && <span className="chip demo">演示任务</span>}
          </div>
          <div className="lead">{doc?.file_name || task?.source_name || '正在读取文件…'}</div>
        </div>
        {task && !preparing && (
          <div className="row">
            <button className="btn btn-ghost btn-sm" onClick={() => void discard()}>
              <Icon name="trash" size={15} /> 放弃
            </button>
          </div>
        )}
      </div>

      {parseFailed && (
        <div className="card" style={{ padding: 22, borderColor: '#f6c9cb', background: '#fffafa' }}>
          <div className="row" style={{ color: 'var(--red)', fontWeight: 700 }}>
            <Icon name="alert" size={18} /> 文件准备失败
          </div>
          <p className="small" style={{ marginTop: 6, color: 'var(--ink-2)' }}>{task?.error || '无法解析该文件。'}</p>
          <button className="btn btn-sm" style={{ marginTop: 12 }} onClick={() => navigate('/')}>重新选择文件</button>
        </div>
      )}

      {error && <div className="card" style={{ padding: 18, color: 'var(--red)' }}>{error}</div>}

      {!parseFailed && (
        <div className="cfg-split">
          <div className="card cfg-thumbs">
            <div className="cfg-thumbs-head">
              <span className="h3">幻灯片</span>
              <span className="tiny muted">{doc ? `${doc.slide_count} 页 · ${doc.aspect_label}` : '读取中'}</span>
            </div>
            {preparing || !slides ? (
              <div className="thumb-skel">
                {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} height={58} />)}
              </div>
            ) : slides.length === 0 ? (
              <EmptyState icon="image" title="暂无预览图" hint="渲染可能失败，仍可继续结构检查。" />
            ) : (
              <div className="thumb-list">
                {slides.map((slide) => {
                  const selected = options?.review_mode === 'manual' && options.manual_pages.includes(slide.index)
                  return (
                    <button
                      key={slide.index}
                      type="button"
                      className={`thumb-item${selected ? ' active' : ''}`}
                      onClick={() => options?.review_mode === 'manual' && toggleManual(slide.index)}
                      style={{ cursor: options?.review_mode === 'manual' ? 'pointer' : 'default' }}
                    >
                      {slide.thumb_url ? <img src={slide.thumb_url} alt={`第 ${slide.index} 页`} loading="lazy" /> : <div style={{ width: 86, height: 54, background: '#0a1020', borderRadius: 5 }} />}
                      <div style={{ minWidth: 0 }}>
                        <div className="idx">P{String(slide.index).padStart(2, '0')} {selected && <span style={{ color: 'var(--indigo)' }}>· 已选</span>}</div>
                        <div className="meta">{modeLabel[slide.content_mode]} · {slide.render_status === 'rendered' ? '已渲染' : '渲染失败'}</div>
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          <div className="cfg-pane">
            <div className="card">
              <div className="config-section">
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  <div>
                    <h3>文件信息</h3>
                    <p className="desc">解析结果来自真实 PPTX 对象层，不是按文件名推测。</p>
                  </div>
                  {doc && <span className="chip cyan">渲染长边 {caps?.limits.render_long_edge ?? 1920}px</span>}
                </div>

                {preparing || !doc ? (
                  <div className="stack" style={{ gap: 10 }}>
                    <Skeleton height={14} width="60%" /><Skeleton height={14} width="80%" />
                    <div className="grid" style={{ gridTemplateColumns: 'repeat(4,1fr)', gap: 10 }}>
                      {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} height={54} />)}
                    </div>
                  </div>
                ) : (
                  <>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 12, marginTop: 8 }}>
                      {[
                        ['页数', `${doc.slide_count}`],
                        ['画面比例', doc.aspect_label],
                        ['文本框', `${doc.textboxes}`],
                        ['可编辑字符', doc.text_chars.toLocaleString()],
                        ['图片', `${doc.pictures}`],
                        ['表格 / 图表', `${doc.tables} / ${doc.charts}`],
                        ['可编辑页', `${doc.editable_pages}`],
                        ['混合 / 位图页', `${doc.mixed_pages} / ${doc.image_only_pages}`],
                      ].map(([k, v]) => (
                        <div key={k} style={{ padding: '10px 12px', background: 'var(--surface-2)', borderRadius: 10, border: '1px solid var(--line)' }}>
                          <div className="tiny muted">{k}</div>
                          <div style={{ fontWeight: 700, marginTop: 2 }}>{v}</div>
                        </div>
                      ))}
                    </div>
                    <div className="row small muted" style={{ marginTop: 12, flexWrap: 'wrap', gap: 12 }}>
                      <span>尺寸 {doc.slide_width_in} × {doc.slide_height_in} in</span>
                      <span>·</span>
                      <span>文件 {formatMb(doc.size_bytes)}</span>
                      {doc.image_only_pages > 0 && <span className="chip gray">含 {doc.image_only_pages} 页整页位图，结构检查受限</span>}
                      {doc.missing_fonts.length > 0 && <span className="chip orange">缺少字体 {doc.missing_fonts.slice(0, 3).join('、')}</span>}
                    </div>
                  </>
                )}
              </div>

              <div className="config-section">
                <h3>审查任务名称</h3>
                <p className="desc">用于历史任务与报告标题，不会写入原文件。</p>
                <input className="input" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="例如：AI 生成版产品介绍 · 第三轮" />
              </div>

              <div className="config-section">
                <h3>用途与审查要求</h3>
                <p className="desc">用于理解页面设计意图。审查重点不会取消其他缺陷类型的检查。</p>
                <div className="input-row">
                  <div className="field"><label htmlFor="review-purpose">使用场景</label><input id="review-purpose" className="input" maxLength={500} value={options?.purpose ?? ''} onChange={(e) => setOpt('purpose', e.target.value)} placeholder="例如：课堂投影、答辩、产品汇报" /></div>
                  <div className="field"><label htmlFor="review-style">期望风格</label><input id="review-style" className="input" maxLength={500} value={options?.style ?? ''} onChange={(e) => setOpt('style', e.target.value)} placeholder="例如：极简、信息密集、非对称构图" /></div>
                </div>
                <div className="row" style={{ flexWrap: 'wrap', marginTop: 12 }}>
                  {DEFECTS.map((d) => <label key={d.key} className="small row" style={{ gap: 5 }}><input type="checkbox" checked={options?.review_focus?.includes(d.key) ?? false} onChange={(e) => setOpt('review_focus', e.target.checked ? [...(options?.review_focus ?? []), d.key] : (options?.review_focus ?? []).filter((key) => key !== d.key))} />{d.label}</label>)}
                </div>
                <div className="field" style={{ marginTop: 12 }}><label htmlFor="original-prompt">原始生成要求（可选）</label><textarea id="original-prompt" className="input" rows={3} maxLength={12000} value={options?.original_prompt ?? ''} onChange={(e) => setOpt('original_prompt', e.target.value)} placeholder="粘贴原始内容要求，供独立的任务符合性审查参考。" /></div>
              </div>

              <div className="config-section">
                <h3>审查层配置</h3>
                <p className="desc">未接入的模块会明确标注，不会用“无缺陷”替代。</p>

                <div className="option-row">
                  <div className="txt">
                    <b>六类专用检测 <span className="badge rule">{sixRelease ? `R${sixRelease}` : '当前版本'}</span></b>
                    <p>本次六类风险候选的主要筛查路径，检查字号、遮挡、拥挤、裁切、留白与异常设计风险，并保留待核实范围。</p>
                    <p className="tiny muted" style={{ overflowWrap: 'anywhere' }}>当前版本：{sixDetector?.version || '尚未取得版本信息'} · {sixStateLabel}</p>
                    <p>{sixDetector?.detail ?? '检测入口状态尚未确认；实际检查结果和能力限制会按页面记录。'}</p>
                  </div>
                  <Toggle label="六类专用检测" checked={options?.enable_six_detectors ?? false} onChange={(v) => setOptions((previous) => previous ? { ...previous, enable_six_detectors: v, ...(!v ? { enable_visual: false } : {}) } : previous)} />
                </div>

                <div className="option-row">
                  <div className="txt">
                    <b>基础结构检查 <span className="badge rule">Layer 1</span></b>
                    <p>对象越界、文本溢出、显式字号、遮挡与密度候选；每条都带规则 id 与坐标。</p>
                  </div>
                  <Toggle label="结构规则检查" checked={options?.enable_structure ?? true} onChange={(v) => setOpt('enable_structure', v)} />
                </div>

                <div className={`option-row${visualReady ? '' : ' disabled'}`}>
                  <div className="txt">
                    <b>CLIP 四标签辅助复核（可选） <span className="badge model">Layer 2</span></b>
                    <p>
                      {visualReady
                        ? `已接入 ${caps?.modules.visual.model_version || '冻结模型'}，按冻结工作点补充前四类页面级风险，默认关闭，可手动开启。`
                        : `${caps?.modules.visual.reason || '未确认可用的 CLIP 模型入口。'} 本项不可启用；六类专用检测仍可独立运行。`}
                    </p>
                    <p>只检查字号、遮挡、拥挤、裁切；不判断留白或异常设计风险，不用低分取消 R2 候选。</p>
                    <p>开启 CLIP 会同时启用 R2 首筛；关闭六类首筛时 CLIP 也会关闭。API 配置完整时 LLM 默认开启，可单独关闭。</p>
                  </div>
                  <Toggle
                    label="视觉分类模型"
                    checked={(options?.enable_visual ?? false) && visualReady}
                    disabled={!visualReady}
                    onChange={(v) => setOptions((previous) => previous ? { ...previous, enable_visual: v, ...(v ? { enable_six_detectors: true } : {}) } : previous)}
                  />
                </div>

                <div className="field" style={{ marginTop: 16 }}>
                  <label htmlFor="llm-backend">复核方式</label>
                  <select id="llm-backend" className="input" value={options?.llm_backend ?? 'api'} onChange={(e) => setOpt('llm_backend', e.target.value as 'api' | 'mock')}>
                    <option value="api">真实模型服务{mllmReady ? '（已配置）' : '（尚不可用）'}</option>
                    <option value="mock">Mock 模拟复核（仅验证流程）</option>
                  </select>
                  {options?.llm_backend === 'mock' && <p className="desc" role="status">模拟复核不会调用模型，结论不能作为真实模型判断。</p>}
                </div>
                <div className={`option-row${reviewAvailable ? '' : ' disabled'}`}>
                  <div className="txt">
                    <b>多模态按需复核 <span className="badge ai">Layer 3</span></b>
                    <p>
                      {options?.llm_backend === 'mock'
                        ? '使用明确标记的模拟结果验证流程，不发送页面到外部服务。'
                        : mllmReady
                        ? 'API 完整时新任务默认开启，可在此关闭。首轮筛查结束后等待 5 秒；未跳过时自动复检选中范围，逐页记录调用结果。'
                        : !caps ? '正在确认 API 状态，保留当前复检选择；后端开始前会再次检查配置。'
                        : '未配置 API。可先完成结构审查；需要时到设置页填写 Base URL、Key 与模型。'}
                    </p>
                    {!mllmReady && options?.llm_backend !== 'mock' && (
                      <button className="btn btn-ghost btn-sm" style={{ marginTop: 4, marginLeft: -8 }} onClick={() => navigate('/settings')}>
                        前往配置 <Icon name="arrow" size={13} />
                      </button>
                    )}
                  </div>
                  <Toggle
                    label="多模态复核"
                    checked={options?.enable_mllm ?? false}
                    disabled={!reviewAvailable && !options?.enable_mllm}
                    onChange={(v) => setOpt('enable_mllm', v)}
                  />
                </div>

                <div role="status" style={{ marginTop: 14, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 10, background: 'var(--surface-2)' }}>
                  <b className="small">本次流程：{options?.enable_six_detectors ? 'R2 六类首筛' : 'R2 未启用（非完整三级流程）'} → {options?.enable_visual ? visualReady ? 'CLIP 四标签辅助复核 → ' : 'CLIP 不可用（未执行） → ' : 'CLIP 关闭 → '}LLM 按需复检</b>
                  <p className="tiny muted" style={{ marginTop: 5, lineHeight: 1.65 }}>
                    {!options?.enable_mllm || options?.review_mode === 'off'
                      ? 'LLM 复检未启用，本次筛查与辅助复核不代表最终复检完成。'
                      : options?.llm_backend === 'mock'
                        ? '本次使用 Mock 复核验证流程，不会完成真实 LLM 复检。'
                        : !mllmReady
                          ? 'LLM 服务尚不可用，相关范围将保留为未完成复检。'
                          : !options.max_review_pages || !options.max_api_calls
                            ? '当前复核预算为零，相关范围不会完成 LLM 复检。'
                            : 'LLM 按本次策略与预算检查选中范围；预算外、失败及仍不确定的部分继续保留待核实。'}
                  </p>
                </div>
              </div>

              <div className="config-section" style={{ opacity: options?.enable_mllm && reviewAvailable ? 1 : .55 }}>
                <h3>复核策略</h3>
                <p className="desc">策略是工程启发式，不是经过实验验证的最优方案；调用记录会写入报告。</p>
                <div className="field" style={{ marginBottom: 14 }}><label htmlFor="review-strategy">候选调度策略</label><select id="review-strategy" className="input" value={options?.review_strategy ?? 'selective_review'} disabled={!options?.enable_mllm || !reviewAvailable} onChange={(e) => setOpt('review_strategy', e.target.value as ReviewOptions['review_strategy'])}><option value="selective_review">按风险、证据及预算选择复核</option><option value="full_review">复核全部符合条件的候选（仍受预算限制）</option></select></div>
                <div className="segmented" style={{ marginBottom: 14 }}>
                  {[
                    ['risk_only', '仅高风险页'],
                    ['all', '全部页面'],
                    ['manual', '手动选择'],
                    ['off', '不复核'],
                  ].map(([key, label]) => (
                    <button
                      key={key}
                      aria-pressed={options?.review_mode === key}
                      disabled={!options?.enable_mllm || !reviewAvailable}
                      onClick={() => setOpt('review_mode', key as ReviewOptions['review_mode'])}
                    >{label}</button>
                  ))}
                </div>
                <div className="input-row">
                  <div className="field">
                    <label htmlFor="review-page-budget">单次最多复核页数</label>
                    <input id="review-page-budget" className="input" type="number" min={0} max={50}
                      value={options?.max_review_pages ?? 6}
                      disabled={!options?.enable_mllm || !reviewAvailable}
                      onChange={(e) => setOpt('max_review_pages', Math.max(0, Math.min(50, Number(e.target.value))))} />
                  </div>
                  <div className="field">
                    <label htmlFor="review-call-budget">API 调用预算（次）</label>
                    <input id="review-call-budget" className="input" type="number" min={0} max={50}
                      value={options?.max_api_calls ?? 6}
                      disabled={!options?.enable_mllm || !reviewAvailable}
                      onChange={(e) => setOpt('max_api_calls', Math.max(0, Math.min(50, Number(e.target.value))))} />
                  </div>
                </div>
                {options?.review_mode === 'manual' && (
                  <div className="field" style={{ marginTop: 12 }}>
                    <label>已选页面（在左侧缩略图点选）</label>
                    <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
                      {options.manual_pages.length === 0 ? <span className="small muted">尚未选择页面</span> :
                        options.manual_pages.map((p) => <span key={p} className="chip indigo">P{String(p).padStart(2, '0')}</span>)}
                    </div>
                  </div>
                )}
                {options?.enable_mllm && mllmReady && options?.llm_backend !== 'mock' && (
                  <div style={{ marginTop: 14, padding: '10px 12px', background: '#fff8ee', border: '1px solid #ffe3c4', borderRadius: 10, fontSize: 11.5, lineHeight: 1.65, color: '#6b3c05' }}>
                    <b>隐私提示：</b>启用后，被选中页面的渲染图与结构证据会发送到你配置的 AI 服务提供方。不会上传整份 PPT，也不会写入任何人工标注真值。
                  </div>
                )}
              </div>

              {mode === 'demo' && (
                <div className="config-section">
                  <div className="chip demo">演示任务</div>
                  <p className="desc" style={{ marginTop: 8 }}>
                    该任务使用系统生成的 fixture 和演示占位结论，报告带 DEMO 标识，与真实任务数据库隔离。
                  </p>
                </div>
              )}

              {task?.warnings?.length ? (
                <div className="config-section">
                  <h3>准备阶段提示</h3>
                  <ul className="small" style={{ margin: '8px 0 0', paddingLeft: 18, color: 'var(--muted)' }}>
                    {task.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                </div>
              ) : null}
            </div>

            <div className="action-bar">
              <div className="summary">
                将执行 <b>8</b> 个阶段。六类筛查：<b>{sixStateLabel}</b>；历史 CLIP：<b>{visualReady ? '可选补充' : '未配置'}</b>。
                <br />
                结构检查与报告导出无需 API Key。
              </div>
              <div className="spacer" />
              <button className="btn btn-ghost" onClick={() => void discard()} disabled={starting}>放弃</button>
              <button className="btn btn-primary btn-lg" onClick={() => void start()} disabled={starting || preparing || !effective}>
                {starting ? '正在创建任务…' : <>开始审查 <Icon name="arrow" size={17} /></>}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import type { Capabilities, ReviewOptions, TaskSummary } from '../lib/types'
import { DEFECTS } from '../lib/types'
import { Icon } from '../components/Icon'
import { UploadZone } from '../components/UploadZone'
import { EmptyState, Skeleton, StatusChip } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'

const DEFAULT_OPTIONS: ReviewOptions = {
  enable_structure: true,
  enable_visual: false,
  enable_mllm: false,
  mllm_user_choice: null,
  review_mode: 'risk_only',
  max_review_pages: 6,
  max_api_calls: 6,
  manual_pages: [],
  enable_six_detectors: true,
  purpose: '',
  style: '',
  review_focus: [],
  original_prompt: '',
  review_strategy: 'selective_review',
  llm_backend: 'api',
}

type DistItem = { key: string; label: string; color: string; count: number }

function percents(counts: number[]): number[] {
  const total = counts.reduce((sum, n) => sum + n, 0)
  if (!total) return counts.map(() => 0)
  const raw = counts.map((n) => (n / total) * 100)
  const base = raw.map((n) => Math.floor(n))
  let rest = 100 - base.reduce((sum, n) => sum + n, 0)
  const order = raw
    .map((n, index) => ({ index, frac: n - Math.floor(n) }))
    .sort((a, b) => b.frac - a.frac)
  for (let i = 0; rest > 0; i += 1, rest -= 1) base[order[i % order.length].index] += 1
  return base
}

function Donut({ items, total }: { items: DistItem[]; total: number }) {
  const radius = 54
  const circumference = 2 * Math.PI * radius
  let start = 0
  const arcs = items.map((item) => {
    const length = (item.count / total) * circumference
    const visible = Math.max(length - 1.6, 0.6)
    const arc = (
      <circle
        key={item.key}
        cx="70"
        cy="70"
        r={radius}
        fill="none"
        stroke={item.color}
        strokeWidth="15"
        strokeDasharray={`${visible} ${circumference - visible}`}
        strokeDashoffset={-start}
        transform="rotate(-90 70 70)"
      />
    )
    start += length
    return arc
  })
  return (
    <div className="donut">
      <svg viewBox="0 0 140 140" role="img" aria-label={`共 ${total} 条风险`}>
        <circle cx="70" cy="70" r={radius} fill="none" stroke="#eef2fb" strokeWidth="15" />
        {arcs}
      </svg>
      <div className="donut-center"><b>{total}</b><span>风险项</span></div>
    </div>
  )
}

export default function Home() {
  const navigate = useNavigate()
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [busyLabel, setBusyLabel] = useState('')
  const [uploadRatio, setUploadRatio] = useState(0)
  const [demoBusy, setDemoBusy] = useState(false)
  const [tab, setTab] = useState<'upload' | 'demo'>('upload')
  const [showHelp, setShowHelp] = useState(false)
  const [showCaps, setShowCaps] = useState(false)

  const sixDetector = caps?.modules.six_detector
  const sixRelease = sixDetector?.version?.match(/(?:^|[-_])r(\d+)(?:$|[-_])/i)?.[1]
  const sixStateLabel = !caps ? '正在探测' : sixDetector?.state === 'ready' ? '入口可用' : sixDetector?.state === 'unsupported' ? '当前不支持' : sixDetector?.state === 'error' ? '暂不可用' : '状态未确认'
  const sixShort = !caps ? '探测中' : sixDetector?.state === 'ready' ? '可用' : sixDetector?.state === 'unsupported' ? '不支持' : sixDetector?.state === 'error' ? '不可用' : '待确认'
  const structureRules = caps?.modules.structure.rules ?? []
  const structureState = caps?.modules.structure.state
  const structureStateLabel = !caps ? '正在探测' : structureState === 'ready' ? '基础规则可用' : structureState === 'unsupported' ? '当前不支持' : structureState === 'error' ? '暂不可用' : '状态未确认'
  const structureShort = !caps ? '探测中' : structureState === 'ready' ? '可用' : structureState === 'unsupported' ? '不支持' : structureState === 'error' ? '不可用' : '待确认'
  const visualStateLabel = !caps ? '组件状态尚未确认' : caps.modules.visual.available ? '组件可用，默认关闭' : caps.modules.visual.state === 'not_configured' ? '接口保留，未部署/未启用' : caps.modules.visual.state === 'error' ? '加载失败（未启用）' : '状态未确认'
  const visualShort = !caps ? '探测中' : caps.modules.visual.available ? '可用' : caps.modules.visual.state === 'not_configured' ? '未部署' : caps.modules.visual.state === 'error' ? '加载失败' : '待确认'

  useEffect(() => {
    api.capabilities().then(setCaps).catch((err) => toast(err.message, 'error'))
    api.listTasks().then((res) => setTasks(res.items)).catch((err) => toast(err.message, 'error'))
  }, [])

  const handleFile = async (file: File) => {
    if (!file.name.toLowerCase().endsWith('.pptx')) {
      toast('只支持 .pptx 文件，旧版 .ppt 请先另存为 .pptx。', 'error')
      return
    }
    if (caps && file.size > caps.limits.max_upload_mb * 1024 * 1024) {
      toast(`文件超过 ${caps.limits.max_upload_mb} MB 上限。`, 'error')
      return
    }
    setBusy(true)
    setUploadRatio(0)
    setBusyLabel('正在上传并解析…')
    try {
      const initialCaps = await api.capabilities().catch(() => null)
      if (initialCaps) setCaps(initialCaps)
      const initialOptions: Partial<ReviewOptions> = { ...DEFAULT_OPTIONS }
      // Automatic policy is resolved by the backend, not persisted as an
      // apparent manual false when capability discovery is delayed or stale.
      delete initialOptions.enable_mllm
      const task = await api.uploadWithProgress(file, file.name.replace(/\.pptx$/i, ''), initialOptions, true, setUploadRatio)
      toast('文件已接收，正在解析与渲染预览。', 'success')
      navigate(`/tasks/${task.id}/configure`)
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '上传失败，请重试。'
      toast(message, 'error')
      setBusy(false)
      setBusyLabel('')
      setUploadRatio(0)
    }
  }

  const startDemo = async (scenario = 'full') => {
    setDemoBusy(true)
    try {
      const task = await api.createDemo(scenario)
      navigate(`/tasks/${task.id}/progress?mode=demo`)
    } catch (err) {
      toast((err as Error).message, 'error')
    } finally {
      setDemoBusy(false)
    }
  }

  const openTask = (task: TaskSummary) => {
    if (task.status === 'ready' || task.status === 'draft' || task.status === 'preparing') {
      navigate(`/tasks/${task.id}/configure`)
    } else if (task.status === 'queued' || task.status === 'running') {
      navigate(`/tasks/${task.id}/progress`)
    } else {
      navigate(`/tasks/${task.id}/results`)
    }
  }

  const openPicker = () => {
    setTab('upload')
    window.requestAnimationFrame(() => {
      document.querySelector<HTMLElement>('.dropzone')?.click()
    })
  }

  const latestDone = tasks?.find((task) => task.summary && (task.status === 'completed' || task.status === 'completed_with_errors')) ?? null
  const distItems: DistItem[] = useMemo(() => {
    const byType = latestDone?.summary?.by_type
    if (!byType) return []
    return DEFECTS
      .map((defect) => ({ key: defect.key, label: defect.label, color: defect.color, count: byType[defect.key] ?? 0 }))
      .filter((item) => item.count > 0)
  }, [latestDone])
  const distTotal = distItems.reduce((sum, item) => sum + item.count, 0)
  const distPercents = percents(distItems.map((item) => item.count))

  const capabilityCards = [
    {
      no: '01',
      tone: 'indigo',
      icon: 'layers' as const,
      title: sixRelease ? `六类筛查 R${sixRelease}` : '六类筛查',
      desc: '字号、遮挡、拥挤、溢出与留白，先出候选再逐条核实。',
      chip: sixShort,
      chipTitle: sixStateLabel,
      ok: sixDetector?.state === 'ready',
    },
    {
      no: '02',
      tone: 'cyan',
      icon: 'scan' as const,
      title: caps ? `${structureRules.length} 条结构规则` : '结构规则',
      desc: '越界、溢出、字号与密度，全部给出可核对的几何证据。',
      chip: structureShort,
      chipTitle: structureStateLabel,
      ok: structureState === 'ready',
    },
    {
      no: '03',
      tone: 'orange',
      icon: 'spark' as const,
      title: '多模态复核',
      desc: caps?.modules.mllm.state === 'ready'
        ? `已配置 ${caps.modules.mllm.model || '模型'}，新任务默认复检，可在配置中关闭。`
        : '尚未配置；不配置也能完成结构检查与报告导出。',
      chip: caps?.modules.mllm.state === 'ready' ? '已配置' : '未配置',
      chipTitle: caps?.modules.mllm.detail ?? '正在确认模型配置',
      ok: caps?.modules.mllm.state === 'ready',
    },
    {
      no: '04',
      tone: 'teal',
      icon: 'cpu' as const,
      title: 'CLIP 辅助复核',
      desc: '补充字号、遮挡、拥挤、裁切四类视觉证据，默认关闭。',
      chip: visualShort,
      chipTitle: visualStateLabel,
      ok: Boolean(caps?.modules.visual.available),
    },
  ]

  return (
    <div className="fade-up home">
      <div className="home-grid">
        <section className="hero-block">
          <span className="hero-eyebrow">AI 生成 PPT 的最后一公里</span>
          <h1 className="hero-title">
            让每一页 AI 演示，<br />
            <span>经得起最后一公里</span>
          </h1>
          <p className="hero-lead">
            从结构、视觉到语义，逐页检查字体、留白、遮挡与裁切，
            发现问题，给出可落地的修改建议。
          </p>
          <div className="hero-actions">
            <button className="btn btn-primary btn-lg" onClick={openPicker}>
              <Icon name="upload" size={17} />
              开始审查
              <Icon name="arrow" size={15} />
            </button>
            <button className="btn btn-lg" onClick={() => void startDemo('full')} disabled={demoBusy}>
              {demoBusy ? <Icon name="spinner" className="spin" /> : <Icon name="play" size={14} />}
              查看演示任务
            </button>
          </div>
          <div className="hero-trust">
            <span>{caps?.modules.mllm.state === 'ready' ? 'API 就绪默认复检，可关闭' : 'API 未就绪时不外发'}</span>
            <i />
            <span>三层证据各自标注来源</span>
            <i />
            <span>渲染保持原始比例</span>
          </div>

          <div className="hero-art" aria-hidden="true" />
          <div className="hero-chips" aria-hidden="true">
            <span className="art-chip c1"><Icon name="scan" size={12} />结构</span>
            <span className="art-chip c2"><Icon name="image" size={12} />视觉</span>
            <span className="art-chip c3"><Icon name="file" size={12} />语义</span>
            <span className="art-chip c4"><Icon name="shield" size={12} />要求</span>
            <span className="art-chip c5"><Icon name="spark" size={12} />风格</span>
            <span className="art-chip c6"><Icon name="chart" size={12} />报告</span>
          </div>
        </section>

        <section className="upload-card" aria-label="上传或体验">
          <div className="up-head">
            <div className="up-tabs" role="tablist" aria-label="上传方式">
              <button type="button" role="tab" aria-selected={tab === 'upload'}
                className={`up-tab${tab === 'upload' ? ' on' : ''}`}
                onClick={() => setTab('upload')} disabled={busy}>上传审查</button>
              <button type="button" role="tab" aria-selected={tab === 'demo'}
                className={`up-tab${tab === 'demo' ? ' on' : ''}`}
                onClick={() => setTab('demo')} disabled={busy}>示例体验</button>
            </div>
            <button type="button" className={`up-help${showHelp ? ' on' : ''}`} aria-expanded={showHelp}
              onClick={() => setShowHelp((v) => !v)}>
              <Icon name="help" size={14} />
              使用说明
            </button>
          </div>

          {showHelp && (
            <div className="up-help-panel">
              <p><b>格式</b>.pptx，单份 ≤ {caps?.limits.max_upload_mb ?? 80} MB、≤ {caps?.limits.max_slides ?? 200} 页；渲染长边 {caps?.limits.render_long_edge ?? 1920}px，保持原始比例。</p>
              <p><b>隐私</b>API 配置完整时，新任务默认启用 LLM 复检，将选中页面图与证据发送到已配置的服务。可在配置中关闭，或在首轮结束后的等待窗口跳过；未配置或无法确认 API 时不会自动调用，也不会自动切换 Mock。</p>
              <p><b>判定边界</b>结构规则是确定性测量；六类筛查给的是候选；未复核范围不等于已确认。</p>
            </div>
          )}

          {tab === 'upload' ? (
            <UploadZone
              onFile={handleFile}
              busy={busy}
              busyLabel={busyLabel}
              progress={uploadRatio}
              maxMb={caps?.limits.max_upload_mb ?? 80}
              maxSlides={caps?.limits.max_slides ?? 200}
            />
          ) : (
            <div className="demo-panel">
              <p className="demo-intro">
                演示任务使用系统生成的 fixture 与演示结论，全程带 DEMO 标识，存放在独立数据库，不会混入真实任务。
              </p>
              {caps === null && (
                <div className="demo-list">
                  <Skeleton height={52} radius={10} />
                  <Skeleton height={52} radius={10} />
                  <Skeleton height={52} radius={10} />
                </div>
              )}
              {caps && !caps.demo.available && <p className="demo-empty">当前没有可用的演示场景。</p>}
              {caps?.demo.available && (
                <div className="demo-list">
                  {caps.demo.scenarios.map((scenario) => (
                    <button key={scenario.key} type="button" className="demo-item"
                      disabled={demoBusy} onClick={() => void startDemo(scenario.key)}>
                      <span className="demo-ico"><Icon name="play" size={12} /></span>
                      <span className="demo-txt"><b>{scenario.label}</b><i>{scenario.description}</i></span>
                      <Icon name="arrow" size={13} />
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>

        <section className="caps-block">
          <div className="sec-head">
            <h2>核心能力</h2>
            <span>覆盖结构、视觉、语义等维度，全面提升演示文稿质量</span>
          </div>
          <div className="cap-grid">
            {capabilityCards.map((card) => (
              <article key={card.no} className="cap-card">
                <div className="cap-top">
                  <span className={`cap-ico ${card.tone}`}><Icon name={card.icon} size={18} /></span>
                  <span className="cap-no">{card.no}</span>
                </div>
                <h3>{card.title}</h3>
                <p>{card.desc}</p>
                <div className="cap-foot">
                  <span className={`cap-state${card.ok ? ' ok' : ''}`} title={card.chipTitle}><i />{card.chip}</span>
                  {card.no === '03' ? (
                    <button type="button" className="cap-link" onClick={() => navigate('/settings')}>
                      {caps?.modules.mllm.state === 'ready' ? '调整' : '配置'}
                    </button>
                  ) : (
                    <button type="button" className={`cap-more${showCaps ? ' on' : ''}`}
                      aria-label="查看能力依据与限制" aria-expanded={showCaps}
                      onClick={() => setShowCaps((v) => !v)}>
                      <Icon name="arrow" size={13} />
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>

          <p className="caps-note">规则数与检测器数量不是缺陷类别数；未复核范围不视为已确认。</p>

          {showCaps && (
            <div className="caps-detail">
              <div>
                <h4>六类筛查 <span>{sixDetector?.version || '尚未取得版本信息'}</span></h4>
                <p>{sixDetector?.detail ?? '正在确认检测入口；每页实际可判范围以本次检查记录为准。'}</p>
                <p>候选不等于缺陷：每页都会记录「发现候选 / 可判范围未发现 / 待核实」等状态，未检查的范围不会计入正常。</p>
              </div>
              <div>
                <h4>结构规则 <span>确定性几何测量</span></h4>
                {structureRules.length > 0
                  ? <ul>{structureRules.map((rule) => <li key={rule}>{rule}</li>)}</ul>
                  : <p>尚未取得规则清单，不能据此推定检查已执行。</p>}
                <p>R01 对象越界与 R02 文本溢出同属裁切/溢出检查，其余规则补充字号、密度与遮挡证据。</p>
              </div>
              <div>
                <h4>多模态复核 <span>{caps?.modules.mllm.model || '未配置模型'}</span></h4>
                <p>{caps?.modules.mllm.detail ?? '尚未完成 API 配置；不配置也能完成结构检查与报告导出。'}</p>
                <p>单任务调用上限 {caps?.modules.mllm.max_calls_per_task ?? 6} 次，只复核选中的页面；预算不足的范围仍属于未完成复检。</p>
              </div>
              <div>
                <h4>CLIP 辅助 <span>{visualStateLabel}</span></h4>
                <p>{caps?.modules.visual.reason || '页面级四标签辅助概率，不提供对象定位；概率来自历史冻结分类器，未对新文稿校准。'}</p>
                <p>只补充字号、遮挡、拥挤、裁切四类证据，不判断留白与异常设计，也不否决六类候选。{caps?.modules.visual.model_version ? ` 当前模型：${caps.modules.visual.model_version}。` : ''}</p>
              </div>
            </div>
          )}
        </section>

        <section className="recent-card">
          <div className="side-head">
            <h2>最近任务</h2>
            <button type="button" className="link-btn" onClick={() => navigate('/tasks')}>
              查看全部 <Icon name="arrow" size={13} />
            </button>
          </div>
          {tasks === null ? (
            <div className="recent-list">
              <Skeleton height={52} radius={10} />
              <Skeleton height={52} radius={10} />
              <Skeleton height={52} radius={10} />
            </div>
          ) : tasks.length === 0 ? (
            <EmptyState icon="history" title="还没有审查记录" hint="拖入第一份 PPTX，或先运行一个演示任务。" />
          ) : (
            <div className="recent-list">
              {tasks.slice(0, 4).map((task) => (
                <button key={task.id} type="button" className="recent-row" onClick={() => openTask(task)}>
                  <span className="recent-tile">P</span>
                  <span className="recent-main">
                    <b>{task.name}</b>
                    <i>{formatDate(task.created_at)} · {task.slide_count ? `${task.slide_count} 页` : '—'}</i>
                  </span>
                  <StatusChip status={task.status} />
                  <Icon name="chevron" size={13} className="recent-chev" />
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="result-card">
          <div className="sec-head">
            <h2>审查结果会包含</h2>
            <span>从问题发现到修改建议，每一步都能回溯到证据</span>
          </div>
          <div className="steps">
            <div className="step">
              <span className="step-ico red"><Icon name="alert" size={17} /></span>
              <div><b>风险定位</b><p>精确到页面与元素，给出可核对的边界，而不是一个总分。</p></div>
            </div>
            <span className="step-arrow"><Icon name="arrow" size={15} /></span>
            <div className="step">
              <span className="step-ico indigo"><Icon name="file" size={17} /></span>
              <div><b>证据来源</b><p>规则、模型与复核各自标注来源，候选不会被写成已确认。</p></div>
            </div>
            <span className="step-arrow"><Icon name="arrow" size={15} /></span>
            <div className="step">
              <span className="step-ico green"><Icon name="bulb" size={17} /></span>
              <div><b>修改建议</b><p>给出可落地的调整方向与优先顺序，直接回到稿子里改。</p></div>
            </div>
          </div>
          <div className="result-foot">
            <span>报告里可逐条核对</span>
            <i />
            <span>页码</span>
            <i />
            <span>元素边界</span>
            <i />
            <span>判定来源</span>
            <i />
            <span>复核状态</span>
          </div>
        </section>

        <section className="risk-card">
          <div className="side-head">
            <h2>风险分布</h2>
            {latestDone && (
              <button type="button" className="link-btn" onClick={() => navigate(`/tasks/${latestDone.id}/results`)}>
                查看详情 <Icon name="arrow" size={13} />
              </button>
            )}
          </div>
          {distTotal > 0 ? (
            <>
              <div className="risk-body">
                <Donut items={distItems} total={distTotal} />
                <ul className="risk-legend">
                  {distItems.map((item, index) => (
                    <li key={item.key}>
                      <i style={{ background: item.color }} />
                      <span>{item.label}</span>
                      <b>{item.count}</b>
                      <em>{distPercents[index]}%</em>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="risk-src">来自最近完成任务 · {latestDone?.name}</div>
            </>
          ) : (
            <div className="risk-empty">
              <span className="risk-ring" aria-hidden="true" />
              <b>还没有可统计的结果</b>
              <p>完成一次审查后，这里会按六类缺陷显示真实分布，不使用演示数字。</p>
            </div>
          )}
        </section>
      </div>

      <footer className="home-foot">
        <span>数据目录：agent_app/backend/data（与 ml_stage4 研究目录完全隔离）</span>
        <i />
        <span>版本 {caps?.app.version ?? '4.4A'}</span>
        <i />
        <span>不修改任何既有基准、模型或人工标签</span>
      </footer>
    </div>
  )
}

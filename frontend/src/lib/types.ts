export type TaskStatus =
  | 'draft'
  | 'preparing'
  | 'ready'
  | 'queued'
  | 'running'
  | 'completed'
  | 'completed_with_errors'
  | 'failed'

export type StageStatus = 'pending' | 'running' | 'done' | 'skipped' | 'unsupported' | 'failed'

export interface Stage {
  key: string
  label: string
  status: StageStatus
  message: string
  progress: number
  started_at: string | null
  finished_at: string | null
  detail: Record<string, unknown>
}

export interface Summary {
  slide_count: number
  issue_count: number
  visible_issue_count: number
  rejected_count: number
  dismissed_count?: number
  pages_with_issues: number
  high_risk_pages: number[]
  by_type: Record<string, number>
  by_source: Record<string, number>
  ai_reviewed_pages: number
  ai_supported: number
  ai_rejected: number
  visual_pages_scored: number
  unreviewed_count: number
  detection_status_counts?: Record<string, number>
  review_planning?: unknown
}

export interface DocumentSlide {
  index: number
  text_shapes: number
  text_chars: number
  pictures: number
  tables: number
  charts: number
  full_slide_picture: boolean
  content_mode: 'editable' | 'mixed' | 'image_only'
  notes_chars: number
}

export interface DocumentMeta {
  file_name: string
  slide_count: number
  slide_width_in: number
  slide_height_in: number
  aspect_ratio: number
  aspect_label: string
  textboxes: number
  text_chars: number
  pictures: number
  tables: number
  charts: number
  editable_pages: number
  mixed_pages: number
  image_only_pages: number
  fonts_used: string[]
  missing_fonts: string[]
  slides: DocumentSlide[]
  sha256?: string
  size_bytes?: number
}

export interface TaskSummary {
  id: string
  mode: 'real' | 'demo'
  name: string
  status: TaskStatus
  progress: number
  source_name: string
  slide_count: number | null
  aspect_label: string | null
  created_at: string
  finished_at: string | null
  error: string | null
  summary: Summary | null
  demo_scenario: string | null
}

export interface ReviewOptions {
  enable_structure: boolean
  enable_visual: boolean
  enable_mllm: boolean
  mllm_user_choice?: boolean | null
  review_mode: 'off' | 'risk_only' | 'manual' | 'all'
  max_review_pages: number
  max_api_calls: number
  manual_pages: number[]
  enable_six_detectors?: boolean
  purpose?: string
  style?: string
  review_focus?: string[]
  original_prompt?: string
  review_strategy?: 'full_review' | 'selective_review'
  llm_backend?: 'api' | 'mock'
}

export type DetectionStatus = 'candidate' | 'no_candidate_in_available_scope' | 'unknown' | 'unsupported' | 'not_checked'
export type Box = { x: number; y: number; w: number; h: number }

export interface Claim {
  claim_id?: string
  candidate_id?: string | null
  document_id?: string
  slide_id?: string
  slide_index?: number
  defect_type: string
  subtype?: string | null
  raw_status?: DetectionStatus
  status?: DetectionStatus
  detector_version?: string
  score?: number | null
  score_kind?: string
  object_ids?: string[]
  input_versions?: unknown
  summary?: string
  evidence_summary?: string
  normalized_bbox?: Box | number[] | null
  related_slides?: (number | string)[]
  limitations?: string[]
  scope?: unknown
  available_scope?: unknown
  evidence?: unknown[]
}

export interface ReviewRecord {
  judgement?: 'supported' | 'rejected' | 'uncertain'
  evidence?: string | string[]
  rationale?: string
  reason?: string
  recommendation?: string
  model?: string
  llm_model?: string
  model_name?: string
  rule_version?: string
  rule_versions?: Record<string, string> | string[]
  backend?: 'api' | 'mock'
  mock?: boolean
  demo?: boolean
  question?: string
  user_question?: string
  reviewed_at?: string
  created_at?: string
  error?: string | null
  execution_trace?: unknown
  [key: string]: unknown
}

export interface ReviewPlanItem {
  issue_id?: string
  candidate_id?: string
  slide_index?: number
  related_slides?: (number | string)[]
  defect_type?: string
  selected?: boolean
  selection_reason?: string | string[]
  selected_tool?: string
  evidence_requested?: unknown
  budget?: unknown
  outcome?: unknown
  execution_trace?: unknown
  skip_reason?: string | null
  [key: string]: unknown
}

export interface ReviewPlan {
  items: ReviewPlanItem[]
  strategy?: string
  backend?: string
  budget?: unknown
  usage?: unknown
  [key: string]: unknown
}

export interface TaskDetail extends TaskSummary {
  options: ReviewOptions
  stages: Stage[]
  document: DocumentMeta | null
  warnings: string[]
  report_ready: boolean
  images_available: boolean
  review_gate?: ReviewGate
  recovery?: RecoveryInfo
  lineage?: RecoveryLineage | null
}

export interface RecoveryLineage {
  parent_task_id: string
  action: 'continue' | 'restart'
  reused_stages?: string[]
}

export interface RecoveryInfo {
  available: boolean
  can_continue: boolean
  can_restart: boolean
  reason?: string
  continue_label?: string
  reused_stages?: string[]
  pending_review_calls?: number
  additional_api_calls_possible?: boolean
  warnings?: string[]
  parent_task_id?: string | null
}

export interface RecoveryResult {
  task_id: string
  mode: 'real' | 'demo'
  parent_task_id: string
  action: 'continue' | 'restart'
  reused_stages?: string[]
  pending_review_calls?: number
  additional_api_calls_possible?: boolean
  message?: string
}

export interface ReviewGate {
  state: 'not_ready' | 'waiting' | 'executing' | 'skipped' | 'disabled' | 'completed' | 'invalid'
  deadline_at: string | null
  remaining_ms: number
  can_skip: boolean
  skip_available_before_stage?: boolean
  skipped_before_review?: boolean
  countdown_seconds: number | null
  eligible: boolean
  skip_reason: string | null
  skipped_by_user: boolean
  execution_started_at: string | null
  completed_at: string | null
  outcome: string | null
  duplicate_execution_guard: boolean
  warning: string | null
  task_status?: TaskStatus
  result_ready?: boolean
}

export interface SlideSummary {
  index: number
  content_mode: 'editable' | 'mixed' | 'image_only'
  text_chars: number
  textboxes: number
  pictures: number
  tables: number
  charts: number
  render_status: 'rendered' | 'failed' | 'unavailable'
  render_error: string | null
  width: number | null
  height: number | null
  thumb_url: string | null
  image_url: string | null
  issue_count: number
  issue_types: string[]
  high_risk: boolean
  ai_reviewed: boolean
}

export interface IssueSource {
  source: string
  rule_id?: string | null
  summary?: string
  probability?: number
  model_version?: string
  model?: string
  threshold?: number | null
  work_point?: string | null
  model_revision?: string | null
}

export interface ClipReview {
  status: string
  probability?: number | null
  threshold?: number | null
  alert?: boolean | number | null
  model_version?: string | null
  work_point?: string | null
  model_revision?: string | null
  scope?: 'page'
  limitations?: string[]
}

export interface VisualResult {
  status: string
  slide_index: number
  probabilities?: Record<string, number | null>
  alerts?: Record<string, boolean | number | null>
  thresholds?: Record<string, number | null>
  model_version?: string | null
  work_point?: string | null
  model_revision?: string | null
  limitations?: string[]
  latency_ms?: number | null
  error?: string | null
}

export interface VerificationPipeline {
  clip_enabled: boolean
  ordered_layers: { key: 'six_detector' | 'clip_four_label' | 'mllm'; label: string; status: string; scope: unknown }[]
  clip_pages_scored: number
  clip_pages_failed: number
  llm_mode: 'api' | 'mock' | 'disabled'
  llm_call_records: number
  llm_actual_http_calls: number
  llm_unreviewed_items: number
  llm_uncertain_verdicts: number
  final_review_status: 'not_requested' | 'not_checked' | 'mock_only' | 'partial' | 'reviewed_with_uncertainty' | 'planned_scope_reviewed' | 'user_skipped'
  review_gate?: ReviewGate
  note: string
}

export interface Issue {
  issue_id: string
  slide_index: number
  defect_type: string
  title: string
  evidence_source: string
  rule_id: string | null
  evidence_summary: string
  element_id: string | null
  bbox: { x: number; y: number; w: number; h: number } | null
  visual_probability: number | null
  severity: 'high' | 'medium' | 'low' | 'info'
  confidence: string
  review_status: 'unreviewed' | 'supported' | 'rejected' | 'uncertain'
  review_result: {
    judgement: string
    evidence?: string
    recommendation?: string
    confidence?: string
    model?: string
    demo?: boolean
    mock?: boolean
    backend?: string
  } | null
  recommendation: string
  sources: IssueSource[]
  demo?: boolean
  candidate_id?: string | null
  subtype?: string | null
  normalized_bbox?: Box | number[] | null
  related_slides?: (number | string)[]
  detector_version?: string | null
  raw_status?: DetectionStatus
  limitations?: string[]
  claims?: Claim[]
  review_history?: ReviewRecord[]
  scope?: unknown
  available_scope?: unknown
  source_scope?: unknown
  source_conflicts?: unknown[]
  evidence_conflict?: unknown
  clip_review?: ClipReview | null
  user_verdict?: 'dismissed' | null
  user_verdict_note?: string
  user_verdict_at?: string
}

export interface AdviceAction {
  defect_type: string
  label: string
  title: string
  directive: string
  pages: number[]
  pages_label: string
  count: number
  high: boolean
  steps: string[]
  acceptance: string
}

export interface AdvicePage {
  slide_index: number
  label: string
  count: number
  focus: string
  high: boolean
}

export interface AdvicePhase {
  title: string
  detail: string
  pages: number[]
  pages_label: string
  count: number
}

export interface Advice {
  generated_at: string
  source: 'rule' | 'llm'
  model: string | null
  fingerprint: string
  stats: {
    issues: number
    pages: number
    high: number
    dismissed: number
    rejected: number
    unreviewed: number
    ai_reviewed_pages: number
    ai_supported: number
  }
  headline: string
  verdict: string
  summary: string[]
  actions: AdviceAction[]
  page_plan: AdvicePage[]
  sequence: AdvicePhase[]
  note: string
  degraded?: string
  stale?: boolean
}

export interface SlideDetail {
  index: number
  slide: DocumentSlide
  render: { status?: string; error?: string; width?: number; height?: number }
  issues: Issue[]
  image_url: string
  thumb_url: string
  issues_confirmed: number
  issues_rejected: number
  issues_dismissed?: number
  claims?: Claim[]
  visual_result?: VisualResult | null
}

export interface VisualStatus {
  state: 'ready' | 'not_configured' | 'error'
  available: boolean
  reason: string
  labels: string[]
  model_version: string | null
  work_point: string | null
  input_recipe: string | null
  integration_hint: string
}

export interface Capabilities {
  app: { name: string; subtitle: string; version: string }
  modules: {
    structure: { state: string; name: string; detail: string; rules: string[] }
    visual: VisualStatus & { name: string }
    mllm: {
      state: string
      name: string
      model: string | null
      base_url_set: boolean
      key_set: boolean
      vision_verified: boolean
      max_calls_per_task: number
      detail: string
    }
    six_detector?: { state: string; name?: string; detail?: string; version?: string }
  }
  labels: { key: string; label: string }[]
  limits: { max_upload_mb: number; max_slides: number; render_long_edge: number }
  demo: { available: boolean; scenarios: { key: string; label: string; description: string }[]; task_count: number }
  task_count: number
  privacy: string
}

export interface AppSettings {
  api_base_url: string
  api_model: string
  api_timeout_s: number
  api_max_retries: number
  api_max_concurrency: number
  api_temperature: number
  max_calls_per_task: number
  allow_private_base_url: boolean
  allow_insecure_base_url: boolean
  response_format_json: boolean
  vision_verified: boolean
  last_test: {
    status: string
    message: string
    latency_ms: number | null
    http_status: number | null
    mode?: string
    tested_at?: string
    model?: string
  } | null
  api_key_set: boolean
  api_key_masked: string
}

export interface ConnectionResult {
  status: string
  code: string
  message: string
  latency_ms: number | null
  http_status: number | null
  mode?: string
}

export const DEFECTS = [
  { key: 'improper_font_sizing', label: '字号不合理', short: '字号', color: '#5b6cff' },
  { key: 'occluded_content', label: '内容遮挡', short: '遮挡', color: '#e8662a' },
  { key: 'cluttered_layout', label: '布局拥挤', short: '拥挤', color: '#b7791f' },
  { key: 'content_overflow_cutoff', label: '溢出或裁切', short: '溢出', color: '#d9363e' },
  { key: 'excessive_whitespace', label: '过分留白', short: '留白', color: '#168c88' },
  { key: 'generative_design_anomaly', label: '异常设计风险', short: '设计风险', color: '#8655bd' },
] as const

export const DETECTION_STATUS_LABEL: Record<DetectionStatus, string> = {
  candidate: '发现候选',
  no_candidate_in_available_scope: '可判范围未发现候选',
  unknown: '待核实',
  unsupported: '能力暂不支持',
  not_checked: '未检查',
}

export const DEFECT_LABEL: Record<string, string> = Object.fromEntries(
  DEFECTS.map((d) => [d.key, d.label]),
)
export const DEFECT_COLOR: Record<string, string> = Object.fromEntries(
  DEFECTS.map((d) => [d.key, d.color]),
)

export const STATUS_LABEL: Record<TaskStatus, string> = {
  draft: '准备中',
  preparing: '解析与渲染中',
  ready: '待开始审查',
  queued: '排队中',
  running: '审查中',
  completed: '已完成',
  completed_with_errors: '已完成（含失败模块）',
  failed: '失败',
}

export const STAGE_LABEL: Record<StageStatus, string> = {
  pending: '等待中',
  running: '运行中',
  done: '已完成',
  skipped: '已跳过',
  unsupported: '未接入',
  failed: '失败',
}

// ---------------------------------------------------------------- AI 修复
export interface RepairOp {
  issue_id: string
  slide_index: number
  defect_type: string
  label: string
  rule_id?: string | null
  title?: string
  action: 'move_inside' | 'wrap_and_grow' | 'raise_font' | 'bring_front' | 'manual'
  target_pt?: number | null
  status: 'pending' | 'pending-layer' | 'applied' | 'partial' | 'manual' | 'skipped'
  detail: string
  note: string
  /** 第几轮复核产生；单轮修复为 1 */
  round?: number
}

export interface RepairStage {
  key: string
  label: string
  status: 'pending' | 'active' | 'done' | 'failed'
}

export interface RepairPage {
  index: number
  before_url: string
  after_url: string | null
  /** 原始页面 + 缺陷编号框，用于「标注」视图与外部 LLM 修改 */
  annot_url?: string | null
  ops: number
  applied: number
  /** 已写入但画面没变（图层误报 / 改不动），不能算已修复 */
  partial?: number
  manual: number
  before: number
  after: number
  /** 修复后该页仍存在的可见重叠条数（不在自动修复范围内，需人工确认） */
  overlaps?: number
  /** 这一页是否真的产生了**可见**改动；几何动过但画面一致时也算 false */
  changed?: boolean
  /** 本页顺带整理了多少处间距/对齐 */
  tidy?: number
  /** 修复前/后渲染图的像素比对结果，是「图像有没有变」的唯一凭证 */
  visual?: { ratio: number; px: number; mean: number; visible: boolean } | null
  /** 上面那份比对的现成中文说明 */
  visual_note?: string
}

export interface RepairVerify {
  before_structure: number
  after_structure: number
  fixed: number
  introduced: number
  by_type: Record<string, { before: number; after: number }>
  pages: { index: number; before: number; after: number }[]
  /** 修复后复扫出的可见重叠；只报告、不算进结构问题 */
  overlaps?: { count: number; pages: { index: number; count: number; items: { kind: 'cover' | 'text'; a: string; b: string; ratio: number }[] }[] }
  /** 像素复核：渲染图真的变了才算修好 */
  visual?: { checked: number; visible_pages: number; identical: number[] }
}

export interface RepairRound {
  round: number
  issues: number
  applied: number
  manual: number
  before: number
  after: number
  introduced: number
  note: string
}

export interface RepairRecord {
  repair_id: string
  task_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  message: string
  plan_source: 'rule' | 'llm'
  model: string | null
  degraded: string
  plan_summary?: string
  created_at: string
  updated_at: string
  finished_at: string | null
  summary: { issues: number; applied: number; manual: number; skipped: number; partial: number; rounds?: number; hints?: number; unchanged?: number; visible_pages?: number; rule_resolved?: number }
  review?: { enabled: boolean; max_rounds: number; rounds: RepairRound[] }
  stages: RepairStage[]
  log: { at: string; text: string }[]
  ops: RepairOp[]
  pages: RepairPage[]
  output: { file: string; name: string; size: number; url: string; bundle_url?: string } | null
  verify: RepairVerify | null
  render?: { ok: boolean; error: string; pages: number }
}

export interface RepairProject {
  task: TaskSummary
  repair: {
    repair_id: string
    status: string
    progress: number
    summary: RepairRecord['summary']
    verify: RepairVerify | null
    created_at: string
    finished_at: string | null
  } | null
}

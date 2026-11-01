import type {
  Advice,
  AppSettings,
  Capabilities,
  ConnectionResult,
  Claim,
  Issue,
  ReviewPlan,
  ReviewRecord,
  ReviewGate,
  ReviewOptions,
  RecoveryInfo,
  RecoveryResult,
  RepairProject,
  RepairRecord,
  SlideDetail,
  SlideSummary,
  Summary,
  TaskDetail,
  TaskSummary,
} from './types'

export class ApiError extends Error {
  code: string
  status: number
  constructor(code: string, message: string, status: number) {
    super(message)
    this.code = code
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers || {}),
    },
  })
  const text = await res.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = null
  }
  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(err?.code || `http_${res.status}`, err?.message || `请求失败（${res.status}）`, res.status)
  }
  return body as T
}

const modeQuery = (mode: 'real' | 'demo') => (mode === 'demo' ? '&mode=demo' : '')

export const api = {
  capabilities: () => request<Capabilities>('/api/system/capabilities'),

  listTasks: (mode: 'real' | 'demo' = 'real') =>
    request<{ items: TaskSummary[]; total: number }>(`/api/tasks?limit=24&offset=0${mode === 'demo' ? '&mode=demo' : ''}`),

  getTask: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<TaskDetail>(`/api/tasks/${id}?mode=${mode}`),

  reviewGate: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<ReviewGate>(`/api/tasks/${id}/review-gate?mode=${mode}`),

  skipReview: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<ReviewGate>(`/api/tasks/${id}/skip-review?mode=${mode}`, { method: 'POST' }),

  recovery: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<RecoveryInfo>(`/api/tasks/${id}/recovery?mode=${mode}`),

  recover: (id: string, action: 'continue' | 'restart', mode: 'real' | 'demo' = 'real') =>
    request<RecoveryResult>(`/api/tasks/${id}/recover?mode=${mode}`, {
      method: 'POST', body: JSON.stringify({ action }),
    }),

  upload: (file: File, name: string, options: Partial<ReviewOptions>, prepareOnly = true) => {
    const form = new FormData()
    form.append('file', file)
    form.append('name', name)
    form.append('options', JSON.stringify(options))
    form.append('prepare_only', String(prepareOnly))
    return request<TaskSummary>('/api/tasks', { method: 'POST', body: form })
  },

  uploadWithProgress: (file: File, name: string, options: Partial<ReviewOptions>, prepareOnly: boolean,
                       onProgress: (ratio: number) => void) =>
    new Promise<TaskSummary>((resolve, reject) => {
      const form = new FormData()
      form.append('file', file)
      form.append('name', name)
      form.append('options', JSON.stringify(options))
      form.append('prepare_only', String(prepareOnly))
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/tasks')
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total)
      }
      xhr.onerror = () => reject(new ApiError('network_error', '上传失败：网络错误。', 0))
      xhr.onload = () => {
        let body: unknown = null
        try { body = JSON.parse(xhr.responseText) } catch { body = null }
        if (xhr.status >= 200 && xhr.status < 300) {
          onProgress(1)
          resolve(body as TaskSummary)
        } else {
          const err = (body as { error?: { code?: string; message?: string } } | null)?.error
          reject(new ApiError(err?.code || `http_${xhr.status}`, err?.message || `上传失败（${xhr.status}）`, xhr.status))
        }
      }
      xhr.send(form)
    }),

  startTask: (id: string, options: ReviewOptions, name?: string) =>
    request<TaskSummary>(`/api/tasks/${id}/start`, {
      method: 'POST',
      body: JSON.stringify({ options, name: name || null }),
    }),

  deleteTask: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<{ ok: boolean }>(`/api/tasks/${id}?mode=${mode}`, { method: 'DELETE' }),

  slides: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<{ slides: SlideSummary[]; checked_by: Record<string, boolean>; slide_count: number }>(
      `/api/tasks/${id}/slides?mode=${mode}`,
    ),

  slide: (id: string, index: number, mode: 'real' | 'demo' = 'real') =>
    request<SlideDetail>(`/api/tasks/${id}/slides/${index}?mode=${mode}`),

  claims: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<{ claims: Claim[]; capability?: unknown }>(`/api/tasks/${id}/claims?mode=${mode}`),

  reviewPlan: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<ReviewPlan>(`/api/tasks/${id}/review-plan?mode=${mode}`),

  reviewIssue: (id: string, issueId: string, question: string, llmBackend: 'mock' | 'api', mode: 'real' | 'demo' = 'real') =>
    request<{ issue: Issue; review: ReviewRecord; history: ReviewRecord[] }>(
      `/api/tasks/${id}/issues/${encodeURIComponent(issueId)}/review?mode=${mode}`,
      { method: 'POST', body: JSON.stringify({ question, llm_backend: llmBackend }) },
    ),

  issueReviews: (id: string, issueId: string, mode: 'real' | 'demo' = 'real') =>
    request<{ history: ReviewRecord[] }>(`/api/tasks/${id}/issues/${encodeURIComponent(issueId)}/reviews?mode=${mode}`),

  setVerdict: (id: string, issueId: string, status: 'dismissed' | 'confirmed',
               note = '', mode: 'real' | 'demo' = 'real') =>
    request<{ issue: Issue; summary: Summary; advice?: Advice; verdicts: Record<string, { status: string }> }>(
      `/api/tasks/${id}/issues/${encodeURIComponent(issueId)}/verdict?mode=${mode}`,
      { method: 'POST', body: JSON.stringify({ status, note }) },
    ),

  advice: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<Advice>(`/api/tasks/${id}/advice?mode=${mode}`),

  refreshAdvice: (id: string, backend: 'api' | 'rule', mode: 'real' | 'demo' = 'real') =>
    request<{ advice: Advice; summary: Summary }>(`/api/tasks/${id}/advice?mode=${mode}`, {
      method: 'POST', body: JSON.stringify({ backend }),
    }),

  promptUrl: (id: string, download = false, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/prompt?download=${download}${modeQuery(mode)}`,

  settings: () => request<AppSettings>('/api/settings'),

  saveSettings: (payload: Record<string, unknown>) =>
    request<AppSettings>('/api/settings', { method: 'PUT', body: JSON.stringify(payload) }),

  testConnection: (mode: 'text' | 'vision', apiKey?: string) =>
    request<{ result: ConnectionResult; settings: AppSettings }>('/api/settings/test-connection', {
      method: 'POST',
      body: JSON.stringify({ mode, api_key: apiKey || null }),
    }),

  clearKey: () => request<AppSettings>('/api/settings/api-key', { method: 'DELETE' }),

  createDemo: (scenario: string, name?: string) =>
    request<TaskSummary>('/api/demo/tasks', {
      method: 'POST',
      body: JSON.stringify({ scenario, name: name || '' }),
    }),

  demoScenarios: () =>
    request<{ items: { key: string; label: string; description: string }[] }>('/api/demo/scenarios'),

  reportUrl: (id: string, format: 'html' | 'json' | 'pdf', download = false, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/report?format=${format}${download ? '&download=true' : ''}${modeQuery(mode)}`,

  slideImage: (id: string, index: number, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/slides/${index}/image?kind=render${modeQuery(mode)}`,

  // ------------------------------------------------------------ AI 修复
  repairProjects: (mode: 'real' | 'demo' = 'real') =>
    request<{ items: RepairProject[] }>(`/api/repairs?mode=${mode}`),

  repairs: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<{ items: RepairRecord[] }>(`/api/tasks/${id}/repairs?mode=${mode}`),

  repair: (id: string, repairId: string, mode: 'real' | 'demo' = 'real') =>
    request<RepairRecord>(`/api/tasks/${id}/repairs/${repairId}?mode=${mode}`),

  startRepair: (id: string, backend: 'api' | 'rule' = 'api', mode: 'real' | 'demo' = 'real',
                review = false, rounds = 3) =>
    request<RepairRecord>(
      `/api/tasks/${id}/repairs?mode=${mode}&backend=${backend}&review=${review ? 1 : 0}&rounds=${rounds}`,
      { method: 'POST' },
    ),

  repairFileUrl: (id: string, repairId: string, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/repairs/${repairId}/file${mode === 'demo' ? '?mode=demo' : ''}`,

  repairSlideUrl: (id: string, repairId: string, index: number, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/repairs/${repairId}/slides/${index}/image${mode === 'demo' ? '?mode=demo' : ''}`,

  repairAnnotUrl: (id: string, repairId: string, index: number, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/repairs/${repairId}/slides/${index}/annot${mode === 'demo' ? '?mode=demo' : ''}`,

  repairBrief: (id: string, repairId: string, index: number, mode: 'real' | 'demo' = 'real') =>
    request<{ slide: number; hint: string; prompt: string; issues: { number: number; label: string; title?: string; element?: string; status?: string; detail?: string }[] }>(
      `/api/tasks/${id}/repairs/${repairId}/brief/${index}?mode=${mode}`),

  repairHints: (id: string, mode: 'real' | 'demo' = 'real') =>
    request<{ hints: Record<string, string> }>(`/api/tasks/${id}/repair-hints?mode=${mode}`),

  saveRepairHint: (id: string, slideIndex: number, text: string, mode: 'real' | 'demo' = 'real') =>
    request<{ hints: Record<string, string> }>(`/api/tasks/${id}/repair-hints?mode=${mode}`, {
      method: 'PUT',
      body: JSON.stringify({ slide_index: slideIndex, text }),
    }),

  repairBundleUrl: (id: string, repairId: string, mode: 'real' | 'demo' = 'real') =>
    `/api/tasks/${id}/repairs/${repairId}/bundle${mode === 'demo' ? '?mode=demo' : ''}`,
}

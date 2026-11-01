import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { AppSettings, Capabilities, ConnectionResult } from '../lib/types'
import { Icon } from '../components/Icon'
import { Skeleton, Toggle } from '../components/Ui'
import { toast } from '../components/Toasts'
import { formatDate } from '../lib/format'

const TEST_LABEL: Record<string, { text: string; cls: string }> = {
  ok: { text: '连接成功', cls: 'ok' },
  auth_failed: { text: '鉴权失败', cls: 'high' },
  network_error: { text: '网络失败', cls: 'high' },
  timeout: { text: '请求超时', cls: 'high' },
  incompatible: { text: '接口不兼容', cls: 'high' },
  vision_unsupported: { text: '多模态能力不满足', cls: 'high' },
  rate_limited: { text: '被限流', cls: 'high' },
  api_error: { text: '接口报错', cls: 'high' },
  invalid_config: { text: '配置不完整', cls: 'high' },
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [form, setForm] = useState<AppSettings | null>(null)
  const [key, setKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState<'text' | 'vision' | null>(null)
  const [result, setResult] = useState<ConnectionResult | null>(null)
  const [error, setError] = useState('')

  const load = async () => {
    try {
      const [s, c] = await Promise.all([api.settings(), api.capabilities()])
      setSettings(s)
      setForm(s)
      setCaps(c)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  useEffect(() => { void load() }, [])

  const patch = <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))
  }

  const save = async (silent = false) => {
    if (!form) return false
    setSaving(true)
    setError('')
    try {
      const payload: Record<string, unknown> = {
        api_base_url: form.api_base_url,
        api_model: form.api_model,
        api_timeout_s: form.api_timeout_s,
        api_max_retries: form.api_max_retries,
        api_max_concurrency: form.api_max_concurrency,
        api_temperature: form.api_temperature,
        max_calls_per_task: form.max_calls_per_task,
        allow_private_base_url: form.allow_private_base_url,
        allow_insecure_base_url: form.allow_insecure_base_url,
        response_format_json: form.response_format_json,
      }
      if (key.trim()) payload.api_key = key.trim()
      const next = await api.saveSettings(payload)
      setSettings(next)
      setForm(next)
      setKey('')
      if (!silent) toast('配置已保存，密钥只保存在本机后端。', 'success')
      api.capabilities().then(setCaps).catch(() => undefined)
      return true
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '保存失败。'
      setError(message)
      if (!silent) toast(message, 'error')
      return false
    } finally {
      setSaving(false)
    }
  }

  const test = async (mode: 'text' | 'vision') => {
    if (!form) return
    if (!form.api_base_url || !form.api_model) {
      toast('请先填写并保存 Base URL 与模型名称。', 'error')
      return
    }
    setTesting(mode)
    setResult(null)
    try {
      const saved = await save(true)
      if (!saved) return
      const res = await api.testConnection(mode, key.trim() || undefined)
      setResult(res.result)
      setSettings(res.settings)
      if (res.result.status === 'ok') {
        toast(mode === 'vision' ? '图像请求成功，可启用云端复核。' : '文本请求成功。', 'success')
      } else {
        toast(res.result.message, 'error')
      }
      api.capabilities().then(setCaps).catch(() => undefined)
    } catch (err) {
      toast(err instanceof ApiError ? err.message : '测试失败。', 'error')
    } finally {
      setTesting(null)
    }
  }

  const clearKey = async () => {
    if (!window.confirm('清除本机保存的 API Key？')) return
    try {
      const next = await api.clearKey()
      setSettings(next)
      setForm(next)
      toast('已清除本机 API Key。', 'success')
    } catch (err) {
      toast((err as Error).message, 'error')
    }
  }

  if (!form) {
    return <div className="stack"><Skeleton height={120} /><Skeleton height={320} /></div>
  }

  const status = result?.status ? TEST_LABEL[result.status] : null

  // 只有"非本机 + 明文 http"才需要用户手动放行，本机回环地址一直允许
  const isPlainHttpRemote = (() => {
    const raw = (form.api_base_url || '').trim().toLowerCase()
    if (!raw.startsWith('http://')) return false
    const host = raw.slice(7).split(/[/?#]/)[0].split(':')[0]
    return !['localhost', '127.0.0.1', '::1', '[::1]'].includes(host)
  })()

  return (
    <div className="fade-up">
      <div className="page-head">
        <div>
          <span className="eyebrow">Settings & credentials</span>
          <h1 style={{ marginTop: 10 }}>系统与 API 配置</h1>
          <div className="lead">配置只保存在本机后端；密钥不会返回前端，也不会写入报告或日志。</div>
        </div>
        <span className={`chip ${caps?.modules.visual.available ? 'green' : 'gray'}`}>
          <i className="dot" />视觉模型{caps?.modules.visual.available ? '已接入' : '尚未接入'}
        </span>
      </div>

      {error && <div className="card" style={{ padding: 14, color: 'var(--red)', marginBottom: 14 }}>{error}</div>}

      <div className="grid-2">
        <div className="stack">
          <div className="card">
            <div className="config-section">
              <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <div>
                  <h3>OpenAI 兼容多模态接口</h3>
                  <p className="desc">支持任何兼容 <span className="mono">/chat/completions</span> 的服务；不写死服务商。</p>
                </div>
                <span className={`chip ${settings?.api_key_set ? 'green' : 'gray'}`}>
                  <i className="dot" />{settings?.api_key_set ? 'Key 已保存' : '未保存 Key'}
                </span>
              </div>

              <div className="stack" style={{ gap: 14 }}>
                <div className="field">
                  <label htmlFor="base">API Base URL</label>
                  <input id="base" className="input" placeholder="https://api.example.com/v1"
                    value={form.api_base_url}
                    onChange={(e) => patch('api_base_url', e.target.value)} />
                  <div className="hint">
                    必须以 <span className="mono">/v1</span> 结尾（或包含完整 <span className="mono">/chat/completions</span>）。
                    非本机地址默认要求 HTTPS；如确需明文，请打开下方「允许非本机 HTTP」。
                  </div>
                  {isPlainHttpRemote && (
                    <div className="hint" style={{ color: 'var(--red)' }}>
                      当前填的是非本机 <span className="mono">http://</span> 地址，
                      {form.allow_insecure_base_url
                        ? '已开启放行，保存后将直接以明文访问。'
                        : '保存会被拒绝，请先打开「允许非本机 HTTP」。'}
                    </div>
                  )}
                </div>

                <div className="field">
                  <label htmlFor="key">API Key</label>
                  <div className="row" style={{ gap: 8 }}>
                    <input id="key" className="input" type={showKey ? 'text' : 'password'}
                      placeholder={settings?.api_key_set ? `已保存 ${settings.api_key_masked}，留空表示不修改` : 'sk-...'}
                      value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" />
                    <button className="icon-btn" aria-label={showKey ? '隐藏密钥' : '显示密钥'} onClick={() => setShowKey((v) => !v)}>
                      <Icon name={showKey ? 'eyeOff' : 'eye'} size={16} />
                    </button>
                  </div>
                  <div className="hint">只保存在后端 <span className="mono">data/secrets.json</span>，前端仅能看到掩码。</div>
                </div>

                <div className="field">
                  <label htmlFor="model">模型名称</label>
                  <input id="model" className="input" placeholder="例如 gpt-4o-mini / qwen-vl-max / glm-4v"
                    value={form.api_model} onChange={(e) => patch('api_model', e.target.value)} />
                </div>

                <div className="input-row">
                  <div className="field">
                    <label>超时（秒）</label>
                    <input className="input" type="number" min={5} max={600} value={form.api_timeout_s}
                      onChange={(e) => patch('api_timeout_s', Number(e.target.value))} />
                  </div>
                  <div className="field">
                    <label>最大重试次数</label>
                    <input className="input" type="number" min={0} max={5} value={form.api_max_retries}
                      onChange={(e) => patch('api_max_retries', Number(e.target.value))} />
                  </div>
                  <div className="field">
                    <label>最大并发</label>
                    <input className="input" type="number" min={1} max={8} value={form.api_max_concurrency}
                      onChange={(e) => patch('api_max_concurrency', Number(e.target.value))} />
                  </div>
                  <div className="field">
                    <label>温度</label>
                    <input className="input" type="number" step={0.1} min={0} max={2} value={form.api_temperature}
                      onChange={(e) => patch('api_temperature', Number(e.target.value))} />
                  </div>
                </div>

                <div className="input-row">
                  <div className="field">
                    <label>单任务默认调用上限</label>
                    <input className="input" type="number" min={0} max={50} value={form.max_calls_per_task}
                      onChange={(e) => patch('max_calls_per_task', Number(e.target.value))} />
                    <div className="hint">任务中设置的预算不能超过此上限。</div>
                  </div>
                  <div className="field">
                    <label>请求选项</label>
                    <div className="row" style={{ height: 40 }}>
                      <Toggle label="JSON 响应格式" checked={form.response_format_json}
                        onChange={(v) => patch('response_format_json', v)} />
                      <span className="small muted">要求 response_format=json_object</span>
                    </div>
                  </div>
                </div>

                <div className="option-row" style={{ borderTop: '1px solid var(--line)' }}>
                  <div className="txt">
                    <b>允许内网 Base URL</b>
                    <p>本地部署的模型服务（如 Ollama、vLLM）通常解析到 127.0.0.1 以外的内网地址。开启前请确认目标可信。</p>
                  </div>
                  <Toggle label="允许内网 Base URL" checked={form.allow_private_base_url}
                    onChange={(v) => patch('allow_private_base_url', v)} />
                </div>

                <div className="option-row" style={{ borderTop: '1px solid var(--line)' }}>
                  <div className="txt">
                    <b>允许非本机 HTTP</b>
                    <p>
                      默认要求非本机地址使用 HTTPS。开启后可填写 <span className="mono">http://10.0.0.5:8000/v1</span>
                      这类明文地址，用于自建网关、内网代理或只提供 http 的推理服务。
                      本机回环地址不受此开关影响；云元数据地址与 URL 内嵌凭据仍会被拒绝。
                      {isPlainHttpRemote && form.allow_insecure_base_url && (
                        <><br /><span style={{ color: 'var(--red)' }}>
                          已放行：请求内容将以明文传输，请仅在可信网络内使用。
                        </span></>
                      )}
                    </p>
                  </div>
                  <Toggle label="允许非本机 HTTP" checked={form.allow_insecure_base_url}
                    onChange={(v) => patch('allow_insecure_base_url', v)} />
                </div>
              </div>

              <div className="row" style={{ marginTop: 16, flexWrap: 'wrap' }}>
                <button className="btn btn-primary" onClick={() => void save()} disabled={saving}>
                  {saving ? '保存中…' : '保存配置'}
                </button>
                <button className="btn" onClick={() => void test('text')} disabled={testing !== null}>
                  {testing === 'text' ? <Icon name="spinner" size={15} className="spin" /> : <Icon name="refresh" size={15} />}
                  测试文本连接
                </button>
                <button className="btn" onClick={() => void test('vision')} disabled={testing !== null}>
                  {testing === 'vision' ? <Icon name="spinner" size={15} className="spin" /> : <Icon name="image" size={15} />}
                  测试图像能力
                </button>
                {settings?.api_key_set && (
                  <button className="btn btn-ghost btn-sm" onClick={() => void clearKey()}>清除 Key</button>
                )}
              </div>

              {result && status && (
                <div className="card" style={{ marginTop: 14, padding: 14, background: result.status === 'ok' ? '#f3fbf8' : '#fff7f7', borderColor: result.status === 'ok' ? '#bfe9dc' : '#f6c9cb' }}>
                  <div className="row">
                    <span className={`badge ${status.cls}`}>{status.text}</span>
                    <span className="small" style={{ fontWeight: 600 }}>{result.mode === 'vision' ? '图像请求' : '文本请求'}</span>
                    {result.latency_ms != null && <span className="tiny muted">{result.latency_ms} ms</span>}
                    {result.http_status != null && <span className="tiny muted">HTTP {result.http_status}</span>}
                  </div>
                  <div className="small" style={{ marginTop: 6, color: 'var(--ink-2)' }}>{result.message}</div>
                </div>
              )}

              {settings?.last_test && !result && (
                <div className="tiny muted" style={{ marginTop: 12 }}>
                  上次测试：{TEST_LABEL[settings.last_test.status]?.text || settings.last_test.status} · {formatDate(settings.last_test.tested_at)} · {settings.last_test.model}
                </div>
              )}
            </div>
          </div>

          <div className="card" style={{ padding: '16px 20px', background: '#fbfcff' }}>
            <div className="row" style={{ alignItems: 'flex-start', gap: 10 }}>
              <Icon name="shield" size={18} style={{ color: 'var(--cyan)', flex: 'none', marginTop: 2 }} />
              <div className="small" style={{ color: 'var(--ink-2)', lineHeight: 1.7 }}>
                <b>隐私与授权</b><br />
                API 配置完整时，新任务默认启用多模态复检；可在任务配置页关闭，或在首轮结束后的 5 秒窗口内跳过。复检开始后，
                被选中页面的渲染图与结构证据才会发送到你配置的提供方。API 失败时，已经完成的结构检查与页面预览会保留。
              </div>
            </div>
          </div>
        </div>

        <div className="stack">
          <div className="card" style={{ padding: 18 }}>
            <div className="h3" style={{ marginBottom: 10 }}>模块状态</div>
            <div className="stack small" style={{ gap: 12 }}>
              <div>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span>结构规则</span><span className="badge rule">已接入</span>
                </div>
                <div className="tiny muted" style={{ marginTop: 3 }}>5 条规则 · 无需任何 Key</div>
              </div>
              <div>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span>视觉分类</span>
                  <span className={`badge ${caps?.modules.visual.available ? 'model' : 'off'}`}>
                    {caps?.modules.visual.available ? '已接入' : '未接入'}
                  </span>
                </div>
                <div className="tiny muted" style={{ marginTop: 3 }}>
                  {caps?.modules.visual.available ? caps.modules.visual.model_version : '等待 4.3G-2 最终模型'}
                </div>
              </div>
              <div>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <span>多模态复核</span>
                  <span className={`badge ${caps?.modules.mllm.state === 'ready' ? 'ai' : 'off'}`}>
                    {caps?.modules.mllm.state === 'ready' ? '已配置' : '未配置'}
                  </span>
                </div>
                <div className="tiny muted" style={{ marginTop: 3 }}>
                  {caps?.modules.mllm.vision_verified ? '图像能力已验证' : '图像能力未验证'}
                </div>
              </div>
            </div>
          </div>

          <div className="card" style={{ padding: 18 }}>
            <div className="h3" style={{ marginBottom: 8 }}>安全边界</div>
            <ul className="small" style={{ margin: 0, paddingLeft: 17, color: 'var(--muted)', lineHeight: 1.75 }}>
              <li>Key 只由后端读取，接口只返回掩码。</li>
              <li>非本机地址默认要求 HTTPS；明文 HTTP 需手动放行，且始终阻断云元数据地址。</li>
              <li>设置接口仅接受本机回环地址。</li>
              <li>不记录请求头，不在报告中输出密钥。</li>
            </ul>
          </div>

          <div className="card" style={{ padding: 18 }}>
            <div className="h3" style={{ marginBottom: 6 }}>接入最终模型</div>
            <div className="small muted" style={{ lineHeight: 1.75 }}>
              将模型实现放入 <span className="mono">backend/models/</span>，并提供 <span className="mono">visual_model.json</span> 清单。
              服务会自动加载，前端、任务与报告无需改动。
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

import { useRef, useState } from 'react'
import { Icon } from './Icon'
import { Spinner } from './Ui'

interface Props {
  onFile: (file: File) => Promise<void> | void
  busy: boolean
  busyLabel?: string
  progress?: number
  maxMb: number
  maxSlides: number
}

export function UploadZone({ onFile, busy, busyLabel, progress, maxMb, maxSlides }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const depth = useRef(0)
  const [drag, setDrag] = useState(false)

  const pick = () => {
    if (!busy) inputRef.current?.click()
  }

  const handleFiles = async (files: FileList | null) => {
    const file = files?.[0]
    if (file) await onFile(file)
    if (inputRef.current) inputRef.current.value = ''
  }

  return (
    <div
      className={`dropzone${drag ? ' drag' : ''}${busy ? ' busy' : ''}`}
      role="button"
      tabIndex={0}
      aria-label="上传 PPTX 文件"
      onClick={pick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          pick()
        }
      }}
      onDragEnter={(e) => {
        e.preventDefault()
        depth.current += 1
        setDrag(true)
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={(e) => {
        e.preventDefault()
        depth.current -= 1
        if (depth.current <= 0) setDrag(false)
      }}
      onDrop={(e) => {
        e.preventDefault()
        depth.current = 0
        setDrag(false)
        void handleFiles(e.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation"
        hidden
        onChange={(e) => void handleFiles(e.target.files)}
      />

      <div className={`dz-icon${busy ? ' busy' : ''}`}>
        {busy ? <Spinner size={24} /> : <Icon name="upload" size={24} />}
      </div>

      <div className="dz-title">
        {busy ? (busyLabel || '正在上传并解析…') : '拖入你的 PPT，开启智能设计审查'}
      </div>
      <div className="dz-sub">
        {busy
          ? (typeof progress === 'number' && progress < 1
              ? `正在上传 ${Math.round(progress * 100)}% · 完成后将校验并渲染页面`
              : '正在校验 OpenXML、解析对象结构并按原始比例渲染页面。')
          : '支持 .pptx 格式，保留原始排版与比例'}
      </div>

      {busy && typeof progress === 'number' && (
        <div className="dz-progress">
          <i style={{ width: `${Math.max(4, Math.round(progress * 100))}%` }} />
        </div>
      )}

      <button type="button" className="btn btn-primary dz-btn" disabled={busy}
        onClick={(e) => { e.stopPropagation(); pick() }}>
        <Icon name="file" size={16} />
        选择 PPTX 文件
      </button>

      <div className="dz-meta">
        <span>.pptx</span>
        <i />
        <span>≤ {maxMb} MB</span>
        <i />
        <span>≤ {maxSlides} 页</span>
      </div>
    </div>
  )
}

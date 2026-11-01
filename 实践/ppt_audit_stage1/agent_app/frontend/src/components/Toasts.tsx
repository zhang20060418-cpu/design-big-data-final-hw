import { useEffect, useState } from 'react'
import { Icon } from './Icon'

type Kind = 'info' | 'success' | 'error'
type Item = { id: number; message: string; kind: Kind }

export function toast(message: string, kind: Kind = 'info') {
  window.dispatchEvent(new CustomEvent('slidesentinel:toast', { detail: { message, kind } }))
}

export function Toasts() {
  const [items, setItems] = useState<Item[]>([])
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ message: string; kind: Kind }>).detail
      const id = Date.now() + Math.random()
      setItems((prev) => [...prev.slice(-3), { id, ...detail }])
      window.setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== id)), 5600)
    }
    window.addEventListener('slidesentinel:toast', handler)
    return () => window.removeEventListener('slidesentinel:toast', handler)
  }, [])
  return (
    <div className="toast-wrap" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={`toast ${item.kind}`}>
          <span className="ic">
            <Icon name={item.kind === 'error' ? 'alert' : item.kind === 'success' ? 'check' : 'info'} size={16} />
          </span>
          <span>{item.message}</span>
        </div>
      ))}
    </div>
  )
}

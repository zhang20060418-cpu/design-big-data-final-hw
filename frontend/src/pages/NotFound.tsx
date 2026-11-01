import { useNavigate } from 'react-router-dom'
import { Icon } from '../components/Icon'

export default function NotFound() {
  const navigate = useNavigate()
  return (
    <div className="card" style={{ padding: 40, textAlign: 'center', maxWidth: 560, margin: '40px auto' }}>
      <div style={{ width: 54, height: 54, borderRadius: 16, background: '#eef1ff', color: 'var(--indigo)', display: 'grid', placeItems: 'center', margin: '0 auto 14px' }}>
        <Icon name="scan" size={26} />
      </div>
      <div className="h2">页面不存在</div>
      <p className="small muted" style={{ marginTop: 6 }}>链接可能已失效，或任务已被删除。</p>
      <button className="btn btn-primary" style={{ marginTop: 16 }} onClick={() => navigate('/')}>回到工作台</button>
    </div>
  )
}

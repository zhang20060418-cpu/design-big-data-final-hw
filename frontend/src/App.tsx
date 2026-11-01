import { useEffect, useState } from 'react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { Sidebar } from './components/Sidebar'
import { Topbar } from './components/Topbar'
import { Toasts } from './components/Toasts'
import Home from './pages/Home'
import Configure from './pages/Configure'
import Progress from './pages/Progress'
import Results from './pages/Results'
import ReportPage from './pages/Report'
import Reports from './pages/Reports'
import Repair from './pages/Repair'
import RepairReport from './pages/RepairReport'
import RepairProgress from './pages/RepairProgress'
import RepairResult from './pages/RepairResult'
import SettingsPage from './pages/Settings'
import History from './pages/History'
import NotFound from './pages/NotFound'

const RAIL_KEY = 'slidesentinel.rail.collapsed'

function readCollapsed() {
  try {
    return localStorage.getItem(RAIL_KEY) === '1'
  } catch {
    return false
  }
}

function ScrollToTop() {
  const { pathname } = useLocation()
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [pathname])
  return null
}

export default function App() {
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const [topbarHidden, setTopbarHidden] = useState(false)
  const { pathname } = useLocation()

  useEffect(() => {
    try {
      localStorage.setItem(RAIL_KEY, collapsed ? '1' : '0')
    } catch {
      /* 隐私模式下忽略 */
    }
  }, [collapsed])

  // 下滑收起顶栏，上滑或回到顶部再展开
  useEffect(() => {
    let lastY = window.scrollY
    let raf = 0
    const onScroll = () => {
      if (raf) return
      raf = window.requestAnimationFrame(() => {
        raf = 0
        const y = Math.max(0, window.scrollY)
        const delta = y - lastY
        if (y <= 8) setTopbarHidden(false)
        else if (delta > 6 && y > 64) setTopbarHidden(true)
        else if (delta < -6) setTopbarHidden(false)
        lastY = y
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      window.removeEventListener('scroll', onScroll)
      if (raf) window.cancelAnimationFrame(raf)
    }
  }, [])

  // 换页后回到顶部，顶栏也要立刻还原
  useEffect(() => {
    setTopbarHidden(false)
  }, [pathname])

  // Ctrl+K 等操作要把收起的顶栏拉回来
  useEffect(() => {
    const show = () => setTopbarHidden(false)
    window.addEventListener('slidesentinel:topbar-show', show)
    return () => window.removeEventListener('slidesentinel:topbar-show', show)
  }, [])

  return (
    <div className={`app${collapsed ? ' is-collapsed' : ''}${topbarHidden ? ' is-topbar-hidden' : ''}`}>
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)} />
      <div className="shell">
        <Topbar />
        <main className="main">
          <div className="page">
            <ScrollToTop />
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/tasks" element={<History />} />
              <Route path="/tasks/:taskId/configure" element={<Configure />} />
              <Route path="/tasks/:taskId/progress" element={<Progress />} />
              <Route path="/tasks/:taskId/results" element={<Results />} />
              <Route path="/tasks/:taskId/report" element={<ReportPage />} />
              <Route path="/reports" element={<Reports />} />
              <Route path="/repair" element={<Repair />} />
              <Route path="/repair/:taskId" element={<RepairReport />} />
              <Route path="/repair/:taskId/progress" element={<RepairProgress />} />
              <Route path="/repair/:taskId/result" element={<RepairResult />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </div>
        </main>
      </div>
      <Toasts />
    </div>
  )
}

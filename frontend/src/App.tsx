import { Link, Route, Routes } from 'react-router-dom'
import { HealthIndicator } from './components/HealthIndicator'
import { CreateSessionPage } from './pages/CreateSessionPage'
import { SessionDetailPage } from './pages/SessionDetailPage'

export default function App() {
  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <header className="border-b border-white/10">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
          <Link to="/" className="flex items-baseline gap-2">
            <span className="text-sm font-semibold tracking-tight">
              Sales Research Copilot
            </span>
            <span className="text-xs text-slate-600">grounded briefings</span>
          </Link>
          <HealthIndicator />
        </div>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<CreateSessionPage />} />
          <Route path="/sessions/:id" element={<SessionDetailPage />} />
        </Routes>
      </main>
    </div>
  )
}

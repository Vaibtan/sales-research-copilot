import { Link } from 'react-router-dom'
import { useSessionHistory } from '../hooks/useSessionHistory'
import type { SessionSummary } from '../types'
import { StatusBadge } from './StatusBadge'

export function SessionHistory() {
  const { state, sessions, error } = useSessionHistory()

  return (
    <section className="mt-12">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Recent runs
      </h2>

      <div className="mt-3">
        {state === 'loading' ? (
          <p className="text-sm text-slate-600">Loading history…</p>
        ) : state === 'error' ? (
          <p className="text-sm text-rose-300">{error}</p>
        ) : sessions.length === 0 ? (
          <p className="text-sm text-slate-600">
            No runs yet — start one above.
          </p>
        ) : (
          <ul className="divide-y divide-white/5 overflow-hidden rounded-xl border border-white/10">
            {sessions.map((s) => (
              <HistoryRow key={s.id} session={s} />
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

function HistoryRow({ session }: { session: SessionSummary }) {
  return (
    <li>
      <Link
        to={`/sessions/${session.id}`}
        className="flex items-center gap-3 px-4 py-3 transition hover:bg-white/[0.03]"
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-slate-200">
              {session.company}
            </span>
            <StatusBadge status={session.status} />
          </div>
          <p className="mt-0.5 truncate text-xs text-slate-500">
            {session.objective}
          </p>
        </div>
        <time
          className="shrink-0 font-mono text-xs text-slate-600"
          dateTime={session.created_at}
          title={new Date(session.created_at).toLocaleString()}
        >
          {relativeTime(session.created_at)}
        </time>
      </Link>
    </li>
  )
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  const diffSec = Math.round((Date.now() - then) / 1000)
  if (diffSec < 60) return 'just now'
  const diffMin = Math.round(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.round(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.round(diffHr / 24)
  return `${diffDay}d ago`
}

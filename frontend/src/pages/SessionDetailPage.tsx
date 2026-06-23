import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ProgressView } from '../components/ProgressView'
import { ReportView } from '../components/ReportView'
import { StatusBadge } from '../components/StatusBadge'
import { isTerminal, useLiveSession } from '../hooks/useLiveSession'
import { resumeSession } from '../lib/api'
import type { ProgressEvent, SessionDetail } from '../types'

export function SessionDetailPage() {
  const { id = '' } = useParams()
  const { detail, progress, status, loading, error, reload } = useLiveSession(id)

  if (loading) {
    return <CenteredNote>Loading session…</CenteredNote>
  }
  if ((error && !detail) || !detail) {
    return (
      <CenteredNote tone="error">
        {error ?? 'Session not found.'}{' '}
        <Link to="/" className="text-sky-400 hover:underline">
          Start a new run
        </Link>
      </CenteredNote>
    )
  }

  const effectiveStatus = status ?? detail.status
  const terminal = isTerminal(effectiveStatus)
  const degraded = detail.degraded || progress?.degraded || false
  const failed = effectiveStatus === 'failed'

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <Header session={detail} status={effectiveStatus} />

      {degraded && (
        <div className="mt-4 rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-sm text-amber-200">
          <p>
            This briefing is <strong>partial</strong> — some research steps failed
            after retries. Unknowns capture what could not be grounded.
          </p>
          {terminal && (
            <ResumeButton id={id} onResumed={reload} label="Retry the failed steps" />
          )}
        </div>
      )}

      <div className="mt-8">
        {terminal && detail.report ? (
          <ReportView report={detail.report} />
        ) : terminal && failed ? (
          <CenteredNote tone="error">
            The run failed before producing a briefing.
            <div className="mt-4">
              <ResumeButton id={id} onResumed={reload} label="Resume the run" />
            </div>
          </CenteredNote>
        ) : terminal && !detail.report ? (
          <CenteredNote tone="error">
            No report was produced for this run.
          </CenteredNote>
        ) : (
          <Running progress={progress} />
        )}
      </div>
    </div>
  )
}

function ResumeButton({
  id,
  onResumed,
  label,
}: {
  id: string
  onResumed: () => void
  label: string
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function onClick() {
    setBusy(true)
    setErr(null)
    try {
      const { resumed } = await resumeSession(id)
      // Re-attach the live stream so the upgrade renders (resumed=false → no-op,
      // a reload still refreshes the latest detail).
      onResumed()
      if (!resumed) setErr('Nothing to resume — the run already completed.')
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not resume the run')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={onClick}
        disabled={busy}
        className="inline-flex items-center rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-1.5 text-xs font-semibold text-amber-100 transition hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? 'Resuming…' : label}
      </button>
      {err && <p className="mt-2 text-xs text-rose-300">{err}</p>}
    </div>
  )
}

function Header({
  session,
  status,
}: {
  session: SessionDetail
  status: string
}) {
  return (
    <header className="border-b border-white/10 pb-6">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-xs text-slate-500 hover:text-slate-300">
          ← New run
        </Link>
        <StatusBadge status={status} />
        {session.report && (
          <span className="text-xs text-slate-500">
            v{session.report.version}
            {session.report.quality_score != null &&
              ` · quality ${session.report.quality_score.toFixed(2)}`}
          </span>
        )}
      </div>
      <h1 className="mt-3 text-2xl font-semibold tracking-tight text-slate-100">
        {session.company}
      </h1>
      <p className="mt-1 text-sm text-slate-400">{session.objective}</p>
      <p className="mt-2 font-mono text-xs text-slate-600">
        {session.company_key}
        {session.website ? ` · ${session.website}` : ''}
      </p>
    </header>
  )
}

function Running({ progress }: { progress: ProgressEvent | null }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-6 py-8">
      <p className="mb-6 text-sm text-slate-400">
        Researching live — the briefing will render here when the run completes.
      </p>
      <ProgressView progress={progress} />
    </div>
  )
}

function CenteredNote({
  children,
  tone = 'neutral',
}: {
  children: React.ReactNode
  tone?: 'neutral' | 'error'
}) {
  return (
    <div className="mx-auto max-w-3xl px-4 py-20 text-center">
      <p
        className={`text-sm ${tone === 'error' ? 'text-rose-300' : 'text-slate-400'}`}
      >
        {children}
      </p>
    </div>
  )
}

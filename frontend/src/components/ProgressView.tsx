import type { ProgressEvent } from '../types'

/** The graph's user-facing phases, in order; keys are §6.2 statuses. */
const PHASES: { key: string; label: string }[] = [
  { key: 'planning', label: 'Planning research questions' },
  { key: 'recalling', label: 'Recalling prior research' },
  { key: 'gathering', label: 'Gathering sources' },
  { key: 'analyzing', label: 'Extracting & analyzing facts' },
  { key: 'grading', label: 'Grading draft quality' },
  { key: 'reporting', label: 'Assembling briefing' },
]

function phaseIndex(status: string): number {
  const i = PHASES.findIndex((p) => p.key === status)
  if (i !== -1) return i
  if (status === 'done' || status === 'degraded') return PHASES.length
  return 0 // failed / unknown → nothing completed
}

type RowState = 'done' | 'active' | 'pending'

export function ProgressView({ progress }: { progress: ProgressEvent | null }) {
  const status = progress?.status ?? 'planning'
  const failed = status === 'failed'
  const current = phaseIndex(status)

  return (
    <div className="space-y-8">
      <ol className="space-y-1">
        {PHASES.map((phase, i) => {
          const state: RowState =
            i < current ? 'done' : i === current && !failed ? 'active' : 'pending'
          return <PhaseRow key={phase.key} label={phase.label} state={state} />
        })}
      </ol>

      {progress && <LiveDetails progress={progress} />}

      {failed && (
        <p className="rounded-lg border border-rose-500/20 bg-rose-500/5 px-4 py-3 text-sm text-rose-300">
          The run failed before producing a briefing. Start a new run to retry.
        </p>
      )}
    </div>
  )
}

function PhaseRow({ label, state }: { label: string; state: RowState }) {
  return (
    <li className="flex items-center gap-3 py-1.5">
      <Marker state={state} />
      <span
        className={
          state === 'done'
            ? 'text-sm text-slate-400'
            : state === 'active'
              ? 'text-sm font-medium text-slate-100'
              : 'text-sm text-slate-600'
        }
      >
        {label}
      </span>
    </li>
  )
}

function Marker({ state }: { state: RowState }) {
  if (state === 'done') {
    return (
      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-400">
        <svg viewBox="0 0 20 20" fill="currentColor" className="h-3 w-3">
          <path
            fillRule="evenodd"
            d="M16.7 5.3a1 1 0 010 1.4l-7.5 7.5a1 1 0 01-1.4 0L3.3 9.7a1 1 0 011.4-1.4l3.3 3.3 6.8-6.8a1 1 0 011.4 0z"
            clipRule="evenodd"
          />
        </svg>
      </span>
    )
  }
  if (state === 'active') {
    return (
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-sky-500/30 border-t-sky-400" />
    )
  }
  return <span className="h-5 w-5 rounded-full border border-white/10" />
}

function LiveDetails({ progress }: { progress: ProgressEvent }) {
  return (
    <div className="space-y-4 rounded-2xl border border-white/10 bg-slate-900/40 p-5">
      <div className="flex flex-wrap gap-2">
        <Stat label="Questions" value={progress.plan.length} />
        <Stat label="Sources" value={progress.sources.length} />
        <Stat label="Facts" value={progress.facts_count} />
        <Stat
          label="Draft"
          value={progress.draft_ready ? 'ready' : '—'}
          highlight={progress.draft_ready}
        />
      </div>

      {progress.plan.length > 0 && (
        <Panel title="Research plan">
          <ul className="space-y-1.5">
            {progress.plan.map((q, i) => (
              <li key={i} className="text-sm leading-relaxed text-slate-300">
                <span className="mr-2 font-mono text-xs text-slate-600">
                  {i + 1}
                </span>
                {q}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {progress.sources.length > 0 && (
        <Panel title={`Discovered sources (${progress.sources.length})`}>
          <ul className="space-y-1.5">
            {progress.sources.map((s) => (
              <li key={s.id} className="truncate text-sm text-slate-300">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  className="hover:text-sky-300"
                  title={s.url}
                >
                  {s.title}
                </a>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  highlight,
}: {
  label: string
  value: string | number
  highlight?: boolean
}) {
  return (
    <div className="rounded-lg border border-white/10 bg-white/[0.02] px-3 py-1.5">
      <span className="text-xs text-slate-500">{label}</span>{' '}
      <span
        className={`text-sm font-semibold ${highlight ? 'text-emerald-300' : 'text-slate-200'}`}
      >
        {value}
      </span>
    </div>
  )
}

function Panel({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <div>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </h4>
      {children}
    </div>
  )
}

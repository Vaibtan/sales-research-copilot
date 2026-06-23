const TONES: Record<string, string> = {
  done: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  degraded: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
  failed: 'border-rose-500/30 bg-rose-500/10 text-rose-300',
}

const RUNNING = 'border-sky-500/30 bg-sky-500/10 text-sky-300'

const LABELS: Record<string, string> = {
  planning: 'Planning',
  awaiting_approval: 'Awaiting approval',
  recalling: 'Recalling',
  gathering: 'Gathering',
  analyzing: 'Analyzing',
  grading: 'Grading',
  reporting: 'Reporting',
  done: 'Done',
  degraded: 'Degraded',
  failed: 'Failed',
}

export function StatusBadge({ status }: { status: string }) {
  const tone = TONES[status] ?? RUNNING
  const label = LABELS[status] ?? status
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${tone}`}
    >
      {label}
    </span>
  )
}

import { useHealth } from '../hooks/useHealth'

/** Compact API-health dot for the app header (slice-1 health surface). */
export function HealthIndicator() {
  const { status, data } = useHealth(10000)
  const healthy = status === 'ok' && data?.status === 'healthy'
  const label =
    status === 'loading' ? 'checking' : healthy ? 'API healthy' : 'API down'

  return (
    <span className="inline-flex items-center gap-2 text-xs text-slate-400">
      <span
        className={`h-2 w-2 rounded-full ${
          status === 'loading'
            ? 'bg-slate-500'
            : healthy
              ? 'bg-emerald-400'
              : 'bg-rose-500'
        }`}
      />
      {label}
    </span>
  )
}

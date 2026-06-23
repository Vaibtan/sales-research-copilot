import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { SessionHistory } from '../components/SessionHistory'
import { createSession } from '../lib/api'

export function CreateSessionPage() {
  const navigate = useNavigate()
  const [company, setCompany] = useState('')
  const [website, setWebsite] = useState('')
  const [objective, setObjective] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const { session_id } = await createSession({
        company: company.trim(),
        website: website.trim() || undefined,
        objective: objective.trim(),
      })
      navigate(`/sessions/${session_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start the run')
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto max-w-xl px-4 py-12">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-100">
        New research session
      </h1>
      <p className="mt-1 text-sm text-slate-400">
        Generate a grounded, cited sales briefing for a company.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-5">
        <Field label="Company" required>
          <input
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            required
            placeholder="Acme Inc."
            className={inputClass}
          />
        </Field>

        <Field label="Website" hint="optional">
          <input
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
            placeholder="https://acme.com"
            className={inputClass}
          />
        </Field>

        <Field label="Research objective" required>
          <textarea
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
            required
            rows={3}
            placeholder="Prepare for a renewal conversation with their VP of Sales."
            className={`${inputClass} resize-none`}
          />
        </Field>

        {error && (
          <p className="rounded-lg border border-rose-500/20 bg-rose-500/5 px-4 py-3 text-sm text-rose-300">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || !company.trim() || !objective.trim()}
          className="inline-flex w-full items-center justify-center rounded-lg bg-sky-500 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? 'Starting run…' : 'Start research'}
        </button>
      </form>

      <SessionHistory />
    </div>
  )
}

const inputClass =
  'w-full rounded-lg border border-white/10 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-600 focus:border-sky-500/50 focus:outline-none focus:ring-1 focus:ring-sky-500/40'

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string
  required?: boolean
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-center gap-2 text-sm font-medium text-slate-300">
        {label}
        {required && <span className="text-rose-400">*</span>}
        {hint && <span className="text-xs font-normal text-slate-500">{hint}</span>}
      </span>
      {children}
    </label>
  )
}

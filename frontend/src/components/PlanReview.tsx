import { useEffect, useState } from 'react'
import { approvePlan, getPlan, type PlanQuestionEdit } from '../lib/api'
import { SECTION_ORDER } from '../types'

type Row = { id?: string; text: string; section: string }

/**
 * HITL plan-approval (PRD §6.1, §13 P2). Shows the proposed research questions
 * after planning; the user can edit text, remove, or add questions, then approve
 * to resume the run with the (edited) plan.
 */
export function PlanReview({
  sessionId,
  onApproved,
}: {
  sessionId: string
  onApproved: () => void
}) {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getPlan(sessionId)
      .then((qs) => {
        if (!active) return
        setRows(qs.map((q) => ({ id: q.id, text: q.text, section: q.section })))
        setLoading(false)
      })
      .catch((e) => {
        if (!active) return
        setError(e instanceof Error ? e.message : 'Could not load the plan')
        setLoading(false)
      })
    return () => {
      active = false
    }
  }, [sessionId])

  function update(i: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  }
  function remove(i: number) {
    setRows((prev) => prev.filter((_, j) => j !== i))
  }
  function add() {
    setRows((prev) => [...prev, { text: '', section: SECTION_ORDER[0] }])
  }

  async function approve() {
    const questions: PlanQuestionEdit[] = rows
      .filter((r) => r.text.trim())
      .map((r) => ({ id: r.id, text: r.text.trim(), section: r.section }))
    if (questions.length === 0) {
      setError('Keep at least one question.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await approvePlan(sessionId, questions)
      onApproved()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not approve the plan')
      setSubmitting(false)
    }
  }

  if (loading) {
    return <p className="text-sm text-slate-400">Loading the proposed plan…</p>
  }

  return (
    <div className="rounded-2xl border border-sky-500/20 bg-sky-500/[0.03] p-6">
      <h3 className="text-sm font-semibold text-slate-100">Review the research plan</h3>
      <p className="mt-1 text-sm text-slate-400">
        These questions will drive the research. Edit, remove, or add any, then
        approve to continue.
      </p>

      <ul className="mt-5 space-y-3">
        {rows.map((row, i) => (
          <li key={row.id ?? `new-${i}`} className="flex items-start gap-2">
            <select
              value={row.section}
              onChange={(e) => update(i, { section: e.target.value })}
              className="rounded-lg border border-white/10 bg-slate-900/60 px-2 py-2 text-xs text-slate-300 focus:border-sky-500/50 focus:outline-none"
            >
              {SECTION_ORDER.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <textarea
              value={row.text}
              onChange={(e) => update(i, { text: e.target.value })}
              rows={2}
              className="flex-1 resize-none rounded-lg border border-white/10 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500/50 focus:outline-none focus:ring-1 focus:ring-sky-500/40"
            />
            <button
              type="button"
              onClick={() => remove(i)}
              title="Remove"
              className="rounded-lg border border-white/10 px-2 py-2 text-xs text-slate-400 hover:border-rose-500/40 hover:text-rose-300"
            >
              ✕
            </button>
          </li>
        ))}
      </ul>

      {error && <p className="mt-3 text-xs text-rose-300">{error}</p>}

      <div className="mt-5 flex items-center gap-3">
        <button
          type="button"
          onClick={approve}
          disabled={submitting}
          className="inline-flex items-center rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? 'Approving…' : 'Approve & run'}
        </button>
        <button
          type="button"
          onClick={add}
          className="text-xs font-medium text-sky-300 hover:text-sky-200"
        >
          + Add question
        </button>
      </div>
    </div>
  )
}

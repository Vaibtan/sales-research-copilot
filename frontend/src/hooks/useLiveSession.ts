import { useEffect, useRef, useState } from 'react'
import { getSession, sessionStreamUrl } from '../lib/api'
import { TERMINAL_STATUSES, type ProgressEvent, type SessionDetail } from '../types'

const isTerminal = (s: string) =>
  (TERMINAL_STATUSES as readonly string[]).includes(s)

export interface LiveSession {
  /** Header info now; the full report once the run reaches a terminal status. */
  detail: SessionDetail | null
  /** Latest live-progress snapshot from the SSE stream. */
  progress: ProgressEvent | null
  /** Best-known status: live stream wins, else the loaded detail. */
  status: string | null
  loading: boolean
  error: string | null
  /** Re-fetch detail and re-attach the live stream (e.g. after a resume). */
  reload: () => void
}

/**
 * Drive a session view from the live SSE stream.
 *
 * Loads detail once for the header (and the report, if the run is already done),
 * then subscribes to `/sessions/{id}/stream`. The server replays a snapshot on
 * (re)connect, so a refresh mid-run shows current progress rather than a blank
 * UI; EventSource auto-reconnects on transient drops. When the stream signals a
 * terminal status we re-fetch detail to render the finished report.
 */
export function useLiveSession(id: string): LiveSession {
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [progress, setProgress] = useState<ProgressEvent | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by reload() to re-run the effect (re-fetch + re-open the stream) after
  // a resume flips a terminal run back to in-flight.
  const [reloadKey, setReloadKey] = useState(0)
  const lastStep = useRef(Number.NEGATIVE_INFINITY)

  useEffect(() => {
    let active = true
    lastStep.current = Number.NEGATIVE_INFINITY
    setDetail(null)
    setProgress(null)
    setLoading(true)
    setError(null)

    getSession(id)
      .then((d) => {
        if (!active) return
        setDetail(d)
        setLoading(false)
      })
      .catch((e) => {
        if (!active) return
        setError(e instanceof Error ? e.message : 'Failed to load session')
        setLoading(false)
      })

    const es = new EventSource(sessionStreamUrl(id))

    es.addEventListener('progress', (ev) => {
      if (!active) return
      const p = JSON.parse((ev as MessageEvent).data) as ProgressEvent
      if (p.step < lastStep.current) return // ignore out-of-order/stale snapshots
      lastStep.current = p.step
      setProgress(p)
    })

    es.addEventListener('end', () => {
      es.close()
      if (!active) return
      // Terminal: fetch the finished report (and final status/degraded flag).
      getSession(id)
        .then((d) => active && setDetail(d))
        .catch(() => {})
    })

    // EventSource reconnects automatically; the endpoint replays the snapshot on
    // reconnect, so transient errors self-heal — nothing to do here.

    return () => {
      active = false
      es.close()
    }
  }, [id, reloadKey])

  const status = progress?.status ?? detail?.status ?? null
  const reload = () => {
    setProgress(null)
    setReloadKey((k) => k + 1)
  }
  return { detail, progress, status, loading, error, reload }
}

export { isTerminal }

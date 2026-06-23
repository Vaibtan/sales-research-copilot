import { useEffect, useState } from 'react'
import { listSessions } from '../lib/api'
import type { SessionSummary } from '../types'

type LoadState = 'loading' | 'ready' | 'error'

export interface SessionHistoryState {
  state: LoadState
  sessions: SessionSummary[]
  error: string | null
}

/** Fetch the session history list once (most-recent-first, from the API). */
export function useSessionHistory(): SessionHistoryState {
  const [value, setValue] = useState<SessionHistoryState>({
    state: 'loading',
    sessions: [],
    error: null,
  })

  useEffect(() => {
    let active = true
    const controller = new AbortController()

    listSessions(controller.signal)
      .then((sessions) => {
        if (active) setValue({ state: 'ready', sessions, error: null })
      })
      .catch((err) => {
        if (!active || controller.signal.aborted) return
        setValue({
          state: 'error',
          sessions: [],
          error: err instanceof Error ? err.message : 'Failed to load history',
        })
      })

    return () => {
      active = false
      controller.abort()
    }
  }, [])

  return value
}

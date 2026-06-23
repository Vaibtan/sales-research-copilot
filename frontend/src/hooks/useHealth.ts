import { useEffect, useState } from 'react'
import { fetchHealth, type HealthResponse } from '../lib/api'

export type HealthStatus = 'loading' | 'ok' | 'error'

export interface HealthState {
  status: HealthStatus
  data: HealthResponse | null
  error: string | null
}

/** Poll `/healthz` on an interval; reports loading / ok / unreachable. */
export function useHealth(intervalMs = 5000): HealthState {
  const [state, setState] = useState<HealthState>({
    status: 'loading',
    data: null,
    error: null,
  })

  useEffect(() => {
    let active = true
    const controller = new AbortController()

    async function poll() {
      try {
        const data = await fetchHealth(controller.signal)
        if (active) setState({ status: 'ok', data, error: null })
      } catch (err) {
        if (!active || controller.signal.aborted) return
        setState({
          status: 'error',
          data: null,
          error: err instanceof Error ? err.message : 'Backend unreachable',
        })
      }
    }

    void poll()
    const id = window.setInterval(() => void poll(), intervalMs)

    return () => {
      active = false
      controller.abort()
      window.clearInterval(id)
    }
  }, [intervalMs])

  return state
}

/** Thin client for the FastAPI backend. */

import type {
  ChatMessage,
  Citation,
  CreateSessionResponse,
  SessionDetail,
  SessionSummary,
} from '../types'

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export interface HealthResponse {
  status: 'healthy' | 'unhealthy'
  checks: {
    postgres: boolean
    redis: boolean
  }
}

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let message = `Request failed (${res.status})`
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') message = body.detail
    else if (Array.isArray(body?.detail)) message = 'Validation error'
  } catch {
    // non-JSON body — keep the default message
  }
  return new ApiError(message, res.status)
}

/**
 * `/healthz` returns 200 when healthy and 503 when a dependency is down — both
 * carry the same JSON body, so we treat either as a valid response.
 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE_URL}/healthz`, { signal })
  if (res.status !== 200 && res.status !== 503) {
    throw new ApiError(`Unexpected /healthz status ${res.status}`, res.status)
  }
  return (await res.json()) as HealthResponse
}

export interface CreateSessionBody {
  company: string
  website?: string
  objective: string
}

export async function createSession(
  body: CreateSessionBody,
): Promise<CreateSessionResponse> {
  const res = await fetch(`${API_BASE_URL}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await parseError(res)
  return (await res.json()) as CreateSessionResponse
}

export async function getSession(
  id: string,
  signal?: AbortSignal,
): Promise<SessionDetail> {
  const res = await fetch(`${API_BASE_URL}/sessions/${id}`, { signal })
  if (!res.ok) throw await parseError(res)
  return (await res.json()) as SessionDetail
}

export async function listSessions(
  signal?: AbortSignal,
): Promise<SessionSummary[]> {
  const res = await fetch(`${API_BASE_URL}/sessions`, { signal })
  if (!res.ok) throw await parseError(res)
  const body = (await res.json()) as { sessions: SessionSummary[] }
  return body.sessions
}

export interface ResumeResponse {
  session_id: string
  status: string
  /** False when the run was already finished (resume was a no-op). */
  resumed: boolean
}

/**
 * Resume a degraded/failed/interrupted run from its last checkpoint to attempt an
 * upgrade (PRD §9.1). A `done` run is a no-op (`resumed: false`).
 */
export async function resumeSession(id: string): Promise<ResumeResponse> {
  const res = await fetch(`${API_BASE_URL}/sessions/${id}/resume`, {
    method: 'POST',
  })
  if (!res.ok) throw await parseError(res)
  return (await res.json()) as ResumeResponse
}

/** URL of the live-progress SSE stream for a session (consumed via EventSource). */
export function sessionStreamUrl(id: string): string {
  return `${API_BASE_URL}/sessions/${id}/stream`
}

export async function listMessages(
  id: string,
  signal?: AbortSignal,
): Promise<ChatMessage[]> {
  const res = await fetch(`${API_BASE_URL}/sessions/${id}/messages`, { signal })
  if (!res.ok) throw await parseError(res)
  return ((await res.json()) as { messages: ChatMessage[] }).messages
}

export interface ChatDone {
  message_id: string
  citations: Citation[]
  report_version: number | null
  stale: boolean
}

export interface ChatHandlers {
  onToken: (text: string) => void
  onDone: (done: ChatDone) => void
}

/**
 * POST a chat message and consume the SSE answer stream. `EventSource` is GET-only,
 * so we read the POST response body as a stream and parse SSE frames by hand:
 * `token` frames carry answer deltas, the final `done` frame carries the persisted
 * message id + citation snapshots + stale flag.
 */
export async function sendChat(
  id: string,
  message: string,
  handlers: ChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/sessions/${id}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  })
  if (!res.ok || !res.body) throw await parseError(res)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      let event = 'message'
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data = line.slice(5).trim()
      }
      if (!data) continue
      if (event === 'token') handlers.onToken(JSON.parse(data).text as string)
      else if (event === 'done') handlers.onDone(JSON.parse(data) as ChatDone)
    }
  }
}

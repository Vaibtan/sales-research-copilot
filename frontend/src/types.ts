/** Shapes mirrored from the FastAPI response contracts (app.api.schemas). */

export interface CreateSessionResponse {
  session_id: string
  status: string
}

export interface SourceOut {
  id: string
  url: string
  title: string
  snippet: string
}

export interface ReportItemOut {
  text: string
  source_ids: string[]
}

export interface ReportSectionOut {
  section: string
  items: ReportItemOut[]
}

export interface ReportOut {
  version: number
  quality_score: number | null
  sections: ReportSectionOut[]
  unknowns: string[]
  sources: SourceOut[]
  created_at: string | null
}

export interface SessionDetail {
  id: string
  company: string
  website: string | null
  objective: string
  company_key: string
  status: string
  degraded: boolean
  created_at: string
  updated_at: string
  report: ReportOut | null
}

export interface SessionSummary {
  id: string
  company: string
  company_key: string
  website: string | null
  objective: string
  status: string
  degraded: boolean
  created_at: string
  updated_at: string
  report_version: number | null
}

export interface SessionHistory {
  sessions: SessionSummary[]
}

/** A live-progress snapshot streamed over SSE (mirrors domain.ProgressEvent). */
export interface ProgressSource {
  id: string
  title: string
  url: string
}

export interface ProgressEvent {
  session_id: string
  step: number
  status: string
  plan: string[]
  sources: ProgressSource[]
  facts_count: number
  draft_ready: boolean
  degraded: boolean
  terminal: boolean
}

export const TERMINAL_STATUSES = ['done', 'degraded', 'failed'] as const

/** Canonical render order of the seven content sections. */
export const SECTION_ORDER = [
  'Company Overview',
  'Products & Services',
  'Target Customers',
  'Business Signals',
  'Risks & Challenges',
  'Suggested Discovery Questions',
  'Suggested Outreach Strategy',
] as const

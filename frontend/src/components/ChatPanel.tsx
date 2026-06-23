import { useEffect, useRef, useState, type FormEvent } from 'react'
import { listMessages, sendChat } from '../lib/api'
import type { ChatMessage } from '../types'

/**
 * Grounded follow-up chat over the report + sources. Streams the answer
 * token-by-token, renders citation snapshots, and shows a staleness notice on
 * messages grounded on an older report version (PRD §8, §8.1).
 */
export function ChatPanel({ sessionId }: { sessionId: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    listMessages(sessionId)
      .then((m) => active && setMessages(m))
      .catch(() => {})
    return () => {
      active = false
    }
  }, [sessionId])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (!text || sending) return
    setInput('')
    setError(null)
    setSending(true)

    const user: ChatMessage = {
      id: `local-user-${Date.now()}`,
      role: 'user',
      content: text,
      citations: [],
      report_version: null,
      stale: false,
      created_at: new Date().toISOString(),
    }
    const draftId = `local-asst-${Date.now()}`
    const draft: ChatMessage = {
      id: draftId,
      role: 'assistant',
      content: '',
      citations: [],
      report_version: null,
      stale: false,
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, user, draft])

    try {
      await sendChat(sessionId, text, {
        onToken: (tok) =>
          setMessages((prev) =>
            prev.map((m) =>
              m.id === draftId ? { ...m, content: m.content + tok } : m,
            ),
          ),
        onDone: (done) =>
          setMessages((prev) =>
            prev.map((m) =>
              m.id === draftId
                ? {
                    ...m,
                    id: done.message_id,
                    citations: done.citations,
                    report_version: done.report_version,
                    stale: done.stale,
                  }
                : m,
            ),
          ),
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Chat failed')
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="mt-12 border-t border-white/10 pt-8">
      <h3 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-400">
        Ask a follow-up
      </h3>

      <div className="space-y-4">
        {messages.length === 0 && (
          <p className="text-sm text-slate-500">
            Ask about this company — answers are grounded in the report and cite
            their sources.
          </p>
        )}
        {messages.map((m) => (
          <Bubble key={m.id} message={m} />
        ))}
        <div ref={endRef} />
      </div>

      {error && (
        <p className="mt-3 rounded-lg border border-rose-500/20 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
          {error}
        </p>
      )}

      <form onSubmit={onSubmit} className="mt-5 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. What are their main products?"
          className="flex-1 rounded-lg border border-white/10 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-600 focus:border-sky-500/50 focus:outline-none focus:ring-1 focus:ring-sky-500/40"
        />
        <button
          type="submit"
          disabled={sending || !input.trim()}
          className="inline-flex items-center rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {sending ? '…' : 'Send'}
        </button>
      </form>
    </section>
  )
}

function Bubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  return (
    <div className={isUser ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
          isUser
            ? 'bg-sky-500/15 text-sky-50'
            : 'border border-white/10 bg-white/[0.03] text-slate-200'
        }`}
      >
        {message.stale && (
          <p className="mb-1.5 text-[0.7rem] text-amber-300/80">
            The report was updated — this earlier answer may reference changed
            sources.
          </p>
        )}
        <p className="whitespace-pre-wrap">{message.content || '…'}</p>
        {message.citations.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.citations.map((c, i) => (
              <a
                key={c.source_id}
                href={c.url}
                target="_blank"
                rel="noreferrer"
                title={c.title}
                className="rounded bg-sky-500/15 px-1.5 py-0.5 text-[0.65rem] font-medium text-sky-300 hover:bg-sky-500/30"
              >
                [{i + 1}] {c.title.slice(0, 32)}
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

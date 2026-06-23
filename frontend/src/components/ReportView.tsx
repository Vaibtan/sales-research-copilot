import type { ReportOut, SourceOut } from '../types'
import { SECTION_ORDER } from '../types'

function Citations({
  sourceIds,
  indexOf,
  sources,
}: {
  sourceIds: string[]
  indexOf: Map<string, number>
  sources: Map<string, SourceOut>
}) {
  if (sourceIds.length === 0) return null
  return (
    <sup className="ml-1 space-x-0.5">
      {sourceIds.map((id) => {
        const n = indexOf.get(id)
        const src = sources.get(id)
        if (!n || !src) return null
        return (
          <a
            key={id}
            href={src.url}
            target="_blank"
            rel="noreferrer"
            title={src.title}
            className="rounded bg-sky-500/15 px-1 text-[0.65rem] font-medium text-sky-300 hover:bg-sky-500/30"
          >
            {n}
          </a>
        )
      })}
    </sup>
  )
}

export function ReportView({ report }: { report: ReportOut }) {
  const indexOf = new Map(report.sources.map((s, i) => [s.id, i + 1]))
  const sourcesById = new Map(report.sources.map((s) => [s.id, s]))

  const order = (section: string) => {
    const i = SECTION_ORDER.indexOf(section as (typeof SECTION_ORDER)[number])
    return i === -1 ? SECTION_ORDER.length : i
  }
  const sections = [...report.sections].sort(
    (a, b) => order(a.section) - order(b.section),
  )

  return (
    <div className="space-y-8">
      {sections.map((section) => (
        <section key={section.section}>
          <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
            {section.section}
          </h3>
          <ul className="space-y-2">
            {section.items.map((item, i) => (
              <li
                key={i}
                className="rounded-lg border border-white/5 bg-white/[0.02] px-4 py-3 text-sm leading-relaxed text-slate-200"
              >
                {item.text}
                <Citations
                  sourceIds={item.source_ids}
                  indexOf={indexOf}
                  sources={sourcesById}
                />
              </li>
            ))}
          </ul>
        </section>
      ))}

      {/* Section 8: Unknowns (honest gaps, no fabrication). */}
      <section>
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-amber-400/80">
          Unknowns
        </h3>
        {report.unknowns.length === 0 ? (
          <p className="text-sm text-slate-500">None — every section is grounded.</p>
        ) : (
          <ul className="space-y-2">
            {report.unknowns.map((u, i) => (
              <li
                key={i}
                className="rounded-lg border border-amber-500/10 bg-amber-500/[0.04] px-4 py-3 text-sm text-amber-100/80"
              >
                {u}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Section 9: Sources. */}
      <section>
        <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Sources
        </h3>
        <ol className="space-y-2">
          {report.sources.map((s, i) => (
            <li key={s.id} className="flex gap-3 text-sm">
              <span className="font-mono text-xs text-sky-400">[{i + 1}]</span>
              <div className="min-w-0">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-slate-200 hover:text-sky-300"
                >
                  {s.title}
                </a>
                <p className="truncate font-mono text-xs text-slate-500">{s.url}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}

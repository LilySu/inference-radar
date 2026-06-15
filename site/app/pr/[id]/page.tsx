import { notFound } from 'next/navigation';
import Link from 'next/link';
import { getPR, getPRs } from '@/lib/data';

export function generateStaticParams() {
  return getPRs().map((p) => ({ id: String(p.id) }));
}

export default function PRPage({ params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  const pr = getPR(id);
  if (!pr) return notFound();

  return (
    <article className="space-y-8 max-w-3xl">
      <div>
        <div className="eyebrow">
          <Link
            href={`/repo/${encodeURIComponent(pr.repo)}/`}
            className="hover:underline"
          >
            {pr.repo}
          </Link>{' '}
          · #{pr.number}
        </div>
        <h1 className="font-display text-3xl text-white mt-1">{pr.title}</h1>
        <div className="text-xs text-accent-slate mt-2 flex flex-wrap gap-3">
          <span>state: {pr.state}</span>
          {pr.merged_at && <span>merged: {pr.merged_at}</span>}
          <a
            href={pr.html_url}
            target="_blank" rel="noreferrer"
            className="text-accent-sky hover:underline"
          >
            open on GitHub ↗
          </a>
        </div>
      </div>

      {pr.reasoning && (
        <section className="panel p-5 border-accent-purple/40">
          <div className="eyebrow mb-2">why classified · {pr.model}</div>
          <p className="text-white text-lg leading-relaxed">{pr.reasoning}</p>
        </section>
      )}

      <section className="panel p-5">
        <div className="eyebrow mb-2">categories</div>
        <div className="flex flex-wrap gap-2">
          {pr.primary_category ? (
            <span className="rounded bg-accent-purple/15 text-accent-purple px-3 py-1 text-sm">
              {pr.primary_category}
            </span>
          ) : (
            <span className="rounded bg-accent-amber/15 text-accent-amber px-3 py-1 text-sm">
              uncategorized
            </span>
          )}
          {pr.secondary_categories.map((s) => (
            <span key={s} className="rounded bg-accent-slate/20 text-accent-slate px-3 py-1 text-sm">
              {s}
            </span>
          ))}
          {pr.novel_category_proposed && (
            <span className="rounded bg-accent-amber/15 text-accent-amber px-3 py-1 text-sm">
              proposed: {pr.novel_category_proposed}
            </span>
          )}
        </div>
      </section>

      {pr.technical_summary && (
        <section className="panel p-5">
          <div className="eyebrow mb-2">technical summary</div>
          <p className="text-white whitespace-pre-wrap leading-relaxed">
            {pr.technical_summary}
          </p>
        </section>
      )}

      {pr.perf_numbers.length > 0 && (
        <section className="panel p-5">
          <div className="eyebrow mb-3">perf numbers</div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-accent-slate text-left">
                <th className="py-2">metric</th>
                <th className="py-2">baseline</th>
                <th className="py-2">new</th>
                <th className="py-2">Δ</th>
              </tr>
            </thead>
            <tbody>
              {pr.perf_numbers.map((p, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="py-2 text-white">{p.metric}</td>
                  <td className="py-2">{String(p.baseline ?? '—')}</td>
                  <td className="py-2 text-accent-green">{String(p.new ?? '—')}</td>
                  <td className="py-2 text-accent-amber">
                    {p.delta_pct != null ? `${p.delta_pct}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {pr.cross_references.length > 0 && (
        <section className="panel p-5">
          <div className="eyebrow mb-3">cross-references</div>
          <ul className="space-y-2">
            {pr.cross_references.map((x, i) => (
              <li key={i} className="text-sm">
                <a
                  href={`https://github.com/${x.repo}/pull/${x.number}`}
                  target="_blank" rel="noreferrer"
                  className="text-accent-sky hover:underline"
                >
                  {x.repo}#{x.number}
                </a>{' '}
                <span className="text-accent-slate">— {x.why}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {pr.labels.length > 0 && (
        <section>
          <div className="eyebrow mb-2">labels</div>
          <div className="flex flex-wrap gap-2">
            {pr.labels.map((l) => (
              <span key={l} className="rounded border border-border bg-panel px-2 py-1 text-xs text-accent-slate">
                {l}
              </span>
            ))}
          </div>
        </section>
      )}
    </article>
  );
}

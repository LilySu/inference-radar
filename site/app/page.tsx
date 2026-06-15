import Link from 'next/link';
import { getIndex } from '@/lib/data';

export default function Home() {
  const { latest_briefing, top_firsts, recent_prs, repos } = getIndex();

  return (
    <div className="space-y-12">
      <section>
        <div className="eyebrow mb-2">today</div>
        <h1 className="font-display text-5xl gradient-title">Inference Radar</h1>
        <p className="text-accent-slate mt-3 max-w-2xl">
          A daily filter on <span className="text-accent-sky">vllm</span>,{' '}
          <span className="text-accent-sky">sglang</span>,{' '}
          <span className="text-accent-sky">Megatron-LM</span>, and{' '}
          <span className="text-accent-sky">TensorRT-LLM</span>. Three pipelines:
          incremental ingest, LLM classify per repo taxonomy, and a daily news
          brief — plus ntfy phone pushes when a good first issue lands in one of
          three narrow buckets (b200, cutlass_cute, deepseek).
        </p>
      </section>

      {latest_briefing && (
        <section className="panel p-6">
          <div className="eyebrow mb-2">latest brief · {latest_briefing.briefing_date}</div>
          <h2 className="font-display text-2xl text-white mb-3">
            {latest_briefing.script?.title}
          </h2>
          <p className="text-accent-slate mb-4">{latest_briefing.script?.intro}</p>
          {latest_briefing.video_url ? (
            <a
              href={latest_briefing.video_url}
              className="text-accent-sky hover:underline"
              target="_blank" rel="noreferrer"
            >
              Watch on YouTube ↗
            </a>
          ) : (
            <span className="text-accent-slate text-sm">(local mp4 — not yet uploaded)</span>
          )}
          <Link
            href="/briefings/"
            className="ml-4 text-accent-purple hover:underline"
          >
            archive →
          </Link>
        </section>
      )}

      <section>
        <h2 className="font-display text-2xl text-white mb-4">Top picks · firsts</h2>
        {top_firsts.length === 0 ? (
          <p className="text-accent-slate">No open in-scope issues right now.</p>
        ) : (
          <ul className="space-y-3">
            {top_firsts.map((f) => (
              <li key={f.html_url} className="panel p-4 flex items-start gap-4">
                <span
                  className={
                    'shrink-0 rounded px-2 py-1 text-xs font-bold ' +
                    (f.difficulty <= 1
                      ? 'bg-accent-green/15 text-accent-green'
                      : f.difficulty === 2
                        ? 'bg-accent-amber/15 text-accent-amber'
                        : 'bg-accent-slate/15 text-accent-slate')
                  }
                >
                  D{f.difficulty}
                </span>
                <span className="shrink-0 rounded bg-accent-purple/15 text-accent-purple px-2 py-1 text-xs">
                  {f.bucket}
                </span>
                <div className="flex-1">
                  <a
                    href={f.html_url}
                    target="_blank" rel="noreferrer"
                    className="text-white hover:text-accent-sky"
                  >
                    {f.repo_short} #{f.number} — {f.title}
                  </a>
                  <p className="text-accent-slate text-sm mt-1">{f.why}</p>
                </div>
              </li>
            ))}
          </ul>
        )}
        <Link href="/firsts/" className="text-accent-purple hover:underline text-sm mt-4 inline-block">
          all open picks →
        </Link>
      </section>

      <section>
        <h2 className="font-display text-2xl text-white mb-4">Repos</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {repos.map((r) => (
            <Link
              key={r.slug}
              href={`/repo/${encodeURIComponent(r.slug)}/`}
              className="panel p-4 hover:border-accent-sky transition-colors"
            >
              <div className="text-white font-semibold">{r.name}</div>
              <div className="text-accent-slate text-xs mt-1">{r.slug}</div>
              <div className="text-xs mt-3 flex gap-4">
                <span className="text-accent-green">{r.classified_count} classified</span>
                <span className="text-accent-slate">{r.pr_count} prs</span>
              </div>
            </Link>
          ))}
        </div>
      </section>

      <section>
        <h2 className="font-display text-2xl text-white mb-4">Recent PRs</h2>
        {recent_prs.length === 0 ? (
          <p className="text-accent-slate">No classified PRs yet. Run `radar.ingest` + `radar.classify`.</p>
        ) : (
          <ul className="space-y-3">
            {recent_prs.slice(0, 10).map((p) => (
              <li key={p.id} className="panel p-4">
                <div className="flex items-baseline gap-3">
                  <span className="text-accent-slate text-xs">{p.repo_short}</span>
                  <Link
                    href={`/pr/${p.id}/`}
                    className="text-white hover:text-accent-sky"
                  >
                    #{p.number} {p.one_line_summary || p.title}
                  </Link>
                </div>
                {p.primary_category && (
                  <div className="text-xs mt-2">
                    <span className="text-accent-purple">{p.primary_category}</span>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

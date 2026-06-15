import { getFirsts } from '@/lib/data';

export default function FirstsPage() {
  const firsts = getFirsts();
  const byBucket: Record<string, typeof firsts> = {};
  for (const f of firsts) (byBucket[f.bucket] ||= []).push(f);
  const bucketOrder = ['b200', 'cutlass_cute', 'deepseek'];

  return (
    <div className="space-y-8">
      <div>
        <div className="eyebrow">open issues · three buckets</div>
        <h1 className="font-display text-4xl gradient-title">Firsts</h1>
        <p className="text-accent-slate text-sm mt-3 max-w-2xl">
          Open, unassigned issues across the four watched repos that survived
          the three-pass filter — keyword prefilter, LLM verification with
          verbatim evidence quotes, and a deterministic post-verifier that
          catches hallucinated quotes and bucket keyword violations. Ranked by
          difficulty (D1–D5).
        </p>
      </div>

      {firsts.length === 0 ? (
        <p className="text-accent-slate">No open in-scope issues right now.</p>
      ) : (
        bucketOrder.map((bucket) => {
          const list = byBucket[bucket];
          if (!list || list.length === 0) return null;
          return (
            <section key={bucket} className="panel p-5">
              <div className="flex items-baseline justify-between mb-4">
                <h2 className="font-display text-xl text-white">{bucket}</h2>
                <span className="text-xs text-accent-slate">{list.length} open</span>
              </div>
              <ul className="space-y-4">
                {list.map((f) => (
                  <li key={f.html_url} className="border-t border-border pt-3 first:border-t-0 first:pt-0">
                    <div className="flex items-baseline gap-3 mb-1">
                      <span
                        className={
                          'rounded px-2 py-1 text-xs font-bold ' +
                          (f.difficulty <= 1
                            ? 'bg-accent-green/15 text-accent-green'
                            : f.difficulty === 2
                              ? 'bg-accent-amber/15 text-accent-amber'
                              : 'bg-accent-slate/15 text-accent-slate')
                        }
                      >
                        D{f.difficulty}
                      </span>
                      <a
                        href={f.html_url}
                        target="_blank" rel="noreferrer"
                        className="text-white hover:text-accent-sky"
                      >
                        {f.repo_short} #{f.number} — {f.title}
                      </a>
                    </div>
                    <p className="text-accent-slate text-sm">{f.why}</p>
                    {f.evidence_quotes.length > 0 && (
                      <p className="text-xs text-accent-purple mt-1 italic">
                        “{f.evidence_quotes[0].slice(0, 200)}”
                      </p>
                    )}
                    {f.blackwell_intent_signal && (
                      <p className="text-xs text-accent-amber mt-1">
                        signal: {f.blackwell_intent_signal}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          );
        })
      )}
    </div>
  );
}

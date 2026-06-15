import Link from 'next/link';
import { notFound } from 'next/navigation';
import {
  getRepos,
  getCategoriesFor,
  getPRsByRepo,
  type PR,
} from '@/lib/data';

export function generateStaticParams() {
  return getRepos().map((r) => ({ slug: encodeURIComponent(r.slug) }));
}

function groupByCategory(prs: PR[]): Record<string, PR[]> {
  const out: Record<string, PR[]> = {};
  for (const p of prs) {
    const cat = p.primary_category || (p.bot_or_chore ? '_chore' : '_uncategorized');
    (out[cat] ||= []).push(p);
  }
  return out;
}

export default function RepoPage({ params }: { params: { slug: string } }) {
  const repoSlug = decodeURIComponent(params.slug);
  const repos = getRepos();
  const repo = repos.find((r) => r.slug === repoSlug);
  if (!repo) return notFound();

  const categories = getCategoriesFor(repoSlug);
  const prs = getPRsByRepo(repoSlug);
  const grouped = groupByCategory(prs);

  return (
    <div className="space-y-8">
      <div>
        <div className="eyebrow">{repoSlug}</div>
        <h1 className="font-display text-4xl gradient-title">{repo.name}</h1>
        <p className="text-accent-slate text-sm mt-2">
          {repo.classified_count} classified · {repo.pr_count} total PRs
        </p>
      </div>

      <section className="space-y-6">
        {categories.map((cat) => {
          const list = grouped[cat.slug] || [];
          return (
            <div key={cat.slug} className="panel p-5">
              <div className="flex items-baseline justify-between mb-3">
                <h2 className="font-display text-xl text-white">{cat.name}</h2>
                <span className="text-xs text-accent-slate">
                  {list.length} PR{list.length === 1 ? '' : 's'}
                </span>
              </div>
              {list.length === 0 ? (
                <p className="text-accent-slate text-sm">No classified PRs in this category yet.</p>
              ) : (
                <ul className="space-y-2">
                  {list.slice(0, 30).map((p) => (
                    <li key={p.id} className="flex items-baseline gap-2">
                      <Link
                        href={`/pr/${p.id}/`}
                        className="text-accent-sky hover:underline shrink-0"
                      >
                        #{p.number}
                      </Link>
                      <span className="text-white">
                        {p.one_line_summary || p.title}
                      </span>
                      {p.perf_numbers.length > 0 && (
                        <span className="text-accent-green text-xs ml-2">
                          ⚡ {p.perf_numbers.length} perf number{p.perf_numbers.length === 1 ? '' : 's'}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}

        {grouped._uncategorized && grouped._uncategorized.length > 0 && (
          <div className="panel p-5 border-accent-amber/30">
            <h2 className="font-display text-xl text-accent-amber mb-3">Uncategorized</h2>
            <p className="text-accent-slate text-xs mb-3">
              The classifier proposed a new category. Edit{' '}
              <code className="text-accent-purple">seed/categories_seed.yml</code> to accept.
            </p>
            <ul className="space-y-2">
              {grouped._uncategorized.slice(0, 20).map((p) => (
                <li key={p.id}>
                  <Link href={`/pr/${p.id}/`} className="text-accent-sky hover:underline">
                    #{p.number}
                  </Link>{' '}
                  <span className="text-white">{p.one_line_summary || p.title}</span>{' '}
                  {p.novel_category_proposed && (
                    <span className="text-accent-amber text-xs">
                      proposed: {p.novel_category_proposed}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}

// Build-time data loaders. The Python `radar.export` step dumps SQLite slices
// into ./data/*.json. Each page reads these synchronously during `next build`.
import { readFileSync, existsSync } from 'fs';
import path from 'path';

const DATA_DIR = path.join(process.cwd(), 'data');

function readJson<T>(name: string, fallback: T): T {
  const p = path.join(DATA_DIR, name);
  if (!existsSync(p)) return fallback;
  return JSON.parse(readFileSync(p, 'utf8')) as T;
}

export type Repo = {
  slug: string;
  name: string;
  pr_count: number;
  classified_count: number;
};

export type Category = { slug: string; name: string };
export type CategoryDoc = { repo: string; categories: Category[] };

export type PerfNumber = {
  metric: string;
  baseline?: string | number | null;
  new?: string | number | null;
  delta_pct?: number | null;
};

export type CrossRef = { repo: string; number: number; why: string };

export type PR = {
  id: number;
  repo: string;
  repo_short: string;
  number: number;
  title: string;
  html_url: string;
  state: string;
  merged_at: string | null;
  created_at: string;
  updated_at: string;
  labels: string[];
  primary_category: string | null;
  secondary_categories: string[];
  novel_category_proposed: string | null;
  one_line_summary: string | null;
  technical_summary: string | null;
  perf_numbers: PerfNumber[];
  cross_references: CrossRef[];
  reasoning: string | null;
  bot_or_chore: boolean;
  model: string | null;
  classified_at: string | null;
};

export type First = {
  repo: string;
  repo_short: string;
  number: number;
  title: string;
  html_url: string;
  created_at: string;
  updated_at: string;
  bucket: string;
  difficulty: number;
  why: string;
  evidence_quotes: string[];
  blackwell_intent_signal: string | null;
  evaluated_at: string;
  model: string;
};

export type Briefing = {
  briefing_date: string;
  repo_scope: string[];
  script: {
    title?: string;
    intro?: string;
    slides?: Array<{
      heading: string;
      subhead: string;
      bullets: string[];
      narration: string;
      duration_s?: number;
    }>;
    outro?: string;
  };
  video_path: string | null;
  video_url: string | null;
  duration_s: number | null;
  built_at: string;
};

export function getRepos(): Repo[] {
  return readJson<Repo[]>('repos.json', []);
}

export function getCategories(): CategoryDoc[] {
  return readJson<CategoryDoc[]>('categories.json', []);
}

export function getCategoriesFor(repoSlug: string): Category[] {
  const all = getCategories();
  return all.find((d) => d.repo === repoSlug)?.categories ?? [];
}

export function getPRs(): PR[] {
  return readJson<PR[]>('prs.json', []);
}

export function getPR(id: number): PR | undefined {
  return getPRs().find((p) => p.id === id);
}

export function getPRsByRepo(repoSlug: string): PR[] {
  return getPRs().filter((p) => p.repo === repoSlug);
}

export function getFirsts(): First[] {
  return readJson<First[]>('firsts.json', []);
}

export function getBriefings(): Briefing[] {
  return readJson<Briefing[]>('briefings.json', []);
}

export function getIndex(): {
  latest_briefing: Briefing | null;
  top_firsts: First[];
  recent_prs: PR[];
  repos: Repo[];
} {
  return readJson('index.json', {
    latest_briefing: null,
    top_firsts: [],
    recent_prs: [],
    repos: [],
  });
}

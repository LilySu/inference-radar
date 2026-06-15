# Inference Radar

A daily ingest + classifier + brief for inference-stack repos. Watches
`vllm-project/vllm`, `sgl-project/sglang`, `NVIDIA/Megatron-LM`, and
`NVIDIA/TensorRT-LLM`, and produces:

1. **ntfy phone alerts** when an open issue lands in one of three narrow buckets:
   - **`b200`** — Blackwell-targeted kernel work
   - **`cutlass_cute`** — CUTLASS or CuTe specifically
   - **`deepseek`** — DeepSeek 3.2/V4 or its primitives (MLA, MTP, fine-grained MoE w/ shared experts)
2. **A classified PR feed** — every recent PR gets one of 11 per-repo categories
   plus a technical summary, perf numbers, cross-references, and a one-sentence
   reasoning string shown prominently on the site.
3. **A daily YouTube briefing** — terse, perf-numbers-forward, kernel-name-bearing.
   Marp slides, Piper TTS narration, ffmpeg-assembled mp4, uploaded as unlisted.
4. **A Vercel-hosted static site** showing PRs by repo and category, the firsts
   pick list, and the brief archive.

**Cost: $0/month.** Groq free tier for the LLM, Piper for TTS (local), Marp for
slides (local), GitHub Actions free minutes, ntfy.sh, Vercel hobby tier.

Quality model: false positives are worse than false negatives. The firsts
filter runs three passes — a deterministic keyword prefilter, an LLM
verification with mandatory verbatim evidence quotes, and a deterministic
post-verifier that catches hallucinated quotes and bucket keyword violations.
Two ntfy tracks (`confirmed` for label-bearing issues, `speculative` for
unlabeled ones) so you can mute one and keep the other clean.

## Setup

### 1. Clone & install

```bash
git clone https://github.com/LilySu/inference-radar.git
cd inference-radar
uv sync
```

Python 3.11+ via uv. Optional extras:
- `uv sync --extra anthropic` for the Anthropic backend
- `uv sync --extra youtube` for YouTube upload from the brief pipeline

### 2. Get a GitHub PAT

Fine-grained, read-only on the four watched repos (or classic with `public_repo`).
github.com → settings → tokens. Lifts the unauth rate limit (5k/hr authed).

### 3. Get a Groq API key (free tier)

console.groq.com → API keys. Free tier on `llama-3.3-70b-versatile` is 30 RPM,
plenty for this. Store as `GROQ_API_KEY`.

If you'd rather use Claude:

- **Anthropic API**: set `ANTHROPIC_API_KEY` and `RADAR_LLM=anthropic`. Optional
  `ANTHROPIC_MODEL` override (default `claude-sonnet-4-6`).
- **Local Claude Code**: set `RADAR_LLM=claude_code`. No key needed; we shell
  out to `claude -p ... --output-format json`. Useful when running interactively
  on your laptop and you want higher-quality re-evaluation.

### 4. Pick ntfy topics

Pick two unguessable strings. ntfy is public — anyone who knows the topic can
read your notifications.

```bash
NTFY_TOPIC=lily-inference-firsts-CHANGE-ME
NTFY_TOPIC_SPECULATIVE=lily-inference-firsts-spec-CHANGE-ME
```

Install the ntfy mobile app and subscribe to both. The `speculative` track gets
a `warning` tag so you can visually distinguish it.

### 5. Set env

Copy `.env.example` to `.env` and fill in. Or export in your shell:

```bash
export GH_TOKEN=ghp_...
export GROQ_API_KEY=gsk_...
export NTFY_TOPIC=...
export NTFY_TOPIC_SPECULATIVE=...
```

### 6. (Optional) System deps for the brief pipeline

Only needed for the daily YouTube brief; the firsts filter has no system
dependencies beyond Python.

```bash
sudo apt-get install ffmpeg
npm install -g @marp-team/marp-cli
pip install piper-tts
# Download a voice model
mkdir -p ~/.piper-voices
curl -sSL -o ~/.piper-voices/en_US-lessac-medium.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
curl -sSL -o ~/.piper-voices/en_US-lessac-medium.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
export PIPER_MODEL=~/.piper-voices/en_US-lessac-medium.onnx
```

### 7. (Optional) YouTube upload

Only needed to publish the daily brief. console.cloud.google.com → OAuth client
(Desktop app). Mint a refresh token via any standard installed-app OAuth helper
and set:

```
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
```

**Gotcha**: if the OAuth project stays in *Testing* mode, refresh tokens expire
in 7 days. Submit for verification (free) to make them durable, or set up a
weekly token-refresh workflow. Full step-by-step in
[docs/youtube_oauth.md](docs/youtube_oauth.md).

## Usage

```bash
# List all entry points
uv run python -m radar

# End-to-end (mirrors what the daily GH workflow does)
uv run python -m radar.run_all                  # full daily
uv run python -m radar.run_all --hourly         # ingest + firsts only
uv run python -m radar.run_all --no-upload      # build brief mp4 but don't post

# Pull issues + PRs (incremental, cursor-driven)
uv run python -m radar.ingest

# Run the three-pass firsts filter and push to ntfy
uv run python -m radar.firsts
uv run python -m radar.firsts --dry-run

# Classify all newly-ingested PRs
uv run python -m radar.classify --max 200
uv run python -m radar.classify --repo vllm-project/vllm --dry-run

# Build and (optionally) upload the daily brief
uv run python -m radar.brief                    # full pipeline → mp4 → YouTube
uv run python -m radar.brief --no-upload        # local mp4 only
uv run python -m radar.brief --script-only      # write script.json, exit
uv run python -m radar.brief --date 2026-05-12  # back-date a brief

# Dump SQLite slices to site/data/*.json for the Vercel build
uv run python -m radar.export

# Mark notifications correct/incorrect to build a golden set retroactively
uv run python -m radar.label 42 good
uv run python -m radar.label 17 bad --reason "off-bucket"
uv run python -m radar.label list --track speculative --undismissed
```

## Daily flow

```
                        GitHub Actions cron
       ┌──────────────────────────────────────────────────────────┐
       │   "47 * * * *"        — ingest + firsts (hourly)         │
       │   "13 12 * * *"       — full daily run                   │
       │   workflow_dispatch   — run_brief=true forces full       │
       └────────────────────────────┬─────────────────────────────┘
                                    │
       ┌────────────────────────────┴──────────────────────────────┐
       │  ingest.py → SQLite (issues, prs, cursors)                │
       │  firsts.py → 3-pass filter → ntfy two tracks              │
       │                                                            │
       │  daily only:                                               │
       │  classify.py  → pr_classifications + uncategorized.json   │
       │  brief.py     → script.json → marp PNGs → piper wavs →    │
       │                 ffmpeg mp4 → YouTube (unlisted) → briefings│
       │  export.py    → site/data/*.json                          │
       │                                                            │
       │  commit data/ + site/data/ → push                          │
       │  Vercel auto-deploys site from site/                       │
       └────────────────────────────────────────────────────────────┘
```

## How firsts works

```
┌──────────────────────────────────────────────────────────────────────┐
│ pass 1 — prefilter (regex word-boundary on title+body)               │
│          b200, blackwell, sm_*, cutlass, cute, deepseek, mla, mtp,   │
│          shared expert, fp8, fp4, hopper, ...                        │
│          drops ~half of open-unassigned issues                       │
│                                                                      │
│ pass 2 — LLM verification (groq | anthropic | claude_code)           │
│          batch=5, system prompt v1, JSON schema                      │
│          required: evidence_quotes (verbatim), scope_bucket,         │
│                    blackwell_intent_signal (when no explicit kw),    │
│                    difficulty (1–5), why                             │
│                                                                      │
│ pass 3 — verifier (deterministic, in-process)                        │
│          every evidence_quote must be a substring of the issue;      │
│          bucket-specific keyword presence re-checked.                │
│          catches confident hallucination — the #1 failure mode.     │
└────────────────────┬─────────────────────────────────────────────────┘
                     ▼
       confirmed:    in_scope ∧ has 'good first issue' label ∧ D≤2
       speculative:  in_scope ∧ no label                     ∧ D≤2
```

## How classify works

Single pass on Groq Llama 3.3 70B (or whichever backend the `_llm` router
points at). Each PR gets:

- `primary_category` from the per-repo taxonomy in `seed/categories_seed.yml`
  (11 categories per repo).
- `secondary_categories` (≤2) for cross-cutting PRs.
- `novel_category_proposed` when nothing fits — appended to
  `data/uncategorized.json` for human-PR review. Edit
  `seed/categories_seed.yml` to accept.
- `technical_summary`, `perf_numbers[]`, `cross_references[]`, `reasoning`,
  `one_line_summary`, `bot_or_chore`.

Bot/chore PRs (dependabot, version bumps, single-line typo fixes) are still
classified but flagged so the brief script collapses them.

## How the brief works

```
classified PRs since yesterday  ──┐
firsts picks (in-scope, D≤3)     ──┴──► LLM script generator (groq)
                                          │
                                          ▼   script.json
                                          │
                                     marp ─┴─► slides/*.png
                                          │
                                     piper ─┴─► audio/*.wav
                                          │
                                     ffmpeg ─┴─► brief.mp4
                                          │
                              YouTube Data API v3 ─► unlisted upload
```

Style: terse, perf-numbers-forward, kernel-name-bearing. Tone reference is
`~/wsl_git/inference-ideas/*-pr-research.md`.

## Schema

`data/radar.db` (SQLite, WAL, foreign keys on). Tables:

- `repos` — slug + display name.
- `issues`, `prs` — full payload from GH plus `labels_json`, `assignee`,
  `merged_at`.
- `cursors` — `(repo_id, kind)` → last `updated_at` we successfully stored.
- `issue_evaluations` — one row per (issue, prompt_version, model). Never
  overwritten. Holds `in_scope`, `scope_bucket`, `evidence_quotes_json`,
  `blackwell_intent_signal`, `difficulty`, `why`.
- `notifications` — one row per ntfy push, `track ∈ {confirmed, speculative}`.
  Has nullable `dismissed_correct` / `dismissed_reason` for retroactive labels.
- `pr_classifications` — one row per (pr, prompt_version, model). Holds
  `primary_category`, `secondary_categories_json`, `novel_category_proposed`,
  `technical_summary`, `perf_numbers_json`, `cross_references_json`,
  `reasoning`, `one_line_summary`, `bot_or_chore`.
- `briefings` — one row per `briefing_date`. Holds `script_json`,
  `video_path`, `video_url`, `duration_s`.
- `issues_fts`, `prs_fts` — FTS5 mirrors.

Migrations: `radar/migrations/000{1,2}_*.sql`.

## Site

Next.js 14 App Router, `output: 'export'`. Reads `site/data/*.json` at build
time (no runtime DB). Pages:

- `/` — landing: latest brief + top firsts + recent PRs + repo grid
- `/repo/[slug]` — category-grouped PRs for one repo
- `/pr/[id]` — single PR; `reasoning` prominent
- `/firsts` — open in-scope issues by bucket
- `/briefings` — daily video archive

Connect `site/` as the Vercel project root; Vercel auto-deploys on push.

## Don'ts

- Don't infer firsts scope from labels alone — labels are noisy.
  Evidence quotes are the source of truth.
- Don't expand the firsts prefilter keyword list without bumping
  `PROMPT_VERSION` in `radar/firsts.py`.
- Don't notify the same issue twice on the same track.
- Don't paraphrase perf numbers in the brief — quote them verbatim.
- The site is a static export; never add runtime-DB-fetching pages.

## Tests

```bash
uv run pytest -q
```

Deterministic passes (prefilter, verifier, classifier normalization) are unit
tested. LLM-side behavior is exercised manually with `--dry-run`.

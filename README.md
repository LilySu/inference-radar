# Inference Radar

A narrow good-first-issue filter for inference-stack repos. Watches
`vllm-project/vllm`, `sgl-project/sglang`, `NVIDIA/Megatron-LM`, and
`NVIDIA/TensorRT-LLM` and pushes ntfy.sh notifications to your phone when an
issue lands in one of three buckets:

- **`b200`** — Blackwell-targeted kernel work
- **`cutlass_cute`** — CUTLASS or CuTe specifically
- **`deepseek`** — DeepSeek 3.2/V4 or its architectural primitives (MLA, MTP, fine-grained MoE with shared experts)

Quality model: false positives are worse than false negatives. Every wrong
notification costs trust. The filter runs three passes — a deterministic
keyword prefilter, an LLM verification with mandatory verbatim evidence quotes,
and a deterministic post-verifier that catches hallucinated quotes and bucket
keyword violations. Two ntfy tracks (`confirmed` for label-bearing issues,
`speculative` for unlabeled ones) so you can mute one and keep the other clean.

## Setup

### 1. Clone & install

```bash
git clone https://github.com/LilySu/inference-radar.git
cd inference-radar
uv sync
```

Python 3.11+ via uv. Optional: `uv sync --extra anthropic` if you'll use the
Anthropic backend.

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

## Usage

```bash
# Pull issues + PRs (incremental, cursor-driven)
uv run python -m radar.ingest

# Run the three-pass filter and push to ntfy
uv run python -m radar.firsts

# Run everything except the ntfy POST and DB notification write
uv run python -m radar.firsts --dry-run

# Re-evaluate already-stored issues (e.g. after bumping prompt_version
# or when you want claude_code's higher quality on top of groq's earlier pass)
uv run python -m radar.firsts --reevaluate --since 2026-04-01

# Mark a notification good or bad — builds the golden set retroactively
uv run python -m radar.label 42 good
uv run python -m radar.label 17 bad --reason "off-bucket: actually generic Hopper"
uv run python -m radar.label list --track speculative --undismissed
```

## Scheduling

Run hourly via cron, GitHub Actions, or systemd timer. Two patterns work:

- **GitHub Actions**: cron the workflow with secrets for GH_TOKEN, GROQ_API_KEY,
  NTFY_TOPIC, NTFY_TOPIC_SPECULATIVE. Cheapest. Workflow scaffold not yet
  included — add `.github/workflows/radar.yml` when you're ready.
- **Local cron**: simplest. Drop `cd ~/wsl_git/inference-radar && uv run
  python -m radar.ingest && uv run python -m radar.firsts` into crontab.

## How it works

```
┌──────────────────────────────────────────────────────────────────────┐
│ ingest.py                                                            │
│   gh /repos/{slug}/issues  +  gh /repos/{slug}/pulls                 │
│     → SQLite (data/radar.db)  with per-(repo,kind) cursors           │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────────────┐
│ firsts.py                                                            │
│                                                                      │
│  pass 1 — prefilter (regex word-boundary on title+body)              │
│           keywords: b200, blackwell, sm_*, cutlass, cute, deepseek,  │
│                     mla, mtp, shared expert, fp8, fp4, hopper, ...   │
│           → drops ~half of open-unassigned issues                    │
│                                                                      │
│  pass 2 — LLM verification (groq | anthropic | claude_code)          │
│           batch=5, system prompt v1, JSON tool-use schema            │
│           required: evidence_quotes (verbatim), scope_bucket,        │
│                     blackwell_intent_signal (when no explicit kw),   │
│                     difficulty (1–5), why                            │
│                                                                      │
│  pass 3 — verifier (deterministic, in-process)                       │
│           every evidence_quote must be a substring of the issue;     │
│           bucket-specific keyword presence re-checked.               │
│           catches confident hallucination — the #1 failure mode.     │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────────────┐
│ ntfy two-track                                                       │
│                                                                      │
│  confirmed:    in_scope ∧ has 'good first issue' label ∧ D≤2         │
│                → POST $NTFY_TOPIC   (tags: rocket|dart)              │
│                                                                      │
│  speculative:  in_scope ∧ no label                ∧ D≤2              │
│                → POST $NTFY_TOPIC_SPECULATIVE     (+warning tag)     │
└──────────────────────────────────────────────────────────────────────┘
```

Every evaluation row stores `model` (e.g. `groq:llama-3.3-70b-versatile`) and
`prompt_version` (`v1`). When you change the system prompt or keyword list, bump
`PROMPT_VERSION` in `radar/firsts.py` so re-evaluations are diffable.

## Schema

`data/radar.db`. Tables:

- `repos` — slug + display name
- `issues`, `prs` — full payload from GH plus `labels_json`, `assignee`
- `cursors` — `(repo_id, kind)` → last `updated_at` we successfully stored
- `issue_evaluations` — one row per (issue, prompt_version, model) — never
  overwritten. `in_scope`, `scope_bucket`, `evidence_quotes_json`,
  `blackwell_intent_signal`, `difficulty`, `why`.
- `notifications` — one row per push, `track ∈ {confirmed, speculative}`. Has
  nullable `dismissed_correct` / `dismissed_reason` for retroactive labeling.
- `issues_fts`, `prs_fts` — FTS5 mirrors for future use.

WAL, busy_timeout=5000, foreign keys on. `radar/db.py` is the storage helper.

## Don'ts

- Don't infer scope from labels alone — labels are noisy across these repos.
  Evidence quotes are the source of truth.
- Don't expand the prefilter keyword list without bumping `PROMPT_VERSION`.
- PR notifications aren't sent. PRs are ingested for future use (a daily
  briefing pipeline isn't part of this phase).
- Don't notify the same issue twice on the same track.

## Tests

```bash
uv run pytest -q
```

Verifier and prefilter are deterministically tested. LLM-side behavior is
exercised manually with `--dry-run`.

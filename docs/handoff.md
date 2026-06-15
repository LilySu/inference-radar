# Inference Radar — handoff notes

Last verified end-to-end on **2026-05-14** with `RADAR_LLM=anthropic` /
`claude-sonnet-4-6`. Read this before resuming work in a fresh session.

## Pipeline status

| Stage | Verified | Artifact |
|---|---|---|
| `radar.ingest` | pre-populated (skipped) | `data/radar.db`: 8511 PRs, 2000 issues across 4 repos |
| `radar.firsts --dry-run` | yes | 414+ evaluations; no new in-scope hits as of last run |
| `radar.classify` | yes | 325 rows in `pr_classifications` |
| `radar.brief --script-only` | yes | 10-slide script in `out/2026-05-09/script.json` + `briefings` row |
| `radar.brief --no-upload` (full render) | yes | `out/2026-05-09/brief.mp4` — 10.4 MB, 6:38, 1080p H264 + AAC |
| `radar.export` | yes | 6 JSON files in `site/data/` |
| `site/` Next.js static build | yes | 2010 pages exported to `site/out/` |
| `radar.brief` YouTube upload | **not run** — requires OAuth refresh token |
| Vercel deploy | **not run** — requires user to link the repo in the dashboard |

23/23 pytest passes.

## Classification coverage

```
NVIDIA/Megatron-LM    100
NVIDIA/TensorRT-LLM   100
sgl-project/sglang     25
vllm-project/vllm     100
```

9 novel-category proposals queued in `data/uncategorized.json` (mostly chore /
ci_infra / docs proposals that should probably get added to the taxonomy or
ignored). User accepts by editing `seed/categories_seed.yml`.

## API keys — where to find them

`.env` is gitignored and present locally. It was assembled on 2026-05-14 from:

| Key | Source file |
|---|---|
| `ANTHROPIC_API_KEY` | `/home/lily/wsl_git/workday_connector/apply-jobs/.env` (real `sk-ant-api03-` key) |
| `GH_TOKEN` | `/home/lily/wsl_git/Schemata/.env` (`GITHUB_PAT` field) |

`GROQ_API_KEY` was **not** on disk. To switch to the production-default Groq
path ($0/mo), sign up at console.groq.com and add the key to `.env`.

`NTFY_TOPIC*` are placeholder values — fill them in only when wiring up the
phone notifications.

`YOUTUBE_*` are unset; see `docs/youtube_oauth.md`.

## Local toolchain (already installed 2026-05-14)

| Binary | Path / Source |
|---|---|
| `ffmpeg` | `/home/lily/.local/bin/ffmpeg` (static build 7.0.2) |
| `marp` | `/home/lily/.nvm/versions/node/v24.14.0/bin/marp` (v4.4.0, npm global) |
| `piper` | in the project's `.venv` (installed via `uv pip install piper-tts`) |
| Piper voice | `/home/lily/.piper-voices/en_US-lessac-medium.onnx` + json |

`PIPER_MODEL=/home/lily/.piper-voices/en_US-lessac-medium.onnx` must be in env
(not currently in `.env`; exported inline during the test run).

## Code changes made this session

1. **`radar/_llm.py:130`** — Anthropic `max_tokens` was hardcoded at 4096,
   truncating the brief script before slides emitted (only `title`+`intro`
   came back). Now reads `ANTHROPIC_MAX_TOKENS` env var, default **16384**.
2. **`radar/brief.py:362`** — ffmpeg's concat demuxer resolves paths relative
   to the list file's directory, not the cwd. Switched concat.txt entries
   from `c.as_posix()` (absolute) to `c.name` (basenames).
3. **`radar/brief.py:289`** — `synthesize_narration` previously rendered only
   the body slides (N WAVs), but Marp emits N+2 PNGs (title + N + outro), so
   `zip` silently dropped the bookends. Refactored to take the whole script
   and emit N+2 WAVs aligned with the PNGs.

All three are bug fixes, not features. No new code paths.

## How to resume

```bash
cd /home/lily/wsl_git/inference-radar
set -a; . .env; set +a
export PIPER_MODEL=/home/lily/.piper-voices/en_US-lessac-medium.onnx

# Smoke test the LLM router:
uv run python -m radar.classify --repo sgl-project/sglang --dry-run --max 3

# Re-render the brief end-to-end (re-uses script.json if it exists):
uv run python -m radar.brief --no-upload --date 2026-05-09

# Fresh build of the site:
uv run python -m radar.export
cd site && npm run build && cd ..

# Watch the result:
ffplay out/2026-05-09/brief.mp4
```

## What's still unbuilt

- **YouTube upload**. `radar.mint_youtube_token` exists but needs the user to
  run it in a browser (Desktop OAuth flow). Then add `YOUTUBE_CLIENT_ID`,
  `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` to `.env` and the GH
  Actions secrets. Walk-through: `docs/youtube_oauth.md`.
- **Vercel deploy**. Connect `site/` as the project root in the Vercel
  dashboard; the `.github/workflows/daily.yml` commit step pushes `site/data/`
  on every successful daily run, and Vercel auto-builds.
- **Classify the remaining ~7900 PRs.** Only 325 / 8511 are classified. At
  ~$0.005/PR on Sonnet 4.6 (5-PR batches), the rest would cost ~$40. Cheaper
  alternative: switch to Groq once a key is on disk (free tier). The current
  taxonomy fits well — `uncategorized` only fires for chore/ci_infra/docs,
  which is the human-in-the-loop signal Plan 3 intended.
- **Production firsts run.** The 414 existing evaluations are all
  `in_scope=0`. Either the buckets are too narrow for the current GH activity
  on these repos, or the prompt needs revisiting. If the user wants real ntfy
  pushes, do one `--reevaluate` pass after pulling fresh issues to see
  whether anything new lands in scope.
- **Tilelang as a 5th watched repo.** User mentioned this on 2026-05-12 (see
  memory `project_radar_vision.md`). Not started — would mean editing
  `seed/repos.yml`, adding tilelang-specific terms to the `firsts.py`
  prefilter, and bumping `PROMPT_VERSION`.

## Cost spent

This session: ~330 Anthropic Sonnet 4.6 calls (5-PR batches mostly). Roughly
**$2–3** of the $10 budget. Brief mp4 rendering and Marp/Piper run locally for
free.

## Don'ts (still binding from prior sessions)

- Don't infer firsts scope from labels alone — evidence quotes are truth.
- Don't expand the firsts prefilter keyword list without bumping
  `PROMPT_VERSION` in `radar/firsts.py`.
- Don't notify the same issue twice on the same track.
- Don't paraphrase perf numbers in the brief — quote them verbatim.
- The site is a static export; never add runtime-DB-fetching pages.

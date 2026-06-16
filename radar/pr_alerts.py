"""Per-repo PR alert feeds for vllm / sglang / TRT-LLM.

Six ntfy feeds, two per repo:

  bw  — open + unassigned PRs (past 3 days) that mention BOTH a Blackwell-family
        term AND a CUTLASS/CuTeDSL-family term. An LLM then verifies the PR is
        genuinely about that work (false positives are worse than negatives).

  all — every open + unassigned PR (past 3 days), no LLM filter.

Cadence: at most 3 ntfy pushes per feed per run. With the hourly cron that
caps each feed at 3/hour. Backlog drains naturally — oldest-unsent first, so
the user sees the front of the queue before fresh arrivals.

Topics are read from env vars listed in FEEDS below.

Run:
  uv run python -m radar.pr_alerts
  uv run python -m radar.pr_alerts --dry-run
  uv run python -m radar.pr_alerts --max 1
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from radar._llm import complete_json, model_id, selected_provider
from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

PROMPT_VERSION = "pr_v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "pr_alerts_v1.md"
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")

WINDOW_DAYS = 3
DEFAULT_MAX_PER_FEED = 3
BATCH_SIZE = 5

FEEDS = [
    {
        "slug": "vllm-project/vllm", "short": "vLLM",
        "topics": {"bw": "NTFY_TOPIC_VLLM_BW", "all": "NTFY_TOPIC_VLLM_ALL"},
    },
    {
        "slug": "sgl-project/sglang", "short": "SGLang",
        "topics": {"bw": "NTFY_TOPIC_SGL_BW", "all": "NTFY_TOPIC_SGL_ALL"},
    },
    {
        "slug": "NVIDIA/TensorRT-LLM", "short": "TRT-LLM",
        "topics": {"bw": "NTFY_TOPIC_TRT_BW", "all": "NTFY_TOPIC_TRT_ALL"},
    },
]

BW_BLACKWELL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:b200|blackwell|gb200|sm_?100|sm_?120|tmem|umma|tcgen05)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
BW_CUTLASS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:cutlass|cute|cutedsl)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass
class PRRow:
    id: int
    repo_id: int
    number: int
    title: str
    body: str
    labels: list[str]
    html_url: str
    created_at: str

    @property
    def text(self) -> str:
        return f"{self.title or ''}\n{self.body or ''}"


def to_pr(row) -> PRRow:
    return PRRow(
        id=int(row["id"]), repo_id=int(row["repo_id"]),
        number=int(row["number"]),
        title=row["title"] or "", body=row["body"] or "",
        labels=loads(row["labels_json"]) or [],
        html_url=row["html_url"] or "",
        created_at=row["created_at"] or "",
    )


def bw_prefilter(pr: PRRow) -> bool:
    return bool(BW_BLACKWELL_RE.search(pr.text) and BW_CUTLASS_RE.search(pr.text))


# ---------- LLM batch eval (bw track) ----------

EVAL_BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pr_index": {"type": "integer"},
                    "in_scope": {"type": "boolean"},
                    "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["pr_index", "in_scope", "relevance",
                             "evidence_quotes", "why"],
            },
        },
    },
    "required": ["evaluations"],
}


def _format_batch(batch: list[PRRow], short_repo: str) -> str:
    parts = []
    for i, pr in enumerate(batch):
        parts.append(
            f"## PR index {i} — {short_repo} #{pr.number}\n"
            f"Title: {pr.title}\n"
            f"Body:\n{(pr.body or '(empty)').strip()[:6000]}\n"
        )
    return "\n---\n".join(parts)


async def evaluate_bw_batch(
    system: str, batch: list[PRRow], short_repo: str,
) -> list[dict]:
    user = _format_batch(batch, short_repo)
    out = await complete_json(system, user, EVAL_BATCH_SCHEMA)
    evals = out.get("evaluations", []) if isinstance(out, dict) else []
    by_index = {e.get("pr_index"): e for e in evals if isinstance(e, dict)}
    aligned: list[dict] = []
    for i in range(len(batch)):
        e = by_index.get(i) or {
            "pr_index": i, "in_scope": False, "relevance": 1,
            "evidence_quotes": [], "why": "model returned no evaluation",
        }
        aligned.append(e)
    return aligned


# ---------- ntfy ----------

def _rfc2047(s: str) -> str:
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


async def send_ntfy(topic: str, title: str, body: str, click: str, tags: str) -> str:
    """POST to ntfy.sh. Returns "<code>:<snippet>" on success or "error:<reason>"
    on any transport failure. Never raises — a transient ntfy hiccup must not
    kill the whole run.
    """
    url = f"https://ntfy.sh/{topic}"
    headers = {"Title": _rfc2047(title), "Click": _rfc2047(click), "Tags": tags}
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(url, content=body.encode("utf-8"), headers=headers)
        return f"{r.status_code}:{r.text[:80]}"
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy_send_failed", topic=topic, err=str(e)[:200])
        return f"error:{type(e).__name__}:{str(e)[:80]}"


def _format_title(short: str, track: str, pr: PRRow, relevance: int | None) -> str:
    title = (pr.title or "").strip()
    if track == "bw":
        return f"R{relevance or 0} · {short} PR #{pr.number} · {title[:60]}"
    return f"{short} PR #{pr.number} · {title[:70]}"


def _format_body(pr: PRRow, ev: dict | None) -> str:
    if ev:
        quotes = ev.get("evidence_quotes") or []
        first = quotes[0] if quotes else ""
        return f"{ev.get('why') or ''}\n\nEvidence: \"{first[:140]}\""
    body = (pr.body or "").strip()
    return body[:300] or "(no description)"


# ---------- per-feed runner ----------

async def _repo_id_for(db: RadarDB, slug: str) -> int | None:
    async with db.conn.execute("SELECT id FROM repos WHERE slug=?", (slug,)) as cur:
        row = await cur.fetchone()
    return None if row is None else int(row["id"])


async def run_feed_bw(
    db: RadarDB, system: str, slug: str, short: str, topic: str,
    *, dry_run: bool, max_send: int,
) -> int:
    repo_id = await _repo_id_for(db, slug)
    if repo_id is None:
        log.warning("repo_missing", slug=slug)
        return 0

    rows = await db.fetch_recent_open_unassigned_prs(repo_id, WINDOW_DAYS)
    prs = [to_pr(r) for r in rows]
    prefiltered = [p for p in prs if bw_prefilter(p)]

    # Evaluate any PR that doesn't already have an eval at this prompt_version.
    to_eval: list[PRRow] = []
    for p in prefiltered:
        cached = await db.get_pr_alert_eval(p.id, "bw", PROMPT_VERSION)
        if cached is None:
            to_eval.append(p)

    log.info("bw_pipeline", slug=slug, candidates=len(prs),
             prefilter_passed=len(prefiltered), need_eval=len(to_eval))

    for i in range(0, len(to_eval), BATCH_SIZE):
        batch = to_eval[i:i + BATCH_SIZE]
        try:
            evals = await evaluate_bw_batch(system, batch, short)
        except Exception as e:  # noqa: BLE001
            log.error("bw_eval_failed", slug=slug, err=str(e), batch_start=i)
            continue
        for pr, ev in zip(batch, evals, strict=False):
            # verify evidence quotes appear in the PR text — guard against
            # hallucination, same idea as firsts pass 3.
            quotes = ev.get("evidence_quotes") or []
            haystack = " ".join(pr.text.lower().split())
            ok = all(" ".join(q.lower().split()) in haystack for q in quotes)
            in_scope = bool(ev.get("in_scope")) and ok and len(quotes) > 0
            await db.insert_pr_alert_eval(
                pr_id=pr.id, track="bw",
                in_scope=in_scope,
                relevance=int(ev.get("relevance") or 1),
                evidence_quotes=quotes,
                why=(ev.get("why") or "") + ("" if ok else " [verifier:hallucinated_quote]"),
                model=model_id(), prompt_version=PROMPT_VERSION,
            )

    # Build the alert queue: in_scope evals, no prior notification, sorted
    # oldest-first (queue drains FIFO).
    queue: list[tuple[PRRow, dict]] = []
    for p in prefiltered:
        cached = await db.get_pr_alert_eval(p.id, "bw", PROMPT_VERSION)
        if cached is None or not cached["in_scope"]:
            continue
        if await db.has_pr_notification(p.id, "bw"):
            continue
        queue.append((p, {
            "relevance": cached["relevance"],
            "why": cached["why"],
            "evidence_quotes": loads(cached["evidence_quotes_json"]) or [],
        }))

    queue.sort(key=lambda pe: pe[0].created_at)
    sent = 0
    for pr, ev in queue[:max_send]:
        title = _format_title(short, "bw", pr, ev.get("relevance"))
        body = _format_body(pr, ev)
        print(f"[bw] {title}\n  → {pr.html_url}\n  {body[:140]!r}")
        if dry_run:
            continue
        resp = await send_ntfy(topic=topic, title=title, body=body,
                               click=pr.html_url, tags="rocket")
        if resp.startswith("error:"):
            continue
        await db.insert_pr_notification(
            pr_id=pr.id, track="bw", ntfy_response=resp,
        )
        sent += 1
    return sent


async def run_feed_all(
    db: RadarDB, slug: str, short: str, topic: str,
    *, dry_run: bool, max_send: int,
) -> int:
    repo_id = await _repo_id_for(db, slug)
    if repo_id is None:
        log.warning("repo_missing", slug=slug)
        return 0

    rows = await db.fetch_recent_open_unassigned_prs(repo_id, WINDOW_DAYS)
    prs = [to_pr(r) for r in rows]
    queue: list[PRRow] = []
    for p in prs:
        if await db.has_pr_notification(p.id, "all"):
            continue
        queue.append(p)

    log.info("all_pipeline", slug=slug, candidates=len(prs), queue=len(queue))

    sent = 0
    for pr in queue[:max_send]:
        title = _format_title(short, "all", pr, None)
        body = _format_body(pr, None)
        print(f"[all] {title}\n  → {pr.html_url}")
        if dry_run:
            continue
        resp = await send_ntfy(topic=topic, title=title, body=body,
                               click=pr.html_url, tags="page_facing_up")
        if resp.startswith("error:"):
            continue
        await db.insert_pr_notification(
            pr_id=pr.id, track="all", ntfy_response=resp,
        )
        sent += 1
    return sent


# ---------- orchestrator ----------

async def run(db: RadarDB, *, dry_run: bool, max_per_feed: int) -> None:
    system = PROMPT_PATH.read_text()
    totals: dict[str, int] = {}
    for feed in FEEDS:
        slug = feed["slug"]
        short = feed["short"]
        for track in ("bw", "all"):
            env_name = feed["topics"][track]
            topic = os.environ.get(env_name, "")
            effective_dry = dry_run or not topic
            if not topic:
                log.warning("topic_env_missing", env=env_name,
                            slug=slug, track=track)
            if track == "bw":
                n = await run_feed_bw(
                    db, system, slug, short, topic,
                    dry_run=effective_dry, max_send=max_per_feed,
                )
            else:
                n = await run_feed_all(
                    db, slug, short, topic,
                    dry_run=effective_dry, max_send=max_per_feed,
                )
            totals[f"{short}:{track}"] = n
    print()
    print("ntfy sent per feed (this run):")
    for k, v in totals.items():
        print(f"  {k:<20} {v}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — per-repo PR alerts")
    p.add_argument("--dry-run", action="store_true",
                   help="Run everything but skip ntfy POSTs and notification inserts.")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_PER_FEED,
                   help=f"Max ntfy pushes per feed per run (default: {DEFAULT_MAX_PER_FEED}).")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    log.info("config", provider=selected_provider(),
             prompt_version=PROMPT_VERSION,
             dry_run=args.dry_run, max_per_feed=args.max)
    async with RadarDB(DEFAULT_DB_PATH) as db:
        await run(db, dry_run=args.dry_run, max_per_feed=args.max)


if __name__ == "__main__":
    asyncio.run(main())

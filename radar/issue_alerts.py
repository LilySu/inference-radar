"""Per-repo "achievable issue" alert feeds for Megatron / FlashInfer / TileLang / CUTLASS.

For each of the 4 repos, one ntfy feed:
  meg-firsts   → NVIDIA/Megatron-LM
  flash-firsts → flashinfer-ai/flashinfer
  tile-firsts  → tile-ai/tilelang
  cutlass-firsts → NVIDIA/cutlass

An LLM judges each open + unassigned issue for:
  1. achievable in ~1-3 days of focused work,
  2. concrete & actionable (names a function / kernel / file / test / error),
  3. not already being worked on (no WIP PR mentions, no "I'm on it" comments),
  4. not duplicated or already resolved (no "duplicate of #N", "fixed in #N").

Evaluations are cached per (issue, prompt_version) so subsequent runs only
LLM-eval newly-ingested issues. Dedup via issue_alert_notifications so we
never push the same issue twice on the same feed.

Cadence: up to 3 ntfy pushes per feed per run. With the hourly cron that
caps each feed at 3/hour; oldest-unsent first so backlogs drain FIFO.

Run:
  uv run python -m radar.issue_alerts
  uv run python -m radar.issue_alerts --dry-run
  uv run python -m radar.issue_alerts --max 1
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

from radar._llm import complete_json, model_id, selected_provider
from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

PROMPT_VERSION = "issue_v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "issue_alerts_v1.md"
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")

DEFAULT_MAX_PER_FEED = 3
BATCH_SIZE = 5
MAX_EVAL_PER_RUN = 60  # cap LLM calls per run; backlog drains across hours

FEEDS = [
    {
        "slug": "NVIDIA/Megatron-LM", "short": "Megatron",
        "track": "meg-firsts", "topic_env": "NTFY_TOPIC_MEG_FIRSTS",
    },
    {
        "slug": "flashinfer-ai/flashinfer", "short": "FlashInfer",
        "track": "flash-firsts", "topic_env": "NTFY_TOPIC_FLASH_FIRSTS",
    },
    {
        "slug": "tile-ai/tilelang", "short": "TileLang",
        "track": "tile-firsts", "topic_env": "NTFY_TOPIC_TILE_FIRSTS",
    },
    {
        "slug": "NVIDIA/cutlass", "short": "CUTLASS",
        "track": "cutlass-firsts", "topic_env": "NTFY_TOPIC_CUTLASS_FIRSTS",
    },
]


@dataclass
class IssueRow:
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


def to_issue(row) -> IssueRow:
    return IssueRow(
        id=int(row["id"]), repo_id=int(row["repo_id"]),
        number=int(row["number"]),
        title=row["title"] or "", body=row["body"] or "",
        labels=loads(row["labels_json"]) or [],
        html_url=row["html_url"] or "",
        created_at=row["created_at"] or "",
    )


# ---------- LLM batch eval ----------

EVAL_BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_index": {"type": "integer"},
                    "in_scope": {"type": "boolean"},
                    "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["issue_index", "in_scope", "relevance",
                             "evidence_quotes", "why"],
            },
        },
    },
    "required": ["evaluations"],
}


def _format_batch(batch: list[IssueRow], short_repo: str) -> str:
    parts = []
    for i, iss in enumerate(batch):
        parts.append(
            f"## Issue index {i} — {short_repo} #{iss.number}\n"
            f"Title: {iss.title}\n"
            f"Labels: {', '.join(iss.labels) or '(none)'}\n"
            f"Body:\n{(iss.body or '(empty)').strip()[:6000]}\n"
        )
    return "\n---\n".join(parts)


async def evaluate_batch(
    system: str, batch: list[IssueRow], short_repo: str,
) -> list[dict]:
    user = _format_batch(batch, short_repo)
    out = await complete_json(system, user, EVAL_BATCH_SCHEMA)
    evals = out.get("evaluations", []) if isinstance(out, dict) else []
    by_index = {e.get("issue_index"): e for e in evals if isinstance(e, dict)}
    aligned: list[dict] = []
    for i in range(len(batch)):
        e = by_index.get(i) or {
            "issue_index": i, "in_scope": False, "relevance": 1,
            "evidence_quotes": [], "why": "model returned no evaluation",
        }
        aligned.append(e)
    return aligned


def verify_quotes(issue: IssueRow, quotes: list[str]) -> bool:
    haystack = " ".join(issue.text.lower().split())
    for q in quotes:
        if " ".join(q.lower().split()) not in haystack:
            return False
    return True


# ---------- ntfy ----------

def _rfc2047(s: str) -> str:
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


async def send_ntfy(topic: str, title: str, body: str, click: str, tags: str) -> str:
    url = f"https://ntfy.sh/{topic}"
    headers = {"Title": _rfc2047(title), "Click": _rfc2047(click), "Tags": tags}
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post(url, content=body.encode("utf-8"), headers=headers)
        return f"{r.status_code}:{r.text[:80]}"
    except Exception as e:  # noqa: BLE001
        log.warning("ntfy_send_failed", topic=topic, err=str(e)[:200])
        return f"error:{type(e).__name__}:{str(e)[:80]}"


def _format_title(short: str, iss: IssueRow, relevance: int | None) -> str:
    title = (iss.title or "").strip()
    return f"R{relevance or 0} · {short} #{iss.number} · {title[:60]}"


def _format_body(iss: IssueRow, ev: dict) -> str:
    quotes = ev.get("evidence_quotes") or []
    first = quotes[0] if quotes else ""
    return f"{ev.get('why') or ''}\n\nEvidence: \"{first[:140]}\""


# ---------- per-feed runner ----------

async def _repo_id_for(db: RadarDB, slug: str) -> int | None:
    async with db.conn.execute("SELECT id FROM repos WHERE slug=?", (slug,)) as cur:
        row = await cur.fetchone()
    return None if row is None else int(row["id"])


async def run_feed(
    db: RadarDB, system: str, slug: str, short: str, track: str, topic: str,
    *, dry_run: bool, max_send: int,
) -> int:
    repo_id = await _repo_id_for(db, slug)
    if repo_id is None:
        log.warning("repo_missing", slug=slug)
        return 0

    rows = await db.fetch_open_unassigned_issues(repo_id)
    issues = [to_issue(r) for r in rows]

    # 1) Find issues that still need an eval at this prompt_version.
    to_eval: list[IssueRow] = []
    for iss in issues:
        cached = await db.get_issue_alert_eval(iss.id, track, PROMPT_VERSION)
        if cached is None:
            to_eval.append(iss)
        if len(to_eval) >= MAX_EVAL_PER_RUN:
            break

    log.info("feed_pipeline", slug=slug, track=track,
             open_unassigned=len(issues), need_eval=len(to_eval))

    # 2) LLM-eval them in batches.
    for i in range(0, len(to_eval), BATCH_SIZE):
        batch = to_eval[i:i + BATCH_SIZE]
        try:
            evals = await evaluate_batch(system, batch, short)
        except Exception as e:  # noqa: BLE001
            log.error("eval_failed", slug=slug, err=str(e), batch_start=i)
            continue
        for iss, ev in zip(batch, evals, strict=False):
            quotes = ev.get("evidence_quotes") or []
            in_scope_raw = bool(ev.get("in_scope"))
            quotes_ok = verify_quotes(iss, quotes) and len(quotes) > 0
            in_scope = in_scope_raw and quotes_ok
            await db.insert_issue_alert_eval(
                issue_id=iss.id, track=track,
                in_scope=in_scope,
                relevance=int(ev.get("relevance") or 1),
                evidence_quotes=quotes,
                why=(ev.get("why") or "") + (
                    "" if quotes_ok or not in_scope_raw
                    else " [verifier:hallucinated_quote]"
                ),
                model=model_id(), prompt_version=PROMPT_VERSION,
            )

    # 3) Build alert queue: in_scope, not yet notified, sorted oldest-first.
    queue: list[tuple[IssueRow, dict]] = []
    for iss in issues:
        cached = await db.get_issue_alert_eval(iss.id, track, PROMPT_VERSION)
        if cached is None or not cached["in_scope"]:
            continue
        if await db.has_issue_alert_notification(iss.id, track):
            continue
        queue.append((iss, {
            "relevance": cached["relevance"],
            "why": cached["why"],
            "evidence_quotes": loads(cached["evidence_quotes_json"]) or [],
        }))

    # Sort by relevance desc, then created_at asc — highest-confidence picks
    # surface first; ties go to the oldest stale issue.
    queue.sort(key=lambda ie: (-(ie[1]["relevance"] or 0), ie[0].created_at))

    sent = 0
    for iss, ev in queue[:max_send]:
        title = _format_title(short, iss, ev.get("relevance"))
        body = _format_body(iss, ev)
        print(f"[{track}] {title}\n  → {iss.html_url}\n  {body[:160]!r}")
        if dry_run:
            continue
        resp = await send_ntfy(topic=topic, title=title, body=body,
                               click=iss.html_url, tags="dart")
        if resp.startswith("error:"):
            continue
        await db.insert_issue_alert_notification(
            issue_id=iss.id, track=track, ntfy_response=resp,
        )
        sent += 1
    return sent


# ---------- orchestrator ----------

async def run(db: RadarDB, *, dry_run: bool, max_per_feed: int) -> None:
    system = PROMPT_PATH.read_text()
    totals: dict[str, int] = {}
    for feed in FEEDS:
        topic = os.environ.get(feed["topic_env"], "")
        effective_dry = dry_run or not topic
        if not topic:
            log.warning("topic_env_missing", env=feed["topic_env"],
                        slug=feed["slug"], track=feed["track"])
        n = await run_feed(
            db, system, feed["slug"], feed["short"], feed["track"], topic,
            dry_run=effective_dry, max_send=max_per_feed,
        )
        totals[feed["track"]] = n
    print()
    print("ntfy sent per feed (this run):")
    for k, v in totals.items():
        print(f"  {k:<18} {v}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — per-repo achievable-issue alerts")
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

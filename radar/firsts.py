"""Three-pass good-first-issue filter for the b200/cutlass_cute/deepseek buckets.

Pass 1 — deterministic prefilter (regex on title+body).
Pass 2 — LLM verification with mandatory evidence quotes (batched, 5 per call).
Pass 3 — deterministic verifier: every evidence quote must be a substring of the
         normalized title+body; bucket-specific keyword presence re-checked.

After verifier: two-track ntfy push.
- confirmed:  in_scope=1 AND label_confirmed=1 AND difficulty<=2 AND no prior confirmed
- speculative: in_scope=1 AND label_confirmed=0 AND difficulty<=2 AND no prior notif

Run:
  uv run python -m radar.firsts
  uv run python -m radar.firsts --dry-run
  uv run python -m radar.firsts --reevaluate --since 2026-04-01
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

PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "firsts_system_v1.md"
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")
BATCH_SIZE = 5

# Pass 1 — keyword regex. Word-boundary, case-insensitive.
PREFILTER_TERMS = [
    r"b200", r"blackwell", r"sm_?100", r"sm_?120", r"gb200",
    r"tmem", r"umma", r"tcgen05",
    r"cutlass", r"cute", r"cutlass::", r"cute::", r"wgmma", r"tma",
    r"fifth-gen tensor",
    r"deepseek", r"mla", r"multi-head latent", r"multi-token prediction", r"mtp",
    r"shared expert", r"fine-grained moe",
    r"fp8", r"fp4", r"e4m3", r"e5m2", r"mxfp",
    r"fused moe", r"grouped gemm", r"hopper", r"sm_?90",
]
PREFILTER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(PREFILTER_TERMS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Pass 3 — bucket-specific keyword presence (substring, case-insensitive)
BUCKET_KEYWORDS = {
    "b200": [
        "b200", "blackwell", "sm_100", "sm100", "sm_120", "sm120", "gb200",
        "tmem", "umma", "tcgen05",
    ],
    "cutlass_cute": ["cutlass", "cute"],
    "deepseek": [
        "deepseek", "mla", "multi-head latent", "mtp", "multi-token prediction",
        "shared expert",
    ],
}

LABEL_TRIGGERS = {"good first issue", "help wanted"}


@dataclass
class IssueRow:
    id: int
    repo_id: int
    repo_slug: str
    repo_short: str
    number: int
    title: str
    body: str
    labels: list[str]
    assignee: str | None
    state: str
    html_url: str

    @property
    def label_confirmed(self) -> bool:
        return any(lbl.lower() in LABEL_TRIGGERS for lbl in self.labels)

    @property
    def text(self) -> str:
        return f"{self.title or ''}\n{self.body or ''}"


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


# ---------------- pass 1 ----------------

def prefilter_match(issue: IssueRow) -> bool:
    return PREFILTER_RE.search(issue.text or "") is not None


# ---------------- pass 3 ----------------

def verify_evaluation(issue: IssueRow, ev: dict) -> tuple[bool, str | None]:
    """Return (ok, reason_for_failure). Mutates ev to force in_scope=False on failure."""
    if not ev.get("in_scope"):
        return True, None

    quotes = ev.get("evidence_quotes") or []
    if not quotes:
        return False, "no_evidence_quotes"

    haystack = normalize_ws(issue.text)
    for q in quotes:
        if normalize_ws(q) not in haystack:
            return False, "hallucinated_quote"

    bucket = ev.get("scope_bucket")
    if bucket not in BUCKET_KEYWORDS:
        return False, "invalid_bucket"

    quotes_lower = " ".join(q.lower() for q in quotes)
    needs = BUCKET_KEYWORDS[bucket]

    if bucket == "b200":
        explicit = any(k in quotes_lower for k in needs)
        if not explicit:
            signal = ev.get("blackwell_intent_signal")
            if not signal:
                return False, "b200_no_explicit_no_signal"
    else:
        if not any(k in quotes_lower for k in needs):
            return False, f"{bucket}_keyword_missing"

    return True, None


# ---------------- pass 2 (batched LLM) ----------------

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
                    "scope_bucket": {
                        "type": ["string", "null"],
                        "enum": ["b200", "cutlass_cute", "deepseek", None],
                    },
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                    "blackwell_intent_signal": {
                        "type": ["string", "null"],
                        "enum": [
                            "explicit-sm100", "port-stated",
                            "arch-dispatch-includes-sm100", None,
                        ],
                    },
                    "difficulty": {"type": "integer", "minimum": 1, "maximum": 5},
                    "why": {"type": "string"},
                },
                "required": [
                    "issue_index", "in_scope", "scope_bucket",
                    "evidence_quotes", "difficulty", "why",
                ],
            },
        },
    },
    "required": ["evaluations"],
}


def _format_batch_user(batch: list[IssueRow]) -> str:
    parts = []
    for i, iss in enumerate(batch):
        parts.append(
            f"## Issue index {i} — {iss.repo_slug}#{iss.number}\n"
            f"Title: {iss.title}\n"
            f"Labels: {', '.join(iss.labels) or '(none)'}\n"
            f"Body:\n{(iss.body or '(empty)').strip()[:6000]}\n"
        )
    return "\n---\n".join(parts)


async def evaluate_batch(system: str, batch: list[IssueRow]) -> list[dict]:
    user = _format_batch_user(batch)
    out = await complete_json(system, user, EVAL_BATCH_SCHEMA)
    evals = out.get("evaluations", []) if isinstance(out, dict) else []
    by_index = {e.get("issue_index"): e for e in evals if isinstance(e, dict)}
    aligned = []
    for i in range(len(batch)):
        e = by_index.get(i) or {
            "issue_index": i, "in_scope": False, "scope_bucket": None,
            "evidence_quotes": [], "blackwell_intent_signal": None,
            "difficulty": 3, "why": "model returned no evaluation",
        }
        aligned.append(e)
    return aligned


# ---------------- candidate selection ----------------

OPEN_UNASSIGNED_SQL = """
SELECT i.id, i.repo_id, r.slug AS repo_slug, r.name AS repo_short,
       i.number, i.title, i.body, i.labels_json, i.assignee, i.state, i.html_url
  FROM issues i
  JOIN repos r ON r.id = i.repo_id
 WHERE i.state = 'open'
   AND (i.assignee IS NULL OR i.assignee = '')
   AND NOT EXISTS (
       SELECT 1 FROM issue_evaluations e
        WHERE e.issue_id = i.id AND e.prompt_version = ?
   )
"""

OPEN_UNASSIGNED_SINCE_SQL = OPEN_UNASSIGNED_SQL + " AND i.updated_at >= ?"


async def fetch_candidates(
    db: RadarDB, since: str | None, reevaluate: bool,
) -> list[IssueRow]:
    """Return open, unassigned issues that have no current-prompt-version eval yet.

    With --reevaluate, we drop the NOT EXISTS clause so prior evals are recomputed.
    """
    if reevaluate:
        sql = (
            "SELECT i.id, i.repo_id, r.slug AS repo_slug, r.name AS repo_short, "
            "i.number, i.title, i.body, i.labels_json, i.assignee, i.state, i.html_url "
            "FROM issues i JOIN repos r ON r.id = i.repo_id "
            "WHERE i.state = 'open' AND (i.assignee IS NULL OR i.assignee = '')"
        )
        params: tuple = ()
        if since:
            sql += " AND i.updated_at >= ?"
            params = (since,)
    else:
        sql = OPEN_UNASSIGNED_SINCE_SQL if since else OPEN_UNASSIGNED_SQL
        params = (PROMPT_VERSION, since) if since else (PROMPT_VERSION,)

    async with db.conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [
        IssueRow(
            id=int(r["id"]), repo_id=int(r["repo_id"]),
            repo_slug=r["repo_slug"], repo_short=r["repo_short"],
            number=int(r["number"]),
            title=r["title"] or "", body=r["body"] or "",
            labels=loads(r["labels_json"]) or [],
            assignee=r["assignee"], state=r["state"], html_url=r["html_url"] or "",
        )
        for r in rows
    ]


# ---------------- ntfy ----------------

def _rfc2047(s: str) -> str:
    # HTTP headers are ASCII-only. ntfy accepts RFC 2047 encoded-words for
    # non-ASCII titles (e.g. issue titles with emoji, CJK, or the middle-dot
    # we use as a separator).
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


async def send_ntfy(topic: str, title: str, body: str, click: str, tags: str) -> str:
    url = f"https://ntfy.sh/{topic}"
    headers = {"Title": _rfc2047(title), "Click": _rfc2047(click), "Tags": tags}
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(url, content=body.encode("utf-8"), headers=headers)
    return f"{r.status_code}:{r.text[:80]}"


def _bullet_title(issue: IssueRow, ev: dict) -> str:
    title = (issue.title or "").strip()
    return (
        f"D{ev['difficulty']} · {issue.repo_short} #{issue.number} · "
        f"{ev['scope_bucket']} · {title[:60]}"
    )


def _body(ev: dict) -> str:
    quotes = ev.get("evidence_quotes") or []
    first = quotes[0] if quotes else ""
    return f"{ev.get('why', '')}\n\nEvidence: \"{first[:120]}\""


def _tags(ev: dict, speculative: bool) -> str:
    base = "rocket" if ev["difficulty"] == 1 else "dart"
    return f"{base},warning" if speculative else base


# ---------------- orchestrator ----------------

async def run(
    db: RadarDB,
    *,
    dry_run: bool,
    reevaluate: bool,
    since: str | None,
    max_evaluate: int,
) -> None:
    system = PROMPT_PATH.read_text()
    candidates = await fetch_candidates(db, since=since, reevaluate=reevaluate)
    log.info("candidates_fetched", n=len(candidates))

    # Pass 1 prefilter
    pass1: list[IssueRow] = []
    skipped_p1: list[IssueRow] = []
    for c in candidates:
        if prefilter_match(c):
            pass1.append(c)
        else:
            skipped_p1.append(c)
    log.info("prefilter", passed=len(pass1), skipped=len(skipped_p1))

    # Persist deterministic-rejected items so we don't re-process every run
    for iss in skipped_p1:
        await db.insert_evaluation(
            issue_id=iss.id, in_scope=False, scope_bucket=None,
            label_confirmed=iss.label_confirmed, evidence_quotes=[],
            blackwell_intent_signal=None, difficulty=None,
            why="prefilter_no_keyword",
            model="prefilter", prompt_version=PROMPT_VERSION,
        )

    if max_evaluate >= 0 and len(pass1) > max_evaluate:
        log.info("truncate_for_run", to=max_evaluate, of=len(pass1))
        pass1 = pass1[:max_evaluate]

    confirmed_topic = os.environ.get("NTFY_TOPIC")
    speculative_topic = os.environ.get("NTFY_TOPIC_SPECULATIVE")
    if not dry_run and not (confirmed_topic and speculative_topic):
        log.warning("ntfy_topics_missing — switching to dry-run mode")
        dry_run = True

    sent = {"confirmed": 0, "speculative": 0}
    for i in range(0, len(pass1), BATCH_SIZE):
        batch = pass1[i : i + BATCH_SIZE]
        try:
            evals = await evaluate_batch(system, batch)
        except Exception as e:  # noqa: BLE001
            log.error("evaluate_batch_failed", err=str(e), batch_start=i)
            continue

        for issue, ev in zip(batch, evals, strict=False):
            ok, fail = verify_evaluation(issue, ev)
            if not ok:
                ev["in_scope"] = False
                ev["scope_bucket"] = None
                ev["why"] = f"{ev.get('why','')} [verifier:{fail}]"

            label_confirmed = issue.label_confirmed
            eval_id = await db.insert_evaluation(
                issue_id=issue.id,
                in_scope=bool(ev.get("in_scope")),
                scope_bucket=ev.get("scope_bucket"),
                label_confirmed=label_confirmed,
                evidence_quotes=ev.get("evidence_quotes") or [],
                blackwell_intent_signal=ev.get("blackwell_intent_signal"),
                difficulty=ev.get("difficulty"),
                why=ev.get("why"),
                model=model_id(),
                prompt_version=PROMPT_VERSION,
            )

            if not ev.get("in_scope"):
                continue

            difficulty = ev.get("difficulty") or 99
            if difficulty > 2:
                continue

            track = "confirmed" if label_confirmed else "speculative"
            if await db.has_notification(issue.id, track):
                continue

            title = _bullet_title(issue, ev)
            body = _body(ev)
            tags = _tags(ev, speculative=(track == "speculative"))
            print(f"[{track}] {title}\n  → {issue.html_url}\n  {body!r}\n  tags={tags}")
            if dry_run:
                continue
            topic = confirmed_topic if track == "confirmed" else speculative_topic
            assert topic is not None
            resp = await send_ntfy(
                topic=topic, title=title, body=body, click=issue.html_url, tags=tags,
            )
            await db.insert_notification(
                issue_id=issue.id, evaluation_id=eval_id, track=track,
                ntfy_response=resp,
            )
            sent[track] += 1

    print(f"\nntfy sent: confirmed={sent['confirmed']} speculative={sent['speculative']}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — first-issue filter")
    p.add_argument("--dry-run", action="store_true",
                   help="Run all passes but do not POST to ntfy or write notifications.")
    p.add_argument("--reevaluate", action="store_true",
                   help="Re-evaluate issues even if a current-prompt-version eval exists.")
    p.add_argument("--since", default=None,
                   help="Only consider issues updated since this ISO timestamp.")
    p.add_argument("--max-evaluate", type=int, default=80,
                   help="Cap on issues sent to LLM in one run (default: 80; -1 for unlimited).")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    log.info("config", provider=selected_provider(), prompt_version=PROMPT_VERSION,
             dry_run=args.dry_run, reevaluate=args.reevaluate, since=args.since)
    async with RadarDB(DEFAULT_DB_PATH) as db:
        await run(
            db,
            dry_run=args.dry_run, reevaluate=args.reevaluate, since=args.since,
            max_evaluate=args.max_evaluate,
        )


if __name__ == "__main__":
    asyncio.run(main())

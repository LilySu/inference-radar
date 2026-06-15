"""PR classifier — categorize + technically summarize each ingested PR.

Plan 3 simplified the two-pass Haiku→Sonnet design down to a single pass on
Groq's free Llama 3.3 70B (or any backend the _llm router exposes). One row
per (pr, prompt_version) goes into pr_classifications. Novel-category
proposals get appended to data/uncategorized.json for human-in-the-loop review
— accept by editing seed/categories_seed.yml and merging.

Bot/chore PRs are classified but flagged so the daily brief can collapse them.

Run:
  uv run python -m radar.classify
  uv run python -m radar.classify --dry-run
  uv run python -m radar.classify --since 2026-05-01 --reclassify
  uv run python -m radar.classify --repo vllm-project/vllm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

from radar._llm import complete_json, model_id, selected_provider
from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "classify_v1.md"
SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "categories_seed.yml"
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")
UNCATEGORIZED_PATH = Path(os.environ.get("RADAR_UNCATEGORIZED", "data/uncategorized.json"))
BATCH_SIZE = 5

BOT_AUTHOR_PATTERNS = ("dependabot", "pre-commit-ci", "github-actions", "renovate")


@dataclass
class PRRow:
    id: int
    repo_id: int
    repo_slug: str
    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    html_url: str
    author: str | None
    additions: int | None
    deletions: int | None

    @property
    def text(self) -> str:
        return f"{self.title or ''}\n{self.body or ''}"

    @property
    def is_bot_author(self) -> bool:
        if not self.author:
            return False
        a = self.author.lower()
        return any(p in a for p in BOT_AUTHOR_PATTERNS)


CLASSIFY_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pr_index": {"type": "integer"},
                    "primary_category": {"type": ["string", "null"]},
                    "secondary_categories": {"type": "array", "items": {"type": "string"}},
                    "novel_category_proposed": {"type": ["string", "null"]},
                    "technical_summary": {"type": "string"},
                    "perf_numbers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "metric": {"type": "string"},
                                "baseline": {"type": ["string", "number", "null"]},
                                "new": {"type": ["string", "number", "null"]},
                                "delta_pct": {"type": ["number", "null"]},
                            },
                        },
                    },
                    "cross_references": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "repo": {"type": "string"},
                                "number": {"type": "integer"},
                                "why": {"type": "string"},
                            },
                        },
                    },
                    "reasoning": {"type": "string"},
                    "one_line_summary": {"type": "string"},
                    "bot_or_chore": {"type": "boolean"},
                },
                "required": [
                    "pr_index", "primary_category", "reasoning", "one_line_summary",
                    "bot_or_chore",
                ],
            },
        },
    },
    "required": ["classifications"],
}


def load_taxonomy() -> dict[str, list[dict[str, str]]]:
    """{repo_slug: [{slug, name}, ...]} keyed by repo."""
    seed = yaml.safe_load(SEED_PATH.read_text())
    return {entry["repo"]: entry["categories"] for entry in seed}


def _format_batch_user(batch: list[PRRow], categories: list[dict[str, str]]) -> str:
    cats = "\n".join(f"- {c['slug']}: {c['name']}" for c in categories)
    parts = ["## Available categories\n" + cats + "\n"]
    for i, pr in enumerate(batch):
        parts.append(
            f"## PR index {i} — {pr.repo_slug}#{pr.number}\n"
            f"Author: {pr.author or '(unknown)'}  "
            f"State: {pr.state}  "
            f"+{pr.additions or 0}/-{pr.deletions or 0}\n"
            f"Title: {pr.title}\n"
            f"Labels: {', '.join(pr.labels) or '(none)'}\n"
            f"Body:\n{(pr.body or '(empty)').strip()[:6000]}\n"
        )
    return "\n---\n".join(parts)


def _normalize_eval(c: dict[str, Any], valid_slugs: set[str]) -> dict[str, Any]:
    """Coerce model output into a sane shape; drop invalid slugs."""
    primary = c.get("primary_category")
    if primary and primary not in valid_slugs:
        # Model invented a slug — treat as a novel-category proposal
        c["novel_category_proposed"] = c.get("novel_category_proposed") or primary
        c["primary_category"] = None
    secondaries = [s for s in (c.get("secondary_categories") or []) if s in valid_slugs]
    c["secondary_categories"] = secondaries
    return c


async def classify_batch(
    system: str,
    batch: list[PRRow],
    categories: list[dict[str, str]],
) -> list[dict[str, Any]]:
    user = _format_batch_user(batch, categories)
    out = await complete_json(system, user, CLASSIFY_BATCH_SCHEMA)
    raw = out.get("classifications", []) if isinstance(out, dict) else []
    valid_slugs = {c["slug"] for c in categories}
    by_index = {c.get("pr_index"): c for c in raw if isinstance(c, dict)}
    aligned = []
    for i, pr in enumerate(batch):
        c = by_index.get(i) or {
            "pr_index": i,
            "primary_category": None,
            "secondary_categories": [],
            "novel_category_proposed": None,
            "technical_summary": "",
            "perf_numbers": [],
            "cross_references": [],
            "reasoning": "model returned no classification",
            "one_line_summary": (pr.title or "")[:90],
            "bot_or_chore": pr.is_bot_author,
        }
        c.setdefault("technical_summary", "")
        c.setdefault("perf_numbers", [])
        c.setdefault("cross_references", [])
        c.setdefault("secondary_categories", [])
        c.setdefault("novel_category_proposed", None)
        # If the author is clearly a bot, override the model's flag.
        if pr.is_bot_author:
            c["bot_or_chore"] = True
        aligned.append(_normalize_eval(c, valid_slugs))
    return aligned


# ---------------- candidate selection ----------------

CANDIDATES_SQL = """
SELECT p.id, p.repo_id, r.slug AS repo_slug, p.number, p.title, p.body,
       p.labels_json, p.state, p.html_url, p.raw_json
  FROM prs p
  JOIN repos r ON r.id = p.repo_id
 WHERE 1=1
"""


async def fetch_candidates(
    db: RadarDB,
    *,
    since: str | None,
    reclassify: bool,
    repo_filter: str | None,
    max_count: int,
) -> list[PRRow]:
    sql = CANDIDATES_SQL
    params: list[Any] = []
    if since:
        sql += " AND p.updated_at >= ?"
        params.append(since)
    if repo_filter:
        sql += " AND r.slug = ?"
        params.append(repo_filter)
    if not reclassify:
        sql += (
            " AND NOT EXISTS ("
            "  SELECT 1 FROM pr_classifications c"
            "   WHERE c.pr_id = p.id AND c.prompt_version = ?"
            " )"
        )
        params.append(PROMPT_VERSION)
    sql += " ORDER BY p.updated_at DESC LIMIT ?"
    params.append(max_count)

    async with db.conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    out: list[PRRow] = []
    for r in rows:
        raw = loads(r["raw_json"]) or {}
        user = (raw.get("user") or {}).get("login")
        out.append(
            PRRow(
                id=int(r["id"]),
                repo_id=int(r["repo_id"]),
                repo_slug=r["repo_slug"],
                number=int(r["number"]),
                title=r["title"] or "",
                body=r["body"] or "",
                labels=loads(r["labels_json"]) or [],
                state=r["state"] or "",
                html_url=r["html_url"] or "",
                author=user,
                additions=raw.get("additions"),
                deletions=raw.get("deletions"),
            )
        )
    return out


# ---------------- uncategorized queue ----------------

def append_uncategorized(novel_slug: str, pr: PRRow, reasoning: str) -> None:
    UNCATEGORIZED_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if UNCATEGORIZED_PATH.exists():
        try:
            existing = json.loads(UNCATEGORIZED_PATH.read_text())
        except json.JSONDecodeError:
            existing = []
    existing.append(
        {
            "proposed_slug": novel_slug,
            "repo": pr.repo_slug,
            "pr_number": pr.number,
            "pr_url": pr.html_url,
            "title": pr.title,
            "reasoning": reasoning,
            "prompt_version": PROMPT_VERSION,
        }
    )
    UNCATEGORIZED_PATH.write_text(json.dumps(existing, indent=2))


# ---------------- orchestrator ----------------

async def run(
    db: RadarDB,
    *,
    dry_run: bool,
    reclassify: bool,
    since: str | None,
    repo_filter: str | None,
    max_count: int,
) -> None:
    system = PROMPT_PATH.read_text()
    taxonomy = load_taxonomy()
    candidates = await fetch_candidates(
        db, since=since, reclassify=reclassify,
        repo_filter=repo_filter, max_count=max_count,
    )
    log.info("candidates_fetched", n=len(candidates), repo=repo_filter)
    if not candidates:
        print("nothing to classify.")
        return

    # Batches must be all-same-repo so each gets that repo's category list.
    by_repo: dict[str, list[PRRow]] = {}
    for c in candidates:
        by_repo.setdefault(c.repo_slug, []).append(c)

    summary: dict[str, int] = {}
    for repo_slug, prs in by_repo.items():
        cats = taxonomy.get(repo_slug)
        if not cats:
            log.warning("no_taxonomy", repo=repo_slug)
            continue
        log.info("classify_repo", repo=repo_slug, n=len(prs))
        for i in range(0, len(prs), BATCH_SIZE):
            batch = prs[i : i + BATCH_SIZE]
            try:
                evals = await classify_batch(system, batch, cats)
            except Exception as e:  # noqa: BLE001
                log.error("classify_batch_failed", repo=repo_slug, err=str(e), batch_start=i)
                continue

            for pr, ev in zip(batch, evals, strict=False):
                if dry_run:
                    print(
                        f"[{repo_slug}#{pr.number}] "
                        f"primary={ev.get('primary_category')!s:<20} "
                        f"novel={ev.get('novel_category_proposed')!s:<20} "
                        f"{(ev.get('one_line_summary') or pr.title)[:70]}"
                    )
                    continue
                await db.insert_classification(
                    pr_id=pr.id,
                    primary_category=ev.get("primary_category"),
                    secondary_categories=ev.get("secondary_categories") or [],
                    novel_category_proposed=ev.get("novel_category_proposed"),
                    technical_summary=ev.get("technical_summary"),
                    perf_numbers=ev.get("perf_numbers") or [],
                    cross_references=ev.get("cross_references") or [],
                    reasoning=ev.get("reasoning") or "",
                    one_line_summary=ev.get("one_line_summary"),
                    bot_or_chore=bool(ev.get("bot_or_chore")),
                    model=model_id(),
                    prompt_version=PROMPT_VERSION,
                )
                if ev.get("novel_category_proposed"):
                    append_uncategorized(
                        ev["novel_category_proposed"], pr, ev.get("reasoning") or "",
                    )
                key = ev.get("primary_category") or "uncategorized"
                summary[key] = summary.get(key, 0) + 1

    if not dry_run:
        print("\nclassified counts by primary category:")
        for k in sorted(summary, key=lambda s: -summary[s]):
            print(f"  {k:<24} {summary[k]:>4}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — PR classifier")
    p.add_argument("--dry-run", action="store_true",
                   help="Run the LLM but do not write classifications.")
    p.add_argument("--reclassify", action="store_true",
                   help="Re-classify PRs even if a current-prompt-version row exists.")
    p.add_argument("--since", default=None,
                   help="Only consider PRs updated since this ISO timestamp.")
    p.add_argument("--repo", default=None,
                   help="Restrict to one repo slug, e.g. vllm-project/vllm.")
    p.add_argument("--max", type=int, default=200,
                   help="Cap on PRs classified in one run (default 200).")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    log.info("config", provider=selected_provider(), prompt_version=PROMPT_VERSION,
             dry_run=args.dry_run, reclassify=args.reclassify, since=args.since)
    async with RadarDB(DEFAULT_DB_PATH) as db:
        await run(
            db,
            dry_run=args.dry_run,
            reclassify=args.reclassify,
            since=args.since,
            repo_filter=args.repo,
            max_count=args.max,
        )


if __name__ == "__main__":
    asyncio.run(main())

"""Incremental ingestion of issues and PRs from the four watched repos.

Reads `seed/repos.yml`, upserts each repo, then for each pulls issues+PRs since
the saved cursor. Cursor is the most recent `updated_at` we successfully stored.

Run: `uv run python -m radar.ingest`
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import structlog
import yaml

from radar.db import RadarDB
from radar.gh import GitHub

log = structlog.get_logger(__name__)

REPO_LIMIT_PER_RUN = 500  # safety: cap how many issues+prs we'll write per repo per run
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")
SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "repos.yml"


async def ingest_repo(db: RadarDB, gh: GitHub, slug: str, name: str) -> tuple[int, int]:
    """Ingest issues and PRs for one repo. Returns (issue_count, pr_count) upserted."""
    repo_id = await db.upsert_repo(slug, name)

    issues_cursor = await db.get_cursor(repo_id, "issues")
    issue_count = 0
    newest_issue_ts = issues_cursor

    # /issues returns both issues and PRs. We split: PRs go to prs table, issues to issues.
    prs_via_issues = 0
    async for item in gh.list_issues(slug, since=issues_cursor, state="all"):
        if issue_count >= REPO_LIMIT_PER_RUN:
            break
        ts = item.get("updated_at")
        if "pull_request" in item:
            await db.upsert_pr(repo_id, item)
            prs_via_issues += 1
        else:
            await db.upsert_issue(repo_id, item)
            issue_count += 1
        if ts and (newest_issue_ts is None or ts > newest_issue_ts):
            newest_issue_ts = ts

    if newest_issue_ts:
        await db.set_cursor(repo_id, "issues", newest_issue_ts)

    # Also ingest PRs via /pulls — gives us merged_at and richer label data not
    # always present on the /issues view. Only since cursor; on first run we
    # bootstrap with state=open to keep volume reasonable.
    prs_cursor = await db.get_cursor(repo_id, "prs")
    pr_count = prs_via_issues
    newest_pr_ts = prs_cursor
    state = "all" if prs_cursor else "open"
    async for item in gh.list_pulls(slug, state=state):
        if pr_count >= REPO_LIMIT_PER_RUN:
            break
        ts = item.get("updated_at")
        if prs_cursor and ts and ts <= prs_cursor:
            break  # /pulls is sorted desc by updated; safe to stop
        await db.upsert_pr(repo_id, item)
        pr_count += 1
        if ts and (newest_pr_ts is None or ts > newest_pr_ts):
            newest_pr_ts = ts

    if newest_pr_ts:
        await db.set_cursor(repo_id, "prs", newest_pr_ts)

    return issue_count, pr_count


async def main() -> None:
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    repos = yaml.safe_load(SEED_PATH.read_text())
    async with RadarDB(DEFAULT_DB_PATH) as db, GitHub() as gh:
        totals = []
        for r in repos:
            try:
                issues, prs = await ingest_repo(db, gh, r["slug"], r["name"])
                log.info("ingested", repo=r["slug"], issues=issues, prs=prs)
                totals.append((r["slug"], issues, prs))
            except Exception as e:  # noqa: BLE001
                log.error("ingest_failed", repo=r["slug"], err=str(e))
                totals.append((r["slug"], -1, -1))
        print()
        print(f"{'repo':<32} {'issues':>8} {'prs':>8}")
        for slug, i, p in totals:
            print(f"{slug:<32} {i:>8} {p:>8}")


if __name__ == "__main__":
    if not os.environ.get("GH_TOKEN"):
        print("error: GH_TOKEN env var is required", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main())

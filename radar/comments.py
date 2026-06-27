"""Fetch and store GitHub PR comments for the analytics layer.

Two comment types per PR:
  issue_comment  — general discussion thread (GET /issues/{n}/comments)
  review         — formal approve / request-changes decisions (GET /pulls/{n}/reviews)

Inline review comments (on specific diff lines) are skipped — they're too
low-signal for stall-reason classification and cost 1 extra API call each.

Rate budget: ~2 API calls per PR × 100 PRs = 200 calls/run, well within the
5000/hour GitHub authenticated limit. Capped via --max flag.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import structlog

from radar.db import RadarDB
from radar.gh import GitHub

log = structlog.get_logger(__name__)

PROMPT_VERSION = "comments_v1"


async def fetch_and_store(
    db: RadarDB,
    gh: GitHub,
    pr_db_id: int,
    pr_number: int,
    repo_slug: str,
) -> int:
    """Fetch issue comments + PR reviews for one PR. Returns total comments stored."""
    stored = 0

    # Issue/PR discussion comments
    try:
        async for item in gh.paginate(f"/repos/{repo_slug}/issues/{pr_number}/comments"):
            body = (item.get("body") or "").strip()
            if not body:
                continue
            await db.upsert_pr_comment(
                pr_id=pr_db_id,
                comment_id=int(item["id"]),
                author_login=(item.get("user") or {}).get("login"),
                body=body[:4000],  # cap body size stored
                created_at=item.get("created_at"),
                source="issue_comment",
            )
            stored += 1
    except Exception as exc:
        log.warning("comments_fetch_failed", pr=pr_number, repo=repo_slug,
                    source="issue_comment", err=str(exc))

    # Formal PR reviews (approve / request changes / comment)
    try:
        async for item in gh.paginate(f"/repos/{repo_slug}/pulls/{pr_number}/reviews"):
            body = (item.get("body") or "").strip()
            state = item.get("state", "")
            # Always store reviews with a formal state even if body is empty,
            # so reviewer_stance is detectable even for silent approvals.
            if not body and state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            await db.upsert_pr_comment(
                pr_id=pr_db_id,
                comment_id=int(item["id"]),
                author_login=(item.get("user") or {}).get("login"),
                body=(body or f"[{state}]")[:4000],
                created_at=item.get("submitted_at"),
                source="review",
            )
            stored += 1
    except Exception as exc:
        log.warning("comments_fetch_failed", pr=pr_number, repo=repo_slug,
                    source="review", err=str(exc))

    await db.mark_comments_fetched(pr_db_id)
    return stored


async def run(max_prs: int = 100, db_path: str | None = None) -> None:
    db_path = db_path or os.environ.get("RADAR_DB", "data/radar.db")
    async with RadarDB(db_path) as db, GitHub() as gh:
        prs = await db.fetch_prs_needing_comments(max_prs)
        log.info("comments_candidates", n=len(prs))
        total_stored = 0
        for pr in prs:
            n = await fetch_and_store(
                db, gh,
                pr_db_id=int(pr["id"]),
                pr_number=int(pr["number"]),
                repo_slug=str(pr["repo_slug"]),
            )
            total_stored += n
            log.info("comments_fetched", pr=pr["number"], repo=pr["repo_slug"], stored=n)
            # Commit periodically so a mid-run failure doesn't lose all work
            if total_stored % 50 == 0:
                await db.conn.commit()
        await db.conn.commit()
        log.info("comments_done", prs_processed=len(prs), comments_stored=total_stored)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch PR comments for analytics")
    parser.add_argument("--max", type=int, default=100, dest="max_prs",
                        help="Max PRs to process per run (default 100)")
    args = parser.parse_args()
    asyncio.run(run(max_prs=args.max_prs))


if __name__ == "__main__":
    main()

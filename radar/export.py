"""Dump SQLite slices into site/data/*.json for Next.js static build.

The Vercel site is `output: 'export'` — it reads JSON at build time, not at
request time. This script is the bridge.

Outputs:
- site/data/repos.json        — [{slug, name, pr_count, classified_count}]
- site/data/categories.json   — taxonomy by repo (mirror of seed)
- site/data/prs.json          — every PR with its latest classification
- site/data/firsts.json       — current open in-scope picks
- site/data/briefings.json    — chronological brief archive
- site/data/index.json        — landing-page summary: latest brief + top picks

Run:
  uv run python -m radar.export
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog
import yaml

from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")
DEFAULT_OUT = Path(os.environ.get("RADAR_SITE_DATA", "site/data"))
SEED_CATEGORIES = Path(__file__).resolve().parent.parent / "seed" / "categories_seed.yml"

CLASSIFY_PV = "v1"
FIRSTS_PV = "v1"


async def dump_repos(db: RadarDB, out: Path) -> list[dict[str, Any]]:
    rows = await db.list_repos()
    items: list[dict[str, Any]] = []
    for r in rows:
        async with db.conn.execute(
            "SELECT COUNT(*) AS n FROM prs WHERE repo_id=?", (r["id"],),
        ) as cur:
            pr_count = int((await cur.fetchone())["n"])  # type: ignore[index]
        async with db.conn.execute(
            """SELECT COUNT(*) AS n FROM pr_classifications c
                 JOIN prs p ON p.id = c.pr_id
                WHERE p.repo_id = ? AND c.prompt_version = ?""",
            (r["id"], CLASSIFY_PV),
        ) as cur:
            classified = int((await cur.fetchone())["n"])  # type: ignore[index]
        items.append(
            {
                "slug": r["slug"], "name": r["name"],
                "pr_count": pr_count, "classified_count": classified,
            }
        )
    (out / "repos.json").write_text(json.dumps(items, indent=2))
    return items


def dump_categories(out: Path) -> None:
    seed = yaml.safe_load(SEED_CATEGORIES.read_text())
    (out / "categories.json").write_text(json.dumps(seed, indent=2))


async def dump_prs(db: RadarDB, out: Path, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT p.id, p.number, p.title, p.html_url, p.merged_at, p.state,
           p.created_at, p.updated_at,
           r.slug AS repo, r.name AS repo_short, p.labels_json,
           c.primary_category, c.secondary_categories_json, c.novel_category_proposed,
           c.one_line_summary, c.technical_summary, c.perf_numbers_json,
           c.cross_references_json, c.reasoning, c.bot_or_chore,
           c.model, c.classified_at, c.id AS classification_id
      FROM prs p
      JOIN repos r ON r.id = p.repo_id
 LEFT JOIN pr_classifications c
        ON c.pr_id = p.id
       AND c.prompt_version = ?
       AND c.id = (SELECT id FROM pr_classifications c2
                    WHERE c2.pr_id = p.id ORDER BY c2.classified_at DESC LIMIT 1)
     ORDER BY p.updated_at DESC
     LIMIT ?
    """
    items: list[dict[str, Any]] = []
    async with db.conn.execute(sql, (CLASSIFY_PV, limit)) as cur:
        async for r in cur:
            items.append(
                {
                    "id": int(r["id"]),
                    "repo": r["repo"], "repo_short": r["repo_short"],
                    "number": int(r["number"]),
                    "title": r["title"], "html_url": r["html_url"],
                    "state": r["state"], "merged_at": r["merged_at"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                    "labels": loads(r["labels_json"]) or [],
                    "primary_category": r["primary_category"],
                    "secondary_categories": loads(r["secondary_categories_json"]) or [],
                    "novel_category_proposed": r["novel_category_proposed"],
                    "one_line_summary": r["one_line_summary"],
                    "technical_summary": r["technical_summary"],
                    "perf_numbers": loads(r["perf_numbers_json"]) or [],
                    "cross_references": loads(r["cross_references_json"]) or [],
                    "reasoning": r["reasoning"],
                    "bot_or_chore": bool(r["bot_or_chore"] or 0),
                    "model": r["model"],
                    "classified_at": r["classified_at"],
                }
            )
    (out / "prs.json").write_text(json.dumps(items, indent=2))
    return items


async def dump_firsts(db: RadarDB, out: Path, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT i.number, i.title, i.html_url, i.created_at, i.updated_at,
           r.slug AS repo, r.name AS repo_short,
           e.scope_bucket, e.difficulty, e.why, e.evidence_quotes_json,
           e.blackwell_intent_signal, e.evaluated_at, e.model
      FROM issue_evaluations e
      JOIN issues i ON i.id = e.issue_id
      JOIN repos  r ON r.id = i.repo_id
     WHERE e.in_scope = 1
       AND e.prompt_version = ?
       AND i.state = 'open'
       AND (i.assignee IS NULL OR i.assignee = '')
       AND e.id = (SELECT id FROM issue_evaluations e2
                    WHERE e2.issue_id = i.id ORDER BY e2.evaluated_at DESC LIMIT 1)
     ORDER BY e.difficulty ASC, e.evaluated_at DESC
     LIMIT ?
    """
    items: list[dict[str, Any]] = []
    async with db.conn.execute(sql, (FIRSTS_PV, limit)) as cur:
        async for r in cur:
            items.append(
                {
                    "repo": r["repo"], "repo_short": r["repo_short"],
                    "number": int(r["number"]),
                    "title": r["title"], "html_url": r["html_url"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                    "bucket": r["scope_bucket"],
                    "difficulty": int(r["difficulty"] or 0),
                    "why": r["why"],
                    "evidence_quotes": loads(r["evidence_quotes_json"]) or [],
                    "blackwell_intent_signal": r["blackwell_intent_signal"],
                    "evaluated_at": r["evaluated_at"], "model": r["model"],
                }
            )
    (out / "firsts.json").write_text(json.dumps(items, indent=2))
    return items


async def dump_briefings(db: RadarDB, out: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    async with db.conn.execute(
        "SELECT * FROM briefings ORDER BY briefing_date DESC LIMIT 60"
    ) as cur:
        async for r in cur:
            items.append(
                {
                    "briefing_date": r["briefing_date"],
                    "repo_scope": loads(r["repo_scope"]) or [],
                    "script": loads(r["script_json"]) or {},
                    "video_path": r["video_path"],
                    "video_url": r["video_url"],
                    "duration_s": r["duration_s"],
                    "built_at": r["built_at"],
                }
            )
    (out / "briefings.json").write_text(json.dumps(items, indent=2))
    return items


def write_index(
    out: Path,
    repos: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    firsts: list[dict[str, Any]],
    briefings: list[dict[str, Any]],
) -> None:
    landing = {
        "latest_briefing": briefings[0] if briefings else None,
        "top_firsts": firsts[:5],
        "recent_prs": [p for p in prs if not p.get("bot_or_chore")][:10],
        "repos": repos,
    }
    (out / "index.json").write_text(json.dumps(landing, indent=2))


async def run(db: RadarDB, out: Path, *, prs_limit: int, firsts_limit: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    repos = await dump_repos(db, out)
    dump_categories(out)
    prs = await dump_prs(db, out, prs_limit)
    firsts = await dump_firsts(db, out, firsts_limit)
    briefings = await dump_briefings(db, out)
    write_index(out, repos, prs, firsts, briefings)
    log.info("exported", out=str(out), repos=len(repos), prs=len(prs),
             firsts=len(firsts), briefings=len(briefings))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export SQLite slices as JSON for the site")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="Output directory (default: site/data)")
    p.add_argument("--prs-limit", type=int, default=2000)
    p.add_argument("--firsts-limit", type=int, default=200)
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    async with RadarDB(DEFAULT_DB_PATH) as db:
        await run(db, Path(args.out),
                  prs_limit=args.prs_limit, firsts_limit=args.firsts_limit)


if __name__ == "__main__":
    asyncio.run(main())

"""End-to-end orchestrator: ingest → firsts → classify → brief → export.

Idempotent. Each step is independently skippable. Designed to mirror the
GitHub Actions daily.yml flow so you can rehearse the daily run locally
before pushing keys to repo secrets.

Run:
  uv run python -m radar.run_all                       # full daily
  uv run python -m radar.run_all --hourly              # ingest + firsts only
  uv run python -m radar.run_all --skip-brief          # skip the mp4 stage
  uv run python -m radar.run_all --no-upload           # build brief mp4 but don't post
  uv run python -m radar.run_all --dry-run             # never POST or write notifications
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from radar import brief as brief_mod
from radar import classify as classify_mod
from radar import export as export_mod
from radar import firsts as firsts_mod
from radar import ingest as ingest_mod
from radar.db import RadarDB
from radar.gh import GitHub

log = structlog.get_logger(__name__)

DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")


# ---------------- step wrappers ----------------

async def step_ingest(db: RadarDB) -> dict[str, tuple[int, int]]:
    if not os.environ.get("GH_TOKEN"):
        log.warning("ingest_skipped — GH_TOKEN missing")
        return {}
    import yaml  # local import; pyyaml is already a dep
    repos = yaml.safe_load(ingest_mod.SEED_PATH.read_text())
    out: dict[str, tuple[int, int]] = {}
    async with GitHub() as gh:
        for r in repos:
            try:
                i, p = await ingest_mod.ingest_repo(db, gh, r["slug"], r["name"])
                log.info("ingested", repo=r["slug"], issues=i, prs=p)
                out[r["slug"]] = (i, p)
            except Exception as e:  # noqa: BLE001
                log.error("ingest_failed", repo=r["slug"], err=str(e))
                out[r["slug"]] = (-1, -1)
    return out


async def step_firsts(db: RadarDB, *, dry_run: bool) -> None:
    if not _have_llm_key():
        log.warning("firsts_skipped — no LLM key configured")
        return
    await firsts_mod.run(
        db, dry_run=dry_run, reevaluate=False, since=None, max_evaluate=80,
    )


async def step_classify(db: RadarDB, *, dry_run: bool) -> None:
    if not _have_llm_key():
        log.warning("classify_skipped — no LLM key configured")
        return
    await classify_mod.run(
        db, dry_run=dry_run, reclassify=False, since=None,
        repo_filter=None, max_count=200,
    )


async def step_brief(db: RadarDB, *, no_upload: bool, script_only: bool) -> None:
    if not _have_llm_key():
        log.warning("brief_skipped — no LLM key configured")
        return
    briefing_date = datetime.now(UTC).date().isoformat()
    await brief_mod.run(
        db, briefing_date=briefing_date,
        no_upload=no_upload, script_only=script_only, classify_pv="v1",
    )


async def step_export(db: RadarDB) -> None:
    out_dir = export_mod.DEFAULT_OUT
    await export_mod.run(db, out_dir, prs_limit=2000, firsts_limit=200)


# ---------------- helpers ----------------

def _have_llm_key() -> bool:
    provider = os.environ.get("RADAR_LLM", "groq").lower()
    if provider == "groq":
        return bool(os.environ.get("GROQ_API_KEY"))
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return provider == "claude_code"  # subprocess; no key needed


async def _run(steps: list[tuple[str, Callable[[], Awaitable[object]]]]) -> None:
    for name, fn in steps:
        log.info("step_start", name=name)
        try:
            await fn()
            log.info("step_done", name=name)
        except Exception as e:  # noqa: BLE001
            log.error("step_failed", name=name, err=str(e))
            # Keep going — later steps may still produce useful artifacts.


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — end-to-end runner")
    p.add_argument("--hourly", action="store_true",
                   help="Ingest + firsts only. Skip classify, brief, export.")
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--skip-firsts", action="store_true")
    p.add_argument("--skip-classify", action="store_true")
    p.add_argument("--skip-brief", action="store_true")
    p.add_argument("--skip-export", action="store_true")
    p.add_argument("--no-upload", action="store_true",
                   help="Build the brief mp4 but skip the YouTube upload.")
    p.add_argument("--script-only", action="store_true",
                   help="Brief stops after writing script.json.")
    p.add_argument("--dry-run", action="store_true",
                   help="Firsts won't POST to ntfy; classify won't write rows.")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])

    async with RadarDB(DEFAULT_DB_PATH) as db:
        steps: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        if not args.skip_ingest:
            steps.append(("ingest", lambda: step_ingest(db)))
        if not args.skip_firsts:
            steps.append(("firsts", lambda: step_firsts(db, dry_run=args.dry_run)))
        if not args.hourly:
            if not args.skip_classify:
                steps.append(
                    ("classify", lambda: step_classify(db, dry_run=args.dry_run))
                )
            if not args.skip_brief:
                steps.append(
                    (
                        "brief",
                        lambda: step_brief(
                            db, no_upload=args.no_upload, script_only=args.script_only,
                        ),
                    )
                )
            if not args.skip_export:
                steps.append(("export", lambda: step_export(db)))
        await _run(steps)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)

"""Early warning digest: SQL queries over all analytics tables.

Produces a structured private report answering:
  EMERGING     — techniques in papers/cross-repo not yet in vLLM
  RIPENING     — stalled open PRs a newcomer could address
  OPEN TERRAIN — keyword areas with no dominant org (contribution territory)
  COMPANY MAP  — which orgs own which topic areas
  RISING       — contributors who have recently accelerated

Run with: python -m radar.intel [--days N] [--out FILE]

Output goes to stdout (or --out path) as plain text. Designed to be read
privately, not published.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import structlog

from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

# Bot/automation accounts that open PRs but are not real contributors.
# Used in RISING CONTRIBUTORS and CROSS-REPO TRAVELERS queries.
_AUTHOR_BOTS = (
    "dependabot", "dependabot[bot]", "pre-commit-ci", "pre-commit-ci[bot]",
    "github-actions", "github-actions[bot]", "renovate", "renovate[bot]",
    "codecov", "codecov[bot]", "copilot", "copilot[bot]",
    "coderabbitai", "coderabbitai[bot]", "sourcery-ai",
    "tensorrt-cicd", "mcore-oncall", "nvidia", "aws",
)


# ---------------------------------------------------------------------------
# Section builders — each returns a list of text lines
# ---------------------------------------------------------------------------

async def _section_emerging(db: RadarDB, lookback_days: int) -> list[str]:
    """Papers published in the last N days that match inference buckets
    but have NOT yet appeared in vLLM PRs."""
    lines = ["=== EMERGING — techniques not yet in vLLM ===", ""]

    # Papers with no vLLM PR appearance yet
    async with db.conn.execute(
        """SELECT ps.title, ps.published_date, ps.keyword_buckets,
                  ps.abstract_snippet, ps.hf_url,
                  MIN(kfs.first_seen) AS earliest_cross_repo
           FROM paper_signals ps
           LEFT JOIN keyword_first_seen kfs ON (
               kfs.bucket IN (SELECT value FROM json_each(ps.keyword_buckets))
               AND kfs.repo_id != (
                   SELECT id FROM repos WHERE slug='vllm-project/vllm'
               )
           )
           WHERE ps.vllm_pr_appeared IS NULL
             AND ps.published_date > date('now', ?)
           GROUP BY ps.paper_id
           ORDER BY ps.published_date DESC""",
        (f"-{lookback_days} days",),
    ) as cur:
        papers = await cur.fetchall()

    if not papers:
        lines.append("  (none in window)")
    for p in papers:
        buckets = loads(p["keyword_buckets"]) or []
        cross = p["earliest_cross_repo"]
        age_label = f"published {p['published_date']}"
        cross_label = f", in cross-repo since {cross}" if cross else ""
        lines.append(f"  [{', '.join(buckets[:2])}] {p['title'][:80]}")
        lines.append(f"      {age_label}{cross_label}")
        if p["hf_url"]:
            lines.append(f"      {p['hf_url']}")
        if p["abstract_snippet"]:
            lines.append(f"      {p['abstract_snippet'][:120]}…")
        lines.append("")

    # Keyword buckets present in SGLang but absent from vLLM
    async with db.conn.execute(
        """SELECT kfs_s.bucket,
                  kfs_s.first_seen AS sglang_date,
                  kfs_v.first_seen AS vllm_date,
                  CAST(julianday('now') - julianday(kfs_s.first_seen) AS INTEGER) AS days_lag
           FROM keyword_first_seen kfs_s
           JOIN repos rs ON rs.id = kfs_s.repo_id AND rs.slug = 'sgl-project/sglang'
           LEFT JOIN keyword_first_seen kfs_v ON (
               kfs_v.bucket = kfs_s.bucket
               AND kfs_v.repo_id = (SELECT id FROM repos WHERE slug='vllm-project/vllm')
           )
           WHERE kfs_v.bucket IS NULL
             AND days_lag BETWEEN 5 AND 90
           ORDER BY kfs_s.first_seen DESC"""
    ) as cur:
        lags = await cur.fetchall()

    if lags:
        lines.append("  [Cross-repo lag — in SGLang, not yet in vLLM]")
        for lag in lags:
            lines.append(
                f"    {lag['bucket']:30s}  SGLang: {lag['sglang_date'][:10]}"
                f"  lag: {lag['days_lag']}d"
            )
        lines.append("")

    return lines


async def _section_ripening(db: RadarDB) -> list[str]:
    """Open PRs that are stalled but a newcomer could realistically address."""
    lines = ["=== RIPENING — stalled PRs a newcomer can address ===", ""]

    async with db.conn.execute(
        """SELECT p.number, p.title, p.html_url, p.created_at, p.keyword_bucket,
                  rs.stall_reason, rs.reviewer_stance, rs.one_line_reason,
                  r.slug AS repo_slug,
                  CAST(julianday('now') - julianday(p.created_at) AS INTEGER) AS days_open,
                  co.org AS author_org,
                  p.author_login
           FROM prs p
           JOIN repos r       ON r.id  = p.repo_id
           JOIN pr_review_signal rs ON rs.pr_id = p.id
           LEFT JOIN contributor_orgs co ON co.login = p.author_login
           WHERE p.state = 'open'
             AND rs.newbie_viable = 1
             AND rs.stall_reason NOT IN ('duplicate_internal','not_on_roadmap','needs_rfc_first')
           ORDER BY
               CASE rs.stall_reason
                   WHEN 'style_nits_only'      THEN 1
                   WHEN 'needs_rebase'         THEN 2
                   WHEN 'needs_tests'          THEN 3
                   WHEN 'ci_failure'           THEN 4
                   WHEN 'needs_benchmarks'     THEN 5
                   WHEN 'explicitly_welcomed'  THEN 6
                   WHEN 'no_reviewer_capacity' THEN 7
                   ELSE 8
               END,
               days_open DESC
           LIMIT 20"""
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        lines.append("  (none yet — run enrich + comments first)")
    for r in rows:
        bucket = r["keyword_bucket"] or "uncategorized"
        org_tag = f" [{r['author_org']}]" if r["author_org"] else ""
        lines.append(
            f"  [{r['stall_reason']}] #{r['number']} {r['title'][:70]}"
        )
        lines.append(
            f"      repo: {r['repo_slug']}  bucket: {bucket}"
            f"  open: {r['days_open']}d  stance: {r['reviewer_stance']}"
        )
        lines.append(
            f"      author: @{r['author_login'] or '?'}{org_tag}"
        )
        lines.append(f"      why: {r['one_line_reason']}")
        if r["html_url"]:
            lines.append(f"      {r['html_url']}")
        lines.append("")

    return lines


async def _section_open_terrain(db: RadarDB) -> list[str]:
    """Keyword areas where no single org dominates (>40% merge share)."""
    lines = ["=== OPEN TERRAIN — no dominant org (safe to contribute) ===", ""]

    async with db.conn.execute(
        """SELECT p.keyword_bucket,
                  co.org,
                  COUNT(*) AS pr_count,
                  SUM(CASE WHEN p.merged_at IS NOT NULL THEN 1 ELSE 0 END) AS merged
           FROM prs p
           JOIN contributor_orgs co ON co.login = p.author_login
           WHERE p.keyword_bucket IS NOT NULL
             AND co.org IS NOT NULL
             AND p.created_at > date('now', '-180 days')
           GROUP BY p.keyword_bucket, co.org
           ORDER BY p.keyword_bucket, merged DESC"""
    ) as cur:
        rows = await cur.fetchall()

    # Group by bucket, compute max org share
    from collections import defaultdict
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bucket_total: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r["keyword_bucket"] or not r["org"]:
            continue
        buckets[r["keyword_bucket"]][r["org"]] += int(r["merged"] or 0)
        bucket_total[r["keyword_bucket"]] += int(r["merged"] or 0)

    open_buckets = []
    for bucket, org_counts in buckets.items():
        total = bucket_total[bucket]
        if total < 3:
            continue
        top_share = max(org_counts.values()) / total if total else 1.0
        if top_share < 0.45:
            top_org = max(org_counts, key=lambda k: org_counts[k])
            open_buckets.append((bucket, top_share, total, top_org, org_counts[top_org]))

    open_buckets.sort(key=lambda x: x[1])

    if not open_buckets:
        lines.append("  (not enough data yet — run enrich to populate contributor_orgs)")
    for bucket, share, total, top_org, top_count in open_buckets:
        lines.append(
            f"  {bucket:30s}  top org: {top_org} ({top_count}/{total} merges, "
            f"{share*100:.0f}% share)"
        )
    lines.append("")
    return lines


async def _section_company_map(db: RadarDB) -> list[str]:
    """Which companies are responsible for which keyword areas."""
    lines = ["=== COMPANY MAP — org × keyword area ===", ""]

    async with db.conn.execute(
        """SELECT co.org, p.keyword_bucket,
                  COUNT(*) AS pr_count,
                  SUM(CASE WHEN p.merged_at IS NOT NULL THEN 1 ELSE 0 END) AS merged,
                  ROUND(
                      SUM(CASE WHEN p.merged_at IS NOT NULL THEN 1 ELSE 0 END) * 100.0
                      / COUNT(*), 0
                  ) AS merge_pct
           FROM prs p
           JOIN contributor_orgs co ON co.login = p.author_login
           WHERE p.keyword_bucket IS NOT NULL
             AND co.org IS NOT NULL
             AND p.created_at > date('now', '-180 days')
           GROUP BY co.org, p.keyword_bucket
           HAVING pr_count >= 2
           ORDER BY co.org, merged DESC"""
    ) as cur:
        rows = await cur.fetchall()

    from collections import defaultdict
    by_org: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["org"] and r["keyword_bucket"]:
            by_org[r["org"]].append(r)

    if not by_org:
        lines.append("  (not enough data yet)")
    for org in sorted(by_org, key=lambda o: sum(r["merged"] or 0 for r in by_org[o]), reverse=True):
        buckets_str = "  |  ".join(
            f"{r['keyword_bucket']} ({r['merged']}/{r['pr_count']}, {r['merge_pct']:.0f}%)"
            for r in by_org[org][:5]
        )
        lines.append(f"  {org:20s}  {buckets_str}")
    lines.append("")
    return lines


async def _section_label_merge_times(db: RadarDB) -> list[str]:
    """Median days-to-merge per label × keyword bucket (top combinations)."""
    lines = ["=== MERGE VELOCITY — median days by label × bucket ===", ""]

    async with db.conn.execute(
        """SELECT p.labels_json, p.keyword_bucket,
                  COUNT(*) AS n,
                  ROUND(
                      AVG(julianday(p.merged_at) - julianday(p.created_at)), 1
                  ) AS avg_days
           FROM prs p
           WHERE p.merged_at IS NOT NULL
             AND p.keyword_bucket IS NOT NULL
             AND p.labels_json IS NOT NULL
             AND p.labels_json != '[]'
             AND p.created_at > date('now', '-365 days')
           GROUP BY p.labels_json, p.keyword_bucket
           HAVING n >= 3
           ORDER BY avg_days ASC
           LIMIT 20"""
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        lines.append("  (not enough merged PRs with labels yet)")
    for r in rows:
        labels = loads(r["labels_json"]) or []
        label_str = ", ".join(labels[:3])
        lines.append(
            f"  {r['keyword_bucket']:28s}  [{label_str}]"
            f"  n={r['n']}  avg {r['avg_days']}d"
        )
    lines.append("")
    return lines


async def _section_mentions_power(db: RadarDB) -> list[str]:
    """Who gets mentioned most (by keyword area) — the de-facto gatekeepers."""
    lines = ["=== MENTION POWER — who controls each area ===", ""]

    # Known bots/team-accounts to exclude from mention power ranking
    _MENTION_BOTS = (
        "coderabbitai", "dependabot", "github-actions", "renovate",
        "pre-commit-ci", "codecov", "copilot", "sourcery-ai", "greptile",
        "sweep-ai", "ellipsis-dev", "nvidia", "anthropic", "meta-llama",
        "google", "microsoft", "amazon", "aws", "mcore-oncall",
    )
    bot_placeholders = ",".join("?" * len(_MENTION_BOTS))
    # Use all PRs (open + closed/merged) — merged_at is sparse in early ingest data
    async with db.conn.execute(
        f"""SELECT pm.mentioned_login, co.org, p.keyword_bucket,
                  COUNT(*) AS mention_count
           FROM pr_mentions pm
           JOIN prs p         ON p.id = pm.pr_id
           LEFT JOIN contributor_orgs co ON co.login = pm.mentioned_login
           WHERE p.keyword_bucket IS NOT NULL
             AND p.created_at > date('now', '-180 days')
             AND pm.mentioned_login NOT IN ({bot_placeholders})
           GROUP BY pm.mentioned_login, p.keyword_bucket
           HAVING mention_count >= 3
           ORDER BY p.keyword_bucket, mention_count DESC""",
        list(_MENTION_BOTS),
    ) as cur:
        rows = await cur.fetchall()

    from collections import defaultdict
    by_bucket: dict[str, list] = defaultdict(list)
    for r in rows:
        if r["keyword_bucket"]:
            by_bucket[r["keyword_bucket"]].append(r)

    if not by_bucket:
        lines.append("  (run comments + enrich to populate mentions)")
    for bucket in sorted(by_bucket):
        top = by_bucket[bucket][:3]
        people = "  |  ".join(
            f"@{r['mentioned_login']} ({r['org'] or '?'}, {r['mention_count']}x)"
            for r in top
        )
        lines.append(f"  {bucket:30s}  {people}")
    lines.append("")
    return lines


async def _section_rising_contributors(db: RadarDB) -> list[str]:
    """Contributors whose recent merge count exceeds their historical pace."""
    lines = ["=== RISING CONTRIBUTORS — recent acceleration ===", ""]

    # Include closed PRs as proxy for merged (merged_at sparse in early data)
    bot_ph = ",".join("?" * len(_AUTHOR_BOTS))
    async with db.conn.execute(
        f"""SELECT p.author_login AS login,
                  co.org,
                  p.keyword_bucket,
                  SUM(CASE WHEN p.created_at > date('now', '-60 days') THEN 1 ELSE 0 END) AS recent,
                  SUM(CASE WHEN p.created_at <= date('now', '-60 days') THEN 1 ELSE 0 END) AS historical
           FROM prs p
           LEFT JOIN contributor_orgs co ON co.login = p.author_login
           WHERE (p.merged_at IS NOT NULL OR p.state = 'closed')
             AND p.created_at > date('now', '-365 days')
             AND p.author_login IS NOT NULL
             AND p.author_login NOT IN ({bot_ph})
           GROUP BY p.author_login, p.keyword_bucket
           HAVING recent >= 3 AND recent > historical
           ORDER BY recent DESC
           LIMIT 15""",
        list(_AUTHOR_BOTS),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        lines.append("  (not enough merge history yet)")
    for r in rows:
        org_tag = f" [{r['org']}]" if r["org"] else ""
        lines.append(
            f"  @{r['login']}{org_tag}  {r['keyword_bucket'] or 'general'}"
            f"  +{r['recent']} recent vs {r['historical']} historical"
        )
    lines.append("")
    return lines


async def _section_cross_repo_travelers(db: RadarDB) -> list[str]:
    """Contributors active in multiple repos — highest-leverage to know."""
    lines = ["=== CROSS-REPO TRAVELERS — multi-repo contributors ===", ""]

    bot_ph = ",".join("?" * len(_AUTHOR_BOTS))
    async with db.conn.execute(
        f"""SELECT p.author_login AS login,
                  co.org,
                  COUNT(DISTINCT p.repo_id) AS repo_count,
                  GROUP_CONCAT(DISTINCT r.slug) AS repos,
                  SUM(CASE WHEN p.merged_at IS NOT NULL THEN 1 ELSE 0 END) AS total_merges
           FROM prs p
           JOIN repos r ON r.id = p.repo_id
           LEFT JOIN contributor_orgs co ON co.login = p.author_login
           WHERE p.created_at > date('now', '-180 days')
             AND p.author_login IS NOT NULL
             AND p.author_login NOT IN ({bot_ph})
           GROUP BY p.author_login
           HAVING repo_count > 1
           ORDER BY total_merges DESC
           LIMIT 20""",
        list(_AUTHOR_BOTS),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        lines.append("  (need data from multiple repos)")
    for r in rows:
        org_tag = f" [{r['org']}]" if r["org"] else ""
        lines.append(
            f"  @{r['login']}{org_tag}  {r['repo_count']} repos  "
            f"{r['total_merges']} merges  | {r['repos']}"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(
    lookback_days: int = 30,
    output_path: str | None = None,
    db_path: str | None = None,
) -> str:
    db_path = db_path or os.environ.get("RADAR_DB", "data/radar.db")
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    async with RadarDB(db_path) as db:
        buf = StringIO()
        buf.write(f"{'='*60}\n")
        buf.write(f"  INFERENCE RADAR INTEL  —  {today}\n")
        buf.write(f"{'='*60}\n\n")

        sections = [
            await _section_emerging(db, lookback_days),
            await _section_ripening(db),
            await _section_open_terrain(db),
            await _section_company_map(db),
            await _section_label_merge_times(db),
            await _section_mentions_power(db),
            await _section_rising_contributors(db),
            await _section_cross_repo_travelers(db),
        ]
        for section in sections:
            buf.write("\n".join(section))
            buf.write("\n")

        report = buf.getvalue()

    if output_path:
        Path(output_path).write_text(report, encoding="utf-8")
        log.info("intel_written", path=output_path)
    else:
        print(report)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate private intel digest")
    parser.add_argument("--days", type=int, default=30,
                        help="Lookback window for emerging topics (default 30)")
    parser.add_argument("--out", type=str, default=None,
                        help="Write report to this file instead of stdout")
    args = parser.parse_args()
    asyncio.run(run(lookback_days=args.days, output_path=args.out))


if __name__ == "__main__":
    main()

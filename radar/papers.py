"""HuggingFace daily papers scraper for the emerging-topic early warning layer.

Fetches the HF Papers feed (curated ~15 ML papers/day) for recent days,
matches each paper's title + abstract against KEYWORD_BUCKETS, and stores
hits in paper_signals. Also cross-references stored papers against vLLM PR
keyword appearances to mark when a paper's technique finally shows up in code.

Endpoint: https://huggingface.co/api/daily_papers?date=YYYY-MM-DD
Falls back to today if date parameter not accepted.

Run with: python -m radar.papers [--days-back N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import structlog

from radar.db import RadarDB, loads
from radar.enrich import assign_keyword_bucket

log = structlog.get_logger(__name__)

HF_PAPERS_URL = "https://huggingface.co/api/daily_papers"

# Only store papers that match at least one inference-relevant bucket.
# "model_support" is too broad on its own; require a second bucket alongside it.
_INFERENCE_BUCKETS = frozenset({
    "speculative_decoding", "moe_routing", "attention_kernel", "kv_cache",
    "quantization", "hardware_hopper", "hardware_ada", "hardware_amd",
    "hardware_other", "lora_serving", "distributed", "scheduler",
})

# At least one LLM/inference anchor must appear in title+abstract to avoid
# false positives: "mixture of experts" in a speech paper, "kv cache" in a
# database paper, etc.
_LLM_ANCHOR_RE = re.compile(
    r"\b("
    r"large language model|language model|llm|"
    r"autoregressive|transformer\b.{0,40}(?:model|inference|serving)|"
    r"token generation|decoding|"
    r"(?:llm|model)\s+inference|inference serving|"
    r"prefill|kv.?cache|attention mechanism|"
    r"speculative decod|vllm|sglang|trt-llm|tensorrt"
    r")\b",
    re.IGNORECASE,
)


async def _fetch_papers_for_date(
    cli: httpx.AsyncClient, target_date: date,
) -> list[dict]:
    date_str = target_date.strftime("%Y-%m-%d")
    try:
        r = await cli.get(HF_PAPERS_URL, params={"date": date_str}, timeout=20)
        if r.status_code != 200:
            log.warning("hf_papers_error", date=date_str, status=r.status_code)
            return []
        data = r.json()
        if not isinstance(data, list):
            log.warning("hf_papers_unexpected_shape", date=date_str)
            return []
        return data
    except Exception as exc:
        log.warning("hf_papers_fetch_failed", date=date_str, err=str(exc))
        return []


def _extract_fields(item: dict) -> tuple[str, str, str | None, str | None, str | None]:
    """Pull (paper_id, title, abstract, published_date, arxiv_id) from HF response item."""
    paper = item.get("paper") or item  # top-level or nested under "paper" key
    paper_id: str = paper.get("id") or item.get("id") or ""
    title: str = paper.get("title") or ""
    abstract: str = paper.get("summary") or paper.get("abstract") or ""
    # HF uses publishedAt at item level or paper level
    published_raw: str | None = (
        item.get("publishedAt") or paper.get("publishedAt") or paper.get("published_at")
    )
    published_date: str | None = None
    if published_raw:
        try:
            published_date = published_raw[:10]  # YYYY-MM-DD
        except Exception:
            pass
    arxiv_id: str | None = paper.get("arxivId") or paper.get("arxiv_id")
    if not arxiv_id and paper_id and not paper_id.startswith("http"):
        # HF paper IDs are often the arXiv ID directly
        arxiv_id = paper_id
    return paper_id, title, abstract[:600], published_date, arxiv_id


def _is_inference_relevant(buckets: list[str], text: str) -> bool:
    """Require a hard inference bucket AND an LLM-context anchor in the text.

    The anchor check blocks keyword false-positives: 'mixture of experts' in a
    speech model paper, 'kv cache' in a database paper, etc.
    """
    if not (set(buckets) & _INFERENCE_BUCKETS):
        return False
    return bool(_LLM_ANCHOR_RE.search(text))


async def _cross_reference_vllm(db: RadarDB) -> int:
    """
    For each paper_signal where vllm_pr_appeared is NULL,
    check whether any vLLM PR now has a matching keyword bucket.
    If yes, mark the date it first appeared.
    """
    updated = 0
    async with db.conn.execute(
        """SELECT ps.paper_id, ps.keyword_buckets, ps.published_date
           FROM paper_signals ps
           WHERE ps.vllm_pr_appeared IS NULL
             AND ps.published_date > date('now', '-60 days')"""
    ) as cur:
        papers = await cur.fetchall()

    vllm_repo_row = None
    async with db.conn.execute(
        "SELECT id FROM repos WHERE slug='vllm-project/vllm'"
    ) as cur:
        vllm_repo_row = await cur.fetchone()
    if not vllm_repo_row:
        return 0
    vllm_repo_id = int(vllm_repo_row["id"])

    for paper in papers:
        buckets: list[str] = loads(paper["keyword_buckets"]) or []
        if not buckets:
            continue
        placeholders = ",".join("?" * len(buckets))
        async with db.conn.execute(
            f"""SELECT MIN(p.created_at) AS first_seen
                FROM prs p
                WHERE p.repo_id = ?
                  AND p.keyword_bucket IN ({placeholders})
                  AND p.created_at >= ?""",
            [vllm_repo_id, *buckets, paper["published_date"] or "2020-01-01"],
        ) as cur:
            row = await cur.fetchone()
        if row and row["first_seen"]:
            appeared_date = str(row["first_seen"])[:10]
            await db.mark_paper_vllm_appeared(str(paper["paper_id"]), appeared_date)
            updated += 1

    return updated


async def run(days_back: int = 7, db_path: str | None = None) -> None:
    db_path = db_path or os.environ.get("RADAR_DB", "data/radar.db")
    today = datetime.now(UTC).date()

    async with RadarDB(db_path) as db:
        async with httpx.AsyncClient(
            headers={"User-Agent": "inference-radar/0.1"},
        ) as cli:
            total_stored = 0
            for offset in range(days_back):
                target = today - timedelta(days=offset)
                items = await _fetch_papers_for_date(cli, target)
                log.info("hf_papers_fetched", date=str(target), n=len(items))

                for item in items:
                    paper_id, title, abstract, published_date, arxiv_id = _extract_fields(item)
                    if not paper_id or not title:
                        continue

                    text = f"{title} {abstract}"
                    primary, secondary = assign_keyword_bucket(text)
                    all_buckets = ([primary] if primary else []) + secondary

                    if not _is_inference_relevant(all_buckets, text):
                        continue

                    hf_url = (
                        f"https://huggingface.co/papers/{arxiv_id}"
                        if arxiv_id else None
                    )
                    await db.upsert_paper_signal(
                        paper_id=paper_id,
                        title=title,
                        published_date=published_date or str(target),
                        keyword_buckets=all_buckets,
                        abstract_snippet=abstract[:400] or None,
                        hf_url=hf_url,
                        arxiv_id=arxiv_id,
                    )
                    total_stored += 1
                    log.info("paper_stored", title=title[:60], buckets=all_buckets)

        cross_updated = await _cross_reference_vllm(db)
        log.info("papers_done", stored=total_stored, cross_ref_updated=cross_updated)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch HF daily papers for analytics")
    parser.add_argument("--days-back", type=int, default=7,
                        help="How many days back to fetch (default 7)")
    args = parser.parse_args()
    asyncio.run(run(days_back=args.days_back))


if __name__ == "__main__":
    main()

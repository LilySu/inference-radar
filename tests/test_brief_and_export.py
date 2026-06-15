"""Tests for the brief input formatter and the SQLite-to-JSON export shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from radar.brief import BriefInput, FirstItem, PRItem, _format_input_for_prompt
from radar.db import RadarDB
from radar.export import dump_briefings, dump_firsts, dump_prs, dump_repos, write_index


def _pr_item(**overrides) -> PRItem:
    defaults = {
        "repo": "vllm-project/vllm", "repo_short": "vLLM",
        "number": 1, "title": "Some PR", "html_url": "https://x/1",
        "primary_category": "moe", "secondary_categories": [],
        "one_line_summary": "Fused FP4 MoE GEMM",
        "technical_summary": "Adds an FP4 path to fused MoE GEMM.",
        "perf_numbers": [{"metric": "tok/s", "baseline": "180", "new": "240", "delta_pct": 33.3}],
        "bot_or_chore": False,
    }
    defaults.update(overrides)
    return PRItem(**defaults)


def test_format_input_quotes_perf_numbers_verbatim():
    bi = BriefInput(
        briefing_date="2026-05-12",
        repo_scope=["vllm-project/vllm"],
        prs=[_pr_item()],
        firsts=[],
    )
    s = _format_input_for_prompt(bi)
    # The technical summary, perf numbers, and category must all be present.
    assert "tok/s 180→240" in s
    assert "Fused FP4 MoE GEMM" in s
    assert "moe" in s


def test_format_input_separates_chore_prs():
    bi = BriefInput(
        briefing_date="2026-05-12",
        repo_scope=["vllm-project/vllm"],
        prs=[_pr_item(), _pr_item(number=2, bot_or_chore=True, title="Bump version")],
        firsts=[],
    )
    s = _format_input_for_prompt(bi)
    assert "Chore/bot PRs" in s
    assert "Bump version" in s


def test_format_input_emits_picks_section():
    bi = BriefInput(
        briefing_date="2026-05-12",
        repo_scope=["vllm-project/vllm"],
        prs=[],
        firsts=[
            FirstItem(
                repo="vllm-project/vllm", repo_short="vLLM",
                number=123, title="Add MLA fast path",
                html_url="https://x/123", bucket="deepseek",
                difficulty=2, why="self-contained, 30 lines",
            ),
        ],
    )
    s = _format_input_for_prompt(bi)
    assert "Open first-issue picks" in s
    assert "D2 · vLLM #123 · deepseek" in s


@pytest.mark.asyncio
async def test_export_writes_all_expected_files(tmp_path: Path):
    db_path = tmp_path / "radar.db"
    async with RadarDB(db_path) as db:
        # Seed two repos so dump_repos has something to query
        await db.upsert_repo("vllm-project/vllm", "vLLM")
        await db.upsert_repo("sgl-project/sglang", "SGLang")

        out = tmp_path / "site_data"
        out.mkdir()
        repos = await dump_repos(db, out)
        prs = await dump_prs(db, out, 100)
        firsts = await dump_firsts(db, out, 50)
        briefings = await dump_briefings(db, out)
        write_index(out, repos, prs, firsts, briefings)

    expected = {"repos.json", "prs.json", "firsts.json", "briefings.json", "index.json"}
    written = {p.name for p in out.iterdir()}
    # categories.json isn't written here (no dump_categories call), but the
    # five files driven by SQLite must all be present.
    assert expected.issubset(written), f"missing: {expected - written}"
    assert len(repos) == 2
    assert prs == []  # no PRs seeded
    assert firsts == []

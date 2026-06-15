"""Tests for the classifier — schema normalization + uncategorized queue."""

from __future__ import annotations

import json

import radar.classify as classify_mod
from radar.classify import PRRow, _normalize_eval, append_uncategorized


def _pr(**overrides) -> PRRow:
    defaults = {
        "id": 1, "repo_id": 1, "repo_slug": "vllm-project/vllm",
        "number": 42, "title": "Some PR", "body": "",
        "labels": [], "state": "open", "html_url": "https://x/42",
        "author": "alice", "additions": 10, "deletions": 5,
    }
    defaults.update(overrides)
    return PRRow(**defaults)


def test_normalize_accepts_valid_primary():
    valid = {"attention", "moe"}
    ev = {"primary_category": "attention", "secondary_categories": ["moe"]}
    out = _normalize_eval(ev, valid)
    assert out["primary_category"] == "attention"
    assert out["secondary_categories"] == ["moe"]


def test_normalize_demotes_invalid_primary_to_novel():
    valid = {"attention"}
    ev = {"primary_category": "made_up_slug", "secondary_categories": []}
    out = _normalize_eval(ev, valid)
    assert out["primary_category"] is None
    assert out["novel_category_proposed"] == "made_up_slug"


def test_normalize_drops_invalid_secondaries():
    valid = {"attention"}
    ev = {"primary_category": "attention", "secondary_categories": ["fake", "attention"]}
    out = _normalize_eval(ev, valid)
    assert out["secondary_categories"] == ["attention"]


def test_bot_author_detection():
    bot = _pr(author="dependabot[bot]")
    assert bot.is_bot_author is True
    human = _pr(author="alice")
    assert human.is_bot_author is False


def test_append_uncategorized_creates_file(tmp_path, monkeypatch):
    target = tmp_path / "uncat.json"
    monkeypatch.setattr(classify_mod, "UNCATEGORIZED_PATH", target)
    pr = _pr()
    append_uncategorized("new_slug", pr, "reason")
    data = json.loads(target.read_text())
    assert len(data) == 1
    assert data[0]["proposed_slug"] == "new_slug"
    assert data[0]["pr_number"] == 42


def test_append_uncategorized_appends_to_existing(tmp_path, monkeypatch):
    target = tmp_path / "uncat.json"
    target.write_text(json.dumps([{"proposed_slug": "old"}]))
    monkeypatch.setattr(classify_mod, "UNCATEGORIZED_PATH", target)
    pr = _pr(number=99)
    append_uncategorized("new", pr, "r")
    data = json.loads(target.read_text())
    assert len(data) == 2
    assert data[0]["proposed_slug"] == "old"
    assert data[1]["pr_number"] == 99

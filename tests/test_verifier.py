"""Tests for the deterministic pass-3 verifier and pass-1 prefilter."""

from __future__ import annotations

from radar.firsts import IssueRow, prefilter_match, verify_evaluation


def _issue(title: str, body: str = "") -> IssueRow:
    return IssueRow(
        id=0, repo_id=0, repo_slug="x/y", repo_short="X",
        number=1, title=title, body=body, labels=[], assignee=None,
        state="open", html_url="https://x/y/1",
    )


# ---- pass 1 ----

def test_prefilter_picks_up_blackwell():
    assert prefilter_match(_issue("Optimize FA4 for B200"))


def test_prefilter_picks_up_cutlass():
    assert prefilter_match(_issue("Add CUTLASS epilogue visitor for FP4"))


def test_prefilter_picks_up_deepseek_explicit():
    assert prefilter_match(_issue("DeepSeek-V3.2 sparse MLA backend"))


def test_prefilter_picks_up_mla_only():
    assert prefilter_match(_issue("Wire up MLA decode path"))


def test_prefilter_word_boundary_avoids_substring():
    # 'tma' appears inside 'pragmatic' — word-boundary regex should not match
    assert not prefilter_match(_issue("Pragmatic refactor of the trainer"))


def test_prefilter_rejects_unrelated():
    assert not prefilter_match(_issue("Fix README typo"))


# ---- pass 3 ----

def test_verify_passes_real_quote_b200_explicit():
    iss = _issue("Add B200 support to TMA path", "uses sm_100 MMA")
    ev = {
        "in_scope": True, "scope_bucket": "b200",
        "evidence_quotes": ["B200", "sm_100"],
        "blackwell_intent_signal": "explicit-sm100",
        "difficulty": 3, "why": "explicit",
    }
    ok, why = verify_evaluation(iss, ev)
    assert ok, why


def test_verify_rejects_hallucinated_quote():
    iss = _issue("Random unrelated issue")
    ev = {
        "in_scope": True, "scope_bucket": "deepseek",
        "evidence_quotes": ["this exact phrase is NOT in the issue"],
        "blackwell_intent_signal": None,
        "difficulty": 2, "why": "fabricated",
    }
    ok, why = verify_evaluation(iss, ev)
    assert not ok
    assert why == "hallucinated_quote"


def test_verify_rejects_b200_hopper_only_no_signal():
    iss = _issue("Hopper FP8 GEMM tweak", "sm_90 only, no Blackwell")
    ev = {
        "in_scope": True, "scope_bucket": "b200",
        "evidence_quotes": ["Hopper FP8 GEMM", "sm_90"],
        "blackwell_intent_signal": None,
        "difficulty": 2, "why": "Hopper only",
    }
    ok, why = verify_evaluation(iss, ev)
    assert not ok
    assert why == "b200_no_explicit_no_signal"


def test_verify_accepts_b200_port_stated_signal():
    iss = _issue(
        "Hopper kernel cleanup",
        "preparing this path for Blackwell follow-up; will extend to sm_100 next.",
    )
    ev = {
        "in_scope": True, "scope_bucket": "b200",
        "evidence_quotes": ["preparing this path for Blackwell follow-up"],
        "blackwell_intent_signal": "port-stated",
        "difficulty": 3, "why": "port stated",
    }
    ok, why = verify_evaluation(iss, ev)
    assert ok, why


def test_verify_rejects_cutlass_without_keyword_in_quotes():
    iss = _issue("FP8 GEMM speedup", "Triton-only; CUTLASS not yet")
    ev = {
        "in_scope": True, "scope_bucket": "cutlass_cute",
        "evidence_quotes": ["FP8 GEMM speedup"],
        "blackwell_intent_signal": None,
        "difficulty": 4, "why": "wrong bucket",
    }
    ok, why = verify_evaluation(iss, ev)
    assert not ok
    assert why == "cutlass_cute_keyword_missing"


def test_verify_accepts_deepseek_via_mla_quote():
    iss = _issue("Refactor MLA dispatch", "split MLA prefill from decode")
    ev = {
        "in_scope": True, "scope_bucket": "deepseek",
        "evidence_quotes": ["MLA prefill"],
        "blackwell_intent_signal": None,
        "difficulty": 3, "why": "MLA primitive",
    }
    ok, why = verify_evaluation(iss, ev)
    assert ok, why


def test_verify_passes_through_when_in_scope_false():
    iss = _issue("Anything")
    ev = {
        "in_scope": False, "scope_bucket": None,
        "evidence_quotes": [], "blackwell_intent_signal": None,
        "difficulty": 3, "why": "",
    }
    ok, why = verify_evaluation(iss, ev)
    assert ok and why is None

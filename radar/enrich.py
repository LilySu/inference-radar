"""Enrichment pipeline: keyword buckets, @mentions, org lookup, review signals.

Four sequential passes over existing PR/comment data:

  1. keyword_bucket  — regex on pr.title + pr.body; no API, no LLM; backfills
                       keyword_bucket + keyword_secondary_json columns on prs.
                       Also updates keyword_first_seen per (bucket, repo).

  2. mentions        — regex @username from pr.body + pr_comments; no API, no LLM;
                       populates pr_mentions.

  3. contributor_orgs — GET /users/{login} for each unique author not yet cached;
                        infers org from company field or bio keyword match.

  4. review_signals  — LLM pass: for each open PR with comments but no signal,
                       classify stall_reason + reviewer_stance + newbie_viable.
                       Batched 5 PRs per call, same pattern as classify.py.

Run with: python -m radar.enrich [--skip-orgs] [--skip-signals] [--max-signals N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any

import httpx
import structlog

from radar._llm import complete_json, model_id
from radar.db import RadarDB, dumps, loads, now_iso

log = structlog.get_logger(__name__)

PROMPT_VERSION = "enrich_v1"

# ---------------------------------------------------------------------------
# Keyword bucket definitions
# ---------------------------------------------------------------------------

KEYWORD_BUCKETS: dict[str, re.Pattern[str]] = {
    b: re.compile(p, re.IGNORECASE)
    for b, p in {
        "speculative_decoding": (
            r"\b(speculative[\s_-]?decod|spec[\s_-]?dec|eagle[\d]?|medusa|"
            r"draft[\s_-]?model|lookahead[\s_-]?decod)\b"
        ),
        "moe_routing": (
            r"\b(mixture[\s_-]?of[\s_-]?experts|moe|expert[\s_-]?parallel|"
            r"ep\d+|megamoe|deepgemm|moe[\s_-]?dispatch|moe[\s_-]?routing|"
            r"grouped[\s_-]?gemm|top[\s_-]?k[\s_-]?routing)\b"
        ),
        "attention_kernel": (
            r"\b(flash[\s_-]?att|flashmla|paged[\s_-]?att|mla\b|"
            r"\bmha\b|gqa\b|mqa\b|chunked[\s_-]?prefill|"
            r"sliding[\s_-]?window|ring[\s_-]?att|context[\s_-]?parallel)\b"
        ),
        "kv_cache": (
            r"\b(kv[\s_-]?cache|prefix[\s_-]?cach|radix[\s_-]?cach|"
            r"block[\s_-]?manager|paged[\s_-]?kv|kv[\s_-]?compress|"
            r"kv[\s_-]?transfer|disaggregat)\b"
        ),
        "quantization": (
            r"\b(fp8\b|fp4\b|int4\b|int8\b|gptq|awq\b|w4a|w8a|"
            r"bitsandbytes|bnb\b|aqlm|quant(?:iz|s\b)|block[\s_-]?quant|"
            r"activation[\s_-]?quant|weight[\s_-]?quant|gguf)\b"
        ),
        "hardware_hopper": (
            r"\b(hopper|h100|h200|sm[\s_]?90|nvlink|nvl72|"
            r"gb200\b|b200\b|blackwell|sm[\s_]?100|b300\b|gb300\b)\b"
        ),
        "hardware_ada": (
            r"\b(ada[\s_-]?lovelace|sm[\s_]?89|rtx[\s_-]?40|"
            r"l40\b|l40s\b|consumer[\s_-]?gpu|4080|4090|3090)\b"
        ),
        "hardware_amd": (
            r"\b(rocm|mi300|mi355|mi250|amd\b|hip\b|aiter|"
            r"atom[\s_-]?kernel|triton[\s_-]?amd|composable[\s_-]?kernel)\b"
        ),
        "hardware_other": (
            r"\b(gaudi[\s_-]?[23]?|neuron\b|aws[\s_-]?neuron|tpu\b|"
            r"qualcomm|npu\b|ascend|cann\b|xpu\b|intel[\s_-]?arc|"
            r"sycl\b|oneapi)\b"
        ),
        "lora_serving": (
            r"\b(lora\b|qlora|adapter[\s_-]?serv|peft\b|"
            r"fine[\s_-]?tun|rank[\s_-]?adapt|lora[\s_-]?hot)\b"
        ),
        "distributed": (
            r"\b(tensor[\s_-]?parallel|pipeline[\s_-]?parallel|"
            r"\btp[\s_-]?\d+|\bpp[\s_-]?\d+|data[\s_-]?parallel|"
            r"expert[\s_-]?parallel|sequence[\s_-]?parallel|"
            r"disaggregat|prefill[\s_-]?decode[\s_-]?dis)\b"
        ),
        "model_support": (
            r"\b(deepseek|llama[\s_-]?\d|qwen[\d\s_-]|mistral|gemma[\d\s_-]|"
            r"phi[\s_-]?\d|mixtral|falcon|starcoder|mamba[\d\s_-]?|"
            r"claude|gpt[\s_-]?[234]|command[\s_-]?r|kimi|minimax|"
            r"internlm|baichuan|chatglm|yi[\s_-]?\d)\b"
        ),
        "scheduler": (
            r"\b(schedul|preempt|prioriti|fcfs\b|continuous[\s_-]?batch|"
            r"chunked[\s_-]?prefill|async[\s_-]?output|"
            r"output[\s_-]?proc|request[\s_-]?queue|token[\s_-]?budget)\b"
        ),
    }.items()
}

# Maps substrings (lowercased) in GitHub company/bio → canonical org name.
ORG_KEYWORDS: list[tuple[str, str]] = [
    ("nvidia",          "NVIDIA"),
    ("red hat",         "Red Hat"),
    ("redhat",          "Red Hat"),
    ("ibm",             "IBM"),
    (" amd",            "AMD"),
    ("advanced micro",  "AMD"),
    ("intel",           "Intel"),
    ("google",          "Google"),
    ("deepmind",        "Google"),
    ("anthropic",       "Anthropic"),
    ("meta ",           "Meta"),
    ("facebook",        "Meta"),
    ("microsoft",       "Microsoft"),
    ("amazon",          "Amazon"),
    (" aws",            "Amazon"),
    ("bytedance",       "ByteDance"),
    ("tiktok",          "ByteDance"),
    ("together",        "Together AI"),
    ("fireworks",       "Fireworks AI"),
    ("baseten",         "Baseten"),
    ("anyscale",        "Anyscale"),
    ("lmsys",           "LMSys"),
    ("modal ",          "Modal"),
    (" groq",           "Groq"),
    ("qualcomm",        "Qualcomm"),
    ("hugging face",    "Hugging Face"),
    ("huggingface",     "Hugging Face"),
    ("neural magic",    "Neural Magic"),
    ("neuralmagic",     "Neural Magic"),
    ("cerebras",        "Cerebras"),
    ("sambanova",       "SambaNova"),
    ("baidu",           "Baidu"),
    ("alibaba",         "Alibaba"),
    ("tencent",         "Tencent"),
    ("mistral",         "Mistral AI"),
    ("cohere",          "Cohere"),
    ("x.ai",            "xAI"),
    (" xai",            "xAI"),
    ("deepseek",        "DeepSeek"),
    ("01.ai",           "01.AI"),
    ("zhipu",           "Zhipu AI"),
    ("moonshot",        "Moonshot AI"),
    # Academic labs and inference research groups
    ("lmsys",           "LMSys"),
    ("uc berkeley",     "UC Berkeley"),
    ("berkeley",        "UC Berkeley"),
    ("stanford",        "Stanford"),
    ("mit ",            "MIT"),
    ("carnegie mellon", "CMU"),
    ("cmu",             "CMU"),
    ("university of washington", "UW"),
    ("peking university", "PKU"),
    ("tsinghua",        "Tsinghua"),
    ("shanghai ai",     "SHAI Lab"),
    ("shanghai jiao",   "SJTU"),
    ("zhejiang",        "Zhejiang U"),
    # Inference startups and cloud providers
    ("runpod",          "RunPod"),
    ("lambda labs",     "Lambda Labs"),
    ("lambdalabs",      "Lambda Labs"),
    ("coreweave",       "CoreWeave"),
    ("vast.ai",         "Vast.ai"),
    ("scale ai",        "Scale AI"),
    ("scaleai",         "Scale AI"),
    ("lepton",          "Lepton AI"),
    ("perplexity",      "Perplexity"),
    ("inflection",      "Inflection AI"),
    ("reka",            "Reka AI"),
    ("lightllm",        "LightLLM"),
    ("sagemaker",       "Amazon"),
    ("nvidia research", "NVIDIA"),
    ("nvidia labs",     "NVIDIA"),
]

# Regex to extract GitHub @mentions from text
_MENTION_RE = re.compile(r"@([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?)")

# Bots and AI-review accounts to exclude from mention extraction
_BOT_LOGINS = frozenset({
    "dependabot", "dependabot[bot]", "pre-commit-ci", "pre-commit-ci[bot]",
    "github-actions", "github-actions[bot]", "renovate", "renovate[bot]",
    "codecov", "codecov[bot]", "copilot", "copilot[bot]",
    # AI code-review bots
    "coderabbitai", "coderabbitai[bot]", "sourcery-ai", "deepsource-autofix",
    "greptile", "sweep-ai", "ellipsis-dev",
    # Corporate oncall/team accounts (not individual engineers)
    "mcore-oncall", "nvidia", "anthropic", "meta-llama", "amd",
    "google", "microsoft", "amazon-chime-sdk", "aws", "flashinfer",
    "huggingface", "openai", "pytorch", "triton-lang",
    # Common false-positive tokens that regex picks up from prose / email refs
    "gmail", "users", "pytest", "torch", "python", "linux",
    "example", "localhost", "noreply", "mention", "here",
    # Python decorators / stdlib that get picked up from code blocks
    "cache", "property", "staticmethod", "classmethod", "abstractmethod",
    "dataclass", "override", "deprecated",
})

# Review signal LLM schema
_SIGNAL_SCHEMA = {
    "type": "object",
    "required": ["signals"],
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "pr_index", "stall_reason", "reviewer_stance",
                    "newbie_viable", "one_line_reason",
                ],
                "properties": {
                    "pr_index":       {"type": "integer"},
                    "stall_reason":   {
                        "type": "string",
                        "enum": [
                            "needs_tests", "needs_rebase", "ci_failure",
                            "needs_benchmarks", "approach_disagreement",
                            "needs_rfc_first", "duplicate_internal",
                            "not_on_roadmap", "no_reviewer_capacity",
                            "author_unresponsive", "style_nits_only",
                            "explicitly_welcomed", "in_active_review",
                            "merged_or_closed",
                        ],
                    },
                    "reviewer_stance": {
                        "type": "string",
                        "enum": [
                            "approved", "changes_requested",
                            "no_response", "rejected", "wants_rfc",
                        ],
                    },
                    "newbie_viable":  {"type": "boolean"},
                    "one_line_reason": {"type": "string"},
                },
            },
        },
    },
}

_SIGNAL_SYSTEM = """\
You classify GitHub PR stall patterns for an open-source LLM inference project (vLLM, SGLang, etc.).

Given a PR title and 1-3 comment excerpts, assign:

stall_reason — why the PR is stalled or what state it is in:
  needs_tests          — reviewer asked for test coverage
  needs_rebase         — merge conflict or "please rebase"
  ci_failure           — CI is red, tests are failing
  needs_benchmarks     — reviewer asked for perf numbers
  approach_disagreement — reviewer wants a fundamentally different design
  needs_rfc_first      — reviewer says design needs a proposal/RFC before code
  duplicate_internal   — corporate team is already working on this internally
  not_on_roadmap       — explicitly out of scope for the project
  no_reviewer_capacity — no one has reviewed yet; not a technical block
  author_unresponsive  — reviewer gave feedback, author went silent
  style_nits_only      — only minor style/format comments remain
  explicitly_welcomed  — maintainer said "PRs welcome" or "help wanted"
  in_active_review     — currently being actively reviewed, not stalled
  merged_or_closed     — already resolved

reviewer_stance: approved | changes_requested | no_response | rejected | wants_rfc

newbie_viable: true if an outsider newcomer could realistically address the blocker
in under 1 week with public information (no insider access needed).
"""


# ---------------------------------------------------------------------------
# Pass 1: keyword bucket assignment
# ---------------------------------------------------------------------------

def assign_keyword_bucket(text: str) -> tuple[str | None, list[str]]:
    """Return (primary_bucket, secondary_buckets_list) for text."""
    matches: list[tuple[str, int]] = []
    for bucket, pattern in KEYWORD_BUCKETS.items():
        m = pattern.search(text)
        if m:
            matches.append((bucket, m.start()))
    if not matches:
        return None, []
    matches.sort(key=lambda x: x[1])
    primary = matches[0][0]
    secondary = [b for b, _ in matches[1:] if b != primary]
    return primary, secondary


async def _pass_keyword_buckets(db: RadarDB) -> None:
    # Single query: everything needed in one shot — title, body, repo_id, created_at, raw_json
    async with db.conn.execute(
        """SELECT id, repo_id, title, body, created_at, raw_json
           FROM prs
           WHERE keyword_bucket IS NULL
           ORDER BY updated_at DESC LIMIT 2000"""
    ) as cur:
        rows = await cur.fetchall()

    log.info("keyword_bucket_candidates", n=len(rows))

    bucket_first: dict[tuple[str, int], tuple[int, str]] = {}  # (bucket, repo_id) → (pr_id, created_at)

    for row in rows:
        pr_id = int(row["id"])
        repo_id = int(row["repo_id"])
        raw = loads(row["raw_json"]) or {}
        author_login: str | None = (raw.get("user") or {}).get("login")
        title = str(row["title"] or "")
        body = str(row["body"] or "")
        full_text = f"{title} {body}"

        primary, secondary = assign_keyword_bucket(full_text)
        await db.update_pr_keyword_bucket(pr_id, primary, secondary, author_login)

        # Only record keyword_first_seen when the bucket matches in the PR
        # TITLE (not just the body) — title matches are far less noisy and
        # represent genuine intent rather than incidental mention.
        if primary:
            title_primary, _ = assign_keyword_bucket(title)
            if title_primary == primary:  # confirmed in title
                key = (primary, repo_id)
                created = str(row["created_at"] or now_iso())
                if key not in bucket_first or created < bucket_first[key][1]:
                    bucket_first[key] = (pr_id, created)

    await db.conn.commit()

    for (bucket, repo_id), (first_pr_id, first_seen) in bucket_first.items():
        await db.upsert_keyword_first_seen(
            bucket=bucket, repo_id=repo_id,
            first_pr_id=first_pr_id, first_seen=first_seen,
        )

    log.info("keyword_bucket_done", processed=len(rows))


# ---------------------------------------------------------------------------
# Pass 2: @mention extraction
# ---------------------------------------------------------------------------

def extract_mentions(text: str) -> list[str]:
    """Return unique GitHub logins @mentioned in text, bots and noise excluded."""
    found = {m.lower() for m in _MENTION_RE.findall(text)}
    result = []
    for m in found:
        if m in _BOT_LOGINS:
            continue
        if m.isdigit():                  # @1, @32 — issue/PR number refs
            continue
        if len(m) < 3:                   # single/double char noise
            continue
        if m[0] == "v" and m[1:].replace(".", "").isdigit():  # @v1.0.0
            continue
        result.append(m)
    return result


async def _pass_mentions(db: RadarDB) -> None:
    # PRs whose body hasn't been scanned for mentions yet
    async with db.conn.execute(
        """SELECT p.id, p.body, p.number FROM prs p
           WHERE NOT EXISTS (
               SELECT 1 FROM pr_mentions pm WHERE pm.pr_id = p.id AND pm.source='body'
           )
           AND p.body IS NOT NULL
           ORDER BY p.updated_at DESC LIMIT 3000"""
    ) as cur:
        pr_rows = await cur.fetchall()

    mention_count = 0
    for row in pr_rows:
        pr_id = int(row["id"])
        for login in extract_mentions(str(row["body"] or "")):
            await db.upsert_pr_mention(pr_id=pr_id, mentioned_login=login, source="body")
            mention_count += 1

    # Comments not yet processed for mentions
    async with db.conn.execute(
        """SELECT pc.id AS comment_db_id, pc.pr_id, pc.body FROM pr_comments pc
           WHERE NOT EXISTS (
               SELECT 1 FROM pr_mentions pm
               WHERE pm.pr_id = pc.pr_id AND pm.source='comment'
               LIMIT 1
           )
           AND pc.body IS NOT NULL
           LIMIT 5000"""
    ) as cur:
        comment_rows = await cur.fetchall()

    for row in comment_rows:
        pr_id = int(row["pr_id"])
        for login in extract_mentions(str(row["body"] or "")):
            await db.upsert_pr_mention(pr_id=pr_id, mentioned_login=login, source="comment")
            mention_count += 1

    await db.conn.commit()
    log.info("mentions_done",
             prs_scanned=len(pr_rows),
             comments_scanned=len(comment_rows),
             mentions_stored=mention_count)


# ---------------------------------------------------------------------------
# Pass 3: contributor org lookup
# ---------------------------------------------------------------------------

def infer_org(company_raw: str | None, bio: str | None) -> tuple[str | None, str]:
    """Infer canonical org from GitHub profile fields. Returns (org, source)."""
    text = " ".join(filter(None, [
        (company_raw or "").lower(),
        (bio or "").lower(),
    ]))
    if not text.strip():
        return None, "unknown"
    for keyword, canonical in ORG_KEYWORDS:
        if keyword in text:
            source = "github_company" if keyword in (company_raw or "").lower() else "bio_keyword"
            return canonical, source
    return None, "unknown"


async def _pass_contributor_orgs(db: RadarDB, max_lookups: int = 200) -> None:
    logins = await db.fetch_logins_needing_org_lookup(max_lookups)
    log.info("org_lookup_candidates", n=len(logins))
    if not logins:
        return

    token = os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "inference-radar/0.1",
    }

    async with httpx.AsyncClient(timeout=20, headers=headers) as cli:
        for login in logins:
            try:
                r = await cli.get(f"https://api.github.com/users/{login}")
                if r.status_code == 404:
                    await db.upsert_contributor_org(
                        login=login, org=None, org_source="not_found",
                        company_raw=None, bio_snippet=None,
                    )
                    continue
                if r.status_code != 200:
                    log.warning("org_lookup_error", login=login, status=r.status_code)
                    continue
                data = r.json()
                company_raw = (data.get("company") or "").strip().lstrip("@")
                bio = (data.get("bio") or "")[:300]
                org, source = infer_org(company_raw or None, bio or None)
                await db.upsert_contributor_org(
                    login=login, org=org, org_source=source,
                    company_raw=company_raw or None,
                    bio_snippet=bio[:200] or None,
                )
                log.info("org_resolved", login=login, org=org, source=source)
            except Exception as exc:
                log.warning("org_lookup_failed", login=login, err=str(exc))

    await db.conn.commit()
    log.info("org_lookup_done", processed=len(logins))


# ---------------------------------------------------------------------------
# Pass 4: review signal classification (LLM)
# ---------------------------------------------------------------------------

def _build_signal_user_prompt(batch: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for i, item in enumerate(batch):
        parts.append(f"--- PR {i} ---")
        parts.append(f"Title: {item['title']}")
        parts.append(f"State: {item['state']}")
        for j, c in enumerate(item["comments"][:3]):
            author = c.get("author_login") or "unknown"
            src = c.get("source", "")
            snippet = (c.get("body") or "")[:400]
            parts.append(f"Comment {j+1} [{src}] by @{author}: {snippet}")
        parts.append("")
    return "\n".join(parts)


async def _classify_signal_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = _build_signal_user_prompt(batch)
    result = await complete_json(_SIGNAL_SYSTEM, user, _SIGNAL_SCHEMA)
    return result.get("signals", [])


async def _pass_review_signals(db: RadarDB, max_prs: int = 20) -> None:
    prs = await db.fetch_prs_needing_review_signal(max_prs)
    log.info("review_signal_candidates", n=len(prs))
    if not prs:
        return

    model = model_id()
    classified = 0

    for i in range(0, len(prs), 5):
        chunk = prs[i : i + 5]
        batch: list[dict[str, Any]] = []
        for pr in chunk:
            comments = await db.fetch_pr_comments(int(pr["id"]))
            # Select up to 3 comments: first non-trivial, last, and any review
            selected: list[dict[str, Any]] = []
            for c in comments:
                body = (c["body"] or "").strip()
                if len(body) > 20 and len(selected) < 3:
                    selected.append(dict(c))
            batch.append({
                "title": pr["title"],
                "state": pr["state"],
                "comments": selected,
            })

        try:
            signals = await _classify_signal_batch(batch)
        except Exception as exc:
            log.warning("review_signal_batch_failed", err=str(exc))
            continue

        for sig in signals:
            idx = sig.get("pr_index", -1)
            if not (0 <= idx < len(chunk)):
                continue
            pr = chunk[idx]
            await db.upsert_pr_review_signal(
                pr_id=int(pr["id"]),
                stall_reason=sig.get("stall_reason", "no_reviewer_capacity"),
                reviewer_stance=sig.get("reviewer_stance", "no_response"),
                newbie_viable=bool(sig.get("newbie_viable", False)),
                one_line_reason=(sig.get("one_line_reason") or "")[:200],
                model=model,
                prompt_version=PROMPT_VERSION,
            )
            classified += 1
            log.info("review_signal_classified",
                     pr=pr["id"], stall=sig.get("stall_reason"),
                     newbie=sig.get("newbie_viable"))

    log.info("review_signal_done", classified=classified)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _pass_backfill_merged_at(db: RadarDB) -> None:
    """Backfill merged_at for closed PRs where raw_json has the merged timestamp.

    PRs ingested via /issues endpoint have null merged_at even when merged,
    because /issues doesn't return that field. The /pulls endpoint does — and
    raw_json for pull objects from /pulls has a top-level merged_at. This pass
    recovers it from raw_json for any closed PR where the column is still NULL.
    """
    async with db.conn.execute(
        """SELECT id, raw_json FROM prs
           WHERE state = 'closed' AND merged_at IS NULL AND raw_json IS NOT NULL
           LIMIT 5000"""
    ) as cur:
        rows = await cur.fetchall()

    updated = 0
    for row in rows:
        raw = loads(row["raw_json"]) or {}
        merged_at = raw.get("merged_at")
        if not merged_at:
            # Also try nested pull_request stub
            merged_at = (raw.get("pull_request") or {}).get("merged_at")
        if merged_at:
            await db.conn.execute(
                "UPDATE prs SET merged_at=? WHERE id=?", (merged_at, row["id"])
            )
            updated += 1

    if updated:
        await db.conn.commit()
    log.info("backfill_merged_at_done", updated=updated, checked=len(rows))


async def run(
    skip_orgs: bool = False,
    skip_signals: bool = False,
    max_signals: int = 20,
    max_org_lookups: int = 200,
    db_path: str | None = None,
) -> None:
    db_path = db_path or os.environ.get("RADAR_DB", "data/radar.db")
    async with RadarDB(db_path) as db:
        log.info("enrich_start")

        await _pass_backfill_merged_at(db)
        await _pass_keyword_buckets(db)
        await _pass_mentions(db)

        if not skip_orgs:
            await _pass_contributor_orgs(db, max_lookups=max_org_lookups)

        if not skip_signals:
            await _pass_review_signals(db, max_prs=max_signals)

        log.info("enrich_done")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich PR data for analytics")
    parser.add_argument("--skip-orgs", action="store_true",
                        help="Skip contributor org lookup (saves GitHub API calls)")
    parser.add_argument("--skip-signals", action="store_true",
                        help="Skip LLM review signal classification")
    parser.add_argument("--max-signals", type=int, default=20,
                        help="Max PRs to classify review signals for (default 20)")
    parser.add_argument("--max-org-lookups", type=int, default=200,
                        help="Max contributor org lookups per run (default 200)")
    args = parser.parse_args()
    asyncio.run(run(
        skip_orgs=args.skip_orgs,
        skip_signals=args.skip_signals,
        max_signals=args.max_signals,
        max_org_lookups=args.max_org_lookups,
    ))


if __name__ == "__main__":
    main()

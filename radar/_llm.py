"""LLM provider router with three backends: groq | anthropic | claude_code.

Single entry point: `await complete_json(system, user, schema)` returns parsed JSON.

Selection is via the RADAR_LLM env var (default: groq).
- groq: Llama 3.3 70B Versatile via OpenAI-compatible endpoint. GROQ_API_KEY.
- anthropic: Claude Sonnet via Anthropic API. ANTHROPIC_API_KEY. Tool-use for JSON.
- claude_code: subprocess `claude -p ... --output-format json`. No API key.

Schema enforcement: groq uses response_format={"type":"json_object"} + we instruct
the schema in the prompt. anthropic uses tool-use with the provided JSON schema.
claude_code asks for raw JSON and we parse stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_DEFAULT = "llama-3.3-70b-versatile"
GROQ_BACKOFF = [2, 4, 8, 16]


def _groq_model() -> str:
    return os.environ.get("GROQ_MODEL", GROQ_MODEL_DEFAULT)

ANTHROPIC_MODEL_DEFAULT = "claude-sonnet-4-6"


def selected_provider() -> str:
    return os.environ.get("RADAR_LLM", "groq").strip().lower()


def model_id() -> str:
    """Stable string written to issue_evaluations.model for diffability."""
    p = selected_provider()
    if p == "groq":
        return f"groq:{_groq_model()}"
    if p == "anthropic":
        return f"anthropic:{os.environ.get('ANTHROPIC_MODEL', ANTHROPIC_MODEL_DEFAULT)}"
    if p == "claude_code":
        return "claude_code"
    return f"unknown:{p}"


async def complete_json(system: str, user: str, schema: dict[str, Any]) -> Any:
    """Run the prompt, return parsed JSON.

    `schema` is a JSON Schema dict; used by anthropic tool-use and inserted into
    the groq prompt as text (groq does not enforce schemas server-side).
    """
    p = selected_provider()
    if p == "groq":
        return await _complete_groq(system, user, schema)
    if p == "anthropic":
        return await _complete_anthropic(system, user, schema)
    if p == "claude_code":
        return await _complete_claude_code(system, user, schema)
    raise ValueError(f"unknown RADAR_LLM={p!r} (expected groq|anthropic|claude_code)")


# ---------------- groq ----------------

async def _complete_groq(system: str, user: str, schema: dict[str, Any]) -> Any:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY env var required for RADAR_LLM=groq")

    payload = {
        "model": _groq_model(),
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system + _schema_hint(schema)},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as cli:
        for attempt, delay in enumerate([0, *GROQ_BACKOFF]):
            if delay:
                await asyncio.sleep(delay)
            r = await cli.post(GROQ_URL, headers=headers, json=payload)
            if r.status_code == 429:
                log.warning("groq_rate_limited", attempt=attempt, retry_in=delay)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"groq {r.status_code}: {r.text[:300]}")
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        raise RuntimeError("groq: rate-limited after backoff exhausted")


def _schema_hint(schema: dict[str, Any]) -> str:
    return (
        "\n\nReturn ONLY a single JSON object that conforms to this JSON Schema:\n"
        + json.dumps(schema, indent=2)
        + "\nNo prose, no markdown code fences."
    )


# ---------------- anthropic ----------------

async def _complete_anthropic(system: str, user: str, schema: dict[str, Any]) -> Any:
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK not installed. uv add inference-radar[anthropic]"
        ) from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY required for RADAR_LLM=anthropic")
    model = os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL_DEFAULT)
    cli = anthropic.AsyncAnthropic(api_key=api_key)
    tool_name = "emit_result"
    tool = {
        "name": tool_name,
        "description": "Emit the structured evaluation.",
        "input_schema": schema,
    }
    max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16384"))
    msg = await cli.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    raise RuntimeError(f"anthropic: no tool_use block in response: {msg}")


# ---------------- claude_code ----------------

async def _complete_claude_code(system: str, user: str, schema: dict[str, Any]) -> Any:
    """Shell out to Claude Code in headless mode. No API key.

    `claude -p PROMPT --output-format json` returns a JSON envelope with `.result`
    containing the assistant's final text. We ask for strict JSON in the prompt
    and parse it out of `.result`.
    """
    prompt = (
        f"{system}\n\n---\n\n{user}\n\n"
        f"Respond with ONLY a single JSON object matching this schema "
        f"(no prose, no fences):\n{json.dumps(schema, indent=2)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--output-format", "json",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    try:
        envelope = json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"claude rc={proc.returncode}, non-JSON stdout: {stdout[:200]!r}; "
            f"stderr={stderr[:200]!r}"
        ) from e
    if envelope.get("is_error"):
        raise RuntimeError(
            f"claude error rc={proc.returncode}: "
            f"{envelope.get('result') or envelope.get('subtype') or 'unknown'}"
        )
    text = envelope.get("result", "")
    text = _strip_fences(text.strip())
    return json.loads(text)


def _strip_fences(s: str) -> str:
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return s

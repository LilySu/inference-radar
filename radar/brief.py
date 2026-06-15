"""Daily brief pipeline — classified PRs + recent firsts → script → mp4 → YouTube.

Plan 3 stack ($0/mo):
- LLM script gen via the _llm router (default groq).
- Marp-CLI for slide rendering (Markdown → PNG).
- Piper TTS for narration (local neural, no API key).
- ffmpeg for video assembly.
- YouTube Data API v3 for upload (only step needing OAuth).

External binaries expected on PATH: `marp`, `piper`, `ffmpeg`. The GitHub
Actions workflow installs all three. Locally:
  npm i -g @marp-team/marp-cli
  pip install piper-tts && piper-download voices/en_US-lessac-medium.onnx
  apt-get install ffmpeg

Run:
  uv run python -m radar.brief                       # full pipeline + upload
  uv run python -m radar.brief --no-upload           # local, stop after mp4
  uv run python -m radar.brief --script-only         # write script.json, exit
  uv run python -m radar.brief --date 2026-05-12     # back-date a brief
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml
from jinja2 import Template

from radar._llm import complete_json, selected_provider
from radar.db import RadarDB, loads

log = structlog.get_logger(__name__)

PROMPT_VERSION = "v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "brief_v1.md"
SLIDE_TEMPLATE = Path(__file__).resolve().parent / "templates" / "slide.md.j2"
REPOS_SEED = Path(__file__).resolve().parent.parent / "seed" / "repos.yml"
DEFAULT_DB_PATH = os.environ.get("RADAR_DB", "data/radar.db")

OUT_ROOT = Path(os.environ.get("RADAR_OUT", "out"))
PIPER_MODEL = os.environ.get(
    "PIPER_MODEL", "/usr/share/piper-voices/en_US-lessac-medium.onnx",
)


# ---------------- data shapes ----------------

@dataclass
class PRItem:
    repo: str
    repo_short: str
    number: int
    title: str
    html_url: str
    primary_category: str | None
    secondary_categories: list[str]
    one_line_summary: str
    technical_summary: str
    perf_numbers: list[dict[str, Any]]
    bot_or_chore: bool


@dataclass
class FirstItem:
    repo: str
    repo_short: str
    number: int
    title: str
    html_url: str
    bucket: str
    difficulty: int
    why: str


@dataclass
class BriefInput:
    briefing_date: str
    repo_scope: list[str]
    prs: list[PRItem]
    firsts: list[FirstItem]


# ---------------- DB pulls ----------------

PR_SINCE_SQL = """
SELECT p.id, p.number, p.title, p.html_url, r.slug AS repo, r.name AS repo_short,
       c.primary_category, c.secondary_categories_json, c.one_line_summary,
       c.technical_summary, c.perf_numbers_json, c.bot_or_chore
  FROM prs p
  JOIN repos r ON r.id = p.repo_id
  JOIN pr_classifications c ON c.pr_id = p.id
 WHERE (p.merged_at >= ? OR p.updated_at >= ?)
   AND c.prompt_version = ?
   AND c.id = (
       SELECT id FROM pr_classifications c2
        WHERE c2.pr_id = p.id ORDER BY c2.classified_at DESC LIMIT 1
   )
 ORDER BY p.merged_at DESC, p.updated_at DESC
"""

FIRSTS_SINCE_SQL = """
SELECT i.number, i.title, i.html_url, r.slug AS repo, r.name AS repo_short,
       e.scope_bucket, e.difficulty, e.why
  FROM issue_evaluations e
  JOIN issues i ON i.id = e.issue_id
  JOIN repos  r ON r.id = i.repo_id
 WHERE e.in_scope = 1
   AND e.evaluated_at >= ?
   AND i.state = 'open'
   AND (i.assignee IS NULL OR i.assignee = '')
 ORDER BY e.evaluated_at DESC
 LIMIT 8
"""


async def fetch_brief_input(db: RadarDB, briefing_date: str, classify_pv: str) -> BriefInput:
    repos = yaml.safe_load(REPOS_SEED.read_text())
    repo_scope = [r["slug"] for r in repos]

    since_dt = datetime.fromisoformat(briefing_date) - timedelta(days=1)
    since = since_dt.isoformat()

    prs: list[PRItem] = []
    async with db.conn.execute(PR_SINCE_SQL, (since, since, classify_pv)) as cur:
        async for r in cur:
            prs.append(
                PRItem(
                    repo=r["repo"],
                    repo_short=r["repo_short"],
                    number=int(r["number"]),
                    title=r["title"] or "",
                    html_url=r["html_url"] or "",
                    primary_category=r["primary_category"],
                    secondary_categories=loads(r["secondary_categories_json"]) or [],
                    one_line_summary=r["one_line_summary"] or "",
                    technical_summary=r["technical_summary"] or "",
                    perf_numbers=loads(r["perf_numbers_json"]) or [],
                    bot_or_chore=bool(r["bot_or_chore"]),
                )
            )

    firsts: list[FirstItem] = []
    async with db.conn.execute(FIRSTS_SINCE_SQL, (since,)) as cur:
        async for r in cur:
            firsts.append(
                FirstItem(
                    repo=r["repo"],
                    repo_short=r["repo_short"],
                    number=int(r["number"]),
                    title=r["title"] or "",
                    html_url=r["html_url"] or "",
                    bucket=r["scope_bucket"] or "",
                    difficulty=int(r["difficulty"] or 5),
                    why=r["why"] or "",
                )
            )

    return BriefInput(
        briefing_date=briefing_date,
        repo_scope=repo_scope,
        prs=prs,
        firsts=firsts,
    )


# ---------------- script generation ----------------

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "subhead": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "narration": {"type": "string"},
                    "duration_s": {"type": "number"},
                },
                "required": ["heading", "subhead", "bullets", "narration"],
            },
        },
        "outro": {"type": "string"},
    },
    "required": ["title", "intro", "slides", "outro"],
}


def _format_input_for_prompt(bi: BriefInput) -> str:
    lines = [
        f"## Briefing date: {bi.briefing_date}",
        f"## Repos in scope: {', '.join(bi.repo_scope)}",
        "",
    ]
    real_prs = [p for p in bi.prs if not p.bot_or_chore]
    chore_prs = [p for p in bi.prs if p.bot_or_chore]
    lines.append(
        f"## Classified PRs since yesterday "
        f"({len(real_prs)} feature, {len(chore_prs)} chore)"
    )
    lines.append("")
    for p in real_prs[:80]:
        cats = p.primary_category or "uncategorized"
        if p.secondary_categories:
            cats += " (+ " + ", ".join(p.secondary_categories) + ")"
        perf_str = ""
        if p.perf_numbers:
            perf_str = " · perf: " + "; ".join(
                f"{pn.get('metric','')} {pn.get('baseline','')}→{pn.get('new','')}"
                + (f" ({pn.get('delta_pct')}%)" if pn.get("delta_pct") is not None else "")
                for pn in p.perf_numbers[:3]
            )
        lines.append(
            f"- [{p.repo_short}#{p.number}] cat={cats}{perf_str}\n"
            f"  one-line: {p.one_line_summary}\n"
            f"  technical: {p.technical_summary[:400]}"
        )
    if chore_prs:
        lines.append("")
        lines.append("### Chore/bot PRs (collapse unless user-visible):")
        for p in chore_prs[:20]:
            lines.append(f"- [{p.repo_short}#{p.number}] {p.title[:80]}")
    if bi.firsts:
        lines.append("")
        lines.append("## Open first-issue picks (for the 'Picks' slide):")
        for f in bi.firsts:
            lines.append(
                f"- D{f.difficulty} · {f.repo_short} #{f.number} · {f.bucket} · "
                f"{f.title[:80]} — {f.why[:140]}"
            )
    return "\n".join(lines)


async def generate_script(bi: BriefInput) -> dict[str, Any]:
    system = PROMPT_PATH.read_text()
    user = _format_input_for_prompt(bi)
    return await complete_json(system, user, SCRIPT_SCHEMA)


# ---------------- rendering ----------------

def have_binary(name: str) -> bool:
    return shutil.which(name) is not None


def render_slides_markdown(script: dict[str, Any], briefing_date: str) -> str:
    tpl = Template(SLIDE_TEMPLATE.read_text())
    return tpl.render(
        title_eyebrow=f"INFERENCE RADAR · {briefing_date}",
        title=script.get("title", f"Inference Radar — {briefing_date}"),
        intro=script.get("intro", ""),
        slides=script.get("slides", []),
        outro_title="That's the radar.",
        outro=script.get("outro", ""),
    )


def render_slides_to_png(slides_md_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if not have_binary("marp"):
        log.warning("marp_missing — skipping slide PNG render")
        return []
    cmd = [
        "marp", str(slides_md_path),
        "--images", "png",
        "--allow-local-files",
        "-o", str(out_dir / "slide.png"),
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("slide.*.png"))


def synthesize_narration(script: dict[str, Any], out_dir: Path) -> list[Path]:
    """Render WAVs for title + each body slide + outro, in that order.

    Marp generates N+2 PNGs (title + N content + outro); audio counts must match
    so assemble_video doesn't drop the bookend slides.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if not have_binary("piper"):
        log.warning("piper_missing — skipping TTS synthesis")
        return []
    if not Path(PIPER_MODEL).exists():
        log.warning("piper_model_missing", path=PIPER_MODEL)
        return []
    title_narr = (script.get("title") or "") + ". " + (script.get("intro") or "")
    outro_narr = (script.get("outro") or "").strip() or "That's the radar."
    slides = script.get("slides") or []
    texts = [title_narr.strip(), *(s.get("narration", "").strip() for s in slides), outro_narr]
    paths: list[Path] = []
    for i, text in enumerate(texts):
        wav = out_dir / f"slide_{i:03d}.wav"
        if not text:
            continue
        subprocess.run(
            ["piper", "--model", PIPER_MODEL, "--output_file", str(wav)],
            input=text.encode("utf-8"), check=True,
        )
        paths.append(wav)
    return paths


def assemble_video(
    slide_pngs: list[Path], audio_wavs: list[Path], out_mp4: Path,
) -> int | None:
    """Build mp4 from per-slide (png + wav). Returns duration in seconds."""
    if not have_binary("ffmpeg"):
        log.warning("ffmpeg_missing — skipping video assembly")
        return None
    if not slide_pngs or not audio_wavs:
        log.warning("nothing_to_assemble", slides=len(slide_pngs), audios=len(audio_wavs))
        return None

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    work = out_mp4.parent / "_clips"
    work.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    total = 0
    for i, (png, wav) in enumerate(zip(slide_pngs, audio_wavs, strict=False)):
        clip = work / f"clip_{i:03d}.mp4"
        # Probe audio duration so the still-image stays as long as the narration.
        probe = subprocess.run(
            ["ffprobe", "-i", str(wav), "-show_entries", "format=duration",
             "-v", "quiet", "-of", "csv=p=0"],
            check=True, capture_output=True, text=True,
        )
        try:
            dur = float(probe.stdout.strip())
        except ValueError:
            dur = 12.0
        total += int(dur + 1)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(png),
                "-i", str(wav),
                "-c:v", "libx264", "-tune", "stillimage",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=1920:1080",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-t", f"{dur:.2f}",
                str(clip),
            ],
            check=True, capture_output=True,
        )
        clips.append(clip)

    list_file = work / "concat.txt"
    # ffmpeg's concat demuxer resolves entries relative to the list file's
    # directory; clips are written to that same dir, so use basenames.
    list_file.write_text("\n".join(f"file '{c.name}'" for c in clips))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(out_mp4)],
        check=True, capture_output=True,
    )
    return total


def upload_to_youtube(
    mp4_path: Path, *, title: str, description: str, briefing_date: str,
) -> str | None:
    """Upload to YouTube as unlisted. Returns the watch URL or None on failure.

    Requires env: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN.
    Lazy-imports google API libs so the rest of the pipeline runs without them.
    """
    cid = os.environ.get("YOUTUBE_CLIENT_ID")
    csec = os.environ.get("YOUTUBE_CLIENT_SECRET")
    rt = os.environ.get("YOUTUBE_REFRESH_TOKEN")
    if not (cid and csec and rt):
        log.warning("youtube_oauth_missing — skipping upload")
        return None
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError:
        log.warning("google_api_libs_missing — pip install google-api-python-client google-auth")
        return None

    creds = Credentials(
        token=None, refresh_token=rt,
        client_id=cid, client_secret=csec,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "28",  # Science & Technology
            "tags": ["vllm", "sglang", "blackwell", "inference"],
        },
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(mp4_path), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = req.next_chunk()
    video_id = response.get("id")
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


# ---------------- orchestrator ----------------

def _description_text(bi: BriefInput, script: dict[str, Any]) -> str:
    lines = [
        script.get("intro", ""),
        "",
        "Covered repos: " + ", ".join(bi.repo_scope),
        "",
        "Inference Radar is generated nightly: ingest → classify → script → render →",
        "narrate → upload.",
        "Source: https://github.com/LilySu/inference-radar",
    ]
    return "\n".join(lines)


async def run(
    db: RadarDB,
    *,
    briefing_date: str,
    no_upload: bool,
    script_only: bool,
    classify_pv: str,
) -> None:
    bi = await fetch_brief_input(db, briefing_date, classify_pv)
    log.info("brief_input", prs=len(bi.prs), firsts=len(bi.firsts), date=briefing_date)
    if not bi.prs and not bi.firsts:
        log.warning("no_input — nothing to brief")
        return

    out_dir = OUT_ROOT / briefing_date
    out_dir.mkdir(parents=True, exist_ok=True)

    script_path = out_dir / "script.json"
    if script_path.exists():
        log.info("script_cached", path=str(script_path))
        script = json.loads(script_path.read_text())
    else:
        script = await generate_script(bi)
        script_path.write_text(json.dumps(script, indent=2))
        log.info("script_written", path=str(script_path), slides=len(script.get("slides", [])))

    # Persist the script row so the site picks it up even if rendering fails.
    await db.upsert_briefing(
        briefing_date=briefing_date,
        repo_scope=bi.repo_scope,
        script=script,
        video_path=None, video_url=None, duration_s=None,
    )

    if script_only:
        print(f"script-only: {script_path}")
        return

    # 2. Render Marp markdown → PNGs
    slides_md = render_slides_markdown(script, briefing_date)
    slides_md_path = out_dir / "slides.md"
    slides_md_path.write_text(slides_md)

    slide_pngs = render_slides_to_png(slides_md_path, out_dir / "slides")
    log.info("slides_rendered", n=len(slide_pngs))

    # 3. Synthesize narration with Piper (title + body slides + outro)
    audio_wavs = synthesize_narration(script, out_dir / "audio")
    log.info("narration_done", n=len(audio_wavs))

    # 4. Assemble mp4
    mp4_path = out_dir / "brief.mp4"
    duration_s = assemble_video(slide_pngs, audio_wavs, mp4_path)
    if duration_s:
        log.info("video_assembled", path=str(mp4_path), duration_s=duration_s)
        await db.upsert_briefing(
            briefing_date=briefing_date,
            repo_scope=bi.repo_scope,
            script=script,
            video_path=str(mp4_path),
            video_url=None,
            duration_s=duration_s,
        )

    if no_upload or not mp4_path.exists():
        return

    # 5. Upload to YouTube
    url = upload_to_youtube(
        mp4_path,
        title=script.get("title", f"Inference Radar — {briefing_date}"),
        description=_description_text(bi, script),
        briefing_date=briefing_date,
    )
    if url:
        await db.upsert_briefing(
            briefing_date=briefing_date,
            repo_scope=bi.repo_scope,
            script=script,
            video_path=str(mp4_path),
            video_url=url,
            duration_s=duration_s,
        )
        print(f"uploaded: {url}")
    else:
        print(f"local mp4 (no upload): {mp4_path}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inference Radar — daily brief")
    p.add_argument("--date", default=None,
                   help="Briefing date YYYY-MM-DD (default: today UTC).")
    p.add_argument("--no-upload", action="store_true",
                   help="Build mp4 but skip YouTube upload.")
    p.add_argument("--script-only", action="store_true",
                   help="Generate script.json and exit (no rendering, no TTS, no ffmpeg).")
    p.add_argument("--classify-pv", default="v1",
                   help="prompt_version of classifications to read (default v1).")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    structlog.configure(processors=[structlog.dev.ConsoleRenderer()])
    briefing_date = args.date or date.today().isoformat()  # noqa: DTZ011
    if args.date:
        # Validate
        datetime.fromisoformat(args.date)
    else:
        briefing_date = datetime.now(UTC).date().isoformat()
    log.info("config", provider=selected_provider(), date=briefing_date)
    async with RadarDB(DEFAULT_DB_PATH) as db:
        await run(
            db,
            briefing_date=briefing_date,
            no_upload=args.no_upload,
            script_only=args.script_only,
            classify_pv=args.classify_pv,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)

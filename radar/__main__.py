"""`python -m radar` prints the command map.

Each subcommand is its own `python -m radar.<name>`; this module just lists
them so a new operator can see the whole surface at a glance.
"""

from __future__ import annotations

import sys

COMMANDS: list[tuple[str, str]] = [
    ("radar.ingest", "Incremental fetch of issues + PRs from the four watched repos."),
    ("radar.firsts", "Three-pass good-first-issue filter; pushes to ntfy on hits."),
    ("radar.classify", "Categorize + technically summarize each PR (per-repo taxonomy)."),
    ("radar.brief", "Daily LLM-written brief → Marp slides → Piper TTS → ffmpeg → YouTube."),
    ("radar.export", "Dump SQLite slices to site/data/*.json for the static site."),
    ("radar.label", "Mark a notification good/bad to build a retroactive golden set."),
    ("radar.run_all", "End-to-end orchestrator (ingest → firsts → classify → brief → export)."),
    ("radar.mint_youtube_token", "One-time helper to mint a YouTube refresh token."),
]


def main() -> int:
    print("Inference Radar — entry points:\n")
    for name, desc in COMMANDS:
        print(f"  python -m {name:<28} {desc}")
    print("\nAdd --help to any command for its flags.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

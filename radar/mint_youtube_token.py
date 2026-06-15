"""One-time helper: mint a YouTube Data API v3 refresh token.

Run on your laptop (not in CI). It opens a browser for OAuth consent, then
prints the refresh token. Copy that into your secrets manager / .env /
GH Actions repo secret `YOUTUBE_REFRESH_TOKEN`.

Prereqs:
  - Google Cloud project with OAuth client of type "Desktop".
  - YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET set in env.
  - uv sync --extra youtube  (installs google-auth-oauthlib).

Run:
  uv run --extra youtube python -m radar.mint_youtube_token

Reminder: if the OAuth project stays in *Testing* mode, the refresh token
expires after 7 days. Submit the project for *verification* (free, no quota
implications for the `youtube.upload` scope) to make the refresh token
durable.
"""

from __future__ import annotations

import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    cid = os.environ.get("YOUTUBE_CLIENT_ID")
    csec = os.environ.get("YOUTUBE_CLIENT_SECRET")
    if not (cid and csec):
        print(
            "set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first "
            "(create a Desktop OAuth client at console.cloud.google.com).",
            file=sys.stderr,
        )
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        print(
            "missing dep: uv sync --extra youtube  "
            "(installs google-auth-oauthlib + googleapiclient).",
            file=sys.stderr,
        )
        return 2

    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": csec,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    rt = creds.refresh_token
    if not rt:
        print(
            "no refresh_token issued. Re-run; if the project is in Testing "
            "mode, make sure you accepted the consent screen and that access_type "
            "is `offline` with prompt=consent.",
            file=sys.stderr,
        )
        return 1

    print("\n=== YOUTUBE_REFRESH_TOKEN ===")
    print(rt)
    print()
    print(
        "Add this to your .env and the GH Actions repo secret "
        "`YOUTUBE_REFRESH_TOKEN`."
    )
    # Also dump a JSON envelope on stderr for scripting
    print(json.dumps({"refresh_token": rt}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

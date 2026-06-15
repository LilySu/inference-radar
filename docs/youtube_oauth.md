# YouTube OAuth — one-time setup

The daily brief uploads to YouTube via the Data API v3. This needs an OAuth
"Desktop" client and a refresh token. Everything else (client id, client
secret, refresh token) lives in env vars / repo secrets thereafter.

## 1. Create the OAuth client

1. https://console.cloud.google.com → **APIs & Services** → **Library** →
   enable **YouTube Data API v3** for your project.
2. **APIs & Services** → **Credentials** → **Create credentials** → **OAuth
   client ID** → **Desktop**. Name it `inference-radar-brief-uploader`.
3. Copy the resulting **client_id** and **client_secret**.

## 2. Get the project into a durable mode

If your OAuth consent screen stays in **Testing** mode, refresh tokens expire
**after 7 days** (your daily upload breaks on day 8).

Two options:

- **Recommended**: submit the consent screen for **verification**. Verification
  for the `youtube.upload` scope is free and usually takes a few days. After
  verification the refresh token is durable.
- **Workaround**: leave the project in Testing and re-mint the refresh token
  weekly — either manually with the script below, or by adding a weekly
  workflow that re-runs the OAuth flow on a self-hosted runner (more work).

## 3. Mint the refresh token

On your laptop:

```bash
export YOUTUBE_CLIENT_ID='....apps.googleusercontent.com'
export YOUTUBE_CLIENT_SECRET='...'
uv sync --extra youtube
uv run python -m radar.mint_youtube_token
```

The helper opens a browser. Accept the consent screen. It prints the
refresh token to stdout.

## 4. Wire up the secrets

Locally:

```bash
# in .env
YOUTUBE_CLIENT_ID=...
YOUTUBE_CLIENT_SECRET=...
YOUTUBE_REFRESH_TOKEN=...
```

GitHub Actions:

```bash
gh secret set YOUTUBE_CLIENT_ID --body '...'
gh secret set YOUTUBE_CLIENT_SECRET --body '...'
gh secret set YOUTUBE_REFRESH_TOKEN --body '...'
```

That's it. `radar.brief` will pick them up and upload as unlisted.

## What gets uploaded

- **Title**: `Inference Radar — YYYY-MM-DD` (taken from the LLM-written
  `script.title`).
- **Description**: the intro paragraph plus the four repos in scope and a
  link back to this GitHub repo.
- **Privacy**: `unlisted` always. We never publish without the user clicking
  through.
- **Category**: 28 (Science & Technology).

If you want a different default privacy, change `privacyStatus` in
`radar/brief.py:upload_to_youtube` to `private` or `public`.

## Troubleshooting

- **"invalid_grant"**: refresh token expired (7-day Testing-mode rule above)
  or the OAuth client was deleted. Re-mint.
- **"insufficientPermissions"**: scope drift — the helper requests
  `https://www.googleapis.com/auth/youtube.upload`. If your channel needs a
  different scope (e.g. brand account), edit `SCOPES` in
  `radar/mint_youtube_token.py`.
- **"quotaExceeded"**: one upload costs 1600 quota units of the 10,000
  daily quota. You can request more from the Google Cloud console, but you
  shouldn't need to for one upload per day.

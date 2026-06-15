# site/

Next.js 14 (App Router, `output: 'export'`) front-end for Inference Radar.

The site is a static export with no runtime DB. Data lives in `./data/*.json`,
populated by:

```bash
uv run python -m radar.export
```

That dumps the relevant slices of `data/radar.db` into this directory. The
site then reads them at build time.

## Develop

```bash
cd site
npm install
npm run dev   # http://localhost:3000
```

## Build (static export)

```bash
uv run python -m radar.export      # write site/data/*.json
cd site
npm run build                       # produces ./out/ — static, ready for Vercel
```

## Deploy to Vercel

Connect this directory as the Vercel project root. Vercel auto-detects Next.js
and runs `next build`; with `output: 'export'` it serves the static export
from `./out`. The CI step that produces `site/data/*.json` runs in GitHub
Actions before Vercel deploys (see `.github/workflows/daily.yml`).

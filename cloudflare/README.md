# githubusers.archivebox.io — Cloudflare deploy

Tiny static site that publishes precomputed contribution dashboards for selected GitHub users.

## How it works

1. `users.txt` lists GitHub logins to mine.
2. A daily GitHub Action (`.github/workflows/mine-and-deploy.yml`) runs `generate_stats.py --user <login>` for each entry and copies the resulting HTML into `cloudflare/public/<login>.html`.
3. `pirate`'s enhanced dashboard is **not** auto-generated — the Action copies `stats.html` (which pirate maintains by running the script locally and committing the result) to `cloudflare/public/pirate.html`.
4. `wrangler deploy` ships `./public/` as Cloudflare Workers static assets at `githubusers.archivebox.io`.

No R2, no Worker code, no committed-back HTML in `gh-pages`. Single `wrangler deploy` from CI replaces the previous deployment atomically.

## URL routing

- `/<login>` → `public/<login>.html` (via `html_handling = "auto-trailing-slash"`)
- `/` → `public/index.html` (landing page with list of mined users)
- any other path → `public/404.html`

## Required GitHub Action secrets

| Secret | Purpose |
|---|---|
| `GH_MINING_TOKEN` | a GitHub PAT (or fine-grained token) used by the Action to call the GitHub REST API at higher rate limits |
| `CLOUDFLARE_API_TOKEN` | Cloudflare token with `Workers Scripts:Edit` permission |
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare account ID |

## Adding a user

Open a PR adding their login to `users.txt`. The Action runs on push to `main` and re-deploys.

## Updating pirate's enhanced dashboard

Pirate runs `python3 generate_stats.py` locally (uses his rich personalized config + local git mining), then commits the resulting `stats.html`. The next Action run picks it up.

## First-time setup

1. In Cloudflare dashboard: add a custom domain `githubusers.archivebox.io` → CNAME to the Worker.
2. In the GitHub repo settings: add the three secrets above.
3. Push to `main` (or click `Run workflow` in the Actions tab).

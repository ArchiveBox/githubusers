# githubusers.archivebox.io

Self-serve GitHub contribution dashboards. Visit `https://githubusers.archivebox.io/<login>` to see (or trigger mining of) any GitHub user's commit / PR / issue / star history.

- `/pirate` → custom enhanced dashboard (career timeline, manual canonical names, etc.)
- `/<any-other-login>` → auto-generated dashboard
- Unknown users get a "mining…" page that automatically dispatches a CI job and polls for completion

## How it works

1. `generate_stats.py` mines a user's commits + PRs + issues + stars via the GitHub API (and local clones when run locally). It dedupes commits across forks, canonicalizes renamed/transferred repos, attributes commits to companies (when configured), and writes a single self-contained HTML file.
2. `.github/workflows/mine-and-deploy.yml` runs `generate_stats.py --user <login>` for each entry in `cloudflare/users.txt`, copies each result into `cloudflare/public/<login>.html`, and deploys via `wrangler deploy`.
3. `cloudflare/worker/index.ts` serves static assets at `githubusers.archivebox.io` and falls through to a "mining…" page that POSTs `/api/refresh` to dispatch the workflow on demand.

No R2, no DO, no Workflows. Single Worker + Static Assets + one cron-triggered GitHub Action.

## Repo layout

```
.
├── generate_stats.py            # mining script (Python 3.13)
├── stats_template.html          # dashboard HTML template
├── cloudflare/
│   ├── wrangler.toml            # Worker + assets config
│   ├── worker/index.ts          # /api/refresh + 404 → loading page
│   ├── tsconfig.json
│   ├── users.txt                # list of GitHub logins to keep mining
│   ├── public/
│   │   ├── 404.html             # fallback
│   │   ├── index.html           # landing page
│   │   └── pirate.html          # pirate's enhanced version (committed)
│   └── README.md
└── .github/workflows/
    └── mine-and-deploy.yml      # daily + on-demand + on-push CI
```

## First-time setup

1. Set Cloudflare custom domain `githubusers.archivebox.io` → Worker `githubusers-archivebox-io`.
2. Add repo secrets at `https://github.com/ArchiveBox/githubusers/settings/secrets/actions`:
   - `CLOUDFLARE_API_TOKEN` — `Workers Scripts:Edit` permission
   - `CLOUDFLARE_ACCOUNT_ID`
   - `GH_MINING_TOKEN` — a PAT with `repo` + `workflow` scope (used by the Action to query the API + commit users.txt back)
3. Set the Worker secret used by `/api/refresh` to dispatch the Action:
   ```
   echo "$YOUR_GH_PAT" | wrangler secret put GH_DISPATCH_TOKEN
   ```
4. Push to `main` (or click *Run workflow* in the Actions tab) — first run mines every user in `users.txt` and deploys.

## Adding a user (zero-friction)

Just visit `https://githubusers.archivebox.io/<login>`. The Worker dispatches a single-user mining job and the page auto-polls for completion (~3-8 min). The login is appended to `users.txt` on success so future cron runs keep it fresh.

## Updating pirate's enhanced dashboard

```
python3 generate_stats.py                              # uses pirate's baked-in personalized config
cp stats.html cloudflare/public/pirate.html
git add cloudflare/public/pirate.html && git commit -m 'refresh pirate.html'
git push
```

The push triggers the Action which redeploys (CI doesn't regenerate pirate's HTML, it just copies the committed one through).

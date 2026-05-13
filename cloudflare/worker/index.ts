// Worker for githubusers.archivebox.io
//
// Static asset serving + two dynamic paths:
//   /api/refresh?user=X  POST → dispatch the GH Action to mine that user
//   /<unknown-login>     GET  → render "mining @login…" page that auto-
//                               triggers a refresh and polls for completion
//
// Everything else (known assets, /, /404.html) is served by ASSETS.

export interface Env {
  ASSETS: Fetcher;
  GH_DISPATCH_TOKEN: string;         // GitHub PAT with `actions:write`
  GH_REPO?: string;                  // "owner/repo" — defaults to ArchiveBox/githubusers
  GH_WORKFLOW?: string;              // workflow file — defaults to mine-and-deploy.yml
}

const VALID_LOGIN = /^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$/;

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    // -- API: refresh a single user --------------------------------------
    if (url.pathname === "/api/refresh") {
      return handleRefresh(req, env, url);
    }
    if (url.pathname === "/api/status") {
      return handleStatus(req, env, url);
    }
    if (url.pathname === "/api/progress") {
      return handleProgress(req, env, url);
    }

    // -- Dynamic homepage: render live deployed + queued user lists -------
    if (url.pathname === "/" || url.pathname === "/index.html") {
      try {
        return await handleIndex(env, url);
      } catch (e: any) {
        console.error("handleIndex failed:", e?.message, e?.stack);
        // Fall through to the static index.html in /public on any error.
      }
    }

    // -- Static assets ----------------------------------------------------
    const assetResp = await env.ASSETS.fetch(req);
    if (assetResp.status !== 404) return assetResp;

    // -- 404 fallback: render a loading page if it looks like a user URL
    const m = url.pathname.match(/^\/([a-zA-Z0-9][a-zA-Z0-9-]{0,38})\/?$/);
    if (m && VALID_LOGIN.test(m[1])) {
      return new Response(loadingPage(m[1]), {
        status: 200,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
        },
      });
    }
    return assetResp;
  },
};


async function handleRefresh(
  req: Request,
  env: Env,
  url: URL,
): Promise<Response> {
  const user = url.searchParams.get("user")?.trim();
  if (!user || !VALID_LOGIN.test(user)) {
    return json({ error: "invalid user" }, 400);
  }
  if (req.method !== "POST" && req.method !== "GET") {
    return new Response("method not allowed", { status: 405 });
  }
  // Manual refresh button passes ?force=1 to bypass the "already deployed"
  // short-circuit. Without force, a request for a user whose dashboard
  // already exists is a no-op (we want pages to stay valid indefinitely
  // once mined).
  const force = url.searchParams.get("force") === "1";

  // Short-circuit if the static dashboard is already deployed. The Worker
  // fallback only sends people here when the asset is missing, so this
  // mainly catches direct /api/refresh callers (bots, refresh button
  // without force).
  if (!force) {
    const probe = new URL(url.toString());
    probe.pathname = `/${user}.html`;
    const probeResp = await env.ASSETS.fetch(
      new Request(probe.toString(), { method: "GET" }),
    );
    if (probeResp.status === 200) {
      return json({
        ok: true,
        user,
        status: "already_deployed",
        message: "Dashboard already exists. Pass ?force=1 to re-mine.",
      }, 200);
    }
  }

  // Dedup: for non-force calls (loading page after initial visit), don't
  // re-dispatch within ~6 hours of the most recent one for this user.
  // Force calls (manual "Refresh stats" button) bypass dedup — GitHub
  // Actions' own concurrency.group keeps at most one queued run, which
  // is plenty of protection against rapid double-clicks.
  const cache = caches.default;
  const dedupKey = new Request(
    `https://internal-dedup.invalid/dispatch/${user}`,
  );
  if (!force) {
    const existing = await cache.match(dedupKey);
    if (existing) {
      return json({
        ok: true,
        user,
        status: "already_running",
        dispatched_at: existing.headers.get("X-Dispatched-At") ?? null,
      }, 202);
    }
  }

  const repo = env.GH_REPO ?? "ArchiveBox/githubusers";
  const wf = env.GH_WORKFLOW ?? "mine-and-deploy.yml";

  // First quickly check if the GH user even exists, to avoid wasting CI.
  const ghCheck = await fetch(`https://api.github.com/users/${user}`, {
    headers: {
      "user-agent": "githubusers-archivebox-io",
      Accept: "application/vnd.github+json",
    },
  });
  if (ghCheck.status === 404) {
    return json({ error: "no such GitHub user" }, 404);
  }

  const resp = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "githubusers-archivebox-io",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { user },
      }),
    },
  );
  if (!resp.ok) {
    const body = await resp.text();
    return json({ error: "dispatch failed", status: resp.status, body }, 502);
  }
  // Set the dedup marker after a successful dispatch. 6-hour TTL — a
  // successful mine doesn't need to be re-run sooner than that, and a
  // failed/stuck job becomes retryable after 6 hours.
  await cache.put(
    dedupKey,
    new Response("dispatched", {
      headers: {
        "Cache-Control": "max-age=21600",
        "X-Dispatched-At": new Date().toISOString(),
      },
    }),
  );
  return json({ ok: true, user, status: "dispatched" }, 202);
}


function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}


async function handleProgress(
  req: Request,
  env: Env,
  url: URL,
): Promise<Response> {
  // POST { phase, message, totals } — keyed by ?user=X — read back via
  // /api/status. Auth: Bearer header must match GH_DISPATCH_TOKEN
  // (the same token the Action gets from secrets) so random clients
  // can't spam fake progress.
  if (req.method !== "POST") {
    return json({ error: "POST only" }, 405);
  }
  const auth = req.headers.get("Authorization") ?? "";
  if (!auth.startsWith("Bearer ") ||
      auth.slice(7) !== env.GH_DISPATCH_TOKEN) {
    return json({ error: "unauthorized" }, 401);
  }
  const user = url.searchParams.get("user")?.trim();
  if (!user || !VALID_LOGIN.test(user)) {
    return json({ error: "invalid user" }, 400);
  }
  const body = await req.text();
  // Sanity cap — small JSON only.
  if (body.length > 4096) return json({ error: "too large" }, 413);
  try { JSON.parse(body); } catch { return json({ error: "bad json" }, 400); }
  const cache = caches.default;
  await cache.put(
    new Request(`https://internal-progress.invalid/${user}`),
    new Response(body, {
      headers: {
        "Cache-Control": "max-age=3600",
        "Content-Type": "application/json",
        "X-Received-At": new Date().toISOString(),
      },
    }),
  );
  return json({ ok: true });
}


// Fetch the GH-API-derived state used by /api/status. Encapsulated so
// handleStatus can cache the whole result. On any non-OK GH response,
// returns { error: ..., status: ... } — callers fall back to a stale
// cached copy when present.
async function fetchGhState(env: Env, repo: string): Promise<any> {
  const ghHeaders = {
    Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
    "User-Agent": "githubusers-archivebox-io",
    Accept: "application/vnd.github+json",
  };
  // 1) Recent workflow runs.
  const r = await fetch(
    `https://api.github.com/repos/${repo}/actions/runs?per_page=5`,
    { headers: ghHeaders },
  );
  if (!r.ok) {
    let message = "";
    try { message = (await r.json() as any).message ?? ""; } catch {}
    return { error: "gh_api_failed", status: r.status, message };
  }
  const data = await r.json() as any;
  const runs = data.workflow_runs ?? [];
  const run = runs.find((x: any) => x.status === "in_progress")
           ?? runs.find((x: any) => x.status === "queued")
           ?? runs[0];
  if (!run) return { run: null };

  // 2) Job + steps for the chosen run.
  const jr = await fetch(
    `https://api.github.com/repos/${repo}/actions/runs/${run.id}/jobs`,
    { headers: ghHeaders },
  );
  let steps: any[] = [];
  let job: any = null;
  if (jr.ok) {
    const jdata = await jr.json() as any;
    job = (jdata.jobs ?? [])[0];
    steps = (job?.steps ?? []).map((s: any) => ({
      name: s.name,
      status: s.status,
      conclusion: s.conclusion,
    }));
  }

  // 3) Rate-limit gauge (free endpoint — doesn't count against quota).
  let rateLimit: any = null;
  try {
    const rl = await fetch("https://api.github.com/rate_limit",
                           { headers: ghHeaders });
    if (rl.ok) {
      const rd = await rl.json() as any;
      const rr = rd?.resources ?? {};
      rateLimit = {
        search: rr.search ? {
          remaining: rr.search.remaining,
          limit: rr.search.limit,
          reset: rr.search.reset,
        } : null,
        core: rr.core ? {
          remaining: rr.core.remaining,
          limit: rr.core.limit,
          reset: rr.core.reset,
        } : null,
      };
    }
  } catch {}

  // 4) Tail of recent log output (only when the job is in_progress —
  // saves a hefty fetch on idle runs).
  let recentLog: string[] = [];
  if (job?.id && job.status === "in_progress") {
    try {
      const lr = await fetch(
        `https://api.github.com/repos/${repo}/actions/jobs/${job.id}/logs`,
        { headers: ghHeaders },
      );
      if (lr.ok) {
        const txt = await lr.text();
        recentLog = txt
          .split("\n")
          .map((l) => l.replace(/^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?/, ""))
          .filter((l) => /^(>>|\s*\[|\s*-{2}|\s*!|\s*resolved\b|\s*scanning |\s*fetching |\s*mining |\s*deploying|\s*search quota|\s*resolving )/i
                          .test(l))
          .slice(-20);
      }
    } catch {}
  }

  return {
    run: {
      id: run.id,
      status: run.status,
      conclusion: run.conclusion,
      run_started_at: run.run_started_at,
      html_url: run.html_url,
    },
    job: job ? { id: job.id, status: job.status } : null,
    steps,
    rate_limit: rateLimit,
    recent_log: recentLog,
  };
}


async function handleStatus(
  req: Request,
  env: Env,
  url: URL,
): Promise<Response> {
  const user = url.searchParams.get("user")?.trim();
  if (!user || !VALID_LOGIN.test(user)) {
    return json({ error: "invalid user" }, 400);
  }
  const repo = env.GH_REPO ?? "ArchiveBox/githubusers";

  // GH API state (workflow runs, jobs, logs, rate-limit) is the same for
  // every visitor / user — cache it globally for 15s. Loading pages poll
  // every 4s; without this cache we burn ~45 GH API requests per minute
  // per active visitor, which exhausts the 5000/hr PAT limit fast.
  const ghStateKey = new Request(
    `https://internal-status.invalid/gh-state-v1`,
  );
  let ghState: any = null;
  let stale = false;
  const cached = await caches.default.match(ghStateKey);
  if (cached) {
    try { ghState = await cached.json(); } catch {}
  }
  if (!ghState) {
    ghState = await fetchGhState(env, repo);
    if (ghState.error) {
      // Couldn't reach GH — fall back to whatever we last saw (if any).
      // Without a fallback we serve {error:"..."} which makes the
      // loading page render nothing.
      const stale_resp = await caches.default.match(
        new Request(`https://internal-status.invalid/gh-state-stale-v1`),
      );
      if (stale_resp) {
        try { ghState = await stale_resp.json(); stale = true; } catch {}
      }
      if (!ghState || ghState.error) {
        return json({
          ok: false,
          error: "gh_unreachable",
          gh_status: ghState?.status,
          gh_message: ghState?.message,
        }, 200);
      }
    } else {
      // Cache for 15s (frequent polling) and keep a separate "stale"
      // copy that lives much longer (1h) so we can fall back when GH
      // rate-limits us.
      await caches.default.put(
        ghStateKey,
        new Response(JSON.stringify(ghState), {
          headers: {
            "Cache-Control": "max-age=15",
            "Content-Type": "application/json",
          },
        }),
      );
      await caches.default.put(
        new Request(`https://internal-status.invalid/gh-state-stale-v1`),
        new Response(JSON.stringify(ghState), {
          headers: {
            "Cache-Control": "max-age=3600",
            "Content-Type": "application/json",
          },
        }),
      );
    }
  }
  const run = ghState.run;
  if (!run) {
    return json({ ok: false, status: "no_runs", stale });
  }
  const steps = ghState.steps ?? [];
  const rateLimit = ghState.rate_limit ?? null;
  const recentLog: string[] = ghState.recent_log ?? [];
  const job = ghState.job;

  // Read the latest progress update posted by the running Python script.
  let progress: any = null;
  try {
    const pres = await caches.default.match(
      new Request(`https://internal-progress.invalid/${user}`),
    );
    if (pres) {
      progress = await pres.json();
      progress.received_at = pres.headers.get("X-Received-At");
    }
  } catch {}

  return json({
    ok: true,
    run_id: run.id,
    run_status: run.status,
    run_conclusion: run.conclusion,
    run_started_at: run.run_started_at,
    run_url: run.html_url,
    job_status: job?.status,
    current_step: steps.find((s: any) => s.status === "in_progress")?.name
                  ?? steps.at(-1)?.name ?? null,
    steps,
    rate_limit: rateLimit,
    recent_log: recentLog,
    progress,
  });
}


// Dynamic homepage. Reads /users.txt (also deployed as a static asset
// by CI) for the canonical list of users we want dashboards for, then
// probes /{user}.html via the ASSETS binding to see which are ready vs
// still queued/mining. Output is cached in Workers Cache for 30s so
// repeated visits don't fan out to N internal asset probes each time.
async function handleIndex(env: Env, url: URL): Promise<Response> {
  const cache = caches.default;
  const cacheKey = new Request("https://internal-index.invalid/v1");
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  // Read users.txt from the deployed assets.
  const probeBase = new URL(url.toString());
  probeBase.search = "";
  const usersTxtUrl = new URL(probeBase.toString());
  usersTxtUrl.pathname = "/users.txt";
  let users: string[] = [];
  try {
    const r = await env.ASSETS.fetch(new Request(usersTxtUrl.toString()));
    if (r.ok) {
      const txt = await r.text();
      users = txt.split("\n")
        .map((l) => l.split("#", 1)[0].trim())
        .filter((l) => l.length > 0);
    }
  } catch {}

  // Add pirate (intentionally not in users.txt — built locally).
  if (!users.includes("pirate")) users.unshift("pirate");

  // Probe each user's /<u>.html for deploy status in parallel.
  const states = await Promise.all(users.map(async (u) => {
    const probeUrl = new URL(probeBase.toString());
    probeUrl.pathname = `/${u}.html`;
    try {
      const r = await env.ASSETS.fetch(new Request(probeUrl.toString()));
      return { user: u, deployed: r.status === 200 };
    } catch {
      return { user: u, deployed: false };
    }
  }));

  const deployed = states
    .filter((s) => s.deployed)
    .map((s) => s.user)
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const queued = states
    .filter((s) => !s.deployed)
    .map((s) => s.user)
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));

  const html = indexPage(deployed, queued);
  const resp = new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "public, max-age=30",
    },
  });
  // Stash a clone for future hits (the response itself can only be
  // consumed once; cache.put is fine with the cloned Response).
  await cache.put(cacheKey, resp.clone());
  return resp;
}


function indexPage(deployed: string[], queued: string[]): string {
  const escape = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const deployedRows = deployed.map((u) => {
    const suffix = u === "pirate" ? " — Nick Sweeting (enhanced)" : "";
    return `      <li class="ready"><a href="/${escape(u)}">/${escape(u)}</a>${suffix}</li>`;
  }).join("\n");
  const queuedRows = queued.map((u) =>
    `      <li class="mining"><span>/${escape(u)}</span> <em>· queued / mining</em></li>`
  ).join("\n");
  const queuedSection = queued.length
    ? `\n      <li class="section-hdr">Queued for next CI run (${queued.length})</li>\n${queuedRows}`
    : "";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>githubusers.archivebox.io</title>
<style>
  html, body {
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 0; min-height: 100%;
  }
  .wrap { max-width: 640px; margin: 0 auto; padding: 48px 24px; }
  h1 { font-size: 24px; margin: 0 0 8px; }
  p { color: #8b949e; line-height: 1.5; }
  a { color: #58a6ff; }
  ul { list-style: none; padding: 0; }
  ul li { padding: 8px 0; border-bottom: 1px solid #21262d; }
  ul li.mining { color: #8b949e; }
  ul li.mining em {
    color: #d29922; font-style: normal; font-size: 11px;
    background: #1f1810; border: 1px solid #443322;
    padding: 1px 6px; border-radius: 4px; margin-left: 6px;
  }
  ul li.section-hdr {
    color: #6e7681; font-size: 11px;
    border: 0; padding: 16px 0 4px;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  code { background: #21262d; padding: 2px 6px; border-radius: 4px; font-size: 90%; }
  .meta { font-size: 11px; color: #6e7681; margin-top: 24px; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>githubusers.archivebox.io</h1>
    <p>
      Precomputed contribution dashboards for selected GitHub users.
      Navigate to <code>/&lt;login&gt;</code> for any user listed below
      (or any other login — mining auto-triggers on first visit).
    </p>
    <ul>
${deployedRows}${queuedSection}
    </ul>
    <p class="meta">
      ${deployed.length} deployed · ${queued.length} queued · refreshed every 30s
    </p>
  </div>
</body>
</html>`;
}


function loadingPage(user: string): string {
  // Inline HTML for the "mining…" view. Polls /api/status for the GH
  // workflow run's step list + /<user>.html for the first partial deploy.
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mining @${user} — githubusers.archivebox.io</title>
<style>
  html, body {
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    max-width: 620px; width: calc(100% - 32px);
    padding: 32px 36px;
    border: 1px solid #30363d; border-radius: 12px; background: #161b22;
  }
  h1 { font-size: 16px; margin: 0; font-weight: 500; color: #8b949e; }
  h2 { font-size: 28px; margin: 4px 0 16px; font-weight: 600;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  p { color: #8b949e; line-height: 1.5; margin: 10px 0; font-size: 13px; }
  .row { display: flex; align-items: center; gap: 10px; margin: 18px 0; }
  .spinner {
    width: 22px; height: 22px;
    border: 2px solid #30363d; border-top-color: #58a6ff;
    border-radius: 50%; animation: spin 0.9s linear infinite; flex: 0 0 auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .summary { font-size: 13px; }
  .summary .now { color: #e6edf3; font-weight: 500; }
  .summary .elapsed { color: #8b949e; font-variant-numeric: tabular-nums; }
  .progress-track {
    height: 6px; background: #21262d; border-radius: 3px;
    overflow: hidden; margin: 8px 0 20px;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #58a6ff, #3fb950);
    width: 0%; transition: width 0.4s;
  }
  ol.steps {
    list-style: none; padding: 0; margin: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  ol.steps li {
    padding: 7px 12px; border-radius: 4px; margin-bottom: 3px;
    display: flex; align-items: center; gap: 10px;
    background: #0d1117; border: 1px solid transparent;
  }
  ol.steps li.done { color: #3fb950; }
  ol.steps li.done::before { content: "✓"; flex: 0 0 14px; }
  ol.steps li.running {
    color: #58a6ff; border-color: #1f4d7a; background: #0e2640;
  }
  ol.steps li.running::before {
    content: ""; flex: 0 0 14px; width: 12px; height: 12px;
    border: 2px solid #30363d; border-top-color: #58a6ff;
    border-radius: 50%; animation: spin 0.9s linear infinite;
  }
  ol.steps li.pending { color: #6e7681; }
  ol.steps li.pending::before { content: "◌"; flex: 0 0 14px; }
  ol.steps li.failed { color: #f85149; border-color: #6e2120; background: #2a0e10; }
  ol.steps li.failed::before { content: "✗"; flex: 0 0 14px; }
  .err {
    color: #f85149; padding: 10px 14px; background: #2a0e10;
    border: 1px solid #6e2120; border-radius: 6px; font-size: 13px;
    margin: 10px 0;
  }
  .ratelimit {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; padding: 8px 12px; margin: 0 0 14px;
    background: #0d1117; border: 1px solid #21262d;
    border-radius: 6px; color: #8b949e;
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; flex-wrap: wrap;
  }
  .ratelimit.cooldown {
    background: #2a1a08; border-color: #6e4c18; color: #ffa657;
  }
  .ratelimit .gauge {
    flex: 1; height: 4px; background: #21262d;
    border-radius: 2px; overflow: hidden; min-width: 80px;
  }
  .ratelimit .gauge > span {
    display: block; height: 100%; background: #3fb950;
  }
  .ratelimit.cooldown .gauge > span { background: #d97706; }
  .phase-msg {
    background: #0e2640; border: 1px solid #1f4d7a;
    color: #58a6ff; padding: 12px 14px; border-radius: 6px;
    margin: 0 0 14px; font-size: 13px;
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px; flex-wrap: wrap;
  }
  .phase-msg .pm-msg { flex: 1; min-width: 200px; }
  .phase-msg .pm-counts {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; color: #8b949e;
  }
  .phase-msg .pm-counts strong { color: #c9d1d9; }
  .livelog {
    background: #0d1117; border: 1px solid #21262d;
    border-radius: 6px; padding: 10px 12px; margin: 14px 0 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px; color: #c9d1d9;
    max-height: 180px; overflow-y: auto; line-height: 1.45;
    white-space: pre-wrap; word-break: break-all;
  }
  .livelog .l-hdr { color: #58a6ff; }
  .livelog .l-warn { color: #ffa657; }
  .livelog .l-err { color: #f85149; }
  a { color: #58a6ff; }
  code { background: #21262d; padding: 1px 5px; border-radius: 3px;
         font-size: 90%; font-family: inherit; }
  .footer-row {
    margin-top: 22px; padding-top: 18px; border-top: 1px solid #21262d;
    font-size: 11px; color: #6e7681;
    display: flex; justify-content: space-between; align-items: center;
  }
</style>
</head>
<body>
  <div class="card">
    <h1>Mining contribution stats for</h1>
    <h2 id="hdr">@${user}</h2>

    <div class="row">
      <div class="spinner" id="hdr-spinner"></div>
      <div class="summary" style="flex:1">
        <div class="now" id="now-line">Triggering mining job…</div>
        <div class="elapsed" id="elapsed-line">elapsed 00:00</div>
      </div>
    </div>

    <div class="progress-track"><div class="progress-fill" id="progress"></div></div>

    <div id="phase-msg" class="phase-msg" style="display:none"></div>

    <div id="ratelimit" class="ratelimit" style="display:none"></div>

    <ol class="steps" id="steps"></ol>

    <pre id="livelog" class="livelog" style="display:none"></pre>

    <div id="error" class="err" style="display:none"></div>

    <div class="footer-row">
      <div>
        Bookmark <code>/${user}</code> · subsequent visits load instantly
      </div>
      <a id="run-link" href="#" target="_blank" rel="noreferrer" style="display:none">view CI run →</a>
    </div>
  </div>

<script>
"use strict";
const USER = ${JSON.stringify(user)};

const $now = document.getElementById("now-line");
const $elapsed = document.getElementById("elapsed-line");
const $progress = document.getElementById("progress");
const $steps = document.getElementById("steps");
const $err = document.getElementById("error");
const $runLink = document.getElementById("run-link");
const $spinner = document.getElementById("hdr-spinner");
const $rl = document.getElementById("ratelimit");
const $log = document.getElementById("livelog");
const $pmsg = document.getElementById("phase-msg");

const startedAt = Date.now();
function fmtElapsed(sec) {
  const m = Math.floor(sec / 60), s = sec % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}
setInterval(() => {
  $elapsed.textContent = "elapsed " + fmtElapsed(
    Math.floor((Date.now() - startedAt) / 1000),
  );
}, 1000);

function showError(msg) {
  $err.style.display = "block";
  $err.textContent = msg;
}

async function dispatch() {
  try {
    const r = await fetch("/api/refresh?user=" + encodeURIComponent(USER), {
      method: "POST",
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 404) {
      showError("GitHub user @" + USER + " does not exist.");
      $spinner.style.display = "none";
      return false;
    }
    if (!r.ok) {
      showError("Dispatch failed (HTTP " + r.status + "): " + (data.error || "unknown"));
      return false;
    }
    if (data.status === "already_deployed") {
      // Race: dashboard came back online between page render and dispatch.
      $now.textContent = "Dashboard already deployed — reloading…";
      setTimeout(() => location.reload(), 400);
      return false;
    }
    $now.textContent = data.status === "already_running"
      ? "Mining already running — joining in progress…"
      : "Mining job dispatched.";
  } catch (e) {
    showError("Dispatch network error: " + e.message);
    return false;
  }
  return true;
}

async function fetchStatus() {
  try {
    const r = await fetch("/api/status?user=" + encodeURIComponent(USER),
      { cache: "no-store" });
    if (!r.ok) return null;
    const j = await r.json();
    // Worker hit a GH API outage / rate limit. Surface a friendly note
    // instead of silently rendering nothing.
    if (j && j.error === "gh_unreachable") {
      $now.textContent = "Waiting on GitHub API… (" +
        (j.gh_status || "unreachable") + ") — will retry";
      return null;
    }
    return j;
  } catch (e) { return null; }
}

async function checkDeployed() {
  try {
    const r = await fetch("/" + USER, {
      cache: "no-store",
      headers: { "X-Stats-Poll": "1" },
    });
    if (!r.ok) return false;
    const txt = await r.text();
    // If the response is our loading shell, the asset isn't there yet.
    return !txt.includes('id="hdr">@' + USER);
  } catch (e) {
    return false;
  }
}

function renderProgress(p) {
  if (!p || !p.phase) { $pmsg.style.display = "none"; return; }
  const countKeys = ["repos", "commits", "prs", "issues", "stars",
                     "repos_accessible"];
  const counts = countKeys
    .filter(k => p[k] != null)
    .map(k => '<strong>' + p[k] + '</strong> ' + k);
  $pmsg.innerHTML =
    '<div class="pm-msg">' +
      (p.message || p.phase) +
      ' <code style="font-size:10px;color:#8b949e;margin-left:6px">' +
      p.phase + '</code></div>' +
    (counts.length ? '<div class="pm-counts">' + counts.join(" · ") + '</div>' : "");
  $pmsg.style.display = "flex";
}

function renderLog(lines) {
  if (!Array.isArray(lines) || lines.length === 0) {
    $log.style.display = "none"; return;
  }
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  $log.innerHTML = lines.map(l => {
    const cls = /^!|fail|error|❌/i.test(l) ? "l-err"
              : /quota|warn|^  !/i.test(l) ? "l-warn"
              : /^>>/.test(l) ? "l-hdr"
              : "";
    return '<span class="' + cls + '">' + esc(l) + '</span>';
  }).join("\n");
  $log.style.display = "block";
  $log.scrollTop = $log.scrollHeight;
}

function renderRateLimit(rl) {
  if (!rl || (!rl.search && !rl.core)) {
    $rl.style.display = "none"; return;
  }
  const now = Math.floor(Date.now() / 1000);
  const items = [];
  for (const kind of ["search", "core"]) {
    const r = rl[kind];
    if (!r) continue;
    const pct = Math.max(0, Math.min(100, (r.remaining / r.limit) * 100));
    const cooldown = r.remaining < (kind === "search" ? 5 : 100);
    const secs = Math.max(0, (r.reset || 0) - now);
    const label = kind === "search" ? "GitHub search" : "GitHub core";
    items.push(
      '<div style="display:flex;align-items:center;gap:8px;flex:1;min-width:160px">' +
        '<span>' + label + ' ' + r.remaining + '/' + r.limit + '</span>' +
        '<div class="gauge"><span style="width:' + pct + '%"></span></div>' +
        (cooldown ? '<span style="color:#ffa657">resets in ' + secs + 's</span>' : '') +
      '</div>'
    );
  }
  $rl.innerHTML = items.join("");
  const lowSearch = rl.search && rl.search.remaining < 5;
  const lowCore = rl.core && rl.core.remaining < 100;
  $rl.className = "ratelimit" + (lowSearch || lowCore ? " cooldown" : "");
  $rl.style.display = "flex";
}

function renderSteps(status) {
  if (!status || !status.steps) return;
  if (status.run_url) {
    $runLink.href = status.run_url;
    $runLink.style.display = "inline";
  }
  // Filter to the steps that matter for the user.
  const meaningful = status.steps.filter(s =>
    !["Set up job", "Complete job"].includes(s.name) &&
    !s.name.startsWith("Post ")
  );
  const total = meaningful.length || 1;
  let done = 0, running = 0;
  $steps.innerHTML = meaningful.map(s => {
    let cls = "pending";
    if (s.status === "completed") {
      if (s.conclusion === "success") { cls = "done"; done++; }
      else if (s.conclusion === "skipped") { cls = "done"; done++; }
      else { cls = "failed"; }
    } else if (s.status === "in_progress") {
      cls = "running"; running++;
    } else if (s.status === "queued") {
      cls = "pending";
    }
    return '<li class="' + cls + '">' + s.name + '</li>';
  }).join("");
  // Progress = (done + 0.5 * running) / total
  const pct = Math.min(100, ((done + 0.5 * running) / total) * 100);
  $progress.style.width = pct + "%";
  // Header status line
  const runStep = meaningful.find(s => s.status === "in_progress");
  if (status.run_status === "completed") {
    if (status.run_conclusion === "success") {
      $now.textContent = "Run completed · loading dashboard…";
    } else {
      $now.textContent = "Run " + status.run_conclusion + " — see CI link";
    }
  } else if (runStep) {
    $now.textContent = "Running: " + runStep.name;
  } else if (status.run_status === "queued") {
    $now.textContent = "Queued in GitHub Actions…";
  }
}

(async () => {
  const ok = await dispatch();
  if (!ok) return;
  const interval = setInterval(async () => {
    const [status, deployed] = await Promise.all([
      fetchStatus(),
      checkDeployed(),
    ]);
    renderSteps(status);
    if (status) {
      renderProgress(status.progress);
      renderRateLimit(status.rate_limit);
      renderLog(status.recent_log);
    }
    if (deployed) {
      clearInterval(interval);
      $now.textContent = "Dashboard ready — reloading…";
      setTimeout(() => location.reload(), 500);
    }
  }, 8000);
})();
</script>
</body>
</html>`;
}

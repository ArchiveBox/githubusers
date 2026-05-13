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

  // Dedup: don't re-dispatch within 8 min of the most recent one for this
  // user. Uses Workers Cache API — no KV/DO binding needed.
  const cache = caches.default;
  const dedupKey = new Request(
    `https://internal-dedup.invalid/dispatch/${user}`,
  );
  const existing = await cache.match(dedupKey);
  if (existing) {
    return json({
      ok: true,
      user,
      status: "already_running",
      dispatched_at: existing.headers.get("X-Dispatched-At") ?? null,
    }, 202);
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
  // Fetch the most recent workflow_dispatch run. Since concurrency.group
  // serializes mines, the latest in_progress (or most recent overall)
  // is most likely the one for this user.
  const r = await fetch(
    `https://api.github.com/repos/${repo}/actions/runs?per_page=5&event=workflow_dispatch`,
    {
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        "User-Agent": "githubusers-archivebox-io",
        Accept: "application/vnd.github+json",
      },
    },
  );
  if (!r.ok) {
    return json({ error: "gh api failed", status: r.status }, 502);
  }
  const data = await r.json() as any;
  const run = (data.workflow_runs ?? [])[0];
  if (!run) {
    return json({ ok: false, status: "no_runs" });
  }
  // Get job steps for the run.
  const jr = await fetch(
    `https://api.github.com/repos/${repo}/actions/runs/${run.id}/jobs`,
    {
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        "User-Agent": "githubusers-archivebox-io",
        Accept: "application/vnd.github+json",
      },
    },
  );
  const jdata = await jr.json() as any;
  const job = (jdata.jobs ?? [])[0];
  const steps = (job?.steps ?? []).map((s: any) => ({
    name: s.name,
    status: s.status,
    conclusion: s.conclusion,
  }));

  // Surface current GitHub API rate-limit state so the loading page can
  // explain delays. Uses the same PAT the CI runs with, so the search /
  // core remaining numbers are very close to what the CI job sees.
  let rateLimit: any = null;
  try {
    const rl = await fetch("https://api.github.com/rate_limit", {
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        "User-Agent": "githubusers-archivebox-io",
        Accept: "application/vnd.github+json",
      },
    });
    if (rl.ok) {
      const rd = await rl.json() as any;
      const r = rd?.resources ?? {};
      rateLimit = {
        search: r.search ? {
          remaining: r.search.remaining,
          limit: r.search.limit,
          reset: r.search.reset,    // epoch seconds
        } : null,
        core: r.core ? {
          remaining: r.core.remaining,
          limit: r.core.limit,
          reset: r.core.reset,
        } : null,
      };
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
  });
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

    <div id="ratelimit" class="ratelimit" style="display:none"></div>

    <ol class="steps" id="steps"></ol>

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
    return await r.json();
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
    if (status) renderRateLimit(status.rate_limit);
    if (deployed) {
      clearInterval(interval);
      $now.textContent = "Dashboard ready — reloading…";
      setTimeout(() => location.reload(), 500);
    }
  }, 4000);
})();
</script>
</body>
</html>`;
}

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
  return json({ ok: true, user }, 202);
}


function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}


function loadingPage(user: string): string {
  // Inline HTML for the "mining…" view. Polls /<user> every 4 seconds and
  // reloads once we get a real (non-fallback) response. Also kicks off
  // /api/refresh on load so users don't need a config change.
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
    margin: 0; padding: 0; min-height: 100%;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    max-width: 540px; padding: 40px 36px; text-align: center;
    border: 1px solid #30363d; border-radius: 12px; background: #161b22;
  }
  h1 { font-size: 18px; margin: 0 0 6px; font-weight: 500; }
  h2 { font-size: 26px; margin: 0 0 18px; font-weight: 600;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  p { color: #8b949e; line-height: 1.5; margin: 12px 0; font-size: 13px; }
  .spinner {
    width: 36px; height: 36px; margin: 24px auto;
    border: 3px solid #30363d; border-top-color: #58a6ff;
    border-radius: 50%; animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .status {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: #8b949e; margin-top: 16px;
    padding: 8px 12px; background: #0d1117; border-radius: 6px;
    border: 1px solid #21262d;
  }
  .status.ok { color: #3fb950; border-color: #1f6028; }
  .status.err { color: #f85149; border-color: #6e2120; }
  a { color: #58a6ff; }
  code { background: #21262d; padding: 2px 6px; border-radius: 4px; font-size: 90%; }
</style>
</head>
<body>
  <div class="card">
    <h1>Mining contribution stats for</h1>
    <h2>@${user}</h2>
    <div class="spinner"></div>
    <p>
      This takes <strong>~3–8 minutes</strong> the first time
      (gathering commits, PRs, issues, stars, and merged-PR diff stats
      from the GitHub API).
      The page will reload automatically when ready.
    </p>
    <div id="status" class="status">Triggering mining job…</div>
    <p style="margin-top:24px;font-size:11px;">
      Tip: bookmark <code>githubusers.archivebox.io/${user}</code>.
      Subsequent visits load instantly from cache.
    </p>
  </div>

<script>
"use strict";
const USER = ${JSON.stringify(user)};
const statusEl = document.getElementById("status");

function setStatus(msg, cls = "") {
  statusEl.textContent = msg;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

async function dispatch() {
  try {
    const r = await fetch("/api/refresh?user=" + encodeURIComponent(USER), {
      method: "POST",
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 202) {
      setStatus("Mining job dispatched. Polling for completion…");
    } else if (r.status === 404) {
      setStatus("GitHub user @" + USER + " does not exist.", "err");
      return false;
    } else {
      setStatus("Dispatch failed (HTTP " + r.status + "): " + (data.error || "unknown"), "err");
    }
  } catch (e) {
    setStatus("Dispatch network error: " + e.message, "err");
  }
  return true;
}

async function poll() {
  try {
    const r = await fetch("/" + USER, {
      cache: "no-store",
      headers: { "X-Stats-Poll": "1" },
    });
    if (!r.ok) return false;
    const txt = await r.text();
    // If the page is our own loading shell, keep waiting.
    if (txt.includes("Mining contribution stats for")) return false;
    // Otherwise the real dashboard has landed — reload to display it.
    return true;
  } catch (e) {
    return false;
  }
}

(async () => {
  const ok = await dispatch();
  if (!ok) return;
  const started = Date.now();
  const interval = setInterval(async () => {
    const ready = await poll();
    const elapsed = Math.floor((Date.now() - started) / 1000);
    if (ready) {
      clearInterval(interval);
      setStatus("Dashboard ready — reloading…", "ok");
      setTimeout(() => location.reload(), 500);
    } else {
      setStatus("Mining in progress · " + elapsed + "s elapsed · still polling…");
    }
  }, 4000);
})();
</script>
</body>
</html>`;
}

#!/usr/bin/env python3
"""
generate_stats.py — mine pirate's git history across local clones and GitHub.

Pipeline:
  1. Walk local filesystem for .git dirs (under /Users/squash/Local & Documents).
  2. Mine each repo with `git log --no-merges --numstat` filtered to pirate's
     author identities. Cache per-repo results to JSON.
  3. Query gh API for owned repos. Any owned repo not covered locally is
     pulled via `gh api /repos/.../commits` + per-commit stats fetch.
  4. Query gh search/commits to discover repos pirate has contributed to
     but does not own. Fetch missing commits via API.
  5. Dedupe by SHA across all sources, aggregate by year/repo/day, and
     render a single-file stats.html using an embedded template.

Run:  python3 generate_stats.py            # full run, uses cache
      python3 generate_stats.py --no-api   # local-only, skip GitHub API
      python3 generate_stats.py --render   # re-render HTML from cached data
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Configuration (mutable at startup via load_config / CLI flags).
# Defaults below are pirate's personal config; override via --user / --config.
# ---------------------------------------------------------------------------

GH_LOGIN = "pirate"
GH_NAME = "Nick Sweeting"

# Optional: when set, the script POSTs phase updates to this URL so the
# live "mining…" page can show real-time progress. The Worker's
# /api/progress endpoint checks the Bearer token against GH_DISPATCH_TOKEN.
PROGRESS_URL = os.environ.get(
    "STATS_PROGRESS_URL",
    "https://githubusers.archivebox.io/api/progress",
)
PROGRESS_TOKEN = os.environ.get("STATS_PROGRESS_TOKEN", "")

# Known author emails pirate has used over the years.
PIRATE_EMAILS = {
    "nikisweeting@gmail.com",
    "githubpirate@gmail.com",
    "nickwentboom@gmail.com",
    "nick@sweeting.me",
    "git@sweeting.me",
    "github@sweeting.me",
    "git@nicksweeting.com",
    "root@home.sweeting.me",
    "pirate@browserbase.com",
    "511499+pirate@users.noreply.github.com",
    "pirate@users.noreply.github.com",
}

# Substrings that strongly indicate an author entry belongs to pirate.
PIRATE_NEEDLES = (
    "sweeting",
    "githubpirate",
    "nickwentboom",
    "pirate@browserbase",
    "511499+pirate",
)

# Whether to render personalized sections (career timeline, company colors).
# Set to False for "generic" runs of arbitrary GH users.
PERSONALIZED = True

ROOT = Path(__file__).resolve().parent
# Cache paths are namespaced per user (default = pirate's). Re-bound in
# rebind_cache_paths() after load_config_from_file / auto_derive_config.
CACHE = ROOT / "cache"
CACHE_REPOS = CACHE / "repos"
CACHE_BARE = CACHE / "bare"
CACHE_API = CACHE / "api"
CACHE_AGG = CACHE / "commits_all.json"
TEMPLATE_FILE = ROOT / "stats_template.html"
OUTPUT_FILE = ROOT / "stats.html"


def rebind_cache_paths() -> None:
    """Switch cache + output paths to be user-namespaced when running for
    a non-default user. Pirate's default cache (cache/) stays in place;
    other users get cache_<login>/ + stats_<login>.html so re-runs don't
    clobber each other."""
    global CACHE, CACHE_REPOS, CACHE_BARE, CACHE_API, CACHE_AGG, OUTPUT_FILE
    if GH_LOGIN == "pirate":
        # Backwards compat for the original owner's files.
        return
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", GH_LOGIN)
    CACHE = ROOT / f"cache_{safe}"
    CACHE_REPOS = CACHE / "repos"
    CACHE_BARE = CACHE / "bare"
    CACHE_API = CACHE / "api"
    CACHE_AGG = CACHE / "commits_all.json"
    OUTPUT_FILE = ROOT / f"stats_{safe}.html"

SEARCH_DIRS = [
    Path("/Users/squash/Local"),
    Path("/Users/squash/Documents"),
]

# Path patterns to skip (vendored / nested git repos that are dependencies).
EXCLUDE_PATH_PATTERNS = (
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/.cache/",
    "/__pycache__/",
    "/site-packages/",
    "/data/archive/",      # ArchiveBox snapshot mirrors
    "/lolcommits/",
    "/.bash-prompt/",      # oh-my-zsh themes
    "/.oh-my-zsh/",
    "/.oh-my-fish/",
    "/bash-utils/",
)

# File-path substrings excluded from line counts (vendored / generated /
# locked / binary files). Matched as substring against the file path
# reported by `git log --numstat`. Keep these as substrings so paths like
# "frontend/node_modules/foo.js" or "src/dist/bundle.js" match.
EXCLUDE_FILE_SUBSTRINGS = (
    # Vendored / dependency directories
    "node_modules/", "vendor/", "bower_components/", "third_party/",
    ".venv/", "venv/", "env/", "site-packages/", "__pycache__/",
    # Build / dist / generated output
    "dist/", "build/", "target/", "out/",
    ".next/", ".nuxt/", ".turbo/", ".parcel-cache/",
    "coverage/", "htmlcov/", ".pytest_cache/", ".mypy_cache/",
    ".tox/", ".eggs/", ".egg-info/",
    # ArchiveBox snapshot dirs (huge data dumps)
    "/archive/", "/snapshots/",
)

# File-name (basename) exact matches — lock files etc.
EXCLUDE_FILE_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock", "pylock.toml",
    "Gemfile.lock", "composer.lock", "Cargo.lock", "go.sum",
    "Podfile.lock", "mix.lock", "flake.lock", "deno.lock", "bun.lockb",
    ".gitmodules", ".DS_Store",
}

# File extensions excluded — binaries, media, archives, minified.
EXCLUDE_FILE_EXTS = {
    # Compiled / bytecode
    ".pyc", ".pyo", ".pyd", ".so", ".o", ".a", ".class", ".dll", ".dylib",
    ".jar", ".war", ".ear",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".ico", ".heic", ".heif", ".avif", ".raw",
    # Vector graphics (often huge embedded data)
    ".svg",
    # Video / audio
    ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".flv",
    ".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".opus",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz", ".tbz",
    # Documents (binary)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Minified / bundled / sourcemaps
    ".min.js", ".min.css", ".bundle.js", ".chunk.js", ".map",
    # Misc binaries
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3", ".mmdb",
    # Generated/exported data dumps
    ".sql.gz", ".dump", ".pickle", ".pkl", ".parquet", ".feather",
    ".h5", ".hdf5", ".npy", ".npz", ".pt", ".pth", ".onnx",
}


EXT_TO_LANG = {
    ".py": "Python", ".pyi": "Python", ".ipynb": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby", ".erb": "Ruby",
    ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin",
    ".swift": "Swift",
    ".m": "Objective-C", ".mm": "Objective-C",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++",
    ".cs": "C#",
    ".php": "PHP",
    ".scala": "Scala",
    ".clj": "Clojure", ".cljs": "Clojure",
    ".lua": "Lua",
    ".erl": "Erlang", ".hrl": "Erlang",
    ".ex": "Elixir", ".exs": "Elixir",
    ".dart": "Dart",
    ".hs": "Haskell",
    ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "CSS", ".sass": "CSS", ".less": "CSS",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".astro": "Astro",
    ".md": "Markdown", ".mdx": "Markdown",
    ".rst": "reST",
    ".tex": "TeX",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".xml": "XML",
    ".proto": "Protobuf",
    ".graphql": "GraphQL", ".gql": "GraphQL",
    ".dockerfile": "Docker",
    ".tf": "Terraform", ".hcl": "HCL",
    ".vim": "Vim script",
    ".nix": "Nix",
    ".zig": "Zig",
    ".cr": "Crystal",
    ".jl": "Julia",
    ".r": "R",
    ".pl": "Perl", ".pm": "Perl",
    ".coffee": "CoffeeScript",
    ".elm": "Elm",
    ".purs": "PureScript",
    ".fs": "F#", ".fsi": "F#", ".fsx": "F#",
    ".ml": "OCaml", ".mli": "OCaml",
    ".re": "ReasonML", ".rei": "ReasonML",
    ".sol": "Solidity",
    ".move": "Move",
    ".cairo": "Cairo",
    ".v": "Verilog", ".sv": "SystemVerilog", ".vhdl": "VHDL",
    ".asm": "Assembly", ".s": "Assembly",
    ".lisp": "Lisp", ".scm": "Scheme",
    ".raku": "Raku",
}


def lang_for(path: str) -> str:
    """Return a coarse language label for a file path. 'Other' for unknown."""
    if not path:
        return "Other"
    base = path.rsplit("/", 1)[-1].lower()
    if base == "dockerfile" or base.startswith("dockerfile."):
        return "Docker"
    if base == "makefile" or base.startswith("makefile."):
        return "Make"
    plower = path.lower()
    # Compound suffixes first (e.g., .test.ts)
    for ext, name in EXT_TO_LANG.items():
        if plower.endswith(ext):
            return name
    return "Other"


def _is_excluded_file(path: str) -> bool:
    """Return True if the file path matches an exclusion pattern."""
    if not path:
        return False
    # Substring (directory) match
    for sub in EXCLUDE_FILE_SUBSTRINGS:
        if sub in path:
            return True
    # Basename exact match
    base = path.rsplit("/", 1)[-1]
    if base in EXCLUDE_FILE_BASENAMES:
        return True
    # Extension match (lowercase, support compound suffixes like .min.js)
    plower = path.lower()
    for ext in EXCLUDE_FILE_EXTS:
        if plower.endswith(ext):
            return True
    return False

# Repo path → display name fallback when remote is unavailable.
def repo_display_name(local_path: Path, remote_url: str | None) -> str:
    if remote_url:
        # git@github.com:foo/bar.git or https://github.com/foo/bar.git
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url)
        if m:
            return m.group(1)
    return local_path.name


# ---------------------------------------------------------------------------
# Local git mining
# ---------------------------------------------------------------------------

def find_local_repos(search_dirs: list[Path]) -> list[Path]:
    """Find all .git directories under the given search roots.
    Returns repos ordered by HEAD mtime descending (most recently active first)
    so that incremental rendering shows the user's recent activity quickly."""
    seen: set[Path] = set()
    repos: list[Path] = []
    for root in search_dirs:
        if not root.exists():
            continue
        try:
            out = subprocess.run(
                ["find", str(root), "-maxdepth", "8", "-type", "d", "-name", ".git"],
                capture_output=True, text=True, timeout=180,
            ).stdout
        except subprocess.TimeoutExpired:
            print(f"  ! find timed out on {root}", file=sys.stderr)
            continue
        for line in out.splitlines():
            git_dir = Path(line)
            repo_dir = git_dir.parent
            sline = str(repo_dir) + "/"
            if any(p in sline for p in EXCLUDE_PATH_PATTERNS):
                continue
            if repo_dir in seen:
                continue
            seen.add(repo_dir)
            repos.append(repo_dir)

    # Sort by .git/HEAD mtime descending (most recent activity first).
    def sort_key(p: Path) -> float:
        try:
            head = p / ".git" / "HEAD"
            if head.exists():
                return -head.stat().st_mtime
            return 0.0
        except Exception:
            return 0.0
    repos.sort(key=sort_key)
    return repos


def git_remote_url(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        url = out.stdout.strip()
        return url or None
    except Exception:
        return None


def mine_local_repo(repo: Path) -> list[dict]:
    """Return a list of commit records authored by the user in this repo."""
    author_pattern = r"sweeting\|githubpirate\|pirate@\|nicksweeting"

    # Quick existence check first — git stops walking on first match.
    # HEAD-only is fast on any repo; if no hit, try --all with shorter timeout.
    def precheck(refs_args: list[str], timeout: int) -> bool | None:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "log", *refs_args, "-i",
                 f"--author={author_pattern}", "-n", "1", "--format=%H"],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        return bool(r.stdout.strip())

    head_match = precheck([], timeout=20)
    if head_match is None:
        print(f"  ! pre-check (HEAD) timed out on {repo}", file=sys.stderr)
        return []
    if head_match is False:
        all_match = precheck(["--all"], timeout=30)
        if not all_match:
            return []

    # Try fast path first (default branch only).
    def run_log(extra_args: list[str], timeout: int) -> str | None:
        cmd = [
            "git", "-C", str(repo), "log", "--no-merges", "-i",
            f"--author={author_pattern}",
            "--pretty=format:__C__%H|%aI|%aE|%aN|%s",
            "--numstat",
        ] + extra_args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0:
            return None
        return r.stdout

    # Prefer --all (covers HEAD + all branches/tags). Fall back to HEAD-only
    # if --all is slow (lots of refs / I/O contention).
    output = run_log(["--all"], timeout=90)
    if output is None:
        print(f"  ! --all slow on {repo} — falling back to HEAD", file=sys.stderr)
        output = run_log([], timeout=30)
    if output is None:
        print(f"  ! git log failed on {repo}", file=sys.stderr)
        return []

    out_stdout = output

    remote = git_remote_url(repo)
    name = repo_display_name(repo, remote)

    records: list[dict] = []
    current: dict | None = None

    for line in out_stdout.split("\n"):
        if not line:
            continue
        if line.startswith("__C__"):
            # Flush previous record.
            if current is not None:
                records.append(current)
            try:
                sha, iso_date, email, author_name, subject = line[5:].split("|", 4)
            except ValueError:
                current = None
                continue
            # Check pirate-ness.
            ident = f"{author_name} {email}".lower()
            is_pirate = (
                email.lower() in PIRATE_EMAILS
                or any(n in ident for n in PIRATE_NEEDLES)
            )
            if not is_pirate:
                current = None
                continue
            current = {
                "sha": sha,
                "date": iso_date,
                "email": email,
                "author": author_name,
                "subject": subject,
                "additions": 0,
                "deletions": 0,
                "files": 0,
                "by_lang": {},
                "repo": name,
                "repo_local_path": str(repo),
                "repo_remote": remote,
                "source": "local",
            }
        else:
            if current is None:
                continue
            # numstat line: "<add>\t<del>\t<path>" — add/del may be "-" for binary.
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            a, d, path = parts
            # Skip vendored / lock / generated files from line counts.
            if _is_excluded_file(path):
                continue
            add_i = int(a) if a.isdigit() else 0
            del_i = int(d) if d.isdigit() else 0
            current["additions"] += add_i
            current["deletions"] += del_i
            current["files"] += 1
            lang = lang_for(path)
            bl = current["by_lang"].setdefault(lang, [0, 0])
            bl[0] += add_i
            bl[1] += del_i

    if current is not None:
        records.append(current)
    return records


def mine_all_local(
    repos: list[Path], force: bool = False,
    incremental_render_every: int = 0,
) -> dict[str, list[dict]]:
    """Mine each repo, caching results by repo path. Returns {repo_path: records}.
    Loads ALL cached repos upfront so incremental renders include history that
    isn't part of this iteration's processing order."""
    CACHE_REPOS.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {}

    # Preload every cache file (covers repos no longer on disk, or whose
    # turn hasn't come yet in this iteration).
    for cf in CACHE_REPOS.glob("*.json"):
        try:
            results[cf.stem] = json.loads(cf.read_text())
        except Exception:
            pass

    new_count = 0
    for i, repo in enumerate(repos, 1):
        key = str(repo).replace("/", "__").lstrip("__")
        cache_file = CACHE_REPOS / f"{key}.json"
        if cache_file.exists() and not force:
            try:
                results[str(repo)] = json.loads(cache_file.read_text())
                continue
            except Exception:
                pass
        print(f"  [{i}/{len(repos)}] {repo}", file=sys.stderr)
        records = mine_local_repo(repo)
        cache_file.write_text(json.dumps(records))
        results[str(repo)] = records
        new_count += 1
        if incremental_render_every and new_count % incremental_render_every == 0:
            try:
                all_recs = [r for rs in results.values() for r in rs]
                merged = dedupe_commits(all_recs)
                agg = aggregate(merged)
                OUTPUT_FILE.write_text(render_html(agg))
                print(f"    -- incremental: {len(merged)} commits, {agg['totals']['repos']} repos --",
                      file=sys.stderr)
            except Exception as e:
                print(f"    incremental render failed: {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# GitHub API mining
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def gh_api(path: str, *, paginate: bool = False, jq: str | None = None) -> object:
    """Run `gh api` and return parsed JSON.
    For multi-result jq selectors, pass jq that pipes through `@json` to get
    one compact JSON value per line — gh's default pretty-prints across lines,
    which breaks line-by-line parsing.
    Returns a list when jq is set or when paginate yields concatenated objects;
    a parsed scalar/dict/list otherwise."""
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    if jq:
        # Force compact one-line output for stream parsing.
        if "@json" not in jq:
            jq = f"{jq} | @json"
        cmd += ["--jq", jq]
    env = {**os.environ, "NO_COLOR": "1", "CLICOLOR": "0", "GH_NO_COLOR": "1"}
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"gh api failed: {path}\n{out.stderr}")
    text = ANSI_RE.sub("", out.stdout).strip()
    if not text:
        return [] if paginate else None
    if jq:
        # Each line is a JSON value (or, if jq yields a multi-line value, parse).
        results = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return results
    # Try one-shot parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Parse concatenated JSON values via raw_decode.
    dec = json.JSONDecoder()
    pos = 0
    n = len(text)
    parts = []
    while pos < n:
        while pos < n and text[pos] in " \t\n\r":
            pos += 1
        if pos >= n:
            break
        try:
            val, end = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        parts.append(val)
        pos = end
    # If every part is a list (paginated array endpoint), flatten.
    if parts and all(isinstance(p, list) for p in parts):
        merged = []
        for p in parts:
            merged.extend(p)
        return merged
    return parts


def list_owned_repos() -> list[dict]:
    """List all repos owned by the configured user (public + private if
    authenticated)."""
    cache_file = CACHE_API / "owned_repos.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    print(f"  fetching owned repos for @{GH_LOGIN} ...", file=sys.stderr)
    if PERSONALIZED:
        data = gh_api("/user/repos?per_page=100&affiliation=owner&sort=updated",
                      paginate=True)
    else:
        data = gh_api(f"/users/{GH_LOGIN}/repos?per_page=100&sort=updated",
                      paginate=True)
    repos = data if isinstance(data, list) else []
    CACHE_API.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(repos))
    return repos


def list_accessible_repos() -> list[dict]:
    """List all repos for the configured user. For the authenticated user
    (PERSONALIZED=True), gets owner+collaborator+organization_member access.
    For other users (--user mode), only their PUBLIC owned repos are
    available via /users/{login}/repos."""
    cache_file = CACHE_API / "accessible_repos.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    if PERSONALIZED:
        print("  fetching all accessible repos for authenticated user ...",
              file=sys.stderr)
        data = gh_api("/user/repos?per_page=100"
                      "&affiliation=owner,collaborator,organization_member"
                      "&sort=updated", paginate=True)
    else:
        print(f"  fetching public repos for @{GH_LOGIN} ...", file=sys.stderr)
        data = gh_api(f"/users/{GH_LOGIN}/repos?per_page=100&sort=updated",
                      paginate=True)
    repos = data if isinstance(data, list) else []
    CACHE_API.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(repos))
    return repos


def list_repo_commits_by_author(full_name: str) -> list[dict]:
    """List commit SHAs in a repo authored by the user (uses commits API).
    Returns minimal commit records (no stats yet)."""
    cache_file = CACHE_API / "repo_commits" / f"{full_name.replace('/', '__')}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    try:
        items = gh_api(
            f"/repos/{full_name}/commits?author={GH_LOGIN}&per_page=100",
            paginate=True, jq=".[]",
        )
    except RuntimeError as e:
        print(f"    ! list commits {full_name}: {e}", file=sys.stderr)
        return []
    items = items or []
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(items))
    return items


def mine_github_accessible(known_shas: set[str],
                           max_repos: int = 1000,
                           max_fetches: int = 5000) -> list[dict]:
    """For each repo pirate has access to, list his commits via commits API
    and fetch per-commit stats. Returns list of records not already in
    known_shas. Cache-friendly."""
    repos = list_accessible_repos()
    print(f"  {len(repos)} accessible repos", file=sys.stderr)
    records: list[dict] = []
    fetches = 0
    for i, r in enumerate(repos[:max_repos], 1):
        full = r.get("full_name")
        if not full:
            continue
        commits = list_repo_commits_by_author(full)
        if not commits:
            continue
        new_shas = [c["sha"] for c in commits if c.get("sha") not in known_shas]
        if not new_shas:
            continue
        print(f"    [{i}/{len(repos)}] {full}: {len(new_shas)} new commits",
              file=sys.stderr)
        for sha in new_shas:
            if fetches >= max_fetches:
                print("  ! reached max_fetches", file=sys.stderr)
                return records
            stats_data = fetch_commit_stats(full, sha)
            fetches += 1
            if not stats_data:
                continue
            add_f, del_f, n_f, bl_f = _stats_from_commit(stats_data)
            commit_meta = stats_data.get("commit") or {}
            author = commit_meta.get("author") or {}
            records.append({
                "sha": sha,
                "date": author.get("date"),
                "email": author.get("email"),
                "author": author.get("name"),
                "subject": (commit_meta.get("message") or "").split("\n")[0],
                "additions": add_f,
                "deletions": del_f,
                "files": n_f,
                "by_lang": bl_f,
                "repo": full,
                "repo_local_path": None,
                "repo_remote": f"https://github.com/{full}.git",
                "source": "github_commits_api",
            })
            known_shas.add(sha)
    return records


def _wait_for_search_quota(min_remaining: int = 11):
    """Sleep until search quota recovers to at least min_remaining."""
    while True:
        try:
            r = gh_api("/rate_limit")
        except RuntimeError:
            time.sleep(5); continue
        s = (r or {}).get("resources", {}).get("search", {})
        rem = s.get("remaining", 30)
        reset = s.get("reset", 0)
        if rem >= min_remaining:
            return
        wait = max(2, int(reset - time.time()) + 2)
        print(f"    search quota low ({rem}); waiting {wait}s",
              file=sys.stderr)
        time.sleep(min(wait, 70))


def list_search_commits() -> list[dict]:
    """Use search/commits API to discover repos pirate has contributed to.
    The search API is capped at 1000 results per query, so we page by year.
    Rate limit is 30 req/min (primary) — we wait when quota gets low.
    GitHub also enforces secondary rate limits (HTTP 403 'secondary rate
    limit') for bursts; we retry those with exponential backoff and write
    incrementally per-year so progress isn't lost."""
    final_cache = CACHE_API / "search_commits.json"
    if final_cache.exists():
        return json.loads(final_cache.read_text())
    # Per-year cache → resumable.
    per_year_dir = CACHE_API / "search_by_year"
    per_year_dir.mkdir(parents=True, exist_ok=True)
    print("  searching commits authored by the user ...", file=sys.stderr)
    all_items: list[dict] = []
    # Page by year (1000 cap per query). When a year approaches the cap,
    # re-page by month to capture more unique results.
    def query(date_filter: str) -> list[dict]:
        _wait_for_search_quota(11)
        q = f"author:{GH_LOGIN}+author-date:{date_filter}"
        try:
            r = gh_api(
                f"/search/commits?q={q}&per_page=100&sort=author-date",
                paginate=True, jq=".items[]",
            )
        except RuntimeError as e:
            print(f"    ! query {date_filter} failed: {e}", file=sys.stderr)
            return []
        return r or []

    for year in range(2010, datetime.now().year + 1):
        items = query(f"{year}-01-01..{year}-12-31")
        if len(items) >= 990:
            # Hit (or nearly hit) the 1000 cap — sub-page by month.
            print(f"    {year}: {len(items)} (capped) — re-paging by month",
                  file=sys.stderr)
            items = []
            for month in range(1, 13):
                # Actual last day of month — accept slight overshoot via "<"
                if month == 12:
                    next_first = date(year + 1, 1, 1)
                else:
                    next_first = date(year, month + 1, 1)
                last = next_first - timedelta(days=1)
                first = date(year, month, 1)
                month_items = query(f"{first.isoformat()}..{last.isoformat()}")
                print(f"      {first.isoformat()[:7]}: {len(month_items)}",
                      file=sys.stderr)
                items.extend(month_items)
                time.sleep(0.3)
        print(f"    {year}: {len(items)} results", file=sys.stderr)
        all_items.extend(items)
        time.sleep(0.3)
    CACHE_API.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(all_items))
    return all_items


def list_prs_and_issues() -> tuple[list[dict], list[dict]]:
    """Use search/issues API to find all PRs and issues authored by the user.
    Returns (prs, issues). Cached per-kind, paged per year, with secondary
    rate-limit retry. Resumable via per-year cache files."""
    out: dict[str, list[dict]] = {}
    for kind in ("pr", "issue"):
        cache_file = CACHE_API / f"{kind}s.json"
        if cache_file.exists():
            out[kind] = json.loads(cache_file.read_text())
            continue
        per_year_dir = CACHE_API / f"{kind}s_by_year"
        per_year_dir.mkdir(parents=True, exist_ok=True)
        print(f"  searching {kind}s authored by the user ...", file=sys.stderr)
        all_items: list[dict] = []
        for year in range(2010, datetime.now().year + 1):
            year_file = per_year_dir / f"{year}.json"
            if year_file.exists():
                items = json.loads(year_file.read_text())
                all_items.extend(items)
                continue
            _wait_for_search_quota(11)
            q = (f"author:{GH_LOGIN}+is:{kind}"
                 f"+created:{year}-01-01..{year}-12-31")
            try:
                items = gh_api(
                    f"/search/issues?q={q}&per_page=100",
                    paginate=True, jq=".items[]",
                )
            except RuntimeError as e:
                msg = str(e).lower()
                if "secondary rate" in msg or "403" in msg:
                    print(f"    secondary rate limit on {kind} {year} — sleeping 90s",
                          file=sys.stderr)
                    time.sleep(90)
                    try:
                        items = gh_api(
                            f"/search/issues?q={q}&per_page=100",
                            paginate=True, jq=".items[]",
                        )
                    except RuntimeError as e2:
                        print(f"    ! {kind} {year} failed twice: {e2}",
                              file=sys.stderr)
                        items = []
                else:
                    print(f"    ! {kind} {year} failed: {e}", file=sys.stderr)
                    items = []
            items = items or []
            print(f"    {kind} {year}: {len(items)}", file=sys.stderr)
            year_file.write_text(json.dumps(items))
            all_items.extend(items)
            time.sleep(0.3)
        cache_file.write_text(json.dumps(all_items))
        out[kind] = all_items
    return out["pr"], out["issue"]


def _stats_from_commit(stats_data: dict) -> tuple[int, int, int, dict]:
    """Compute (additions, deletions, files_count, by_lang) from a GH commit
    detail, excluding vendored/lock/generated files. by_lang = {lang: [add, del]}."""
    files = stats_data.get("files") or []
    if not files:
        s = stats_data.get("stats") or {}
        return int(s.get("additions") or 0), int(s.get("deletions") or 0), 0, {}
    add, dlt, n = 0, 0, 0
    by_lang: dict[str, list[int]] = {}
    for f in files:
        path = f.get("filename", "")
        if _is_excluded_file(path):
            continue
        fa = int(f.get("additions") or 0)
        fd = int(f.get("deletions") or 0)
        add += fa
        dlt += fd
        n += 1
        bl = by_lang.setdefault(lang_for(path), [0, 0])
        bl[0] += fa
        bl[1] += fd
    return add, dlt, n, by_lang


def fetch_commit_stats(full_name: str, sha: str) -> dict | None:
    """Fetch a single commit's stats via the GitHub API, cached."""
    cache_file = CACHE_API / "commit_stats" / f"{full_name.replace('/', '__')}__{sha}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    try:
        data = gh_api(f"/repos/{full_name}/commits/{sha}")
    except RuntimeError as e:
        return None
    if not isinstance(data, dict):
        return None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    return data


def mine_github_search(items: list[dict], known_shas: set[str],
                       max_fetches: int = 5000) -> list[dict]:
    """For each search/commits item whose SHA isn't already known, fetch
    stats via the per-commit API and build a record."""
    records: list[dict] = []
    fetches = 0
    # Dedup search results by (repo, sha) — forks can appear multiple times.
    seen: set[tuple[str, str]] = set()
    for it in items:
        repo_info = it.get("repository") or {}
        full_name = repo_info.get("full_name")
        sha = it.get("sha")
        if not full_name or not sha:
            continue
        if sha in known_shas:
            continue
        key = (full_name, sha)
        if key in seen:
            continue
        seen.add(key)
        if fetches >= max_fetches:
            print(f"  ! reached max_fetches={max_fetches}, stopping", file=sys.stderr)
            break
        stats_data = fetch_commit_stats(full_name, sha)
        fetches += 1
        if fetches % 25 == 0:
            print(f"    fetched stats for {fetches} commits", file=sys.stderr)
        if not stats_data:
            continue
        add_f, del_f, n_f, bl_f = _stats_from_commit(stats_data)
        commit_meta = stats_data.get("commit") or {}
        author = commit_meta.get("author") or {}
        records.append({
            "sha": sha,
            "date": author.get("date"),
            "email": author.get("email"),
            "author": author.get("name"),
            "subject": (commit_meta.get("message") or "").split("\n")[0],
            "additions": add_f,
            "deletions": del_f,
            "files": n_f,
            "by_lang": bl_f,
            "repo": full_name,
            "repo_local_path": None,
            "repo_remote": f"https://github.com/{full_name}.git",
            "source": "github_search",
        })
    return records


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    """Lowercase name with non-word chars → hyphens, stripped."""
    n = name.split("/")[-1]  # strip owner prefix if any
    return re.sub(r"[\W_]+", "-", n).lower().strip("-")


# Manual canonical-name overrides — for repos whose upstream org was
# deleted, so GH no longer knows about the fork relationship.
MANUAL_CANONICAL = {
    "pirate/drchrono-web": "drchrono/drchrono-web",
    "pirate/DeliveryHeroChina": "DeliveryHero/DeliveryHeroChina",
    "lucase/DeliveryHeroChina": "DeliveryHero/DeliveryHeroChina",
    "pirate/cmdty.ncm-ui": "Monadical-Inc/cmdty.ncm-ui",
}


_RENAME_CACHE_FILE = None  # set in resolve_canonical_name
def resolve_canonical_name(full_name: str) -> str:
    """Query GitHub for the current canonical name of a repo. If the repo
    was renamed, GitHub redirects and returns the new full_name. Cached."""
    global _RENAME_CACHE_FILE
    if _RENAME_CACHE_FILE is None:
        _RENAME_CACHE_FILE = CACHE_API / "renames.json"
    cache: dict[str, str] = {}
    if _RENAME_CACHE_FILE.exists():
        try:
            cache = json.loads(_RENAME_CACHE_FILE.read_text())
        except Exception:
            pass
    # Manual override (for cases where GH lost the parent relationship).
    if full_name in MANUAL_CANONICAL:
        canon = MANUAL_CANONICAL[full_name]
        cache[full_name] = canon
        _RENAME_CACHE_FILE.write_text(json.dumps(cache))
        return canon
    if full_name in cache:
        return cache[full_name]
    if "/" not in full_name:
        return full_name
    try:
        r = gh_api(f"/repos/{full_name}")
        if isinstance(r, dict) and r.get("full_name"):
            canon = r["full_name"]
            cache[full_name] = canon
            _RENAME_CACHE_FILE.write_text(json.dumps(cache))
            return canon
    except RuntimeError:
        pass
    cache[full_name] = full_name
    _RENAME_CACHE_FILE.write_text(json.dumps(cache))
    return full_name


def _build_repo_alias_map(records: list[dict]) -> dict[str, str]:
    """Find repos that share >50% of their SHAs (i.e. forks/clones of each
    other) and alias them to a single canonical name. Also matches by name
    slug for repos with no remote (local-only clones with no upstream
    info) — e.g. local "Security Growler" → "pirate/security-growler".

    Canonical choice priority:
      1. Has a github.com remote (so we can link to it)
      2. Repo with the most unique SHAs (the "fullest" history)
      3. Tiebreak: prefer non-pirate/* owner (upstream beats fork)."""
    from collections import defaultdict
    sha_by_repo: dict[str, set[str]] = defaultdict(set)
    has_gh_remote: dict[str, bool] = {}
    for r in records:
        sha = r.get("sha")
        if not sha:
            continue
        rc = repo_canonical(r)
        sha_by_repo[rc].add(sha)
        remote = (r.get("repo_remote") or "")
        if rc not in has_gh_remote:
            has_gh_remote[rc] = False
        if "github.com" in remote:
            has_gh_remote[rc] = True

    # Union-find clustering by SHA overlap.
    parent = {n: n for n in sha_by_repo}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb

    names = list(sha_by_repo.keys())

    # First pass: resolve GitHub canonical names (handles renames).
    # Repos that resolve to the same canonical name → same cluster.
    canonical_by_name: dict[str, str] = {}
    print(f"  resolving canonical names for {len([n for n in names if has_gh_remote.get(n)])} repos...",
          file=sys.stderr)
    for n in names:
        if has_gh_remote.get(n) and "/" in n:
            canonical_by_name[n] = resolve_canonical_name(n)
        else:
            canonical_by_name[n] = n
    # Union by GH-canonical name
    canon_to_names: dict[str, list[str]] = defaultdict(list)
    for n, c in canonical_by_name.items():
        canon_to_names[c].append(n)
    for canon, group in canon_to_names.items():
        if len(group) > 1:
            for n in group[1:]:
                union(group[0], n)

    # Second pass: SHA-overlap (covers forks like pirate/FOCS ↔ Mavrx-inc/FOCS)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            sa, sb = sha_by_repo[a], sha_by_repo[b]
            inter = len(sa & sb)
            if inter == 0:
                continue
            # Strong overlap: >50% of either side's SHAs
            if inter / min(len(sa), len(sb)) > 0.5:
                union(a, b)

    # Pass 2.5: fork-parent matching — if pirate/X is a fork of upstream/X,
    # merge them. Reads cached repo_detail (which we fetch for stars anyway).
    # Catches cases like pirate/Gmail_GeoIPTagger (fork w/ local commits)
    # ↔ benjojo/Gmail_GeoIPTagger (upstream w/ pirate's merged PR).
    detail_dir = CACHE_API / "repo_detail"
    if detail_dir.exists():
        for df in detail_dir.glob("*.json"):
            try:
                rd = json.loads(df.read_text())
            except Exception:
                continue
            this_name = rd.get("full_name")
            upstream_name = (rd.get("parent") or {}).get("full_name")
            if not this_name or not upstream_name:
                continue
            # Ensure both nodes exist in union-find structure AND in the
            # cluster-iteration list (names) so they get clustered.
            for n in (this_name, upstream_name):
                if n not in parent:
                    parent[n] = n
                    sha_by_repo.setdefault(n, set())
                    has_gh_remote.setdefault(n, True)
                    canonical_by_name.setdefault(n, n)
                    names.append(n)
            union(this_name, upstream_name)

    # Second pass: name-slug matching ONLY for repos missing a GH remote.
    # Each local-only repo can be merged into a corresponding GH-backed
    # repo with the same slug. Repos that BOTH have GH remotes are NOT
    # merged via this pass (different repos can share a name like "core").
    slug_to_names: dict[str, list[str]] = defaultdict(list)
    for n in names:
        slug_to_names[_slug(n)].append(n)
    for slug, group in slug_to_names.items():
        if len(group) < 2:
            continue
        local_only = [g for g in group if not has_gh_remote.get(g)]
        with_gh = [g for g in group if has_gh_remote.get(g)]
        if not local_only or not with_gh:
            continue
        # Only merge local-only entries into the first GH-backed one.
        canonical = with_gh[0]
        for n in local_only:
            union(n, canonical)

    # Build canonical for each cluster.
    clusters: dict[str, list[str]] = defaultdict(list)
    for n in names:
        clusters[find(n)].append(n)

    # Collect fork→parent map from repo_detail cache for the canonical_score.
    fork_parent: dict[str, str] = {}
    if detail_dir.exists():
        for df in detail_dir.glob("*.json"):
            try:
                rd = json.loads(df.read_text())
            except Exception:
                continue
            tn = rd.get("full_name")
            pn = (rd.get("parent") or {}).get("full_name")
            if tn and pn:
                fork_parent[tn] = pn

    def canonical_score(name: str) -> tuple:
        unique = len(sha_by_repo.get(name, set()))
        has_remote = has_gh_remote.get(name, False)
        is_gh_canonical = canonical_by_name.get(name) == name
        is_fork = name in fork_parent
        is_pirate_fork = name.lower().startswith("pirate/")
        # Lower tuple is better. Priority:
        #  1. Name is its own GH-canonical (not renamed away from)
        #  2. NOT a fork (prefer upstream over its forks)
        #  3. Has a GH remote
        #  4. More unique SHAs
        #  5. Non-pirate/* owner
        #  6. Alphabetical
        return (
            0 if is_gh_canonical else 1,
            1 if is_fork else 0,
            0 if has_remote else 1,
            -unique,
            is_pirate_fork,
            name,
        )

    alias: dict[str, str] = {}
    for members in clusters.values():
        # Pick the canonical-scoring "best" from members ...
        best = members[0] if len(members) == 1 else min(members, key=canonical_score)
        # ... then if THAT name is itself a stale alias of a renamed/transferred
        # repo, redirect to the current canonical (which may be outside the
        # cluster — e.g. cluster={codimd/cli, hackmdio/codimd-cli} both have
        # rename targets, so we redirect to the target of the best member).
        final = canonical_by_name.get(best, best)
        for m in members:
            alias[m] = final
    return alias


_LAST_ALIAS_MAP: dict[str, str] = {}


def dedupe_commits(records: Iterable[dict]) -> list[dict]:
    """Dedupe by SHA, preferring records with the most detail (most files/lines).
    Also collapses fork-pair attribution so the same commit doesn't get split
    across two related repos (e.g. pirate/FOCS + Mavrx-inc/FOCS).
    Stores the alias map in _LAST_ALIAS_MAP so aggregate() can apply the same
    canonicalization to PR/issue repo names."""
    global _LAST_ALIAS_MAP
    records = list(records)
    alias = _build_repo_alias_map(records)
    _LAST_ALIAS_MAP = alias

    by_sha: dict[str, dict] = {}
    for r in records:
        sha = r.get("sha")
        if not sha:
            continue
        prev = by_sha.get(sha)
        if prev is None:
            by_sha[sha] = r
            continue
        # Prefer record with greater total line change (likely full data).
        prev_total = prev["additions"] + prev["deletions"]
        cur_total = r["additions"] + r["deletions"]
        if cur_total > prev_total:
            by_sha[sha] = r

    # Rewrite repo attribution to canonical (alias map).
    for r in by_sha.values():
        canon_input = repo_canonical(r)
        target = alias.get(canon_input, canon_input)
        if target != canon_input:
            r["repo"] = target
            # Use a synthetic remote pointing to the canonical owner/repo
            # if it looks like a github full_name.
            if "/" in target and not target.startswith("http"):
                r["repo_remote"] = f"https://github.com/{target}.git"
    return list(by_sha.values())


def repo_canonical(record: dict) -> str:
    """Canonical repo name for grouping (prefer GitHub full_name from remote)."""
    remote = record.get("repo_remote")
    if remote:
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote)
        if m:
            return m.group(1)
        # Other remote (gitlab, self-hosted)
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote)
        if m:
            return m.group(1)
    return record.get("repo") or "unknown"


REPO_JOB_PATTERNS = [
    # (regex, company)  — same as the JSON list above; duplicated here for
    # in-process aggregation. Kept in sync manually.
    (re.compile(r"^ArchiveBox/", re.I), "ArchiveBox.io"),
    (re.compile(r"^pirate/(ArchiveBox|abx|archive|wireguard|grab-site|browsertrix|archivebox)", re.I), "ArchiveBox.io"),
    (re.compile(r"^pirate/(canarytokens|webrtcchat|pocket|drivesync)", re.I), "ArchiveBox.io"),
    (re.compile(r"oddslingers", re.I), "OddSlingers Labs"),
    (re.compile(r"grater_experiment", re.I), "OddSlingers Labs"),
    (re.compile(r"^Monadical(-Inc|-SAS|-)?/", re.I), "Monadical"),
    (re.compile(r"^pirate/(redux-time|warped-time|inferno-redux-time|react-components|gzint|monadical|puppetmaster|currents|cmdty)", re.I), "Monadical"),
    (re.compile(r"currents\.fm|currents\.api|currents-", re.I), "Currents.fm"),
    (re.compile(r"^drchrono/|drchrono-web|onpatient-web|mdhunter|drchrono_chat|drchrono_public|drchrono-setup", re.I), "DrChrono"),
    (re.compile(r"Mavrx-inc/|^pirate/FOCS|toadstool", re.I), "Mavrx"),
    (re.compile(r"hotspot|expospot|stacey|moshly|spyce|china-vpn|freevpn", re.I), "ExpoSpot / Hotspot"),
    (re.compile(r"DeliveryHero", re.I), "DeliveryHero China"),
    (re.compile(r"^browser-use/|^pirate/browser-use", re.I), "Browser-Use"),
    (re.compile(r"^browserbase/|stagehand", re.I), "Browserbase"),
]


def repo_to_company(name: str) -> str | None:
    for pat, company in REPO_JOB_PATTERNS:
        if pat.search(name):
            return company
    return None


def load_star_counts() -> dict[str, int]:
    """Map full_name → stargazers_count from cached repo lists +
    repo-detail cache (filled on demand by `fill_star_counts`)."""
    stars: dict[str, int] = {}
    for fname in ("accessible_repos.json", "owned_repos.json"):
        f = CACHE_API / fname
        if not f.exists():
            continue
        try:
            for r in json.loads(f.read_text()):
                n = r.get("full_name")
                if n and "stargazers_count" in r:
                    stars[n] = max(stars.get(n, 0), int(r["stargazers_count"]))
        except Exception:
            pass
    # Layer on per-repo detail cache (covers public repos pirate doesn't own).
    detail_dir = CACHE_API / "repo_detail"
    if detail_dir.exists():
        for f in detail_dir.glob("*.json"):
            try:
                r = json.loads(f.read_text())
                n = r.get("full_name")
                if n and "stargazers_count" in r:
                    stars[n] = max(stars.get(n, 0), int(r["stargazers_count"]))
            except Exception:
                pass
    return stars


BARE_CLONE_DIR = CACHE / "bare"


def _bare_path(full_name: str) -> Path:
    return BARE_CLONE_DIR / (full_name.replace("/", "__") + ".git")


def bare_clone_repo(full_name: str, timeout: int = 300) -> bool:
    """Bare-clone a GitHub repo into cache/bare. Returns True on success or
    if already present. Falls back to https if ssh fails."""
    dest = _bare_path(full_name)
    if dest.exists():
        return True
    BARE_CLONE_DIR.mkdir(parents=True, exist_ok=True)
    for url in (
        f"git@github.com:{full_name}.git",
        f"https://github.com/{full_name}.git",
    ):
        try:
            r = subprocess.run(
                ["git", "clone", "--bare", "--quiet", url, str(dest)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
            continue
        if r.returncode == 0 and dest.exists():
            return True
        if dest.exists():
            subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
    return False


def mine_bare_repo_to_cache(bare: Path) -> int:
    """Mine a bare clone and write a per-repo cache entry. Returns # records.
    Skips if already cached."""
    key = "bare__" + bare.name
    cf = CACHE_REPOS / f"{key}.json"
    if cf.exists() and cf.stat().st_size > 10:
        return -1  # already mined
    recs = mine_local_repo(bare)
    # The bare-clone file name encodes the canonical full_name.
    raw = bare.name.replace(".git", "")
    if "_" in raw:
        owner, name = raw.split("_", 1)
        full = f"{owner}/{name}"
    else:
        full = raw
    for r in recs:
        r["repo"] = full
        r["repo_remote"] = f"https://github.com/{full}.git"
        r["source"] = "bare_clone"
    cf.write_text(json.dumps(recs))
    return len(recs)


def clone_and_mine_repos(repos: Iterable[str]) -> int:
    """For each full_name not already represented in cache, bare-clone and
    mine. Returns # of new repos mined."""
    n_new = 0
    repos = list(repos)
    print(f"  bare-cloning {len(repos)} repos ...", file=sys.stderr)
    for i, full in enumerate(repos, 1):
        if "/" not in full:
            continue
        if not bare_clone_repo(full):
            print(f"  ! clone failed: {full}", file=sys.stderr)
            continue
        bare = _bare_path(full)
        recs = mine_bare_repo_to_cache(bare)
        if recs > 0:
            n_new += 1
        if i % 10 == 0:
            print(f"    [{i}/{len(repos)}] {full}", file=sys.stderr)
    return n_new


def list_pr_commits(full_name: str, number: int) -> list[dict]:
    """List commits in a PR via /repos/{r}/pulls/{n}/commits (cached)."""
    cache_dir = CACHE_API / "pr_commits"
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{full_name.replace('/', '__')}__{number}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    try:
        items = gh_api(
            f"/repos/{full_name}/pulls/{number}/commits?per_page=100",
            paginate=True, jq=".[]",
        )
    except RuntimeError:
        return []
    items = items or []
    f.write_text(json.dumps(items))
    return items


def mine_commits_from_merged_prs(known_shas: set[str],
                                  max_fetches: int = 2000) -> int:
    """For every merged PR by the user, list its commits via the PR API and
    fetch stats for each. Captures pirate's contributions even when his
    commit author isn't linked to his GH user (e.g. old emails). Writes
    new records to a cache file."""
    pr_file = CACHE_API / "prs.json"
    if not pr_file.exists():
        return 0
    try:
        prs = json.loads(pr_file.read_text())
    except Exception:
        return 0
    merged = [p for p in prs
              if (p.get("pull_request") or {}).get("merged_at")]
    print(f"  scanning {len(merged)} merged PRs for commits ...",
          file=sys.stderr)
    new_records: list[dict] = []
    fetches = 0
    for i, p in enumerate(merged, 1):
        ru = p.get("repository_url", "")
        m = re.search(r"/repos/([^/]+/[^/]+)$", ru)
        if not m:
            continue
        full = m.group(1)
        num = p.get("number")
        if not num:
            continue
        pr_commits = list_pr_commits(full, num)
        # Filter to commits actually authored by the user (or his historical
        # emails) — not co-authors, not the merger's commits.
        for c in pr_commits:
            sha = c.get("sha")
            if not sha or sha in known_shas:
                continue
            commit = c.get("commit") or {}
            author = commit.get("author") or {}
            email = (author.get("email") or "").lower()
            name = (author.get("name") or "").lower()
            ident = f"{name} {email}"
            is_pirate = (
                email in PIRATE_EMAILS
                or any(n in ident for n in PIRATE_NEEDLES)
            )
            if not is_pirate:
                continue
            if fetches >= max_fetches:
                print(f"  ! pr-commit cap {max_fetches} reached",
                      file=sys.stderr)
                break
            stats_data = fetch_commit_stats(full, sha)
            fetches += 1
            if not stats_data:
                continue
            add_f, del_f, n_f, bl_f = _stats_from_commit(stats_data)
            cm = stats_data.get("commit") or {}
            ca = cm.get("author") or {}
            new_records.append({
                "sha": sha,
                "date": ca.get("date"),
                "email": ca.get("email"),
                "author": ca.get("name"),
                "subject": (cm.get("message") or "").split("\n")[0],
                "additions": add_f,
                "deletions": del_f,
                "files": n_f,
                "by_lang": bl_f,
                "repo": full,
                "repo_local_path": None,
                "repo_remote": f"https://github.com/{full}.git",
                "source": "pr_commits_api",
            })
            known_shas.add(sha)
        if i % 25 == 0:
            print(f"    [{i}/{len(merged)}] cumulative new: {len(new_records)}",
                  file=sys.stderr)
        if fetches >= max_fetches:
            break

    if new_records:
        cf = CACHE_REPOS / "_pr_commits.json"
        existing = []
        if cf.exists():
            try:
                existing = json.loads(cf.read_text())
            except Exception:
                pass
        existing.extend(new_records)
        cf.write_text(json.dumps(existing))
    return len(new_records)


def mine_commits_for_pr_repos(repo_names: Iterable[str],
                               known_shas: set[str],
                               max_fetches: int = 2000) -> int:
    """For each repo with pirate PRs, fetch all commits authored by the user
    via /repos/{r}/commits?author=pirate (cheap, paginated). Then fetch
    stats for each new SHA. Returns # of new commit records written."""
    repos = list(repo_names)
    print(f"  mining pirate's commits in {len(repos)} repos ...", file=sys.stderr)
    fetches = 0
    new_records: list[dict] = []
    for i, full in enumerate(repos, 1):
        if "/" not in full:
            continue
        commits = list_repo_commits_by_author(full)  # cached
        if not commits:
            continue
        new_shas = [c["sha"] for c in commits if c.get("sha") not in known_shas]
        if not new_shas:
            continue
        for sha in new_shas:
            if fetches >= max_fetches:
                print(f"  ! commit-stats cap {max_fetches} reached", file=sys.stderr)
                break
            stats_data = fetch_commit_stats(full, sha)
            fetches += 1
            if not stats_data:
                continue
            add_f, del_f, n_f, bl_f = _stats_from_commit(stats_data)
            cm = stats_data.get("commit") or {}
            author = cm.get("author") or {}
            new_records.append({
                "sha": sha,
                "date": author.get("date"),
                "email": author.get("email"),
                "author": author.get("name"),
                "subject": (cm.get("message") or "").split("\n")[0],
                "additions": add_f,
                "deletions": del_f,
                "files": n_f,
                "by_lang": bl_f,
                "repo": full,
                "repo_local_path": None,
                "repo_remote": f"https://github.com/{full}.git",
                "source": "pr_repo_commits_api",
            })
            known_shas.add(sha)
        if i % 10 == 0:
            print(f"    [{i}/{len(repos)}] {full}: {len(new_shas)} new commits",
                  file=sys.stderr)
        if fetches >= max_fetches:
            break

    # Persist these as a cache file so subsequent runs include them.
    if new_records:
        cf = CACHE_REPOS / "_pr_repo_commits.json"
        existing = []
        if cf.exists():
            try:
                existing = json.loads(cf.read_text())
            except Exception:
                pass
        existing.extend(new_records)
        cf.write_text(json.dumps(existing))
    return len(new_records)


def fetch_pr_details_for_merged(max_fetches: int = 2000) -> int:
    """For every merged PR in cache/api/prs.json, fetch its detail (which
    includes additions/deletions). Returns # of new fetches."""
    pr_file = CACHE_API / "prs.json"
    if not pr_file.exists():
        return 0
    try:
        prs = json.loads(pr_file.read_text())
    except Exception:
        return 0
    todo = []
    for p in prs:
        merged = bool((p.get("pull_request") or {}).get("merged_at"))
        if not merged:
            continue
        ru = p.get("repository_url", "")
        m = re.search(r"/repos/([^/]+/[^/]+)$", ru)
        if not m:
            continue
        full = m.group(1)
        num = p.get("number")
        if not num:
            continue
        cf = CACHE_API / "pr_detail" / f"{full.replace('/', '__')}__{num}.json"
        if cf.exists():
            continue
        todo.append((full, num))
    print(f"  fetching detail for {len(todo)} merged PRs ...", file=sys.stderr)
    n = 0
    for i, (full, num) in enumerate(todo, 1):
        if n >= max_fetches:
            print(f"  ! pr-detail cap {max_fetches} reached", file=sys.stderr)
            break
        if fetch_pr_detail(full, num):
            n += 1
        if i % 50 == 0:
            print(f"    [{i}/{len(todo)}] {full}#{num}", file=sys.stderr)
    return n


def fetch_stars_for_repos(repo_names: Iterable[str], max_fetches: int = 1000) -> int:
    """Ensure each given repo has its detail (stargazers_count + fork.parent
    info) cached. Returns # of new fetches.
    Also fetches detail for owned pirate forks (which we already have stars
    for, via owned_repos.json) because we need their `parent` field to merge
    fork↔upstream entries in the alias map."""
    repo_names = list(repo_names)
    detail_dir = CACHE_API / "repo_detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    # Set of repos already in detail cache.
    cached_detail = set()
    for f in detail_dir.glob("*.json"):
        cached_detail.add(f.stem.replace("__", "/", 1))

    # Also: owned pirate forks (need parent info).
    owned_file = CACHE_API / "owned_repos.json"
    extra: list[str] = []
    if owned_file.exists():
        try:
            for r in json.loads(owned_file.read_text()):
                if r.get("fork") and r.get("full_name") and \
                        r["full_name"] not in cached_detail:
                    extra.append(r["full_name"])
        except Exception:
            pass

    known = load_star_counts()
    missing = [n for n in repo_names if "/" in n and n not in known
               and n not in cached_detail]
    # Also include the fork-parent fetches.
    todo = sorted(set(missing) | set(extra))
    print(f"  fetching repo detail for {len(todo)} repos "
          f"({len(missing)} for stars, {len(extra)} for fork-parent) ...",
          file=sys.stderr)
    n = 0
    for i, name in enumerate(todo, 1):
        if n >= max_fetches:
            print(f"  ! detail-fetch cap {max_fetches} reached", file=sys.stderr)
            break
        if fetch_repo_detail(name):
            n += 1
        if i % 25 == 0:
            print(f"    [{i}/{len(todo)}] {name}", file=sys.stderr)
    return n


def fetch_repo_contrib_stats(full_name: str) -> dict | None:
    """Return {"total": N, "user": M} from the GH contributors API where N
    is the sum of all contributors' default-branch commits and M is the
    user's. Both come from the same source so share = M/N is apples-to-apples.
    Cached."""
    cache_dir = CACHE_API / "repo_total_commits"
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / (full_name.replace("/", "__") + ".json")
    if f.exists():
        try:
            d = json.loads(f.read_text())
            if isinstance(d, dict):
                return d
            # Legacy: file used to hold a bare int (total only).
            return {"total": int(d), "user": None}
        except Exception:
            pass
    try:
        contributors = gh_api(
            f"/repos/{full_name}/contributors?per_page=100&anon=1",
            paginate=True, jq=".[] | {login: (.login // .name), c: .contributions}",
        )
    except RuntimeError:
        return None
    if not contributors:
        return None
    total = 0
    user_c = 0
    for c in contributors:
        try:
            n = int(c.get("c") or 0)
        except Exception:
            n = 0
        total += n
        if (c.get("login") or "").lower() == GH_LOGIN.lower():
            user_c = n
    out = {"total": total, "user": user_c}
    f.write_text(json.dumps(out))
    return out


# Back-compat alias kept while we transition.
def fetch_repo_total_commits(full_name: str) -> int | None:
    d = fetch_repo_contrib_stats(full_name)
    return d.get("total") if d else None


def fetch_pr_detail(full_name: str, number: int) -> dict | None:
    """Fetch /repos/{full_name}/pulls/{number} (cached). Returns the PR object
    which includes additions/deletions/changed_files."""
    cache_dir = CACHE_API / "pr_detail"
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{full_name.replace('/', '__')}__{number}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    try:
        r = gh_api(f"/repos/{full_name}/pulls/{number}")
    except RuntimeError:
        return None
    if not isinstance(r, dict):
        return None
    f.write_text(json.dumps(r))
    return r


def fetch_repo_detail(full_name: str) -> dict | None:
    """Fetch /repos/{full_name} (cached). Returns dict with stargazers_count
    and other metadata."""
    cache_dir = CACHE_API / "repo_detail"
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / (full_name.replace("/", "__") + ".json")
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    try:
        r = gh_api(f"/repos/{full_name}")
    except RuntimeError:
        return None
    if not isinstance(r, dict):
        return None
    f.write_text(json.dumps(r))
    return r


def fill_star_counts(repo_names: Iterable[str], known: dict[str, int]) -> int:
    """For each repo name not in `known`, fetch its detail to get stargazers.
    Returns number of new repos resolved."""
    n = 0
    for name in repo_names:
        if "/" not in name:
            continue
        if name in known:
            continue
        detail = fetch_repo_detail(name)
        if detail:
            n += 1
    return n


def fetch_profile() -> dict:
    """Followers / following / sponsors info. Cached after first fetch."""
    cache_file = CACHE_API / "profile.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    profile: dict = {}
    # REST: basic counts
    try:
        u = gh_api(f"/users/{GH_LOGIN}")
        if isinstance(u, dict):
            profile.update({
                "followers": u.get("followers", 0),
                "following": u.get("following", 0),
                "public_repos": u.get("public_repos", 0),
                "public_gists": u.get("public_gists", 0),
                "created_at": u.get("created_at"),
                "html_url": u.get("html_url"),
                "avatar_url": u.get("avatar_url"),
                "bio": u.get("bio"),
                "blog": u.get("blog"),
                "location": u.get("location"),
            })
    except RuntimeError as e:
        print(f"  ! /users/{GH_LOGIN} failed: {e}", file=sys.stderr)
    # GraphQL: sponsorship info
    query = (
        'query { user(login: "' + GH_LOGIN + '") {'
        '  sponsors(first: 100) { totalCount nodes { '
        '    ... on User { login name avatarUrl }'
        '    ... on Organization { login name avatarUrl } } }'
        '  sponsoring(first: 100) { totalCount nodes {'
        '    ... on User { login } ... on Organization { login } } }'
        '  hasSponsorsListing'
        '  sponsorsListing { name }'
        '} }'
    )
    try:
        env = {**os.environ, "NO_COLOR": "1"}
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if r.returncode == 0:
            text = ANSI_RE.sub("", r.stdout)
            data = json.loads(text).get("data", {}).get("user", {})
            sponsors = data.get("sponsors") or {}
            sponsoring = data.get("sponsoring") or {}
            profile["sponsors_count"] = sponsors.get("totalCount", 0)
            profile["sponsors"] = [
                {"login": n.get("login"),
                 "name": n.get("name"),
                 "avatar_url": n.get("avatarUrl")}
                for n in (sponsors.get("nodes") or [])
            ]
            profile["sponsoring_count"] = sponsoring.get("totalCount", 0)
            profile["has_sponsors_listing"] = data.get("hasSponsorsListing", False)
            profile["sponsors_url"] = (
                f"https://github.com/sponsors/{GH_LOGIN}"
                if data.get("hasSponsorsListing") else None
            )
    except Exception as e:
        print(f"  ! sponsors graphql failed: {e}", file=sys.stderr)
    CACHE_API.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(profile))
    return profile


def load_prs_issues_counts():
    """Return counts from cached search results.

    Returns dict with keys:
      pr_repo, pr_merged_repo, iss_repo,
      pr_year, pr_merged_year, iss_year,
      pr_merged_add_repo, pr_merged_del_repo (lines from per-PR detail cache,
        used to fill in repos we don't have local commit data for)
    """
    from collections import defaultdict
    pr_repo: dict[str, int] = defaultdict(int)
    pr_merged_repo: dict[str, int] = defaultdict(int)
    iss_repo: dict[str, int] = defaultdict(int)
    pr_year: dict[int, int] = defaultdict(int)
    pr_merged_year: dict[int, int] = defaultdict(int)
    iss_year: dict[int, int] = defaultdict(int)
    pr_merged_add_repo: dict[str, int] = defaultdict(int)
    pr_merged_del_repo: dict[str, int] = defaultdict(int)

    # Read cached PR-detail files for additions/deletions per repo.
    detail_dir = CACHE_API / "pr_detail"
    if detail_dir.exists():
        for f in detail_dir.glob("*.json"):
            try:
                pd = json.loads(f.read_text())
            except Exception:
                continue
            if not pd.get("merged_at"):
                continue
            # File name encodes the original repo name as fetched —
            # f.stem is "{owner}__{name}__{number}".
            parts = f.stem.split("__")
            if len(parts) < 3:
                continue
            full_name = "__".join(parts[:-1]).replace("__", "/", 1)
            add = int(pd.get("additions") or 0)
            dlt = int(pd.get("deletions") or 0)
            pr_merged_add_repo[full_name] += add
            pr_merged_del_repo[full_name] += dlt

    pr_file = CACHE_API / "prs.json"
    if pr_file.exists():
        try:
            for it in json.loads(pr_file.read_text()):
                ru = it.get("repository_url", "")
                m = re.search(r"/repos/([^/]+/[^/]+)$", ru)
                if not m:
                    continue
                repo = m.group(1)
                pr_repo[repo] += 1
                merged = bool((it.get("pull_request") or {}).get("merged_at"))
                if merged:
                    pr_merged_repo[repo] += 1
                created = it.get("created_at", "")
                if created:
                    try:
                        year = int(created[:4])
                        pr_year[year] += 1
                        if merged:
                            pr_merged_year[year] += 1
                    except ValueError:
                        pass
        except Exception:
            pass

    iss_file = CACHE_API / "issues.json"
    if iss_file.exists():
        try:
            for it in json.loads(iss_file.read_text()):
                ru = it.get("repository_url", "")
                m = re.search(r"/repos/([^/]+/[^/]+)$", ru)
                if not m:
                    continue
                repo = m.group(1)
                iss_repo[repo] += 1
                created = it.get("created_at", "")
                if created:
                    try:
                        year = int(created[:4])
                        iss_year[year] += 1
                    except ValueError:
                        pass
        except Exception:
            pass

    return {
        "pr_repo": pr_repo,
        "pr_merged_repo": pr_merged_repo,
        "iss_repo": iss_repo,
        "pr_year": pr_year,
        "pr_merged_year": pr_merged_year,
        "iss_year": iss_year,
        "pr_merged_add_repo": pr_merged_add_repo,
        "pr_merged_del_repo": pr_merged_del_repo,
    }


def _generic_company_color_jobs() -> list[dict]:
    """Synthesize color-only job entries for generic --user runs so the
    template's COMPANY_COLOR lookup still resolves per-company colors.
    These entries have null start/end so the career-timeline section
    treats them as "no date range" and won't render (we also gate on
    PERSONALIZED in the template)."""
    palette = [
        "#84cc16", "#ec4899", "#0ea5e9", "#f59e0b", "#b51f08", "#22c55e",
        "#d97706", "#246af0", "#7d4cdb", "#e0264c", "#1f7a4d", "#a47148",
    ]
    seen: list[str] = []
    for _pat, company in REPO_JOB_PATTERNS:
        if company not in seen:
            seen.append(company)
    return [
        {"company": c, "role": "", "start": None, "end": None,
         "color": palette[i % len(palette)], "anchor": None,
         "synthetic": True}
        for i, c in enumerate(seen)
    ]


def aggregate(records: list[dict]) -> dict:
    """Build the aggregate data structure for rendering."""
    # Normalize date → ISO date string (UTC date).
    def iso_day(s: str) -> str:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.astimezone(timezone.utc).date().isoformat()
        except Exception:
            return ""

    by_day: dict[str, dict] = defaultdict(lambda: {"c": 0, "a": 0, "d": 0})
    # Per-day per-company commit count, used to color heatmap by dominant job.
    by_day_company: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_year: dict[int, dict] = defaultdict(lambda: {"commits": 0, "add": 0, "del": 0, "repos": set()})
    by_repo: dict[str, dict] = defaultdict(lambda: {
        "commits": 0, "add": 0, "del": 0, "first": None, "last": None,
        "years": set(), "owned": False, "remote": None, "local_paths": set(),
    })
    by_year_repo: dict[tuple[int, str], dict] = defaultdict(
        lambda: {"commits": 0, "add": 0, "del": 0})
    # year → company → commit count (for stacked per-year bars)
    by_year_company: dict[int, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    by_year_company_lines: dict[int, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"add": 0, "del": 0}))
    # Hour-of-day histogram in commit-LOCAL time (uses the timezone the
    # commit was authored in — git records this in the ISO date string).
    by_hour_local: dict[int, int] = defaultdict(int)
    # Per-language and per-(year, language) line totals
    by_lang: dict[str, dict] = defaultdict(lambda: {"add": 0, "del": 0, "commits": 0})
    by_year_lang: dict[int, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"add": 0, "del": 0}))

    repo_keys: dict[str, str] = {}  # display-name → canonical key

    for r in records:
        day = iso_day(r["date"])
        if not day:
            continue
        year = int(day[:4])
        canonical = repo_canonical(r)
        add, dlt = int(r.get("additions") or 0), int(r.get("deletions") or 0)

        # Hour-of-day in the AUTHOR'S local time (timezone they were in
        # when they committed). The ISO string itself has the local hour;
        # we just slice it. This naturally adjusts for PST↔EST moves.
        try:
            iso = r.get("date") or ""
            if len(iso) >= 13 and iso[10] == "T":
                local_hr = int(iso[11:13])
                if 0 <= local_hr < 24:
                    by_hour_local[local_hr] += 1
        except Exception:
            pass

        # Per-language stats from this commit's by_lang field
        for lang, addel in (r.get("by_lang") or {}).items():
            if isinstance(addel, list) and len(addel) == 2:
                la, ld = addel
            elif isinstance(addel, dict):
                la, ld = addel.get("add", 0), addel.get("del", 0)
            else:
                continue
            by_lang[lang]["add"] += la
            by_lang[lang]["del"] += ld
            by_lang[lang]["commits"] += 1
            by_year_lang[year][lang]["add"] += la
            by_year_lang[year][lang]["del"] += ld

        # Per-day
        by_day[day]["c"] += 1
        by_day[day]["a"] += add
        by_day[day]["d"] += dlt

        # Per-day-per-company (for heatmap coloring) — and year-per-company
        # (for stacked per-year bars)
        company = repo_to_company(canonical)
        if company:
            by_day_company[day][company] += 1
        # Year × company aggregation (None bucket = uncategorized/personal)
        co_key = company or "_other"
        by_year_company[year][co_key] += 1
        by_year_company_lines[year][co_key]["add"] += add
        by_year_company_lines[year][co_key]["del"] += dlt

        # Per-year
        y = by_year[year]
        y["commits"] += 1
        y["add"] += add
        y["del"] += dlt
        y["repos"].add(canonical)

        # Per-repo
        rr = by_repo[canonical]
        rr["commits"] += 1
        rr["add"] += add
        rr["del"] += dlt
        rr["years"].add(year)
        if r.get("repo_remote"):
            rr["remote"] = r["repo_remote"]
        if r.get("repo_local_path"):
            rr["local_paths"].add(r["repo_local_path"])
        if rr["first"] is None or day < rr["first"]:
            rr["first"] = day
        if rr["last"] is None or day > rr["last"]:
            rr["last"] = day

        # Per-year-per-repo
        yr = by_year_repo[(year, canonical)]
        yr["commits"] += 1
        yr["add"] += add
        yr["del"] += dlt

    # Stars + PR/issue counts (from API cache).
    star_counts = load_star_counts()
    pri = load_prs_issues_counts()
    pr_repo = pri["pr_repo"]
    pr_merged_repo = pri["pr_merged_repo"]
    iss_repo = pri["iss_repo"]
    pr_year = pri["pr_year"]
    pr_merged_year = pri["pr_merged_year"]
    iss_year = pri["iss_year"]
    pr_merged_add_repo = pri["pr_merged_add_repo"]
    pr_merged_del_repo = pri["pr_merged_del_repo"]
    profile = fetch_profile()

    # Add virtual entries for repos where pirate has PRs/issues but no commits.
    # Resolve their canonical name (handle renames/transfers) so we don't
    # double-count under both old and new names.
    rename_cache_file = CACHE_API / "renames.json"
    rename_cache: dict[str, str] = {}
    if rename_cache_file.exists():
        try:
            rename_cache = json.loads(rename_cache_file.read_text())
        except Exception:
            pass

    pr_issue_repos = set(pr_repo) | set(iss_repo)
    for full in list(pr_issue_repos):
        if "/" not in full:
            continue
        # First try the GH rename redirect cache.
        canon = rename_cache.get(full)
        if canon is None:
            canon = resolve_canonical_name(full)
            rename_cache[full] = canon
        # Then apply the SHA-overlap / fork-parent alias from commit dedup
        # (so e.g. timvisee/send → mozilla/send is honored on PR counts too).
        canon = _LAST_ALIAS_MAP.get(canon, canon)
        if canon and canon != full:
            # Merge counts from old name into canonical.
            pr_repo[canon] = pr_repo.get(canon, 0) + pr_repo.get(full, 0)
            pr_merged_repo[canon] = (pr_merged_repo.get(canon, 0) +
                                     pr_merged_repo.get(full, 0))
            iss_repo[canon] = iss_repo.get(canon, 0) + iss_repo.get(full, 0)
            pr_merged_add_repo[canon] = (pr_merged_add_repo.get(canon, 0) +
                                         pr_merged_add_repo.get(full, 0))
            pr_merged_del_repo[canon] = (pr_merged_del_repo.get(canon, 0) +
                                         pr_merged_del_repo.get(full, 0))
            pr_repo.pop(full, None)
            pr_merged_repo.pop(full, None)
            iss_repo.pop(full, None)
            pr_merged_add_repo.pop(full, None)
            pr_merged_del_repo.pop(full, None)
    rename_cache_file.write_text(json.dumps(rename_cache))

    # Now create virtual by_repo entries (PR/issue-only contributions).
    for full in set(pr_repo) | set(iss_repo):
        if "/" not in full:
            continue
        if full in by_repo:
            continue
        # Synthetic entry; commits=0, prs/issues will be filled in below.
        by_repo[full] = {
            "commits": 0, "add": 0, "del": 0,
            "first": None, "last": None,
            "years": set(), "owned": False,
            "remote": f"https://github.com/{full}.git",
            "local_paths": set(),
        }

    # Stringify sets for JSON.
    for y, v in by_year.items():
        v["repos"] = sorted(v["repos"])
        v["prs"] = pr_year.get(y, 0)
        v["prs_merged"] = pr_merged_year.get(y, 0)
        v["issues"] = iss_year.get(y, 0)
    for k, v in by_repo.items():
        v["years"] = sorted(v["years"])
        v["local_paths"] = sorted(v["local_paths"])
        v["company"] = repo_to_company(k)
        v["stars"] = star_counts.get(k, 0)
        v["prs"] = pr_repo.get(k, 0)
        v["prs_merged"] = pr_merged_repo.get(k, 0)
        v["issues"] = iss_repo.get(k, 0)
        # If we have no commit data but DO have merged PRs, use the PR's
        # additions/deletions as a proxy for line contribution.
        if v["commits"] == 0 and v["prs_merged"] > 0:
            v["add"] = pr_merged_add_repo.get(k, 0)
            v["del"] = pr_merged_del_repo.get(k, 0)
            v["lines_from_prs"] = True
        else:
            v["lines_from_prs"] = False
        # Did pirate actually contribute (commits or merged PR)?
        v["contributed"] = v["commits"] > 0 or v["prs_merged"] > 0

    # Attach dominant company per day for heatmap coloring.
    for day, counts in by_day_company.items():
        if not counts:
            continue
        # Pick the company with the most commits that day.
        dom = max(counts.items(), key=lambda kv: kv[1])[0]
        by_day[day]["co"] = dom

    # Totals
    total_commits = sum(v["commits"] for v in by_year.values())
    total_add = sum(v["add"] for v in by_year.values())
    total_del = sum(v["del"] for v in by_year.values())
    total_repos = len(by_repo)
    # "Stars earned" should only credit repos pirate owns or that are owned
    # by orgs he's worked at (current + past jobs). Drive-by PRs to
    # tensorflow/rust/etc. don't get to claim the repo's full star count.
    accessible_owners: set[str] = {GH_LOGIN}
    try:
        acc = json.loads((CACHE_API / "accessible_repos.json").read_text())
        for r in acc:
            n = r.get("full_name", "")
            if "/" in n:
                accessible_owners.add(n.split("/", 1)[0])
    except Exception:
        pass

    def is_owned_or_member(repo_full: str) -> bool:
        """Repo counts toward the user's 'stars earned' if owned by them or
        by an org they've been a member of (current accessible_repos covers
        current; the REPO_JOB_PATTERNS-derived company classification covers
        past orgs like browser-use, drchrono that he's no longer in)."""
        if "/" not in repo_full:
            return False
        owner = repo_full.split("/", 1)[0]
        if owner in accessible_owners:
            return True
        # If the repo classifies as one of pirate's jobs, it's a past org.
        return repo_to_company(repo_full) is not None

    # "Stars earned" only credits repos where the user authored ≥15% of all
    # commits in the repo's history. To make the share apples-to-apples we
    # compare GH-API user commits vs GH-API total commits (both from the
    # /contributors endpoint — default branch only, no PR/branch inflation).
    SHARE_THRESHOLD = 0.15
    contrib_cache: dict[str, dict] = {}
    tcdir = CACHE_API / "repo_total_commits"
    if tcdir.exists():
        for tcf in tcdir.glob("*.json"):
            try:
                d = json.loads(tcf.read_text())
                if isinstance(d, dict):
                    contrib_cache[tcf.stem.replace("__", "/", 1)] = d
                else:
                    contrib_cache[tcf.stem.replace("__", "/", 1)] = {
                        "total": int(d), "user": None,
                    }
            except Exception:
                pass

    def passes_share(k: str, v: dict) -> bool:
        if not v["contributed"]:
            return False
        cs = contrib_cache.get(k)
        if cs and cs.get("total"):
            user = cs.get("user")
            if user is None:
                # Old cache file (total only). Fall back to local-mine count
                # but cap denominator-wise to avoid inflated shares from
                # all-branch counts.
                user = min(v["commits"], cs["total"])
            return (user / cs["total"]) >= SHARE_THRESHOLD
        # No contributor data — fall back: owned/member in personalized mode,
        # accept anything in generic mode.
        return is_owned_or_member(k) if PERSONALIZED else True

    total_stars = sum(v["stars"] for k, v in by_repo.items() if passes_share(k, v))
    for k, v in by_repo.items():
        cs = contrib_cache.get(k)
        if cs and cs.get("total"):
            v["repo_total_commits"] = cs["total"]
            user = cs.get("user")
            if user is None:
                user = min(v["commits"], cs["total"])
            v["share"] = user / cs["total"]
            v["user_commits_default_branch"] = user
        else:
            v["repo_total_commits"] = None
            v["share"] = None
    total_prs = sum(pr_year.values())
    total_prs_merged = sum(pr_merged_year.values())
    total_issues = sum(iss_year.values())

    # Per-year-per-repo as list.
    yr_list = []
    for (year, repo), v in by_year_repo.items():
        yr_list.append({"year": year, "repo": repo, **v})

    # Hour-of-day histogram (commit-local time, 0-23).
    hour_local_list = [
        {"hour": h, "c": by_hour_local.get(h, 0)} for h in range(24)
    ]

    # Per-language totals + per-year × language
    by_lang_out = {
        lang: {"add": v["add"], "del": v["del"], "commits": v["commits"]}
        for lang, v in by_lang.items()
    }
    by_year_lang_out: dict[str, dict[str, dict]] = {}
    for year, lang_d in by_year_lang.items():
        by_year_lang_out[str(year)] = {
            lang: {"add": x["add"], "del": x["del"]}
            for lang, x in lang_d.items()
        }

    # Year × company breakdown for stacked per-year bars.
    by_year_company_out: dict[str, dict[str, dict]] = {}
    for year, counts in by_year_company.items():
        by_year_company_out[str(year)] = {
            co: {
                "c": cnt,
                "a": by_year_company_lines[year][co]["add"],
                "d": by_year_company_lines[year][co]["del"],
            }
            for co, cnt in counts.items()
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "login": GH_LOGIN,
            "name": GH_NAME,
            "emails": sorted(PIRATE_EMAILS),
            "profile": profile,
        },
        "annotations": {
            # Full-time jobs (personalized — only included when running for
            # the script's "owner" user). Generic --user runs emit synthetic
            # color-only entries below so the COMPANY_COLOR map works but
            # the career-timeline UI section stays hidden.
            "jobs": _generic_company_color_jobs() if not PERSONALIZED else [
                {"company": "DBond IT",
                 "role": "Mac Repair Tech & Internal Tools Dev",
                 "start": "2013-01", "end": "2013-06",
                 "color": "#a47148",
                 "anchor": "-DBond-IT-Mac-Repair-Technician--Internal-Tools-Developer-2013"},
                {"company": "DeliveryHero China",
                 "role": "Junior Full-Stack Dev",
                 "start": "2013-06", "end": "2013-09",
                 "color": "#e0264c",
                 "anchor": "-DeliveryHero-China-Junior-Full-Stack-Developer-2013"},
                {"company": "ExpoSpot / Hotspot",
                 "role": "Co-Founder / CTO",
                 "start": "2013-09", "end": "2014-12",
                 "color": "#1f7a4d",
                 "anchor": "-ExpoSpot--Hotspot-Co-Founder--CTO-2013---2014"},
                {"company": "DrChrono",
                 "role": "Full-Stack Engineer",
                 "start": "2014-12", "end": "2016-06",
                 "color": "#246af0",
                 "anchor": "-DrChrono-Full-Stack-Engineer-2014---2016"},
                {"company": "Mavrx",
                 "role": "Engineering & Data",
                 "start": "2016-06", "end": "2016-12",
                 "color": "#7d4cdb",
                 "anchor": "-Mavrx-Engineering--Data-2016"},
                {"company": "OddSlingers Labs",
                 "role": "Co-Founder / CTO",
                 "start": "2016-12", "end": "2018-12",
                 "color": "#d97706",
                 "anchor": "-OddSlingers-Labs-Co-Founder--CTO-2016---2018"},
                {"company": "Monadical",
                 "role": "Co-Founder / CTO",
                 "start": "2016-12", "end": "2021-12",
                 "color": "#b51f08",
                 "anchor": "-Monadical-Co-Founder--CTO-2016---2021"},
                {"company": "Currents.fm",
                 "role": "Director of Engineering",
                 "start": "2020-01", "end": "2022-12",
                 "color": "#0ea5e9",
                 "anchor": "-Currentsfm-Director-of-Engineering-2020---2022"},
                {"company": "Self-Employed (research)",
                 "role": "Mental health treatment research",
                 "start": "2022-01", "end": "2023-12",
                 "color": "#64748b",
                 "anchor": "-Self-Employed-Mental-health-treatment-research-2022---2023"},
                {"company": "Browser-Use",
                 "role": "Founding Engineer",
                 "start": "2025-01", "end": "2025-11",
                 "color": "#ec4899",
                 "anchor": "-Browser-Use-Founding-Engineer-2025"},
                {"company": "Browserbase",
                 "role": "Stagehand Engineer",
                 "start": "2025-11", "end": None,
                 "color": "#f59e0b",
                 "anchor": "-BrowserBase-Stagehand-Engineer-2025-11---Present"},
                {"company": "ArchiveBox.io",
                 "role": "Founder / OSS Maintainer",
                 "start": "2017-01", "end": None,
                 "color": "#84cc16",
                 "anchor": "-ArchiveBoxio-Founder--Open-Source-Maintainer-2017---present",
                 "ongoing": True},
            ],
            "jobs_url": (
                "https://docs.sweeting.me/s/blog" if PERSONALIZED else None),
            # Repo → company mapping rules (emitted from the live config so
            # generic --user runs reflect derived patterns).
            "repo_job_patterns": [
                {"pattern": pat.pattern, "company": company}
                for pat, company in REPO_JOB_PATTERNS
            ],
        },
        "totals": {
            "commits": total_commits,
            "additions": total_add,
            "deletions": total_del,
            "repos": total_repos,
            "stars": total_stars,
            "prs": total_prs,
            "prs_merged": total_prs_merged,
            "issues": total_issues,
            "first_day": min(by_day) if by_day else None,
            "last_day": max(by_day) if by_day else None,
        },
        "by_day": dict(by_day),
        "by_year": {str(y): v for y, v in sorted(by_year.items())},
        "by_repo": dict(by_repo),
        "by_year_repo": yr_list,
        "by_year_company": by_year_company_out,
        "by_hour_local": hour_local_list,
        "by_lang": by_lang_out,
        "by_year_lang": by_year_lang_out,
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_html(agg: dict) -> str:
    template = TEMPLATE_FILE.read_text()
    return template.replace(
        "/*__DATA__*/null",
        json.dumps(agg, indent=None, separators=(",", ":")),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _collect_all_records() -> list[dict]:
    """Load every cached per-repo commit list."""
    import glob as _glob
    all_recs: list[dict] = []
    for f in _glob.glob(str(CACHE_REPOS / "*.json")):
        try:
            all_recs.extend(json.loads(open(f).read()))
        except Exception:
            pass
    return all_recs


def report_progress(phase: str, message: str = "", **extra) -> None:
    """POST a small JSON progress update to the live /api/progress endpoint
    so the user's loading page can render real-time phase info. Best-effort
    — failures are silent."""
    if not PROGRESS_URL or not PROGRESS_TOKEN:
        return
    try:
        import urllib.request, urllib.error
        payload = {
            "phase": phase,
            "message": message,
            "ts": time.time(),
            "user": GH_LOGIN,
            **extra,
        }
        data = json.dumps(payload).encode()
        url = f"{PROGRESS_URL}?user={GH_LOGIN}"
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {PROGRESS_TOKEN}",
            },
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _render_now(*, mining_status: str = "complete") -> dict:
    """Aggregate from cache and write stats.html. Returns aggregate dict.
    `mining_status` is embedded in the output so the live-mining UI knows
    when to keep auto-refreshing. Values: 'partial' | 'complete'."""
    all_recs = _collect_all_records()
    merged = dedupe_commits(all_recs)
    agg = aggregate(merged)
    agg["mining_status"] = mining_status
    if mining_status != "complete":
        agg["mining_phase"] = mining_status
    CACHE_AGG.write_text(json.dumps(agg))
    OUTPUT_FILE.write_text(render_html(agg))
    return agg


def load_config_from_file(path: str) -> None:
    """Apply a JSON config file, mutating the module-level user-specific
    constants. Any field omitted from the file keeps its current (default)
    value."""
    global GH_LOGIN, GH_NAME, PIRATE_EMAILS, PIRATE_NEEDLES
    global MANUAL_CANONICAL, REPO_JOB_PATTERNS, SEARCH_DIRS, PERSONALIZED
    with open(path) as f:
        c = json.load(f)
    if c.get("login"):
        GH_LOGIN = c["login"]
    if c.get("name"):
        GH_NAME = c["name"]
    if "emails" in c:
        PIRATE_EMAILS = set(c["emails"])
    if "name_patterns" in c:
        PIRATE_NEEDLES = tuple(c["name_patterns"])
    if "manual_canonical" in c:
        MANUAL_CANONICAL.clear()
        MANUAL_CANONICAL.update(c["manual_canonical"])
    if "repo_job_patterns" in c:
        REPO_JOB_PATTERNS.clear()
        for p in c["repo_job_patterns"]:
            REPO_JOB_PATTERNS.append(
                (re.compile(p["pattern"], re.I), p["company"]))
    if "search_dirs" in c:
        SEARCH_DIRS[:] = [Path(p) for p in c["search_dirs"]]
    if "personalized" in c:
        PERSONALIZED = bool(c["personalized"])
    print(f"  loaded config for @{GH_LOGIN} from {path}", file=sys.stderr)


def auto_derive_config_for_user(login: str) -> None:
    """Switch the script over to mining `login` instead of the default user.
    Auto-derives email aliases by sampling commits the user authored.
    Sets PERSONALIZED=False so the template hides personalized sections
    (career timeline, manual company colors)."""
    global GH_LOGIN, GH_NAME, PIRATE_EMAILS, PIRATE_NEEDLES
    global MANUAL_CANONICAL, REPO_JOB_PATTERNS, SEARCH_DIRS, PERSONALIZED
    GH_LOGIN = login
    PERSONALIZED = False

    # Profile lookup → real name
    try:
        u = gh_api(f"/users/{login}")
        if isinstance(u, dict):
            GH_NAME = u.get("name") or login
    except Exception:
        GH_NAME = login

    # Email discovery: query the user's most recent commits and harvest
    # author.email values seen on them. The login + name are also added
    # as substring needles.
    discovered_emails: set[str] = set()
    discovered_names: set[str] = set()
    try:
        items = gh_api(
            f"/search/commits?q=author:{login}&per_page=100&sort=author-date",
            paginate=False, jq=".items[]",
        )
        for it in items or []:
            ca = (it.get("commit") or {}).get("author") or {}
            em = (ca.get("email") or "").strip().lower()
            nm = (ca.get("name") or "").strip()
            if em:
                discovered_emails.add(em)
            if nm:
                discovered_names.add(nm.lower())
    except Exception as e:
        print(f"  ! email discovery failed: {e}", file=sys.stderr)

    PIRATE_EMAILS = discovered_emails or {f"{login}@users.noreply.github.com"}
    PIRATE_NEEDLES = tuple(
        sorted({login.lower()} | discovered_names)
    )

    MANUAL_CANONICAL.clear()
    REPO_JOB_PATTERNS.clear()
    SEARCH_DIRS[:] = []  # generic users — no local mining by default
    print(f"  auto-config: @{login} ({GH_NAME}) "
          f"with {len(PIRATE_EMAILS)} emails discovered", file=sys.stderr)


def derive_company_patterns_from_repos() -> None:
    """For generic users with no manual REPO_JOB_PATTERNS, infer 'companies'
    from the top GH org owners in the accessible_repos list. Each becomes
    a pseudo-job entry so the per-year stacked bars / repo-row dots are
    still colorful and meaningful."""
    if REPO_JOB_PATTERNS:
        return  # user already configured
    acc_file = CACHE_API / "accessible_repos.json"
    if not acc_file.exists():
        return
    from collections import Counter
    owner_counts: Counter = Counter()
    try:
        for r in json.loads(acc_file.read_text()):
            n = r.get("full_name", "")
            if "/" in n:
                owner_counts[n.split("/", 1)[0]] += 1
    except Exception:
        return
    # Skip pirate's own login (already handled implicitly)
    top = [o for o, _ in owner_counts.most_common(12)
           if o.lower() != GH_LOGIN.lower()]
    palette = [
        "#84cc16", "#ec4899", "#0ea5e9", "#f59e0b", "#b51f08", "#22c55e",
        "#d97706", "#246af0", "#7d4cdb", "#e0264c", "#1f7a4d", "#a47148",
    ]
    for owner, color in zip(top, palette):
        REPO_JOB_PATTERNS.append(
            (re.compile(rf"^{re.escape(owner)}/", re.I), owner))
    print(f"  derived {len(REPO_JOB_PATTERNS)} company patterns from "
          f"top org owners: {top[:5]}{'…' if len(top) > 5 else ''}",
          file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mine git history (local + GitHub) and render stats.html.")
    ap.add_argument("--user",
                    help="mine a different GitHub user (auto-derives emails, "
                         "skips personalized sections like career timeline)")
    ap.add_argument("--config",
                    help="path to JSON config file overriding the defaults")
    ap.add_argument("--render-only", action="store_true",
                    help="re-render HTML from cached aggregate (no fetching)")
    ap.add_argument("--no-local", action="store_true",
                    help="skip local-repo filesystem walk")
    ap.add_argument("--no-clone", action="store_true",
                    help="skip bare-cloning missing owned/org repos")
    ap.add_argument("--no-prs", action="store_true",
                    help="skip PR/issue search and PR-detail fetch")
    ap.add_argument("--no-stars", action="store_true",
                    help="skip per-repo star fetch")
    ap.add_argument("--no-search-commits", action="store_true",
                    help="skip search/commits discovery (heavy + rate-limited)")
    ap.add_argument("--refresh-local", action="store_true",
                    help="re-mine local repos (ignore per-repo cache)")
    ap.add_argument("--max-api-fetches", type=int, default=2000,
                    help="cap on per-commit stat / per-repo API calls per phase")
    args = ap.parse_args()

    # Apply config overrides BEFORE any work — they affect cache layout
    # and what data we collect.
    if args.user:
        auto_derive_config_for_user(args.user)
    if args.config:
        load_config_from_file(args.config)
    rebind_cache_paths()

    CACHE.mkdir(parents=True, exist_ok=True)
    CACHE_API.mkdir(parents=True, exist_ok=True)
    CACHE_REPOS.mkdir(parents=True, exist_ok=True)

    if args.render_only:
        agg = json.loads(CACHE_AGG.read_text())
        OUTPUT_FILE.write_text(render_html(agg))
        print(f"Wrote {OUTPUT_FILE}")
        return 0

    report_progress("starting", f"Mining @{GH_LOGIN}")

    # ---- Phase 1: local filesystem mining --------------------------------
    if not args.no_local:
        print(">> [1/7] Walking filesystem for local git repos ...", file=sys.stderr)
        repos = find_local_repos(SEARCH_DIRS)
        print(f">> {len(repos)} repos found", file=sys.stderr)
        print(">> [1/7] Mining local repos (incremental render every 10) ...",
              file=sys.stderr)
        mine_all_local(repos, force=args.refresh_local,
                       incremental_render_every=10)

    # ---- Phase 2: bare-clone owned/org GH repos we don't have locally ----
    if not args.no_clone:
        report_progress("phase-2-listing", "Listing accessible GitHub repos")
        print(">> [2/7] Listing accessible GitHub repos ...", file=sys.stderr)
        accessible = list_accessible_repos()
        report_progress("phase-2-cloning", f"{len(accessible)} repos accessible — cloning",
                        repos_accessible=len(accessible))
        print(f">> {len(accessible)} accessible repos via API", file=sys.stderr)
        # For generic --user runs, derive company patterns from the user's
        # top org owners so the dashboard still has some color coding.
        if not PERSONALIZED:
            derive_company_patterns_from_repos()

        # Build set of repos we already have local commits for.
        already_covered: set[str] = set()
        for recs in (_collect_all_records(),):
            for r in recs:
                rn = r.get("repo")
                if rn:
                    already_covered.add(rn)

        # Bare-clone any non-fork accessible repo not yet covered, plus all
        # owned non-fork repos pirate created (high-value personal projects).
        to_clone: list[str] = []
        for r in accessible:
            if r.get("fork"):
                continue
            full = r.get("full_name")
            if not full or full in already_covered:
                continue
            # Skip ridiculously large repos (>2GB) — they're forks or mirrors.
            if (r.get("size") or 0) > 2_000_000:
                continue
            to_clone.append(full)
        if to_clone:
            print(f">> [2/7] Bare-cloning {len(to_clone)} GH repos ...",
                  file=sys.stderr)
            clone_and_mine_repos(to_clone[:args.max_api_fetches])
        else:
            print(">> [2/7] Nothing to clone — all accessible repos covered",
                  file=sys.stderr)
        try:
            _render_now(mining_status="phase-2-cloned-repos")
        except Exception:
            pass

    # ---- Phase 3: search/commits discovery -------------------------------
    if not args.no_search_commits:
        print(">> [3/7] search/commits discovery (rate-limited) ...",
              file=sys.stderr)
        try:
            search_items = list_search_commits()
            print(f">> {len(search_items)} search hits", file=sys.stderr)
            # Bare-clone any repo discovered via search/commits that we still
            # don't have. (Many will be forks of pirate's commits.)
            covered = {r.get("repo")
                       for r in _collect_all_records() if r.get("repo")}
            discovered = {(it.get("repository") or {}).get("full_name")
                          for it in search_items}
            discovered.discard(None)
            new_repos = sorted(discovered - covered)
            if new_repos:
                print(f">> [3/7] Cloning {len(new_repos)} new discovered repos",
                      file=sys.stderr)
                clone_and_mine_repos(new_repos[:args.max_api_fetches])
        except Exception as e:
            print(f"  ! search/commits failed: {e}", file=sys.stderr)

    # ---- Phase 4: PR & issue search --------------------------------------
    try:
        _render_now(mining_status="phase-4-searching-prs")
    except Exception:
        pass
    if not args.no_prs:
        report_progress("phase-4-prs", "Searching PRs + issues authored by user")
        print(">> [4/7] Searching PRs + issues by the user ...", file=sys.stderr)
        try:
            prs, issues = list_prs_and_issues()
            report_progress("phase-4-prs-done",
                            f"Found {len(prs)} PRs, {len(issues)} issues",
                            prs=len(prs), issues=len(issues))
            print(f">> {len(prs)} PRs, {len(issues)} issues", file=sys.stderr)
        except Exception as e:
            print(f"  ! PR/issue search failed: {e}", file=sys.stderr)

        # ---- Phase 5: PR-detail fetch (lines added/removed) --------------
        report_progress("phase-5-pr-details", "Fetching per-PR additions/deletions")
        print(">> [5/7] Fetching merged-PR additions/deletions ...",
              file=sys.stderr)
        try:
            n = fetch_pr_details_for_merged(max_fetches=args.max_api_fetches)
            print(f">> {n} new PR details fetched", file=sys.stderr)
        except Exception as e:
            print(f"  ! PR-detail fetch failed: {e}", file=sys.stderr)

        # ---- Phase 5a: per-PR commits (handles non-GH-linked author emails)
        report_progress("phase-5a-pr-commits",
                        "Walking each merged PR's commit list")
        try:
            existing_shas: set[str] = {r.get("sha")
                                       for r in _collect_all_records()
                                       if r.get("sha")}
            print(">> [5a/7] Fetching commits from each merged PR ...",
                  file=sys.stderr)
            n = mine_commits_from_merged_prs(
                existing_shas, max_fetches=args.max_api_fetches)
            print(f">> {n} new commits via PR-commits API", file=sys.stderr)
        except Exception as e:
            print(f"  ! per-PR commits fetch failed: {e}", file=sys.stderr)

        try:
            _render_now(mining_status="phase-5-pr-detail-fetched")
        except Exception:
            pass

        # ---- Phase 5b: actual commits in PR-only repos -------------------
        # If pirate's PR was merged, his commits are in the upstream repo's
        # history. Use /repos/{r}/commits?author=pirate to fetch them.
        try:
            # Repos where pirate has merged PRs but we don't have commit data
            from collections import Counter as _Counter
            merged_pr_repos: set[str] = set()
            try:
                _prs = json.loads((CACHE_API / "prs.json").read_text())
                for _p in _prs:
                    if not (_p.get("pull_request") or {}).get("merged_at"):
                        continue
                    _m = re.search(r"/repos/([^/]+/[^/]+)$",
                                   _p.get("repository_url", ""))
                    if _m:
                        merged_pr_repos.add(_m.group(1))
            except Exception:
                pass

            # Build known_shas from existing cache
            existing_shas: set[str] = set()
            for recs in (_collect_all_records(),):
                for r in recs:
                    s = r.get("sha")
                    if s:
                        existing_shas.add(s)

            # Skip repos we already have data for
            covered = {r.get("repo")
                       for r in _collect_all_records() if r.get("repo")}
            todo = sorted(r for r in merged_pr_repos
                          if r not in covered and "/" in r)
            if todo:
                print(f">> [5b/7] Fetching commits in {len(todo)} PR-only repos ...",
                      file=sys.stderr)
                n = mine_commits_for_pr_repos(
                    todo, existing_shas, max_fetches=args.max_api_fetches)
                print(f">> {n} new commit records added", file=sys.stderr)
        except Exception as e:
            print(f"  ! PR-repo commits fetch failed: {e}", file=sys.stderr)

    # ---- Phase 6: star counts (full coverage) ----------------------------
    if not args.no_stars:
        report_progress("phase-6-stars",
                        "Fetching star counts + total-commits per repo")
        print(">> [6/7] Fetching star counts for all repos ...",
              file=sys.stderr)
        # Collect every canonical repo name in our data + every repo in
        # PRs/issues so we can show stars even for issue-only repos.
        agg = _render_now(mining_status="phase-6-fetching-stars")
        repo_names = set(agg["by_repo"].keys())
        n = fetch_stars_for_repos(repo_names, max_fetches=args.max_api_fetches)
        print(f">> {n} new star records fetched", file=sys.stderr)
        # Also fetch total-commits for each repo where the user has commits —
        # used to gate "stars earned" on a meaningful contribution share.
        print(">> [6b/7] Fetching total-commit counts (for share %) ...",
              file=sys.stderr)
        contributed_repos = [k for k, v in agg["by_repo"].items()
                             if "/" in k and v.get("commits", 0) > 0]
        fetched = 0
        for i, name in enumerate(contributed_repos, 1):
            if fetched >= args.max_api_fetches:
                print(f"  ! total-commits cap reached", file=sys.stderr)
                break
            r = fetch_repo_total_commits(name)
            if r is not None:
                fetched += 1
            if i % 25 == 0:
                print(f"    [{i}/{len(contributed_repos)}] {name}",
                      file=sys.stderr)
        print(f">> {fetched} repo total-commits fetched", file=sys.stderr)

    # ---- Phase 7: final aggregate + render -------------------------------
    print(">> [7/7] Final aggregate + render ...", file=sys.stderr)
    try:
        # Refresh profile (followers/sponsors) — short-lived cache.
        prof_cache = CACHE_API / "profile.json"
        if prof_cache.exists():
            # Refresh once per run if older than 1 hour
            import time as _t
            if _t.time() - prof_cache.stat().st_mtime > 3600:
                prof_cache.unlink()
        fetch_profile()
    except Exception as e:
        print(f"  ! profile fetch failed: {e}", file=sys.stderr)

    agg = _render_now()
    t = agg["totals"]
    print(f">> DONE: {t['commits']} commits, {t['repos']} repos, "
          f"{t['stars']:,}⭐ {t['prs_merged']}/{t['prs']} PRs merged, "
          f"{t['issues']} issues", file=sys.stderr)
    report_progress("done", "Mining complete",
                    commits=t['commits'], repos=t['repos'],
                    stars=t['stars'], prs=t['prs'], issues=t['issues'])
    print(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

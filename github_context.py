"""
github_context.py — Repository Context Fetching (raw materials only, no LLM calls here)

This module ONLY talks to GitHub. It fetches:
  - Tech stack (from manifest files)
  - Top-level folder structure
  - A filtered, capped listing of source file paths (for an LLM agent to reason over)
  - Arbitrary file contents, on demand, by validated path

The actual "which files matter and why" reasoning is done by a separate LLM agent
(skills_repo_synth.md) in app.py — this module deliberately stays dumb and cheap,
since naive substring keyword-matching (an earlier version of this file) missed
files that don't share vocabulary with the UI labels but ARE semantically relevant.

Design notes / constraints (verified against the live API):
  - GitHub's Code Search API (`/search/code`) now REQUIRES authentication, even for
    public repos. Since this is a no-auth, public-only flow, we don't use it.
  - Instead: fetch the full repo file tree ONCE (1 API call against the 60/hr
    unauthenticated core rate limit). An LLM agent then picks relevant paths from
    that listing — no further core API calls needed for the picking step.
  - Actual file CONTENTS are fetched via raw.githubusercontent.com, which is
    NOT subject to the api.github.com rate limit at all.
  - Total core API cost per run: ~2 calls (get default branch + get tree),
    regardless of how many files end up being inspected.
"""

import re
import requests

GITHUB_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

EXCLUDED_DIR_PARTS = {
    "node_modules", "dist", "build", ".git", "vendor", "venv", ".venv",
    "__pycache__", "coverage", ".next", "target", "bin", "obj",
}
RELEVANT_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".py", ".java", ".kt", ".go",
    ".rb", ".php", ".cs", ".html", ".css", ".scss",
}
MANIFEST_FILES = {
    "package.json": "Node.js / JavaScript",
    "requirements.txt": "Python",
    "pyproject.toml": "Python",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "Gemfile": "Ruby",
    "go.mod": "Go",
    "composer.json": "PHP",
}


def parse_github_url(url):
    """Extract (owner, repo) from a github.com URL. Returns None if invalid."""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url.strip())
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    repo = repo.removesuffix(".git")
    return owner, repo


def get_default_branch(owner, repo):
    resp = requests.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}", timeout=10)
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        return None, (
            "GitHub's unauthenticated rate limit (60 requests/hour, shared across "
            "this network) is exhausted for now. It resets on a rolling hourly window — "
            "try again shortly, or add a free GitHub personal access token to raise this "
            "to 5,000/hour if you're hitting this often."
        )
    if resp.status_code != 200:
        return None, f"Repo lookup failed ({resp.status_code}). Is it public and spelled correctly?"
    return resp.json().get("default_branch", "main"), None


def get_repo_tree(owner, repo, branch):
    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        timeout=15,
    )
    if resp.status_code != 200:
        return [], f"Tree fetch failed ({resp.status_code})."
    data = resp.json()
    files = [item for item in data.get("tree", []) if item.get("type") == "blob"]
    return files, None


def _is_excluded(path):
    parts = set(path.split("/"))
    return bool(parts & EXCLUDED_DIR_PARTS)


def fetch_raw_file(owner, repo, branch, path, max_chars=1500):
    resp = requests.get(f"{RAW_BASE}/{owner}/{repo}/{branch}/{path}", timeout=10)
    if resp.status_code != 200:
        return None
    text = resp.text
    return text[:max_chars] + ("\n... (truncated)" if len(text) > max_chars else "")


def detect_tech_stack(owner, repo, branch, tree_files):
    """Look for known manifest files near the root and summarize dependencies."""
    tree_paths = {f["path"] for f in tree_files}
    findings = []

    for manifest_name, label in MANIFEST_FILES.items():
        matches = [p for p in tree_paths if p == manifest_name or p.endswith(f"/{manifest_name}")]
        for path in sorted(matches, key=len)[:1]:  # prefer shortest (closest to root)
            content = fetch_raw_file(owner, repo, branch, path, max_chars=4000)
            if not content:
                continue
            findings.append(f"- **{label}** detected (`{path}`)")
            if manifest_name == "package.json":
                dep_blocks = re.findall(r'"(?:dev)?[Dd]ependencies"\s*:\s*\{([^}]*)\}', content)
                deps = []
                for block in dep_blocks:
                    deps.extend(re.findall(r'"([a-zA-Z0-9@_/.\-]+)"\s*:\s*"[^"]+"', block))
                if deps:
                    findings.append(f"  Full dependency list: {', '.join(deps)}")
            elif manifest_name == "requirements.txt":
                pkgs = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith("#")]
                if pkgs:
                    findings.append(f"  Full dependency list: {', '.join(pkgs)}")

    return "\n".join(findings) if findings else "No recognized manifest file found near repo root."


def get_top_level_structure(tree_files, max_entries=25):
    top_level = sorted({f["path"].split("/")[0] for f in tree_files if not _is_excluded(f["path"])})
    return ", ".join(top_level[:max_entries])


def build_path_listing(tree_files, max_paths=350):
    """
    A filtered, capped listing of source file paths for an LLM agent to read and
    reason over (which files are architecturally relevant to a given feature).
    Sorted shortest-path-first so root/architecturally-central files surface first
    when the list has to be truncated.
    """
    candidates = [
        f["path"] for f in tree_files
        if not _is_excluded(f["path"])
        and ("." + f["path"].split(".")[-1] if "." in f["path"] else "") in RELEVANT_EXTENSIONS
        and f.get("size", 0) <= 60000
    ]
    candidates.sort(key=len)
    truncated = len(candidates) > max_paths
    return candidates[:max_paths], truncated


def fetch_files_by_paths(owner, repo, branch, paths, tree_files, max_chars=1000, max_files=10):
    """
    Fetch content for a list of paths an LLM agent selected — but only paths that
    actually exist in the real tree (prevents fetching/trusting a hallucinated path).
    """
    valid_paths = {f["path"] for f in tree_files}
    snippets = []
    for path in paths[:max_files]:
        path = path.strip().strip("`")
        if path not in valid_paths:
            continue
        content = fetch_raw_file(owner, repo, branch, path, max_chars=max_chars)
        if content:
            snippets.append(f"**`{path}`**\n```\n{content}\n```")
    return snippets


def fetch_raw_materials(repo_url):
    """
    Stage 1 (no LLM): fetch everything an LLM agent would need to reason about
    this repo. Returns (bundle_dict, error_message).
    """
    parsed = parse_github_url(repo_url)
    if not parsed:
        return None, "Couldn't parse a GitHub repo from that URL. Expected format: https://github.com/owner/repo"
    owner, repo = parsed

    branch, err = get_default_branch(owner, repo)
    if err:
        return None, err

    tree_files, err = get_repo_tree(owner, repo, branch)
    if err:
        return None, err

    tech_stack = detect_tech_stack(owner, repo, branch, tree_files)
    top_level = get_top_level_structure(tree_files)
    path_listing, was_truncated = build_path_listing(tree_files)

    return {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "tree_files": tree_files,
        "tech_stack": tech_stack,
        "top_level": top_level,
        "path_listing": path_listing,
        "path_listing_truncated": was_truncated,
    }, None
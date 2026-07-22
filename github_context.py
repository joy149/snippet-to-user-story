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

# Matches a file path wrapped in backticks with a recognizable extension, e.g. `src/foo/Bar.jsx`
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+\.[a-zA-Z0-9]{1,10})`")
# Fallback: same shape but without requiring backticks (in case the model drops them),
# anchored so we don't grab stray sentence fragments — must look like a path (contains a slash
# or a dot before the extension) and be reasonably path-like (no spaces).
_BARE_PATH_RE = re.compile(r"(?<![`\w])([\w./\-]+/[\w.\-]+\.[a-zA-Z0-9]{1,10})(?![`\w])")

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


def extract_picked_paths(selector_brief):
    """
    Pull candidate file paths out of the file-selector LLM's free-text output.

    The skill instructs the model to wrap paths in backticks, but models don't
    always comply exactly, and previously any deviation meant we'd silently
    fall back to zero picked files (repo grounding degrades quietly with no
    visible signal to the user). This function:
      1. Tries the strict backtick-wrapped pattern first (what the skill asks for).
      2. If that yields nothing, falls back to a looser bare-path pattern.
      3. Reports whether the fallback was needed, so the caller can log/warn
         instead of failing silently.

    Returns (paths, used_fallback: bool). `paths` may still contain entries
    that don't exist in the real repo tree — the caller (fetch_files_by_paths)
    is responsible for validating against the real tree before fetching.
    """
    strict_matches = _BACKTICK_PATH_RE.findall(selector_brief)
    if strict_matches:
        seen = set()
        ordered = []
        for p in strict_matches:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        return ordered, False

    fallback_matches = _BARE_PATH_RE.findall(selector_brief)
    seen = set()
    ordered = []
    for p in fallback_matches:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered, bool(ordered)


def parse_github_url(url):
    """Extract (owner, repo) from a github.com URL. Returns None if invalid."""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url.strip())
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    repo = repo.removesuffix(".git")
    return owner, repo


def _auth_headers(token=None):
    """Build request headers, adding an Authorization header if a token was supplied.
    A token is entirely optional — the module still works unauthenticated — but
    raises the core API rate limit from 60/hr to 5,000/hr when provided."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def get_default_branch(owner, repo, token=None):
    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}", headers=_auth_headers(token), timeout=10
    )
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        if token:
            return None, (
                "GitHub's authenticated rate limit (5,000 requests/hour) is exhausted "
                "for now. It resets on a rolling hourly window — try again shortly."
            )
        return None, (
            "GitHub's unauthenticated rate limit (60 requests/hour, shared across "
            "this network) is exhausted for now. It resets on a rolling hourly window — "
            "try again shortly, or add a free GitHub personal access token to raise this "
            "to 5,000/hour if you're hitting this often."
        )
    if resp.status_code == 401:
        return None, "GitHub token was rejected (401 Unauthorized). Check that it's valid and not expired."
    if resp.status_code != 200:
        return None, f"Repo lookup failed ({resp.status_code}). Is it public and spelled correctly?"
    return resp.json().get("default_branch", "main"), None


def get_repo_tree(owner, repo, branch, token=None):
    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        headers=_auth_headers(token),
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


import os

_JS_TS_IMPORT_RE = re.compile(r"""(?:import|export)\s+(?:[\w\s{},*]+from\s+)?['"](\.[^'"]+)['"]|require\(['"](\.[^'"]+)['"]\)""")
_PY_IMPORT_RE = re.compile(r"""from\s+(\.[^\s]+)\s+import|import\s+(\.[^\s]+)""")


def resolve_imported_dependencies(snippets_fetched, owner, repo, branch, tree_files, max_extra_files=3, max_chars=2500):
    """
    Parse relative imports inside already-fetched code snippets, resolve them against tree_files,
    and fetch content for those imported dependency files so Pass 2 has graph awareness.
    `snippets_fetched` is a list of (path, content) tuples.
    """
    valid_paths = {f["path"] for f in tree_files}
    already_fetched = {p for p, _ in snippets_fetched}
    extra_snippets = []

    for path, content in snippets_fetched:
        if len(extra_snippets) >= max_extra_files:
            break
        if not content:
            continue

        dir_name = os.path.dirname(path)
        raw_imports = []

        # JS/TS relative imports
        for m in _JS_TS_IMPORT_RE.finditer(content):
            rel = m.group(1) or m.group(2)
            if rel:
                raw_imports.append(rel)

        # Python relative imports
        for m in _PY_IMPORT_RE.finditer(content):
            rel = m.group(1) or m.group(2)
            if rel:
                raw_imports.append(rel.replace(".", "/"))

        for rel in raw_imports:
            if len(extra_snippets) >= max_extra_files:
                break

            norm_target = os.path.normpath(os.path.join(dir_name, rel)).replace("\\", "/")
            candidates = [norm_target]
            if not os.path.splitext(norm_target)[1]:
                candidates.extend([
                    f"{norm_target}.ts", f"{norm_target}.tsx", f"{norm_target}.js", f"{norm_target}.jsx",
                    f"{norm_target}/index.ts", f"{norm_target}/index.tsx", f"{norm_target}/index.js",
                    f"{norm_target}.py"
                ])

            for cand in candidates:
                if cand in valid_paths and cand not in already_fetched:
                    extra_content = fetch_raw_file(owner, repo, branch, cand, max_chars=max_chars)
                    if extra_content:
                        already_fetched.add(cand)
                        extra_snippets.append(
                            f"**`{cand}`** *(automatically included import dependency of `{path}`)*\n```\n{extra_content}\n```"
                        )
                    break

    return extra_snippets


def get_top_level_structure(tree_files, max_entries=25):
    top_level = sorted({f["path"].split("/")[0] for f in tree_files if not _is_excluded(f["path"])})
    return ", ".join(top_level[:max_entries])


def build_path_listing(tree_files, max_paths=600):
    """
    A filtered, capped listing of source file paths for an LLM agent to read and
    reason over (which files are architecturally relevant to a given feature).
    Depth-balanced sorting so deeply nested feature components aren't excluded.
    """
    candidates = [
        f["path"] for f in tree_files
        if not _is_excluded(f["path"])
        and ("." + f["path"].split(".")[-1] if "." in f["path"] else "") in RELEVANT_EXTENSIONS
        and f.get("size", 0) <= 60000
    ]
    # Balanced sort by directory depth first, then length
    candidates.sort(key=lambda p: (p.count("/"), len(p)))
    truncated = len(candidates) > max_paths
    return candidates[:max_paths], truncated


def fetch_files_by_paths(owner, repo, branch, paths, tree_files, max_chars=2500, max_files=12, resolve_imports=True):
    """
    Fetch content for a list of paths an LLM agent selected — but only paths that
    actually exist in the real tree. Automatically resolves imported dependencies if enabled.
    """
    valid_paths = {f["path"] for f in tree_files}
    snippets = []
    fetched_records = []
    for path in paths[:max_files]:
        path = path.strip().strip("`")
        if path not in valid_paths:
            continue
        content = fetch_raw_file(owner, repo, branch, path, max_chars=max_chars)
        if content:
            snippets.append(f"**`{path}`**\n```\n{content}\n```")
            fetched_records.append((path, content))

    if resolve_imports and fetched_records:
        extra_snippets = resolve_imported_dependencies(
            fetched_records, owner, repo, branch, tree_files, max_extra_files=3, max_chars=max_chars
        )
        snippets.extend(extra_snippets)

    return snippets


def fetch_raw_materials(repo_url, token=None):
    """
    Stage 1 (no LLM): fetch everything an LLM agent would need to reason about
    this repo. Returns (bundle_dict, error_message).
    """
    parsed = parse_github_url(repo_url)
    if not parsed:
        return None, "Couldn't parse a GitHub repo from that URL. Expected format: https://github.com/owner/repo"
    owner, repo = parsed

    branch, err = get_default_branch(owner, repo, token=token)
    if err:
        return None, err

    tree_files, err = get_repo_tree(owner, repo, branch, token=token)
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
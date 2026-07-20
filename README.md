# snippet-to-user-story

Turns uploaded UI screenshots/mocks (including reviewer markup like circled
annotations or red-ink change requests) into structured, developer-ready user
stories — optionally grounded against a real GitHub repository so the
generated tasks reference actual file paths, dependencies, and coding
conventions instead of generic scaffolding.

## How it works

A multi-agent pipeline (via the OpenAI API) processes each run:

1. **Visual Auditor** (`skills_agent1.md`) — extracts a factual inventory of
   every UI element from the screenshot(s), separating the app's real
   rendered state from any reviewer-added annotations/markup.
2. **Decomposition Architect** (`skills_agent2.md`) — partitions that
   inventory into independent, MECE feature nodes.
3. **Repo Context Enrichment** *(optional)* — if a GitHub repo URL is
   provided:
   - fetches the repo's file tree and manifest/dependency info
     (`github_context.py`, no LLM calls),
   - a **File & Dependency Selector** (`skills_repo_synth.md`) picks which
     real files are worth reading,
   - an **Architecture & Pattern Synthesizer** (`skills_repo_architecture.md`)
     reads that real code and detects actual conventions to follow.
4. **Agile Writer** (`skills_agent3.md`) — compiles final developer epics
   (one call per feature node), including a dedicated epic for any
   reviewer-requested change, grounded in the repo context above when
   available.

Agent 2's output and the repo-grounding pipeline are independent of each
other (both only depend on Agent 1's output), so they run **in parallel** to
cut wall-clock time.

## Setup

```bash
pip3 install -r requirements.txt
export OPENAI_API_KEY="sk-..."   # required
python3 -m streamlit run app.py
```

## Using repo context enrichment

In the sidebar, optionally provide:

- **Public GitHub repo URL** — e.g. `https://github.com/owner/repo`. No
  token required for public repos.
- **GitHub personal access token** *(optional)* — raises GitHub's core API
  rate limit from 60 requests/hour (unauthenticated) to 5,000/hour.
  Useful if you're re-running the pipeline against the same repo
  repeatedly in one session. Never stored beyond the browser session.

Repo trees are cached per-session per-URL, so re-running the pipeline
against the same repo (e.g. after tweaking a screenshot) doesn't re-spend
an API call on re-fetching the tree.

## Collapse mode

For 1–2 screenshots, "Collapse mode" merges the Visual Auditor and
Decomposition Architect steps into a single model call to save tokens. For
more complex, multi-screen flows, leave it off to run the full pipeline.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI + pipeline orchestration |
| `github_context.py` | GitHub API access (tree fetch, file fetch, dependency parsing) — no LLM calls |
| `pipeline_utils.py` | Pure helper logic (e.g. splitting a blueprint into feature nodes), kept Streamlit-free so it's directly testable |
| `skills_agent1.md` / `skills_agent2.md` / `skills_agent3.md` | System prompts for the three main pipeline agents |
| `skills_repo_synth.md` / `skills_repo_architecture.md` | System prompts for the two-pass repo grounding agents |

## Running tests

```bash
pip install -r requirements.txt
pytest -v
```

The test suite (`test_github_context.py`, `test_pipeline_utils.py`) covers
the pure, deterministic logic — URL parsing, path filtering, dependency
parsing, the feature-node splitter and its schema-deviation fallback — with
no network calls, so it runs in well under a second. `app.py` itself isn't
imported by the tests since it builds Streamlit UI at import time; its
testable logic lives in `pipeline_utils.py` for exactly this reason.

CI (`.github/workflows/test.yml`) runs the suite automatically on every push
and pull request against Python 3.10–3.12.
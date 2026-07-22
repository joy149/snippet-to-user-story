# snippet-to-user-story

Turns uploaded UI screenshots/mocks (including reviewer markup like circled
annotations or red-ink change requests) into structured, developer-ready user
stories — grounded against a real GitHub repository so the generated tasks
reference actual file paths, dependencies, and coding conventions instead of generic scaffolding.

## How it works

A multi-agent pipeline (via the OpenAI API) processes each run:

1. **Visual Auditor** (`skills_agent1.md`) — extracts a factual inventory of
   every UI element from the screenshot(s), separating the app's real
   rendered state from any reviewer-added annotations/markup.
2. **Decomposition Architect** (`skills_agent2.md`) — partitions that
   inventory into independent, MECE feature nodes.
3. **Repo Context Enrichment (RAG Grounding)** *(optional)* — if a GitHub repo URL is provided:
   - **Tree & Dependency Fetch**: Fetches repo structure and dependency manifests (`github_context.py`, no LLM calls).
   - **Semantic Code Search**: Chunking (`CHUNK_MAX_CHARS=2400`) and batch embeddings (`text-embedding-3-small`) with **multi-query max-similarity fusion** to surface semantically related code (`code_search.py`).
   - **Import Graph Resolution**: Automatically traces local JS/TS/Python relative imports from selected files to pull in dependent helper/service files.
   - **Pass 1 - File Selector** (`skills_repo_synth.md`): Evaluates route matches, dependencies, semantic search hits, and tree paths to pick target files.
   - **Pass 2 - Architecture Synthesizer** (`skills_repo_architecture.md`): Reads actual code content, verifies Pass 1 verdicts, and extracts exact symbols, state patterns, and conventions.
4. **Agile Writer (Gap Analysis & Delta Engine)** (`skills_agent3.md`) — compiles final developer epics:
   - **Repo Context Available**: Performs a Gap Analysis against the codebase:
     - *Already Implemented*: Emits a 1-line `✅ Already Implemented` status banner (skipping redundant task lists).
     - *Existing + Reviewer Markup*: Emits a `🔧 DELTA / MODIFICATION EPIC` with tasks ONLY for the code diff targeting existing files.
     - *Net-New*: Emits a `🆕 NET-NEW DEVELOPER EPIC` with creation tasks grounded in detected repo conventions.
   - **No Repo Context**: Treats all features as 100% scratch development.

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

### Performance & Caching
- **Repo Trees**: Cached per-session per-URL to save GitHub REST API calls.
- **Embedding Index**: Saved to local disk cache (`.cache_code_index/`), reducing re-run indexing latency to **~0 ms** and spending zero OpenAI embedding API calls on repeated runs.

## Collapse mode

For 1–2 screenshots, "Collapse mode" merges the Visual Auditor and
Decomposition Architect steps into a single model call to save tokens. For
more complex, multi-screen flows, leave it off to run the full pipeline.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI + pipeline orchestration |
| `github_context.py` | GitHub API access (tree fetch, file fetch, dependency parsing, import graph resolution) — no LLM calls |
| `code_search.py` | Dense vector embedding index (`text-embedding-3-small`), AST boundary chunking, multi-query search fusion, and disk persistence |
| `pipeline_utils.py` | Pure helper logic (e.g. splitting a blueprint into feature nodes), kept Streamlit-free so it's directly testable |
| `skills_agent1.md` / `skills_agent2.md` / `skills_agent3.md` | System prompts for the main pipeline agents (`agent3` acts as Gap Analysis Delta Engine) |
| `skills_repo_synth.md` / `skills_repo_architecture.md` | System prompts for the two-pass repo grounding agents |

## Running tests

```bash
pip install -r requirements.txt
pytest -v
```

The test suite (`test_github_context.py`, `test_code_search.py`, `test_pipeline_utils.py`) covers
all deterministic logic — URL parsing, path filtering, dependency parsing, AST boundary chunking, multi-query search fusion, import graph resolution, and disk caching — with
no network calls, so it runs in well under a second. `app.py` itself isn't
imported by the tests since it builds Streamlit UI at import time; its
testable logic lives in standalone modules for exactly this reason.

CI (`.github/workflows/test.yml`) runs the suite automatically on every push
and pull request against Python 3.10–3.12.
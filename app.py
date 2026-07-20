import streamlit as st
import base64
import os
import re
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
import github_context
import code_search
from pipeline_utils import split_into_feature_nodes

# 1. Page Configuration
st.set_page_config(page_title="Developer User Story Agent", layout="wide", page_icon="⚙️")
st.title("⚙️ Developer User Story Extraction Engine")
st.caption("Multi-agent pipeline (OpenAI API) with an optional token-saving collapse mode.")

# --- Client setup ---
# NEVER hardcode keys. Set OPENAI_API_KEY as an environment variable before running:
#   export OPENAI_API_KEY="sk-..."
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

MODEL_VISION = "gpt-4o"        # needed for the image-understanding step
MODEL_TEXT = "gpt-4o-mini"     # cheaper for pure text-restructuring steps
# Note: OpenAI applies automatic prompt caching server-side for repeated prefixes
# over ~1024 tokens, at 50% discount — no code changes needed to benefit from it,
# but keeping your system prompt text identical across calls maximizes the cache hit.

# Helper to read skill markdown files
def load_skill_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    else:
        st.error(f"Missing skill configuration file: `{filename}`")
        return None

# Helper to encode images
def encode_image(uploaded_file):
    uploaded_file.seek(0)
    return base64.b64encode(uploaded_file.read()).decode("utf-8")

def image_data_url(file):
    ext = file.name.lower().split(".")[-1]
    media_type = "image/png" if ext == "png" else "image/jpeg"
    return f"data:{media_type};base64,{encode_image(file)}"

def call_openai_raw(model, system_prompt, user_content, max_tokens=2000):
    """Thread-safe variant — never touches Streamlit APIs (st.* calls aren't safe
    from background threads). Returns (content, error_message)."""
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content, None
    except Exception as e:
        return None, str(e)


def call_openai(model, system_prompt, user_content, max_tokens=2000):
    """Streamlit-facing wrapper for sequential (main-thread) call sites — surfaces
    failures via st.error. For calls made inside a background thread, use
    call_openai_raw directly and handle the error on the main thread instead."""
    content, err = call_openai_raw(model, system_prompt, user_content, max_tokens)
    if err:
        st.error(f"API call failed: {err}")
    return content


def run_repo_grounding_pipeline(github_repo_url, github_token, agent_1_output,
                                cached_bundle=None, cached_code_index=None):
    """
    The full repo-context enrichment path (raw fetch -> file selector LLM pass ->
    architecture LLM pass), factored out so it contains NO Streamlit calls and is
    therefore safe to run on a background thread in parallel with Agent 2 (item 5) —
    the two are independent since neither depends on the other's output, both only
    need agent_1_output.

    Also applies the hardened path extraction (item 1: falls back gracefully instead
    of silently yielding zero files if the model doesn't use backticks).

    Per-session caching (item 2) is handled entirely by the CALLER on the main
    thread: st.session_state must never be read or written from a background
    thread in Streamlit, so this function accepts already-cached objects
    (cached_bundle, cached_code_index) if the caller has them, and — when it
    has to build fresh — returns them back in the result dict under
    "bundle_to_cache" / "code_index_to_cache" so the caller can store them
    after the thread finishes, on the main thread.

    Returns a dict:
      {"context": str, "status": str, "raw_snippets": str, "warning": str|None,
       "bundle_to_cache": dict|None, "code_index_to_cache": CodeIndex|None}
    """
    if not github_repo_url.strip():
        return {
            "context": "",
            "status": "No repo URL provided — stories written without repo grounding.",
            "raw_snippets": "",
            "semantic_hits": "",
            "warning": None,
            "bundle_to_cache": None,
            "code_index_to_cache": None,
        }

    from_cache = cached_bundle is not None
    if cached_bundle is not None:
        bundle, ctx_err = cached_bundle, None
    else:
        bundle, ctx_err = github_context.fetch_raw_materials(github_repo_url, token=github_token or None)

    if ctx_err:
        return {
            "context": "",
            "status": f"❌ Skipped — {ctx_err}",
            "raw_snippets": "",
            "semantic_hits": "",
            "warning": f"Repo context skipped: {ctx_err}",
            "bundle_to_cache": None,
            "code_index_to_cache": None,
        }

    cache_note = " _(reused from an earlier fetch this session)_" if from_cache else ""

    # --- Semantic code search: build or reuse the embedding index ---
    code_index = cached_code_index
    code_index_to_cache = None
    semantic_hits_block = ""
    index_warning = None

    if code_index is None and bundle["path_listing"]:
        code_index, idx_err = code_search.build_index(
            client,
            bundle["owner"], bundle["repo"], bundle["branch"],
            bundle["path_listing"],
        )
        if idx_err:
            index_warning = f"Semantic code index skipped: {idx_err}"
        if code_index is not None:
            code_index_to_cache = code_index    # caller caches on main thread

    if code_index is not None:
        hits = code_search.search(client, code_index, agent_1_output, top_k=15)
        semantic_hits_block = code_search.format_hits_for_prompt(hits, max_hits=10)

    # --- Build the file-selector prompt ---
    skill_repo_synth = load_skill_file("skills_repo_synth.md")
    path_listing_text = "\n".join(bundle["path_listing"])
    truncation_note = (
        "\n(Note: path listing was truncated to fit budget — "
        "treat absence from this list as 'unknown', not 'doesn't exist'.)"
        if bundle["path_listing_truncated"] else ""
    )

    # Inject semantic hits (if any) between the UI audit and the path listing,
    # so the LLM sees content-based evidence BEFORE reasoning over bare paths.
    semantic_section = (
        f"\n\n{semantic_hits_block}\n"
        if semantic_hits_block
        else ""
    )

    selector_user_content = [{
        "type": "text",
        "text": (
            f"UI audit (what the screenshot(s) show, including any requested changes):\n"
            f"{agent_1_output}\n\n"
            f"Detected tech stack:\n{bundle['tech_stack']}\n\n"
            f"Top-level structure: {bundle['top_level']}"
            f"{semantic_section}\n\n"
            f"Source file path listing ({len(bundle['path_listing'])} files):"
            f"{truncation_note}\n{path_listing_text}"
        ),
    }]
    selector_brief, sel_err = call_openai_raw(MODEL_TEXT, skill_repo_synth, selector_user_content, max_tokens=1200)

    # Only newly-fetched bundles need caching back by the caller; a bundle that
    # was already served from cache doesn't need re-storing.
    bundle_to_cache = None if from_cache else bundle

    if selector_brief is None:
        return {
            "context": "",
            "status": "❌ Skipped — file selector LLM call failed.",
            "raw_snippets": "",
            "semantic_hits": semantic_hits_block,
            "warning": f"Repo file-selector call failed: {sel_err}" if sel_err else None,
            "bundle_to_cache": bundle_to_cache,
            "code_index_to_cache": code_index_to_cache,
        }

    # --- Item 1: robust path extraction with an explicit fallback signal ---
    picked_paths, used_fallback = github_context.extract_picked_paths(selector_brief)
    warning = index_warning   # carry forward any index-build warning
    if used_fallback:
        warning = (
            "Heads up: the file-selector step didn't wrap its picked paths in backticks "
            "as instructed, so a looser fallback matcher was used to recover them. "
            "Worth double-checking the repo grounding brief below looks sensible."
        )
    elif not picked_paths:
        warning = (
            "The file-selector step didn't reference any file paths — proceeding with "
            "no fetched code (treated as net-new functionality)."
        )

    snippets = github_context.fetch_files_by_paths(
        bundle["owner"], bundle["repo"], bundle["branch"],
        picked_paths, bundle["tree_files"],
        max_chars=2500, max_files=12,
    )

    if not snippets:
        return {
            "context": (
                f"### Repository: `{bundle['owner']}/{bundle['repo']}` (branch: `{bundle['branch']}`){cache_note}\n\n"
                f"{selector_brief}\n\n"
                f"_No existing files were relevant enough to fetch — treat this as net-new functionality "
                f"with no established pattern to follow yet in this repo._"
            ),
            "status": (
                f"✅ Checked `{bundle['owner']}/{bundle['repo']}` — no relevant existing files found "
                f"(net-new functionality)."
            ),
            "raw_snippets": "",
            "semantic_hits": semantic_hits_block,
            "warning": warning,
            "bundle_to_cache": bundle_to_cache,
            "code_index_to_cache": code_index_to_cache,
        }

    skill_repo_arch = load_skill_file("skills_repo_architecture.md")
    arch_user_content = [{
        "type": "text",
        "text": (
            f"UI audit:\n{agent_1_output}\n\n"
            f"File & Dependency Selector's findings so far:\n{selector_brief}\n\n"
            f"Actual fetched code from the files selected above:\n\n"
            + "\n\n".join(snippets)
        ),
    }]
    architecture_brief, arch_err = call_openai_raw(MODEL_TEXT, skill_repo_arch, arch_user_content, max_tokens=1800)
    if architecture_brief is None:
        architecture_brief = "_(Architecture synthesis failed — falling back to file-selector findings only.)_"
        arch_warning = f"Architecture synthesis call failed: {arch_err}" if arch_err else None
        warning = f"{warning} {arch_warning}" if (warning and arch_warning) else (warning or arch_warning)

    # Include semantic hit count in the status line for visibility
    semantic_note = ""
    if semantic_hits_block:
        hit_count = semantic_hits_block.count("\n|") - 2   # subtract header rows
        semantic_note = f" Semantic code index: {hit_count} content-matched file(s) surfaced."

    return {
        "context": (
            f"### Repository: `{bundle['owner']}/{bundle['repo']}` (branch: `{bundle['branch']}`){cache_note}\n\n"
            f"{selector_brief}\n\n"
            f"{architecture_brief}"
        ),
        "status": (
            f"✅ Grounded against `{bundle['owner']}/{bundle['repo']}` "
            f"— {len(snippets)} real file(s) read and analyzed for actual "
            f"architecture/style, not just dependency-checked.{semantic_note}"
        ),
        "raw_snippets": "\n\n".join(snippets),
        "semantic_hits": semantic_hits_block,
        "warning": warning,
        "bundle_to_cache": bundle_to_cache,
        "code_index_to_cache": code_index_to_cache,
    }

# Initialize session states
for key in ["agent_1_output", "agent_2_output", "agent_3_output", "repo_context",
            "repo_context_status", "repo_context_raw_snippets", "repo_semantic_hits"]:
    if key not in st.session_state:
        st.session_state[key] = ""

# Cache of fetched repo bundles keyed by the repo URL, so re-running the pipeline
# against the same repo (e.g. tweaking a screenshot and retrying) doesn't re-spend
# a tree-fetch API call against the 60/hr (or 5,000/hr with a token) rate limit for
# no reason. Cleared implicitly when the browser session ends.
#
# IMPORTANT: this cache is only ever read/written from the main Streamlit thread
# (see get_cached_bundle / cache_bundle below) — never from inside a background
# thread, since st.session_state access isn't safe off the main script thread.
if "repo_bundle_cache" not in st.session_state:
    st.session_state.repo_bundle_cache = {}

# Cache of embedding indexes keyed by repo URL, so re-running the pipeline
# against the same repo doesn't re-fetch + re-embed all files.  Same
# main-thread-only constraint as the bundle cache above.
if "code_index_cache" not in st.session_state:
    st.session_state.code_index_cache = {}


def _bundle_cache_key(repo_url):
    return repo_url.strip().rstrip("/").lower()


def get_cached_bundle(repo_url):
    """Main-thread-only read of the repo bundle cache. Returns bundle dict or None."""
    return st.session_state.repo_bundle_cache.get(_bundle_cache_key(repo_url))


def cache_bundle(repo_url, bundle):
    """Main-thread-only write to the repo bundle cache."""
    if bundle is not None:
        st.session_state.repo_bundle_cache[_bundle_cache_key(repo_url)] = bundle


def get_cached_code_index(repo_url):
    """Main-thread-only read of the code embedding index cache. Returns CodeIndex or None."""
    return st.session_state.code_index_cache.get(_bundle_cache_key(repo_url))


def cache_code_index(repo_url, code_index):
    """Main-thread-only write to the code embedding index cache."""
    if code_index is not None:
        st.session_state.code_index_cache[_bundle_cache_key(repo_url)] = code_index

# 2. Sidebar Configuration
with st.sidebar:
    st.header("📥 Input UI Screenshots")
    uploaded_files = st.file_uploader(
        "Upload reference app screens:",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
    )
    st.markdown("---")
    st.subheader("⚡ Token Optimization")
    collapse_mode = st.checkbox(
        "Collapse mode (1-2 screenshots)",
        value=len(uploaded_files) <= 2 if uploaded_files else False,
        help="Merges visual audit + component boundary steps into a single call. "
             "Saves tokens on small inputs; use full 3-stage pipeline for complex, multi-screen flows.",
    )
    st.markdown("---")
    st.subheader("🔗 Repo Context Enrichment")
    github_repo_url = st.text_input(
        "Public GitHub repo URL (optional)",
        placeholder="https://github.com/owner/repo",
        help="Pulls tech stack + existing related files from this repo so generated "
             "stories reference real file paths and conventions instead of generic scaffolding. "
             "Public repos only.",
    )
    github_token = st.text_input(
        "GitHub personal access token (optional)",
        type="password",
        placeholder="ghp_...",
        help="Not required — public repos work without one. Raises GitHub's core API "
             "rate limit from 60/hour to 5,000/hour, which helps if you're re-running "
             "this pipeline against the same repo repeatedly in a session. "
             "Never stored beyond this browser session.",
    )
    st.markdown("---")
    generate_btn = st.button("🚀 Process Developer Stories", type="primary", disabled=not uploaded_files)

# 3. Main Screen Layout Splitting
col1, col2 = st.columns([1, 1.4], gap="large")

with col1:
    st.subheader("🖼️ Loaded UI Assets")
    if uploaded_files:
        for file in uploaded_files:
            st.image(file, caption=file.name, use_container_width=True)
    else:
        st.info("Upload screenshots to map developer stories.")

with col2:
    st.subheader("📝 Engineering Specifications Workspace")

    if generate_btn and uploaded_files:
        skill_1 = load_skill_file("skills_agent1.md")
        skill_2 = load_skill_file("skills_agent2.md")
        skill_3 = load_skill_file("skills_agent3.md")

        if skill_1 and skill_2 and skill_3:

            image_blocks = [
                {"type": "image_url", "image_url": {"url": image_data_url(f)}}
                for f in uploaded_files
            ]

            if collapse_mode:
                # --- COLLAPSED: Visual Audit + Boundary Mapping in one call ---
                with st.status("👁️📐 Running Collapsed Visual + Boundary Pass...", expanded=True) as status:
                    combined_skill = f"{skill_1}\n\n---\n\n{skill_2}"
                    user_content = [{"type": "text", "text": "Extract visual elements AND partition into component boundaries in one pass."}] + image_blocks
                    result = call_openai(MODEL_VISION, combined_skill, user_content)
                    if result is None:
                        st.stop()
                    st.session_state.agent_1_output = result
                    st.session_state.agent_2_output = result
                    status.update(label="👁️📐 Visual + Boundary Pass Complete", state="complete")

                # Nothing else to overlap this with in collapse mode (there's no
                # separate Agent 2 call to parallelize against), so run it plainly.
                with st.status("🔗 Running repo context enrichment (incl. semantic code index)...", expanded=True) as status:
                    cached_bundle = get_cached_bundle(github_repo_url) if github_repo_url.strip() else None
                    cached_code_idx = get_cached_code_index(github_repo_url) if github_repo_url.strip() else None
                    repo_result = run_repo_grounding_pipeline(
                        github_repo_url, github_token, st.session_state.agent_1_output,
                        cached_bundle=cached_bundle, cached_code_index=cached_code_idx,
                    )
                    cache_bundle(github_repo_url, repo_result["bundle_to_cache"])
                    cache_code_index(github_repo_url, repo_result["code_index_to_cache"])
                    st.session_state.repo_context = repo_result["context"]
                    st.session_state.repo_context_status = repo_result["status"]
                    st.session_state.repo_context_raw_snippets = repo_result["raw_snippets"]
                    st.session_state.repo_semantic_hits = repo_result["semantic_hits"]
                    if repo_result["warning"]:
                        st.warning(repo_result["warning"])
                    status.update(label="🔗 Repo Grounding Complete", state="complete")
            else:
                # --- AGENT 1: THE VISUAL AUDITOR ---
                with st.status("👁️ Executing Visual Component Audit...", expanded=True) as status:
                    user_content = [{"type": "text", "text": "Extract all visual elements factually."}] + image_blocks
                    result_1 = call_openai(MODEL_VISION, skill_1, user_content)
                    if result_1 is None:
                        st.stop()
                    st.session_state.agent_1_output = result_1
                    status.update(label="👁️ Visual Components Extracted", state="complete")

                # --- AGENT 2 (Component Boundaries) + REPO CONTEXT ENRICHMENT, IN PARALLEL ---
                # These two are independent: Agent 2 only needs Agent 1's output, and the repo
                # grounding pipeline (fetch -> file selector LLM -> architecture LLM) also only
                # needs Agent 1's output — neither depends on the other's result. Running them
                # concurrently cuts wall-clock time instead of paying for both sequentially.
                with st.status("📐🔗 Mapping component boundaries + grounding against repo...", expanded=True) as status:
                    # Cache reads happen here, on the main thread, BEFORE handing off
                    # to the background thread — st.session_state must never be
                    # touched from inside a background thread in Streamlit.
                    cached_bundle = get_cached_bundle(github_repo_url) if github_repo_url.strip() else None
                    cached_code_idx = get_cached_code_index(github_repo_url) if github_repo_url.strip() else None

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        agent2_future = executor.submit(
                            call_openai_raw, MODEL_TEXT, skill_2,
                            [{"type": "text", "text": f"Partition these components:\n\n{st.session_state.agent_1_output}"}],
                        )
                        repo_future = executor.submit(
                            run_repo_grounding_pipeline, github_repo_url, github_token,
                            st.session_state.agent_1_output, cached_bundle, cached_code_idx,
                        )
                        result_2, err_2 = agent2_future.result()
                        repo_result = repo_future.result()

                    if result_2 is None:
                        st.error(f"API call failed: {err_2}")
                        st.stop()
                    st.session_state.agent_2_output = result_2

                    # Cache writes also happen back on the main thread, after the
                    # background thread has fully finished.
                    cache_bundle(github_repo_url, repo_result["bundle_to_cache"])
                    cache_code_index(github_repo_url, repo_result["code_index_to_cache"])

                    st.session_state.repo_context = repo_result["context"]
                    st.session_state.repo_context_status = repo_result["status"]
                    st.session_state.repo_context_raw_snippets = repo_result["raw_snippets"]
                    st.session_state.repo_semantic_hits = repo_result["semantic_hits"]
                    if repo_result["warning"]:
                        st.warning(repo_result["warning"])

                    status.update(label="📐🔗 Component Boundaries Mapped & Repo Grounding Complete", state="complete")

            # --- AGENT 3: THE AGILE WRITER (runs once per feature node) ---
            with st.status("✍️ Compiling Engineering User Stories...", expanded=True) as status:
                domain_summary, node_chunks = split_into_feature_nodes(st.session_state.agent_2_output)

                if not node_chunks:
                    # Fallback: blueprint didn't match the expected "## 🏗️ FEATURE NODE" format
                    # (e.g. a model deviated from the schema) — send it through as a single call.
                    node_chunks = [st.session_state.agent_2_output]
                    domain_summary = ""

                story_sections = []
                for i, node_chunk in enumerate(node_chunks, start=1):
                    status.update(label=f"✍️ Compiling User Stories — Feature Node {i}/{len(node_chunks)}...")
                    if st.session_state.repo_context:
                        repo_context_block = (
                            f"\n\nExisting repository context (ground your tasks and file references in this "
                            f"where relevant — reference real paths/conventions instead of generic scaffolding):\n"
                            f"{st.session_state.repo_context}"
                        )
                    else:
                        # Deterministic, code-level reinforcement of the skill's "no repo
                        # context -> treat as complete scratch development" rule. Repeated
                        # per-node-call rather than relying solely on one line inside a long
                        # system prompt to be recalled reliably every time.
                        repo_context_block = (
                            "\n\nNo repository context is available for this feature node "
                            "(either no GitHub URL was provided, or the grounding lookup did not "
                            "succeed). Treat this feature node as complete, from-scratch "
                            "development: do not reference, guess at, or imply any existing file "
                            "path, dependency, symbol, or pattern. Every technical task should "
                            "describe building the component/endpoint new, in generic terms."
                        )
                    user_content = [{
                        "type": "text",
                        "text": (
                            f"Overall domain context:\n{domain_summary}\n\n"
                            f"Create developer user stories for this single feature node:\n\n{node_chunk}"
                            f"{repo_context_block}"
                        ),
                    }]
                    # Each call only ever covers ONE feature node, so output size is bounded
                    # regardless of total screenshot/annotation count — no truncation risk.
                    result = call_openai(MODEL_TEXT, skill_3, user_content, max_tokens=3000)
                    if result is None:
                        st.stop()
                    story_sections.append(result)

                st.session_state.agent_3_output = "\n\n".join(story_sections)
                status.update(label="✍️ Engineering Stories Compiled", state="complete")
                st.rerun()

    # --- THE DUAL-MODE PREVIEW & EDIT UTILITY ---
    if st.session_state.agent_3_output:

        # Persistent, impossible-to-miss confirmation of whether repo grounding
        # actually happened for this run — a transient st.warning() during a busy
        # multi-stage run is too easy to scroll past.
        if st.session_state.repo_context_status:
            if st.session_state.repo_context_status.startswith("✅"):
                st.success(st.session_state.repo_context_status)
            elif st.session_state.repo_context_status.startswith("❌"):
                st.error(st.session_state.repo_context_status)
            else:
                st.info(st.session_state.repo_context_status)

        if st.session_state.repo_context:
            with st.expander("🔍 View the actual repo grounding brief used for this run"):
                st.markdown(st.session_state.repo_context)
            if st.session_state.repo_semantic_hits:
                with st.expander("🔎 View semantic code search hits (content-based file matching)"):
                    st.markdown(st.session_state.repo_semantic_hits)
            if st.session_state.repo_context_raw_snippets:
                with st.expander("📄 View the raw fetched code the analysis above is based on"):
                    st.markdown(st.session_state.repo_context_raw_snippets)

        action_col1, action_col2 = st.columns([1, 1])
        with action_col1:
            edit_mode = st.toggle("✏️ Switch to Text Edit Mode", value=False)
        with action_col2:
            st.download_button(
                label="📥 Download as Markdown File (.md)",
                data=st.session_state.agent_3_output,
                file_name="developer_user_stories.md",
                mime="text/markdown",
            )

        st.markdown("---")

        if edit_mode:
            st.session_state.agent_3_output = st.text_area(
                label="Plain Text Editor",
                value=st.session_state.agent_3_output,
                height=550,
                label_visibility="collapsed",
            )
        else:
            st.markdown(st.session_state.agent_3_output)
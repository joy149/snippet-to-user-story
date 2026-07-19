import streamlit as st
import base64
import os
import re
from openai import OpenAI
import github_context

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

# Splits Agent 2's blueprint into: (domain summary, [individual feature node chunks]).
# This lets Agent 3 run once per feature node instead of once for the whole batch,
# so its output size never scales with how many screenshots/annotations you feed in.
def split_into_feature_nodes(blueprint_text):
    parts = re.split(r"(?=## 🏗️ FEATURE NODE)", blueprint_text)
    domain_summary = parts[0].strip()
    node_chunks = [p.strip() for p in parts[1:] if p.strip()]
    return domain_summary, node_chunks
def call_openai(model, system_prompt, user_content, max_tokens=2000):
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"API call failed: {e}")
        return None

# Initialize session states
for key in ["agent_1_output", "agent_2_output", "agent_3_output", "repo_context",
            "repo_context_status", "repo_context_raw_snippets"]:
    if key not in st.session_state:
        st.session_state[key] = ""

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
             "Public repos only, no token needed.",
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
            else:
                # --- AGENT 1: THE VISUAL AUDITOR ---
                with st.status("👁️ Executing Visual Component Audit...", expanded=True) as status:
                    user_content = [{"type": "text", "text": "Extract all visual elements factually."}] + image_blocks
                    result_1 = call_openai(MODEL_VISION, skill_1, user_content)
                    if result_1 is None:
                        st.stop()
                    st.session_state.agent_1_output = result_1
                    status.update(label="👁️ Visual Components Extracted", state="complete")

                # --- AGENT 2: THE FUNCTIONAL ARCHITECT ---
                with st.status("📐 Calculating Component Boundaries...", expanded=True) as status:
                    user_content = [{"type": "text", "text": f"Partition these components:\n\n{st.session_state.agent_1_output}"}]
                    result_2 = call_openai(MODEL_TEXT, skill_2, user_content)
                    if result_2 is None:
                        st.stop()
                    st.session_state.agent_2_output = result_2
                    status.update(label="📐 Component Boundaries Mapped", state="complete")

            # --- REPO CONTEXT ENRICHMENT (optional, runs once per batch) ---
            # Two stages: (1) cheap no-LLM fetch of raw materials from GitHub,
            # (2) an LLM agent that reasons over those materials to pick what's
            # actually relevant — replaces naive keyword/substring matching, which
            # missed semantically-relevant files that don't share vocabulary with
            # the UI labels, and never cross-checked existing dependencies before
            # recommending a new library.
            if github_repo_url.strip():
                with st.status("🔗 Fetching repo raw materials...", expanded=True) as status:
                    bundle, ctx_err = github_context.fetch_raw_materials(github_repo_url)
                    if ctx_err:
                        st.warning(f"Repo context skipped: {ctx_err}")
                        st.session_state.repo_context = ""
                        st.session_state.repo_context_status = f"❌ Skipped — {ctx_err}"
                        st.session_state.repo_context_raw_snippets = ""
                    else:
                        # --- PASS 1: File & Dependency Selector (path-level only, no code yet) ---
                        status.update(label="🧠 Selecting relevant files (pass 1/2)...")
                        skill_repo_synth = load_skill_file("skills_repo_synth.md")
                        path_listing_text = "\n".join(bundle["path_listing"])
                        truncation_note = (
                            "\n(Note: path listing was truncated to fit budget — "
                            "treat absence from this list as 'unknown', not 'doesn't exist'.)"
                            if bundle["path_listing_truncated"] else ""
                        )
                        selector_user_content = [{
                            "type": "text",
                            "text": (
                                f"UI audit (what the screenshot(s) show, including any requested changes):\n"
                                f"{st.session_state.agent_1_output}\n\n"
                                f"Detected tech stack:\n{bundle['tech_stack']}\n\n"
                                f"Top-level structure: {bundle['top_level']}\n\n"
                                f"Source file path listing ({len(bundle['path_listing'])} files):"
                                f"{truncation_note}\n{path_listing_text}"
                            ),
                        }]
                        selector_brief = call_openai(MODEL_TEXT, skill_repo_synth, selector_user_content, max_tokens=1200)

                        if selector_brief is None:
                            st.session_state.repo_context = ""
                            st.session_state.repo_context_status = "❌ Skipped — file selector LLM call failed."
                            st.session_state.repo_context_raw_snippets = ""
                        else:
                            # Fetch generously (more files, more chars/file) — pass 2 needs
                            # enough real code to detect actual patterns, not just confirm a filename.
                            picked_paths = re.findall(r"`([^`]+\.[a-zA-Z0-9]+)`", selector_brief)
                            snippets = github_context.fetch_files_by_paths(
                                bundle["owner"], bundle["repo"], bundle["branch"],
                                picked_paths, bundle["tree_files"],
                                max_chars=2500, max_files=8,
                            )

                            if not snippets:
                                # Nothing real to analyze — pass 2 would have nothing to work
                                # with, so skip it rather than let it hallucinate "patterns."
                                st.session_state.repo_context = (
                                    f"### Repository: `{bundle['owner']}/{bundle['repo']}` (branch: `{bundle['branch']}`)\n\n"
                                    f"{selector_brief}\n\n"
                                    f"_No existing files were relevant enough to fetch — treat this as net-new functionality "
                                    f"with no established pattern to follow yet in this repo._"
                                )
                                st.session_state.repo_context_status = (
                                    f"✅ Checked `{bundle['owner']}/{bundle['repo']}` — no relevant existing files found "
                                    f"(net-new functionality)."
                                )
                                st.session_state.repo_context_raw_snippets = ""
                            else:
                                # --- PASS 2: Architecture & Pattern Synthesizer (reads real code) ---
                                status.update(label="🏛️ Detecting architecture & coding patterns (pass 2/2)...")
                                skill_repo_arch = load_skill_file("skills_repo_architecture.md")
                                arch_user_content = [{
                                    "type": "text",
                                    "text": (
                                        f"UI audit:\n{st.session_state.agent_1_output}\n\n"
                                        f"File & Dependency Selector's findings so far:\n{selector_brief}\n\n"
                                        f"Actual fetched code from the files selected above:\n\n"
                                        + "\n\n".join(snippets)
                                    ),
                                }]
                                architecture_brief = call_openai(
                                    MODEL_TEXT, skill_repo_arch, arch_user_content, max_tokens=1800
                                )
                                if architecture_brief is None:
                                    architecture_brief = "_(Architecture synthesis failed — falling back to file-selector findings only.)_"

                                st.session_state.repo_context = (
                                    f"### Repository: `{bundle['owner']}/{bundle['repo']}` (branch: `{bundle['branch']}`)\n\n"
                                    f"{selector_brief}\n\n"
                                    f"{architecture_brief}"
                                )
                                st.session_state.repo_context_status = (
                                    f"✅ Grounded against `{bundle['owner']}/{bundle['repo']}` "
                                    f"— {len(snippets)} real file(s) read and analyzed for actual "
                                    f"architecture/style, not just dependency-checked."
                                )
                                # Kept separately (not sent to Agent 3) purely so the debug
                                # expander can show the raw code that backs the analysis above.
                                st.session_state.repo_context_raw_snippets = "\n\n".join(snippets)
                                status.update(label="🔗 Repo Grounding Brief Ready", state="complete")
            else:
                st.session_state.repo_context = ""
                st.session_state.repo_context_status = "No repo URL provided — stories written without repo grounding."
                st.session_state.repo_context_raw_snippets = ""

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
                    repo_context_block = (
                        f"\n\nExisting repository context (ground your tasks and file references in this "
                        f"where relevant — reference real paths/conventions instead of generic scaffolding):\n"
                        f"{st.session_state.repo_context}"
                        if st.session_state.repo_context else ""
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
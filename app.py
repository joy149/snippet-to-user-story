import streamlit as st
import base64
import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

# 1. Page Configuration
st.set_page_config(page_title="Enterprise Agile Agent", layout="wide", page_icon="⚙️")
st.title("⚙️ Enterprise Agile Requirement Engine")
st.caption("A multi-agent LangChain workspace supporting Greenfield creation and multi-modal change diffing.")

# Helper to read skill markdown files
def load_skill_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    else:
        st.error(f"Missing skill configuration file: `{filename}`")
        return ""

# Helper to encode images
def encode_image(uploaded_file):
    return base64.b64encode(uploaded_file.read()).decode('utf-8')

# Initialize session states
if "agent_1_output" not in st.session_state: st.session_state.agent_1_output = ""
if "agent_2_output" not in st.session_state: st.session_state.agent_2_output = ""
if "agent_3_output" not in st.session_state: st.session_state.agent_3_output = ""

# Set explicit OpenAI API key for prototyping
api_key_input = "your-actual-openai-api-key-here"

# 2. Sidebar Configuration with Project Mode Select
with st.sidebar:
    st.header("🎯 Project Strategy Mode")
    project_mode = st.radio(
        "Select Workflow Theme:",
        options=["🆕 Build From Scratch (Greenfield)", "🔄 Change Request / Evolution (Diff Mode)"]
    )
    
    st.markdown("---")
    st.header("📥 Visual Asset Inputs")
    
    # Dynamic Upload Zone Routing based on selected Project Mode
    if project_mode == "🆕 Build From Scratch (Greenfield)":
        uploaded_files = st.file_uploader(
            "Upload complete target UI screenshots:", 
            type=["png", "jpg", "jpeg"], 
            accept_multiple_files=True
        )
    else:
        # Evolution Mode requires explicit Baseline vs Target separation
        baseline_file = st.file_uploader("1. Upload Current Baseline UI (Production Screen):", type=["png", "jpg", "jpeg"])
        target_file = st.file_uploader("2. Upload Proposed Target Mockup (With Updates):", type=["png", "jpg", "jpeg"])
        
        # Consolidate into a list format so down-stream checks remain valid
        uploaded_files = []
        if baseline_file: uploaded_files.append(baseline_file)
        if target_file: uploaded_files.append(target_file)

    st.markdown("---")
    generate_btn = st.button("🚀 Process Agile Requirements", type="primary", disabled=len(uploaded_files) == 0)

# 3. Main Screen Layout Splitting
col1, col2 = st.columns([1, 1.4], gap="large")

with col1:
    st.subheader("🖼️ Loaded UI Assets")
    if project_mode == "🆕 Build From Scratch (Greenfield)":
        if uploaded_files:
            for file in uploaded_files:
                st.image(file, caption=f"Scope Asset: {file.name}", use_container_width=True)
        else:
            st.info("Upload screenshots to map standard developer user stories.")
    else:
        # Visual arrangement for evolution change tracking
        sub_col1, sub_col2 = st.columns(2)
        with sub_col1:
            if baseline_file:
                st.image(baseline_file, caption="📉 Current Baseline UI", use_container_width=True)
            else:
                st.caption("Missing Baseline image.")
        with sub_col2:
            if target_file:
                st.image(target_file, caption="📈 Target Mockup Evolution", use_container_width=True)
            else:
                st.caption("Missing Target image.")
        if not baseline_file or not target_file:
            st.info("Please upload both baseline and target visual snapshots to run a delta evaluation analysis.")

with col2:
    st.subheader("📝 Engineering Specifications Workspace")
    
    if generate_btn and len(uploaded_files) > 0:
        skill_1 = load_skill_file("skills_agent1.md")
        skill_2 = load_skill_file("skills_agent2.md")
        skill_3 = load_skill_file("skills_agent3.md")
        
        if skill_1 and skill_2 and skill_3:
            
            # --- AGENT 1: THE VISUAL AUDITOR (With Dynamic Prompt Injection) ---
            with st.status("👁️ Executing Visual Component Audit...", expanded=True) as status:
                llm_vision = ChatOpenAI(model="gpt-4o", api_key=api_key_input, temperature=0.1)
                
                # Adjust instruction focus dynamically on the fly based on the chosen interface view
                if project_mode == "🆕 Build From Scratch (Greenfield)":
                    agent_1_instruction = "Extract all visual elements factually from the provided images."
                else:
                    agent_1_instruction = """
                    You are comparing two images of an evolving interface. The first image represents the current baseline. The second image contains changes or additions.
                    Your primary mission is to calculate the VISUAL DELTA. Identify what elements are identical, what elements have been modified, and what elements are entirely new in the second mockup. 
                    Focus 90% of your audit weight describing ONLY the new or changed components.
                    """
                
                human_content = [{"type": "text", "text": agent_1_instruction}]
                for file in uploaded_files:
                    file.seek(0)
                    b64_string = encode_image(file)
                    human_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_string}"}})
                
                messages = [SystemMessage(content=skill_1), HumanMessage(content=human_content)]
                response_1 = llm_vision.invoke(messages)
                st.session_state.agent_1_output = response_1.content
                status.update(label="👁️ Visual Component Mapping Complete", state="complete")

            # --- AGENT 2: THE FUNCTIONAL ARCHITECT ---
            with st.status("📐 Calculating Scope Boundaries...", expanded=True) as status:
                llm_text = ChatOpenAI(model="gpt-4o-mini", api_key=api_key_input, temperature=0.2)
                
                # Context adjustment for partitioning agent
                if project_mode == "🆕 Build From Scratch (Greenfield)":
                    agent_2_instruction = "Partition these components into clean isolated blocks."
                else:
                    agent_2_instruction = "Partition ONLY the changes, modifications, and new delta elements identified by the auditor into standalone evolution requirements nodes. Ignore baseline legacy elements that did not change."
                
                messages = [
                    SystemMessage(content=skill_2),
                    HumanMessage(content=f"{agent_2_instruction}\n\nHere is the raw audit payload:\n{st.session_state.agent_1_output}")
                ]
                response_2 = llm_text.invoke(messages)
                st.session_state.agent_2_output = response_2.content
                status.update(label="📐 Scope Partitioning Complete", state="complete")

            # --- AGENT 3: THE AGILE WRITER ---
            with st.status("✍️ Compiling Engineering User Stories...", expanded=True) as status:
                
                if project_mode == "🆕 Build From Scratch (Greenfield)":
                    agent_3_instruction = "Create developer user stories from these blocks."
                else:
                    agent_3_instruction = "Create modification/evolution user stories for developers based on these delta blocks. Frame descriptions around updating an existing component layout rather than building something from scratch."
                
                messages = [
                    SystemMessage(content=skill_3),
                    HumanMessage(content=f"{agent_3_instruction}\n\nHere are the target architectural scope nodes:\n{st.session_state.agent_2_output}")
                ]
                response_3 = llm_text.invoke(messages)
                st.session_state.agent_3_output = response_3.content
                status.update(label="✍️ Technical Documentation Complete", state="complete")
                st.rerun()

    # --- THE DUAL-MODE PREVIEW & PLAIN TEXT WORKSPACE ---
    if st.session_state.agent_3_output:
        action_col1, action_col2 = st.columns([2, 1])
        with action_col1:
            edit_mode = st.toggle("✏️ Switch to Text Edit Mode", value=False)
        with action_col2:
            st.download_button(
                label="📥 Download Engineering File (.md)",
                data=st.session_state.agent_3_output,
                file_name="developer_user_stories.md",
                mime="text/markdown"
            )
            
        st.markdown("---")
        
        if edit_mode:
            st.session_state.agent_3_output = st.text_area(
                label="Plain Text Editor",
                value=st.session_state.agent_3_output,
                height=550,
                label_visibility="collapsed"
            )
        else:
            st.markdown(st.session_state.agent_3_output)

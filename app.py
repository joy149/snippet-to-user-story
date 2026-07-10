import streamlit as st
import base64
import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

# 1. Page Configuration
st.set_page_config(page_title="Developer User Story Agent", layout="wide", page_icon="⚙️")
st.title("⚙️ Developer User Story Extraction Engine")
st.caption("Multi-agent setup with an instant switch between polished reading layout and a clean plain-text workspace.")

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

# Set explicit API key for prototyping
api_key_input = "Your-OpenAI-API-Key-Here"  # Replace with your actual OpenAI API key

# 2. Sidebar Configuration
with st.sidebar:
    st.header("📥 Input UI Screenshots")
    uploaded_files = st.file_uploader(
        "Upload reference app screens:", 
        type=["png", "jpg", "jpeg"], 
        accept_multiple_files=True
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
            
            # --- AGENT 1: THE VISUAL AUDITOR ---
            with st.status("👁️ Executing Visual Component Audit...", expanded=True) as status:
                llm_vision = ChatOpenAI(model="gpt-4o", api_key=api_key_input, temperature=0.1)
                human_content = [{"type": "text", "text": "Extract all visual elements factually."}]
                for file in uploaded_files:
                    file.seek(0)
                    b64_string = encode_image(file)
                    human_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_string}"}})
                
                messages = [SystemMessage(content=skill_1), HumanMessage(content=human_content)]
                response_1 = llm_vision.invoke(messages)
                st.session_state.agent_1_output = response_1.content
                status.update(label="👁️ Visual Components Extracted", state="complete")

            # --- AGENT 2: THE FUNCTIONAL ARCHITECT ---
            with st.status("📐 Calculating Component Boundaries...", expanded=True) as status:
                llm_text = ChatOpenAI(model="gpt-4o-mini", api_key=api_key_input, temperature=0.2)
                messages = [
                    SystemMessage(content=skill_2),
                    HumanMessage(content=f"Partition these components:\n\n{st.session_state.agent_1_output}")
                ]
                response_2 = llm_text.invoke(messages)
                st.session_state.agent_2_output = response_2.content
                status.update(label="📐 Component Boundaries Mapped", state="complete")

            # --- AGENT 3: THE AGILE WRITER ---
            with st.status("✍️ Compiling Engineering User Stories...", expanded=True) as status:
                messages = [
                    SystemMessage(content=skill_3),
                    HumanMessage(content=f"Create developer user stories from these blocks:\n\n{st.session_state.agent_2_output}")
                ]
                response_3 = llm_text.invoke(messages)
                st.session_state.agent_3_output = response_3.content
                status.update(label="✍️ Engineering Stories Compiled", state="complete")
                st.rerun()

    # --- THE DUAL-MODE PREVIEW & EDIT UTILITY ---
    if st.session_state.agent_3_output:
        
        # Action Bar Row
        action_col1, action_col2 = st.columns([1, 1])
        with action_col1:
            # Simple clickable view toggle switch
            edit_mode = st.toggle("✏️ Switch to Text Edit Mode", value=False)
        with action_col2:
            # Download button always saves clean Markdown format in the background
            st.download_button(
                label="📥 Download as Markdown File (.md)",
                data=st.session_state.agent_3_output,
                file_name="developer_user_stories.md",
                mime="text/markdown"
            )
            
        st.markdown("---")
        
        if edit_mode:
            # Mode A: Clean Text Workspace for simple editing without layout clutter
            st.session_state.agent_3_output = st.text_area(
                label="Plain Text Editor",
                value=st.session_state.agent_3_output,
                height=550,
                label_visibility="collapsed"
            )
        else:
            # Mode B: Supreme, beautiful executive layout for client review
            st.markdown(st.session_state.agent_3_output)

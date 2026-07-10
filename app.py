import streamlit as st
import base64
import os
from openai import OpenAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

# 1. Page Configuration
st.set_page_config(page_title="LangChain Multi-Agent Stories", layout="wide", page_icon="🦜")
st.title("🦜 LangChain Multi-Agent User Story Pipeline")
st.caption("Orchestrating specialized agents via LangChain, utilizing external skill configuration files.")

# Helper to securely read skill markdown files
def load_skill_file(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return f.read()
    else:
        st.error(f"Missing skill configuration file: `{filename}`")
        return ""

# Helper to encode file strings for LangChain Vision payloads
def encode_image(uploaded_file):
    return base64.b64encode(uploaded_file.read()).decode('utf-8')

# Initialize standard state mechanisms
if "agent_1_output" not in st.session_state: st.session_state.agent_1_output = ""
if "agent_2_output" not in st.session_state: st.session_state.agent_2_output = ""
if "agent_3_output" not in st.session_state: st.session_state.agent_3_output = ""

# Set explicit hardcoded API key for prototyping
api_key_input = "YOUR_OPENAI_API_KEY"  # Replace with your actual OpenAI API key

# 2. Sidebar Configuration
with st.sidebar:
    st.header("📥 Input UI Screenshots")
    uploaded_files = st.file_uploader(
        "Upload reference app screens:", 
        type=["png", "jpg", "jpeg"], 
        accept_multiple_files=True
    )
    st.markdown("---")
    generate_btn = st.button("🚀 Execute LangChain Pipeline", type="primary", disabled=not uploaded_files)

# 3. Main Screen Layout Splitting
col1, col2 = st.columns([1, 1.5], gap="large")

with col1:
    st.subheader("🖼️ UI Assets")
    if uploaded_files:
        for file in uploaded_files:
            st.image(file, caption=file.name, use_container_width=True)
    else:
        st.info("Upload screenshots to map features.")

with col2:
    st.subheader("⚙️ Agent Routing Activity Logs")
    
    if generate_btn and uploaded_files:
        
        # Load external system prompts from markdown files
        skill_1 = load_skill_file("skills_agent1.md")
        skill_2 = load_skill_file("skills_agent2.md")
        skill_3 = load_skill_file("skills_agent3.md")
        
        # Ensure all skill files exist before execution
        if skill_1 and skill_2 and skill_3:
            
            # --- AGENT 1: THE VISUAL AUDITOR (Vision Node) ---
            with st.status("👁️ LangChain Agent 1: Running UI Pixel Audit...", expanded=True) as status:
                
                # Instantiate premium vision LLM via LangChain wrapper
                llm_vision = ChatOpenAI(model="gpt-4o", api_key=api_key_input, temperature=0.1)
                
                # Assemble multi-modal content structure required by LangChain
                human_content = [{"type": "text", "text": "Run a structural elements audit based on these screenshots."}]
                for file in uploaded_files:
                    file.seek(0)
                    b64_string = encode_image(file)
                    human_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_string}"}
                    })
                
                # Invoke LangChain model using standardized Message classes
                messages = [
                    SystemMessage(content=skill_1),
                    HumanMessage(content=human_content)
                ]
                
                response_1 = llm_vision.invoke(messages)
                st.session_state.agent_1_output = response_1.content
                st.write("✅ Factual element extraction completed.")
                status.update(label="👁️ Agent 1 Chain Executed", state="complete")

            # --- AGENT 2: THE FUNCTIONAL ARCHITECT (Text Node) ---
            with st.status("📐 LangChain Agent 2: Computing Atomic Slices...", expanded=True) as status:
                
                # Instantiate lightweight model for structured layout transformations
                llm_text = ChatOpenAI(model="gpt-4o-mini", api_key=api_key_input, temperature=0.2)
                
                messages = [
                    SystemMessage(content=skill_2),
                    HumanMessage(content=f"Analyze this raw component list and map isolated feature slices:\n\n{st.session_state.agent_1_output}")
                ]
                
                response_2 = llm_text.invoke(messages)
                st.session_state.agent_2_output = response_2.content
                st.write("✅ Modular system scope boundaries mapped.")
                status.update(label="📐 Agent 2 Chain Executed", state="complete")

            # --- AGENT 3: THE AGILE WRITER (Text Node) ---
            with st.status("✍️ LangChain Agent 3: Drafting User Stories & BDD...", expanded=True) as status:
                
                messages = [
                    SystemMessage(content=skill_3),
                    HumanMessage(content=f"Convert these independent functional boundaries into target stories:\n\n{st.session_state.agent_2_output}")
                ]
                
                response_3 = llm_text.invoke(messages)
                st.session_state.agent_3_output = response_3.content
                st.write("✅ Production documentation compiled.")
                status.update(label="✍️ Agent 3 Chain Executed", state="complete")
                st.rerun()

    # Display clean tab view dashboard of final artifacts
    if st.session_state.agent_3_output:
        st.markdown("### 🏆 Structured Output Inspection")
        tab1, tab2, tab3 = st.tabs(["📝 Final Agile Artifacts", "📐 Agent 2 Map", "👁️ Agent 1 System Audit"])
        
        with tab1:
            st.download_button(
                label="📥 Download Production Stories (.md)",
                data=st.session_state.agent_3_output,
                file_name="langchain_multi_agent_stories.md",
                mime="text/markdown"
            )
            st.markdown(st.session_state.agent_3_output)
            
        with tab2:
            st.markdown(st.session_state.agent_2_output)
            
        with tab3:
            st.markdown(st.session_state.agent_1_output)

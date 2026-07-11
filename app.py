import streamlit as st
import base64
import os
import time
import logging
from typing import List, Optional, Dict, Any
from PIL import Image
import io
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from openai import RateLimitError, APIConnectionError, AuthenticationError, APIError

# 1. Page Configuration
st.set_page_config(page_title="Developer User Story Agent", layout="wide", page_icon="⚙️")
st.title("⚙️ Developer User Story Extraction Engine")
st.caption("Multi-agent setup with an instant switch between polished reading layout and a clean plain-text workspace.")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE_MB = 10
MAX_IMAGE_COUNT = 10
MIN_IMAGE_DIMENSION = 100
MAX_IMAGE_DIMENSION = 4096
MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]  # exponential backoff in seconds

# Cached skill file loading (Phase 3.1: Optimization)
@st.cache_resource
def load_skill_file(filename):
    """Load and cache skill configuration files."""
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                logger.warning(f"Skill file {filename} is empty")
            return content
    else:
        logger.error(f"Missing skill configuration file: {filename}")
        return ""

def validate_skill_file(content: str, min_length: int = 50) -> bool:
    """Validate skill file has minimum structure."""
    return len(content.strip()) >= min_length

# Input validation: File size and count (Phase 1.1)
def validate_uploaded_files(files: List) -> tuple[bool, str]:
    """Validate uploaded files before processing.
    
    Returns:
        (is_valid, error_message)
    """
    if not files:
        return False, "No files uploaded"
    
    if len(files) > MAX_IMAGE_COUNT:
        return False, f"Maximum {MAX_IMAGE_COUNT} images allowed, got {len(files)}"
    
    for file in files:
        # Check file size
        file.seek(0, 2)  # Seek to end
        file_size_mb = file.tell() / (1024 * 1024)
        file.seek(0)  # Reset to beginning
        
        if file_size_mb > MAX_FILE_SIZE_MB:
            return False, f"File '{file.name}' exceeds {MAX_FILE_SIZE_MB}MB limit ({file_size_mb:.1f}MB)"
        
        # Check image dimensions
        try:
            file.seek(0)
            image = Image.open(file)
            width, height = image.size
            
            if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
                return False, f"Image '{file.name}' too small ({width}x{height}). Minimum: {MIN_IMAGE_DIMENSION}x{MIN_IMAGE_DIMENSION}px"
            
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                return False, f"Image '{file.name}' too large ({width}x{height}). Maximum: {MAX_IMAGE_DIMENSION}x{MAX_IMAGE_DIMENSION}px"
        except Exception as e:
            return False, f"Image '{file.name}' is corrupted or invalid: {str(e)}"
    
    return True, ""

# Image compression (Phase 2.1: Token Optimization)
def compress_image(uploaded_file, max_width: int = 1024, quality: int = 85) -> tuple[bytes, Dict[str, float]]:
    """Compress image to reduce token usage.
    
    Returns:
        (compressed_bytes, stats_dict)
    """
    uploaded_file.seek(0)
    original_size = len(uploaded_file.getvalue())
    
    try:
        image = Image.open(uploaded_file)
        
        # Maintain aspect ratio
        ratio = min(max_width / image.width, 1.0)
        new_size = (int(image.width * ratio), int(image.height * ratio))
        
        if new_size != image.size:
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        
        # Convert to JPEG for compression
        buffer = io.BytesIO()
        save_format = "JPEG" if image.mode in ("RGB", "RGBA") else "PNG"
        if image.mode == "RGBA":
            rgb_image = Image.new("RGB", image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[3])
            image = rgb_image
        
        image.save(buffer, format=save_format, quality=quality, optimize=True)
        compressed_bytes = buffer.getvalue()
        compression_ratio = 1 - (len(compressed_bytes) / original_size)
        
        return compressed_bytes, {
            "original_size_kb": original_size / 1024,
            "compressed_size_kb": len(compressed_bytes) / 1024,
            "compression_ratio": compression_ratio * 100,
            "dimensions": new_size
        }
    except Exception as e:
        logger.error(f"Image compression failed: {e}")
        return uploaded_file.getvalue(), {"error": str(e)}

# Helper to encode images with compression
def encode_image_optimized(uploaded_file):
    """Encode image after compression."""
    compressed_bytes, stats = compress_image(uploaded_file)
    encoded = base64.b64encode(compressed_bytes).decode('utf-8')
    return encoded, stats

# API error handling with retry wrapper (Phase 1.2)
def call_llm_with_retry(llm_instance: ChatOpenAI, messages: List, agent_name: str, max_retries: int = MAX_RETRIES) -> Optional[str]:
    """Call LLM with exponential backoff retry logic.
    
    Returns:
        Response content or None if all retries failed
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            logger.info(f"{agent_name}: Attempt {attempt + 1}/{max_retries}")
            response = llm_instance.invoke(messages)
            
            # Track token usage
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                usage = response.usage_metadata
                logger.info(f"{agent_name}: Tokens - Input: {usage.get('input_tokens', 'N/A')}, Output: {usage.get('output_tokens', 'N/A')}")
                if "token_counts" not in st.session_state:
                    st.session_state.token_counts = {}
                st.session_state.token_counts[agent_name] = usage.get('output_tokens', 0)
            
            return response.content
        
        except AuthenticationError as e:
            logger.error(f"{agent_name}: Authentication failed - Invalid API key")
            return None  # Don't retry auth errors
        
        except RateLimitError as e:
            last_error = e
            logger.warning(f"{agent_name}: Rate limited. Attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = RETRY_DELAYS[attempt]
                time.sleep(delay)
        
        except APIConnectionError as e:
            last_error = e
            logger.warning(f"{agent_name}: Connection error. Attempt {attempt + 1}/{max_retries}: {str(e)}")
            if attempt < max_retries - 1:
                delay = RETRY_DELAYS[attempt]
                time.sleep(delay)
        
        except APIError as e:
            last_error = e
            logger.warning(f"{agent_name}: API error. Attempt {attempt + 1}/{max_retries}: {str(e)}")
            if attempt < max_retries - 1:
                delay = RETRY_DELAYS[attempt]
                time.sleep(delay)
        
        except Exception as e:
            logger.error(f"{agent_name}: Unexpected error: {type(e).__name__}: {str(e)}")
            return None  # Don't retry unknown errors
    
    # All retries exhausted
    logger.error(f"{agent_name}: All retries failed. Last error: {last_error}")
    return None

def log_error(agent_name: str, error: Exception, context: Dict[str, Any]):
    """Log error details for debugging."""
    logger.error(f"{agent_name} failed: {type(error).__name__}: {str(error)}", extra=context)

# Initialize session states
if "agent_1_output" not in st.session_state: st.session_state.agent_1_output = ""
if "agent_2_output" not in st.session_state: st.session_state.agent_2_output = ""
if "agent_3_output" not in st.session_state: st.session_state.agent_3_output = ""
if "token_counts" not in st.session_state: st.session_state.token_counts = {}
if "processing_errors" not in st.session_state: st.session_state.processing_errors = []

# API key handling - use environment variable or secrets
api_key_input = os.getenv("OPENAI_API_KEY") or st.secrets.get("openai_api_key", None)
if not api_key_input:
    logger.warning("No OpenAI API key found. Set OPENAI_API_KEY env var or openai_api_key in Streamlit secrets.")

# 2. Sidebar Configuration
with st.sidebar:
    st.header("📥 Input UI Screenshots")
    uploaded_files = st.file_uploader(
        "Upload reference app screens:", 
        type=["png", "jpg", "jpeg"], 
        accept_multiple_files=True
    )
    st.markdown("---")
    
    # Input validation feedback
    if uploaded_files:
        is_valid, error_msg = validate_uploaded_files(uploaded_files)
        if not is_valid:
            st.error(error_msg)
            uploaded_files = None
        else:
            total_size = sum(len(f.getvalue()) for f in uploaded_files) / (1024 * 1024)
            st.success(f"✓ {len(uploaded_files)} valid image(s) ({total_size:.1f}MB)")
    
    generate_btn = st.button("🚀 Process Developer Stories", type="primary", disabled=not uploaded_files or not api_key_input)

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
        # Validate skill files exist and have content
        skill_1 = load_skill_file("skills_agent1.md")
        skill_2 = load_skill_file("skills_agent2.md")
        skill_3 = load_skill_file("skills_agent3.md")
        
        if not all([skill_1, skill_2, skill_3]):
            st.error("❌ One or more skill configuration files are missing or empty. Cannot proceed.")
            logger.error("Skill files validation failed")
        elif not api_key_input:
            st.error("❌ OpenAI API key not found. Set OPENAI_API_KEY environment variable or add to Streamlit secrets.")
        else:
            # Clear previous errors
            st.session_state.processing_errors = []
            
            # --- VISUAL AGENT: THE VISUAL AUDITOR ---
            with st.status("👁️ Executing Visual Component Audit...", expanded=True) as status:
                try:
                    llm_vision = ChatOpenAI(model="gpt-4o", api_key=api_key_input, temperature=0.1)
                    human_content = [{"type": "text", "text": "Extract all visual elements factually."}]
                    
                    for file in uploaded_files:
                        file.seek(0)
                        # Use compression to reduce token usage
                        b64_string, comp_stats = encode_image_optimized(file)
                        if "error" not in comp_stats:
                            logger.info(f"Image compressed: {comp_stats['original_size_kb']:.1f}KB -> {comp_stats['compressed_size_kb']:.1f}KB ({comp_stats['compression_ratio']:.1f}% saved)")
                        human_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_string}"}})
                    
                    messages = [SystemMessage(content=skill_1), HumanMessage(content=human_content)]
                    response_1 = call_llm_with_retry(llm_vision, messages, "Visual_Agent")
                    
                    if response_1:
                        st.session_state.agent_1_output = response_1
                        status.update(label="👁️ Visual Components Extracted", state="complete")
                    else:
                        error_msg = "Visual Agent failed after retries. Check API key and connection."
                        st.error(f"❌ {error_msg}")
                        st.session_state.processing_errors.append(error_msg)
                        status.update(label="👁️ Visual Audit Failed", state="error")
                
                except Exception as e:
                    error_msg = f"Visual Agent error: {type(e).__name__}: {str(e)}"
                    st.error(f"❌ {error_msg}")
                    st.session_state.processing_errors.append(error_msg)
                    log_error("Visual_Agent", e, {"uploaded_files": len(uploaded_files)})
                    status.update(label="👁️ Visual Audit Failed", state="error")

            # --- PM AGENT: THE FUNCTIONAL ARCHITECT ---
            if st.session_state.agent_1_output:
                with st.status("📐 Calculating Component Boundaries...", expanded=True) as status:
                    try:
                        llm_text = ChatOpenAI(model="gpt-4o-mini", api_key=api_key_input, temperature=0.2)
                        messages = [
                            SystemMessage(content=skill_2),
                            HumanMessage(content=f"Partition these components:\n\n{st.session_state.agent_1_output}")
                        ]
                        response_2 = call_llm_with_retry(llm_text, messages, "PM_Agent")
                        
                        if response_2:
                            st.session_state.agent_2_output = response_2
                            status.update(label="📐 Component Boundaries Mapped", state="complete")
                        else:
                            error_msg = "PM Agent failed after retries."
                            st.error(f"❌ {error_msg}")
                            st.session_state.processing_errors.append(error_msg)
                            status.update(label="📐 Component Boundaries Failed", state="error")
                    
                    except Exception as e:
                        error_msg = f"PM Agent error: {type(e).__name__}: {str(e)}"
                        st.error(f"❌ {error_msg}")
                        st.session_state.processing_errors.append(error_msg)
                        log_error("PM_Agent", e, {})
                        status.update(label="📐 Component Boundaries Failed", state="error")

                # --- STORY WRITER: THE AGILE WRITER ---
                if st.session_state.agent_2_output:
                    with st.status("✍️ Compiling Engineering User Stories...", expanded=True) as status:
                        try:
                            messages = [
                                SystemMessage(content=skill_3),
                                HumanMessage(content=f"Create developer user stories from these blocks:\n\n{st.session_state.agent_2_output}")
                            ]
                            response_3 = call_llm_with_retry(llm_text, messages, "Story_Writer")
                            
                            if response_3:
                                st.session_state.agent_3_output = response_3
                                status.update(label="✍️ Engineering Stories Compiled", state="complete")
                                st.rerun()
                            else:
                                error_msg = "Story Writer failed after retries."
                                st.error(f"❌ {error_msg}")
                                st.session_state.processing_errors.append(error_msg)
                                status.update(label="✍️ Engineering Stories Failed", state="error")
                        
                        except Exception as e:
                            error_msg = f"Story Writer error: {type(e).__name__}: {str(e)}"
                            st.error(f"❌ {error_msg}")
                            st.session_state.processing_errors.append(error_msg)
                            log_error("Story_Writer", e, {})
                            status.update(label="✍️ Engineering Stories Failed", state="error")

    # --- THE DUAL-MODE PREVIEW & EDIT UTILITY ---
    if st.session_state.agent_3_output:
        
        # Token usage metrics (Phase 2.2: Token Tracking)
        if st.session_state.token_counts:
            metric_col1, metric_col2, metric_col3 = st.columns(3)
            with metric_col1:
                agent1_tokens = st.session_state.token_counts.get("Visual_Agent", 0)
                st.metric("👁️ Visual Agent Output Tokens", agent1_tokens)
            with metric_col2:
                agent2_tokens = st.session_state.token_counts.get("PM_Agent", 0)
                st.metric("📐 PM Agent Output Tokens", agent2_tokens)
            with metric_col3:
                agent3_tokens = st.session_state.token_counts.get("Story_Writer", 0)
                st.metric("✍️ Story Writer Output Tokens", agent3_tokens)
        
        # Action Bar Row
        action_col1, action_col2, action_col3 = st.columns([1, 1, 1])
        with action_col1:
            edit_mode = st.toggle("✏️ Switch to Text Edit Mode", value=False)
        with action_col2:
            st.download_button(
                label="📥 Download as Markdown File (.md)",
                data=st.session_state.agent_3_output,
                file_name="developer_user_stories.md",
                mime="text/markdown"
            )
        with action_col3:
            if st.button("🔄 Start Over"):
                st.session_state.agent_1_output = ""
                st.session_state.agent_2_output = ""
                st.session_state.agent_3_output = ""
                st.session_state.token_counts = {}
                st.rerun()
            
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
    
    # Show error log if any errors occurred
    if st.session_state.processing_errors:
        with st.expander("📋 Error Log"):
            for error in st.session_state.processing_errors:
                st.warning(error)

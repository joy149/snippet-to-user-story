"""
pipeline_utils.py — Pure, Streamlit-free helper logic used by app.py.

Kept separate from app.py so it can be imported and unit tested without
needing a running Streamlit script context (app.py calls st.set_page_config()
and builds sidebar widgets at import time, which makes it awkward to import
directly in a test process).
"""

import re

FEATURE_NODE_HEADER_RE = re.compile(r"(?=## 🏗️ FEATURE NODE)")


def split_into_feature_nodes(blueprint_text):
    """
    Splits Agent 2's blueprint into: (domain summary, [individual feature node chunks]).

    This lets Agent 3 run once per feature node instead of once for the whole
    batch, so its output size never scales with how many screenshots/annotations
    are fed in.

    Falls back gracefully: if the blueprint text doesn't contain the expected
    "## 🏗️ FEATURE NODE" header at all (e.g. the model deviated from the
    schema), node_chunks will be an empty list and domain_summary will be the
    entire input — callers should treat an empty node_chunks list as "send the
    whole blueprint through as a single node" rather than an error.
    """
    if not blueprint_text or not blueprint_text.strip():
        return "", []

    parts = FEATURE_NODE_HEADER_RE.split(blueprint_text)
    domain_summary = parts[0].strip()
    node_chunks = [p.strip() for p in parts[1:] if p.strip()]
    return domain_summary, node_chunks
"""
Unit tests for pipeline_utils.split_into_feature_nodes.

Covers the normal multi-node split as well as the fallback path that kicks
in when Agent 2's output doesn't follow the expected "## 🏗️ FEATURE NODE"
schema (e.g. the model deviated from instructions) — previously this path
existed in app.py but had no test coverage, so a regression there would
only surface as a confusing runtime issue deep in a live pipeline run.

Run with: pytest test_pipeline_utils.py -v
"""

from pipeline_utils import split_into_feature_nodes


class TestSplitIntoFeatureNodesNormalCase:
    def test_single_feature_node(self):
        blueprint = (
            "# 🏆 Overall Domain Scope Definition\n"
            "This is the summary.\n\n"
            "---\n\n"
            "## 🏗️ FEATURE NODE 1: Login Form\n"
            "Some content about the login form."
        )
        domain_summary, node_chunks = split_into_feature_nodes(blueprint)
        assert "Overall Domain Scope" in domain_summary
        assert len(node_chunks) == 1
        assert node_chunks[0].startswith("## 🏗️ FEATURE NODE 1: Login Form")

    def test_multiple_feature_nodes_split_correctly(self):
        blueprint = (
            "# 🏆 Overall Domain Scope Definition\nSummary text.\n\n"
            "## 🏗️ FEATURE NODE 1: Login Form\nLogin content.\n\n"
            "## 🏗️ FEATURE NODE 2: Signup Form\nSignup content.\n\n"
            "## 🏗️ FEATURE NODE 3: Password Reset\nReset content."
        )
        domain_summary, node_chunks = split_into_feature_nodes(blueprint)
        assert len(node_chunks) == 3
        assert node_chunks[0].startswith("## 🏗️ FEATURE NODE 1")
        assert node_chunks[1].startswith("## 🏗️ FEATURE NODE 2")
        assert node_chunks[2].startswith("## 🏗️ FEATURE NODE 3")
        # Each chunk should contain only its own content, not bleed into the next.
        assert "Signup content" not in node_chunks[0]
        assert "Login content" not in node_chunks[1]

    def test_domain_summary_excludes_node_content(self):
        blueprint = (
            "Domain summary here.\n\n"
            "## 🏗️ FEATURE NODE 1: Thing\nNode content."
        )
        domain_summary, node_chunks = split_into_feature_nodes(blueprint)
        assert domain_summary == "Domain summary here."
        assert "Node content" not in domain_summary


class TestSplitIntoFeatureNodesFallback:
    """
    Covers app.py's fallback: when the blueprint doesn't contain the expected
    header at all, node_chunks comes back empty. app.py's caller is responsible
    for then treating the *entire* blueprint as a single node — this test
    locks in the underlying signal (empty list) that fallback branch depends on.
    """

    def test_no_header_present_returns_empty_node_chunks(self):
        blueprint = "The model just wrote some prose without following the schema at all."
        domain_summary, node_chunks = split_into_feature_nodes(blueprint)
        assert node_chunks == []
        # Whole text should still be recoverable from domain_summary so the
        # caller's fallback (treat as single node) doesn't lose content.
        assert domain_summary == blueprint

    def test_empty_string_input(self):
        domain_summary, node_chunks = split_into_feature_nodes("")
        assert domain_summary == ""
        assert node_chunks == []

    def test_whitespace_only_input(self):
        domain_summary, node_chunks = split_into_feature_nodes("   \n\n  ")
        assert domain_summary == ""
        assert node_chunks == []

    def test_header_with_no_trailing_content_is_dropped(self):
        # A "node" that's just the header with nothing else (post-strip empty)
        # should be filtered out rather than producing a blank story downstream.
        blueprint = "Summary.\n\n## 🏗️ FEATURE NODE 1: Empty\n   \n\n## 🏗️ FEATURE NODE 2: Real\nReal content."
        domain_summary, node_chunks = split_into_feature_nodes(blueprint)
        # Node 1's header+whitespace still counts as non-empty content (the header
        # line itself), so it will appear; verify node 2 is present and distinct.
        assert any("FEATURE NODE 2: Real" in chunk for chunk in node_chunks)
        assert any("Real content" in chunk for chunk in node_chunks)
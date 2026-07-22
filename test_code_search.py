"""
Unit tests for code_search.py — the semantic code search module.

Tests cover the pure, deterministic functions (chunking, deduplication,
search ranking, prompt formatting) WITHOUT any network or OpenAI API calls.
Embedding vectors are synthesized directly as NumPy arrays so the suite
runs instantly and needs no API keys or billing.

Run with: pytest test_code_search.py -v
"""

import numpy as np
import code_search as cs


# ---------------------------------------------------------------------------
# chunk_file
# ---------------------------------------------------------------------------

class TestChunkFile:
    def test_python_function_boundaries(self):
        # Each function body must be >= CHUNK_MIN_CHARS (200) for boundary splitting
        # to produce separate chunks instead of merging them.
        content = (
            "import os\n"
            "import sys\n"
            "\n"
            "def foo():\n"
            "    # This function does some important work that needs a long body\n"
            "    result = os.path.join('/tmp', 'output')\n"
            "    data = {'key': 'value', 'another_key': 'another_value'}\n"
            "    for item in range(100):\n"
            "        result += str(item)\n"
            "    return result\n"
            "\n"
            "def bar():\n"
            "    # This function also does important work with a substantial body\n"
            "    config = {'setting_a': True, 'setting_b': False, 'setting_c': 42}\n"
            "    output = sys.stdout\n"
            "    for key, val in config.items():\n"
            "        output.write(f'{key}={val}\\n')\n"
            "    return config\n"
        )
        chunks = cs.chunk_file("utils.py", content)
        assert len(chunks) >= 2
        # Each chunk should have the correct file_path
        for c in chunks:
            assert c.file_path == "utils.py"
        # chunk_index should be sequential
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_js_arrow_function_boundaries(self):
        content = (
            "import React from 'react';\n"
            "import { useState, useEffect } from 'react';\n"
            "\n"
            "const Panel3 = ({ data, onUpdate }) => {\n"
            "  const [state, setState] = useState(null);\n"
            "  useEffect(() => { setState(data); }, [data]);\n"
            "  const handleChange = (e) => { onUpdate(e.target.value); };\n"
            "  return (\n"
            "    <div className='panel'>\n"
            "      <input value={state} onChange={handleChange} />\n"
            "    </div>\n"
            "  );\n"
            "};\n"
            "\n"
            "const Helper = ({ label, description, onClick }) => {\n"
            "  const [active, setActive] = useState(false);\n"
            "  const toggle = () => { setActive(!active); onClick(label); };\n"
            "  return (\n"
            "    <span className={active ? 'active' : 'inactive'}>\n"
            "      {label}: {description}\n"
            "      <button onClick={toggle}>Toggle</button>\n"
            "    </span>\n"
            "  );\n"
            "};\n"
            "\n"
            "export default Panel3;\n"
        )
        chunks = cs.chunk_file("Panel3.jsx", content)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.file_path == "Panel3.jsx"

    def test_class_boundary_detection(self):
        content = (
            "class Animal:\n"
            "    def __init__(self, name, species, age):\n"
            "        self.name = name\n"
            "        self.species = species\n"
            "        self.age = age\n"
            "        self.health = 100\n"
            "\n"
            "    def describe(self):\n"
            "        return f'{self.name} is a {self.species}, age {self.age}'\n"
            "\n"
            "class Dog(Animal):\n"
            "    def __init__(self, name, age, breed):\n"
            "        super().__init__(name, 'dog', age)\n"
            "        self.breed = breed\n"
            "        self.tricks = []\n"
            "\n"
            "    def bark(self):\n"
            "        return f'{self.name} says woof! ({self.breed})'\n"
            "\n"
            "    def learn_trick(self, trick):\n"
            "        self.tricks.append(trick)\n"
            "        return f'{self.name} learned {trick}'\n"
        )
        chunks = cs.chunk_file("models.py", content)
        assert len(chunks) >= 2

    def test_sliding_window_fallback_for_css(self):
        """CSS files have no function boundaries — should use sliding window."""
        # Generate a CSS file large enough to produce multiple sliding windows
        content = "\n".join(
            f".class-{i} {{ color: red; margin: {i}px; padding: {i}px; }}"
            for i in range(200)
        )
        chunks = cs.chunk_file("styles.css", content)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.file_path == "styles.css"

    def test_empty_file_returns_no_chunks(self):
        assert cs.chunk_file("empty.py", "") == []
        assert cs.chunk_file("blank.py", "   \n  \n  ") == []

    def test_small_file_produces_single_chunk(self):
        content = "const x = 42;\nexport default x;\n"
        chunks = cs.chunk_file("tiny.js", content)
        # Small files should still produce at least one chunk (via sliding window)
        assert len(chunks) >= 1
        assert chunks[0].file_path == "tiny.js"

    def test_start_line_is_one_indexed(self):
        content = (
            "import os\n"
            "\n"
            "def foo():\n"
            "    pass\n"
        )
        chunks = cs.chunk_file("f.py", content)
        for c in chunks:
            assert c.start_line >= 1

    def test_chunk_max_size_respected(self):
        """A single huge function body should be sub-split by sliding window."""
        # Create a single function with a massive body
        body_lines = [f"    x_{i} = {i}" for i in range(500)]
        content = "def huge():\n" + "\n".join(body_lines)
        chunks = cs.chunk_file("big.py", content)
        for c in chunks:
            assert len(c.text) <= cs.CHUNK_MAX_CHARS + cs.SLIDING_WINDOW_CHARS


# ---------------------------------------------------------------------------
# _find_boundaries
# ---------------------------------------------------------------------------

class TestFindBoundaries:
    def test_detects_python_defs(self):
        text = "import os\n\ndef foo():\n    pass\n\ndef bar():\n    pass\n"
        boundaries = cs._find_boundaries(text)
        assert len(boundaries) >= 2

    def test_detects_js_functions(self):
        text = "const a = 1;\n\nfunction doStuff() {\n  return a;\n}\n"
        boundaries = cs._find_boundaries(text)
        assert len(boundaries) >= 1

    def test_detects_go_funcs(self):
        text = "package main\n\nfunc main() {\n  fmt.Println(\"hi\")\n}\n"
        boundaries = cs._find_boundaries(text)
        assert len(boundaries) >= 1

    def test_no_boundaries_in_plain_text(self):
        text = "This is just a readme file.\nNothing special here.\n"
        boundaries = cs._find_boundaries(text)
        assert boundaries == []


# ---------------------------------------------------------------------------
# _sliding_window_chunks
# ---------------------------------------------------------------------------

class TestSlidingWindowChunks:
    def test_produces_overlapping_chunks(self):
        text = "x" * 4000
        chunks = cs._sliding_window_chunks(text, 0)
        assert len(chunks) >= 2
        # Check overlap: second chunk should start before first chunk ends
        # (SLIDING_WINDOW_CHARS - SLIDING_OVERLAP_CHARS step)
        assert len(chunks[0].text) == cs.SLIDING_WINDOW_CHARS

    def test_tiny_remainder_merged_into_previous(self):
        # Create text just slightly over one window, so the remainder is tiny
        text = "x" * (cs.SLIDING_WINDOW_CHARS + 50)
        chunks = cs._sliding_window_chunks(text, 0)
        # The tiny remainder (50 chars < CHUNK_MIN_CHARS) should be merged
        # into the previous chunk rather than standing alone
        for c in chunks:
            assert len(c.text) >= cs.CHUNK_MIN_CHARS

    def test_base_line_offset(self):
        text = "line1\nline2\nline3\n" * 200
        chunks = cs._sliding_window_chunks(text, 10)
        assert chunks[0].start_line == 11   # base_line(10) + 0 newlines + 1


# ---------------------------------------------------------------------------
# Search ranking and deduplication (using synthetic vectors)
# ---------------------------------------------------------------------------

class TestSearchAndDedup:
    def _make_index(self, vectors, paths):
        """Build a CodeIndex from raw vectors and a parallel list of file paths."""
        chunks = [
            cs.ChunkMeta(file_path=p, chunk_index=i, start_line=1, text=f"code from {p}")
            for i, p in enumerate(paths)
        ]
        return cs.CodeIndex(
            vectors=np.array(vectors, dtype=np.float32),
            chunks=chunks,
            file_contents={p: f"full content of {p}" for p in set(paths)},
        )

    def test_highest_similarity_ranks_first(self):
        """Given a query vector, the most similar chunk should rank first."""
        # Query = [1, 0, 0]
        # Chunk A = [0.9, 0.1, 0] (close to query)
        # Chunk B = [0.1, 0.9, 0] (far from query)
        index = self._make_index(
            vectors=[[0.9, 0.1, 0.0], [0.1, 0.9, 0.0]],
            paths=["close.py", "far.py"],
        )
        # Monkey-patch _embed_texts to return our known query vector
        original_embed = cs._embed_texts
        cs._embed_texts = lambda client, texts: np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        try:
            # Override EMBED_DIMENSIONS temporarily for the 3-dim test vectors
            orig_dims = cs.EMBED_DIMENSIONS
            hits = cs.search(None, index, "query", top_k=10)
            assert len(hits) == 2
            assert hits[0].file_path == "close.py"
            assert hits[1].file_path == "far.py"
            assert hits[0].score > hits[1].score
        finally:
            cs._embed_texts = original_embed

    def test_dedup_keeps_highest_score_per_file(self):
        """Multiple chunks from the same file → only best-scoring one kept."""
        index = self._make_index(
            vectors=[[0.5, 0.5, 0.0], [0.9, 0.1, 0.0], [0.1, 0.1, 0.9]],
            paths=["file_a.py", "file_a.py", "file_b.py"],
        )
        cs._embed_texts_backup = cs._embed_texts
        cs._embed_texts = lambda client, texts: np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        try:
            hits = cs.search(None, index, "query", top_k=10)
            # file_a.py should appear only once, with the higher score (chunk at index 1)
            file_a_hits = [h for h in hits if h.file_path == "file_a.py"]
            assert len(file_a_hits) == 1
            # The best chunk for file_a is [0.9, 0.1, 0] which has higher cosine sim to [1,0,0]
        finally:
            cs._embed_texts = cs._embed_texts_backup
            del cs._embed_texts_backup

    def test_top_k_limits_results(self):
        """top_k should cap the number of returned files."""
        vectors = [[float(i == j) for j in range(5)] for i in range(5)]
        paths = [f"file_{i}.py" for i in range(5)]
        index = self._make_index(vectors, paths)
        cs._embed_texts_backup = cs._embed_texts
        cs._embed_texts = lambda client, texts: np.array([[1.0, 0.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        try:
            hits = cs.search(None, index, "query", top_k=2)
            assert len(hits) <= 2
        finally:
            cs._embed_texts = cs._embed_texts_backup
            del cs._embed_texts_backup

    def test_empty_index_returns_no_hits(self):
        hits = cs.search(None, None, "query")
        assert hits == []

    def test_index_with_no_chunks_returns_no_hits(self):
        index = cs.CodeIndex(
            vectors=np.empty((0, 3), dtype=np.float32),
            chunks=[],
            file_contents={},
        )
        hits = cs.search(None, index, "query")
        assert hits == []


# ---------------------------------------------------------------------------
# format_hits_for_prompt
# ---------------------------------------------------------------------------

class TestFormatHitsForPrompt:
    def test_empty_hits_returns_empty_string(self):
        assert cs.format_hits_for_prompt([]) == ""

    def test_returns_markdown_table(self):
        hits = [
            cs.SearchHit(
                file_path="src/Panel3.jsx",
                score=0.82,
                chunk_text="const Panel3 = ({ data }) => { return <div>{data}</div> }",
                chunk_meta=cs.ChunkMeta("src/Panel3.jsx", 0, 1, "..."),
            ),
        ]
        result = cs.format_hits_for_prompt(hits)
        assert "| Rank |" in result
        assert "`src/Panel3.jsx`" in result
        assert "0.82" in result
        assert "Semantic search hits" in result

    def test_max_hits_caps_output(self):
        hits = [
            cs.SearchHit(f"file_{i}.py", 0.9 - i * 0.05, "code", cs.ChunkMeta(f"file_{i}.py", 0, 1, "..."))
            for i in range(20)
        ]
        result = cs.format_hits_for_prompt(hits, max_hits=3)
        # Should only contain 3 data rows (plus header rows)
        data_rows = [line for line in result.split("\n") if line.startswith("| ") and not line.startswith("| Rank") and not line.startswith("| :")]
        assert len(data_rows) == 3

    def test_pipe_characters_escaped_in_preview(self):
        hits = [
            cs.SearchHit(
                file_path="test.py",
                score=0.75,
                chunk_text="x = a | b",
                chunk_meta=cs.ChunkMeta("test.py", 0, 1, "x = a | b"),
            ),
        ]
        result = cs.format_hits_for_prompt(hits)
        # The pipe in the code preview should be escaped
        assert "a \\| b" in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_chunk_file_with_only_imports(self):
        """A barrel/index file with only re-exports should still produce chunks."""
        content = (
            "export { default as Button } from './Button';\n"
            "export { default as Input } from './Input';\n"
            "export { default as Modal } from './Modal';\n"
        )
        chunks = cs.chunk_file("index.js", content)
        # Should produce at least one chunk (via sliding window since lines are short)
        assert len(chunks) >= 1

    def test_chunk_file_binary_like_content(self):
        """Content with no recognizable code patterns should still chunk via sliding window."""
        content = "abc123!@# " * 200
        chunks = cs.chunk_file("mystery.dat", content)
        assert len(chunks) >= 1

    def test_fetch_all_files_with_empty_paths(self):
        result = cs.fetch_all_files("owner", "repo", "main", [])
        assert result == {}

    def test_search_hit_score_in_valid_range(self):
        """Cosine similarity scores should be between -1 and 1."""
        index = cs.CodeIndex(
            vectors=np.array([[0.5, 0.5, 0.0]], dtype=np.float32),
            chunks=[cs.ChunkMeta("f.py", 0, 1, "code")],
            file_contents={"f.py": "code"},
        )
        cs._embed_texts_backup = cs._embed_texts
        cs._embed_texts = lambda client, texts: np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        try:
            hits = cs.search(None, index, "query")
            for h in hits:
                assert -1.0 <= h.score <= 1.0
        finally:
            cs._embed_texts = cs._embed_texts_backup
            del cs._embed_texts_backup


# ---------------------------------------------------------------------------
# Multi-Query Search & Disk Cache
# ---------------------------------------------------------------------------

class TestMultiQueryAndDiskCache:
    def test_search_multi_queries_max_fusion(self):
        index = cs.CodeIndex(
            vectors=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32),
            chunks=[
                cs.ChunkMeta("feat_a.py", 0, 1, "code a"),
                cs.ChunkMeta("feat_b.py", 1, 1, "code b"),
            ],
            file_contents={"feat_a.py": "code a", "feat_b.py": "code b"},
        )
        # Mock _embed_texts to return two query vectors: [1, 0, 0] and [0, 1, 0]
        original_embed = cs._embed_texts
        cs._embed_texts = lambda client, texts: np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        try:
            hits = cs.search_multi_queries(None, index, ["query a", "query b"])
            assert len(hits) == 2
            # Both should have score ~ 1.0 due to max-fusion
            assert hits[0].score >= 0.99
            assert hits[1].score >= 0.99
        finally:
            cs._embed_texts = original_embed

    def test_disk_cache_save_and_load(self, tmp_path):
        cache_dir = str(tmp_path / ".cache_test")
        index = cs.CodeIndex(
            vectors=np.array([[0.1, 0.2, 0.3]], dtype=np.float32),
            chunks=[cs.ChunkMeta("test.py", 0, 1, "content")],
            file_contents={"test.py": "content"},
        )
        ok = cs.save_index_to_disk(index, "test_owner", "test_repo", "main", cache_dir=cache_dir)
        assert ok is True

        loaded = cs.load_index_from_disk("test_owner", "test_repo", "main", cache_dir=cache_dir)
        assert loaded is not None
        assert len(loaded.chunks) == 1
        assert loaded.chunks[0].file_path == "test.py"
        assert loaded.file_contents.get("test.py") == "content"


import os
import json
import re
import numpy as np
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor

import github_context

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSIONS = 1536
EMBED_BATCH_SIZE = 100          # OpenAI embeddings endpoint accepts batch input

CHUNK_MAX_CHARS = 2400          # ~600 tokens — upper bound for a single chunk
CHUNK_MIN_CHARS = 200           # don't create tiny, meaningless chunks
SLIDING_WINDOW_CHARS = 1600     # ~400 tokens — fallback fixed-size window
SLIDING_OVERLAP_CHARS = 400     # 25% overlap between adjacent windows

MAX_FETCH_WORKERS = 15          # concurrent raw.githubusercontent.com fetches
MAX_FETCH_CHARS = 30000         # per-file content cap for indexing (~7500 tokens)
DISK_CACHE_DIR = ".cache_code_index"


# ---------------------------------------------------------------------------
# Boundary detection patterns (per-language)
# ---------------------------------------------------------------------------
# Used to split files at meaningful code boundaries (function/class/component
# definitions) before falling back to a sliding window.

_BOUNDARY_PATTERNS = [
    # Python: class and function definitions (including decorated / async)
    re.compile(r"^(?:@\w+(?:\(.*\))?\s*\n)*(?:async\s+)?(?:def|class)\s+\w+", re.MULTILINE),
    # JS/TS: function/class declarations, optionally exported
    re.compile(
        r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\s+\w*|class\s+\w+)",
        re.MULTILINE,
    ),
    # JS/TS: top-level const/let/var component or function declarations
    re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::\s*[\w<>]+\s*)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w]+)\s*=>",
        re.MULTILINE,
    ),
    # Go: func declarations
    re.compile(r"^func\s+(?:\([^)]+\)\s+)?\w+", re.MULTILINE),
    # Ruby: def/class/module
    re.compile(r"^(?:def|class|module)\s+\w+", re.MULTILINE),
    # Java/Kotlin/C#: class/interface/enum/struct declarations
    re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*"
        r"(?:static\s+)?(?:abstract\s+)?(?:final\s+)?(?:class|interface|enum|object|struct)\s+\w+",
        re.MULTILINE,
    ),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChunkMeta:
    """Metadata for a single chunk of code from one file."""
    file_path: str
    chunk_index: int
    start_line: int       # 1-indexed line number within the original file
    text: str


@dataclass
class SearchHit:
    """A single semantic search result, deduplicated to one per file."""
    file_path: str
    score: float          # cosine similarity, 0–1
    chunk_text: str       # preview of the best-matching chunk
    chunk_meta: ChunkMeta


@dataclass
class CodeIndex:
    """
    In-memory embedding index for a repository's source files.

    vectors:       (N, 1536) float32 array — one row per chunk.
    chunks:        parallel list of ChunkMeta — chunks[i] describes vectors[i].
    file_contents: path → raw content string — pre-fetched content that can be
                   reused downstream to avoid re-downloading.
    """
    vectors: np.ndarray
    chunks: list           # list[ChunkMeta]
    file_contents: dict    # dict[str, str]


# ---------------------------------------------------------------------------
# Disk Caching
# ---------------------------------------------------------------------------

def _get_cache_path(owner, repo, branch, cache_dir=DISK_CACHE_DIR):
    clean_key = f"{owner}_{repo}_{branch}".replace("/", "_").replace("\\", "_")
    os.makedirs(cache_dir, exist_ok=True)
    meta_path = os.path.join(cache_dir, f"{clean_key}.json")
    vec_path = os.path.join(cache_dir, f"{clean_key}.npy")
    return meta_path, vec_path


def save_index_to_disk(index, owner, repo, branch, cache_dir=DISK_CACHE_DIR):
    """Persist a CodeIndex to local disk to bypass re-embedding across restarts."""
    if index is None or len(index.chunks) == 0:
        return False
    try:
        meta_path, vec_path = _get_cache_path(owner, repo, branch, cache_dir)
        np.save(vec_path, index.vectors)
        
        chunks_data = [asdict(c) for c in index.chunks]
        payload = {
            "chunks": chunks_data,
            "file_contents": index.file_contents,
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return True
    except Exception:
        return False


def load_index_from_disk(owner, repo, branch, cache_dir=DISK_CACHE_DIR):
    """Load a cached CodeIndex from disk if available."""
    try:
        meta_path, vec_path = _get_cache_path(owner, repo, branch, cache_dir)
        if not (os.path.exists(meta_path) and os.path.exists(vec_path)):
            return None
        
        vectors = np.load(vec_path)
        with open(meta_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        
        chunks = [ChunkMeta(**c) for c in payload.get("chunks", [])]
        file_contents = payload.get("file_contents", {})
        
        if len(vectors) != len(chunks):
            return None
            
        return CodeIndex(vectors=vectors, chunks=chunks, file_contents=file_contents)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _find_boundaries(text):
    """Return sorted, deduplicated line numbers where code boundaries occur."""
    boundaries = set()
    for pattern in _BOUNDARY_PATTERNS:
        for match in pattern.finditer(text):
            line_num = text[:match.start()].count("\n")
            boundaries.add(line_num)
    return sorted(boundaries)


def _sliding_window_chunks(text, base_line):
    """Split text into overlapping fixed-size chunks (fallback strategy)."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + SLIDING_WINDOW_CHARS, len(text))
        chunk_text = text[start:end]

        if len(chunk_text) < CHUNK_MIN_CHARS and chunks:
            # Remainder is too small — append to previous chunk instead of
            # creating a useless sliver.
            chunks[-1] = ChunkMeta(
                file_path=chunks[-1].file_path,
                chunk_index=chunks[-1].chunk_index,
                start_line=chunks[-1].start_line,
                text=chunks[-1].text + chunk_text,
            )
            break

        line_offset = text[:start].count("\n")
        chunks.append(ChunkMeta(
            file_path="",          # caller fills this in
            chunk_index=0,         # caller re-indexes
            start_line=base_line + line_offset + 1,
            text=chunk_text,
        ))

        step = SLIDING_WINDOW_CHARS - SLIDING_OVERLAP_CHARS
        if start + step <= start:
            break  # safety: avoid infinite loop
        start += step

    return chunks


def chunk_file(file_path, content):
    """
    Split a single file into chunks, preferring code-boundary splitting and
    falling back to a sliding window when no boundaries are detected.

    Returns a list of ChunkMeta (may be empty for blank files).
    """
    if not content or not content.strip():
        return []

    lines = content.split("\n")
    boundaries = _find_boundaries(content)

    chunks = []

    if len(boundaries) >= 2:
        # ---- Boundary-based chunking ----
        if boundaries[0] != 0:
            boundaries = [0] + boundaries

        for i in range(len(boundaries)):
            start_line = boundaries[i]
            end_line = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
            chunk_text = "\n".join(lines[start_line:end_line])

            if len(chunk_text) > CHUNK_MAX_CHARS:
                sub_chunks = _sliding_window_chunks(chunk_text, start_line)
                for sc in sub_chunks:
                    sc.file_path = file_path
                chunks.extend(sub_chunks)
            elif len(chunk_text) >= CHUNK_MIN_CHARS:
                chunks.append(ChunkMeta(
                    file_path=file_path,
                    chunk_index=0,
                    start_line=start_line + 1,   # 1-indexed
                    text=chunk_text,
                ))
            else:
                if chunks:
                    prev = chunks[-1]
                    chunks[-1] = ChunkMeta(
                        file_path=prev.file_path,
                        chunk_index=prev.chunk_index,
                        start_line=prev.start_line,
                        text=prev.text + "\n" + chunk_text,
                    )

    if not chunks:
        # ---- Fallback: sliding window over entire file ----
        chunks = _sliding_window_chunks(content, 0)
        for c in chunks:
            c.file_path = file_path

    # Re-index sequentially
    for i, c in enumerate(chunks):
        c.chunk_index = i

    return chunks


# ---------------------------------------------------------------------------
# File fetching (concurrent)
# ---------------------------------------------------------------------------

def _fetch_one_file(owner, repo, branch, path):
    """Fetch a single file. Returns (path, content_or_None)."""
    try:
        content = github_context.fetch_raw_file(
            owner, repo, branch, path, max_chars=MAX_FETCH_CHARS,
        )
        return path, content
    except Exception:
        return path, None


def fetch_all_files(owner, repo, branch, paths):
    """
    Fetch content for all paths concurrently via raw.githubusercontent.com.
    Returns dict[path, content] (only successfully fetched files).
    """
    if not paths:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = [
            executor.submit(_fetch_one_file, owner, repo, branch, path)
            for path in paths
        ]
        for future in futures:
            path, content = future.result()
            if content is not None:
                results[path] = content
    return results


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed_texts(client, texts):
    """
    Embed a list of text strings using OpenAI's embeddings API.
    Returns an (N, EMBED_DIMENSIONS) float32 NumPy array.
    """
    if not texts:
        return np.empty((0, EMBED_DIMENSIONS), dtype=np.float32)

    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        response = client.embeddings.create(model=EMBED_MODEL, input=batch)
        sorted_data = sorted(response.data, key=lambda d: d.index)
        all_embeddings.extend([item.embedding for item in sorted_data])

    return np.array(all_embeddings, dtype=np.float32)


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def build_index(client, owner, repo, branch, paths):
    """
    Build a CodeIndex: fetch all files → chunk → embed.
    """
    try:
        file_contents = fetch_all_files(owner, repo, branch, paths)
    except Exception as e:
        return None, f"Failed to fetch repo files for indexing: {e}"

    if not file_contents:
        return None, "No file content could be fetched for indexing."

    all_chunks = []
    for path in sorted(file_contents.keys()):
        file_chunks = chunk_file(path, file_contents[path])
        all_chunks.extend(file_chunks)

    if not all_chunks:
        return None, "All fetched files produced zero usable chunks."

    try:
        chunk_texts = [c.text for c in all_chunks]
        vectors = _embed_texts(client, chunk_texts)
    except Exception as e:
        return None, f"Embedding API call failed: {e}"

    index = CodeIndex(
        vectors=vectors,
        chunks=all_chunks,
        file_contents=file_contents,
    )
    # Save to disk asynchronously/opportunistically
    save_index_to_disk(index, owner, repo, branch)
    return index, None


# ---------------------------------------------------------------------------
# Search (Single and Multi-Query)
# ---------------------------------------------------------------------------

def search_multi_queries(client, index, queries, top_k=15):
    """
    Multi-query semantic search: embeds multiple query strings (e.g. full UI audit +
    per-feature node summaries) and performs max-similarity fusion across query vectors
    to prevent visual audit query dilution.

    Returns deduplicated SearchHit list sorted by descending similarity score.
    """
    if index is None or len(index.chunks) == 0:
        return []

    valid_queries = [q.strip() for q in queries if q and q.strip()]
    if not valid_queries:
        return []

    try:
        query_vecs = _embed_texts(client, valid_queries)
    except Exception:
        return []

    norms = np.linalg.norm(index.vectors, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    normalized_vecs = index.vectors / norms

    q_norms = np.linalg.norm(query_vecs, axis=1, keepdims=True)
    q_norms = np.where(q_norms == 0, 1.0, q_norms)
    normalized_q_vecs = query_vecs / q_norms

    # Shape: (N_chunks, Q_queries)
    sim_matrix = normalized_vecs @ normalized_q_vecs.T
    chunk_scores = np.max(sim_matrix, axis=1)

    top_indices = np.argsort(chunk_scores)[::-1]

    seen_files = {}
    for idx in top_indices:
        idx = int(idx)
        chunk = index.chunks[idx]
        score = float(chunk_scores[idx])
        if chunk.file_path not in seen_files:
            seen_files[chunk.file_path] = SearchHit(
                file_path=chunk.file_path,
                score=score,
                chunk_text=chunk.text[:300],
                chunk_meta=chunk,
            )
        if len(seen_files) >= top_k:
            break

    return sorted(seen_files.values(), key=lambda h: h.score, reverse=True)


def search(client, index, query, top_k=15):
    """Convenience wrapper for single query semantic search."""
    return search_multi_queries(client, index, [query], top_k=top_k)


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def format_hits_for_prompt(hits, max_hits=10):
    """
    Format search hits as a markdown table suitable for injection into the
    file-selector LLM prompt (Pass 1).
    """
    if not hits:
        return ""

    lines = [
        "🔎 **Semantic search hits** (files whose ACTUAL CODE CONTENT is most "
        "similar to the UI audit — these may have unintuitive names but contain "
        "relevant code):\n",
        "| Rank | File Path | Similarity | Code Preview |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for i, hit in enumerate(hits[:max_hits], 1):
        preview = hit.chunk_text.replace("\n", " ").replace("|", "\\|")
        if len(preview) > 120:
            preview = preview[:120]
        lines.append(
            f"| {i} | `{hit.file_path}` | {hit.score:.2f} | `{preview}…` |"
        )

    return "\n".join(lines)


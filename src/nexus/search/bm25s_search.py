"""BM25S-based text search for fast ranked retrieval.

Implements BM25S (arXiv:2407.03618) for 500x faster ranked text search:
- Eager sparse scoring during indexing (pre-computed BM25 scores)
- Scipy sparse matrices for efficient computation
- Memory-mapped index loading for large codebases
- Code-aware tokenization (camelCase, snake_case splitting)

This module provides an alternative to database FTS when:
- SQLite FTS5 lacks true BM25 scoring with IDF
- PostgreSQL < 17 has slow ts_rank() degradation
- Code-specific tokenization is needed

Usage:
    from nexus.search.bm25s_search import BM25SIndex, CodeTokenizer

    # Create index
    index = BM25SIndex(index_dir=".nexus-data/bm25s")
    await index.initialize()

    # Index documents
    await index.index_document(path_id, path, content)

    # Search
    results = await index.search("authentication handler", limit=10)

References:
    - BM25S Paper: https://arxiv.org/abs/2407.03618
    - BM25S GitHub: https://github.com/xhluca/bm25s
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import bm25s as bm25s_module

logger = logging.getLogger(__name__)

# Check if bm25s is available
try:
    import bm25s as bm25s_module

    BM25S_AVAILABLE = True
except ImportError:
    bm25s_module = None
    BM25S_AVAILABLE = False


# Common programming stopwords (keywords that appear frequently but aren't discriminating)
CODE_STOPWORDS = frozenset(
    {
        # Common English stopwords
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "and",
        "but",
        "if",
        "or",
        "because",
        "until",
        "while",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        # Common code tokens (too frequent to be discriminating)
        "i",
        "j",
        "k",
        "n",
        "x",
        "y",
        "z",
        "s",
        "t",
        "e",
        "args",
        "kwargs",
        "self",
        "cls",
        "none",
        "true",
        "false",
        "null",
    }
)


@dataclass
class BM25SSearchResult:
    """BM25S search result with metadata."""

    path: str
    path_id: str
    score: float
    content_preview: str = ""
    matched_field: str = (
        "content"  # Issue #1092: Track which field matched (filename, path, content)
    )


@dataclass
class CodeTokenizer:
    """Code-aware tokenizer that splits identifiers.

    Handles:
    - camelCase: getUserName -> [get, user, name]
    - snake_case: get_user_name -> [get, user, name]
    - PascalCase: UserNameHandler -> [user, name, handler]
    - SCREAMING_SNAKE_CASE: MAX_VALUE -> [max, value]
    - Numbers: user123 -> [user, 123] (keeps numbers for version matching)
    """

    stopwords: frozenset[str] = field(default_factory=lambda: CODE_STOPWORDS)
    min_token_length: int = 2
    max_token_length: int = 50

    def split_identifier(self, identifier: str) -> list[str]:
        """Split a single identifier into component words.

        Args:
            identifier: A single identifier (e.g., "getUserName", "get_user_name")

        Returns:
            List of lowercase component words
        """
        if not identifier:
            return []

        # Handle snake_case and SCREAMING_SNAKE_CASE
        if "_" in identifier:
            parts = identifier.split("_")
            tokens = []
            for part in parts:
                if part:
                    # Recursively split camelCase within snake_case parts
                    tokens.extend(self._split_camel_case(part))
            return tokens

        # Handle camelCase and PascalCase
        return self._split_camel_case(identifier)

    def _split_camel_case(self, text: str) -> list[str]:
        """Split camelCase/PascalCase into words, including number boundaries.

        Args:
            text: Text to split

        Returns:
            List of lowercase words
        """
        if not text:
            return []

        # Insert space before uppercase letters (handles camelCase)
        # Also handles sequences like "HTTPServer" -> "HTTP Server"
        result = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        result = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", result)

        # Split on letter-to-number and number-to-letter boundaries
        # e.g., "user123" -> "user 123", "123user" -> "123 user"
        result = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", result)
        result = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", result)

        # Split and lowercase
        words = result.split()
        return [w.lower() for w in words if w]

    def tokenize(self, text: str) -> list[str]:
        """Tokenize text with code-aware splitting.

        Args:
            text: Text to tokenize (code, comments, documentation)

        Returns:
            List of tokens
        """
        if not text:
            return []

        # Extract words and numbers (identifiers, keywords, literals)
        # This regex matches: identifiers, numbers, and common operators
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+", text)

        tokens: list[str] = []
        for word in words:
            # Split identifiers
            sub_tokens = self.split_identifier(word)

            for token in sub_tokens:
                # Apply filters
                if len(token) < self.min_token_length:
                    continue
                if len(token) > self.max_token_length:
                    continue
                if token in self.stopwords:
                    continue

                tokens.append(token)

        return tokens

    def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
        """Tokenize multiple texts.

        Args:
            texts: List of texts to tokenize

        Returns:
            List of token lists
        """
        return [self.tokenize(text) for text in texts]


class BM25SIndex:
    """BM25S index for fast ranked text search.

    Manages a BM25S index with:
    - Code-aware tokenization
    - Memory-mapped loading for large indices
    - Incremental updates via delta indexing
    - Thread-safe operations
    """

    def __init__(
        self,
        index_dir: str | Path = ".nexus-data/bm25s",
        method: str = "lucene",
        k1: float = 1.5,
        b: float = 0.75,
    ):
        """Initialize BM25S index.

        Args:
            index_dir: Directory to store index files
            method: BM25 variant ("lucene", "robertson", "atire", "bm25l", "bm25+")
            k1: Term frequency saturation parameter (default: 1.5)
            b: Length normalization parameter (default: 0.75)
        """
        self.index_dir = Path(index_dir)
        self.method = method
        self.k1 = k1
        self.b = b

        # Tokenizer
        self.tokenizer = CodeTokenizer()

        # Index state
        self._retriever: Any | None = None
        self._corpus: list[str] = []  # Document contents
        self._path_ids: list[str] = []  # Document path IDs
        self._paths: list[str] = []  # Document paths
        self._path_to_idx: dict[str, int] = {}  # path_id -> corpus index

        # Thread safety
        self._lock = threading.RLock()
        self._initialized = False

        # Delta index for incremental updates
        self._delta_corpus: list[str] = []
        self._delta_path_ids: list[str] = []
        self._delta_paths: list[str] = []
        self._delta_threshold = 100  # Merge delta after N documents

    @property
    def is_available(self) -> bool:
        """Check if BM25S is available."""
        return BM25S_AVAILABLE

    async def initialize(self) -> bool:
        """Initialize or load existing index.

        Returns:
            True if initialization successful
        """
        if not BM25S_AVAILABLE:
            logger.warning("bm25s not installed. Install with: pip install bm25s")
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._initialize_sync)

    def _initialize_sync(self) -> bool:
        """Synchronous initialization."""
        with self._lock:
            if self._initialized:
                return True

            try:
                # Create index directory
                self.index_dir.mkdir(parents=True, exist_ok=True)

                # Try to load existing index
                index_path = self.index_dir / "index"
                if index_path.exists():
                    self._load_index()
                else:
                    # Create empty retriever
                    self._retriever = bm25s_module.BM25(method=self.method, k1=self.k1, b=self.b)

                self._initialized = True
                logger.info(
                    f"BM25S index initialized: {len(self._corpus)} documents, "
                    f"method={self.method}, k1={self.k1}, b={self.b}"
                )
                return True

            except Exception as e:
                logger.error(f"Failed to initialize BM25S index: {e}")
                return False

    def _load_index(self) -> None:
        """Load existing index from disk."""
        index_path = self.index_dir / "index"

        # Load retriever with memory mapping for efficiency
        self._retriever = bm25s_module.BM25.load(
            str(index_path),
            mmap=True,  # Memory-mapped for large indices
        )

        # Load document metadata
        metadata_path = self.index_dir / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                metadata = json.load(f)
                self._corpus = metadata.get("corpus", [])
                self._path_ids = metadata.get("path_ids", [])
                self._paths = metadata.get("paths", [])
                self._path_to_idx = {pid: i for i, pid in enumerate(self._path_ids)}

        logger.info(f"Loaded BM25S index: {len(self._corpus)} documents")

    def _save_index(self) -> None:
        """Save index to disk."""
        if self._retriever is None:
            return

        index_path = self.index_dir / "index"

        # Save retriever
        self._retriever.save(str(index_path))

        # Save document metadata
        metadata_path = self.index_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(
                {
                    "corpus": self._corpus,
                    "path_ids": self._path_ids,
                    "paths": self._paths,
                },
                f,
            )

        logger.debug(f"Saved BM25S index: {len(self._corpus)} documents")

    async def index_document(
        self,
        path_id: str,
        path: str,
        content: str,
    ) -> bool:
        """Index a single document.

        Uses delta indexing for efficiency - documents are added to a delta
        index and merged periodically.

        Args:
            path_id: Unique document identifier
            path: Document path
            content: Document content

        Returns:
            True if indexing successful
        """
        if not self._initialized and not await self.initialize():
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._index_document_sync,
            path_id,
            path,
            content,
        )

    def _index_document_sync(
        self,
        path_id: str,
        path: str,
        content: str,
    ) -> bool:
        """Synchronous document indexing."""
        with self._lock:
            try:
                # Check if document already exists
                if path_id in self._path_to_idx:
                    # Remove from main index by marking for rebuild
                    # (BM25S doesn't support in-place updates)
                    self._remove_document_sync(path_id)

                # Add to delta index
                self._delta_corpus.append(content)
                self._delta_path_ids.append(path_id)
                self._delta_paths.append(path)

                # Merge delta if threshold reached
                if len(self._delta_corpus) >= self._delta_threshold:
                    self._merge_delta()

                return True

            except Exception as e:
                logger.error(f"Failed to index document {path}: {e}")
                return False

    def _remove_document_sync(self, path_id: str) -> None:
        """Remove document from index (marks for rebuild)."""
        if path_id not in self._path_to_idx:
            return

        idx = self._path_to_idx[path_id]

        # Remove from lists (this is expensive, but rare)
        del self._corpus[idx]
        del self._path_ids[idx]
        del self._paths[idx]

        # Rebuild path_to_idx mapping
        self._path_to_idx = {pid: i for i, pid in enumerate(self._path_ids)}

        # Mark index as needing rebuild
        self._needs_rebuild = True

    def _merge_delta(self) -> None:
        """Merge delta index into main index."""
        if not self._delta_corpus:
            return

        logger.debug(f"Merging {len(self._delta_corpus)} documents into main index")

        # Combine main and delta
        all_corpus = self._corpus + self._delta_corpus
        all_path_ids = self._path_ids + self._delta_path_ids
        all_paths = self._paths + self._delta_paths

        # Tokenize all documents
        all_tokens = self.tokenizer.tokenize_batch(all_corpus)

        # Rebuild index
        self._retriever = bm25s_module.BM25(method=self.method, k1=self.k1, b=self.b)
        self._retriever.index(all_tokens)

        # Update state
        self._corpus = all_corpus
        self._path_ids = all_path_ids
        self._paths = all_paths
        self._path_to_idx = {pid: i for i, pid in enumerate(self._path_ids)}

        # Clear delta
        self._delta_corpus = []
        self._delta_path_ids = []
        self._delta_paths = []

        # Save to disk
        self._save_index()

    async def index_documents_bulk(
        self,
        documents: list[tuple[str, str, str]],  # (path_id, path, content)
    ) -> int:
        """Index multiple documents in bulk.

        More efficient than individual indexing as it builds index once.

        Args:
            documents: List of (path_id, path, content) tuples

        Returns:
            Number of documents indexed
        """
        if not documents:
            return 0

        if not self._initialized and not await self.initialize():
            return 0

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._index_documents_bulk_sync,
            documents,
        )

    def _index_documents_bulk_sync(
        self,
        documents: list[tuple[str, str, str]],
    ) -> int:
        """Synchronous bulk indexing."""
        with self._lock:
            try:
                # Add all documents to delta
                for path_id, path, content in documents:
                    # Remove existing if present
                    if path_id in self._path_to_idx:
                        self._remove_document_sync(path_id)

                    self._delta_corpus.append(content)
                    self._delta_path_ids.append(path_id)
                    self._delta_paths.append(path)

                # Force merge
                self._merge_delta()

                return len(documents)

            except Exception as e:
                logger.error(f"Failed to bulk index documents: {e}")
                return 0

    async def search(
        self,
        query: str,
        limit: int = 10,
        path_filter: str | None = None,
    ) -> list[BM25SSearchResult]:
        """Search documents with BM25 ranking.

        Args:
            query: Search query
            limit: Maximum number of results
            path_filter: Optional path prefix filter

        Returns:
            List of search results ranked by relevance
        """
        if not self._initialized and not await self.initialize():
            return []

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._search_sync,
            query,
            limit,
            path_filter,
        )

    def _detect_matched_field(self, query: str, path: str, content: str) -> str:  # noqa: ARG002
        """Detect which field the query primarily matched in.

        Issue #1092: Used for attribute-based ranking.

        Args:
            query: Search query
            path: File path
            content: File content

        Returns:
            Name of matched field: "filename", "path", or "content"
        """
        query_lower = query.lower().strip()
        query_terms = query_lower.split()

        # Extract filename from path
        filename = path.split("/")[-1].lower() if path else ""
        filename_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename

        # Check filename (highest priority)
        if query_lower in filename or query_lower in filename_without_ext:
            return "filename"

        # Check if all query terms appear in filename
        if query_terms and all(term in filename for term in query_terms):
            return "filename"

        # Check path (excluding filename)
        path_lower = path.lower() if path else ""
        path_without_filename = "/".join(path_lower.split("/")[:-1]) if "/" in path_lower else ""
        if query_lower in path_without_filename:
            return "path"

        # Default to content
        return "content"

    def _search_sync(
        self,
        query: str,
        limit: int,
        path_filter: str | None,
    ) -> list[BM25SSearchResult]:
        """Synchronous search."""
        with self._lock:
            # Merge any pending delta documents first
            if self._delta_corpus:
                self._merge_delta()

            if not self._corpus or self._retriever is None:
                return []

            try:
                # Tokenize query
                query_tokens = self.tokenizer.tokenize(query)
                if not query_tokens:
                    return []

                # Search - retrieve more than needed for filtering
                k = min(limit * 3, len(self._corpus))
                results, scores = self._retriever.retrieve(
                    bm25s_module.tokenize([" ".join(query_tokens)]),
                    k=k,
                )

                # Build results
                search_results: list[BM25SSearchResult] = []
                for idx, score in zip(results[0], scores[0], strict=True):
                    if len(search_results) >= limit:
                        break

                    # Skip zero/negative scores
                    if score <= 0:
                        continue

                    path = self._paths[idx]
                    path_id = self._path_ids[idx]

                    # Apply path filter
                    if path_filter and not path.startswith(path_filter):
                        continue

                    # Get content preview (first 200 chars)
                    content = self._corpus[idx]
                    preview = content[:200] + "..." if len(content) > 200 else content

                    # Issue #1092: Detect which field matched for attribute ranking
                    matched_field = self._detect_matched_field(query, path, content)

                    search_results.append(
                        BM25SSearchResult(
                            path=path,
                            path_id=path_id,
                            score=float(score),
                            content_preview=preview,
                            matched_field=matched_field,
                        )
                    )

                return search_results

            except Exception as e:
                logger.error(f"BM25S search failed: {e}")
                return []

    async def delete_document(self, path_id: str) -> bool:
        """Delete a document from the index.

        Args:
            path_id: Document identifier to delete

        Returns:
            True if deletion successful
        """
        if not self._initialized:
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._delete_document_sync, path_id)

    def _delete_document_sync(self, path_id: str) -> bool:
        """Synchronous document deletion."""
        with self._lock:
            try:
                self._remove_document_sync(path_id)
                # Rebuild index after deletion
                if self._corpus:
                    all_tokens = self.tokenizer.tokenize_batch(self._corpus)
                    self._retriever = bm25s_module.BM25(method=self.method, k1=self.k1, b=self.b)
                    self._retriever.index(all_tokens)
                    self._save_index()
                return True
            except Exception as e:
                logger.error(f"Failed to delete document {path_id}: {e}")
                return False

    async def rebuild_index(self) -> bool:
        """Rebuild the entire index from current corpus.

        Returns:
            True if rebuild successful
        """
        if not self._initialized:
            return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._rebuild_index_sync)

    def _rebuild_index_sync(self) -> bool:
        """Synchronous index rebuild."""
        with self._lock:
            try:
                # Merge any pending delta
                if self._delta_corpus:
                    self._merge_delta()
                    return True

                # Rebuild from scratch
                if self._corpus:
                    all_tokens = self.tokenizer.tokenize_batch(self._corpus)
                    self._retriever = bm25s_module.BM25(method=self.method, k1=self.k1, b=self.b)
                    self._retriever.index(all_tokens)
                    self._save_index()

                return True

            except Exception as e:
                logger.error(f"Failed to rebuild index: {e}")
                return False

    async def get_stats(self) -> dict[str, Any]:
        """Get index statistics.

        Returns:
            Dictionary with index statistics
        """
        with self._lock:
            return {
                "available": BM25S_AVAILABLE,
                "initialized": self._initialized,
                "total_documents": len(self._corpus),
                "delta_documents": len(self._delta_corpus),
                "method": self.method,
                "k1": self.k1,
                "b": self.b,
                "index_dir": str(self.index_dir),
            }

    async def clear(self) -> bool:
        """Clear all indexed documents.

        Returns:
            True if clear successful
        """
        with self._lock:
            try:
                self._retriever = bm25s_module.BM25(method=self.method, k1=self.k1, b=self.b)
                self._corpus = []
                self._path_ids = []
                self._paths = []
                self._path_to_idx = {}
                self._delta_corpus = []
                self._delta_path_ids = []
                self._delta_paths = []

                # Remove index files
                if self.index_dir.exists():
                    import shutil

                    shutil.rmtree(self.index_dir)
                    self.index_dir.mkdir(parents=True, exist_ok=True)

                return True

            except Exception as e:
                logger.error(f"Failed to clear index: {e}")
                return False


# Global singleton for shared index access
_global_index: BM25SIndex | None = None
_global_lock = threading.Lock()


def get_bm25s_index(index_dir: str | Path = ".nexus-data/bm25s") -> BM25SIndex:
    """Get or create global BM25S index.

    Args:
        index_dir: Directory for index storage

    Returns:
        BM25SIndex instance
    """
    global _global_index

    with _global_lock:
        if _global_index is None:
            _global_index = BM25SIndex(index_dir=index_dir)
        return _global_index


def is_bm25s_available() -> bool:
    """Check if BM25S is available.

    Returns:
        True if bm25s library is installed
    """
    return BM25S_AVAILABLE

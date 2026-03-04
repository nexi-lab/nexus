"""SPLADE learned sparse retrieval.

SPLADE (SParse Lexical AnD Expansion) uses a masked language model to produce
weighted sparse vectors that capture both term importance and query expansion.
It outperforms BM25 on most retrieval benchmarks while maintaining the
efficiency of inverted indexes.

This module provides a SpladeRetriever that:
1. Loads a pre-trained SPLADE model (e.g. naver/splade-cocondenser-ensembledistil)
2. Encodes documents into sparse vectors during initialization
3. Performs fast sparse retrieval at query time

Reference:
- SPLADE v2: https://arxiv.org/abs/2109.10086
- Production usage: SPLADE -> ColBERT/Cross-encoder for best quality
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports for optional dependencies
_SPLADE_AVAILABLE = False
try:
    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    _SPLADE_AVAILABLE = True
except ImportError:
    pass


@dataclass
class SearchResult:
    """Minimal search result for SPLADE retrieval."""

    path: str
    chunk_index: int
    chunk_text: str
    score: float
    start_offset: int | None = None
    end_offset: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    search_type: str = "splade"


class SpladeRetriever:
    """SPLADE learned sparse retrieval over an in-memory document collection.

    Uses a masked language model to produce sparse vectors with term
    expansion, then performs efficient dot-product retrieval.
    """

    def __init__(self, model_name: str = "naver/splade-cocondenser-ensembledistil"):
        self._model_name = model_name
        self._tokenizer: Any = None
        self._model: Any = None
        self._device: str = "cpu"
        self._initialized = False

        # Document index: sparse vectors stored as {token_id: weight}
        self._doc_vectors: list[dict[int, float]] = []
        self._doc_paths: list[str] = []
        self._doc_texts: list[str] = []

    async def initialize(self) -> None:
        """Load SPLADE model (CPU-only, runs in thread to avoid blocking)."""
        if not _SPLADE_AVAILABLE:
            raise ImportError("SPLADE requires torch and transformers")

        def _load() -> None:
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            self._model = AutoModelForMaskedLM.from_pretrained(self._model_name)
            self._model.eval()
            logger.info(
                "SPLADE model loaded: %s (vocab=%d)",
                self._model_name,
                self._tokenizer.vocab_size,
            )

        await asyncio.to_thread(_load)
        self._initialized = True

    def _encode_sparse(self, text: str) -> dict[int, float]:
        """Encode text into a SPLADE sparse vector.

        Uses log(1 + ReLU(logits)) aggregated over tokens to produce
        sparse term weights with expansion.
        """
        inputs = self._tokenizer(
            text,
            return_tensors="pt",
            max_length=512,
            truncation=True,
            padding=True,
        )
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            output = self._model(**inputs)

        # SPLADE aggregation: max over sequence of log(1 + ReLU(logits))
        logits = output.logits  # (1, seq_len, vocab_size)
        weights = torch.max(
            torch.log1p(torch.relu(logits)),
            dim=1,
        ).values.squeeze(0)  # (vocab_size,)

        # Convert to sparse dict (only non-zero entries)
        non_zero = weights.nonzero(as_tuple=True)[0]
        sparse_vec = {idx.item(): weights[idx].item() for idx in non_zero}
        return sparse_vec

    async def index_documents(
        self,
        documents: list[tuple[str, str]],
        batch_size: int = 32,
    ) -> int:
        """Index documents for SPLADE retrieval.

        Args:
            documents: List of (path, content) tuples
            batch_size: Documents per batch

        Returns:
            Number of documents indexed
        """
        if not self._initialized:
            raise RuntimeError("SPLADE not initialized")

        total = len(documents)
        logger.info("[SPLADE] Indexing %d documents...", total)

        def _index_batch(batch: list[tuple[str, str]]) -> list[tuple[str, str, dict]]:
            results = []
            for path, content in batch:
                # Use first 512 tokens worth of content for SPLADE
                sparse = self._encode_sparse(content)
                results.append((path, content, sparse))
            return results

        indexed = 0
        for start in range(0, total, batch_size):
            end = min(start + batch_size, total)
            batch = documents[start:end]

            batch_results = await asyncio.to_thread(_index_batch, batch)
            for path, content, sparse in batch_results:
                self._doc_paths.append(path)
                self._doc_texts.append(content)
                self._doc_vectors.append(sparse)
                indexed += 1

            if (start // batch_size) % 10 == 0:
                logger.info("[SPLADE] Indexed %d/%d docs", indexed, total)

        logger.info("[SPLADE] Indexing complete: %d documents", indexed)
        return indexed

    async def search(
        self,
        query: str,
        limit: int = 10,
        path_filter: str | None = None,
    ) -> list[SearchResult]:
        """Search using SPLADE sparse dot-product retrieval."""
        if not self._initialized or not self._doc_vectors:
            return []

        def _search_sync() -> list[SearchResult]:
            # Encode query
            query_vec = self._encode_sparse(query)
            if not query_vec:
                return []

            # Compute dot products with all documents
            scores: list[tuple[int, float]] = []
            for i, doc_vec in enumerate(self._doc_vectors):
                # Apply path filter
                if path_filter and not self._doc_paths[i].startswith(path_filter):
                    continue

                # Sparse dot product (only overlapping terms)
                score = sum(query_vec[tid] * doc_vec.get(tid, 0.0) for tid in query_vec)
                if score > 0:
                    scores.append((i, score))

            # Sort by score and take top-k
            scores.sort(key=lambda x: x[1], reverse=True)
            top_k = scores[:limit]

            results = []
            for idx, score in top_k:
                text = self._doc_texts[idx]
                results.append(
                    SearchResult(
                        path=self._doc_paths[idx],
                        chunk_index=0,
                        chunk_text=text[:500] if len(text) > 500 else text,
                        score=score,
                        search_type="splade",
                    )
                )

            return results

        return await asyncio.to_thread(_search_sync)

    @property
    def document_count(self) -> int:
        """Number of indexed documents."""
        return len(self._doc_vectors)

"""Tests for citation extraction (src/nexus/services/llm_citation.py)."""

from __future__ import annotations

from nexus.services.llm.llm_citation import Citation, CitationExtractor, DocumentReadResult


class TestCitation:
    """Test Citation dataclass."""

    def test_citation_creation(self) -> None:
        """Test creating a citation with all fields."""
        citation = Citation(
            path="/docs/readme.md",
            chunk_index=2,
            score=0.85,
            start_offset=100,
            end_offset=200,
        )
        assert citation.path == "/docs/readme.md"
        assert citation.chunk_index == 2
        assert citation.score == 0.85
        assert citation.start_offset == 100
        assert citation.end_offset == 200

    def test_citation_defaults(self) -> None:
        """Test citation with only required field."""
        citation = Citation(path="/test.txt")
        assert citation.path == "/test.txt"
        assert citation.chunk_index is None
        assert citation.score is None
        assert citation.start_offset is None
        assert citation.end_offset is None


class TestDocumentReadResult:
    """Test DocumentReadResult dataclass."""

    def test_document_read_result_creation(self) -> None:
        """Test creating a full DocumentReadResult."""
        citations = [Citation(path="/a.txt", score=0.9)]
        result = DocumentReadResult(
            answer="The answer is 42.",
            citations=citations,
            sources=["/a.txt"],
            tokens_used=150,
            cost=0.001,
            cached=False,
        )
        assert result.answer == "The answer is 42."
        assert len(result.citations) == 1
        assert result.sources == ["/a.txt"]
        assert result.tokens_used == 150
        assert result.cost == 0.001
        assert result.cached is False
        assert result.cache_savings is None

    def test_document_read_result_from_cached(self) -> None:
        """Test creating result from cached response."""
        chunks = [
            {"path": "/a.txt", "chunk_index": 0, "score": 0.9, "start_offset": 0, "end_offset": 50},
            {
                "path": "/b.txt",
                "chunk_index": 1,
                "score": 0.8,
                "start_offset": None,
                "end_offset": None,
            },
        ]
        result = DocumentReadResult.from_cached("Cached answer", chunks=chunks)
        assert result.answer == "Cached answer"
        assert result.cached is True
        assert result.tokens_used == 0
        assert result.cost == 0.0
        assert len(result.citations) == 2
        assert result.sources == ["/a.txt", "/b.txt"]

    def test_document_read_result_from_cached_no_chunks(self) -> None:
        """Test creating result from cached response without chunks."""
        result = DocumentReadResult.from_cached("Cached answer")
        assert result.answer == "Cached answer"
        assert result.cached is True
        assert result.citations == []
        assert result.sources == []

    def test_document_read_result_from_cached_dedup_sources(self) -> None:
        """Test that from_cached deduplicates source paths."""
        chunks = [
            {"path": "/a.txt", "chunk_index": 0, "score": 0.9},
            {"path": "/a.txt", "chunk_index": 1, "score": 0.8},
        ]
        result = DocumentReadResult.from_cached("answer", chunks=chunks)
        assert result.sources == ["/a.txt"]
        assert len(result.citations) == 2


class TestCitationExtractor:
    """Test CitationExtractor."""

    def test_extract_citations_with_matches(self) -> None:
        """Test extracting citations that match source patterns."""
        answer = "According to [Source: /docs/readme.md], the answer is clear."
        chunks = [
            {
                "path": "/docs/readme.md",
                "chunk_index": 0,
                "score": 0.9,
                "start_offset": 0,
                "end_offset": 100,
            },
        ]
        citations = CitationExtractor.extract_citations(answer, chunks, include_all_sources=False)
        assert len(citations) == 1
        assert citations[0].path == "/docs/readme.md"
        assert citations[0].score == 0.9

    def test_extract_citations_empty_output(self) -> None:
        """Test extracting citations from empty answer."""
        chunks = [
            {"path": "/a.txt", "chunk_index": 0, "score": 0.9},
        ]
        citations = CitationExtractor.extract_citations("", chunks, include_all_sources=False)
        assert len(citations) == 0

    def test_extract_citations_no_sources(self) -> None:
        """Test extracting citations with no chunks."""
        citations = CitationExtractor.extract_citations(
            "Some answer text", [], include_all_sources=True
        )
        assert len(citations) == 0

    def test_extract_citations_include_all_sources(self) -> None:
        """Test that include_all_sources adds unreferenced chunks."""
        answer = "The answer is here."
        chunks = [
            {"path": "/a.txt", "chunk_index": 0, "score": 0.9, "start_offset": 0, "end_offset": 50},
            {
                "path": "/b.txt",
                "chunk_index": 1,
                "score": 0.8,
                "start_offset": None,
                "end_offset": None,
            },
        ]
        citations = CitationExtractor.extract_citations(answer, chunks, include_all_sources=True)
        assert len(citations) == 2
        paths = {c.path for c in citations}
        assert "/a.txt" in paths
        assert "/b.txt" in paths

    def test_extract_citations_unicode_paths(self) -> None:
        """Test extracting citations with unicode paths."""
        answer = "According to [Source: /docs/日本語.md], this is correct."
        chunks = [
            {
                "path": "/docs/日本語.md",
                "chunk_index": 0,
                "score": 0.95,
                "start_offset": None,
                "end_offset": None,
            },
        ]
        citations = CitationExtractor.extract_citations(answer, chunks, include_all_sources=False)
        assert len(citations) == 1
        assert citations[0].path == "/docs/日本語.md"

    def test_extract_citations_duplicate_sources(self) -> None:
        """Test that duplicate sources are not repeated in explicit matches."""
        answer = "See [Source: /a.txt] and also [Source: /a.txt] again."
        chunks = [
            {"path": "/a.txt", "chunk_index": 0, "score": 0.9, "start_offset": 0, "end_offset": 50},
        ]
        citations = CitationExtractor.extract_citations(answer, chunks, include_all_sources=False)
        # Should only have one citation for /a.txt, not two
        assert len(citations) == 1
        assert citations[0].path == "/a.txt"

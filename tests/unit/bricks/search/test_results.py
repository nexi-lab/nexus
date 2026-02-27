"""Unit tests for detect_matched_field and BaseSearchResult migration (Issue #1499).

Tests cover:
- detect_matched_field: all 6 field types, priority ordering, fallback
- BaseSearchResult: consolidated type produces identical output to subclasses
"""

import pytest

from nexus.bricks.search.results import BaseSearchResult, detect_matched_field

# =============================================================================
# detect_matched_field tests
# =============================================================================


class TestDetectMatchedField:
    """Test detect_matched_field from results.py."""

    def test_filename_exact_match(self) -> None:
        result = detect_matched_field("auth", "/src/auth.py")
        assert result == "filename"

    def test_filename_without_extension(self) -> None:
        result = detect_matched_field("config", "/src/config.yaml")
        assert result == "filename"

    def test_filename_all_terms(self) -> None:
        result = detect_matched_field("query router", "/src/query_router.py")
        assert result == "filename"

    def test_title_match(self) -> None:
        result = detect_matched_field(
            "authentication",
            "/docs/guide.md",
            title="Authentication Guide",
        )
        assert result == "title"

    def test_title_all_terms(self) -> None:
        result = detect_matched_field(
            "search guide",
            "/docs/misc.md",
            title="Search Implementation Guide",
        )
        assert result == "title"

    def test_tags_match(self) -> None:
        result = detect_matched_field(
            "python",
            "/docs/misc.md",
            tags=["python", "programming"],
        )
        assert result == "tags"

    def test_tags_partial_match(self) -> None:
        result = detect_matched_field(
            "python",
            "/docs/misc.md",
            tags=["python3", "code"],
        )
        assert result == "tags"

    def test_path_match(self) -> None:
        """Path match (excluding filename)."""
        result = detect_matched_field("docs", "/docs/guide/readme.md")
        assert result == "path"

    def test_description_match(self) -> None:
        result = detect_matched_field(
            "caching",
            "/src/utils.py",
            description="Implements caching layer for API",
        )
        assert result == "description"

    def test_fallback_to_content(self) -> None:
        result = detect_matched_field(
            "foobar_xyz",
            "/src/main.py",
        )
        assert result == "content"

    def test_fallback_when_no_matches(self) -> None:
        result = detect_matched_field(
            "something_unique",
            "/src/main.py",
            title="Other Title",
            tags=["unrelated"],
            description="Nothing relevant",
        )
        assert result == "content"

    # Priority ordering tests
    def test_filename_has_highest_priority(self) -> None:
        """Filename should beat title, tags, path, description."""
        result = detect_matched_field(
            "auth",
            "/auth/auth.py",
            title="Auth Module",
            tags=["auth"],
            description="auth implementation",
        )
        assert result == "filename"

    def test_title_beats_tags(self) -> None:
        result = detect_matched_field(
            "search",
            "/src/main.py",
            title="Search Module",
            tags=["search"],
            description="search impl",
        )
        assert result == "title"

    def test_tags_beat_path(self) -> None:
        result = detect_matched_field(
            "utils",
            "/utils/main.py",
            tags=["utils"],
        )
        assert result == "tags"

    def test_path_beats_description(self) -> None:
        result = detect_matched_field(
            "services",
            "/services/main.py",
            description="services layer",
        )
        assert result == "path"

    def test_case_insensitive(self) -> None:
        result = detect_matched_field("AUTH", "/src/auth.py")
        assert result == "filename"

    def test_empty_path(self) -> None:
        result = detect_matched_field("test", "")
        assert result == "content"

    def test_query_with_whitespace(self) -> None:
        result = detect_matched_field("  auth  ", "/src/auth.py")
        assert result == "filename"


# =============================================================================
# BaseSearchResult migration tests
# =============================================================================


class TestBaseSearchResultMigration:
    """Test that BaseSearchResult can replace all search result subclasses."""

    def test_base_result_construction(self) -> None:
        result = BaseSearchResult(
            path="/src/test.py",
            chunk_text="hello world",
            score=0.85,
        )
        assert result.path == "/src/test.py"
        assert result.chunk_text == "hello world"
        assert result.score == 0.85
        assert result.chunk_index == 0
        assert result.start_offset is None
        assert result.end_offset is None

    def test_base_result_all_fields(self) -> None:
        result = BaseSearchResult(
            path="/src/test.py",
            chunk_text="hello world",
            score=0.85,
            chunk_index=3,
            start_offset=100,
            end_offset=200,
            line_start=10,
            line_end=20,
            keyword_score=0.7,
            vector_score=0.9,
        )
        assert result.chunk_index == 3
        assert result.start_offset == 100
        assert result.end_offset == 200
        assert result.line_start == 10
        assert result.line_end == 20
        assert result.keyword_score == 0.7
        assert result.vector_score == 0.9

    def test_semantic_search_result_compat(self) -> None:
        """SemanticSearchResult (now BaseSearchResult alias) should work with ranking fields."""
        result = BaseSearchResult(
            path="/src/test.py",
            chunk_text="test",
            score=0.85,
            matched_field="filename",
            attribute_boost=3.0,
            original_score=0.5,
        )
        assert isinstance(result, BaseSearchResult)
        assert result.matched_field == "filename"
        assert result.attribute_boost == 3.0
        assert result.original_score == 0.5

    def test_daemon_search_result_compat(self) -> None:
        """daemon.SearchResult should still work as BaseSearchResult subclass."""
        from nexus.bricks.search.daemon import SearchResult

        result = SearchResult(
            path="/src/test.py",
            chunk_text="test",
            score=0.85,
            search_type="keyword",
        )
        assert isinstance(result, BaseSearchResult)
        assert result.search_type == "keyword"

    @pytest.mark.parametrize(
        ("result_cls", "extra_kwargs"),
        [
            pytest.param(
                "BaseSearchResult",
                {"matched_field": "content", "attribute_boost": 1.0, "original_score": 0.5},
                id="base-with-ranking",
            ),
            pytest.param(
                "SearchResult",
                {"search_type": "hybrid"},
                id="daemon",
            ),
        ],
    )
    def test_all_subclasses_share_base_fields(self, result_cls: str, extra_kwargs: dict) -> None:
        """All result subclasses should share identical base field behavior."""
        if result_cls == "BaseSearchResult":
            cls = BaseSearchResult
        else:
            from nexus.bricks.search.daemon import SearchResult as cls

        base_kwargs = {
            "path": "/test.py",
            "chunk_text": "test content",
            "score": 0.75,
            "chunk_index": 2,
            "line_start": 5,
            "line_end": 10,
        }
        result = cls(**base_kwargs, **extra_kwargs)

        # All base fields should be accessible
        assert result.path == "/test.py"
        assert result.chunk_text == "test content"
        assert result.score == 0.75
        assert result.chunk_index == 2
        assert result.line_start == 5
        assert result.line_end == 10

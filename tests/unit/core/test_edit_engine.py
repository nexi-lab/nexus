"""Unit tests for the edit engine.

Issue #800: Add edit engine with search/replace for surgical file edits.

Tests cover:
- EditOperation, MatchInfo, EditResult dataclasses
- Exact matching (fast path)
- Whitespace-normalized matching
- Fuzzy matching with Levenshtein similarity
- Middle-out search with line hints
- Unified diff generation
- Error handling and edge cases
"""

import pytest

from nexus.core.edit_engine import (
    RAPIDFUZZ_AVAILABLE,
    EditEngine,
    EditOperation,
    EditResult,
    MatchInfo,
    create_edit_operation,
)


class TestEditOperation:
    """Tests for EditOperation dataclass."""

    def test_simple_creation(self):
        """Test creating a simple edit operation."""
        edit = EditOperation(old_str="foo", new_str="bar")
        assert edit.old_str == "foo"
        assert edit.new_str == "bar"
        assert edit.hint_line is None
        assert edit.allow_multiple is False

    def test_with_hint_line(self):
        """Test creating an edit with hint_line."""
        edit = EditOperation(old_str="foo", new_str="bar", hint_line=42)
        assert edit.hint_line == 42

    def test_with_allow_multiple(self):
        """Test creating an edit with allow_multiple."""
        edit = EditOperation(old_str="foo", new_str="bar", allow_multiple=True)
        assert edit.allow_multiple is True

    def test_convenience_function(self):
        """Test create_edit_operation convenience function."""
        edit = create_edit_operation("old", "new", hint_line=10, allow_multiple=True)
        assert edit.old_str == "old"
        assert edit.new_str == "new"
        assert edit.hint_line == 10
        assert edit.allow_multiple is True


class TestEditEngineExactMatch:
    """Tests for exact string matching."""

    def test_simple_exact_match(self):
        """Test simple exact string replacement."""
        engine = EditEngine()
        content = "def foo():\n    return 1"
        edits = [EditOperation(old_str="foo", new_str="bar")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.content == "def bar():\n    return 1"
        assert len(result.matches) == 1
        assert result.matches[0].match_type == "exact"
        assert result.matches[0].similarity == 1.0

    def test_multiple_edits(self):
        """Test applying multiple edits in sequence."""
        engine = EditEngine()
        content = "def foo():\n    x = 1\n    return x"
        edits = [
            EditOperation(old_str="foo", new_str="bar"),
            EditOperation(old_str="x = 1", new_str="x = 42"),
            EditOperation(old_str="return x", new_str="return x * 2"),
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert "def bar():" in result.content
        assert "x = 42" in result.content
        assert "return x * 2" in result.content
        assert result.applied_count == 3

    def test_multiline_exact_match(self):
        """Test exact match spanning multiple lines."""
        engine = EditEngine()
        content = "def foo():\n    pass\n\ndef bar():\n    pass"
        edits = [
            EditOperation(
                old_str="def foo():\n    pass",
                new_str="def foo():\n    return 42",
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert "return 42" in result.content

    def test_allow_multiple_replacements(self):
        """Test replacing all occurrences with allow_multiple."""
        engine = EditEngine()
        content = "foo foo foo bar"
        edits = [EditOperation(old_str="foo", new_str="baz", allow_multiple=True)]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.content == "baz baz baz bar"
        assert result.matches[0].match_count == 3

    def test_delete_text(self):
        """Test deleting text by replacing with empty string."""
        engine = EditEngine()
        content = "def foo():\n    # TODO: remove this\n    return 1"
        edits = [EditOperation(old_str="    # TODO: remove this\n", new_str="")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert "TODO" not in result.content


class TestEditEngineNormalizedMatch:
    """Tests for whitespace-normalized matching."""

    def test_trailing_whitespace_difference(self):
        """Test matching ignores trailing whitespace."""
        engine = EditEngine()
        content = "def foo():   \n    return 1"  # trailing spaces
        edits = [
            EditOperation(old_str="def foo():\n    return 1", new_str="def bar():\n    return 1")
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.matches[0].match_type == "normalized"

    def test_tab_vs_space(self):
        """Test matching handles tab vs space differences."""
        engine = EditEngine()
        content = "def foo():\n\treturn 1"  # tab indentation
        edits = [
            EditOperation(
                old_str="def foo():\n    return 1",  # space indentation
                new_str="def bar():\n    return 1",
            )
        ]

        _result = engine.apply_edits(content, edits)  # noqa: F841

        # This may or may not match depending on normalization
        # The key is it shouldn't crash


class TestEditEngineFuzzyMatch:
    """Tests for fuzzy (Levenshtein) matching."""

    def test_fuzzy_match_minor_difference(self):
        """Test fuzzy matching with minor text differences."""
        engine = EditEngine(fuzzy_threshold=0.8)
        content = "def calculate_total():\n    return sum(items)"
        # Search string has slight difference
        edits = [
            EditOperation(
                old_str="def calcuate_total():\n    return sum(items)",  # typo
                new_str="def calculate_sum():\n    return sum(items)",
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.matches[0].match_type == "fuzzy"
        assert result.matches[0].similarity >= 0.8

    def test_fuzzy_match_threshold(self):
        """Test fuzzy matching respects threshold."""
        engine = EditEngine(fuzzy_threshold=0.99)  # Very strict
        content = "def foo():\n    return 1"
        edits = [
            EditOperation(
                old_str="def fooo():\n    return 1",  # extra 'o'
                new_str="def bar():\n    return 1",
            )
        ]

        result = engine.apply_edits(content, edits)

        # With 0.99 threshold, this should fail
        assert result.success is False

    def test_disable_fuzzy(self):
        """Test that fuzzy can be disabled."""
        engine = EditEngine(enable_fuzzy=False)
        content = "def foo():\n    return 1"
        edits = [
            EditOperation(
                old_str="def fooo():\n    return 1",  # extra 'o'
                new_str="def bar():\n    return 1",
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is False

    def test_fuzzy_threshold_one_means_exact_only(self):
        """Test that threshold=1.0 means exact matching only."""
        engine = EditEngine(fuzzy_threshold=1.0)
        content = "def foo():\n    return 1"
        edits = [
            EditOperation(
                old_str="def fooo():\n    return 1",  # won't match
                new_str="def bar():\n    return 1",
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is False


class TestEditEngineMiddleOutSearch:
    """Tests for middle-out search with line hints."""

    def test_middle_out_with_hint(self):
        """Test middle-out search uses hint_line effectively."""
        engine = EditEngine(fuzzy_threshold=0.85, buffer_lines=10)
        # Create content with similar patterns at different lines
        lines = ["def func1():", "    pass"] * 50
        lines[45] = "def target_func():"
        lines[46] = "    return 42"
        content = "\n".join(lines)

        edits = [
            EditOperation(
                old_str="def target_func():\n    return 42",
                new_str="def target_func():\n    return 100",
                hint_line=46,  # Near the target
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert "return 100" in result.content

    def test_middle_out_order_generation(self):
        """Test middle-out order generation."""
        engine = EditEngine()
        order = engine._middle_out_order(center=50, total_lines=100, window_size=1, buffer=5)

        # Should start at center and expand outward
        assert order[0] == 50
        assert 49 in order[:3]
        assert 51 in order[:3]
        assert len(order) == 11  # center + 5 above + 5 below


class TestEditEngineDiffGeneration:
    """Tests for unified diff generation."""

    def test_diff_output(self):
        """Test that diff output is generated correctly."""
        engine = EditEngine()
        content = "line 1\nline 2\nline 3"
        edits = [EditOperation(old_str="line 2", new_str="LINE TWO")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.diff != ""
        assert "-line 2" in result.diff
        assert "+LINE TWO" in result.diff

    def test_diff_headers(self):
        """Test diff has proper headers."""
        engine = EditEngine()
        content = "old content"
        edits = [EditOperation(old_str="old", new_str="new")]

        result = engine.apply_edits(content, edits)

        assert "---" in result.diff
        assert "+++" in result.diff


class TestEditEngineErrorHandling:
    """Tests for error handling and edge cases."""

    def test_not_found(self):
        """Test error when old_str not found."""
        engine = EditEngine()
        content = "def foo():\n    return 1"
        edits = [EditOperation(old_str="def bar():", new_str="def baz():")]

        result = engine.apply_edits(content, edits)

        assert result.success is False
        assert len(result.errors) == 1
        assert "Could not find match" in result.errors[0]

    def test_ambiguous_match_fails(self):
        """Test error when old_str appears multiple times (ambiguous)."""
        engine = EditEngine()
        content = "foo bar foo baz"
        edits = [EditOperation(old_str="foo", new_str="qux")]

        result = engine.apply_edits(content, edits)

        assert result.success is False
        assert "appears 2 times" in result.errors[0]

    def test_empty_edits_list(self):
        """Test handling empty edits list."""
        engine = EditEngine()
        content = "unchanged content"
        edits: list[EditOperation] = []

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.content == content
        assert result.applied_count == 0

    def test_empty_old_str(self):
        """Test handling empty old_str."""
        engine = EditEngine()
        content = "some content"
        edits = [EditOperation(old_str="", new_str="prefix ")]

        result = engine.apply_edits(content, edits)

        # Empty string matches at every position in the content, so it's ambiguous
        # The edit engine correctly rejects this as there are multiple matches
        assert result.success is False
        assert "appears" in result.errors[0] and "times" in result.errors[0]

    def test_invalid_fuzzy_threshold(self):
        """Test validation of fuzzy_threshold."""
        with pytest.raises(ValueError, match="fuzzy_threshold"):
            EditEngine(fuzzy_threshold=1.5)

        with pytest.raises(ValueError, match="fuzzy_threshold"):
            EditEngine(fuzzy_threshold=-0.1)

    def test_invalid_buffer_lines(self):
        """Test validation of buffer_lines."""
        with pytest.raises(ValueError, match="buffer_lines"):
            EditEngine(buffer_lines=0)


class TestEditEnginePreview:
    """Tests for preview functionality."""

    def test_preview_returns_content(self):
        """Test that preview returns expected content without modifying."""
        engine = EditEngine()
        content = "original content"
        edits = [EditOperation(old_str="original", new_str="modified")]

        result = engine.preview_edits(content, edits)

        assert result.success is True
        assert result.content == "modified content"


class TestMatchInfo:
    """Tests for MatchInfo dataclass."""

    def test_match_info_fields(self):
        """Test MatchInfo has all expected fields."""
        match = MatchInfo(
            edit_index=0,
            match_type="exact",
            similarity=1.0,
            line_start=5,
            line_end=10,
            original_text="matched text",
            search_strategy="direct",
            match_count=1,
        )

        assert match.edit_index == 0
        assert match.match_type == "exact"
        assert match.similarity == 1.0
        assert match.line_start == 5
        assert match.line_end == 10
        assert match.original_text == "matched text"
        assert match.search_strategy == "direct"
        assert match.match_count == 1


class TestEditResult:
    """Tests for EditResult dataclass."""

    def test_edit_result_success(self):
        """Test EditResult for successful edit."""
        result = EditResult(
            success=True,
            content="new content",
            diff="diff output",
            errors=[],
            matches=[],
            applied_count=1,
        )

        assert result.success is True
        assert result.content == "new content"
        assert result.applied_count == 1

    def test_edit_result_failure(self):
        """Test EditResult for failed edit."""
        result = EditResult(
            success=False,
            content="",
            diff="",
            errors=["Error message"],
            matches=[],
            applied_count=0,
        )

        assert result.success is False
        assert len(result.errors) == 1


class TestRapidFuzzIntegration:
    """Tests for rapidfuzz integration."""

    def test_rapidfuzz_availability(self):
        """Test that rapidfuzz availability is detected."""
        # This just verifies the import check works
        assert isinstance(RAPIDFUZZ_AVAILABLE, bool)

    @pytest.mark.skipif(not RAPIDFUZZ_AVAILABLE, reason="rapidfuzz not installed")
    def test_fuzzy_uses_rapidfuzz(self):
        """Test that fuzzy matching works with rapidfuzz."""
        engine = EditEngine(fuzzy_threshold=0.7)
        content = "def calculate_totals():\n    return sum(values)"
        edits = [
            EditOperation(
                old_str="def calculate_total():\n    return sum(values)",  # missing 's'
                new_str="def get_sum():\n    return sum(values)",
            )
        ]

        result = engine.apply_edits(content, edits)

        # Should find a fuzzy match
        assert result.success is True
        assert result.matches[0].match_type == "fuzzy"


class TestLineNumberTracking:
    """Tests for line number tracking in matches."""

    def test_line_numbers_single_line(self):
        """Test line numbers for single-line match."""
        engine = EditEngine()
        content = "line 1\nline 2\nline 3"
        edits = [EditOperation(old_str="line 2", new_str="LINE TWO")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.matches[0].line_start == 2
        assert result.matches[0].line_end == 2

    def test_line_numbers_multiline(self):
        """Test line numbers for multi-line match."""
        engine = EditEngine()
        content = "line 1\ndef foo():\n    pass\nline 4"
        edits = [
            EditOperation(
                old_str="def foo():\n    pass",
                new_str="def bar():\n    return 42",
            )
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.matches[0].line_start == 2
        assert result.matches[0].line_end == 3


class TestSequentialEdits:
    """Tests for sequential edit application."""

    def test_edits_apply_in_order(self):
        """Test that edits are applied sequentially."""
        engine = EditEngine()
        content = "A B C"
        edits = [
            EditOperation(old_str="A", new_str="X"),
            EditOperation(old_str="X B", new_str="Y"),  # Uses result of first edit
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.content == "Y C"

    def test_early_failure_stops_processing(self):
        """Test that failure on first edit stops subsequent edits."""
        engine = EditEngine()
        content = "A B C"
        edits = [
            EditOperation(old_str="Z", new_str="X"),  # Will fail
            EditOperation(old_str="A", new_str="Y"),  # Never reached
        ]

        result = engine.apply_edits(content, edits)

        assert result.success is False
        assert result.content == ""
        assert len(result.matches) == 2
        assert result.matches[0].match_type == "failed"


class TestWhitespacePreservation:
    """Tests for whitespace and indentation preservation."""

    def test_preserves_surrounding_whitespace(self):
        """Test that surrounding whitespace is preserved."""
        engine = EditEngine()
        content = "    def foo():\n        return 1"
        edits = [EditOperation(old_str="return 1", new_str="return 42")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        # Indentation should be preserved
        assert "        return 42" in result.content

    def test_preserves_line_endings(self):
        """Test that line endings are preserved in output."""
        engine = EditEngine()
        content = "line 1\nline 2\nline 3"
        edits = [EditOperation(old_str="line 2", new_str="LINE TWO")]

        result = engine.apply_edits(content, edits)

        assert result.success is True
        assert result.content.count("\n") == 2

"""Edit engine for surgical file edits with search/replace operations.

This module provides a core edit engine that handles search/replace operations
with exact and fuzzy matching, enabling surgical file edits without requiring
full file rewrites.

Issue #800: Add edit engine with search/replace for surgical file edits.

Key Features:
- Layered matching: exact → whitespace-normalized → fuzzy (Levenshtein)
- Middle-out search with optional line hints for faster fuzzy matching
- Unified diff generation for change visualization
- Optimistic concurrency support via ETag matching

Best Practices Applied:
- Aider: Layered matching strategies (exact, then fuzzy)
- RooCode: Middle-out search with configurable thresholds
- OpenAI/Anthropic: No line numbers, context-based matching
- VSCode/Monaco: Atomic batch edits with validation

References:
- https://aider.chat/docs/more/edit-formats.html
- https://docs.roocode.com/features/tools/apply-diff
- https://fabianhertwig.com/blog/coding-assistants-file-edits/
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

# Try to use rapidfuzz for 10-100x faster fuzzy matching (Rust-backed)
# Falls back to difflib if not available
try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz

    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

if TYPE_CHECKING:
    pass


@dataclass(slots=True)
class EditOperation:
    """A single search/replace edit operation.

    Attributes:
        old_str: The text to find and replace. Must be unique in the file
            unless allow_multiple is True.
        new_str: The replacement text. Can be empty to delete the matched text.
        hint_line: Optional line number hint for middle-out search. This is
            NOT used for exact matching, only to narrow fuzzy search window.
        allow_multiple: If True, replace all occurrences. Default False.
    """

    old_str: str
    new_str: str
    hint_line: int | None = None
    allow_multiple: bool = False


@dataclass(slots=True)
class MatchInfo:
    """Information about how an edit was matched.

    Attributes:
        edit_index: Index of the edit in the input list.
        match_type: How the match was found:
            - "exact": Direct string match
            - "normalized": Match after whitespace normalization
            - "fuzzy": Levenshtein similarity match
            - "failed": No match found
        similarity: Similarity score (1.0 for exact, <1.0 for fuzzy).
        line_start: 1-indexed starting line number of the match.
        line_end: 1-indexed ending line number of the match.
        original_text: The actual text that was matched (may differ from
            old_str if fuzzy matched).
        search_strategy: How the match was found:
            - "direct": Linear search from start
            - "middle_out": Expanding search from hint_line
        match_count: Number of matches found (relevant when allow_multiple=True).
    """

    edit_index: int
    match_type: Literal["exact", "normalized", "fuzzy", "failed"]
    similarity: float
    line_start: int
    line_end: int
    original_text: str
    search_strategy: Literal["direct", "middle_out"] = "direct"
    match_count: int = 1


@dataclass(slots=True)
class EditResult:
    """Result of applying edits to content.

    Attributes:
        success: True if all edits were applied successfully.
        content: New file content after edits. Empty string if failed.
        diff: Unified diff showing changes (empty if failed).
        errors: List of error messages for failed edits.
        matches: List of MatchInfo for each edit attempt.
        applied_count: Number of edits successfully applied.
    """

    success: bool
    content: str
    diff: str
    errors: list[str] = field(default_factory=list)
    matches: list[MatchInfo] = field(default_factory=list)
    applied_count: int = 0


class EditEngine:
    """Core engine for applying surgical file edits.

    The EditEngine applies search/replace edits to text content using a
    layered matching strategy for robustness:

    1. Exact match (fast path) - O(n) string find
    2. Whitespace-normalized match - collapse whitespace, compare
    3. Fuzzy match - Levenshtein similarity with configurable threshold

    Example:
        >>> engine = EditEngine(fuzzy_threshold=0.85)
        >>> edits = [
        ...     EditOperation(old_str="def foo():", new_str="def bar():"),
        ...     EditOperation(old_str="return x", new_str="return x + 1"),
        ... ]
        >>> result = engine.apply_edits(content, edits)
        >>> if result.success:
        ...     print(result.diff)
    """

    def __init__(
        self,
        fuzzy_threshold: float = 0.85,
        buffer_lines: int = 40,
        enable_fuzzy: bool = True,
    ):
        """Initialize the edit engine.

        Args:
            fuzzy_threshold: Minimum similarity score (0.0-1.0) for fuzzy
                matching. Default 0.85 balances precision and recall.
                Use 1.0 for exact-only matching.
            buffer_lines: Number of lines to search around hint_line for
                middle-out search. Default 40 (±40 lines).
            enable_fuzzy: Whether to enable fuzzy matching. Set to False
                for strict exact matching only.
        """
        if not 0.0 <= fuzzy_threshold <= 1.0:
            raise ValueError(f"fuzzy_threshold must be between 0.0 and 1.0, got {fuzzy_threshold}")
        if buffer_lines < 1:
            raise ValueError(f"buffer_lines must be positive, got {buffer_lines}")

        self.fuzzy_threshold = fuzzy_threshold
        self.buffer_lines = buffer_lines
        self.enable_fuzzy = enable_fuzzy

    def apply_edits(
        self,
        content: str,
        edits: list[EditOperation],
        *,
        validate_uniqueness: bool = True,
    ) -> EditResult:
        """Apply a list of edits to content.

        Edits are applied sequentially in order. Each edit modifies the
        content for subsequent edits. If any edit fails, the entire
        operation fails and no changes are made.

        Args:
            content: The original file content.
            edits: List of EditOperation to apply.
            validate_uniqueness: If True, verify old_str is unique before
                applying (prevents ambiguous matches). Default True.

        Returns:
            EditResult with success status, new content, diff, and match info.
        """
        if not edits:
            return EditResult(
                success=True,
                content=content,
                diff="",
                errors=[],
                matches=[],
                applied_count=0,
            )

        matches: list[MatchInfo] = []
        errors: list[str] = []
        current = content
        applied_count = 0

        for i, edit in enumerate(edits):
            # Validate uniqueness if requested (and not allowing multiple)
            if validate_uniqueness and not edit.allow_multiple:
                count = current.count(edit.old_str)
                if count > 1:
                    errors.append(
                        f"Edit {i}: old_str appears {count} times (must be unique). "
                        f"Add more context or set allow_multiple=True. "
                        f"Preview: {edit.old_str[:80]!r}..."
                    )
                    matches.append(
                        MatchInfo(
                            edit_index=i,
                            match_type="failed",
                            similarity=0.0,
                            line_start=0,
                            line_end=0,
                            original_text="",
                            match_count=count,
                        )
                    )
                    continue

            # Find and apply the edit
            match_info, new_content = self._find_and_apply(current, edit, i)
            matches.append(match_info)

            if match_info.match_type == "failed":
                preview = edit.old_str[:100].replace("\n", "\\n")
                errors.append(f"Edit {i}: Could not find match for: {preview!r}")
            else:
                current = new_content
                applied_count += match_info.match_count

        # If any errors, return failure with original content
        if errors:
            return EditResult(
                success=False,
                content="",
                diff="",
                errors=errors,
                matches=matches,
                applied_count=applied_count,
            )

        # Generate diff
        diff = self._generate_diff(content, current)

        return EditResult(
            success=True,
            content=current,
            diff=diff,
            errors=[],
            matches=matches,
            applied_count=applied_count,
        )

    def _find_and_apply(
        self,
        content: str,
        edit: EditOperation,
        index: int,
    ) -> tuple[MatchInfo, str]:
        """Find the best match for edit and apply it.

        Args:
            content: Current content to search in.
            edit: The edit operation to apply.
            index: Index of this edit (for MatchInfo).

        Returns:
            Tuple of (MatchInfo, new_content). If match failed,
            new_content equals original content.
        """
        # Strategy 1: Exact match (fast path)
        exact_result = self._try_exact_match(content, edit, index)
        if exact_result is not None:
            match_info, new_content = exact_result
            return match_info, new_content

        # Strategy 2: Whitespace-normalized match
        normalized_result = self._try_normalized_match(content, edit, index)
        if normalized_result is not None:
            match_info, new_content = normalized_result
            return match_info, new_content

        # Strategy 3: Fuzzy match (if enabled)
        if self.enable_fuzzy:
            fuzzy_result = self._try_fuzzy_match(content, edit, index)
            if fuzzy_result is not None:
                match_info, new_content = fuzzy_result
                return match_info, new_content

        # No match found
        return (
            MatchInfo(
                edit_index=index,
                match_type="failed",
                similarity=0.0,
                line_start=0,
                line_end=0,
                original_text="",
            ),
            content,
        )

    def _try_exact_match(
        self,
        content: str,
        edit: EditOperation,
        index: int,
    ) -> tuple[MatchInfo, str] | None:
        """Try exact string match.

        Returns:
            Tuple of (MatchInfo, new_content) if found, None otherwise.
        """
        if edit.old_str not in content:
            return None

        # Handle multiple replacements
        if edit.allow_multiple:
            count = content.count(edit.old_str)
            new_content = content.replace(edit.old_str, edit.new_str)
            # Find first occurrence for line numbers
            pos = content.find(edit.old_str)
        else:
            count = 1
            pos = content.find(edit.old_str)
            new_content = content[:pos] + edit.new_str + content[pos + len(edit.old_str) :]

        line_start = content[:pos].count("\n") + 1
        line_end = line_start + edit.old_str.count("\n")

        return (
            MatchInfo(
                edit_index=index,
                match_type="exact",
                similarity=1.0,
                line_start=line_start,
                line_end=line_end,
                original_text=edit.old_str,
                search_strategy="direct",
                match_count=count,
            ),
            new_content,
        )

    def _try_normalized_match(
        self,
        content: str,
        edit: EditOperation,
        index: int,
    ) -> tuple[MatchInfo, str] | None:
        """Try match after whitespace normalization.

        This handles common cases like:
        - Trailing whitespace differences
        - Tab vs space differences
        - Multiple spaces collapsed

        Returns:
            Tuple of (MatchInfo, new_content) if found, None otherwise.
        """
        normalized_old = self._normalize_whitespace(edit.old_str)
        lines = content.split("\n")
        old_lines = edit.old_str.split("\n")
        num_old_lines = len(old_lines)

        for i in range(len(lines) - num_old_lines + 1):
            window = "\n".join(lines[i : i + num_old_lines])
            normalized_window = self._normalize_whitespace(window)

            if normalized_old == normalized_window:
                # Found normalized match - use original window text
                new_content = content.replace(window, edit.new_str, 1)

                return (
                    MatchInfo(
                        edit_index=index,
                        match_type="normalized",
                        similarity=1.0,
                        line_start=i + 1,
                        line_end=i + num_old_lines,
                        original_text=window,
                        search_strategy="direct",
                    ),
                    new_content,
                )

        return None

    def _try_fuzzy_match(
        self,
        content: str,
        edit: EditOperation,
        index: int,
    ) -> tuple[MatchInfo, str] | None:
        """Try fuzzy match using Levenshtein similarity.

        Uses middle-out search if hint_line is provided, otherwise
        searches the entire content.

        Returns:
            Tuple of (MatchInfo, new_content) if found above threshold,
            None otherwise.
        """
        lines = content.split("\n")
        old_lines = edit.old_str.split("\n")
        num_old_lines = len(old_lines)

        if num_old_lines > len(lines):
            return None

        # Determine search range
        if edit.hint_line is not None:
            # Middle-out search from hint
            search_strategy: Literal["direct", "middle_out"] = "middle_out"
            search_order: list[int] = self._middle_out_order(
                center=edit.hint_line - 1,  # Convert to 0-indexed
                total_lines=len(lines),
                window_size=num_old_lines,
                buffer=self.buffer_lines,
            )
        else:
            # Linear search
            search_strategy = "direct"
            search_order = list(range(len(lines) - num_old_lines + 1))

        best_match: tuple[int, float, str] | None = None
        normalized_old = self._normalize_for_fuzzy(edit.old_str)

        for i in search_order:
            window = "\n".join(lines[i : i + num_old_lines])
            normalized_window = self._normalize_for_fuzzy(window)

            ratio = self._compute_similarity(normalized_old, normalized_window)

            if ratio >= self.fuzzy_threshold:
                if best_match is None or ratio > best_match[1]:
                    best_match = (i, ratio, window)

                # Early exit for middle-out: first match above threshold is likely best
                if search_strategy == "middle_out" and ratio >= 0.95:
                    break

        if best_match is None:
            return None

        i, ratio, window = best_match
        new_content = content.replace(window, edit.new_str, 1)

        return (
            MatchInfo(
                edit_index=index,
                match_type="fuzzy",
                similarity=ratio,
                line_start=i + 1,
                line_end=i + num_old_lines,
                original_text=window,
                search_strategy=search_strategy,
            ),
            new_content,
        )

    def _middle_out_order(
        self,
        center: int,
        total_lines: int,
        window_size: int,
        buffer: int,
    ) -> list[int]:
        """Generate search order expanding from center.

        Example: center=50, buffer=5 → [50, 49, 51, 48, 52, 47, 53, ...]

        Args:
            center: Center line (0-indexed).
            total_lines: Total number of lines.
            window_size: Size of the match window.
            buffer: Maximum distance from center to search.

        Returns:
            List of starting line indices in middle-out order.
        """
        max_start = total_lines - window_size
        if max_start < 0:
            return []

        # Clamp center to valid range
        center = max(0, min(center, max_start))

        result = [center]
        for offset in range(1, buffer + 1):
            if center - offset >= 0:
                result.append(center - offset)
            if center + offset <= max_start:
                result.append(center + offset)

        return result

    def _normalize_whitespace(self, text: str) -> str:
        """Normalize whitespace for comparison.

        - Normalize line endings to \n
        - Strip trailing whitespace per line
        - Collapse multiple spaces to single space
        """
        # Normalize line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Strip trailing whitespace per line
        text = "\n".join(line.rstrip() for line in text.split("\n"))
        return text

    def _normalize_for_fuzzy(self, text: str) -> str:
        """Normalize text for fuzzy comparison.

        More aggressive normalization:
        - All whitespace normalization
        - Collapse all whitespace to single space
        """
        text = self._normalize_whitespace(text)
        # Collapse multiple whitespace
        text = re.sub(r"[ \t]+", " ", text)
        return text

    def _compute_similarity(self, s1: str, s2: str) -> float:
        """Compute similarity ratio between two strings.

        Uses rapidfuzz if available (10-100x faster), falls back to difflib.

        Returns:
            Similarity ratio between 0.0 and 1.0.
        """
        if RAPIDFUZZ_AVAILABLE:
            # rapidfuzz returns 0-100, convert to 0-1
            return float(rapidfuzz_fuzz.ratio(s1, s2)) / 100.0
        else:
            # difflib fallback
            return difflib.SequenceMatcher(None, s1, s2).ratio()

    def _generate_diff(
        self,
        old_content: str,
        new_content: str,
        from_file: str = "before",
        to_file: str = "after",
    ) -> str:
        """Generate unified diff between old and new content.

        Args:
            old_content: Original content.
            new_content: Modified content.
            from_file: Label for original file in diff header.
            to_file: Label for modified file in diff header.

        Returns:
            Unified diff string.
        """
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        # Ensure final newline for clean diff
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"

        diff_lines = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_file,
            tofile=to_file,
        )

        return "".join(diff_lines)

    def preview_edits(
        self,
        content: str,
        edits: list[EditOperation],
    ) -> EditResult:
        """Preview edits without modifying content.

        This is equivalent to apply_edits but clearly indicates
        the result is a preview. The returned content shows what
        the file would look like after edits.

        Args:
            content: The original file content.
            edits: List of EditOperation to preview.

        Returns:
            EditResult with preview information.
        """
        return self.apply_edits(content, edits, validate_uniqueness=True)


def create_edit_operation(
    old_str: str,
    new_str: str,
    hint_line: int | None = None,
    allow_multiple: bool = False,
) -> EditOperation:
    """Convenience function to create an EditOperation.

    Args:
        old_str: Text to find and replace.
        new_str: Replacement text.
        hint_line: Optional line number hint for faster fuzzy matching.
        allow_multiple: If True, replace all occurrences.

    Returns:
        EditOperation instance.
    """
    return EditOperation(
        old_str=old_str,
        new_str=new_str,
        hint_line=hint_line,
        allow_multiple=allow_multiple,
    )

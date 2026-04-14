"""Unit tests for DocumentChunker — Issue #3719.

Covers:
    - Regression tests for existing strategies (FIXED, SEMANTIC, OVERLAPPING)
    - Comprehensive MARKDOWN_AWARE tests (10 cases)
    - build_heading_hierarchy() and _merge_small_segments() helpers
    - Property-based regression for SEMANTIC on .md files
"""

from __future__ import annotations

from nexus.bricks.search.chunking import (
    ChunkStrategy,
    DocumentChunker,
    _MdSegment,
    _merge_small_segments,
    build_heading_hierarchy,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_MD = """\
# Title

Intro paragraph with enough content to be meaningful for the search
indexing pipeline. This paragraph discusses the overall architecture
of the system and how various components interact with each other
to provide a seamless experience for end users. The system consists
of multiple microservices communicating via gRPC and REST APIs with
a central message broker handling asynchronous event processing
between components that need eventual consistency guarantees.

## Section A

Content of section A with some details about the topic. This section
covers authentication mechanisms including JWT tokens, OAuth2 flows,
and session management. The authentication layer sits between the
API gateway and the core business logic, intercepting every request
to validate credentials before forwarding to downstream services.
Token rotation is handled automatically by the refresh middleware
which monitors expiration timestamps and proactively refreshes tokens
before they expire to prevent disruption to long-running operations.

## Section B

Content of section B with different details about the database layer.
The persistence layer uses PostgreSQL for structured data and Redis
for caching hot paths. Connection pooling is managed centrally with
configurable min/max sizes to handle traffic spikes gracefully without
exhausting database resources under load. Migrations are managed via
Alembic with automatic rollback support and a pre-deployment validation
step that checks schema compatibility with the running application
version to prevent breaking changes from reaching production.
"""

DEEP_NESTING_MD = """\
# Top Level

Overview text that provides a comprehensive introduction to the system
architecture and its main components. This document is organized into
categories and subcategories for easier navigation and reference by
the engineering team working on the platform. The architecture follows
a modular design pattern where each component can be independently
deployed and scaled. Service discovery is handled by a central registry
that maintains health check information for all running instances.

## Category

Category description covering the major functional areas of the system.
Each category contains multiple subcategories with detailed implementation
notes and design decisions that inform the development process and help
new team members ramp up on the codebase quickly and effectively. This
section describes the authentication subsystem which handles user identity
verification, session management, and token lifecycle across distributed
services using a centralized identity provider.

### Subcategory

Subcategory details about a specific aspect of the category. This section
covers the implementation patterns used throughout the module, including
error handling strategies, retry policies, and circuit breaker configurations
that ensure the system remains resilient under adverse conditions. The
retry policy uses exponential backoff with jitter starting at 100ms and
capping at 30 seconds. Circuit breakers trip after five consecutive failures
and enter a half-open state after a configurable cooldown period.

#### Detail

Deep detail text about a very specific implementation concern. The token
validation pipeline processes each incoming request through a series of
middleware checks before allowing access to protected resources. Each
check is designed to fail fast and provide meaningful error messages
back to the client. The validation chain includes signature verification,
expiration checking, scope validation, and rate limit enforcement. All
validation results are cached in Redis for five minutes to reduce the
load on the identity provider during traffic spikes.
"""

FRONTMATTER_MD = """\
---
title: Architecture
tags: [auth, api]
---

Preamble text before any heading.

# Overview

System architecture document.

## Authentication

Auth uses JWT tokens.
"""

PREAMBLE_ONLY_MD = """\
This document has content before any heading.

It has multiple paragraphs of preamble text.

# First Section

Section content here.
"""

CODE_FENCE_WITH_HEADING_MD = """\
# Real Heading

Some prose content.

```python
# This is NOT a heading — it's a comment inside a code block
def verify_token(token: str) -> bool:
    return jwt.decode(token)
```

## Another Real Heading

More content.
"""

NO_HEADINGS_MD = """\
This document has no headings at all.

It's just paragraphs of plain text.

With some content that should still be chunked.
"""

TINY_SECTIONS_MD = """\
# Doc

Main document heading with sufficient introductory content so that
this section stands on its own as a meaningful chunk. The document
covers several small sub-topics organized into sections below, some
of which are intentionally tiny to test the merging behavior.

## A

Hi.

## B

Yo.

## C

This section has enough content to be meaningful on its own and
should not be merged with its neighbors because it exceeds the
minimum token threshold for standalone chunks in the system. It
discusses configuration management patterns and best practices
for deploying the application in containerized environments with
proper secrets handling and environment variable management across
multiple deployment stages from development to production.
"""

# A large section that will exceed default chunk_size
LARGE_SECTION_MD = "# Big Section\n\n" + "\n\n".join(
    f"Paragraph {i} with some filler content to make it reasonably sized." for i in range(200)
)


# ---------------------------------------------------------------------------
# Helper: make SectionInfo-like objects for build_heading_hierarchy tests
# ---------------------------------------------------------------------------


class _FakeSection:
    """Minimal stand-in for SectionInfo used by build_heading_hierarchy."""

    def __init__(self, heading: str, depth: int) -> None:
        self.heading = heading
        self.depth = depth


# ---------------------------------------------------------------------------
# Tests: build_heading_hierarchy
# ---------------------------------------------------------------------------


class TestBuildHeadingHierarchy:
    def test_single_section(self) -> None:
        sections = [_FakeSection("Title", 1)]
        assert build_heading_hierarchy(sections, 0, "doc.md") == "[doc.md > Title]"

    def test_nested_hierarchy(self) -> None:
        sections = [
            _FakeSection("Doc", 1),
            _FakeSection("Auth", 2),
            _FakeSection("JWT", 3),
        ]
        assert build_heading_hierarchy(sections, 2, "f.md") == "[f.md > Doc > Auth > JWT]"

    def test_sibling_sections(self) -> None:
        sections = [
            _FakeSection("Doc", 1),
            _FakeSection("Auth", 2),
            _FakeSection("Database", 2),
        ]
        assert build_heading_hierarchy(sections, 2, "f.md") == "[f.md > Doc > Database]"

    def test_deep_nesting(self) -> None:
        sections = [
            _FakeSection("L1", 1),
            _FakeSection("L2", 2),
            _FakeSection("L3", 3),
            _FakeSection("L4", 4),
            _FakeSection("L5", 5),
        ]
        result = build_heading_hierarchy(sections, 4, "d.md")
        assert result == "[d.md > L1 > L2 > L3 > L4 > L5]"

    def test_no_file_name(self) -> None:
        sections = [_FakeSection("Title", 1)]
        assert build_heading_hierarchy(sections, 0) == "[Title]"

    def test_out_of_bounds(self) -> None:
        sections = [_FakeSection("Title", 1)]
        assert build_heading_hierarchy(sections, 5, "f.md") == "[f.md]"

    def test_depth_skip(self) -> None:
        """H1 → H3 (skipping H2) should still build correct path."""
        sections = [
            _FakeSection("Top", 1),
            _FakeSection("Deep", 3),
        ]
        assert build_heading_hierarchy(sections, 1, "f.md") == "[f.md > Top > Deep]"


# ---------------------------------------------------------------------------
# Tests: _merge_small_segments
# ---------------------------------------------------------------------------


class TestMergeSmallSegments:
    def test_no_merge_needed(self) -> None:
        segs = [_MdSegment("text", 0, 100, "[p]", 200)]
        result = _merge_small_segments(segs, min_tokens=80, max_tokens=1024)
        assert len(result) == 1
        assert result[0].tokens == 200

    def test_merge_consecutive_tiny(self) -> None:
        segs = [
            _MdSegment("a", 0, 10, "[p1]", 20),
            _MdSegment("b", 10, 20, "[p2]", 30),
        ]
        result = _merge_small_segments(segs, min_tokens=80, max_tokens=1024)
        assert len(result) == 1
        assert result[0].tokens == 50
        assert result[0].heading_prefix == "[p1]"  # first segment's prefix

    def test_respects_max_tokens(self) -> None:
        segs = [
            _MdSegment("a", 0, 10, "[p]", 60),
            _MdSegment("b", 10, 20, "[p]", 60),
        ]
        result = _merge_small_segments(segs, min_tokens=80, max_tokens=100)
        # Can't merge (60+60=120 > 100), so each stays separate
        assert len(result) == 2

    def test_big_segment_emitted_directly(self) -> None:
        segs = [
            _MdSegment("small", 0, 5, "[p1]", 10),
            _MdSegment("big", 5, 100, "[p2]", 500),
            _MdSegment("small2", 100, 110, "[p3]", 10),
        ]
        result = _merge_small_segments(segs, min_tokens=80, max_tokens=1024)
        # "small" (10 tokens) is accumulated. "big" (500 tokens) is added to
        # accumulator (combined 510 <= 1024). 510 >= 80 so emit merged.
        # "small2" (10 tokens) accumulated and emitted at end.
        assert len(result) == 2
        assert result[0].tokens == 510  # small + big merged
        assert result[0].heading_prefix == "[p1]"  # first segment's prefix
        assert result[1].tokens == 10  # small2 alone

    def test_empty_input(self) -> None:
        assert _merge_small_segments([], min_tokens=80) == []


# ---------------------------------------------------------------------------
# Tests: FIXED strategy regression
# ---------------------------------------------------------------------------


class TestFixedStrategy:
    def test_small_document_single_chunk(self) -> None:
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.FIXED)
        chunks = chunker.chunk("Hello world.", compute_lines=False)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello world."

    def test_large_document_splits(self) -> None:
        content = " ".join(f"word{i}" for i in range(2000))
        chunker = DocumentChunker(chunk_size=256, strategy=ChunkStrategy.FIXED)
        chunks = chunker.chunk(content, compute_lines=False)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.text  # non-empty

    def test_offsets_are_monotonic(self) -> None:
        content = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunker = DocumentChunker(chunk_size=8, strategy=ChunkStrategy.FIXED)
        chunks = chunker.chunk(content, compute_lines=False)
        for i in range(1, len(chunks)):
            assert chunks[i].start_offset >= chunks[i - 1].start_offset


# ---------------------------------------------------------------------------
# Tests: SEMANTIC strategy regression (property-based)
# ---------------------------------------------------------------------------


class TestSemanticStrategyRegression:
    def test_markdown_produces_chunks(self) -> None:
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.SEMANTIC)
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md", compute_lines=True)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.text.strip()

    def test_offsets_monotonic(self) -> None:
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.SEMANTIC)
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md", compute_lines=False)
        for i in range(1, len(chunks)):
            assert chunks[i].start_offset >= chunks[i - 1].start_offset

    def test_line_numbers_present(self) -> None:
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.SEMANTIC)
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md", compute_lines=True)
        for chunk in chunks:
            assert chunk.line_start is not None
            assert chunk.line_end is not None
            assert chunk.line_start >= 1

    def test_code_file_dispatches_to_paragraphs(self) -> None:
        content = "def foo():\n    pass\n\ndef bar():\n    pass"
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.SEMANTIC)
        chunks = chunker.chunk(content, file_path="test.py", compute_lines=False)
        assert len(chunks) >= 1

    def test_no_heading_prefix_on_semantic(self) -> None:
        """SEMANTIC strategy should NOT produce heading_prefix."""
        chunker = DocumentChunker(chunk_size=1024, strategy=ChunkStrategy.SEMANTIC)
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md", compute_lines=False)
        for chunk in chunks:
            assert chunk.heading_prefix is None


# ---------------------------------------------------------------------------
# Tests: OVERLAPPING strategy regression
# ---------------------------------------------------------------------------


class TestOverlappingStrategyRegression:
    def test_produces_overlapping_chunks(self) -> None:
        content = " ".join(f"word{i}" for i in range(500))
        chunker = DocumentChunker(
            chunk_size=100,
            overlap_size=20,
            strategy=ChunkStrategy.OVERLAPPING,
        )
        chunks = chunker.chunk(content, compute_lines=False)
        assert len(chunks) > 1


# ---------------------------------------------------------------------------
# Tests: MARKDOWN_AWARE strategy (10 cases)
# ---------------------------------------------------------------------------


class TestMarkdownAwareStrategy:
    """Comprehensive tests for the MARKDOWN_AWARE chunking strategy."""

    def _make_chunker(self, chunk_size: int = 1024) -> DocumentChunker:
        return DocumentChunker(chunk_size=chunk_size, strategy=ChunkStrategy.MARKDOWN_AWARE)

    # 1. Happy path: multi-section doc with headings
    def test_happy_path_multi_section(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md")
        assert len(chunks) >= 2
        # All chunks should have heading_prefix set
        for chunk in chunks:
            assert chunk.heading_prefix is not None
            assert chunk.heading_prefix.startswith("[")
            assert chunk.heading_prefix.endswith("]")

    # 2. Heading hierarchy prefix format
    def test_heading_hierarchy_prefix(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(DEEP_NESTING_MD, file_path="deep.md")
        prefixes = [c.heading_prefix for c in chunks]
        # Should see nested paths
        assert any(">" in p for p in prefixes if p)
        # The deepest section should have full hierarchy
        deep_chunks = [c for c in chunks if c.heading_prefix and "Detail" in c.heading_prefix]
        assert len(deep_chunks) >= 1
        assert "Subcategory" in deep_chunks[0].heading_prefix
        assert "Category" in deep_chunks[0].heading_prefix

    # 3. Pre-heading content (preamble)
    def test_preamble_content_captured(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(PREAMBLE_ONLY_MD, file_path="pre.md")
        # First chunk should contain the preamble text
        preamble_chunks = [c for c in chunks if "before any heading" in c.text]
        assert len(preamble_chunks) >= 1
        assert preamble_chunks[0].heading_prefix == "[pre.md]"

    # 4. Frontmatter chunk
    def test_frontmatter_as_chunk(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(FRONTMATTER_MD, file_path="arch.md")
        fm_chunks = [c for c in chunks if "frontmatter" in (c.heading_prefix or "")]
        assert len(fm_chunks) == 1
        assert "title" in fm_chunks[0].text.lower() or "---" in fm_chunks[0].text

    # 5. Oversized section triggers sub-splitting
    def test_oversized_section_splits(self) -> None:
        chunker = self._make_chunker(chunk_size=256)
        chunks = chunker.chunk(LARGE_SECTION_MD, file_path="big.md")
        # Should produce multiple chunks from the single large section
        assert len(chunks) > 1
        # All chunks should share the same heading prefix
        prefixes = {c.heading_prefix for c in chunks}
        assert len(prefixes) == 1
        assert "Big Section" in prefixes.pop()

    # 6. Atomic block within budget stays intact
    def test_small_code_block_stays_intact(self) -> None:
        md = "# Code\n\n```python\ndef foo():\n    return 42\n```\n"
        chunker = self._make_chunker(chunk_size=1024)
        chunks = chunker.chunk(md, file_path="code.md")
        # The code block should be within one chunk
        code_chunks = [c for c in chunks if "def foo" in c.text]
        assert len(code_chunks) == 1

    # 7. Tiny sections merge
    def test_tiny_sections_merge(self) -> None:
        chunker = self._make_chunker(chunk_size=1024)
        chunks = chunker.chunk(TINY_SECTIONS_MD, file_path="tiny.md")
        # Sections A ("Hi.") and B ("Yo.") are tiny and should be merged
        # Section C is large enough to stand alone
        # So we expect fewer chunks than sections
        section_count = TINY_SECTIONS_MD.count("\n## ")
        assert len(chunks) < section_count + 1  # +1 for the H1

    # 8. No headings → fallback to _chunk_fixed
    def test_no_headings_fallback(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(NO_HEADINGS_MD, file_path="plain.md")
        assert len(chunks) >= 1
        # Fallback produces chunks without heading_prefix
        for chunk in chunks:
            assert chunk.heading_prefix is None

    # 9. Heading inside code fence not treated as section boundary
    def test_heading_in_code_fence_ignored(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(CODE_FENCE_WITH_HEADING_MD, file_path="fence.md")
        # The comment "# This is NOT a heading" should be inside a chunk,
        # not creating its own section
        headings_in_prefixes = [
            c.heading_prefix
            for c in chunks
            if c.heading_prefix and "NOT a heading" in c.heading_prefix
        ]
        assert len(headings_in_prefixes) == 0

    # 10. Non-markdown file delegates to semantic strategy
    def test_non_markdown_delegates(self) -> None:
        chunker = self._make_chunker()
        content = "def foo():\n    pass\n\ndef bar():\n    pass"
        chunks = chunker.chunk(content, file_path="test.py")
        assert len(chunks) >= 1
        # No heading prefix on non-markdown
        for chunk in chunks:
            assert chunk.heading_prefix is None

    # ── Additional properties ──

    def test_chunk_offsets_monotonic(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md")
        for i in range(1, len(chunks)):
            assert chunks[i].start_offset >= chunks[i - 1].start_offset

    def test_line_numbers_computed(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md", compute_lines=True)
        for chunk in chunks:
            assert chunk.line_start is not None
            assert chunk.line_end is not None

    def test_chunk_indices_sequential(self) -> None:
        chunker = self._make_chunker()
        chunks = chunker.chunk(SIMPLE_MD, file_path="test.md")
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_frontmatter_plus_preamble(self) -> None:
        """Document with frontmatter AND preamble before first heading."""
        chunker = self._make_chunker()
        chunks = chunker.chunk(FRONTMATTER_MD, file_path="doc.md")
        # Should have: frontmatter chunk, preamble chunk, section chunks
        fm_chunks = [c for c in chunks if "frontmatter" in (c.heading_prefix or "")]
        preamble_chunks = [c for c in chunks if "Preamble text" in c.text]
        assert len(fm_chunks) == 1
        assert len(preamble_chunks) >= 1

    # ── Adversarial review regressions (Codex round 1) ──

    def test_frontmatter_does_not_absorb_heading_sections(self) -> None:
        """Short doc: frontmatter must not merge with heading sections."""
        md = "---\ntitle: Test\n---\n\n# Overview\n\nShort overview text.\n"
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="short.md")
        # The heading section must NOT have a frontmatter prefix
        heading_chunks = [
            c for c in chunks if "# Overview" in c.text or "overview" in c.text.lower()
        ]
        for c in heading_chunks:
            assert "frontmatter" not in (c.heading_prefix or ""), (
                f"Heading section wrongly embedded as frontmatter: {c.heading_prefix}"
            )

    def test_oversized_section_preserves_code_fence(self) -> None:
        """Code block within 1.5x budget must not be split mid-fence."""
        # Build a code block of ~100 tokens (within 1.5x of chunk_size=128 = 192)
        code = "\n".join(f"    line_{i} = {i}" for i in range(20))
        md = (
            "# Code\n\nSome introductory prose about the code example.\n\n"
            f"```python\n{code}\n```\n\nTrailing prose after the block.\n"
        )
        chunker = self._make_chunker(chunk_size=128)
        chunks = chunker.chunk(md, file_path="code.md")
        # The code block should be kept intact in a single chunk
        fence_chunks = [c for c in chunks if "```python" in c.text]
        assert len(fence_chunks) >= 1
        for c in fence_chunks:
            assert "```" in c.text[c.text.index("```python") + 10 :], (
                "Code fence was split across chunks — opening ``` without closing ```"
            )

    def test_setext_headings_detected(self) -> None:
        """Setext headings (=== / ---) must be recognized as section boundaries."""
        md = (
            "Title\n=====\n\nIntro content with enough words to exceed the "
            "minimum token threshold for a standalone chunk in tests.\n\n"
            "Subtitle\n--------\n\nMore content with enough text to also "
            "exceed the minimum threshold for standalone chunks.\n"
        )
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="setext.md")
        # Verify setext headings were detected (reflected in heading_prefix)
        prefixes = [c.heading_prefix or "" for c in chunks]
        assert any("Title" in p for p in prefixes), f"Setext H1 (===) not detected: {prefixes}"
        # At least one chunk should exist with correct prefix
        assert len(chunks) >= 1

    def test_indented_atx_heading_detected(self) -> None:
        """ATX headings with up to 3 spaces of indentation are valid CommonMark."""
        md = (
            "   # Indented Heading\n\nContent under the indented heading "
            "with enough words to be meaningful for search indexing.\n\n"
            "## Normal Heading\n\nMore content under a normally formatted "
            "heading that also has enough text for search.\n"
        )
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="indent.md")
        prefixes = [c.heading_prefix or "" for c in chunks]
        assert any("Indented Heading" in p for p in prefixes), (
            f"Indented ATX heading not detected: {prefixes}"
        )

    # ── Adversarial review regressions (Codex round 2) ──

    def test_heading_after_code_fence_detected(self) -> None:
        """Headings after fenced code blocks must still be detected."""
        from nexus.bricks.search.chunking import _parse_headings_fence_aware

        md = (
            "# First\n\nSome prose.\n\n```python\ndef foo():\n    pass\n```\n\n"
            "## Second\n\nMore prose after the code block.\n"
        )
        # Verify at the parser level that both headings survive
        headings = _parse_headings_fence_aware(md)
        heading_names = [h.heading for h in headings]
        assert "First" in heading_names, f"H1 missing: {heading_names}"
        assert "Second" in heading_names, f"H2 after fence missing: {heading_names}"

        # Also verify chunker produces chunks containing "## Second" text
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="fenced.md")
        all_text = " ".join(c.text for c in chunks)
        assert "## Second" in all_text, "Second heading lost during chunking"

    def test_frontmatter_not_parsed_as_setext_heading(self) -> None:
        """YAML frontmatter keys must not become setext headings."""
        md = "---\ntitle: Architecture\ntags: [auth, api]\n---\n\n# Overview\n\nContent.\n"
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="arch.md")
        prefixes = [c.heading_prefix or "" for c in chunks]
        # No prefix should contain YAML keys like "tags"
        for p in prefixes:
            assert "tags" not in p, f"YAML key parsed as setext heading: {p}"

    # ── Adversarial review regressions (Codex round 3) ──

    def test_nested_backtick_fences_do_not_suppress_headings(self) -> None:
        """4-backtick fence containing literal 3-backtick lines."""
        from nexus.bricks.search.chunking import _parse_headings_fence_aware

        md = (
            "## Before\n\nProse.\n\n"
            "````\n```\ninner literal content\n```\n````\n\n"
            "## After\n\nMore prose.\n"
        )
        headings = _parse_headings_fence_aware(md)
        names = [h.heading for h in headings]
        assert "Before" in names
        assert "After" in names, f"Heading after nested fence lost: {names}"

    def test_heading_prefix_truncated_for_long_paths(self) -> None:
        """Very long heading hierarchy is truncated to stay under limit."""
        long_heading = "A" * 100
        sections = [
            _FakeSection(long_heading, 1),
            _FakeSection(long_heading, 2),
            _FakeSection(long_heading, 3),
        ]
        result = build_heading_hierarchy(sections, 2, "file.md")
        assert len(result) <= 250  # reasonable cap
        assert "..." in result, f"Expected truncation ellipsis: {result}"

    # ── Adversarial review regressions (Codex round 4) ──

    def test_thematic_break_not_frontmatter(self) -> None:
        """Document starting with --- thematic break must not be treated as frontmatter."""
        md = "---\n\n# Title\n\nContent after thematic break.\n"
        chunker = self._make_chunker()
        chunks = chunker.chunk(md, file_path="hr.md")
        # No chunk should have a frontmatter prefix
        for c in chunks:
            assert "frontmatter" not in (c.heading_prefix or ""), (
                f"Thematic break wrongly treated as frontmatter: {c.heading_prefix}"
            )

"""Tests for markdown structure parser — Issue #3718.

Covers:
    - Parser correctness (parametrized, ~20 cases)
    - Section lookup and block filtering
    - Serialization round-trip
    - Edge cases and failure modes
"""

from __future__ import annotations

import json

import pytest

from nexus.bricks.parsers.md_structure import (
    SCHEMA_VERSION,
    MarkdownStructureIndex,
    filter_blocks,
    find_section,
    parse_markdown_structure,
    slice_content,
)

# ---------------------------------------------------------------------------
# Fixtures: reusable markdown documents
# ---------------------------------------------------------------------------

SIMPLE_DOC = b"""\
# Title

Intro paragraph.

## Section A

Content of section A.

## Section B

Content of section B.
"""

FULL_DOC = b"""\
---
title: Architecture
tags: [auth, api]
---

# Overview

System architecture document.

## Authentication

Auth uses JWT tokens.

```python
def verify_token(token: str) -> bool:
    return jwt.decode(token)
```

### OAuth Flow

The OAuth flow is standard.

## API Design

| Method | Path       |
|--------|------------|
| GET    | /api/users |
| POST   | /api/users |

## Conclusion

Final thoughts.
"""

CJK_DOC = """\
# 日本語タイトル

導入テキスト。

## セクションA

コンテンツA。

## セクションB

コンテンツB。
""".encode()


# ---------------------------------------------------------------------------
# Parser correctness — parametrized
# ---------------------------------------------------------------------------


class TestParserCorrectness:
    """Parametrized tests for parser edge cases."""

    def test_simple_headings(self) -> None:
        idx = parse_markdown_structure(SIMPLE_DOC)
        assert len(idx.sections) == 3
        assert idx.sections[0].heading == "Title"
        assert idx.sections[0].depth == 1
        assert idx.sections[1].heading == "Section A"
        assert idx.sections[1].depth == 2
        assert idx.sections[2].heading == "Section B"

    def test_frontmatter_parsed(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        assert idx.frontmatter is not None
        assert "title" in idx.frontmatter.keys
        assert "tags" in idx.frontmatter.keys

    def test_code_block_inside_section(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        auth = find_section(idx, "Authentication")
        assert auth is not None
        code_blocks = filter_blocks(auth, "code")
        assert len(code_blocks) == 1
        assert code_blocks[0].language == "python"

    def test_table_inside_section(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        api = find_section(idx, "API Design")
        assert api is not None
        tables = filter_blocks(api, "table")
        assert len(tables) == 1
        assert tables[0].rows == 2

    def test_heading_inside_code_fence_ignored(self) -> None:
        """Headings inside code fences must NOT be indexed."""
        doc = b"""\
## Real Heading

```markdown
# This Is Not A Heading
## Neither Is This
```

## Another Real Heading
"""
        idx = parse_markdown_structure(doc)
        headings = [s.heading for s in idx.sections]
        assert "This Is Not A Heading" not in headings
        assert "Neither Is This" not in headings
        assert "Real Heading" in headings
        assert "Another Real Heading" in headings
        assert len(idx.sections) == 2

    def test_setext_headings(self) -> None:
        """Setext-style headings (=== / ---) must be indexed."""
        doc = b"""\
Setext H1
=========

Some content.

Setext H2
---------

More content.
"""
        idx = parse_markdown_structure(doc)
        assert len(idx.sections) == 2
        assert idx.sections[0].heading == "Setext H1"
        assert idx.sections[0].depth == 1
        assert idx.sections[1].heading == "Setext H2"
        assert idx.sections[1].depth == 2

    def test_utf8_byte_offsets(self) -> None:
        """Byte offsets must be correct for multi-byte characters."""
        idx = parse_markdown_structure(CJK_DOC)
        assert len(idx.sections) == 3
        for sec in idx.sections:
            content = slice_content(CJK_DOC, sec.byte_start, sec.byte_end)
            assert sec.heading in content

    def test_empty_document(self) -> None:
        idx = parse_markdown_structure(b"")
        assert len(idx.sections) == 0
        assert idx.frontmatter is None

    def test_no_headings(self) -> None:
        idx = parse_markdown_structure(b"Just plain text.\nNo headings.\n")
        assert len(idx.sections) == 0

    def test_frontmatter_only(self) -> None:
        doc = b"---\ntitle: Test\n---\n"
        idx = parse_markdown_structure(doc)
        assert idx.frontmatter is not None
        assert idx.frontmatter.keys == ["title"]
        assert len(idx.sections) == 0

    def test_empty_section_between_headings(self) -> None:
        """Adjacent headings with no content between them."""
        doc = b"## A\n## B\n## C\n"
        idx = parse_markdown_structure(doc)
        assert len(idx.sections) == 3
        # All sections should have valid byte ranges (non-negative size)
        for sec in idx.sections:
            assert sec.byte_end >= sec.byte_start

    def test_deeply_nested_headings(self) -> None:
        doc = b"# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n"
        idx = parse_markdown_structure(doc)
        assert len(idx.sections) == 6
        for i, sec in enumerate(idx.sections):
            assert sec.depth == i + 1

    def test_code_fence_with_no_language(self) -> None:
        doc = b"## Section\n\n```\nplain code\n```\n"
        idx = parse_markdown_structure(doc)
        blocks = filter_blocks(idx.sections[0], "code")
        assert len(blocks) == 1
        assert blocks[0].language is None

    def test_code_fence_with_language(self) -> None:
        doc = b"## Section\n\n```rust\nfn main() {}\n```\n"
        idx = parse_markdown_structure(doc)
        blocks = filter_blocks(idx.sections[0], "code")
        assert blocks[0].language == "rust"

    def test_multiple_code_blocks_in_section(self) -> None:
        doc = b"## Section\n\n```python\na = 1\n```\n\nText.\n\n```js\nlet b = 2;\n```\n"
        idx = parse_markdown_structure(doc)
        blocks = filter_blocks(idx.sections[0], "code")
        assert len(blocks) == 2
        assert blocks[0].language == "python"
        assert blocks[1].language == "js"

    def test_nested_code_fences(self) -> None:
        """Nested fences (``````` inside ```) must be handled correctly."""
        doc = b"## Section\n\n````\n```\nnot a heading: # H1\n```\n````\n"
        idx = parse_markdown_structure(doc)
        assert len(idx.sections) == 1
        assert idx.sections[0].heading == "Section"

    def test_section_boundary_respects_depth(self) -> None:
        """H2 section ends at next H2, not at nested H3."""
        doc = b"## A\nContent A.\n### A.1\nNested.\n## B\nContent B.\n"
        idx = parse_markdown_structure(doc)
        sec_a = find_section(idx, "A")
        assert sec_a is not None
        content = slice_content(doc, sec_a.byte_start, sec_a.byte_end)
        assert "Content A" in content
        assert "Nested" in content  # H3 is inside H2 "A"
        assert "Content B" not in content  # H2 "B" is a sibling

    def test_content_hash_stored(self) -> None:
        idx = parse_markdown_structure(SIMPLE_DOC, content_hash="abc123")
        assert idx.content_hash == "abc123"

    def test_tokens_est(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        for sec in idx.sections:
            expected = (sec.byte_end - sec.byte_start) // 4
            assert sec.tokens_est == expected

    def test_schema_version(self) -> None:
        idx = parse_markdown_structure(SIMPLE_DOC)
        assert idx.version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Section lookup
# ---------------------------------------------------------------------------


class TestSectionLookup:
    def test_exact_match(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        sec = find_section(idx, "Authentication")
        assert sec is not None
        assert sec.heading == "Authentication"

    def test_case_insensitive(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        sec = find_section(idx, "authentication")
        assert sec is not None
        assert sec.heading == "Authentication"

    def test_substring_match(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        sec = find_section(idx, "Auth")
        assert sec is not None
        assert sec.heading == "Authentication"

    def test_exact_match_preferred_over_substring(self) -> None:
        doc = b"## Auth\nShort.\n## Authentication\nFull.\n"
        idx = parse_markdown_structure(doc)
        sec = find_section(idx, "Auth")
        assert sec is not None
        assert sec.heading == "Auth"

    def test_not_found(self) -> None:
        idx = parse_markdown_structure(SIMPLE_DOC)
        assert find_section(idx, "Nonexistent") is None

    def test_cjk_lookup(self) -> None:
        idx = parse_markdown_structure(CJK_DOC)
        sec = find_section(idx, "セクションA")
        assert sec is not None
        assert sec.heading == "セクションA"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_roundtrip(self) -> None:
        idx = parse_markdown_structure(FULL_DOC, content_hash="hash123")
        d = idx.to_dict()
        json_str = json.dumps(d)
        idx2 = MarkdownStructureIndex.from_dict(json.loads(json_str))

        assert idx2.version == idx.version
        assert idx2.content_hash == idx.content_hash
        assert idx2.tokens_est_method == idx.tokens_est_method
        assert len(idx2.sections) == len(idx.sections)
        for s1, s2 in zip(idx.sections, idx2.sections):
            assert s1.heading == s2.heading
            assert s1.depth == s2.depth
            assert s1.byte_start == s2.byte_start
            assert s1.byte_end == s2.byte_end
            assert len(s1.blocks) == len(s2.blocks)

    def test_frontmatter_roundtrip(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        d = idx.to_dict()
        idx2 = MarkdownStructureIndex.from_dict(d)
        assert idx2.frontmatter is not None
        assert idx2.frontmatter.keys == idx.frontmatter.keys

    def test_empty_doc_roundtrip(self) -> None:
        idx = parse_markdown_structure(b"")
        d = idx.to_dict()
        idx2 = MarkdownStructureIndex.from_dict(d)
        assert len(idx2.sections) == 0
        assert idx2.frontmatter is None


# ---------------------------------------------------------------------------
# Byte-range slicing round-trip
# ---------------------------------------------------------------------------


class TestByteRangeRoundTrip:
    """Verify that byte offsets produce correct content when sliced."""

    @pytest.mark.parametrize(
        "doc",
        [SIMPLE_DOC, FULL_DOC, CJK_DOC],
        ids=["simple", "full", "cjk"],
    )
    def test_all_sections_slice_correctly(self, doc: bytes) -> None:
        idx = parse_markdown_structure(doc)
        for sec in idx.sections:
            content = slice_content(doc, sec.byte_start, sec.byte_end)
            # Section content should contain its heading
            assert sec.heading in content, (
                f"Section '{sec.heading}' heading not found in sliced content"
            )

    def test_code_block_slice(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        auth = find_section(idx, "Authentication")
        assert auth is not None
        for block in auth.blocks:
            if block.type == "code":
                content = slice_content(FULL_DOC, block.byte_start, block.byte_end)
                assert "verify_token" in content

    def test_table_slice(self) -> None:
        idx = parse_markdown_structure(FULL_DOC)
        api = find_section(idx, "API Design")
        assert api is not None
        for block in api.blocks:
            if block.type == "table":
                content = slice_content(FULL_DOC, block.byte_start, block.byte_end)
                assert "/api/users" in content


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_binary_content_no_crash(self) -> None:
        """Binary content with .md extension should not crash."""
        binary = bytes(range(256)) * 10
        idx = parse_markdown_structure(binary)
        # May produce garbage sections, but must not raise
        assert isinstance(idx, MarkdownStructureIndex)

    def test_malformed_frontmatter(self) -> None:
        """Malformed YAML in frontmatter should not crash."""
        doc = b"---\n: invalid yaml [[\n---\n# Heading\nContent.\n"
        idx = parse_markdown_structure(doc)
        # Parser should handle gracefully
        assert isinstance(idx, MarkdownStructureIndex)
        # Heading should still be indexed
        assert len(idx.sections) >= 1

    def test_very_large_heading_count(self) -> None:
        """Many headings should not cause performance issues."""
        lines = [f"## Heading {i}\n\nContent {i}.\n" for i in range(200)]
        doc = "\n".join(lines).encode("utf-8")
        idx = parse_markdown_structure(doc)
        assert len(idx.sections) == 200

    def test_from_dict_missing_fields(self) -> None:
        """Deserialization should handle missing optional fields."""
        data = {
            "sections": [
                {
                    "heading": "H",
                    "depth": 1,
                    "byte_start": 0,
                    "byte_end": 10,
                    "line_start": 0,
                    "line_end": 1,
                }
            ]
        }
        idx = MarkdownStructureIndex.from_dict(data)
        assert len(idx.sections) == 1
        assert idx.sections[0].tokens_est == 0
        assert idx.frontmatter is None

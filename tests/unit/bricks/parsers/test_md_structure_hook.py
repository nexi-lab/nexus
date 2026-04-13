"""Integration tests for MarkdownStructureWriteHook — Issue #3718.

Tests the full write-hook → metastore → read round trip using a
real DictMetastore (in-memory, no DB).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus.bricks.parsers.md_structure_hook import (
    MD_STRUCTURE_KEY,
    MarkdownStructureWriteHook,
)
from nexus.contracts.vfs_hooks import WriteHookContext

# ---------------------------------------------------------------------------
# Lightweight in-memory metastore stub
# ---------------------------------------------------------------------------


class _StubMetastore:
    """Minimal metastore stub for testing (mirrors DictMetastore API)."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if path not in self._data:
            self._data[path] = {}
        self._data[path][key] = value

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._data.get(path, {}).get(key)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MD = b"""\
---
title: Architecture
---

# Overview

System overview text.

## Authentication

```python
def verify(token):
    return True
```

## API

| Method | Path  |
|--------|-------|
| GET    | /api  |
"""


@pytest.fixture
def meta() -> _StubMetastore:
    return _StubMetastore()


@pytest.fixture
def hook(meta: _StubMetastore) -> MarkdownStructureWriteHook:
    return MarkdownStructureWriteHook(metadata=meta)


def _make_write_ctx(path: str, content: bytes, content_hash: str = "hash1") -> WriteHookContext:
    return WriteHookContext(
        path=path,
        content=content,
        context=None,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# Write hook tests
# ---------------------------------------------------------------------------


class TestWriteHook:
    def test_indexes_md_file(self, hook: MarkdownStructureWriteHook, meta: _StubMetastore) -> None:
        ctx = _make_write_ctx("/docs/arch.md", SAMPLE_MD)
        hook.on_post_write(ctx)

        raw = meta.get_file_metadata("/docs/arch.md", MD_STRUCTURE_KEY)
        assert raw is not None
        data = json.loads(raw)
        assert data["version"] == 1
        assert len(data["sections"]) >= 3
        assert data["content_hash"] == "hash1"

    def test_skips_non_md_files(
        self, hook: MarkdownStructureWriteHook, meta: _StubMetastore
    ) -> None:
        ctx = _make_write_ctx("/data/file.txt", b"# Heading\nContent")
        hook.on_post_write(ctx)
        assert meta.get_file_metadata("/data/file.txt", MD_STRUCTURE_KEY) is None

    def test_updates_on_rewrite(
        self, hook: MarkdownStructureWriteHook, meta: _StubMetastore
    ) -> None:
        ctx1 = _make_write_ctx("/doc.md", b"# V1\nFirst version.\n", content_hash="v1")
        hook.on_post_write(ctx1)

        ctx2 = _make_write_ctx("/doc.md", b"# V2\nSecond version.\n## New\n", content_hash="v2")
        hook.on_post_write(ctx2)

        data = json.loads(meta.get_file_metadata("/doc.md", MD_STRUCTURE_KEY))
        assert data["content_hash"] == "v2"
        headings = [s["heading"] for s in data["sections"]]
        assert "V2" in headings
        assert "New" in headings

    def test_handles_empty_content(
        self, hook: MarkdownStructureWriteHook, meta: _StubMetastore
    ) -> None:
        ctx = _make_write_ctx("/empty.md", b"")
        hook.on_post_write(ctx)
        # Empty content is skipped to prevent poisoning the cache
        raw = meta.get_file_metadata("/empty.md", MD_STRUCTURE_KEY)
        assert raw is None

    def test_no_metadata_no_crash(self) -> None:
        """Hook with no metastore should silently skip."""
        hook = MarkdownStructureWriteHook(metadata=None)
        ctx = _make_write_ctx("/doc.md", SAMPLE_MD)
        hook.on_post_write(ctx)  # should not raise

    def test_hook_spec(self, hook: MarkdownStructureWriteHook) -> None:
        spec = hook.hook_spec()
        assert len(spec.write_hooks) == 1
        assert spec.write_hooks[0] is hook

    def test_hook_name(self, hook: MarkdownStructureWriteHook) -> None:
        assert hook.name == "md_structure"


# ---------------------------------------------------------------------------
# Read path tests
# ---------------------------------------------------------------------------


class TestReadPath:
    def test_get_index_after_write(self, hook: MarkdownStructureWriteHook) -> None:
        ctx = _make_write_ctx("/doc.md", SAMPLE_MD, content_hash="h1")
        hook.on_post_write(ctx)

        index = hook.get_index("/doc.md", current_hash="h1")
        assert index is not None
        assert len(index.sections) >= 3

    def test_stale_index_triggers_reparse(self, hook: MarkdownStructureWriteHook) -> None:
        """If content_hash doesn't match, re-parse with provided content."""
        ctx = _make_write_ctx("/doc.md", b"# Old\nOld content.\n", content_hash="old_hash")
        hook.on_post_write(ctx)

        new_content = b"# New\nNew content.\n## Added\n"
        index = hook.get_index("/doc.md", current_content=new_content, current_hash="new_hash")
        assert index is not None
        assert index.content_hash == "new_hash"
        headings = [s.heading for s in index.sections]
        assert "New" in headings
        assert "Added" in headings

    def test_stale_index_without_content_returns_stale(
        self, hook: MarkdownStructureWriteHook
    ) -> None:
        """If hash mismatches but no content provided, return stale index."""
        ctx = _make_write_ctx("/doc.md", b"# Old\n", content_hash="old")
        hook.on_post_write(ctx)

        index = hook.get_index("/doc.md", current_hash="new")
        assert index is not None  # Returns stale index rather than None
        assert index.content_hash == "old"

    def test_no_index_with_content_parses_on_demand(self, hook: MarkdownStructureWriteHook) -> None:
        """No stored index but content provided → parse on demand."""
        content = b"# OnDemand\nParsed on the fly.\n"
        index = hook.get_index("/new.md", current_content=content, current_hash="fresh")
        assert index is not None
        assert index.sections[0].heading == "OnDemand"

    def test_no_index_no_content_returns_none(self, hook: MarkdownStructureWriteHook) -> None:
        assert hook.get_index("/nonexistent.md") is None

    def test_corrupt_index_discarded(
        self, hook: MarkdownStructureWriteHook, meta: _StubMetastore
    ) -> None:
        """Corrupt JSON in metadata should not crash."""
        meta.set_file_metadata("/doc.md", MD_STRUCTURE_KEY, "not valid json{{{")
        index = hook.get_index("/doc.md")
        assert index is None


# ---------------------------------------------------------------------------
# Section read tests
# ---------------------------------------------------------------------------


class TestSectionRead:
    def _setup(self, hook: MarkdownStructureWriteHook) -> None:
        ctx = _make_write_ctx("/doc.md", SAMPLE_MD, content_hash="h1")
        hook.on_post_write(ctx)

    def test_read_section(self, hook: MarkdownStructureWriteHook) -> None:
        self._setup(hook)
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "Authentication")
        assert result is not None
        assert "verify" in result
        assert "## API" not in result

    def test_read_section_with_block_type(self, hook: MarkdownStructureWriteHook) -> None:
        self._setup(hook)
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "Authentication", block_type="code")
        assert result is not None
        assert "verify" in result
        # Should only contain the code block, not the heading text
        assert "## Authentication" not in result

    def test_read_section_not_found(self, hook: MarkdownStructureWriteHook) -> None:
        self._setup(hook)
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "Nonexistent")
        assert result is None

    def test_read_section_block_type_not_found(self, hook: MarkdownStructureWriteHook) -> None:
        self._setup(hook)
        # API section has a table but no code blocks
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "API", block_type="code")
        assert result is None

    def test_read_section_star_returns_listing(self, hook: MarkdownStructureWriteHook) -> None:
        """section='*' returns structure listing as JSON."""
        self._setup(hook)
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "*")
        assert result is not None
        listing = json.loads(result)
        assert isinstance(listing, list)
        # Should have frontmatter + sections
        types = {e["type"] for e in listing}
        assert "frontmatter" in types
        assert "section" in types

    def test_read_section_frontmatter(self, hook: MarkdownStructureWriteHook) -> None:
        """section='frontmatter' returns the frontmatter block."""
        self._setup(hook)
        result = hook.read_section("/doc.md", SAMPLE_MD, "h1", "frontmatter")
        assert result is not None
        assert "title" in result
        assert "Architecture" in result

    def test_read_section_frontmatter_missing(self, hook: MarkdownStructureWriteHook) -> None:
        """section='frontmatter' on a doc without frontmatter returns None."""
        no_fm = b"# Heading\nContent.\n"
        ctx = _make_write_ctx("/nofm.md", no_fm, content_hash="h2")
        hook.on_post_write(ctx)
        result = hook.read_section("/nofm.md", no_fm, "h2", "frontmatter")
        assert result is None


# ---------------------------------------------------------------------------
# Structure listing tests
# ---------------------------------------------------------------------------


class TestStructureListing:
    def test_listing_format(self, hook: MarkdownStructureWriteHook) -> None:
        ctx = _make_write_ctx("/doc.md", SAMPLE_MD, content_hash="h1")
        hook.on_post_write(ctx)

        listing = hook.get_structure_listing("/doc.md")
        assert listing is not None
        assert len(listing) >= 1

        # Should have frontmatter entry
        fm_entries = [e for e in listing if e["type"] == "frontmatter"]
        assert len(fm_entries) == 1
        assert "title" in fm_entries[0]["keys"]

        # Should have section entries
        sec_entries = [e for e in listing if e["type"] == "section"]
        assert len(sec_entries) >= 3

        # Each section entry has required fields
        for entry in sec_entries:
            assert "heading" in entry
            assert "depth" in entry
            assert "tokens_est" in entry
            assert "blocks" in entry

    def test_listing_no_index(self, hook: MarkdownStructureWriteHook) -> None:
        assert hook.get_structure_listing("/nonexistent.md") is None

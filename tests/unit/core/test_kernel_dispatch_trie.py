"""Unit tests for KernelDispatch trie-based resolver routing (Issue #1317 Phase 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.kernel_dispatch import KernelDispatch

# ── helpers ────────────────────────────────────────────────────────────


def _make_resolver(*, trie_pattern: str | None = None) -> MagicMock:
    """Create a mock VFSPathResolver, optionally with TRIE_PATTERN.

    Sets TRIE_PATTERN explicitly (None for fallback) to avoid MagicMock
    auto-creating a truthy MagicMock when getattr is called.
    """
    r = MagicMock()
    r.TRIE_PATTERN = trie_pattern
    return r


@pytest.fixture()
def dispatch() -> KernelDispatch:
    return KernelDispatch()


# ── PathTrie Rust class (standalone) ──────────────────────────────────


class TestPathTrieRust:
    """Direct tests on the Rust PathTrie (skip if extension not built)."""

    @pytest.fixture()
    def trie(self):
        from nexus_fast import PathTrie

        return PathTrie()

    def test_register_and_lookup(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        assert trie.lookup("/zone/proc/123/status") == 0

    def test_no_match_returns_none(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        assert trie.lookup("/zone/other/123/status") is None

    def test_unregister(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        assert trie.unregister(0) is True
        assert trie.lookup("/zone/proc/123/status") is None

    def test_multiple_patterns(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        trie.register("/.tasks/tasks/{}/agent/status", 1)
        assert trie.lookup("/z/proc/p/status") == 0
        assert trie.lookup("/.tasks/tasks/t1/agent/status") == 1

    def test_literal_priority(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        trie.register("/.tasks/proc/{}/status", 1)
        assert trie.lookup("/.tasks/proc/p/status") == 1
        assert trie.lookup("/other/proc/p/status") == 0

    def test_duplicate_idx_raises(self, trie) -> None:
        trie.register("/a", 0)
        with pytest.raises(ValueError, match="already registered"):
            trie.register("/b", 0)

    def test_len(self, trie) -> None:
        assert len(trie) == 0
        trie.register("/a", 0)
        assert len(trie) == 1
        trie.unregister(0)
        assert len(trie) == 0

    def test_segment_count_mismatch(self, trie) -> None:
        trie.register("/{}/proc/{}/status", 0)
        assert trie.lookup("/zone/proc") is None
        assert trie.lookup("/zone/proc/pid/status/extra") is None


# ── KernelDispatch trie integration ───────────────────────────────────


class TestTrieResolverRegistration:
    def test_trie_resolver_counted(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        dispatch.register_resolver(r)
        assert dispatch.resolver_count == 1

    def test_fallback_resolver_counted(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver()
        dispatch.register_resolver(r)
        assert dispatch.resolver_count == 1

    def test_mixed_resolvers_counted(self, dispatch: KernelDispatch) -> None:
        trie_r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        fallback_r = _make_resolver()
        dispatch.register_resolver(trie_r)
        dispatch.register_resolver(fallback_r)
        assert dispatch.resolver_count == 2


class TestTrieResolverDispatch:
    def test_trie_resolver_handles_read(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        r.try_read.return_value = b'{"pid": "123"}'
        dispatch.register_resolver(r)

        handled, result = dispatch.resolve_read("/zone/proc/123/status")
        assert handled is True
        assert result == b'{"pid": "123"}'
        r.try_read.assert_called_once()

    def test_trie_resolver_returns_none_falls_to_fallback(self, dispatch: KernelDispatch) -> None:
        trie_r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        trie_r.try_read.return_value = None  # trie resolver doesn't claim it
        fallback_r = _make_resolver()
        fallback_r.try_read.return_value = b"fallback"
        dispatch.register_resolver(trie_r)
        dispatch.register_resolver(fallback_r)

        handled, result = dispatch.resolve_read("/zone/proc/123/status")
        assert handled is True
        assert result == b"fallback"

    def test_trie_miss_goes_to_fallback(self, dispatch: KernelDispatch) -> None:
        trie_r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        trie_r.try_read.return_value = None  # doesn't claim non-matching paths
        fallback_r = _make_resolver()
        fallback_r.try_read.return_value = b"fallback"
        dispatch.register_resolver(trie_r)
        dispatch.register_resolver(fallback_r)

        # Path doesn't match trie pattern at all
        handled, result = dispatch.resolve_read("/some/other/path")
        assert handled is True
        assert result == b"fallback"

    def test_no_match_anywhere(self, dispatch: KernelDispatch) -> None:
        trie_r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        trie_r.try_read.return_value = None
        fallback_r = _make_resolver()
        fallback_r.try_read.return_value = None
        dispatch.register_resolver(trie_r)
        dispatch.register_resolver(fallback_r)

        handled, result = dispatch.resolve_read("/no/match")
        assert handled is False
        assert result is None

    def test_trie_resolver_handles_write(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        r.try_write.side_effect = PermissionError("read-only")
        dispatch.register_resolver(r)

        with pytest.raises(PermissionError, match="read-only"):
            dispatch.resolve_write("/zone/proc/123/status", b"data")

    def test_trie_resolver_handles_delete(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        r.try_delete.side_effect = PermissionError("read-only")
        dispatch.register_resolver(r)

        with pytest.raises(PermissionError, match="read-only"):
            dispatch.resolve_delete("/zone/proc/123/status")


class TestTrieResolverUnregister:
    def test_unregister_trie_resolver(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver(trie_pattern="/{}/proc/{}/status")
        r.try_read.return_value = b"data"
        dispatch.register_resolver(r)
        assert dispatch.resolver_count == 1

        assert dispatch.unregister_resolver(r) is True
        assert dispatch.resolver_count == 0

        # Should no longer match
        handled, result = dispatch.resolve_read("/zone/proc/123/status")
        assert handled is False

    def test_unregister_fallback_resolver(self, dispatch: KernelDispatch) -> None:
        r = _make_resolver()
        dispatch.register_resolver(r)
        assert dispatch.unregister_resolver(r) is True
        assert dispatch.resolver_count == 0

    def test_unregister_missing_returns_false(self, dispatch: KernelDispatch) -> None:
        assert dispatch.unregister_resolver(MagicMock()) is False

    def test_unregister_preserves_others(self, dispatch: KernelDispatch) -> None:
        r1 = _make_resolver(trie_pattern="/{}/proc/{}/status")
        r2 = _make_resolver(trie_pattern="/.tasks/tasks/{}/agent/status")
        # Make resolvers only claim paths matching their pattern
        r1.try_read.side_effect = lambda path, **kw: b"proc" if "/proc/" in path else None
        r2.try_read.side_effect = lambda path, **kw: b"task" if "/.tasks/" in path else None
        dispatch.register_resolver(r1)
        dispatch.register_resolver(r2)

        dispatch.unregister_resolver(r1)
        assert dispatch.resolver_count == 1

        # r1's pattern should no longer match
        h1, _ = dispatch.resolve_read("/zone/proc/123/status")
        assert h1 is False

        # r2 should still work
        h2, res2 = dispatch.resolve_read("/.tasks/tasks/t1/agent/status")
        assert h2 is True
        assert res2 == b"task"

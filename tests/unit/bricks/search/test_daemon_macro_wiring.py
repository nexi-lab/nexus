"""Unit tests for daemon expand=macro wiring and DaemonConfig knobs (Issue #4398).

These tests run without a DB, embedder, or startup() call by injecting a fake
fetcher directly onto _vector_backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.search.macro_chunk import ChunkRow


class _FakeFetcher:
    def __init__(self, rows: list[ChunkRow]) -> None:
        self._rows = rows

    async def fetch_ranges(
        self, spans: list[tuple[str, int, int]], zone_id: str | None
    ) -> list[ChunkRow]:
        out: list[ChunkRow] = []
        for path, lo, hi in spans:
            out += [r for r in self._rows if r.path == path and lo <= r.chunk_index <= hi]
        return out


def _rows() -> list[ChunkRow]:
    return [
        ChunkRow(
            path="/a.md",
            chunk_index=i,
            text=f"c{i}",
            tokens=10,
            line_start=i + 1,
            line_end=i + 1,
            heading_prefix="H",
        )
        for i in range(3)
    ]


def _make_daemon() -> Any:
    """Construct a SearchDaemon without startup() using __new__ + minimal attrs."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    d: Any = SearchDaemon.__new__(SearchDaemon)
    d.config = DaemonConfig()
    d._vector_backend = None
    return d


@pytest.mark.asyncio
async def test_apply_macro_expansion_attaches() -> None:
    """expand='macro' with a live backend attaches macro_text to results."""
    from nexus.bricks.search.results import BaseSearchResult

    d = _make_daemon()
    d._vector_backend = _FakeFetcher(_rows())
    r = BaseSearchResult(path="/a.md", chunk_text="c1", score=1.0, chunk_index=1)
    await d._apply_macro_expansion([r], expand="macro", zone_id="z1")
    assert r.macro_text == "c0\nc1\nc2"


@pytest.mark.asyncio
async def test_apply_macro_expansion_noop_when_disabled() -> None:
    """expand='none' must not modify results even with a live backend."""
    from nexus.bricks.search.results import BaseSearchResult

    d = _make_daemon()
    d._vector_backend = _FakeFetcher(_rows())
    r = BaseSearchResult(path="/a.md", chunk_text="c1", score=1.0, chunk_index=1)
    await d._apply_macro_expansion([r], expand="none", zone_id="z1")
    assert r.macro_text is None


@pytest.mark.asyncio
async def test_apply_macro_expansion_noop_without_backend() -> None:
    """expand='macro' with no vector backend must be a silent no-op."""
    from nexus.bricks.search.results import BaseSearchResult

    d = _make_daemon()
    d._vector_backend = None
    r = BaseSearchResult(path="/a.md", chunk_text="c1", score=1.0, chunk_index=1)
    await d._apply_macro_expansion([r], expand="macro", zone_id="z1")
    assert r.macro_text is None


@pytest.mark.asyncio
async def test_apply_macro_expansion_noop_empty_results() -> None:
    """expand='macro' with an empty result list must not call the backend."""
    d = _make_daemon()
    d._vector_backend = _FakeFetcher(_rows())
    # Should complete without error and not raise
    await d._apply_macro_expansion([], expand="macro", zone_id="z1")


# ---------------------------------------------------------------------------
# DaemonConfig knob tests
# ---------------------------------------------------------------------------


def test_daemon_config_macro_chunk_defaults() -> None:
    """DaemonConfig default macro-chunk knobs match documented defaults."""
    from nexus.bricks.search.daemon import DaemonConfig

    cfg = DaemonConfig()
    assert cfg.macro_chunk_tokens == 1024
    assert cfg.macro_chunk_window == 8
    assert cfg.macro_chunk_code_forward_bias is True


def test_daemon_config_macro_chunk_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DaemonConfig macro-chunk knobs respect environment variables."""
    from nexus.bricks.search.daemon import DaemonConfig

    monkeypatch.setenv("NEXUS_SEARCH_MACRO_CHUNK_TOKENS", "256")
    monkeypatch.setenv("NEXUS_SEARCH_MACRO_CHUNK_WINDOW", "4")
    monkeypatch.setenv("NEXUS_SEARCH_MACRO_CHUNK_FORWARD_BIAS", "false")

    cfg = DaemonConfig()
    assert cfg.macro_chunk_tokens == 256
    assert cfg.macro_chunk_window == 4
    assert cfg.macro_chunk_code_forward_bias is False

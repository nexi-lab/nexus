"""Daemon-eligibility gate for the gRPC ``semantic_search`` surface (#4628).

Post-P12 (#4598) the wired ``_search_daemon`` is the Rust-plugin gRPC
shim, which exposes a dial ``_target`` but none of the pre-P12 backend
attributes (``_backend`` / ``_fts_backend`` / ``_vector_backend``) the
delegation gate in ``_semantic_search_impl`` used to sniff.  The gate
therefore silently dropped every gRPC ``semantic_search`` call to the
SQL-ILIKE fallback — wrong ranking, no hybrid fusion, no ``title_score``
— while the HTTP surface (which reaches the daemon directly) worked.

These tests pin the fixed gate:
  1. a shim-shaped daemon (only ``_target``) IS delegated to, and the
     title-arm attribution rides through, and
  2. a service with no daemon at all still raises the documented
     "not available" error instead of pretending the shim path exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from nexus.bricks.search.search_service import SearchService


@dataclass
class _DaemonRow:
    path: str
    chunk_text: str
    score: float
    chunk_index: int = 0
    title_score: float | None = None


class _FakePluginShim:
    """Shape of the post-P12 Rust-plugin gRPC shim: a dial target and
    an async ``search`` — no legacy backend attributes."""

    def __init__(self, rows: list[_DaemonRow]) -> None:
        self._target = "localhost:2126"
        self.requests: list[object] = []
        self._rows = rows
        self.index_calls: list[tuple[str, bool]] = []
        self.index_result: dict[str, int] = {"indexed_count": 22, "skipped_count": 0}
        self.index_should_raise: Exception | None = None

    async def search(self, request: object) -> list[_DaemonRow]:
        self.requests.append(request)
        return self._rows

    async def index(
        self,
        root_path: str,
        *,
        zone_id: str | None = None,  # noqa: ARG002
        recursive: bool = True,
        max_docs: int = 0,  # noqa: ARG002
    ) -> dict[str, int]:
        self.index_calls.append((root_path, recursive))
        if self.index_should_raise is not None:
            raise self.index_should_raise
        return self.index_result


def _make_service(daemon: object | None) -> SearchService:
    svc = SearchService(
        metadata_store=MagicMock(),
        enforce_permissions=False,
    )
    if daemon is not None:
        # SearchService reads ``self._search_daemon`` via getattr; setattr
        # mirrors the production wiring without tripping mypy on a name
        # the class never declares.
        setattr(svc, "_search_daemon", daemon)  # noqa: B010
    return svc


class TestPluginShimGate:
    @pytest.mark.asyncio
    async def test_plugin_shim_daemon_is_delegated_to(self) -> None:
        shim = _FakePluginShim(
            [
                _DaemonRow(
                    path="/designs/atlas.md",
                    chunk_text="# Atlas Design Doc",
                    score=0.9,
                    title_score=7.0,
                ),
                _DaemonRow(path="/notes/other.md", chunk_text="body", score=0.5),
            ]
        )
        svc = _make_service(shim)
        hits = await svc.semantic_search(query="atlas design doc", search_mode="hybrid")
        assert shim.requests, "shim daemon must receive the search request"
        assert [h["path"] for h in hits] == ["/designs/atlas.md", "/notes/other.md"]
        # #4628: title-arm attribution rides the gRPC transport with the
        # same omit-when-None contract as the HTTP surface.
        assert hits[0]["title_score"] == pytest.approx(7.0)
        assert "title_score" not in hits[1]

    @pytest.mark.asyncio
    async def test_no_daemon_no_record_store_still_raises(self) -> None:
        svc = _make_service(daemon=None)
        with pytest.raises(ValueError, match="not available"):
            await svc.semantic_search(query="anything", search_mode="hybrid")

    @pytest.mark.asyncio
    async def test_index_path_routes_through_plugin_when_wired(self) -> None:
        """#4628 residual: the P12 shim gate at ``semantic_search``
        (query path) recognises ``_target`` and delegates to the plugin.
        The companion ``semantic_search_index`` (index path) had NO such
        gate — the CLI ``nexus search index <dir>`` fell into SANDBOX
        indexing while the plugin saw zero docs, and the HERB QUALITY
        GATE hit 0/8 on the edge topology (Docker Publish red since
        2026-08-11).  This test pins that the index path now prefers
        the plugin when a plugin-transport daemon is wired."""
        shim = _FakePluginShim(rows=[])
        svc = _make_service(shim)
        result = await svc.semantic_search_index("/workspace/demo", recursive=True)
        assert shim.index_calls == [("/workspace/demo", True)]
        # Aggregate indexed_count is synthesized into the per-path
        # mapping shape callers already read from the SANDBOX arms.
        assert result == {"/workspace/demo": 22}

    @pytest.mark.asyncio
    async def test_index_path_falls_back_to_sandbox_on_plugin_error(self) -> None:
        """A plugin index() raise (server down, transient RPC failure)
        must not fail the CLI — the SANDBOX arm below still runs, so
        a mixed-topology deployment retains at least the local
        indexing capability."""
        shim = _FakePluginShim(rows=[])
        shim.index_should_raise = RuntimeError("plugin unreachable")
        svc = _make_service(shim)
        # No _indexing_service / _pipeline_indexer wired → returns {}
        # instead of raising; the CLI reports "Files indexed: 0"
        # rather than crashing.
        result = await svc.semantic_search_index("/workspace/demo")
        assert result == {}
        assert shim.index_calls == [("/workspace/demo", True)]

    @pytest.mark.asyncio
    async def test_index_path_no_daemon_uses_sandbox(self) -> None:
        """Plugin-less deployments (SANDBOX only) MUST take the
        pre-existing indexer arms — the gate must not accidentally
        route to a nonexistent plugin."""
        svc = _make_service(daemon=None)
        # Without any indexer wired, the method returns {} (documented
        # shape).  Verifies the gate doesn't trip on missing daemon.
        result = await svc.semantic_search_index("/workspace/demo")
        assert result == {}

    @pytest.mark.asyncio
    async def test_enforcing_without_context_fails_closed(self) -> None:
        """#4628 review R2: an enforcing deployment must not serve a
        context-less call through the daemon path — the post-search
        ReBAC filter only runs with a context, so delegation would
        return unfiltered paths and chunk text.  The SQL fallback
        fails closed here; the daemon path must match."""
        shim = _FakePluginShim(
            [_DaemonRow(path="/private/secret.md", chunk_text="secret body", score=0.9)]
        )
        svc = SearchService(
            metadata_store=MagicMock(),
            permission_enforcer=MagicMock(),
            enforce_permissions=True,
        )
        setattr(svc, "_search_daemon", shim)  # noqa: B010
        hits = await svc.semantic_search(query="secret", search_mode="hybrid", context=None)
        assert hits == [], "enforcing + no context must return nothing"
        assert not shim.requests, "the daemon must not even be consulted"

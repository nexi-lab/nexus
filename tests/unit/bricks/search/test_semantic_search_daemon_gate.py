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
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
        # Recorded documents that the index_documents RPC received —
        # tests inspect this to confirm the fanout enumerated the
        # right corpus before POSTing.
        self.index_documents_calls: list[list[dict[str, Any]]] = []
        self.index_documents_result: dict[str, Any] = {"indexed": 3, "skipped": [], "skipped_count": 0}
        self.index_documents_should_raise: Exception | None = None

    async def search(self, request: object) -> list[_DaemonRow]:
        self.requests.append(request)
        return self._rows

    async def index_documents(
        self,
        documents: list[dict[str, Any]],
        *,
        zone_id: str | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        self.index_documents_calls.append(documents)
        if self.index_documents_should_raise is not None:
            raise self.index_documents_should_raise
        return self.index_documents_result


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
        The companion ``semantic_search_index`` (index path) had NO
        such gate — the CLI ``nexus search index <dir>`` fell into
        SANDBOX indexing while the plugin saw zero docs, and the HERB
        QUALITY GATE hit 0/8 on the edge topology (Docker Publish red
        since 2026-08-11).

        The plugin path enumerates + reads through the SANDBOX
        indexer's ``file_reader`` (nexus-server's own kernel VFS —
        which the plugin's sidecar kernel cannot see through the
        shared /workspace bind mount) and POSTs the bytes via
        IndexDocuments, so the fanout is topology-independent."""
        shim = _FakePluginShim(rows=[])
        svc = _make_service(shim)

        # Fake SANDBOX indexer supplies the file_reader shim +
        # _read_content coroutine that the plugin-arm re-uses for
        # enumeration.  ``_indexing_service`` is a real attribute on
        # SearchService, so wire it via setattr.
        indexer = MagicMock()
        indexer._file_reader = MagicMock()
        indexer._file_reader.list_files = AsyncMock(
            return_value=[
                {"path": "/w/a.md"},
                {"path": "/w/b.md"},
                {"path": "/w/skip.png"},  # binary — filtered
            ]
        )
        indexer._read_content = AsyncMock(side_effect=lambda p: f"body-of-{p}")
        svc._indexing_service = indexer  # noqa: SLF001

        result = await svc.semantic_search_index("/w", recursive=True)

        # Binary is filtered; two text docs POSTed to plugin.
        assert len(shim.index_documents_calls) == 1
        posted = shim.index_documents_calls[0]
        assert [d["path"] for d in posted] == ["/w/a.md", "/w/b.md"]
        assert posted[0]["text"] == "body-of-/w/a.md"
        # Aggregate indexed count is surfaced via the per-path mapping
        # shape callers already read from the SANDBOX arms.
        assert result == {"/w": 3}

    @pytest.mark.asyncio
    async def test_index_path_falls_back_to_sandbox_on_plugin_error(self) -> None:
        """A plugin index_documents() raise (server down, transient RPC
        failure) must not fail the CLI — the SANDBOX arm below still
        runs, so a mixed-topology deployment retains at least the
        local indexing capability."""
        shim = _FakePluginShim(rows=[])
        shim.index_documents_should_raise = RuntimeError("plugin unreachable")
        svc = _make_service(shim)

        indexer = MagicMock()
        indexer._file_reader = MagicMock()
        indexer._file_reader.list_files = AsyncMock(return_value=[{"path": "/w/a.md"}])
        indexer._read_content = AsyncMock(return_value="body")
        # Wire index_directory too so the SANDBOX fallback can run and
        # produces a documented shape.
        indexer.index_directory = AsyncMock(return_value={})
        indexer.index_document = AsyncMock(side_effect=ValueError("dir not file"))
        svc._indexing_service = indexer  # noqa: SLF001

        result = await svc.semantic_search_index("/w", recursive=True)
        # Plugin was tried once + raised; SANDBOX ran too (empty result).
        assert len(shim.index_documents_calls) == 1
        assert result == {}

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

"""E2E: SANDBOX MCP semantic search surfaces ``semantic_degraded`` flag (Issue #3778).

This test exercises the in-process MCP tool handler — the same code path a
real MCP stdio client would hit — to prove that:

1. The MCP ``nexus_semantic_search`` tool resolves ``SearchService`` via
   ``nx.service("search")`` (not the non-existent ``nx.semantic_search``
   attribute), and
2. In SANDBOX profile, the SearchService's semantic path degrades to local
   BM25S and stamps every result dict with ``semantic_degraded=True``, and
3. The WARNING is logged exactly once per SearchService instance.

We don't spawn a real MCP stdio subprocess — FastMCP's ``get_tool()`` gives
us the registered callable, which is exactly what the wire protocol would
invoke.  This is ~30 lines lighter than a subprocess harness while still
covering the wiring gap Task 10 flagged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from nexus.bricks.mcp.server import create_mcp_server
from nexus.bricks.search.search_service import SearchService
from nexus.core.nexus_fs import NexusFS


class _StubRecordStore:
    """Minimal RecordStore stub — enough for SearchService's SQL path to
    return an empty list without raising.  The SANDBOX fallback test does
    not need any real indexed content; it only checks that the stamping
    and logging wiring are correct."""

    def __init__(self) -> None:
        self.engine = MagicMock()

        def _session_factory() -> Any:
            session = MagicMock()
            session.execute.return_value = MagicMock(fetchall=lambda: [])
            return session

        self.session_factory = _session_factory


class _FakeNexus:
    """Duck-typed NexusFS stand-in exposing only ``service("search")``.

    Routes ``service("search")`` to a real SearchService configured for
    SANDBOX.  That's all the MCP ``nexus_semantic_search`` handler touches
    after the Issue #3778 wiring fix.
    """

    def __init__(self, search_service: SearchService) -> None:
        self._search_service = search_service

    def service(self, name: str) -> Any:
        if name == "search":
            return self._search_service
        return None


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_sandbox_mcp_semantic_search_includes_degraded_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SANDBOX + MCP semantic search → every item has ``semantic_degraded=True``.

    This is the Task-10 follow-up that validates the wiring between the MCP
    ``nexus_semantic_search`` handler and
    ``SearchService._semantic_with_sandbox_fallback``.
    """
    monkeypatch.setenv("NEXUS_PROFILE", "sandbox")

    # Build a real SANDBOX SearchService with a stub record_store so the
    # BM25S fallback's SQL path returns [] cleanly (no daemon wired).
    search_service = SearchService(
        metadata_store=MagicMock(),
        enforce_permissions=False,
        record_store=_StubRecordStore(),
        deployment_profile="sandbox",
    )

    # Patch the SQL fallback to return synthetic hits so the test can assert
    # the degraded-flag stamping end-to-end.
    async def _fake_sql(query: str, path: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "path": "/README.md",
                "chunk_text": f"sandbox hit for {query}",
                "score": 0.9,
                "chunk_index": 0,
                "start_offset": 0,
                "end_offset": 10,
                "line_start": 1,
                "line_end": 1,
            },
            {
                "path": "/docs/intro.md",
                "chunk_text": "another sandbox hit",
                "score": 0.7,
                "chunk_index": 0,
                "start_offset": 0,
                "end_offset": 10,
                "line_start": 1,
                "line_end": 1,
            },
        ]

    monkeypatch.setattr(search_service, "_sql_chunk_search", _fake_sql)

    caplog.set_level(logging.DEBUG, logger="nexus.bricks.search.search_service")

    # Spin up the MCP server with our fake Nexus — create_mcp_server registers
    # every tool including nexus_semantic_search.  create_mcp_server is typed
    # to take a concrete NexusFS but only touches ``service(...)`` here, so
    # cast lets the test use the minimal duck-typed stand-in without bringing
    # a real NexusFS + backend stack online.
    fake_nx = cast(NexusFS, _FakeNexus(search_service))
    mcp = await create_mcp_server(nx=fake_nx)

    tool = await mcp.get_tool("nexus_semantic_search")
    assert tool is not None, "nexus_semantic_search tool not registered"

    # Exercise the MCP handler the same way the wire protocol would.
    raw = await tool.fn(query="sandbox", limit=5, search_mode="semantic")
    # The handler serialises via format_response → JSON string for "json" mode.
    resp = json.loads(raw) if isinstance(raw, str) else raw

    assert "items" in resp, f"unexpected response shape: {resp}"
    assert len(resp["items"]) == 2, resp
    assert all(r.get("semantic_degraded") is True for r in resp["items"]), resp["items"]
    assert resp.get("semantic_degraded") is True, resp

    # Fire the search a second + third time to confirm the WARN-once guarantee.
    await tool.fn(query="sandbox again", limit=5, search_mode="semantic")
    await tool.fn(query="sandbox third", limit=5, search_mode="semantic")

    warn_records = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and rec.name == "nexus.bricks.search.search_service"
        and "SANDBOX" in rec.getMessage()
    ]
    assert len(warn_records) == 1, (
        f"expected exactly 1 SANDBOX WARNING across 3 calls, got "
        f"{len(warn_records)}: {[r.getMessage() for r in warn_records]}"
    )
    assert search_service._sandbox_fallback_warned is True

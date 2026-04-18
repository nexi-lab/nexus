"""E2E: SANDBOX nexus + MCP stdio client (Issue #3778).

No subprocess-spawning MCP stdio-client fixture exists in the current test
harness (tests/e2e/self_contained/mcp/conftest.py only provides environment
isolation via `isolate_mcp_integration_tests`).  The in-process `mcp_server`
fixture from test_mcp_server_integration.py calls `nx_instance.semantic_search`
directly and does not route through SearchService._semantic_with_sandbox_fallback,
so the `semantic_degraded` flag would not be visible through that path either.

Wiring the MCP search tool path to invoke _semantic_with_sandbox_fallback when
NEXUS_PROFILE=sandbox would touch the nexus_semantic_search handler and the
NexusFS.semantic_search delegation chain — more than 20 lines and tracked as a
follow-up on Issue #3778.

The test is xfail until a proper MCP e2e harness is added.
"""

from pathlib import Path

import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "Issue #3778: blocked on MCP e2e harness — no existing fixture spawns an MCP "
        "stdio subprocess for the SANDBOX profile.  The in-process mcp_server fixture "
        "does not route through SearchService._semantic_with_sandbox_fallback, so "
        "semantic_degraded=True would not appear.  Wire the fallback into the MCP "
        "nexus_semantic_search handler as a follow-up to this issue."
    ),
    strict=False,
)
async def test_sandbox_mcp_semantic_search_includes_degraded_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SANDBOX profile and no peers configured, semantic search must
    return results (BM25S fallback) with ``semantic_degraded=True`` per result.

    Blocked on:
    1. A subprocess-spawning MCP stdio client fixture that starts Nexus with
       NEXUS_PROFILE=sandbox and sends JSON-RPC tool calls over stdio.
    2. Wiring ``SearchService._semantic_with_sandbox_fallback`` into the MCP
       ``nexus_semantic_search`` tool so the degraded flag propagates to the
       tool response JSON.

    When both are available, replace this skeleton with the real assertion:

        monkeypatch.setenv("NEXUS_PROFILE", "sandbox")
        monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path / "nexus"))

        # Seed content so BM25S has something to return
        await mcp_client.call_tool(
            "nexus_write_file",
            {"path": "/README.md", "content": "hello world from sandbox"},
        )

        resp = await mcp_client.call_tool(
            "nexus_semantic_search",
            {"query": "sandbox", "search_mode": "semantic"},
        )

        assert "items" in resp
        assert len(resp["items"]) >= 1
        assert all(r.get("semantic_degraded") is True for r in resp["items"])
    """
    pytest.xfail("MCP stdio harness not yet available; see Issue #3778 follow-up notes above.")

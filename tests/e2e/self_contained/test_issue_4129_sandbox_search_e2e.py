"""Real sandbox search E2E coverage for Issue #4129.

This test intentionally avoids mocks: it boots a sandbox NexusFS, writes real
files, initializes the real semantic indexer, exercises the public search
service APIs, then calls the CLI and MCP tool handlers against the same live
filesystem instance.  The CLI path uses a no-op close proxy so sequential
commands behave like a remote daemon-backed session instead of destroying the
in-process server after the first command.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest
from click.testing import CliRunner

import nexus
from nexus.bricks.mcp.server import create_mcp_server
from nexus.cli.commands import search as search_module
from nexus.server.lifespan._async_engines import adispose_async_engines

pytestmark = [pytest.mark.e2e, pytest.mark.xdist_group(name="issue_4129_search_e2e")]

T = TypeVar("T")


_BUDGET_MS = {
    "rpc.glob": 1000,
    "rpc.glob_batch": 1000,
    "rpc.grep": 1500,
    "rpc.initialize_semantic_search": 5000,
    "rpc.semantic_search_index": 5000,
    "rpc.semantic_search_stats": 1000,
    "rpc.semantic_search": 2000,
    "cli.glob": 2000,
    "cli.grep": 2500,
    "cli.search_init": 5000,
    "cli.search_index": 5000,
    "cli.search_stats": 2000,
    "cli.search_query": 2500,
    "mcp.nexus_glob": 2000,
    "mcp.nexus_grep": 2500,
    "mcp.nexus_semantic_search": 2500,
    "rpc.openai_hybrid_initialize": 10000,
    "rpc.openai_hybrid_index": 120000,
    "rpc.openai_hybrid_query": 30000,
}


class _NoCloseProxy:
    def __init__(self, nx: Any) -> None:
        self._nx = nx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._nx, name)

    def close(self) -> None:
        return None


async def _timed(
    timings: dict[str, float],
    name: str,
    op: Callable[[], T | Awaitable[T]],
) -> T:
    start = time.perf_counter()
    result = op()
    if inspect.isawaitable(result):
        result = await result
    elapsed_ms = (time.perf_counter() - start) * 1000
    timings[name] = elapsed_ms
    assert elapsed_ms < _BUDGET_MS[name], f"{name} took {elapsed_ms:.1f}ms"
    return result


async def _invoke_cli(
    runner: CliRunner,
    command: Any,
    args: list[str],
    timings: dict[str, float],
    name: str,
) -> str:
    start = time.perf_counter()
    result = await asyncio.to_thread(lambda: runner.invoke(command, args, catch_exceptions=False))
    elapsed_ms = (time.perf_counter() - start) * 1000
    timings[name] = elapsed_ms
    assert result.exit_code == 0, result.output
    assert elapsed_ms < _BUDGET_MS[name], f"{name} took {elapsed_ms:.1f}ms"
    return result.output


def _load_cli_json(output: str) -> Any:
    return json.loads(output)


def _paths(items: list[dict[str, Any]]) -> set[str]:
    return {item["path"] for item in items}


def _assert_semantic_surface(hits: list[dict[str, Any]]) -> bool:
    assert hits, "semantic search should return local vector hits or degraded keyword hits"
    has_vector_lane = any(hit.get("vector_score") is not None for hit in hits)
    if has_vector_lane:
        assert any("keyword_score" in hit for hit in hits)
        assert all(hit.get("semantic_degraded") is not True for hit in hits)
        return False

    assert all(hit.get("semantic_degraded") is True for hit in hits)
    assert all("keyword_score" in hit for hit in hits)
    return True


@pytest.mark.asyncio
async def test_issue_4129_sandbox_search_surfaces_correctness_and_latency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timings: dict[str, float] = {}
    data_dir = tmp_path / "nexus"

    nx = nexus.connect(config={"profile": "sandbox", "data_dir": str(data_dir)})
    if inspect.isawaitable(nx):
        nx = await nx
    try:
        nx.mkdir("/workspace", exist_ok=True)
        nx.mkdir("/workspace/src", exist_ok=True)
        nx.mkdir("/workspace/docs", exist_ok=True)
        nx.write(
            "/workspace/src/app.py",
            b"# TODO: wire sandbox search\n"
            b"def search_story():\n"
            b"    return 'semantic degraded keyword_score vector_score bm25 sqlite vec'\n",
        )
        nx.write(
            "/workspace/src/util.py",
            b"def helper():\n    return 'glob batch grep correctness performance'\n",
        )
        nx.write(
            "/workspace/docs/search.md",
            b"# Sandbox search\n"
            b"Semantic search degrades to local BM25 when vector search is unavailable.\n",
        )
        nx.write("/workspace/notes.txt", b"TODO: document the degraded semantic story\n")

        service = nx.service("search")
        assert service is not None

        glob_result = await _timed(
            timings,
            "rpc.glob",
            lambda: service.glob("**/*.py", "/workspace"),
        )
        assert set(glob_result) == {"/workspace/src/app.py", "/workspace/src/util.py"}

        batch_result = await _timed(
            timings,
            "rpc.glob_batch",
            lambda: service.glob_batch(["**/*.py", "**/*.md"], "/workspace"),
        )
        assert batch_result["**/*.py"] == ["/workspace/src/app.py", "/workspace/src/util.py"]
        assert batch_result["**/*.md"] == ["/workspace/docs/search.md"]

        grep_result = await _timed(timings, "rpc.grep", lambda: service.grep("TODO", "/workspace"))
        assert {match["file"] for match in grep_result} == {
            "/workspace/src/app.py",
            "/workspace/notes.txt",
        }

        await _timed(
            timings,
            "rpc.initialize_semantic_search",
            lambda: service.ainitialize_semantic_search(nx=nx, record_store_engine=None),
        )
        indexed = await _timed(
            timings,
            "rpc.semantic_search_index",
            lambda: service.semantic_search_index("/workspace", recursive=True),
        )
        assert indexed["/workspace/src/app.py"] > 0
        assert indexed["/workspace/docs/search.md"] > 0

        stats = await _timed(timings, "rpc.semantic_search_stats", service.semantic_search_stats)
        assert stats["total_files"] >= 2
        assert stats["total_chunks"] >= 2

        semantic_hits = await _timed(
            timings,
            "rpc.semantic_search",
            lambda: service.semantic_search(
                "semantic degraded keyword score",
                path="/workspace",
                limit=5,
                search_mode="semantic",
            ),
        )
        assert semantic_hits[0]["path"].startswith("/workspace/")
        _assert_semantic_surface(semantic_hits)

        proxy = _NoCloseProxy(nx)

        async def _get_filesystem(_remote_url: str | None, _remote_api_key: str | None) -> Any:
            return proxy

        @asynccontextmanager
        async def _open_filesystem(
            _remote_url: str | None,
            _remote_api_key: str | None,
            **_kwargs: Any,
        ) -> Any:
            yield proxy

        monkeypatch.setattr(search_module, "get_filesystem", _get_filesystem)
        monkeypatch.setattr(search_module, "open_filesystem", _open_filesystem)
        monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")

        runner = CliRunner()
        cli_glob = _load_cli_json(
            await _invoke_cli(
                runner,
                search_module.glob,
                ["**/*.py", "/workspace", "--json"],
                timings,
                "cli.glob",
            )
        )
        assert _paths(cli_glob["data"]) == {"/workspace/src/app.py", "/workspace/src/util.py"}

        cli_grep = _load_cli_json(
            await _invoke_cli(
                runner,
                search_module.grep,
                ["TODO", "/workspace", "--json"],
                timings,
                "cli.grep",
            )
        )
        assert cli_grep["data"]["total_matches"] == 2

        await _invoke_cli(runner, search_module.search_init, [], timings, "cli.search_init")
        await _invoke_cli(
            runner,
            search_module.search_index,
            ["/workspace", "--recursive"],
            timings,
            "cli.search_index",
        )
        cli_stats = await _invoke_cli(
            runner, search_module.search_stats, [], timings, "cli.search_stats"
        )
        assert "Total chunks:" in cli_stats

        cli_query = _load_cli_json(
            await _invoke_cli(
                runner,
                search_module.search_query,
                [
                    "semantic degraded keyword score",
                    "--path",
                    "/workspace",
                    "--limit",
                    "5",
                    "--mode",
                    "semantic",
                    "--json",
                ],
                timings,
                "cli.search_query",
            )
        )
        assert cli_query
        _assert_semantic_surface(cli_query)

        mcp = await create_mcp_server(nx=nx)
        glob_tool = await mcp.get_tool("nexus_glob")
        grep_tool = await mcp.get_tool("nexus_grep")
        semantic_tool = await mcp.get_tool("nexus_semantic_search")
        assert glob_tool is not None
        assert grep_tool is not None
        assert semantic_tool is not None

        mcp_glob = json.loads(
            await _timed(
                timings,
                "mcp.nexus_glob",
                lambda: glob_tool.fn(pattern="**/*.py", path="/workspace"),
            )
        )
        assert set(mcp_glob["items"]) == {"/workspace/src/app.py", "/workspace/src/util.py"}

        mcp_grep = json.loads(
            await _timed(
                timings,
                "mcp.nexus_grep",
                lambda: grep_tool.fn(pattern="TODO", path="/workspace", limit=10),
            )
        )
        assert {item["file"] for item in mcp_grep["items"]} == {
            "/workspace/src/app.py",
            "/workspace/notes.txt",
        }

        mcp_semantic = json.loads(
            await _timed(
                timings,
                "mcp.nexus_semantic_search",
                lambda: semantic_tool.fn(
                    query="semantic degraded keyword score",
                    path="/workspace",
                    limit=5,
                    search_mode="semantic",
                ),
            )
        )
        assert mcp_semantic["items"]
        mcp_degraded = _assert_semantic_surface(mcp_semantic["items"])
        assert bool(mcp_semantic.get("semantic_degraded")) is mcp_degraded
    finally:
        await adispose_async_engines(nx)
        nx.close()


@pytest.mark.asyncio
async def test_issue_4129_openai_hybrid_search_uses_vector_and_keyword_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is required for real OpenAI hybrid E2E")
    pytest.importorskip("litellm")
    pytest.importorskip("sqlite_vec")

    monkeypatch.setenv("NEXUS_EMBEDDER", "litellm")
    monkeypatch.setenv("NEXUS_EMBEDDING_MODEL", "text-embedding-3-small")

    timings: dict[str, float] = {}
    data_dir = tmp_path / "nexus-openai-hybrid"

    nx = nexus.connect(config={"profile": "sandbox", "data_dir": str(data_dir)})
    if inspect.isawaitable(nx):
        nx = await nx
    try:
        nx.mkdir("/workspace", exist_ok=True)
        nx.mkdir("/workspace/src", exist_ok=True)
        nx.mkdir("/workspace/docs", exist_ok=True)
        nx.write(
            "/workspace/src/app.py",
            b"# TODO: wire sandbox search\n"
            b"def search_story():\n"
            b"    return 'semantic degraded keyword_score vector_score bm25 sqlite vec "
            b"OpenAI hybrid retrieval'\n",
        )
        nx.write(
            "/workspace/docs/search.md",
            b"# Sandbox search\n"
            b"Hybrid semantic search should combine OpenAI vector retrieval with "
            b"keyword search signals.\n",
        )
        nx.write("/workspace/notes.txt", b"TODO: document the degraded semantic story\n")

        # Sandbox metadata writes settle asynchronously; wait before directory indexing.
        await asyncio.sleep(1.0)

        service = nx.service("search")
        assert service is not None
        assert getattr(service, "_sqlite_vec_backend", None) is not None

        await _timed(
            timings,
            "rpc.openai_hybrid_initialize",
            lambda: service.ainitialize_semantic_search(
                nx=nx,
                record_store_engine=None,
                embedding_provider="openai",
                embedding_model="text-embedding-3-small",
            ),
        )
        indexed = await _timed(
            timings,
            "rpc.openai_hybrid_index",
            lambda: service.semantic_search_index("/workspace", recursive=True),
        )
        assert indexed["/workspace/src/app.py"] > 0
        assert indexed["/workspace/docs/search.md"] > 0

        hybrid_hits = await _timed(
            timings,
            "rpc.openai_hybrid_query",
            lambda: service.semantic_search(
                "OpenAI hybrid retrieval keyword score",
                path="/workspace",
                limit=5,
                search_mode="hybrid",
            ),
        )
        assert hybrid_hits
        assert {hit["path"] for hit in hybrid_hits} >= {
            "/workspace/src/app.py",
            "/workspace/docs/search.md",
        }
        assert any(hit.get("vector_score") is not None for hit in hybrid_hits)
        assert any(hit.get("keyword_score") is not None for hit in hybrid_hits)
        assert all(hit.get("semantic_degraded") is not True for hit in hybrid_hits)
    finally:
        await adispose_async_engines(nx)
        nx.close()

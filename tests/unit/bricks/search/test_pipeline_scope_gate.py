"""Central-gate scope tests for ``IndexingPipeline.index_documents`` (Issue #3698).

The pipeline's scope_provider is the authoritative cost-control gate for
the embedding pipeline. These tests verify it:

1. Drops out-of-scope paths before they reach the chunker (no embedding
   API calls wasted).
2. Preserves the caller's input order when mixing in-scope and
   out-of-scope paths.
3. Re-reads the scope on every call so a file "moving" across the scope
   boundary (via a fresh mutation event with a different path) picks up
   the current rule without pipeline restart.
4. Returns contract-violation errors on bad path input instead of
   silently swallowing them.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from nexus.bricks.search.chunking import DocumentChunker
from nexus.bricks.search.index_scope import IndexScope
from nexus.bricks.search.indexing import IndexingPipeline


class _FakeChunker:
    """Chunker stub that records which paths got chunked and returns
    empty chunks so the pipeline's phase-2 embed path is a no-op."""

    def __init__(self) -> None:
        self.seen: list[str] = []


async def _fake_chunk_document(self: Any, path: str, content: str, path_id: str) -> Any:
    # Record and return an object that looks chunk-shaped to the pipeline.
    self._chunker.seen.append(path)

    class _Doc:
        chunks: list = []
        chunk_texts: list = []
        path: str = ""
        path_id: str = ""
        context_jsons: list = []
        context_positions: list = []
        source_document_id = None
        contextual_result = None

    d = _Doc()
    d.path = path
    d.path_id = path_id
    return d


@pytest.fixture
def pipeline_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Factory that returns an IndexingPipeline wired with a fake chunker
    and a configurable scope_provider."""

    def _make(scope: IndexScope | None) -> tuple[IndexingPipeline, _FakeChunker]:
        chunker = _FakeChunker()

        def _scope_provider() -> IndexScope | None:
            return scope

        pipeline = IndexingPipeline(
            chunker=cast(DocumentChunker, chunker),
            embedding_provider=None,  # no embedding = phase-2 no-op
            scope_provider=_scope_provider,
        )
        # Patch the chunking method so we can spy on which paths reach it.
        monkeypatch.setattr(
            IndexingPipeline,
            "_chunk_document",
            _fake_chunk_document,
            raising=True,
        )
        return pipeline, chunker

    return _make


@pytest.mark.asyncio
async def test_central_gate_drops_out_of_scope_path(pipeline_factory: Any) -> None:
    """File in a non-indexed directory must never hit the chunker."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    results = await pipeline.index_documents(
        [("/zone/zone_a/docs/README.md", "content", "path-123")]
    )

    assert len(results) == 1
    assert results[0].path == "/zone/zone_a/docs/README.md"
    assert results[0].chunks_indexed == 0
    assert results[0].error is None
    # Critical: the chunker must not have been touched.
    assert chunker.seen == []


@pytest.mark.asyncio
async def test_central_gate_allows_in_scope_path(pipeline_factory: Any) -> None:
    """File under a registered dir must reach the chunker."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    results = await pipeline.index_documents([("/zone/zone_a/src/main.py", "content", "path-456")])

    assert len(results) == 1
    assert chunker.seen == ["/zone/zone_a/src/main.py"]


@pytest.mark.asyncio
async def test_central_gate_preserves_input_order_with_mixed_scope(
    pipeline_factory: Any,
) -> None:
    """Mixed batches must return results in the caller's original order."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    docs = [
        ("/zone/zone_a/docs/a.md", "a", "id-a"),  # out of scope
        ("/zone/zone_a/src/b.py", "b", "id-b"),  # in scope
        ("/zone/zone_a/docs/c.md", "c", "id-c"),  # out of scope
        ("/zone/zone_a/src/lib/d.py", "d", "id-d"),  # in scope (descendant)
    ]

    results = await pipeline.index_documents(docs)

    assert [r.path for r in results] == [
        "/zone/zone_a/docs/a.md",
        "/zone/zone_a/src/b.py",
        "/zone/zone_a/docs/c.md",
        "/zone/zone_a/src/lib/d.py",
    ]
    # Only the two in-scope paths reached the chunker.
    assert set(chunker.seen) == {
        "/zone/zone_a/src/b.py",
        "/zone/zone_a/src/lib/d.py",
    }


@pytest.mark.asyncio
async def test_central_gate_file_move_unindexed_to_indexed(
    pipeline_factory: Any,
) -> None:
    """File-move semantics (Issue #3698 rule 4 / test #11):

    When a file at ``/docs/foo.py`` (unindexed) is later renamed to
    ``/src/foo.py`` (indexed), a subsequent mutation event fires with
    the new path. The pipeline must pick up the current scope and
    embed the new path.
    """
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    # First: file at /docs — out of scope.
    results_before = await pipeline.index_documents(
        [("/zone/zone_a/docs/foo.py", "content-v1", "path-foo")]
    )
    assert results_before[0].chunks_indexed == 0
    assert chunker.seen == []

    # Then: file moves to /src — in scope. New mutation event fires.
    results_after = await pipeline.index_documents(
        [("/zone/zone_a/src/foo.py", "content-v1", "path-foo")]
    )
    assert chunker.seen == ["/zone/zone_a/src/foo.py"]
    assert results_after[0].error is None


@pytest.mark.asyncio
async def test_central_gate_file_move_indexed_to_unindexed(
    pipeline_factory: Any,
) -> None:
    """When a file moves from ``/src`` (indexed) to ``/docs`` (unindexed),
    the new-path upsert must be silently dropped. Note: the DELETE for
    the old path is handled upstream in the mutation consumer, not here —
    deletes are never scope-filtered per the design."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    results = await pipeline.index_documents([("/zone/zone_a/docs/moved.py", "content", "path-1")])
    assert results[0].chunks_indexed == 0
    assert chunker.seen == []


@pytest.mark.asyncio
async def test_central_gate_file_move_within_indexed_scope(
    pipeline_factory: Any,
) -> None:
    """A file moving between two indexed paths stays in scope."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src", "/src/lib"})},
    )
    pipeline, chunker = pipeline_factory(scope)

    # Both old and new paths are under the overlapping /src + /src/lib scope.
    await pipeline.index_documents([("/zone/zone_a/src/old.py", "content", "p-old")])
    await pipeline.index_documents([("/zone/zone_a/src/lib/new.py", "content", "p-new")])

    assert chunker.seen == [
        "/zone/zone_a/src/old.py",
        "/zone/zone_a/src/lib/new.py",
    ]


@pytest.mark.asyncio
async def test_central_gate_reflects_live_scope_changes(
    pipeline_factory: Any,
) -> None:
    """The pipeline must ask for a fresh scope on every call — proving
    that a new registration takes effect without a daemon restart."""

    # Mutable state the scope provider reads.
    scope_state: dict[str, Any] = {
        "scope": IndexScope(
            zone_modes={"zone_a": "scoped"},
            zone_directories={"zone_a": frozenset()},  # empty = nothing in scope
        )
    }

    chunker = _FakeChunker()

    def _scope_provider() -> IndexScope:
        return scope_state["scope"]

    pipeline = IndexingPipeline(
        chunker=cast(DocumentChunker, chunker),
        embedding_provider=None,
        scope_provider=_scope_provider,
    )
    import types

    # Patch instance method — test-isolated monkey patch.
    async def _fake(self_: Any, path: str, content: str, path_id: str) -> Any:
        chunker.seen.append(path)

        class _Doc:
            chunks: list = []
            chunk_texts: list = []
            path_id: str = ""
            context_jsons: list = []
            context_positions: list = []
            source_document_id = None
            contextual_result = None

        d = _Doc()
        d.path = path
        d.path_id = path_id
        return d

    # Bind via setattr so mypy doesn't need a per-line suppression for
    # the method-assign.
    pipeline._chunk_document = types.MethodType(_fake, pipeline)

    # First call: empty scope → nothing indexed.
    await pipeline.index_documents([("/zone/zone_a/src/main.py", "content", "id-1")])
    assert chunker.seen == []

    # Operator registers /src — scope mutates.
    scope_state["scope"] = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )

    # Second call: scope now covers /src → indexed.
    await pipeline.index_documents([("/zone/zone_a/src/main.py", "content", "id-1")])
    assert chunker.seen == ["/zone/zone_a/src/main.py"]


@pytest.mark.asyncio
async def test_central_gate_contract_violation_surfaces_as_error(
    pipeline_factory: Any,
) -> None:
    """A contract-violating path must get an error IndexResult, not a crash."""
    scope = IndexScope(
        zone_modes={"zone_a": "all"},
        zone_directories={},
    )
    pipeline, _ = pipeline_factory(scope)

    # Empty path — contract violation per is_path_indexed.
    results = await pipeline.index_documents([("", "content", "id-1")])
    assert len(results) == 1
    assert results[0].error is not None
    assert "scope check failed" in results[0].error

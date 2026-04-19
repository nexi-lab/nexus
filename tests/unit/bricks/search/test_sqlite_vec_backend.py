"""Unit tests for SqliteVecBackend (Issue #3778).

The tests use an in-memory SQLite database (``:memory:``) and a mocked
``litellm.aembedding`` so they never hit a real embedding API.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest

# Skip the whole module if the optional sqlite-vec / litellm deps aren't
# installed (CI matrix without [sandbox] extra).
sqlite_vec = pytest.importorskip("sqlite_vec")
pytest.importorskip("litellm")

from nexus.bricks.search.sqlite_vec_backend import (  # noqa: E402
    DEFAULT_EMBEDDING_DIM,
    SqliteVecBackend,
)

# Use a tiny dim so tests stay fast and assertions are easy.
TEST_DIM = 4


def _det_vec(seed: float) -> list[float]:
    """Deterministic 4-d unit-ish vector keyed by ``seed``."""
    return [seed, seed + 0.1, seed + 0.2, seed + 0.3]


class _FakeEmbedItem(dict):
    """litellm returns objects supporting both attribute and item access."""


def _fake_response(vectors: list[list[float]]) -> Any:
    class _Resp:
        def __init__(self, vecs: list[list[float]]) -> None:
            self.data = [_FakeEmbedItem(embedding=v, index=i) for i, v in enumerate(vecs)]

    return _Resp(vectors)


@pytest.fixture
def mock_embed():
    """Patch ``litellm.aembedding`` to return deterministic vectors.

    The default fake returns one vector per input string, looking up
    text-keyed vectors from ``vectors_by_text`` (set by the test) or
    a uniform default.
    """
    vectors_by_text: dict[str, list[float]] = {}

    async def _aembedding(*, model: str, input: list[str], **_kwargs: Any) -> Any:
        return _fake_response(
            [vectors_by_text.get(t, _det_vec(float(i + 1))) for i, t in enumerate(input)]
        )

    with patch("litellm.aembedding", side_effect=_aembedding):
        yield vectors_by_text


@pytest.fixture
async def backend(mock_embed):
    """In-memory backend started with a 4-d embedding column."""
    b = SqliteVecBackend(
        db_path=":memory:",
        embedding_model="fake-model",
        embedding_dim=TEST_DIM,
    )
    await b.startup()
    yield b
    await b.shutdown()


# =============================================================================
# Construction-time guards
# =============================================================================


class TestImportGuards:
    def test_construction_succeeds_when_deps_present(self) -> None:
        # Both deps are installed in the test env (importorskip above);
        # constructing the backend must not raise.
        b = SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM)
        assert b._embedding_dim == TEST_DIM

    def test_missing_sqlite_vec_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction must fail with a clear ImportError when sqlite-vec is absent."""
        # Hide the module *and* prevent re-import inside the constructor.
        real_sqlite_vec = sys.modules.pop("sqlite_vec", None)
        try:
            import builtins

            real_import = builtins.__import__

            def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
                if name == "sqlite_vec":
                    raise ImportError("simulated missing sqlite_vec")
                return real_import(name, *a, **kw)

            monkeypatch.setattr(builtins, "__import__", _fake_import)
            with pytest.raises(ImportError, match="sqlite-vec"):
                SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM)
        finally:
            if real_sqlite_vec is not None:
                sys.modules["sqlite_vec"] = real_sqlite_vec

    def test_missing_litellm_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction must fail with a clear ImportError when litellm is absent."""
        real_litellm = sys.modules.pop("litellm", None)
        try:
            import builtins

            real_import = builtins.__import__

            def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
                if name == "litellm":
                    raise ImportError("simulated missing litellm")
                return real_import(name, *a, **kw)

            monkeypatch.setattr(builtins, "__import__", _fake_import)
            with pytest.raises(ImportError, match="litellm"):
                SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM)
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm


# =============================================================================
# Lifecycle
# =============================================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_startup_is_idempotent(self, mock_embed) -> None:
        b = SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM)
        await b.startup()
        # Second call must not raise and must not recreate the conn.
        first_conn = b._conn
        await b.startup()
        assert b._conn is first_conn
        await b.shutdown()

    @pytest.mark.asyncio
    async def test_default_embedding_dim_matches_expected(self) -> None:
        b = SqliteVecBackend(db_path=":memory:")
        assert b._embedding_dim == DEFAULT_EMBEDDING_DIM


# =============================================================================
# Upsert + search roundtrip
# =============================================================================


class TestUpsertSearchRoundtrip:
    @pytest.mark.asyncio
    async def test_upsert_then_search_returns_inserted_doc(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        # Pin specific vectors for the doc and the query so KNN lines up.
        mock_embed["alpha"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["beta"] = [0.0, 1.0, 0.0, 0.0]
        mock_embed["alpha?"] = [1.0, 0.0, 0.0, 0.0]  # query

        n = await backend.upsert(
            [
                {"path": "/docs/a.md", "text": "alpha", "chunk_index": 0},
                {"path": "/docs/b.md", "text": "beta", "chunk_index": 0},
            ],
            zone_id="z1",
        )
        assert n == 2

        results = await backend.search("alpha?", limit=2, zone_id="z1")
        assert results, "expected at least one hit"
        assert results[0].path == "/docs/a.md"
        assert results[0].zone_id == "z1"
        assert results[0].score > 0.0
        assert results[0].vector_score == results[0].score

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing_row(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        mock_embed["v1"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["v2-rewritten"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]

        await backend.upsert([{"path": "/x.md", "text": "v1", "chunk_index": 0}], zone_id="z")
        await backend.upsert(
            [{"path": "/x.md", "text": "v2-rewritten", "chunk_index": 0}], zone_id="z"
        )
        results = await backend.search("q", limit=10, zone_id="z")
        # Only the rewritten chunk should be present.
        paths_texts = [(r.path, r.chunk_text) for r in results]
        assert paths_texts == [("/x.md", "v2-rewritten")]

    @pytest.mark.asyncio
    async def test_search_respects_limit(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        for i in range(5):
            mock_embed[f"doc{i}"] = [float(i), 0.0, 0.0, 0.0]
        mock_embed["q"] = [2.5, 0.0, 0.0, 0.0]
        await backend.upsert(
            [{"path": f"/d{i}.md", "text": f"doc{i}", "chunk_index": 0} for i in range(5)],
            zone_id="z",
        )
        results = await backend.search("q", limit=2, zone_id="z")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_path_filter_prefix_match(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        for label in ("a", "b", "c"):
            mock_embed[label] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]
        await backend.upsert(
            [
                {"path": "/inside/a.md", "text": "a", "chunk_index": 0},
                {"path": "/inside/b.md", "text": "b", "chunk_index": 0},
                {"path": "/outside/c.md", "text": "c", "chunk_index": 0},
            ],
            zone_id="z",
        )
        results = await backend.search("q", limit=10, zone_id="z", path_filter="/inside")
        assert {r.path for r in results} == {"/inside/a.md", "/inside/b.md"}


# =============================================================================
# Zone isolation
# =============================================================================


class TestZoneIsolation:
    @pytest.mark.asyncio
    async def test_docs_in_zone_a_dont_leak_into_zone_b(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        mock_embed["alpha"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]
        await backend.upsert(
            [{"path": "/a.md", "text": "alpha", "chunk_index": 0}], zone_id="zoneA"
        )
        results_a = await backend.search("q", limit=10, zone_id="zoneA")
        results_b = await backend.search("q", limit=10, zone_id="zoneB")
        assert len(results_a) == 1
        assert results_a[0].zone_id == "zoneA"
        assert results_b == []


# =============================================================================
# Delete
# =============================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_doc_from_search(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        mock_embed["alpha"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["beta"] = [0.0, 1.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]
        await backend.upsert(
            [
                {"path": "/a.md", "text": "alpha", "chunk_index": 0},
                {"path": "/b.md", "text": "beta", "chunk_index": 0},
            ],
            zone_id="z",
        )
        n = await backend.delete(["/a.md"], zone_id="z")
        assert n == 1
        results = await backend.search("q", limit=10, zone_id="z")
        assert {r.path for r in results} == {"/b.md"}

    @pytest.mark.asyncio
    async def test_delete_empty_id_list_returns_zero(self, backend: SqliteVecBackend) -> None:
        assert await backend.delete([], zone_id="z") == 0


# =============================================================================
# index() = full rebuild
# =============================================================================


class TestIndexFullRebuild:
    @pytest.mark.asyncio
    async def test_index_drops_stale_zone_rows(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        mock_embed["old1"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["old2"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["fresh"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]
        # Initial seed: two stale docs.
        await backend.upsert(
            [
                {"path": "/old1.md", "text": "old1", "chunk_index": 0},
                {"path": "/old2.md", "text": "old2", "chunk_index": 0},
            ],
            zone_id="z",
        )
        # index() with a single fresh doc: drops both stale rows.
        n = await backend.index(
            [{"path": "/fresh.md", "text": "fresh", "chunk_index": 0}],
            zone_id="z",
        )
        assert n == 1
        results = await backend.search("q", limit=10, zone_id="z")
        assert {r.path for r in results} == {"/fresh.md"}

    @pytest.mark.asyncio
    async def test_index_only_affects_the_target_zone(
        self, backend: SqliteVecBackend, mock_embed: dict[str, list[float]]
    ) -> None:
        mock_embed["a-zone-A"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["a-zone-B"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["fresh-A"] = [1.0, 0.0, 0.0, 0.0]
        mock_embed["q"] = [1.0, 0.0, 0.0, 0.0]
        await backend.upsert([{"path": "/a.md", "text": "a-zone-A", "chunk_index": 0}], zone_id="A")
        await backend.upsert([{"path": "/a.md", "text": "a-zone-B", "chunk_index": 0}], zone_id="B")
        # Rebuild zone A; zone B must remain intact.
        await backend.index([{"path": "/a.md", "text": "fresh-A", "chunk_index": 0}], zone_id="A")
        result_b = await backend.search("q", limit=10, zone_id="B")
        assert len(result_b) == 1
        assert result_b[0].chunk_text == "a-zone-B"

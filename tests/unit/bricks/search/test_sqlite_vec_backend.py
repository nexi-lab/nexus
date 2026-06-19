"""Unit tests for SqliteVecBackend (Issue #3778).

The tests use an in-memory SQLite database (``:memory:``) and a mocked
``litellm.aembedding`` so they never hit a real embedding API.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any
from unittest.mock import patch

import pytest

# Skip the whole module if the optional sqlite-vec / litellm deps aren't
# installed (CI matrix without [sandbox] extra).
sqlite_vec = pytest.importorskip("sqlite_vec")
pytest.importorskip("litellm")
pytest.importorskip("fastembed")

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
        embedder="litellm",
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
        b = SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM, embedder="litellm")
        assert b._embedding_dim == TEST_DIM

    def test_missing_sqlite_vec_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction must fail with a clear ImportError when sqlite-vec is absent."""
        real_find_spec = importlib.util.find_spec

        def _fake_find_spec(name: str, *a: Any, **kw: Any) -> Any:
            if name == "sqlite_vec":
                return None
            return real_find_spec(name, *a, **kw)

        monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
        with pytest.raises(ImportError, match="sqlite-vec"):
            SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM, embedder="litellm")

    def test_missing_litellm_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction with embedder='litellm' must fail clearly when litellm is absent."""
        real_find_spec = importlib.util.find_spec

        def _fake_find_spec(name: str, *a: Any, **kw: Any) -> Any:
            if name == "litellm":
                return None
            return real_find_spec(name, *a, **kw)

        monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
        with pytest.raises(ImportError, match="litellm"):
            SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM, embedder="litellm")


# =============================================================================
# Lifecycle
# =============================================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_startup_is_idempotent(self, mock_embed) -> None:
        b = SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM, embedder="litellm")
        await b.startup()
        # Second call must not raise and must not recreate the conn.
        first_conn = b._conn
        await b.startup()
        assert b._conn is first_conn
        await b.shutdown()

    @pytest.mark.asyncio
    async def test_default_embedding_dim_matches_expected(self) -> None:
        b = SqliteVecBackend(db_path=":memory:", embedder="litellm")
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


class TestPerLoopLocks:
    """Issue #3976: a single backend reused across event loops must not
    raise "bound to a different event loop" on Python 3.14."""

    def test_op_locks_are_distinct_per_event_loop(self) -> None:
        import asyncio
        import threading

        backend = SqliteVecBackend(
            db_path=":memory:",
            embedding_model="fake-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        seen: list[asyncio.Lock] = []
        errors: list[BaseException] = []

        async def acquire_once() -> None:
            async with backend._get_loop_lock(backend._op_locks):
                seen.append(backend._get_loop_lock(backend._op_locks))

        def runner() -> None:
            try:
                asyncio.run(acquire_once())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=runner)
        t2 = threading.Thread(target=runner)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == [], f"acquire raised across loops: {errors!r}"
        assert len(seen) == 2
        assert seen[0] is not seen[1]

    def test_native_lock_serialises_across_loops(self) -> None:
        import asyncio
        import threading
        import time as _time

        backend = SqliteVecBackend(
            db_path=":memory:",
            embedding_model="fake-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        active = 0
        max_active = 0
        active_lock = threading.Lock()
        errors: list[BaseException] = []

        def native_op() -> None:
            nonlocal active, max_active
            with active_lock:
                active += 1
                if active > max_active:
                    max_active = active
            _time.sleep(0.05)
            with active_lock:
                active -= 1

        async def critical() -> None:
            await backend._run_native(native_op)

        def runner() -> None:
            try:
                asyncio.run(critical())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=runner) for _ in range(4)]
        t0 = _time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = _time.perf_counter() - t0
        assert errors == [], f"native section raised: {errors!r}"
        assert max_active == 1, f"cross-loop critical sections overlapped: max={max_active}"
        assert elapsed >= 0.18, f"sections did not serialise: elapsed={elapsed:.3f}s"


# =============================================================================
# Protocol conformance
# =============================================================================


def test_satisfies_search_backend_protocol():
    from nexus.bricks.search.protocols import SearchBackend
    from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

    backend = SqliteVecBackend(db_path=":memory:")
    assert isinstance(backend, SearchBackend)


# =============================================================================
# Embedder kind detection + fastembed offline fallback
# =============================================================================


from nexus.bricks.search.sqlite_vec_backend import (  # noqa: E402
    _REMOTE_API_KEY_ENVS,
    DEFAULT_FASTEMBED_DIM,
    DEFAULT_FASTEMBED_MODEL,
    SqliteVecDimMismatchError,
    SqliteVecEmbedderMismatchError,
    _detect_embedder_kind,
)


class TestEmbedderDetection:
    def test_no_keys_no_flag_picks_fastembed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in _REMOTE_API_KEY_ENVS:
            monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv("NEXUS_OFFLINE_EMBED", raising=False)
        assert _detect_embedder_kind(api_key=None) == "fastembed"

    def test_explicit_api_key_arg_picks_litellm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in _REMOTE_API_KEY_ENVS:
            monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv("NEXUS_OFFLINE_EMBED", raising=False)
        assert _detect_embedder_kind(api_key="sk-test") == "litellm"

    def test_openai_env_picks_litellm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in _REMOTE_API_KEY_ENVS:
            monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv("NEXUS_OFFLINE_EMBED", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert _detect_embedder_kind(api_key=None) == "litellm"

    def test_offline_flag_overrides_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with a key present, the explicit offline flag wins.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("NEXUS_OFFLINE_EMBED", "1")
        assert _detect_embedder_kind(api_key="sk-test") == "fastembed"

    def test_fastembed_defaults_when_no_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in _REMOTE_API_KEY_ENVS:
            monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv("NEXUS_OFFLINE_EMBED", raising=False)
        monkeypatch.delenv("NEXUS_EMBEDDER", raising=False)
        b = SqliteVecBackend(db_path=":memory:")
        assert b._embedder_kind == "fastembed"
        assert b._embedding_model == DEFAULT_FASTEMBED_MODEL
        assert b._embedding_dim == DEFAULT_FASTEMBED_DIM

    def test_invalid_embedder_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown embedder kind"):
            SqliteVecBackend(db_path=":memory:", embedder="bogus")


class _FakeFastembedModel:
    """Stand-in for fastembed.TextEmbedding — returns deterministic vectors."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = vectors_by_text

    def embed(self, texts: list[str]):
        # fastembed yields numpy arrays; lists of floats also work since
        # the backend wraps with ``map(float, ...)``.
        for i, t in enumerate(texts):
            yield self._vectors_by_text.get(t, [float(i + 1), 0.0, 0.0, 0.0])


class TestFastembedRoundtrip:
    """End-to-end upsert/search using the fastembed code path."""

    @pytest.fixture
    async def fast_backend(self):
        vectors_by_text: dict[str, list[float]] = {}
        b = SqliteVecBackend(
            db_path=":memory:",
            embedding_dim=TEST_DIM,
            embedder="fastembed",
        )
        # Pre-fill the lazy slot so we never touch the real ONNX model
        # (or hit the network on a fresh CI runner).
        b._fastembed_model = _FakeFastembedModel(vectors_by_text)
        await b.startup()
        yield b, vectors_by_text
        await b.shutdown()

    @pytest.mark.asyncio
    async def test_upsert_then_search_via_fastembed(self, fast_backend) -> None:
        b, vectors_by_text = fast_backend
        vectors_by_text["alpha"] = [1.0, 0.0, 0.0, 0.0]
        vectors_by_text["beta"] = [0.0, 1.0, 0.0, 0.0]
        vectors_by_text["alpha?"] = [1.0, 0.0, 0.0, 0.0]

        n = await b.upsert(
            [
                {"path": "/a.md", "text": "alpha", "chunk_index": 0},
                {"path": "/b.md", "text": "beta", "chunk_index": 0},
            ],
            zone_id="z",
        )
        assert n == 2

        results = await b.search("alpha?", limit=2, zone_id="z")
        assert results, "fastembed-backed KNN returned nothing"
        assert results[0].path == "/a.md"
        assert results[0].vector_score > 0.0

    @pytest.mark.asyncio
    async def test_litellm_not_required_when_fastembed_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Construction with embedder='fastembed' must succeed even if
        litellm is missing — that's the whole point of the offline path."""
        real_litellm = sys.modules.pop("litellm", None)
        try:
            import builtins

            real_import = builtins.__import__

            def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
                if name == "litellm":
                    raise ImportError("simulated missing litellm")
                return real_import(name, *a, **kw)

            monkeypatch.setattr(builtins, "__import__", _fake_import)
            b = SqliteVecBackend(db_path=":memory:", embedding_dim=TEST_DIM, embedder="fastembed")
            assert b._embedder_kind == "fastembed"
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm


# =============================================================================
# Existing-table dim validation (Codex review, high)
# =============================================================================


class TestExistingTableDimValidation:
    """Codex review (high): if a SANDBOX user populated nexus.db with a
    1536-dim litellm backend and later restarted without an API key,
    the auto-detect picks the 384-dim fastembed embedder. Without
    validation the table stays at 1536 while every upsert packs 384
    floats — silent corruption / per-call errors.

    Validation must surface the mismatch loudly at startup so the
    factory's error handler can disable vec gracefully."""

    @pytest.mark.asyncio
    async def test_dim_match_starts_clean(self, mock_embed, tmp_path) -> None:
        db = str(tmp_path / "vec.sqlite")
        # First start at dim=4 — creates the table.
        b1 = SqliteVecBackend(db_path=db, embedding_dim=TEST_DIM, embedder="litellm")
        await b1.startup()
        await b1.shutdown()
        # Second start at the same dim must succeed.
        b2 = SqliteVecBackend(db_path=db, embedding_dim=TEST_DIM, embedder="litellm")
        await b2.startup()
        assert b2._started is True
        await b2.shutdown()

    @pytest.mark.asyncio
    async def test_dim_mismatch_raises_clear_error(self, mock_embed, tmp_path) -> None:
        db = str(tmp_path / "vec.sqlite")
        # Populate at dim=8 via one backend.
        b1 = SqliteVecBackend(db_path=db, embedding_dim=8, embedder="litellm")
        await b1.startup()
        await b1.shutdown()
        # New backend opens at dim=4 (mimics the litellm→fastembed
        # auto-detect switch after an API key disappears). Must raise.
        b2 = SqliteVecBackend(db_path=db, embedding_dim=4, embedder="litellm")
        with pytest.raises(SqliteVecDimMismatchError) as excinfo:
            await b2.startup()
        msg = str(excinfo.value)
        assert "dim=8" in msg and "dim=4" in msg
        # Resolution hint must point at the recovery path so users
        # aren't stuck guessing.
        assert "rebuild" in msg.lower() or "delete" in msg.lower()

    @pytest.mark.asyncio
    async def test_concurrent_startup_race_caught_by_post_check(self, mock_embed, tmp_path) -> None:
        """Codex review R2 (high): when two backends with different dims
        race on first start, the loser's pre-check sees an empty schema
        and proceeds to ``CREATE VIRTUAL TABLE IF NOT EXISTS`` — which
        becomes a no-op because the winner's table already exists at a
        different dim. Without the post-check the loser would mark
        itself started with the WRONG dim and silently fail every
        upsert/search forever.

        Simulating the race deterministically: patch ``_DIM_REGEX`` to
        report a fabricated dim of 999 on the post-check (the pre-check
        is unaffected because the table truly doesn't exist yet, so
        ``_read_existing_dim`` short-circuits without calling the
        regex). The post-check should fire and raise the ``race=True``
        variant of the dim-mismatch error.
        """
        from unittest.mock import MagicMock

        from nexus.bricks.search import sqlite_vec_backend as svb

        db = str(tmp_path / "vec.sqlite")
        b = SqliteVecBackend(db_path=db, embedding_dim=TEST_DIM, embedder="litellm")

        fake_match = MagicMock()
        fake_match.group.return_value = "999"
        fake_regex = MagicMock()
        fake_regex.search.return_value = fake_match

        with (
            patch.object(svb, "_DIM_REGEX", fake_regex),
            pytest.raises(SqliteVecDimMismatchError) as excinfo,
        ):
            await b.startup()

        msg = str(excinfo.value)
        # The fabricated existing dim must appear in the message so the
        # operator can see what they're up against.
        assert "dim=999" in msg, msg
        assert f"dim={TEST_DIM}" in msg, msg
        # The race-only branch of the error string must distinguish this
        # from the simpler "stale DB" mismatch — ops needs to know it
        # was a concurrent open, not a config drift.
        assert "concurrent" in msg.lower(), f"race=True error must mention concurrency: {msg}"
        # Backend must NOT be marked started after a failed startup.
        assert b._started is False

    def test_open_sets_busy_timeout_pragma(self, mock_embed, tmp_path) -> None:
        """Codex review R2: a 5s ``PRAGMA busy_timeout`` is what lets the
        loser of a concurrent CREATE wait for the winner instead of
        failing fast with SQLITE_BUSY. Verify the pragma is actually set
        on the open connection (regression guard for someone removing
        the line without realising it backstops the race fix)."""
        import asyncio

        async def _run() -> int:
            b = SqliteVecBackend(
                db_path=str(tmp_path / "vec.sqlite"),
                embedding_dim=TEST_DIM,
                embedder="litellm",
            )
            await b.startup()
            try:
                cur = b._conn.execute("PRAGMA busy_timeout")
                row = cur.fetchone()
                return int(row[0])
            finally:
                await b.shutdown()

        timeout_ms = asyncio.run(_run())
        assert timeout_ms >= 5000, (
            f"busy_timeout must be ≥5000ms to absorb concurrent first-start; got {timeout_ms}ms"
        )


class TestEmbedderIdentityValidation:
    """Codex review R3 (medium): the dim check alone misses the case
    where two different models produce the same dim (e.g. bge-small
    and all-MiniLM-L6 both yield 384-d). Mixing their outputs in the
    same vec0 table silently corrupts KNN ranking. The companion meta
    table must record the embedder kind + model name on first start
    and reject mismatched opens loudly."""

    @pytest.mark.asyncio
    async def test_same_embedder_starts_clean(self, mock_embed, tmp_path) -> None:
        db = str(tmp_path / "vec.sqlite")
        b1 = SqliteVecBackend(
            db_path=db,
            embedding_model="model-A",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b1.startup()
        await b1.shutdown()
        # Re-open with the same identity must succeed.
        b2 = SqliteVecBackend(
            db_path=db,
            embedding_model="model-A",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b2.startup()
        assert b2._started is True
        await b2.shutdown()

    @pytest.mark.asyncio
    async def test_same_dim_different_model_raises_clear_error(self, mock_embed, tmp_path) -> None:
        """Sequential mismatch: DB populated with model-A then re-opened
        with model-B at the same dim. Without the meta-table check, both
        would silently start against the same table and corrupt ranking."""
        db = str(tmp_path / "vec.sqlite")
        b1 = SqliteVecBackend(
            db_path=db,
            embedding_model="model-A",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b1.startup()
        await b1.shutdown()

        b2 = SqliteVecBackend(
            db_path=db,
            embedding_model="model-B",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        with pytest.raises(SqliteVecEmbedderMismatchError) as excinfo:
            await b2.startup()
        msg = str(excinfo.value)
        assert "model-A" in msg and "model-B" in msg, msg
        # Resolution hint must be present.
        assert "delete the DB" in msg or "pin the original embedder" in msg.lower(), msg
        # Race-flag-only language must NOT appear when this is a
        # steady-state mismatch (neither field matched ours).
        # However our heuristic flags race=True when at least one field
        # matches; same kind, different model → kind matches → race-ish.
        # Either is acceptable for the sequential case.

    @pytest.mark.asyncio
    async def test_same_model_different_kind_raises(self, mock_embed, tmp_path) -> None:
        """Cross-embedder mismatch: same dim + same model name but
        different ``kind`` (litellm vs fastembed) is still incompatible
        because the underlying embedding implementations differ."""
        db = str(tmp_path / "vec.sqlite")
        b1 = SqliteVecBackend(
            db_path=db,
            embedding_model="shared-name",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b1.startup()
        await b1.shutdown()

        # Force fastembed by patching out the fastembed import error path.
        b2 = SqliteVecBackend(
            db_path=db,
            embedding_model="shared-name",
            embedding_dim=TEST_DIM,
            embedder="fastembed",
        )
        with pytest.raises(SqliteVecEmbedderMismatchError):
            await b2.startup()

    @pytest.mark.asyncio
    async def test_pre_r3_populated_db_without_meta_refuses_to_bless(
        self, mock_embed, tmp_path
    ) -> None:
        """Codex review R4 (high): a pre-R3 database has rows in
        ``nexus_vec`` but no ``nexus_vec_meta`` table. Blindly INSERT-
        OR-IGNORE-ing the new backend's identity would tag those rows
        with whichever embedder happens to open the DB after upgrade —
        a silent corruption mode if the original embedder differed but
        shared the same dim. Startup must FAIL CLOSED on this state."""
        import sqlite3

        from nexus.bricks.search.sqlite_vec_backend import _VEC_META_TABLE, _VEC_TABLE

        db = str(tmp_path / "vec.sqlite")

        # Phase 1: simulate a pre-R3 build by populating nexus_vec at
        # TEST_DIM=4 WITHOUT creating the meta table. We use the real
        # backend to create the vec table cleanly, insert a row, then
        # drop the meta table to simulate the upgrade scenario.
        b1 = SqliteVecBackend(
            db_path=db,
            embedding_model="original-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b1.startup()
        await b1.upsert(
            [{"path": "/seed.md", "text": "seed", "chunk_index": 0}],
            zone_id="z",
        )
        await b1.shutdown()

        # Surgically remove the meta table to simulate a pre-R3 DB.
        # Load the sqlite-vec extension so vec0 virtual tables are
        # queryable from this prep connection.
        prep = sqlite3.connect(db)
        try:
            prep.enable_load_extension(True)
            sqlite_vec.load(prep)
            prep.enable_load_extension(False)
            prep.execute(f"DROP TABLE {_VEC_META_TABLE}")
            prep.commit()
            # Sanity: vec table still has the row.
            row = prep.execute(f"SELECT 1 FROM {_VEC_TABLE} LIMIT 1").fetchone()
            assert row is not None, "test setup invariant: vec table must still hold rows"
        finally:
            prep.close()

        # Phase 2: open with a DIFFERENT model at the same dim. Without
        # the R4 pre-INSERT check, the new backend would INSERT OR
        # IGNORE its own identity and silently bless the existing rows
        # as belonging to the new embedder. With the fix, startup must
        # raise rather than risk corrupted ranking.
        b2 = SqliteVecBackend(
            db_path=db,
            embedding_model="new-after-upgrade",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        with pytest.raises(SqliteVecEmbedderMismatchError) as excinfo:
            await b2.startup()

        msg = str(excinfo.value)
        assert "pre-R3" in msg or "no embedder identity recorded" in msg, msg
        assert "new-after-upgrade" in msg, (
            "error must surface the configured backend's identity so the "
            "operator can confirm what they're about to overwrite"
        )
        # Resolution hint must point at the rebuild path.
        assert "delete the DB" in msg or "drop" in msg.lower(), msg

    @pytest.mark.asyncio
    async def test_pre_r3_empty_db_without_meta_starts_clean(self, mock_embed, tmp_path) -> None:
        """Counter-test for R4 #3: when the pre-R3 vec table exists but
        is EMPTY, it's safe to register the current backend as the
        owner. Only populated tables must fail closed."""
        import sqlite3

        from nexus.bricks.search.sqlite_vec_backend import _VEC_META_TABLE

        db = str(tmp_path / "vec.sqlite")

        # Create an empty vec table at TEST_DIM and drop meta.
        b1 = SqliteVecBackend(
            db_path=db,
            embedding_model="some-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b1.startup()
        await b1.shutdown()

        prep = sqlite3.connect(db)
        try:
            prep.enable_load_extension(True)
            sqlite_vec.load(prep)
            prep.enable_load_extension(False)
            prep.execute(f"DROP TABLE {_VEC_META_TABLE}")
            prep.commit()
        finally:
            prep.close()

        # Re-open: empty vec table + missing meta = state (a) (brand
        # new for our purposes), startup must succeed.
        b2 = SqliteVecBackend(
            db_path=db,
            embedding_model="any-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        await b2.startup()
        assert b2._started is True
        await b2.shutdown()

    @pytest.mark.asyncio
    async def test_concurrent_first_start_same_dim_different_model(
        self, mock_embed, tmp_path
    ) -> None:
        """True race: two backends with the same dim but different model
        names race on first-start. INSERT OR IGNORE makes the first
        write win; the loser's post-write SELECT must reveal the stored
        identity diverges from its own and raise. Simulated
        deterministically via a pre-populated meta row that mimics the
        winner's INSERT having landed first."""
        import sqlite3

        from nexus.bricks.search.sqlite_vec_backend import _VEC_META_TABLE

        db = str(tmp_path / "vec.sqlite")
        # Mimic the "winner" backend having run first: create the meta
        # table + row before our backend's startup tries to insert.
        # (We don't need an actual nexus_vec table — the meta check runs
        # after CREATE IF NOT EXISTS so our backend's CREATE will fire.)
        prep = sqlite3.connect(db)
        try:
            prep.execute(
                f"CREATE TABLE {_VEC_META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            prep.executemany(
                f"INSERT INTO {_VEC_META_TABLE}(key, value) VALUES (?, ?)",
                [("embedder_kind", "litellm"), ("embedding_model", "winner-model")],
            )
            prep.commit()
        finally:
            prep.close()

        loser = SqliteVecBackend(
            db_path=db,
            embedding_model="loser-model",
            embedding_dim=TEST_DIM,
            embedder="litellm",
        )
        with pytest.raises(SqliteVecEmbedderMismatchError) as excinfo:
            await loser.startup()
        msg = str(excinfo.value)
        assert "winner-model" in msg and "loser-model" in msg, msg
        # Race-flagged language must appear because at least one field
        # (kind=litellm) matches between stored and configured.
        assert "concurrent" in msg.lower(), (
            f"same-dim race must surface the concurrent-open hint: {msg}"
        )

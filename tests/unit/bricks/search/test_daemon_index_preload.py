"""Tests for Issue #4269: index_preload option and index_load_ms phase timing."""

from __future__ import annotations

from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# DaemonConfig
# ---------------------------------------------------------------------------


def test_daemon_config_index_preload_defaults_false() -> None:
    from nexus.bricks.search.daemon import DaemonConfig

    assert DaemonConfig().index_preload_enabled is False


def test_daemon_config_index_preload_can_be_enabled() -> None:
    from nexus.bricks.search.daemon import DaemonConfig

    cfg = DaemonConfig(index_preload_enabled=True)
    assert cfg.index_preload_enabled is True


# ---------------------------------------------------------------------------
# Timing dict
# ---------------------------------------------------------------------------


def test_index_load_ms_in_backend_leg_timing_keys() -> None:
    from nexus.bricks.search.daemon import _BACKEND_LEG_TIMING_KEYS

    assert "index_load_ms" in _BACKEND_LEG_TIMING_KEYS


def test_empty_backend_timing_has_index_load_ms() -> None:
    from nexus.bricks.search.daemon import _empty_backend_timing

    t = _empty_backend_timing()
    assert "index_load_ms" in t
    assert t["index_load_ms"] == 0.0


# ---------------------------------------------------------------------------
# PgFtsBackend: keyword_search passes timing → index_load_ms populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_fts_keyword_search_populates_index_load_ms() -> None:
    """PgFtsBackend.keyword_search fills timing['index_load_ms'] when passed."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    # Build a fake engine whose connect() returns a connection that executes
    # a dummy row (path, chunk_text, chunk_index, score).
    fake_row = {
        "chunk_id": "c1",
        "path": "/a/b.md",
        "chunk_text": "hello world",
        "chunk_index": 0,
        "score": 1.0,
    }

    fake_result = MagicMock()
    fake_result.mappings.return_value.all.return_value = [fake_row]

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=fake_result)

    class _FakeConnCtx:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    fake_engine = MagicMock()
    fake_engine.connect.return_value = _FakeConnCtx()

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    timing: dict[str, float] = {}
    await backend.keyword_search("hello", "/", 10, "root", timing=timing)

    assert "index_load_ms" in timing
    assert timing["index_load_ms"] >= 0.0


@pytest.mark.asyncio
async def test_sqlite_fts_keyword_search_populates_index_load_ms(
    tmp_path: Any,
) -> None:
    """SqliteFtsBackend.keyword_search fills timing['index_load_ms'] when passed."""
    import sqlite3

    db_path = str(tmp_path / "test.db")
    # Bootstrap minimal schema so the JOIN query doesn't error.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_paths "
        "(path_id INTEGER PRIMARY KEY, virtual_path TEXT, zone_id TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_chunks "
        "(chunk_id TEXT, path_id INTEGER, chunk_text TEXT, chunk_index INTEGER)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts "
        "USING fts5(chunk_id UNINDEXED, path_id UNINDEXED, chunk_text, chunk_index UNINDEXED)"
    )
    conn.commit()
    conn.close()

    from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend

    backend = SqliteFtsBackend(db_path=db_path)

    timing: dict[str, float] = {}
    await backend.keyword_search("hello", "/", 10, "root", timing=timing)

    assert "index_load_ms" in timing
    assert timing["index_load_ms"] >= 0.0


# ---------------------------------------------------------------------------
# PgFtsBackend: preload() method exists and returns a float
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_fts_backend_has_preload_method() -> None:
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    fake_row = {
        "chunk_id": "c1",
        "path": "/a.md",
        "chunk_text": "warmup",
        "chunk_index": 0,
        "score": 1.0,
    }
    fake_result = MagicMock()
    fake_result.mappings.return_value.all.return_value = [fake_row]
    fake_result.fetchall.return_value = []  # warm-token sample: empty corpus

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=fake_result)

    class _FakeConnCtx:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    fake_engine = MagicMock()
    fake_engine.connect.return_value = _FakeConnCtx()

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    elapsed = await backend.preload()
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0


@pytest.mark.asyncio
async def test_pg_fts_preload_warms_real_hot_path_join() -> None:
    """preload() must fault the same pages a real query touches (#4269 review #4).

    The real keyword query JOINs file_paths and filters deleted_at; a warm that
    only hits document_chunks leaves the file_paths / path_id-FK indexes cold.
    """
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    executed: list[str] = []

    fake_result = MagicMock()
    fake_result.mappings.return_value.all.return_value = []
    fake_result.fetchall.return_value = []  # warm-token sample: empty corpus

    async def _capture(sql: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(sql))
        return fake_result

    fake_conn = AsyncMock()
    fake_conn.execute = _capture

    class _FakeConnCtx:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    fake_engine = MagicMock()
    fake_engine.connect.return_value = _FakeConnCtx()

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    await backend.preload()

    assert executed, "preload issued no query"
    assert any("file_paths" in s for s in executed), (
        "preload warm query must JOIN file_paths to warm the FK/path index too"
    )


@pytest.mark.asyncio
async def test_sqlite_fts_backend_has_preload_method(tmp_path: Any) -> None:
    import sqlite3

    db_path = str(tmp_path / "test2.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_paths "
        "(path_id INTEGER PRIMARY KEY, virtual_path TEXT, zone_id TEXT, deleted_at TEXT)"
    )
    # document_chunks is required: preload now mirrors keyword_search, which
    # JOINs document_chunks + file_paths and orders by bm25() (Codex R1 #2).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_chunks "
        "(chunk_id TEXT, path_id INTEGER, chunk_text TEXT, chunk_index INTEGER)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts "
        "USING fts5(chunk_id UNINDEXED, path_id UNINDEXED, chunk_text, chunk_index UNINDEXED)"
    )
    conn.commit()
    conn.close()

    from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend

    backend = SqliteFtsBackend(db_path=db_path)
    elapsed = await backend.preload()
    assert isinstance(elapsed, float)
    assert elapsed >= 0.0


def test_sqlite_preload_uses_match_and_bm25_not_rowid_scan() -> None:
    """SQLite preload must exercise the FTS5 MATCH/bm25 hot path, not a plain
    rowid scan (Codex R1 #2). Pairs with test_sqlite_fts_backend_has_preload_method,
    which proves this query actually executes against a real FTS5 schema."""
    import inspect

    from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend

    src = inspect.getsource(SqliteFtsBackend._run_warm_queries)
    assert "MATCH" in src, "preload must use FTS5 MATCH, not a plain rowid scan"
    assert "bm25(" in src, "preload must exercise bm25() ranking"
    assert "document_chunks_fts" in src


def test_sqlite_preload_installs_progress_handler_deadline() -> None:
    """SQLite preload must arm a sqlite3 progress handler so a hung warm query
    is actually aborted at the deadline, not merely abandoned by wait_for
    (Codex R9)."""
    import inspect

    from nexus.bricks.search.sqlite_fts_backend import SqliteFtsBackend

    src = inspect.getsource(SqliteFtsBackend.preload)
    assert "set_progress_handler" in src, "preload must arm a progress-handler deadline"
    # And it must be cleared afterward (finally) so the handler doesn't leak.
    assert "set_progress_handler(None, 0)" in src


@pytest.mark.asyncio
async def test_sqlite_preload_aborts_when_deadline_already_passed(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """With the deadline already in the past, a populated warm query is aborted
    by the progress handler rather than running to completion (Codex R9)."""
    import sqlite3

    from nexus.bricks.search import sqlite_fts_backend as sfb

    db_path = str(tmp_path / "abort.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE file_paths "
        "(path_id INTEGER PRIMARY KEY, virtual_path TEXT, zone_id TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE document_chunks "
        "(chunk_id TEXT, path_id INTEGER, chunk_text TEXT, chunk_index INTEGER)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE document_chunks_fts "
        "USING fts5(chunk_id UNINDEXED, path_id UNINDEXED, chunk_text, chunk_index UNINDEXED)"
    )
    # Populate enough rows that the warm executes many VM instructions.
    for i in range(2000):
        conn.execute(
            "INSERT INTO file_paths(path_id, virtual_path, zone_id, deleted_at) "
            "VALUES (?, ?, 'root', NULL)",
            (i, f"/doc{i}.md"),
        )
        conn.execute(
            "INSERT INTO document_chunks(chunk_id, path_id, chunk_text, chunk_index) "
            "VALUES (?, ?, ?, 0)",
            (f"c{i}", i, "revenue grew sharply this quarter"),
        )
        conn.execute(
            "INSERT INTO document_chunks_fts(rowid, chunk_id, path_id, chunk_text, chunk_index) "
            "VALUES (?, ?, ?, ?, 0)",
            (i + 1, f"c{i}", i, "revenue grew sharply this quarter"),
        )
    conn.commit()
    conn.close()

    # Deadline already elapsed + fire the handler every instruction → abort ASAP.
    monkeypatch.setattr(sfb, "_PRELOAD_WARM_TIMEOUT_SECONDS", -1.0)
    monkeypatch.setattr(sfb, "_PRELOAD_PROGRESS_INSTRUCTIONS", 1)

    backend = sfb.SqliteFtsBackend(db_path=db_path)

    with pytest.raises(sqlite3.OperationalError):
        await backend.preload()


# ---------------------------------------------------------------------------
# Daemon: _preload_search_index warms both backends (real method)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preload_search_index_warms_fts_and_vector_backends() -> None:
    """The real _preload_search_index awaits preload() on fts + vector backends."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon.stats = MagicMock()
    daemon._fts_backend = MagicMock()
    daemon._fts_backend.preload = AsyncMock(return_value=10.0)
    daemon._vector_backend = MagicMock()
    daemon._vector_backend.preload = AsyncMock(return_value=20.0)

    await daemon._preload_search_index()

    daemon._fts_backend.preload.assert_awaited_once()
    daemon._vector_backend.preload.assert_awaited_once()


@pytest.mark.asyncio
async def test_preload_search_index_is_fail_soft() -> None:
    """A backend.preload() that raises must not propagate out of
    _preload_search_index, and must be recorded as a FAILED preload
    (index_preload_ok=False) so telemetry doesn't imply success (Codex R4)."""
    from nexus.bricks.search.daemon import DaemonConfig, DaemonStats, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon.stats = DaemonStats()
    daemon._fts_backend = MagicMock()
    daemon._fts_backend.preload = AsyncMock(side_effect=RuntimeError("volume gone"))
    # Both backends present so the query path (and therefore preload) is active.
    daemon._vector_backend = MagicMock()
    daemon._vector_backend.preload = AsyncMock(return_value=5.0)

    # Must not raise.
    await daemon._preload_search_index()

    # ...but the failure must be visible, not reported as a successful warm.
    assert daemon.stats.index_preload_ok is False


@pytest.mark.asyncio
async def test_preload_search_index_skips_backend_without_preload() -> None:
    """Backends lacking a preload() attribute are skipped, not errored on."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon.stats = MagicMock()
    # Both backends present (query path active) but neither exposes preload().
    daemon._fts_backend = object()
    daemon._vector_backend = object()

    # Must not raise (no preload attr → skipped).
    await daemon._preload_search_index()


@pytest.mark.asyncio
async def test_preload_skipped_when_backend_query_path_inactive() -> None:
    """If the new-backend query path is inactive (vector backend absent, e.g.
    SQLite without sqlite_vec), preload must NOT warm a backend queries bypass,
    and must report not-attempted (index_preload_ok=None) — not a misleading
    success (Codex R7)."""
    from nexus.bricks.search.daemon import DaemonConfig, DaemonStats, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon.stats = DaemonStats()
    daemon._fts_backend = MagicMock()
    daemon._fts_backend.preload = AsyncMock(return_value=10.0)
    daemon._vector_backend = None  # inactive query path

    await daemon._preload_search_index()

    daemon._fts_backend.preload.assert_not_awaited()  # bypassed backend not warmed
    assert daemon.stats.index_preload_ok is None  # not-attempted, not success


@pytest.mark.asyncio
async def test_preload_search_index_bounds_hung_backend_with_timeout(
    monkeypatch: Any,
) -> None:
    """A preload() that hangs must be bounded by a timeout, not block startup forever.

    Issue #4269 review (finding #1): preload targets slow/flaky network volumes —
    the exact case where a backend read can hang. _preload_search_index must apply
    a per-backend timeout and stay fail-soft when it fires.
    """
    import asyncio

    from nexus.bricks.search import daemon as daemon_mod
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    # Shrink the timeout so the test is fast.
    monkeypatch.setattr(daemon_mod, "_PRELOAD_TIMEOUT_SECONDS", 0.05)

    async def _hang() -> float:
        await asyncio.sleep(10)
        return 0.0

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon.stats = MagicMock()
    daemon._fts_backend = MagicMock()
    daemon._fts_backend.preload = _hang
    # Both backends present so the query path (and preload) is active.
    daemon._vector_backend = MagicMock()
    daemon._vector_backend.preload = AsyncMock(return_value=1.0)

    # Must return promptly (well under the 10s hang) and not raise.
    await asyncio.wait_for(daemon._preload_search_index(), timeout=2.0)


def test_preload_warm_token_is_not_a_native_stopword() -> None:
    """The preload warm token must survive _native_fts_query (non-empty).

    Issue #4269 review (finding #2): a stopword warm token (e.g. "the") reduces to
    an empty to_tsquery in the native-FTS fallback, faulting in no index pages.
    """
    from nexus.bricks.search.pg_fts_backend import _PRELOAD_WARM_TOKEN, _native_fts_query

    assert _native_fts_query(_PRELOAD_WARM_TOKEN) != ""


def test_first_corpus_token_accepts_short_acronym_tokens() -> None:
    """The sampler must not be stricter than the query analyzers (Codex R8):
    a searchable short-token corpus like "AI ML UX" yields a token, not None."""
    from nexus.bricks.search.pg_fts_backend import _first_corpus_token

    # len>=2 acronyms are searchable (native FTS keeps len>=2; FTS5 has none).
    assert _first_corpus_token("AI ML UX") == "ai"
    # Prefers a non-stopword over a leading stopword.
    assert _first_corpus_token("the quarterly report") == "quarterly"
    # All-stopword text still yields a token (FTS5 matches stopwords); the
    # caller's matched-row check is the per-backend effectiveness gate.
    assert _first_corpus_token("the an of") == "the"
    # Only when there is no >=2-char token at all do we get None.
    assert _first_corpus_token("a b 1") is None


def test_first_corpus_token_accepts_unicode_terms() -> None:
    """The sampler must accept non-ASCII indexed terms (Codex R9): accented
    Latin and CJK are searchable, so they must not be falsely rejected."""
    from nexus.bricks.search.pg_fts_backend import _first_corpus_token

    # Accented Latin (the common Unicode case).
    assert _first_corpus_token("café résumé") == "café"
    # CJK run is a single >=2-char token, not None.
    assert _first_corpus_token("東京 データ") is not None


# ---------------------------------------------------------------------------
# get_stats surfaces preload telemetry (#4269 review #12)
# ---------------------------------------------------------------------------


def test_get_stats_surfaces_index_preload_fields() -> None:
    """index_preload_time_ms and index_preload_enabled must be visible in get_stats."""
    from nexus.bricks.search.daemon import DaemonConfig, DaemonStats, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon.stats = DaemonStats(index_preload_time_ms=12.5)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon._fts_backend = None
    daemon._vector_backend = None

    stats = daemon.get_stats()

    assert stats["index_preload_time_ms"] == 12.5
    assert stats["index_preload_enabled"] is True


# ---------------------------------------------------------------------------
# SearchBackend Protocol honesty: keyword_search accepts timing= (#4269 review #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_backends_keyword_search_accepts_timing() -> None:
    """The vector backends' keyword_search stubs must accept the timing= kwarg too,
    so the SearchBackend Protocol signature stays structurally honest."""
    from nexus.bricks.search.pg_vector_backend import PgVectorBackend
    from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

    pgv: Any = PgVectorBackend.__new__(PgVectorBackend)
    assert await pgv.keyword_search("q", "/", 5, "root", timing={}) == []

    sv: Any = SqliteVecBackend.__new__(SqliteVecBackend)
    assert await sv.keyword_search("q", "/", 5, "root", timing={}) == []


def test_search_backend_protocol_keyword_search_has_timing_param() -> None:
    """The SearchBackend Protocol declares the optional timing= keyword."""
    import inspect

    from nexus.bricks.search.protocols import SearchBackend

    sig = inspect.signature(SearchBackend.keyword_search)
    assert "timing" in sig.parameters


# ---------------------------------------------------------------------------
# Zero-result keyword search must still report index_load_ms (#4269 e2e bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_zero_result_preserves_index_load_ms() -> None:
    """A 0-result keyword query — the issue's exact measured scenario — must
    still surface index_load_ms.

    Regression: when the indexed backend returns empty, the keyword path used
    to clobber last_search_timing with a bare {backend_ms, rerank_ms} dict,
    dropping the index_load_ms the backend had recorded. Found via `nexus up`
    e2e (type=keyword&q=revenue → index_load_ms missing).
    """
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon.last_search_timing = {}

    async def _search_via_backends(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        # Mirror the real backend: record leg timings (incl. index_load_ms)
        # then return EMPTY so the daemon falls through to the legacy stack.
        self.last_search_timing = {
            "backend_ms": 5.0,
            "embed_ms": 0.0,
            "keyword_ms": 4.0,
            "page_keyword_ms": 0.0,
            "vector_ms": 0.0,
            "fusion_ms": 0.0,
            "rerank_ms": 0.0,
            "index_load_ms": 2.0,
        }
        return []

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        # Legacy fallback also finds nothing (empty corpus) but still spends
        # measurable time on the cold read — that latency must be visible.
        import asyncio

        await asyncio.sleep(0.02)
        return []

    def _track_latency(self: Any, latency_ms: float) -> None:
        pass

    async def _attach_path_contexts(
        self: Any, results: list[SearchResult], *, zone_id: str
    ) -> None:
        pass

    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)

    results = await daemon._search_on_current_loop(
        "revenue", search_type="keyword", limit=5, zone_id="root"
    )

    t = results.search_timing
    # Indexed legs preserved.
    assert t.get("index_load_ms") == 2.0
    assert t.get("keyword_ms") == 4.0
    # The ~20ms fallback ran even though it returned 0 results — its latency
    # must be recorded as fallback_ms and folded into backend_ms, not left as an
    # unexplained gap (Codex R5, the issue's exact cold 0-result scenario).
    assert t.get("fallback_ms", 0.0) > 15.0
    assert t.get("backend_ms", 0.0) > 15.0


@pytest.mark.asyncio
async def test_keyword_nonempty_fallback_folds_latency_not_just_indexed_attempt() -> None:
    """When the indexed backend is empty but the legacy keyword fallback returns
    hits, the timing must reflect the fallback work — not only the empty indexed
    attempt (Codex R1 #1). index_load_ms is preserved; backend_ms/keyword_ms
    grow to include the fallback's latency.
    """
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon.last_search_timing = {}

    async def _search_via_backends(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        self.last_search_timing = {
            "backend_ms": 5.0,
            "embed_ms": 0.0,
            "keyword_ms": 4.0,
            "page_keyword_ms": 0.0,
            "vector_ms": 0.0,
            "fusion_ms": 0.0,
            "rerank_ms": 0.0,
            "index_load_ms": 2.0,
        }
        return []  # indexed attempt empty → fall through to legacy

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        # Legacy fallback (e.g. Zoekt/BM25S) DOES find a hit the indexed path
        # missed. Take a measurable delay so the folded latency is unambiguous.
        import asyncio

        await asyncio.sleep(0.02)
        return [
            SearchResult(
                path="/legacy_hit.md",
                chunk_text="found via legacy keyword fallback",
                score=3.0,
                chunk_index=0,
                search_type="keyword",
            )
        ]

    def _track_latency(self: Any, latency_ms: float) -> None:
        pass

    async def _attach_path_contexts(
        self: Any, results: list[SearchResult], *, zone_id: str
    ) -> None:
        pass

    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._keyword_search = MethodType(_keyword_search, daemon)
    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)

    results = await daemon._search_on_current_loop(
        "revenue", search_type="keyword", limit=5, zone_id="root"
    )

    # The fallback produced the result.
    assert [r.path for r in results] == ["/legacy_hit.md"]
    t = results.search_timing
    # index_load_ms + the indexed keyword leg are preserved unchanged — the
    # fallback work is NOT misattributed to them (Codex R2).
    assert t.get("index_load_ms") == 2.0
    assert t.get("keyword_ms") == 4.0
    # The ~20ms fallback is recorded as a distinct fallback_ms leg and folded
    # into backend_ms. Without the fold backend_ms would stay exactly 5.0.
    assert t.get("fallback_ms", 0.0) > 15.0
    assert t.get("backend_ms", 0.0) > 15.0


@pytest.mark.asyncio
async def test_hybrid_nonempty_fallback_records_fallback_ms_without_misattribution() -> None:
    """Hybrid degraded path: indexed backend empty, _hybrid_search fallback
    returns hits. fallback_ms records the fallback work; indexed legs
    (vector_ms/fusion_ms) are NOT inflated (Codex R2 #1)."""
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._fts_backend = object()
    daemon._vector_backend = object()
    daemon.last_search_timing = {}

    async def _search_via_backends(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        self.last_search_timing = {
            "backend_ms": 6.0,
            "embed_ms": 1.0,
            "keyword_ms": 2.0,
            "page_keyword_ms": 1.0,
            "vector_ms": 2.0,
            "fusion_ms": 0.5,
            "rerank_ms": 0.0,
            "index_load_ms": 1.5,
        }
        return []  # indexed hybrid empty → fall through

    async def _hybrid_search(self: Any, *args: Any, **kwargs: Any) -> list[SearchResult]:
        import asyncio

        await asyncio.sleep(0.02)
        return [
            SearchResult(
                path="/hybrid_fallback.md",
                chunk_text="legacy hybrid hit",
                score=2.0,
                chunk_index=0,
                search_type="hybrid",
            )
        ]

    def _track_latency(self: Any, latency_ms: float) -> None:
        pass

    async def _attach_path_contexts(
        self: Any, results: list[SearchResult], *, zone_id: str
    ) -> None:
        pass

    daemon._search_via_backends = MethodType(_search_via_backends, daemon)
    daemon._hybrid_search = MethodType(_hybrid_search, daemon)
    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)

    results = await daemon._search_on_current_loop(
        "revenue", search_type="hybrid", limit=5, zone_id="root"
    )

    assert [r.path for r in results] == ["/hybrid_fallback.md"]
    t = results.search_timing
    # Indexed legs preserved exactly — fallback work not misattributed.
    assert t.get("index_load_ms") == 1.5
    assert t.get("vector_ms") == 2.0
    assert t.get("fusion_ms") == 0.5
    # Fallback work recorded distinctly and folded into backend_ms (~20ms).
    assert t.get("fallback_ms", 0.0) > 15.0
    assert t.get("backend_ms", 0.0) > 15.0


def test_pg_preload_uses_ranked_bm25_query() -> None:
    """PG preload must mirror the ranked query (paradedb.score + ORDER BY), not
    a bare match scan, so the scoring/ranking path is warmed (Codex R2 #3)."""
    import inspect

    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    bm25_src = inspect.getsource(PgFtsBackend._bm25_warm_query)
    assert _BM25_SCORE_FN_IN(bm25_src)
    assert "ORDER BY score DESC" in bm25_src
    # native fallback warm also ranks (ts_rank_cd + ORDER BY)
    native_src = inspect.getsource(PgFtsBackend._native_warm_query)
    assert "ts_rank_cd" in native_src
    assert "ORDER BY score DESC" in native_src


def _BM25_SCORE_FN_IN(src: str) -> bool:
    # The warm query interpolates the _BM25_SCORE_FN constant ("paradedb.score").
    from nexus.bricks.search.pg_fts_backend import _BM25_SCORE_FN

    return _BM25_SCORE_FN in src or "_BM25_SCORE_FN" in src


# ---------------------------------------------------------------------------
# Preload failure is observable, not reported as a successful warm (Codex R4)
# ---------------------------------------------------------------------------


def _conn_ctx(fake_conn: Any) -> Any:
    class _Ctx:
        async def __aenter__(self) -> Any:
            return fake_conn

        async def __aexit__(self, *_: Any) -> None:
            pass

    return _Ctx()


@pytest.mark.asyncio
async def test_pg_preload_propagates_genuine_failure() -> None:
    """A genuine warm failure (e.g. cold-volume read error) must propagate out
    of preload(), not be swallowed into a successful-looking elapsed (Codex R4)."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(side_effect=RuntimeError("cold volume read failed"))
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    with pytest.raises(RuntimeError, match="cold volume read failed"):
        await backend.preload()


@pytest.mark.asyncio
async def test_pg_preload_missing_bm25_warms_native_fallback() -> None:
    """When the BM25 warm hits a missing-extension error, preload must flip
    _bm25_available AND warm the native-FTS fallback path the first real query
    will use — not leave it cold (Codex R4)."""
    from sqlalchemy.exc import ProgrammingError

    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    executed: list[str] = []

    async def _execute(sql: Any, params: Any = None) -> Any:
        s = str(sql)
        executed.append(s)
        # Simulate a missing pg_search extension on the BM25 match query only
        # (keyed on the @@@ operator, robust to the extra warm-token sample).
        if "@@@" in s:
            raise ProgrammingError("stmt", {}, Exception('schema "paradedb" does not exist'))
        res = MagicMock()
        res.fetchall.return_value = []  # warm-token sample: empty corpus
        return res  # sample SELECT + native fallback warm succeed

    fake_conn = AsyncMock()
    fake_conn.execute = _execute
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    elapsed = await backend.preload()

    assert isinstance(elapsed, float)
    assert backend._bm25_available is False  # flipped for the first real query
    # A BM25 (@@@) attempt happened AND a native ranked warm ran afterwards.
    assert any("@@@" in s for s in executed), "BM25 warm must have been attempted"
    assert any("ts_rank_cd" in s or "to_tsquery" in s for s in executed), (
        "native fallback warm must run after BM25 missing"
    )


@pytest.mark.asyncio
async def test_pg_preload_raises_when_corpus_has_rows_but_no_indexable_token() -> None:
    """A non-empty corpus whose sampled rows yield NO indexable token (no
    >=2-char alphanumeric token at all) must NOT report a successful warm —
    preload raises so the daemon records index_preload_ok=False instead of a
    zero-hit false positive (Codex R6)."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    # Sample returns rows, but every chunk_text has only single-char/symbol
    # tokens — nothing the analyzers (min length 2) can index.
    sample_result = MagicMock()
    sample_result.fetchall.return_value = [("a b c",), ("x - y",)]

    async def _execute(sql: Any, params: Any = None) -> Any:
        return sample_result

    fake_conn = AsyncMock()
    fake_conn.execute = _execute
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    with pytest.raises(RuntimeError, match="no indexable token"):
        await backend.preload()


@pytest.mark.asyncio
async def test_pg_preload_uses_sampled_real_token() -> None:
    """When the corpus has a real token, preload warms with THAT token (so the
    query matches live rows), not the fixed fallback (Codex R5/R6)."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    sample_result = MagicMock()
    sample_result.fetchall.return_value = [("quarterly revenue report",)]
    warm_params: list[Any] = []

    async def _execute(sql: Any, params: Any = None) -> Any:
        if "@@@" in str(sql):
            warm_params.append(params)
        return sample_result

    fake_conn = AsyncMock()
    fake_conn.execute = _execute
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    await backend.preload()

    # The BM25 warm used a real corpus token ("quarterly"), not "data".
    assert warm_params and warm_params[0]["q"] == "quarterly"


@pytest.mark.asyncio
async def test_pg_preload_fails_closed_on_sampling_error() -> None:
    """A warm-token SAMPLING error must NOT be swallowed into a successful warm
    (Codex R10): it propagates so the daemon records index_preload_ok=False —
    not a zero-hit fixed-token warm masquerading as success."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    async def _execute(sql: Any, params: Any = None) -> Any:
        if "LIMIT 20" in str(sql):  # the warm-token sample query
            raise RuntimeError("sample read stalled")
        r = MagicMock()
        r.fetchall.return_value = []
        return r

    fake_conn = AsyncMock()
    fake_conn.execute = _execute
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    with pytest.raises(RuntimeError, match="sample read stalled"):
        await backend.preload()


@pytest.mark.asyncio
async def test_pg_preload_raises_when_warm_matches_no_rows_for_real_token() -> None:
    """Even with a real sampled token, if the warm query matches 0 rows (stale
    index / analyzer mismatch) preload must fail rather than report a successful
    warm over a still-cold index (Codex R7)."""
    from nexus.bricks.search.pg_fts_backend import PgFtsBackend

    def _sample_result() -> Any:
        r = MagicMock()
        r.fetchall.return_value = [("quarterly revenue report",)]  # corpus token
        return r

    def _empty_result() -> Any:
        r = MagicMock()
        r.fetchall.return_value = []  # warm matched nothing
        return r

    async def _execute(sql: Any, params: Any = None) -> Any:
        # Sample query (LIMIT 20, no @@@) returns a real token; the BM25 warm
        # (@@@) matches zero rows — a stale/mismatched index.
        return _empty_result() if "@@@" in str(sql) else _sample_result()

    fake_conn = AsyncMock()
    fake_conn.execute = _execute
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = lambda: _conn_ctx(fake_conn)

    backend = PgFtsBackend(engine=fake_engine)
    backend._bm25_available = True

    with pytest.raises(RuntimeError, match="matched no rows"):
        await backend.preload()


def test_get_stats_surfaces_index_preload_ok() -> None:
    """get_stats exposes index_preload_ok so operators can tell a real warm from
    a failed one (Codex R4)."""
    from nexus.bricks.search.daemon import DaemonConfig, DaemonStats, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon.stats = DaemonStats(index_preload_time_ms=3.0, index_preload_ok=False)
    daemon.config = DaemonConfig(index_preload_enabled=True)
    daemon._fts_backend = None
    daemon._vector_backend = None

    stats = daemon.get_stats()
    assert stats["index_preload_ok"] is False

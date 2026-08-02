"""Unit tests for explicit-index bounded projection wait (Issue #4566).

``POST /api/v2/search/index`` immediately after ``files.write`` used to land
inside the operation-log consumer's ``file_paths`` projection lag: the
``path_id`` lookup found no row, the doc was silently dropped, and the call
returned ``count=0`` with no way for the client to distinguish "indexed"
from "lost". These tests pin the fixed contract:

- a row that lands within the bounded wait window is indexed;
- a row that never lands is reported in ``skipped`` (not silently dropped);
- a daemon without a DB session skips immediately without burning the wait;
- a successful write stamps ``indexed_content_id`` so mutation consumers
  treat the content version as covered (clobber-race follow-up).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

# =============================================================================
# Fakes
# =============================================================================


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _Session:
    """Async session that replays scripted SELECT rows and records UPDATEs."""

    def __init__(self, rows_by_call: list[tuple[Any, ...] | None]) -> None:
        self._rows_by_call = rows_by_call
        self.select_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.commits = 0

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        if sql.lstrip().upper().startswith("UPDATE"):
            self.update_calls.append(dict(params or {}))
            return _Result(None)
        if "document_chunks" in sql:
            # Post-index guard watermark: stable so the guard never repairs.
            return _Result((1, "t1"))
        self.select_calls.append(dict(params or {}))
        idx = min(len(self.select_calls) - 1, len(self._rows_by_call) - 1)
        return _Result(self._rows_by_call[idx])

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self, rows_by_call: list[tuple[Any, ...] | None]) -> None:
        self.session = _Session(rows_by_call)

    def __call__(self) -> _Session:
        return self.session


class _PipelineResult:
    def __init__(self, chunks_indexed: int = 1) -> None:
        self.error: str | None = None
        self.chunks_indexed = chunks_indexed


class _Pipeline:
    def __init__(self, chunks_indexed: int = 1) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._chunks_indexed = chunks_indexed

    async def index_document(self, path: str, content: str, path_id: str) -> _PipelineResult:
        self.calls.append((path, content, path_id))
        return _PipelineResult(self._chunks_indexed)


def _make_daemon(
    rows_by_call: list[tuple[Any, ...] | None] | None,
    *,
    attempts: int = 3,
    delay: float = 0.001,
    chunks_indexed: int = 1,
) -> tuple[SearchDaemon, _Pipeline, _SessionFactory | None]:
    config = DaemonConfig(
        index_path_wait_attempts=attempts,
        index_path_wait_seconds=delay,
    )
    factory = _SessionFactory(rows_by_call) if rows_by_call is not None else None
    daemon = SearchDaemon(config, async_session_factory=factory)
    pipeline = _Pipeline(chunks_indexed)
    daemon._indexing_pipeline = cast(Any, pipeline)
    daemon._initialized = True
    return daemon, pipeline, factory


@pytest.fixture
def sleep_recorder(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record daemon wait sleeps without actually waiting."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return delays


# =============================================================================
# Tests
# =============================================================================


class TestExplicitIndexPathWait:
    @pytest.mark.asyncio
    async def test_row_landing_within_wait_window_is_indexed(
        self, sleep_recorder: list[float]
    ) -> None:
        """Write-then-index: row missing on first lookup, lands on the second."""
        daemon, pipeline, factory = _make_daemon([None, ("pid-1", "cid-1", 100)], attempts=3)

        result = await daemon.index_documents(
            [{"id": "1", "text": "zuluxx probe", "path": "/probe.md"}], zone_id="root"
        )

        assert result.indexed == 1
        assert result.skipped == []
        assert pipeline.calls == [("/probe.md", "zuluxx probe", "pid-1")]
        # One wait round was needed before the row landed.
        assert len(sleep_recorder) == 1

    @pytest.mark.asyncio
    async def test_row_never_landing_is_reported_skipped(self, sleep_recorder: list[float]) -> None:
        """Synthetic doc with no backing file: surfaced in skipped, not dropped."""
        daemon, pipeline, factory = _make_daemon([None], attempts=2)

        result = await daemon.index_documents(
            [{"id": "1", "text": "ghost text", "path": "/ghost.md"}], zone_id="root"
        )

        assert result.indexed == 0
        assert result.skipped == ["/ghost.md"]
        assert pipeline.calls == []
        # Initial lookup + one per wait round.
        assert factory is not None
        assert len(factory.session.select_calls) == 3
        assert len(sleep_recorder) == 2

    @pytest.mark.asyncio
    async def test_mixed_batch_indexes_resolved_and_skips_missing(
        self, sleep_recorder: list[float]
    ) -> None:
        daemon, pipeline, _ = _make_daemon(
            # /ok.md resolves immediately; /ghost.md never does.
            [("pid-ok", "cid-ok", 100), None, None, None],
            attempts=2,
        )

        result = await daemon.index_documents(
            [
                {"id": "1", "text": "ok text", "path": "/ok.md"},
                {"id": "2", "text": "ghost text", "path": "/ghost.md"},
            ],
            zone_id="root",
        )

        assert result.indexed == 1
        assert result.skipped == ["/ghost.md"]
        assert [c[2] for c in pipeline.calls] == ["pid-ok"]

    @pytest.mark.asyncio
    async def test_no_session_skips_immediately_without_waiting(
        self, sleep_recorder: list[float]
    ) -> None:
        """No DB session → resolution can never succeed; don't burn the wait."""
        daemon, pipeline, _ = _make_daemon(None, attempts=5)

        result = await daemon.index_documents(
            [{"id": "1", "text": "text", "path": "/a.md"}], zone_id="root"
        )

        assert result.indexed == 0
        assert result.skipped == ["/a.md"]
        assert sleep_recorder == []

    @pytest.mark.asyncio
    async def test_wait_disabled_with_zero_attempts(self, sleep_recorder: list[float]) -> None:
        daemon, pipeline, _ = _make_daemon([None], attempts=0)

        result = await daemon.index_documents(
            [{"id": "1", "text": "text", "path": "/a.md"}], zone_id="root"
        )

        assert result.indexed == 0
        assert result.skipped == ["/a.md"]
        assert sleep_recorder == []

    @pytest.mark.asyncio
    async def test_stats_counters_track_waits_and_skips(self, sleep_recorder: list[float]) -> None:
        daemon, _, _ = _make_daemon([None, ("pid-1", "cid-1", 100)], attempts=1)

        await daemon.index_documents(
            [{"id": "1", "text": "late row", "path": "/late.md"}], zone_id="root"
        )
        assert daemon.stats.explicit_index_path_waits == 1
        assert daemon.stats.explicit_index_skipped_no_path == 0

        daemon2, _, _ = _make_daemon([None], attempts=1)
        await daemon2.index_documents(
            [{"id": "1", "text": "ghost", "path": "/ghost.md"}], zone_id="root"
        )
        assert daemon2.stats.explicit_index_skipped_no_path == 1

    def test_config_defaults_and_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = DaemonConfig()
        assert config.index_path_wait_attempts == 5
        assert config.index_path_wait_seconds == 2.0

        monkeypatch.setenv("NEXUS_SEARCH_INDEX_PATH_WAIT_ATTEMPTS", "7")
        monkeypatch.setenv("NEXUS_SEARCH_INDEX_PATH_WAIT_SECONDS", "0.5")
        overridden = DaemonConfig()
        assert overridden.index_path_wait_attempts == 7
        assert overridden.index_path_wait_seconds == 0.5


class TestExplicitIndexStampsCoveredVersion:
    """#4566 follow-up: successful explicit index marks the content version
    as covered (indexed_content_id = content_id) so mutation consumers do
    not clobber the caller-provided text with raw blob bytes."""

    @pytest.mark.asyncio
    async def test_successful_index_stamps_indexed_content_id(self) -> None:
        daemon, _, factory = _make_daemon([("pid-1", "cid-1", 100)], attempts=0)

        await daemon.index_documents(
            [{"id": "1", "text": "extracted pdf text", "path": "/doc.pdf"}], zone_id="root"
        )

        assert factory is not None
        assert len(factory.session.update_calls) == 1
        stamp = factory.session.update_calls[0]
        assert stamp["pid"] == "pid-1"
        assert stamp["cid"] == "cid-1"
        # CAS must carry the updated_at observed at resolution time so a
        # mid-flight rewrite (which bumps updated_at) voids the stamp.
        assert stamp["observed_updated_at"] == 100
        assert factory.session.commits == 1

    @pytest.mark.asyncio
    async def test_no_stamp_when_row_has_no_content_id(self) -> None:
        daemon, _, factory = _make_daemon([("pid-1", None, 100)], attempts=0)

        result = await daemon.index_documents(
            [{"id": "1", "text": "text", "path": "/a.md"}], zone_id="root"
        )

        assert result.indexed == 1
        assert factory is not None
        assert factory.session.update_calls == []

    @pytest.mark.asyncio
    async def test_no_stamp_when_pipeline_indexed_zero_chunks(self) -> None:
        """Scope-gated no-op (chunks_indexed=0, no error) must NOT stamp —
        stamping would wrongly block the FTS consumer's naive-chunk
        fallback for out-of-scope paths."""
        daemon, pipeline, factory = _make_daemon(
            [("pid-1", "cid-1", 100)], attempts=0, chunks_indexed=0
        )

        result = await daemon.index_documents(
            [{"id": "1", "text": "text", "path": "/outside-scope.md"}], zone_id="root"
        )

        assert result.indexed == 1  # pre-existing contract: no error → counted
        assert pipeline.calls != []
        assert factory is not None
        assert factory.session.update_calls == []

    @pytest.mark.asyncio
    async def test_stamp_failure_is_fail_soft(self) -> None:
        """A stamp error must not fail the request — the text IS indexed."""
        daemon, _, factory = _make_daemon([("pid-1", "cid-1", 100)], attempts=0)
        assert factory is not None

        original_execute = factory.session.execute

        async def _failing_execute(stmt: Any, params: dict[str, Any] | None = None) -> _Result:
            if str(stmt).lstrip().upper().startswith("UPDATE"):
                raise RuntimeError("db down")
            return await original_execute(stmt, params)

        # setattr keeps mypy quiet without a type:ignore (project policy).
        setattr(factory.session, "execute", _failing_execute)  # noqa: B010

        result = await daemon.index_documents(
            [{"id": "1", "text": "text", "path": "/a.md"}], zone_id="root"
        )
        assert result.indexed == 1
        assert result.skipped == []


class _GuardSession:
    """Session for the guard: routes each query shape to scripted answers."""

    def __init__(
        self,
        watermarks: list[tuple[int, Any] | None],
        row_version: tuple[Any, Any] | None,
    ) -> None:
        self._watermarks = watermarks
        self._wm_idx = 0
        self._row_version = row_version
        self.update_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_GuardSession":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        if sql.lstrip().upper().startswith("UPDATE"):
            self.update_calls.append(dict(params or {}))
            return _Result(None)
        if "document_chunks" in sql:
            idx = min(self._wm_idx, len(self._watermarks) - 1)
            self._wm_idx += 1
            return _Result(self._watermarks[idx])
        return _Result(self._row_version)

    async def commit(self) -> None:
        return None


class TestExplicitIndexGuard:
    """#4566: a writer already past its covered-gate check when the stamp
    lands can interleave its blob replace with ours (observed live: two
    chunk writes 3ms apart, mixed set). The guard watches the chunk
    watermark and re-replaces the raced write with the caller's text."""

    def _guard_daemon(
        self,
        watermarks: list[tuple[int, Any] | None],
        row_version: tuple[Any, Any] | None,
    ) -> tuple[SearchDaemon, _Pipeline, _GuardSession]:
        config = DaemonConfig(index_path_wait_attempts=2, index_path_wait_seconds=0.001)
        session = _GuardSession(watermarks, row_version)
        daemon = SearchDaemon(config, async_session_factory=lambda: session)
        pipeline = _Pipeline()
        daemon._indexing_pipeline = cast(Any, pipeline)
        daemon._initialized = True
        return daemon, pipeline, session

    @pytest.mark.asyncio
    async def test_guard_repairs_raced_chunk_write(self, sleep_recorder: list[float]) -> None:
        # Watermark moves off the baseline (a foreign write landed), and the
        # row still holds the version we indexed -> repair + re-stamp.
        daemon, pipeline, session = self._guard_daemon(
            watermarks=[(3, "t2"), (2, "t3")],
            row_version=("cid-1", 100),
        )

        await daemon._explicit_index_guard(
            "/doc.pdf", "explicit text", ("pid-1", "cid-1", 100), baseline=(1, "t1")
        )

        assert pipeline.calls == [("/doc.pdf", "explicit text", "pid-1")]
        assert len(session.update_calls) == 1  # re-stamp
        assert daemon.stats.explicit_index_repairs == 1

    @pytest.mark.asyncio
    async def test_guard_noop_when_watermark_stable(self, sleep_recorder: list[float]) -> None:
        daemon, pipeline, session = self._guard_daemon(
            watermarks=[(1, "t1")],
            row_version=("cid-1", 100),
        )

        await daemon._explicit_index_guard(
            "/doc.pdf", "explicit text", ("pid-1", "cid-1", 100), baseline=(1, "t1")
        )

        assert pipeline.calls == []
        assert session.update_calls == []
        assert daemon.stats.explicit_index_repairs == 0

    @pytest.mark.asyncio
    async def test_guard_stands_down_when_file_moved_on(self, sleep_recorder: list[float]) -> None:
        """A REAL new write owns the index — the guard must never clobber
        the newer version with stale explicit text."""
        daemon, pipeline, session = self._guard_daemon(
            watermarks=[(5, "t9")],
            row_version=("cid-1", 999),  # updated_at advanced past what we indexed
        )

        await daemon._explicit_index_guard(
            "/doc.pdf", "explicit text", ("pid-1", "cid-1", 100), baseline=(1, "t1")
        )

        assert pipeline.calls == []
        assert session.update_calls == []
        assert daemon.stats.explicit_index_repairs == 0

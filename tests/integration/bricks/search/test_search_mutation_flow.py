import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.contracts.constants import ROOT_ZONE_ID


class _SettingsStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get_setting(self, key: str):
        value = self._values.get(key)
        if value is None:
            return None
        return type("Setting", (), {"value": value})()

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        self._values[key] = value


@pytest.mark.asyncio
async def test_consumer_checkpoint_advances_only_after_success() -> None:
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._shutting_down = False

    event = SearchMutationEvent(
        event_id="evt-1",
        operation_id="op-1",
        op=SearchMutationOp.UPSERT,
        path="/zone/root/docs/readme.md",
        zone_id=ROOT_ZONE_ID,
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        sequence_number=7,
    )
    daemon._fetch_mutation_events = AsyncMock(side_effect=[[event], asyncio.CancelledError()])
    handler = AsyncMock()

    await daemon._run_mutation_consumer("txtai", handler)

    assert settings_store.get_setting("search_mutation_checkpoint:txtai").value == "7"
    handler.assert_awaited_once_with([event])


@pytest.mark.asyncio
async def test_fts_mutation_skips_document_chunks_when_path_id_unresolved() -> None:
    daemon = SearchDaemon()
    daemon._chunk_store = AsyncMock()
    event = SearchMutationEvent(
        event_id="evt-1",
        operation_id="op-1",
        op=SearchMutationOp.UPSERT,
        path="/docs/readme.md",
        zone_id=ROOT_ZONE_ID,
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        sequence_number=1,
    )
    daemon._resolve_mutations = AsyncMock(
        return_value=[
            SimpleNamespace(
                event=event,
                zone_id=ROOT_ZONE_ID,
                doc_id="/docs/readme.md",
                path_id="/docs/readme.md",
                virtual_path="/docs/readme.md",
                content="hello",
                path_id_resolved=False,
            )
        ]
    )

    await daemon._consume_fts_mutations([event])

    daemon._chunk_store.replace_document_chunks.assert_not_awaited()


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _SessionCtx:
    def __init__(self, rows):
        self._rows = rows
        self.execute_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt, _params=None):
        self.execute_count += 1
        return _RowsResult(self._rows)


class _SequentialSessionFactory:
    def __init__(self, sessions):
        self._sessions = list(sessions)

    def __call__(self):
        return self._sessions.pop(0)


@pytest.mark.asyncio
async def test_refresh_indexes_reuses_cached_content_path_id() -> None:
    daemon = SearchDaemon()
    daemon._indexing_pipeline = AsyncMock()
    daemon._indexing_pipeline.index_document.return_value = SimpleNamespace(error=None)
    daemon._embedding_provider = object()
    daemon._async_session = _SequentialSessionFactory(
        [
            _SessionCtx([("scoped content", "pid-scoped")]),
            _SessionCtx([("pid-canonical",)]),
            _SessionCtx([(1,)]),
        ]
    )

    await daemon._refresh_indexes(["/zone/tenant/docs/readme.md"])

    daemon._indexing_pipeline.index_document.assert_awaited_once_with(
        "/zone/tenant/docs/readme.md",
        "scoped content",
        "pid-scoped",
    )


def test_refresh_index_lookup_values_cast_rank_for_asyncpg() -> None:
    values_sql, params = SearchDaemon._build_path_lookup_values(
        ["/docs/readme.md", "/zone/root/docs/readme.md"]
    )

    assert "CAST(:rank_0 AS INTEGER)" in values_sql
    assert "CAST(:rank_1 AS INTEGER)" in values_sql
    assert params["rank_0"] == 0
    assert params["rank_1"] == 1


@pytest.mark.asyncio
async def test_consumers_share_one_fetched_mutation_window() -> None:
    daemon = SearchDaemon()
    daemon.config.mutation_batch_size = 2
    daemon._consumer_names = ("fts", "embedding")
    daemon._consumer_last_sequence = {"fts": 0, "embedding": 0}
    session = _SessionCtx(
        [
            SimpleNamespace(
                operation_id="op-1",
                operation_type="write",
                zone_id=ROOT_ZONE_ID,
                path="/zone/root/docs/a.md",
                new_path=None,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                sequence_number=1,
                change_type=None,
            ),
            SimpleNamespace(
                operation_id="op-2",
                operation_type="write",
                zone_id=ROOT_ZONE_ID,
                path="/zone/root/docs/b.md",
                new_path=None,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                sequence_number=2,
                change_type=None,
            ),
        ]
    )
    daemon._async_session = lambda: session  # noqa: E731

    first = await daemon._fetch_mutation_events("fts")
    second = await daemon._fetch_mutation_events("embedding")

    assert [event.sequence_number for event in first] == [1, 2]
    assert [event.sequence_number for event in second] == [1, 2]
    assert session.execute_count == 1


# ─── Issue #4016: startup reconciliation ──────────────────────────────
# Post-#3699: only the fts and embedding consumers exist. The bm25 and
# txtai branches reconciliation used to cover were retired with their
# backends; reconciliation logic itself still applies to the surviving
# consumers and must keep working on the upgrade path.

_CONSUMER_NAMES = ("fts", "embedding")


def _set_all_reconciled(settings_store: _SettingsStore, names: tuple[str, ...]) -> None:
    for name in names:
        settings_store.set_setting(f"search_mutation_reconciled_v1:{name}", "1")


def _enable_all_backends(daemon: SearchDaemon) -> None:
    """Make every consumer's backend live so reconciliation pre-checks pass."""
    daemon._chunk_store = AsyncMock()
    daemon._indexing_pipeline = AsyncMock()
    daemon._embedding_provider = object()


def _make_session_factory(rows: list[tuple[str, str, str]]):
    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            class _Result:
                @staticmethod
                def fetchall():
                    return list(rows)

            return _Result()

    def _factory():
        return _FakeSession()

    return _factory


@pytest.mark.asyncio
async def test_reconcile_unindexed_paths_skips_when_all_markers_set() -> None:
    """All consumers reconciled → reconciliation is a no-op."""
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    _set_all_reconciled(settings_store, daemon._consumer_names)
    daemon._async_session = AsyncMock()  # would crash if SQL ran
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    daemon._consume_fts_mutations.assert_not_awaited()
    daemon._consume_embedding_mutations.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_unindexed_paths_runs_handlers_with_synthesized_events() -> None:
    """Cold start with unindexed file_paths rows → each handler sees events."""
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    _enable_all_backends(daemon)
    daemon._async_session = _make_session_factory(
        [("root", "/foo.md", "pid-1"), ("zone-b", "/bar.md", "pid-2")]
    )
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    for handler in (daemon._consume_fts_mutations, daemon._consume_embedding_mutations):
        handler.assert_awaited_once()
        events_arg = handler.await_args.args[0]
        assert len(events_arg) == 2
        assert events_arg[0].op == SearchMutationOp.UPSERT
        assert events_arg[0].path == "/zone/root/foo.md"
        assert events_arg[0].operation_id == "reconcile:pid-1"
        assert events_arg[1].path == "/zone/zone-b/bar.md"
    for name in daemon._consumer_names:
        assert settings_store.get_setting(f"search_mutation_reconciled_v1:{name}") is not None


@pytest.mark.asyncio
async def test_reconcile_runs_when_marker_unset_even_with_existing_checkpoint() -> None:
    """Codex round-1 finding 1: upgrade path — a checkpoint without a
    reconciliation marker still triggers reconciliation, so deployments
    running the prior buggy version recover their unindexed live files."""
    settings_store = _SettingsStore()
    for name in _CONSUMER_NAMES:
        settings_store.set_setting(f"search_mutation_checkpoint:{name}", "9999")

    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    _enable_all_backends(daemon)
    daemon._async_session = _make_session_factory([("root", "/foo.md", "pid-1")])
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    daemon._consume_fts_mutations.assert_awaited_once()
    daemon._consume_embedding_mutations.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_marker_when_handler_fails() -> None:
    """Codex round-1 finding 2: a failing handler must NOT set its marker;
    other handlers proceed independently and DO set theirs."""
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    _enable_all_backends(daemon)
    daemon._async_session = _make_session_factory([("root", "/foo.md", "pid-1")])
    daemon._consume_fts_mutations = AsyncMock(side_effect=RuntimeError("fts down"))
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    assert settings_store.get_setting("search_mutation_reconciled_v1:fts") is None
    assert settings_store.get_setting("search_mutation_reconciled_v1:embedding") is not None


@pytest.mark.asyncio
async def test_reconcile_skips_marker_when_backend_not_ready() -> None:
    """Codex round-2 finding: a consumer whose backend is None must NOT
    get its marker set. Otherwise a transient backend init failure on
    first start permanently closes the recovery path on next restart."""
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    daemon._chunk_store = AsyncMock()  # fts ready
    # embedding (_indexing_pipeline / _embedding_provider) stays None.
    daemon._async_session = _make_session_factory([("root", "/foo.md", "pid-1")])
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    daemon._consume_fts_mutations.assert_awaited_once()
    assert settings_store.get_setting("search_mutation_reconciled_v1:fts") is not None
    daemon._consume_embedding_mutations.assert_not_awaited()
    assert settings_store.get_setting("search_mutation_reconciled_v1:embedding") is None


@pytest.mark.asyncio
async def test_reconcile_marks_consumers_when_no_unindexed_rows() -> None:
    """Empty result set still records markers, so warm starts skip the SQL scan."""
    settings_store = _SettingsStore()
    daemon = SearchDaemon(settings_store=settings_store)
    daemon._consumer_names = _CONSUMER_NAMES
    _enable_all_backends(daemon)
    daemon._async_session = _make_session_factory([])
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()

    await daemon._reconcile_unindexed_paths_at_startup()

    daemon._consume_fts_mutations.assert_not_awaited()
    for name in daemon._consumer_names:
        assert settings_store.get_setting(f"search_mutation_reconciled_v1:{name}") is not None


@pytest.mark.asyncio
async def test_index_refresh_loop_reconciles_before_initializing_checkpoints() -> None:
    """`_index_refresh_loop` calls reconciliation BEFORE consumer init."""
    daemon = SearchDaemon()
    call_order: list[str] = []

    async def _reconcile():
        call_order.append("reconcile")

    async def _init_checkpoint(name):
        call_order.append(f"init:{name}")
        return 0

    daemon._reconcile_unindexed_paths_at_startup = _reconcile
    daemon._initialize_consumer_checkpoint = _init_checkpoint
    daemon._consume_fts_mutations = AsyncMock()
    daemon._consume_embedding_mutations = AsyncMock()
    # Cancel the gather so the loop exits without driving consumers.
    daemon._run_mutation_consumer = AsyncMock(side_effect=asyncio.CancelledError())

    with contextlib.suppress(asyncio.CancelledError):
        await daemon._index_refresh_loop()

    assert call_order, "loop never ran"
    assert call_order[0] == "reconcile", f"expected reconcile first, got {call_order}"
    assert all(call.startswith("init:") for call in call_order[1:]), call_order

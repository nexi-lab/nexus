import asyncio
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
async def test_legacy_delete_paths_call_delete_backends() -> None:
    daemon = SearchDaemon()
    daemon._chunk_store = AsyncMock()
    daemon._backend = AsyncMock()
    daemon._resolve_mutations = AsyncMock(
        return_value=[
            SimpleNamespace(
                zone_id=ROOT_ZONE_ID,
                doc_id="/docs/readme.md",
                path_id="pid-1",
                virtual_path="/docs/readme.md",
            )
        ]
    )

    await daemon._delete_indexes_for_paths(["/zone/root/docs/readme.md"])

    daemon._chunk_store.delete_document_chunks.assert_awaited_once_with("pid-1")
    daemon._backend.delete.assert_awaited_once_with(["/docs/readme.md"], zone_id=ROOT_ZONE_ID)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

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


@pytest.mark.asyncio
async def test_txtai_bootstrap_groups_chunks_without_postgres_aggregates() -> None:
    daemon = SearchDaemon()
    daemon._backend = AsyncMock()
    daemon._async_session = lambda: _SessionCtx(  # noqa: E731
        [
            SimpleNamespace(
                zone_id=ROOT_ZONE_ID, virtual_path="/docs/a.md", chunk_index=0, chunk_text="A1"
            ),
            SimpleNamespace(
                zone_id=ROOT_ZONE_ID, virtual_path="/docs/a.md", chunk_index=1, chunk_text="A2"
            ),
            SimpleNamespace(
                zone_id="other", virtual_path="/docs/b.md", chunk_index=0, chunk_text="B1"
            ),
        ]
    )

    await daemon._bootstrap_txtai_backend()

    assert daemon._txtai_bootstrapped is True
    daemon._backend.upsert.assert_any_await(
        [
            {
                "id": "/docs/a.md",
                "text": "A1\nA2",
                "path": "/docs/a.md",
                "zone_id": "root",
            }
        ],
        zone_id=ROOT_ZONE_ID,
    )
    daemon._backend.upsert.assert_any_await(
        [
            {
                "id": "other:/docs/b.md",
                "text": "B1",
                "path": "/docs/b.md",
                "zone_id": "other",
            }
        ],
        zone_id="other",
    )


@pytest.mark.asyncio
async def test_consumers_share_one_fetched_mutation_window() -> None:
    daemon = SearchDaemon()
    daemon.config.mutation_batch_size = 2
    daemon._consumer_names = ("bm25", "txtai")
    daemon._consumer_last_sequence = {"bm25": 0, "txtai": 0}
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

    first = await daemon._fetch_mutation_events("bm25")
    second = await daemon._fetch_mutation_events("txtai")

    assert [event.sequence_number for event in first] == [1, 2]
    assert [event.sequence_number for event in second] == [1, 2]
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_txtai_consumer_collapses_duplicate_document_mutations() -> None:
    daemon = SearchDaemon()
    daemon._backend = AsyncMock()
    daemon._resolve_mutations = AsyncMock(
        return_value=[
            SimpleNamespace(
                zone_id=ROOT_ZONE_ID,
                doc_id="/docs/plan.md",
                path_id="pid-1",
                virtual_path="/docs/plan.md",
                content="older",
                event=SimpleNamespace(op=SearchMutationOp.UPSERT),
            ),
            SimpleNamespace(
                zone_id=ROOT_ZONE_ID,
                doc_id="/docs/plan.md",
                path_id="pid-1",
                virtual_path="/docs/plan.md",
                content="newer",
                event=SimpleNamespace(op=SearchMutationOp.UPSERT),
            ),
        ]
    )

    await daemon._consume_txtai_mutations([])

    daemon._backend.upsert.assert_awaited_once_with(
        [
            {
                "id": "/docs/plan.md",
                "text": "newer",
                "path": "/docs/plan.md",
                "zone_id": "root",
            }
        ],
        zone_id=ROOT_ZONE_ID,
    )

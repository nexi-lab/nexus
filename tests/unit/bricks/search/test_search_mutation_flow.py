import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp


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
        zone_id="root",
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
                zone_id="root",
                doc_id="/docs/readme.md",
                path_id="pid-1",
                virtual_path="/docs/readme.md",
            )
        ]
    )

    await daemon._delete_indexes_for_paths(["/zone/root/docs/readme.md"])

    daemon._chunk_store.delete_document_chunks.assert_awaited_once_with("pid-1")
    daemon._backend.delete.assert_awaited_once_with(["/docs/readme.md"], zone_id="root")


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SessionCtx:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt, _params=None):
        return _RowsResult(self._rows)


@pytest.mark.asyncio
async def test_txtai_bootstrap_groups_chunks_without_postgres_aggregates() -> None:
    daemon = SearchDaemon()
    daemon._backend = AsyncMock()
    daemon._async_session = lambda: _SessionCtx(  # noqa: E731
        [
            SimpleNamespace(
                zone_id="root", virtual_path="/docs/a.md", chunk_index=0, chunk_text="A1"
            ),
            SimpleNamespace(
                zone_id="root", virtual_path="/docs/a.md", chunk_index=1, chunk_text="A2"
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
        zone_id="root",
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

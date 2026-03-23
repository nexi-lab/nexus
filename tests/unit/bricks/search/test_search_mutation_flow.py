import asyncio
from datetime import UTC, datetime
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

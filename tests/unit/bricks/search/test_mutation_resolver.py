from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_resolver import MutationResolver


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SessionCtx:
    def __init__(self, rows):
        self._rows = list(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt, _params):
        return _Result(self._rows.pop(0))


@pytest.mark.asyncio
async def test_resolver_batches_path_lookup_and_reuses_cache() -> None:
    file_reader = AsyncMock()
    file_reader.read_text.side_effect = ["alpha", "beta"]
    session_factory = lambda: _SessionCtx(  # noqa: E731
        [
            [("/docs/a.txt", "pid-a"), ("/docs/b.txt", "pid-b")],
        ]
    )

    resolver = MutationResolver(
        file_reader=file_reader,
        async_session_factory=session_factory,
    )
    events = [
        SearchMutationEvent(
            event_id="evt-1",
            operation_id="op-1",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/a.txt",
            zone_id="root",
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=1,
        ),
        SearchMutationEvent(
            event_id="evt-2",
            operation_id="op-2",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/b.txt",
            zone_id="root",
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=2,
        ),
    ]

    resolved = await resolver.resolve_batch(events)
    assert [item.path_id for item in resolved] == ["pid-a", "pid-b"]
    assert file_reader.read_text.await_count == 2

    resolved_again = await resolver.resolve_batch(events)
    assert [item.path_id for item in resolved_again] == ["pid-a", "pid-b"]
    assert file_reader.read_text.await_count == 2

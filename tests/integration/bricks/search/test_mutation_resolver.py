from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import ProgrammingError

from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_resolver import MutationResolver
from nexus.contracts.constants import ROOT_ZONE_ID


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SessionCtx:
    def __init__(self, rows):
        self._rows = rows
        self.statements = []
        self.params = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt, _params):
        self.statements.append(getattr(_stmt, "text", str(_stmt)))
        self.params.append(_params)
        return _Result(self._rows.pop(0))


class _ErrorSessionCtx(_SessionCtx):
    def __init__(self, responses):
        super().__init__([])
        self._responses = list(responses)

    async def execute(self, _stmt, _params):
        self.statements.append(getattr(_stmt, "text", str(_stmt)))
        self.params.append(_params)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Result(response)


class _SequentialSessionFactory:
    def __init__(self, sessions):
        self._sessions = list(sessions)

    def __call__(self):
        return self._sessions.pop(0)


@pytest.mark.asyncio
async def test_resolver_batches_path_lookup_and_reuses_cache() -> None:
    file_reader = AsyncMock()
    file_reader.read_text.side_effect = ["alpha", "beta"]
    session_factory = lambda: _SessionCtx(  # noqa: E731
        [
            [("root", "/docs/a.txt", "pid-a"), ("root", "/docs/b.txt", "pid-b")],
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
            zone_id=ROOT_ZONE_ID,
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=1,
        ),
        SearchMutationEvent(
            event_id="evt-2",
            operation_id="op-2",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/b.txt",
            zone_id=ROOT_ZONE_ID,
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


@pytest.mark.asyncio
async def test_resolver_keeps_zone_isolation_for_duplicate_virtual_paths() -> None:
    file_reader = AsyncMock()
    file_reader.read_text.side_effect = [OSError("missing"), OSError("missing")]
    rows = [
        [("root", "/docs/readme.md", "pid-root"), ("other", "/docs/readme.md", "pid-other")],
        [
            ("root", "/docs/readme.md", "root content"),
            ("other", "/docs/readme.md", "other content"),
        ],
    ]
    session_factory = lambda: _SessionCtx(rows)  # noqa: E731

    resolver = MutationResolver(
        file_reader=file_reader,
        async_session_factory=session_factory,
    )
    events = [
        SearchMutationEvent(
            event_id="evt-root",
            operation_id="op-root",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/readme.md",
            zone_id=ROOT_ZONE_ID,
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=1,
        ),
        SearchMutationEvent(
            event_id="evt-other",
            operation_id="op-other",
            op=SearchMutationOp.UPSERT,
            path="/zone/other/docs/readme.md",
            zone_id="other",
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=2,
        ),
    ]

    resolved = await resolver.resolve_batch(events)
    assert [(item.zone_id, item.path_id, item.content) for item in resolved] == [
        ("root", "pid-root", "root content"),
        ("other", "pid-other", "other content"),
    ]


@pytest.mark.asyncio
async def test_resolver_uses_values_lookup_batches_instead_of_or_chains() -> None:
    file_reader = AsyncMock()
    file_reader.read_text.side_effect = ["alpha", "beta"]
    session = _SessionCtx(
        [
            [("root", "/docs/a.txt", "pid-a")],
            [("root", "/docs/b.txt", "pid-b")],
        ]
    )
    resolver = MutationResolver(
        file_reader=file_reader,
        async_session_factory=lambda: session,
        lookup_batch_size=1,
    )
    events = [
        SearchMutationEvent(
            event_id="evt-1",
            operation_id="op-1",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/a.txt",
            zone_id=ROOT_ZONE_ID,
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=1,
        ),
        SearchMutationEvent(
            event_id="evt-2",
            operation_id="op-2",
            op=SearchMutationOp.UPSERT,
            path="/zone/root/docs/b.txt",
            zone_id=ROOT_ZONE_ID,
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=2,
        ),
    ]

    resolved = await resolver.resolve_batch(events)

    assert [item.path_id for item in resolved] == ["pid-a", "pid-b"]
    assert len(session.statements) == 2
    assert all("WITH lookup(zone_id, virtual_path) AS" in stmt for stmt in session.statements)
    assert all(" OR " not in stmt for stmt in session.statements)


@pytest.mark.asyncio
async def test_resolver_ignores_missing_content_cache_table() -> None:
    file_reader = AsyncMock()
    file_reader.read_text.side_effect = [OSError("missing")]
    missing_table = ProgrammingError(
        "SELECT ... FROM content_cache",
        {},
        Exception('relation "content_cache" does not exist'),
    )
    session_factory = _SequentialSessionFactory(
        [
            _SessionCtx([[("root", "/docs/a.txt", "pid-a")]]),
            _ErrorSessionCtx([missing_table]),
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
            zone_id=ROOT_ZONE_ID,
            timestamp=SimpleNamespace(tzinfo=None),
            sequence_number=1,
        )
    ]

    resolved = await resolver.resolve_batch(events)

    assert len(resolved) == 1
    assert resolved[0].content is None

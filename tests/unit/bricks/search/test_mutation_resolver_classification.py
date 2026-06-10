"""MutationResolver failure-classification tests (#4337)."""

from __future__ import annotations

from datetime import datetime

import pytest

from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_resolver import MutationResolver
from nexus.contracts.exceptions import NexusFileNotFoundError


def _event(op: SearchMutationOp = SearchMutationOp.UPSERT, path: str = "/zone/z1/docs/a.md"):
    return SearchMutationEvent(
        event_id="search:op-1",
        operation_id="op-1",
        op=op,
        path=path,
        zone_id="z1",
        timestamp=datetime(2026, 6, 9, 1, 2, 3),
        sequence_number=7,
    )


class NotFoundReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_text(self, path: str) -> str:
        self.calls += 1
        raise FileNotFoundError(path)


class OutageReader:
    async def read_text(self, path: str) -> str:
        raise TimeoutError("backend down")


class MixedReader:
    """NotFound on scoped path, outage on virtual path."""

    async def read_text(self, path: str) -> str:
        if path.startswith("/zone/"):
            raise FileNotFoundError(path)
        raise TimeoutError("backend down")


class EmptyReader:
    async def read_text(self, path: str) -> str:
        return ""


def test_nexus_not_found_is_filenotfound_subclass() -> None:
    assert issubclass(NexusFileNotFoundError, FileNotFoundError)


@pytest.mark.asyncio
async def test_not_found_on_both_paths_is_permanent() -> None:
    resolver = MutationResolver(file_reader=NotFoundReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "permanent"
    assert mutation.failure_detail


@pytest.mark.asyncio
async def test_non_notfound_failure_is_transient() -> None:
    resolver = MutationResolver(file_reader=OutageReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_mixed_failures_are_transient() -> None:
    resolver = MutationResolver(file_reader=MixedReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_missing_reader_is_transient_boot_window() -> None:
    resolver = MutationResolver(file_reader=None, async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_empty_string_read_is_resolved_not_failure() -> None:
    resolver = MutationResolver(file_reader=EmptyReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is True
    assert mutation.content == ""
    assert mutation.failure_kind is None


@pytest.mark.asyncio
async def test_delete_events_have_no_failure() -> None:
    resolver = MutationResolver(file_reader=NotFoundReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event(op=SearchMutationOp.DELETE)])
    assert mutation.content_resolved is True
    assert mutation.failure_kind is None


@pytest.mark.asyncio
async def test_unresolved_mutations_are_not_cached() -> None:
    reader = NotFoundReader()
    resolver = MutationResolver(file_reader=reader, async_session_factory=None)
    await resolver.resolve_batch([_event()])
    first_calls = reader.calls
    assert first_calls > 0
    assert "search:op-1" not in resolver._cache
    await resolver.resolve_batch([_event()])
    assert reader.calls > first_calls  # second pass re-read (no stale cache)


@pytest.mark.asyncio
async def test_resolved_mutations_are_still_cached() -> None:
    resolver = MutationResolver(file_reader=EmptyReader(), async_session_factory=None)
    await resolver.resolve_batch([_event()])
    assert "search:op-1" in resolver._cache


class HugeErrorReader:
    async def read_text(self, path: str) -> str:
        raise RuntimeError("x" * 10_000)


@pytest.mark.asyncio
async def test_failure_detail_is_bounded() -> None:
    resolver = MutationResolver(file_reader=HugeErrorReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_detail is not None
    assert len(mutation.failure_detail) < 600
    assert mutation.failure_detail.endswith("…[truncated]")

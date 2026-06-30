import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.contracts.metadata import DT_DIR, DT_REG
from nexus.core.pagination import PaginatedResult
from nexus.server.cache_warmer import CacheWarmer, WarmupConfig
from nexus.server.lifespan import permissions


class _FakeNexusFS:
    def __init__(self, pages: dict[tuple[str, str | None], PaginatedResult | list[dict]]):
        self.pages = pages
        self.readdir_calls: list[dict] = []

    def service(self, name: str):
        raise AssertionError(f"unexpected service lookup: {name}")

    def sys_readdir(
        self,
        path: str,
        *,
        recursive: bool,
        details: bool,
        limit: int,
        cursor: str | None,
        context,
    ):
        self.readdir_calls.append(
            {
                "path": path,
                "recursive": recursive,
                "details": details,
                "limit": limit,
                "cursor": cursor,
                "context": context,
            }
        )
        return self.pages.get((path, cursor), [])


class _StatNexusFS(_FakeNexusFS):
    def __init__(self, pages, stats, contents=None):
        super().__init__(pages)
        self.stats = stats
        self.contents = contents or {}
        self.stat_calls: list[dict] = []

    def sys_stat(self, path: str, *, context):
        self.stat_calls.append(
            {
                "path": path,
                "context": context,
                "thread_id": threading.get_ident(),
            }
        )
        result = self.stats.get(path)
        if isinstance(result, BaseException):
            raise result
        return result

    def sys_read(self, path: str, *, context):
        return self.contents[path]


class _DoneTask:
    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.callback = None

    def add_done_callback(self, callback):
        self.callback = callback

    def result(self):
        if self.error is not None:
            raise self.error
        return None


class _RecordingDiskCache:
    def __init__(self):
        self.put_calls: list[tuple[str, bytes, str]] = []

    def put(self, content_id: str, content: bytes, *, zone_id: str):
        self.put_calls.append((content_id, content, zone_id))


def _entry(path: str, entry_type: int) -> dict:
    return {"path": path, "entry_type": entry_type, "size": 0, "content_id": ""}


def test_startup_cache_warmup_disabled_creates_no_task(monkeypatch):
    monkeypatch.setenv("NEXUS_CACHE_WARMUP_ENABLED", "false")
    created = []

    def _create_task(coro):
        coro.close()
        created.append(coro)
        return object()

    monkeypatch.setattr(asyncio, "create_task", _create_task)

    tasks = permissions._startup_cache_warmup(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(nexus_fs=object()),
    )

    assert tasks == []
    assert created == []


@pytest.mark.parametrize("enabled", [None, "true", "1", "yes"])
def test_startup_cache_warmup_returns_tracked_task(monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("NEXUS_CACHE_WARMUP_ENABLED", raising=False)
    else:
        monkeypatch.setenv("NEXUS_CACHE_WARMUP_ENABLED", enabled)
    sentinel = _DoneTask()

    def _create_task(coro):
        coro.close()
        return sentinel

    monkeypatch.setattr(asyncio, "create_task", _create_task)

    tasks = permissions._startup_cache_warmup(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(nexus_fs=object()),
    )

    assert tasks == [sentinel]


def test_startup_cache_warmup_invalid_config_skips(monkeypatch):
    monkeypatch.setenv("NEXUS_CACHE_WARMUP_MAX_FILES", "not-an-int")

    tasks = permissions._startup_cache_warmup(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(nexus_fs=object()),
    )

    assert tasks == []


def test_startup_cache_warmup_observes_background_failure(monkeypatch, caplog):
    task = _DoneTask(RuntimeError("warmup exploded"))

    def _create_task(coro):
        coro.close()
        return task

    monkeypatch.setattr(asyncio, "create_task", _create_task)

    tasks = permissions._startup_cache_warmup(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(nexus_fs=object()),
    )

    assert tasks == [task]
    assert task.callback is not None
    with caplog.at_level(logging.ERROR, logger=permissions.__name__):
        task.callback(task)
    assert "Server startup warmup failed" in caplog.text
    assert "warmup exploded" in caplog.text


def test_startup_cache_warmup_cancellation_is_not_logged_as_error(monkeypatch, caplog):
    task = _DoneTask(asyncio.CancelledError())

    def _create_task(coro):
        coro.close()
        return task

    monkeypatch.setattr(asyncio, "create_task", _create_task)

    permissions._startup_cache_warmup(  # noqa: SLF001
        SimpleNamespace(),
        SimpleNamespace(nexus_fs=object()),
    )

    assert task.callback is not None
    with caplog.at_level(logging.ERROR, logger=permissions.__name__):
        task.callback(task)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_startup_permissions_returns_cache_warmup_task(monkeypatch):
    sentinel = object()

    monkeypatch.setattr(permissions, "_seed_root_zone", lambda svc: None)
    monkeypatch.setattr(permissions, "_startup_async_rebac", AsyncMock())
    monkeypatch.setattr(permissions, "_startup_cache_brick", AsyncMock())
    monkeypatch.setattr(permissions, "_startup_durable_invalidation", AsyncMock())
    monkeypatch.setattr(permissions, "_startup_tiger_cache", AsyncMock(return_value=[]))
    monkeypatch.setattr(permissions, "_startup_backfill", lambda app, svc: [])
    monkeypatch.setattr(permissions, "_startup_cache_warmup", lambda app, svc: [sentinel])
    monkeypatch.setattr(permissions, "_startup_circuit_breaker", AsyncMock())

    tasks = await permissions.startup_permissions(SimpleNamespace(), SimpleNamespace())

    assert tasks == [sentinel]


@pytest.mark.asyncio
async def test_cache_warmup_uses_bounded_paginated_readdir():
    pages = {
        ("/", None): PaginatedResult(
            items=[_entry("/dir", DT_DIR), _entry("/a.txt", DT_REG)],
            next_cursor="/a.txt",
            has_more=True,
        ),
        ("/", "/a.txt"): PaginatedResult(
            items=[_entry("/b.txt", DT_REG), _entry("/c.txt", DT_REG)],
            next_cursor=None,
            has_more=False,
        ),
        ("/dir", None): [_entry("/dir/should-not-list.txt", DT_REG)],
    }
    nexus = _FakeNexusFS(pages)
    warmer = CacheWarmer(nexus, config=WarmupConfig(max_files=2, depth=2))
    warmer._warmup_metadata = AsyncMock(return_value=True)  # noqa: SLF001

    stats = await warmer._warmup_directory_on_current_loop(  # noqa: SLF001
        "/",
        max_files=2,
        depth=2,
        context={"zone_id": "root"},
    )

    assert stats.files_warmed == 2
    assert [call.args[2] for call in warmer._warmup_metadata.await_args_list] == [
        {"zone_id": "root"},
        {"zone_id": "root"},
    ]
    assert [call.args[0] for call in warmer._warmup_metadata.await_args_list] == [
        "/a.txt",
        "/b.txt",
    ]
    assert all(call["recursive"] is False for call in nexus.readdir_calls)
    assert "/dir" not in [call["path"] for call in nexus.readdir_calls]


@pytest.mark.asyncio
async def test_cache_warmup_respects_depth_and_non_positive_limit():
    nexus = _FakeNexusFS(
        {
            ("/", None): [
                _entry("/dir", DT_DIR),
                _entry("/root.txt", DT_REG),
            ],
            ("/dir", None): [_entry("/dir/nested.txt", DT_REG)],
        }
    )
    warmer = CacheWarmer(nexus, config=WarmupConfig(max_files=10, depth=1))
    warmer._warmup_metadata = AsyncMock()  # noqa: SLF001

    await warmer._warmup_directory_on_current_loop("/", max_files=10, depth=1)  # noqa: SLF001

    assert [call.args[0] for call in warmer._warmup_metadata.await_args_list] == ["/root.txt"]
    assert [call["path"] for call in nexus.readdir_calls] == ["/"]

    nexus_zero = _FakeNexusFS({})
    zero_warmer = CacheWarmer(nexus_zero, config=WarmupConfig(max_files=0, depth=2))

    zero_stats = await zero_warmer._warmup_directory_on_current_loop(  # noqa: SLF001
        "/",
        max_files=0,
        depth=2,
    )

    assert zero_stats.files_warmed == 0
    assert nexus_zero.readdir_calls == []


@pytest.mark.asyncio
async def test_cache_warmup_uses_real_sys_stat_off_loop_and_counts_successes():
    context = {"zone_id": "root", "user_id": "warmup"}
    pages = {
        ("/", None): [
            _entry("/ok.txt", DT_REG),
            _entry("/missing.txt", DT_REG),
            _entry("/denied.txt", DT_REG),
        ]
    }
    nexus = _StatNexusFS(
        pages,
        {
            "/ok.txt": {"path": "/ok.txt", "size": 3, "content_id": "ok"},
            "/missing.txt": None,
            "/denied.txt": PermissionError("denied"),
        },
    )
    warmer = CacheWarmer(nexus, config=WarmupConfig(max_files=3, depth=1))
    event_loop_thread = threading.get_ident()

    stats = await warmer._warmup_directory_on_current_loop(  # noqa: SLF001
        "/",
        max_files=3,
        depth=1,
        context=context,
    )

    assert sorted(call["path"] for call in nexus.stat_calls) == [
        "/denied.txt",
        "/missing.txt",
        "/ok.txt",
    ]
    assert all(call["context"] is context for call in nexus.stat_calls)
    assert all(call["thread_id"] != event_loop_thread for call in nexus.stat_calls)
    assert stats.metadata_warmed == 1
    assert stats.files_warmed == 1
    assert stats.skipped == 1
    assert stats.errors == 1


@pytest.mark.asyncio
async def test_cache_warmup_supports_dict_and_object_metadata_fields():
    nexus = _StatNexusFS(
        {},
        {
            "/empty.txt": {"size": 0},
            "/small.txt": SimpleNamespace(size=12),
            "/large.txt": {"size": 4096},
            "/dict-content.txt": {"content_id": "dict-content"},
            "/object-content.txt": SimpleNamespace(content_id="object-content"),
        },
        {
            "/dict-content.txt": b"dict",
            "/object-content.txt": b"object",
        },
    )
    disk_cache = _RecordingDiskCache()
    warmer = CacheWarmer(
        nexus,
        config=WarmupConfig(small_file_threshold_kb=1),
        local_disk_cache=disk_cache,
    )

    small_files = await warmer._filter_small_files(  # noqa: SLF001
        ["/empty.txt", "/small.txt", "/large.txt"],
        {"zone_id": "root"},
    )
    await warmer._warmup_content(  # noqa: SLF001
        "/dict-content.txt",
        "root",
        {"zone_id": "root"},
    )
    await warmer._warmup_content(  # noqa: SLF001
        "/object-content.txt",
        "root",
        {"zone_id": "root"},
    )

    assert small_files == ["/empty.txt", "/small.txt"]
    assert disk_cache.put_calls == [
        ("dict-content", b"dict", "root"),
        ("object-content", b"object", "root"),
    ]


@pytest.mark.asyncio
async def test_cache_warmup_caps_directory_only_paginated_discovery():
    class _DirectoryOnlyNexus(_FakeNexusFS):
        def __init__(self):
            super().__init__({})
            self.entries_returned = 0

        def sys_readdir(
            self,
            path: str,
            *,
            recursive: bool,
            details: bool,
            limit: int,
            cursor: str | None,
            context,
        ):
            if path == "/":
                start = int(cursor or 0)
                remaining = max(0, 32 - start)
                count = min(limit, remaining)
                next_offset = start + count
                items = [_entry(f"/dir-{i}", DT_DIR) for i in range(start, next_offset)]
                next_cursor = str(next_offset) if next_offset < 32 else None
                has_more = next_offset < 32
            elif path.count("/") < 8:
                count = 1
                items = [_entry(f"{path}/child", DT_DIR)]
                next_cursor = None
                has_more = False
            else:
                count = 0
                items = []
                next_cursor = None
                has_more = False
            self.entries_returned += count
            self.readdir_calls.append(
                {
                    "path": path,
                    "recursive": recursive,
                    "details": details,
                    "limit": limit,
                    "cursor": cursor,
                    "context": context,
                }
            )
            return PaginatedResult(
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    nexus = _DirectoryOnlyNexus()
    warmer = CacheWarmer(nexus, config=WarmupConfig(max_files=1, depth=32))

    files = await warmer._discover_files_for_warmup(  # noqa: SLF001
        "/",
        depth=32,
        max_files=1,
        context={"zone_id": "root"},
    )

    assert files == []
    assert nexus.readdir_calls[0]["limit"] == 256
    assert len(nexus.readdir_calls) <= 4
    assert nexus.entries_returned <= 256

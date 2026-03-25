"""Integration test: metastore-first sync model end-to-end (Issue #3266).

Validates the full pipeline:
  mount connector → ConnectorSyncLoop sync → metastore write →
  list from metastore → delta refresh → updated metastore

Uses mock connector backends with real ConnectorSyncLoop, real MountSyncState
tracking, and a mock metastore. No real OAuth or API calls needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.backends.connectors.cli.sync_loop import ConnectorSyncLoop
from nexus.backends.connectors.cli.sync_types import DeltaItem, DeltaSyncResult
from nexus.contracts.capabilities import ConnectorCapability

# ── Helpers ──────────────────────────────────────────────────────────


def _make_sync_eligible_backend(
    name: str = "gmail",
    dir_tree: dict[str, list[str]] | None = None,
    file_contents: dict[str, bytes] | None = None,
    sync_delta_result: DeltaSyncResult | dict | None = None,
) -> MagicMock:
    """Create a mock connector backend with SYNC_ELIGIBLE capability."""
    backend = MagicMock()
    backend.name = name
    backend.capabilities = frozenset(
        {
            ConnectorCapability.SYNC_ELIGIBLE,
            ConnectorCapability.CACHE_BULK_READ,
        }
    )
    backend.has_capability = MagicMock(side_effect=lambda c: c in backend.capabilities)
    backend.use_metadata_listing = True
    backend._has_caching = MagicMock(return_value=False)

    # Wire up list_dir
    tree = dir_tree or {}

    def _list_dir(path: str = "", context: Any = None) -> list[str]:
        return tree.get(path.strip("/") if path else "", [])

    backend.list_dir = MagicMock(side_effect=_list_dir)

    # Wire up read_content
    contents = file_contents or {}

    def _read_content(content_id: str, context: Any = None) -> bytes:
        path = context.backend_path if context and hasattr(context, "backend_path") else content_id
        if path in contents:
            return contents[path]
        raise FileNotFoundError(path)

    backend.read_content = MagicMock(side_effect=_read_content)

    # Wire up sync_delta
    if sync_delta_result is not None:
        backend.sync_delta = MagicMock(return_value=sync_delta_result)
    else:
        # No sync_delta method → triggers full BFS sync
        if hasattr(backend, "sync_delta"):
            del backend.sync_delta

    return backend


def _make_metastore() -> MagicMock:
    """Create a mock metastore that stores FileMetadata entries."""
    store: dict[str, Any] = {}

    metastore = MagicMock()

    def _set(path: str, meta: Any) -> None:
        store[path] = meta

    def _get(path: str) -> Any:
        return store.get(path)

    def _delete(path: str) -> None:
        store.pop(path, None)

    def _list_directory_entries(path: str, zone_id: str | None = None) -> list | None:
        prefix = path.rstrip("/") + "/"
        entries = [
            SimpleNamespace(path=p)
            for p in sorted(store.keys())
            if p.startswith(prefix) and "/" not in p[len(prefix) :]
        ]
        return entries if entries else None

    metastore.set = MagicMock(side_effect=_set)
    metastore.get = MagicMock(side_effect=_get)
    metastore.delete = MagicMock(side_effect=_delete)
    metastore.list_directory_entries = MagicMock(side_effect=_list_directory_entries)
    metastore._store = store  # Expose for assertions
    return metastore


def _make_wired_system(
    mounts: list[dict],
    route_map: dict[str, Any],
    metastore: Any | None = None,
) -> tuple[MagicMock, MagicMock, ConnectorSyncLoop]:
    """Wire up mount service, router, and sync loop like the real boot process."""
    mount_svc = MagicMock()
    mount_svc.list_mounts = AsyncMock(return_value=mounts)
    mount_svc.sync_mount = AsyncMock(return_value={"files_scanned": 0})
    mount_svc._metastore = metastore
    mount_svc._sync_service = None
    mount_svc._search_service = None

    router = MagicMock()

    def _route(path: str) -> Any:
        for prefix, route_obj in route_map.items():
            if path.startswith(prefix):
                return route_obj
        return None

    router.route = MagicMock(side_effect=_route)

    sync_loop = ConnectorSyncLoop(mount_svc, router, interval=60)
    return mount_svc, router, sync_loop


# ── Tests ────────────────────────────────────────────────────────────


class TestMetastoreFirstE2E:
    """Full pipeline: sync → metastore → list."""

    @pytest.mark.asyncio
    async def test_delta_sync_writes_to_metastore(self) -> None:
        """Delta sync fetches content and writes FileMetadata to metastore."""
        metastore = _make_metastore()

        backend = _make_sync_eligible_backend(
            name="gmail",
            file_contents={
                "INBOX/t1-msg1.yaml": b"subject: Hello\nfrom: alice@test.com",
                "SENT/t2-msg2.yaml": b"subject: Reply\nto: bob@test.com",
            },
            sync_delta_result=DeltaSyncResult(
                added=[
                    DeltaItem(id="msg1", path="INBOX/t1-msg1.yaml", size=34),
                    DeltaItem(id="msg2", path="SENT/t2-msg2.yaml", size=32),
                ],
                deleted=[],
                sync_token="history_500",
            ),
        )

        route = SimpleNamespace(backend=backend, mount_point="/mnt/gmail")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/gmail"}],
            route_map={"/mnt/gmail": route},
            metastore=metastore,
        )

        # Run one sync cycle
        await sync_loop._sync_all()

        # Verify: metastore has both files
        assert "/mnt/gmail/INBOX/t1-msg1.yaml" in metastore._store
        assert "/mnt/gmail/SENT/t2-msg2.yaml" in metastore._store

        # Verify: metadata has correct fields
        meta1 = metastore._store["/mnt/gmail/INBOX/t1-msg1.yaml"]
        assert meta1.path == "/mnt/gmail/INBOX/t1-msg1.yaml"
        assert meta1.backend_name == "gmail"
        assert meta1.physical_path == "INBOX/t1-msg1.yaml"
        assert meta1.size == len(b"subject: Hello\nfrom: alice@test.com")
        assert meta1.etag is not None  # sha256 hash

        # Verify: sync state recorded correctly
        state = sync_loop.get_mount_state("/mnt/gmail")
        assert state is not None
        assert state.is_healthy
        assert state.sync_token == "history_500"
        assert state.total_files_synced == 2

    @pytest.mark.asyncio
    async def test_metastore_listing_returns_synced_files(self) -> None:
        """After sync, list_directory_entries returns the synced files."""
        metastore = _make_metastore()

        backend = _make_sync_eligible_backend(
            name="gmail",
            file_contents={
                "INBOX/t1-msg1.yaml": b"subject: Email 1",
                "INBOX/t2-msg2.yaml": b"subject: Email 2",
                "INBOX/t3-msg3.yaml": b"subject: Email 3",
            },
            sync_delta_result=DeltaSyncResult(
                added=[
                    DeltaItem(id="msg1", path="INBOX/t1-msg1.yaml"),
                    DeltaItem(id="msg2", path="INBOX/t2-msg2.yaml"),
                    DeltaItem(id="msg3", path="INBOX/t3-msg3.yaml"),
                ],
                sync_token="h100",
            ),
        )

        route = SimpleNamespace(backend=backend, mount_point="/mnt/gmail")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/gmail"}],
            route_map={"/mnt/gmail": route},
            metastore=metastore,
        )

        # Run sync
        await sync_loop._sync_all()

        # Verify: metastore can list the INBOX
        entries = metastore.list_directory_entries("/mnt/gmail/INBOX")
        assert entries is not None
        assert len(entries) == 3
        paths = {e.path for e in entries}
        assert "/mnt/gmail/INBOX/t1-msg1.yaml" in paths
        assert "/mnt/gmail/INBOX/t2-msg2.yaml" in paths
        assert "/mnt/gmail/INBOX/t3-msg3.yaml" in paths

    @pytest.mark.asyncio
    async def test_delta_deletion_removes_from_metastore(self) -> None:
        """Delta sync with deletions removes entries from metastore."""
        metastore = _make_metastore()

        # Pre-populate metastore (simulating a previous sync)
        from nexus.contracts.metadata import FileMetadata

        for path in [
            "/mnt/gmail/INBOX/t1-msg1.yaml",
            "/mnt/gmail/INBOX/t2-msg2.yaml",
            "/mnt/gmail/INBOX/t3-msg3.yaml",
        ]:
            metastore.set(
                path,
                FileMetadata(
                    path=path,
                    backend_name="gmail",
                    physical_path=path.replace("/mnt/gmail/", ""),
                    size=100,
                    etag="hash123",
                ),
            )

        assert len(metastore._store) == 3

        # Delta: msg2 was deleted
        backend = _make_sync_eligible_backend(
            name="gmail",
            sync_delta_result=DeltaSyncResult(
                added=[],
                deleted=["INBOX/t2-msg2.yaml"],
                sync_token="h200",
            ),
        )

        route = SimpleNamespace(backend=backend, mount_point="/mnt/gmail")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/gmail"}],
            route_map={"/mnt/gmail": route},
            metastore=metastore,
        )

        await sync_loop._sync_all()

        # msg1 and msg3 should still exist, msg2 should be deleted
        assert "/mnt/gmail/INBOX/t1-msg1.yaml" in metastore._store
        assert "/mnt/gmail/INBOX/t2-msg2.yaml" not in metastore._store
        assert "/mnt/gmail/INBOX/t3-msg3.yaml" in metastore._store

    @pytest.mark.asyncio
    async def test_full_sync_fallback_when_no_delta(self) -> None:
        """Connectors without sync_delta fall back to full BFS sync."""
        backend = _make_sync_eligible_backend(
            name="gcalendar",
            dir_tree={"": ["primary/"], "primary": ["event1.yaml", "event2.yaml"]},
        )
        # No sync_delta → should trigger mount_service.sync_mount

        route = SimpleNamespace(backend=backend, mount_point="/mnt/calendar")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/calendar"}],
            route_map={"/mnt/calendar": route},
        )

        await sync_loop._sync_all()

        # sync_mount should be called (full BFS fallback)
        mount_svc.sync_mount.assert_called_once_with(mount_point="/mnt/calendar", recursive=True)

        state = sync_loop.get_mount_state("/mnt/calendar")
        assert state.is_healthy

    @pytest.mark.asyncio
    async def test_multiple_connectors_synced_independently(self) -> None:
        """Gmail and Calendar are synced independently in the same cycle."""
        metastore = _make_metastore()

        gmail_backend = _make_sync_eligible_backend(
            name="gmail",
            file_contents={
                "INBOX/t1-msg1.yaml": b"subject: Email",
            },
            sync_delta_result=DeltaSyncResult(
                added=[DeltaItem(id="msg1", path="INBOX/t1-msg1.yaml")],
                sync_token="gmail_h100",
            ),
        )

        calendar_backend = _make_sync_eligible_backend(
            name="gcalendar",
            file_contents={
                "primary/evt1.yaml": b"summary: Meeting",
            },
            sync_delta_result=DeltaSyncResult(
                added=[DeltaItem(id="evt1", path="primary/evt1.yaml")],
                sync_token="cal_token_50",
            ),
        )

        gmail_route = SimpleNamespace(backend=gmail_backend, mount_point="/mnt/gmail")
        cal_route = SimpleNamespace(backend=calendar_backend, mount_point="/mnt/calendar")

        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[
                {"mount_point": "/mnt/gmail"},
                {"mount_point": "/mnt/calendar"},
            ],
            route_map={
                "/mnt/gmail": gmail_route,
                "/mnt/calendar": cal_route,
            },
            metastore=metastore,
        )

        await sync_loop._sync_all()

        # Both connectors should have entries in metastore
        assert "/mnt/gmail/INBOX/t1-msg1.yaml" in metastore._store
        assert "/mnt/calendar/primary/evt1.yaml" in metastore._store

        # Both should have independent sync states
        gmail_state = sync_loop.get_mount_state("/mnt/gmail")
        cal_state = sync_loop.get_mount_state("/mnt/calendar")
        assert gmail_state.sync_token == "gmail_h100"
        assert cal_state.sync_token == "cal_token_50"

    @pytest.mark.asyncio
    async def test_search_daemon_notified_with_display_paths(self) -> None:
        """Search daemon gets notified with full display paths, not bare IDs."""
        search_daemon = MagicMock()
        search_daemon.notify_file_change = AsyncMock()

        search_svc = MagicMock()
        search_svc._search_daemon = search_daemon

        backend = _make_sync_eligible_backend(
            name="gmail",
            file_contents={
                "SENT/t1-msg1.yaml": b"subject: Outgoing",
                "STARRED/t2-msg2.yaml": b"subject: Important",
            },
            sync_delta_result=DeltaSyncResult(
                added=[
                    DeltaItem(id="msg1", path="SENT/t1-msg1.yaml"),
                    DeltaItem(id="msg2", path="STARRED/t2-msg2.yaml"),
                ],
                sync_token="h300",
            ),
        )

        metastore = _make_metastore()
        route = SimpleNamespace(backend=backend, mount_point="/mnt/gmail")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/gmail"}],
            route_map={"/mnt/gmail": route},
            metastore=metastore,
        )
        mount_svc._search_service = search_svc

        await sync_loop._sync_all()

        # Search daemon should have been notified with FULL paths (not hardcoded INBOX)
        calls = search_daemon.notify_file_change.call_args_list
        notified_paths = {c.args[0] for c in calls}
        assert "/mnt/gmail/SENT/t1-msg1.yaml" in notified_paths
        assert "/mnt/gmail/STARRED/t2-msg2.yaml" in notified_paths
        # Should NOT have hardcoded INBOX path
        assert not any("INBOX" in p for p in notified_paths)

    @pytest.mark.asyncio
    async def test_consecutive_delta_syncs_accumulate(self) -> None:
        """Multiple sync cycles accumulate state correctly."""
        metastore = _make_metastore()

        # First cycle: 2 emails
        backend = _make_sync_eligible_backend(
            name="gmail",
            file_contents={
                "INBOX/msg1.yaml": b"email 1",
                "INBOX/msg2.yaml": b"email 2",
            },
            sync_delta_result=DeltaSyncResult(
                added=[
                    DeltaItem(id="msg1", path="INBOX/msg1.yaml"),
                    DeltaItem(id="msg2", path="INBOX/msg2.yaml"),
                ],
                sync_token="h100",
            ),
        )

        route = SimpleNamespace(backend=backend, mount_point="/mnt/gmail")
        mount_svc, router, sync_loop = _make_wired_system(
            mounts=[{"mount_point": "/mnt/gmail"}],
            route_map={"/mnt/gmail": route},
            metastore=metastore,
        )

        await sync_loop._sync_all()
        assert len(metastore._store) == 2

        # Second cycle: 1 new email
        backend.sync_delta = MagicMock(
            return_value=DeltaSyncResult(
                added=[DeltaItem(id="msg3", path="INBOX/msg3.yaml")],
                sync_token="h200",
            )
        )
        backend.read_content = MagicMock(return_value=b"email 3")

        await sync_loop._sync_all()
        assert len(metastore._store) == 3
        assert "/mnt/gmail/INBOX/msg3.yaml" in metastore._store

        state = sync_loop.get_mount_state("/mnt/gmail")
        assert state.sync_token == "h200"
        assert state.total_files_synced == 3

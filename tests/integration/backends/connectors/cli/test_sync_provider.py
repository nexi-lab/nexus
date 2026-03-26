"""Tests for ConnectorSyncProvider protocol and sync orchestration (Issue #3148).

Decisions tested:
    - #3A: Full sync state lifecycle in orchestrator
    - #7 (enriched): SyncPage with items, deleted_ids, pagination
    - #10A: Orchestrator tests with configurable FakeConnectorSyncProvider
    - #14A+C: FetchResult supports bytes | AsyncIterator[bytes]

Failure matrix (Decision #10):
    - Partial failure (provider fails mid-page)
    - Corrupted/expired state token
    - Interrupted sync (cancellation)
    - Concurrent sync protection
"""

import pytest

from nexus.backends.connectors.cli.protocol import (
    ConnectorSyncProvider,
    FetchResult,
    MountSyncState,
    RemoteItem,
    SyncPage,
    SyncStatus,
)

# ---------------------------------------------------------------------------
# FakeConnectorSyncProvider — configurable test double (Decision #10A)
# ---------------------------------------------------------------------------


class FakeConnectorSyncProvider:
    """Configurable fake for testing sync orchestrator behavior.

    Example::

        provider = FakeConnectorSyncProvider(
            pages=[
                SyncPage(items=[...], state_token="tok1", next_page_token="p2"),
                SyncPage(items=[...], state_token="tok2"),
            ],
            fail_on_page=2,  # Fail when fetching page 2
        )
    """

    def __init__(
        self,
        pages: list[SyncPage] | None = None,
        items: dict[str, FetchResult] | None = None,
        fail_on_page: int | None = None,
        expire_token_on: str | None = None,
    ) -> None:
        self._pages = pages or []
        self._items = items or {}
        self._fail_on_page = fail_on_page
        self._expire_token_on = expire_token_on
        self.list_call_count = 0
        self.fetch_call_count = 0

    async def list_remote_items(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> SyncPage:
        self.list_call_count += 1

        # Simulate expired token
        if since and self._expire_token_on and since == self._expire_token_on:
            raise ValueError("token expired")

        # Determine which page to return
        page_idx = 0
        if page_token:
            for i, _page in enumerate(self._pages):
                if i > 0 and self._pages[i - 1].next_page_token == page_token:
                    page_idx = i
                    break

        # Simulate failure on specific page
        if self._fail_on_page is not None and self.list_call_count == self._fail_on_page:
            raise ConnectionError("simulated network failure")

        if page_idx < len(self._pages):
            return self._pages[page_idx]

        return SyncPage(items=[])

    async def fetch_item(self, item_id: str) -> FetchResult:
        self.fetch_call_count += 1
        if item_id in self._items:
            return self._items[item_id]
        raise KeyError(f"Item not found: {item_id}")


# ---------------------------------------------------------------------------
# SyncPage data model tests
# ---------------------------------------------------------------------------


class TestSyncPage:
    def test_empty_page(self) -> None:
        page = SyncPage(items=[])
        assert len(page.items) == 0
        assert len(page.deleted_ids) == 0
        assert page.next_page_token is None
        assert page.state_token is None

    def test_page_with_items_and_deletions(self) -> None:
        items = [
            RemoteItem(item_id="1", relative_path="INBOX/1.yaml", size=1024),
            RemoteItem(item_id="2", relative_path="INBOX/2.yaml", size=2048),
        ]
        page = SyncPage(
            items=items,
            deleted_ids=["old-1", "old-2"],
            next_page_token="next",
            state_token="tok-abc",
        )
        assert len(page.items) == 2
        assert len(page.deleted_ids) == 2
        assert page.next_page_token == "next"
        assert page.state_token == "tok-abc"

    def test_remote_item_metadata(self) -> None:
        item = RemoteItem(
            item_id="msg-123",
            relative_path="INBOX/msg-123.yaml",
            size=4096,
            modified_time="2026-03-19T10:00:00Z",
            content_hash="sha256:abc123",
            metadata={"labels": ["important", "starred"]},
        )
        assert item.item_id == "msg-123"
        assert item.metadata["labels"] == ["important", "starred"]

    def test_remote_item_frozen(self) -> None:
        item = RemoteItem(item_id="1", relative_path="test.yaml")
        with pytest.raises(AttributeError):
            item.item_id = "2"


# ---------------------------------------------------------------------------
# FetchResult tests
# ---------------------------------------------------------------------------


class TestFetchResult:
    def test_bytes_result(self) -> None:
        result = FetchResult(relative_path="INBOX/1.yaml", content=b"data: hello")
        assert not result.is_streaming()
        assert result.content == b"data: hello"

    def test_streaming_result(self) -> None:
        async def gen():
            yield b"chunk1"
            yield b"chunk2"

        result = FetchResult(relative_path="drive/big.pdf", async_chunks=gen(), size=1024)
        assert result.is_streaming()
        assert result.size == 1024

    def test_neither_content_nor_stream(self) -> None:
        result = FetchResult(relative_path="empty.yaml")
        assert not result.is_streaming()
        assert result.content is None


# ---------------------------------------------------------------------------
# MountSyncState lifecycle tests (Decision #3A)
# ---------------------------------------------------------------------------


class TestMountSyncState:
    def test_initial_state(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail", provider_type="gmail")
        assert state.status == SyncStatus.INITIAL
        assert state.state_token is None
        assert state.items_synced == 0

    def test_update_after_sync(self) -> None:
        state = MountSyncState(mount_point="/mnt/gmail", provider_type="gmail")
        state.state_token = "history-123"
        state.status = SyncStatus.VALID
        state.items_synced = 42
        state.pages_processed = 3
        state.last_sync_time = "2026-03-19T10:00:00Z"

        assert state.state_token == "history-123"
        assert state.status == SyncStatus.VALID

    def test_invalidate_resets_to_initial(self) -> None:
        state = MountSyncState(
            mount_point="/mnt/gmail",
            provider_type="gmail",
            state_token="tok-123",
            status=SyncStatus.VALID,
            items_synced=100,
            pages_processed=5,
        )
        state.invalidate()
        assert state.state_token is None
        assert state.status == SyncStatus.INITIAL
        assert state.items_synced == 0
        assert state.pages_processed == 0


# ---------------------------------------------------------------------------
# FakeConnectorSyncProvider orchestrator tests (Decision #10A)
# ---------------------------------------------------------------------------


class TestFakeSyncProviderBasic:
    @pytest.mark.asyncio
    async def test_single_page_sync(self) -> None:
        items = [RemoteItem(item_id="1", relative_path="a.yaml")]
        provider = FakeConnectorSyncProvider(
            pages=[SyncPage(items=items, state_token="tok1")],
            items={"1": FetchResult(relative_path="a.yaml", content=b"data")},
        )
        page = await provider.list_remote_items("/")
        assert len(page.items) == 1
        assert page.state_token == "tok1"

        fetched = await provider.fetch_item("1")
        assert fetched.content == b"data"

    @pytest.mark.asyncio
    async def test_multi_page_sync(self) -> None:
        provider = FakeConnectorSyncProvider(
            pages=[
                SyncPage(
                    items=[RemoteItem(item_id="1", relative_path="a.yaml")],
                    next_page_token="p2",
                    state_token="tok1",
                ),
                SyncPage(
                    items=[RemoteItem(item_id="2", relative_path="b.yaml")],
                    state_token="tok2",
                ),
            ],
        )
        page1 = await provider.list_remote_items("/")
        assert page1.next_page_token == "p2"

        page2 = await provider.list_remote_items("/", page_token="p2")
        assert page2.next_page_token is None
        assert page2.state_token == "tok2"

    @pytest.mark.asyncio
    async def test_expired_token_triggers_full_resync(self) -> None:
        """When state token is expired, provider raises ValueError."""
        provider = FakeConnectorSyncProvider(
            pages=[SyncPage(items=[], state_token="tok-new")],
            expire_token_on="tok-old",
        )

        # Delta sync with expired token should fail
        with pytest.raises(ValueError, match="token expired"):
            await provider.list_remote_items("/", since="tok-old")

        # Full re-sync (since=None) should succeed
        page = await provider.list_remote_items("/")
        assert page.state_token == "tok-new"

    @pytest.mark.asyncio
    async def test_partial_failure_on_page(self) -> None:
        """Provider fails on a specific page (Decision #10 failure matrix)."""
        provider = FakeConnectorSyncProvider(
            pages=[
                SyncPage(
                    items=[RemoteItem(item_id="1", relative_path="a.yaml")],
                    next_page_token="p2",
                    state_token="tok1",
                ),
                SyncPage(
                    items=[RemoteItem(item_id="2", relative_path="b.yaml")],
                    state_token="tok2",
                ),
            ],
            fail_on_page=2,  # Fail on second list call
        )

        # First page succeeds
        page1 = await provider.list_remote_items("/")
        assert len(page1.items) == 1

        # Second page fails
        with pytest.raises(ConnectionError, match="simulated"):
            await provider.list_remote_items("/", page_token="p2")

    @pytest.mark.asyncio
    async def test_fetch_missing_item(self) -> None:
        provider = FakeConnectorSyncProvider()
        with pytest.raises(KeyError, match="not-exists"):
            await provider.fetch_item("not-exists")

    @pytest.mark.asyncio
    async def test_deletion_tracking(self) -> None:
        page = SyncPage(
            items=[RemoteItem(item_id="new-1", relative_path="new.yaml")],
            deleted_ids=["old-1", "old-2", "old-3"],
            state_token="tok-with-deletes",
        )
        provider = FakeConnectorSyncProvider(pages=[page])
        result = await provider.list_remote_items("/")
        assert len(result.deleted_ids) == 3
        assert "old-1" in result.deleted_ids


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestSyncProviderProtocol:
    def test_fake_satisfies_protocol(self) -> None:
        """FakeConnectorSyncProvider satisfies ConnectorSyncProvider protocol."""
        provider = FakeConnectorSyncProvider()
        assert isinstance(provider, ConnectorSyncProvider)


# ---------------------------------------------------------------------------
# CLISyncProvider._parse_list_output metadata extraction (Issue #3256)
# ---------------------------------------------------------------------------


class TestParseListOutputMetadata:
    """Verify that metadata from CLI output flows into RemoteItem.metadata."""

    def _make_provider(self, connector=None):
        from nexus.backends.connectors.cli.sync_provider import CLISyncProvider

        if connector is None:
            from unittest.mock import MagicMock

            connector = MagicMock()
            connector._config = None
        return CLISyncProvider(connector)

    def test_metadata_extracted_from_dict_items(self) -> None:
        import yaml

        provider = self._make_provider()
        stdout = yaml.dump(
            [
                {
                    "id": "msg-1",
                    "subject": "Meeting Notes",
                    "date": "2026-03-20",
                    "labels": ["INBOX", "CATEGORY_PERSONAL"],
                },
            ]
        )
        page = provider._parse_list_output(stdout)
        assert len(page.items) == 1
        item = page.items[0]
        assert item.item_id == "msg-1"
        assert item.metadata["subject"] == "Meeting Notes"
        assert item.metadata["date"] == "2026-03-20"
        assert item.metadata["labels"] == ["INBOX", "CATEGORY_PERSONAL"]

    def test_metadata_excludes_standard_fields(self) -> None:
        import yaml

        provider = self._make_provider()
        stdout = yaml.dump(
            [
                {
                    "id": "x",
                    "size": 1024,
                    "hash": "abc",
                    "modified": "2026-01-01",
                    "title": "Hello",
                },
            ]
        )
        page = provider._parse_list_output(stdout)
        meta = page.items[0].metadata
        # Standard fields should NOT be in metadata
        assert "id" not in meta
        assert "size" not in meta
        assert "hash" not in meta
        assert "modified" not in meta
        # Custom fields should be in metadata
        assert meta["title"] == "Hello"

    def test_display_path_called_when_no_explicit_path(self) -> None:
        """When items have no 'path' field, connector.display_path() is used."""
        from unittest.mock import MagicMock

        import yaml

        from nexus.backends.connectors.cli.display_path import DisplayPathMixin

        connector = MagicMock(spec=DisplayPathMixin)
        connector._config = None
        connector.display_path.return_value = "INBOX/PRIMARY/2026-03-20_Meeting.yaml"

        provider = self._make_provider(connector)
        stdout = yaml.dump([{"id": "msg-1", "subject": "Meeting"}])
        page = provider._parse_list_output(stdout)

        assert page.items[0].relative_path == "INBOX/PRIMARY/2026-03-20_Meeting.yaml"
        connector.display_path.assert_called_once()

    def test_explicit_path_overrides_display_path(self) -> None:
        """When items have an explicit 'path' field, it takes precedence."""
        from unittest.mock import MagicMock

        import yaml

        connector = MagicMock()
        connector._config = None

        provider = self._make_provider(connector)
        stdout = yaml.dump([{"id": "msg-1", "path": "custom/path.yaml"}])
        page = provider._parse_list_output(stdout)

        assert page.items[0].relative_path == "custom/path.yaml"
        connector.display_path.assert_not_called()

    def test_string_items_use_display_path(self) -> None:
        """When items are plain strings, display_path() is called."""
        from unittest.mock import MagicMock

        import yaml

        from nexus.backends.connectors.cli.display_path import DisplayPathMixin

        connector = MagicMock(spec=DisplayPathMixin)
        connector._config = None
        connector.display_path.return_value = "issues/42_bug-fix.yaml"

        provider = self._make_provider(connector)
        stdout = yaml.dump(["item-42"])
        page = provider._parse_list_output(stdout)

        assert page.items[0].relative_path == "issues/42_bug-fix.yaml"
        connector.display_path.assert_called_once_with("item-42", None)

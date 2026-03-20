"""CLI-backed sync provider — integrates with MountService.sync_mount().

Bridges the ConnectorSyncProvider protocol to CLI-backed connectors
by translating CLI list/get commands into SyncPage/FetchResult objects.

The sync orchestrator in MountService handles state persistence,
pagination, mutex, and idempotent writes. This provider just fetches.

Phase 2 deliverable (Issue #3148).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.cli.protocol import (
    FetchResult,
    RemoteItem,
    SyncPage,
)
from nexus.backends.connectors.cli.result import CLIResult

if TYPE_CHECKING:
    from nexus.backends.connectors.cli.base import CLIConnector

logger = logging.getLogger(__name__)


class CLISyncProvider:
    """Sync provider that delegates to a CLIConnector for list/fetch operations.

    Wraps the CLI connector's list_dir/read_content methods into the
    ConnectorSyncProvider protocol. The CLI's delta command (from config)
    is used for incremental sync when a state token is available.

    Args:
        connector: The CLIConnector backend instance.
    """

    def __init__(self, connector: "CLIConnector") -> None:
        self._connector = connector
        self._config = connector._config

    async def list_remote_items(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> SyncPage:
        """List remote items via CLI.

        Uses the sync delta_command if ``since`` is provided (incremental),
        otherwise falls back to full listing via the read list_command.
        """
        import asyncio

        args = self._build_list_args(path, since=since, page_token=page_token, page_size=page_size)

        result: CLIResult = await asyncio.to_thread(
            self._connector._execute_cli, args, stdin=None, context=None
        )

        if not result.ok:
            # Check for expired token pattern
            if "expired" in result.stderr.lower() or "invalid" in result.stderr.lower():
                raise ValueError(f"token expired: {result.stderr[:200]}")
            logger.warning("CLI list failed: %s", result.summary())
            return SyncPage(items=[])

        return self._parse_list_output(result.stdout)

    async def fetch_item(self, item_id: str) -> FetchResult:
        """Fetch a single item's content via CLI."""
        import asyncio

        args = self._build_get_args(item_id)

        result: CLIResult = await asyncio.to_thread(
            self._connector._execute_cli, args, stdin=None, context=None
        )

        if not result.ok:
            raise KeyError(f"Failed to fetch {item_id}: {result.summary()}")

        return FetchResult(
            relative_path=f"{item_id}.yaml",
            content=result.stdout.encode("utf-8"),
        )

    def _build_list_args(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> list[str]:
        """Build CLI args for listing items."""
        args = [self._connector.CLI_NAME]

        if self._connector.CLI_SERVICE:
            args.append(self._connector.CLI_SERVICE)

        if since and self._config and self._config.sync:
            # Use delta command for incremental sync
            delta_cmd = self._config.sync.delta_command.replace("{since}", since)
            args.extend(delta_cmd.split())
        elif self._config and self._config.read:
            args.append(self._config.read.list_command)
            if path and path != "/":
                args.append(path)

        args.extend(["--limit", str(page_size)])

        if page_token:
            args.extend(["--page-token", page_token])

        return args

    def _build_get_args(self, item_id: str) -> list[str]:
        """Build CLI args for fetching a single item."""
        args = [self._connector.CLI_NAME]

        if self._connector.CLI_SERVICE:
            args.append(self._connector.CLI_SERVICE)

        if self._config and self._config.read:
            args.append(self._config.read.get_command)

        args.append(item_id)
        return args

    def _parse_list_output(self, stdout: str) -> SyncPage:
        """Parse CLI list output into a SyncPage.

        Expects YAML or JSON output with items list and optional
        pagination/state tokens.
        """
        try:
            data: Any = yaml.safe_load(stdout)
        except yaml.YAMLError:
            logger.debug("Failed to parse CLI list output as YAML")
            return SyncPage(items=[])

        if data is None:
            return SyncPage(items=[])

        items: list[RemoteItem] = []
        deleted_ids: list[str] = []
        next_page_token: str | None = None
        state_token: str | None = None

        # Handle list of items
        raw_items: list[Any] = []
        if isinstance(data, list):
            raw_items = data
        elif isinstance(data, dict):
            raw_items = data.get("items", data.get("messages", data.get("results", []))) or []
            next_page_token = data.get("nextPageToken", data.get("next_page_token"))
            deleted_ids = data.get("deleted", data.get("deleted_ids", [])) or []

            # State token from configured field
            if self._config and self._config.sync:
                state_token = data.get(self._config.sync.state_field)

        for raw in raw_items:
            if isinstance(raw, dict):
                item_id = str(raw.get("id", raw.get("item_id", "")))
                if not item_id:
                    continue
                items.append(
                    RemoteItem(
                        item_id=item_id,
                        relative_path=raw.get("path", f"{item_id}.yaml"),
                        size=raw.get("size"),
                        modified_time=raw.get("modified", raw.get("mtime")),
                        content_hash=raw.get("hash", raw.get("content_hash")),
                        metadata={
                            k: v
                            for k, v in raw.items()
                            if k
                            not in ("id", "item_id", "path", "size", "modified", "mtime", "hash")
                        },
                    )
                )
            elif isinstance(raw, str):
                items.append(RemoteItem(item_id=raw, relative_path=f"{raw}.yaml"))

        return SyncPage(
            items=items,
            deleted_ids=deleted_ids,
            next_page_token=next_page_token,
            state_token=state_token,
        )

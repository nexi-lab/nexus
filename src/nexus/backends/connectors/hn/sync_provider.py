"""Poll-based sync provider for HackerNews. Phase 4 (#3148)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.connectors.cli.protocol import FetchResult, RemoteItem, SyncPage

if TYPE_CHECKING:
    from nexus.backends.connectors.hn.connector import HNConnectorBackend

logger = logging.getLogger(__name__)


class HNSyncProvider:
    """Poll-based sync provider for HackerNews stories.

    HN doesn't support delta sync, so this polls the top/new/best
    story endpoints and returns items that changed since the last poll.
    State token is the max item ID seen.
    """

    def __init__(self, connector: "HNConnectorBackend | None" = None) -> None:
        self._connector = connector

    async def list_remote_items(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> SyncPage:
        """Poll HN API for stories, return items with IDs > since.

        Args:
            path: Feed name (e.g., "top", "new", "best").
            since: Max item ID from previous sync (None = full sync).
            page_token: Not used (HN has no pagination token).
            page_size: Max items to return.

        Returns:
            SyncPage with story items and updated state token.
        """
        feed = path.strip("/")
        if feed.startswith("hn/"):
            feed = feed[3:]

        if self._connector is None:
            return SyncPage(items=[], state_token=since)

        # Fetch story IDs from HN API (async method)
        story_ids = await self._connector._fetch_story_ids(feed)
        stories_limit = getattr(self._connector, "stories_per_feed", page_size)
        ids_to_fetch = story_ids[: min(page_size, stories_limit)]

        # Filter to items newer than `since` if provided
        since_id = int(since) if since else 0
        filtered_ids = [sid for sid in ids_to_fetch if sid > since_id]

        items: list[RemoteItem] = []
        for story_id in filtered_ids:
            items.append(
                RemoteItem(
                    item_id=str(story_id),
                    relative_path=f"{feed}/{story_id}.json",
                )
            )

        max_id = str(max(int(i.item_id) for i in items)) if items else since
        return SyncPage(items=items, state_token=max_id)

    async def fetch_item(self, item_id: str) -> FetchResult:
        """Fetch a single story's content by item ID.

        Args:
            item_id: HN story/item ID.

        Returns:
            FetchResult with JSON content bytes.
        """
        if self._connector is None:
            return FetchResult(relative_path=f"{item_id}.json", content=b"{}")

        story: dict[str, Any] | None = await self._connector._fetch_item(int(item_id))
        content = json.dumps(story or {}, indent=2, ensure_ascii=False).encode("utf-8")
        return FetchResult(relative_path=f"{item_id}.json", content=content)

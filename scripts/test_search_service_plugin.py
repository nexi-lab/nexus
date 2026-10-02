"""Live SearchService contract against a running signed Search plugin.

Run inside the Nexus image with NEXUS_SEARCH_PLUGIN_TARGET pointing at the
loopback plugin host. The fixture writes and removes only its unique VFS path.
"""

from __future__ import annotations

import asyncio
import os
from typing import NoReturn
from uuid import uuid4

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.search_service import SearchService
from nexus.remote.rpc_transport import RPCTransport


class NoSqlSearch:
    def session_factory(self) -> NoReturn:
        raise AssertionError("Indexed search must not read the SQL record store")


async def main() -> None:
    target = os.environ.get("NEXUS_SEARCH_PLUGIN_TARGET", "127.0.0.1:2126")
    path = f"/search-contract-{uuid4().hex}.txt"
    text = "marigold constellation search contract"
    transport = RPCTransport(target, timeout=15)
    daemon = SearchDaemon(target=target)
    service = SearchService(
        metadata_store=None, record_store=NoSqlSearch(), enforce_permissions=False
    )
    service._search_daemon = daemon
    created = False
    try:
        await daemon.startup()
        await asyncio.to_thread(transport.write_file, path, text.encode())
        created = True
        indexed = await daemon.index_documents([{"path": path, "text": text}])
        assert indexed["indexed"] == 1, indexed
        hits = await service.semantic_search("marigold", path=path, search_mode="keyword")
        assert [hit["path"] for hit in hits] == [path], hits
        assert "marigold" in hits[0]["chunk_text"], hits
        assert hits[0]["score"] > 0, hits
        stats = await service.semantic_search_stats()
        assert stats["fts_doc_count"] >= 1, stats
        assert (
            await service.semantic_search("absentuniquetoken", path=path, search_mode="keyword")
            == []
        )

        await asyncio.to_thread(transport.delete_file, path)
        created = False
        await daemon.notify_file_change(path, "delete")
        assert await service.semantic_search("marigold", path=path, search_mode="keyword") == []

        service._search_daemon = None
        for operation, kwargs in (
            (service.semantic_search, {"query": "marigold", "search_mode": "keyword"}),
            (service.semantic_search_stats, {}),
        ):
            try:
                await operation(**kwargs)
            except ValueError as exc:
                assert "search-plugin" in str(exc), exc
            else:
                raise AssertionError("Missing plugin must report unavailable")
        print("SearchService live contract passed: index/query/stats/delete/unavailable")
    finally:
        if created:
            await asyncio.to_thread(transport.delete_file, path)
            await daemon.notify_file_change(path, "delete")
        service.close()
        await daemon.shutdown()
        transport.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))

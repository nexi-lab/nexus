"""Live SearchService contract against a running signed Search plugin.

Run inside the Nexus image with NEXUS_SEARCH_PLUGIN_TARGET pointing at the
loopback plugin host. The fixture writes and removes only its unique VFS path.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from nexus.bricks.search.daemon import SearchDaemon
from nexus.bricks.search.search_service import SearchService
from nexus.lib.request_credentials import request_api_key
from nexus.remote.rpc_transport import RPCTransport


async def main() -> None:
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    tls = SimpleNamespace(
        ca_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        node_cert_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
        node_key_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
    )
    path = f"/workspace/search-contract-{uuid4().hex}.txt"
    text = "marigold constellation search contract"
    transport = RPCTransport(target, auth_token=admin, tls_config=tls, timeout=15)
    daemon = SearchDaemon(target=target)
    service = SearchService(kernel=transport)
    created = False
    context = SimpleNamespace(zone_id="sharedzone")
    credential_scope = request_api_key.set(admin)
    try:
        await daemon.startup()
        await asyncio.to_thread(transport.write_file, path, text.encode())
        created = True
        indexed = await daemon.index_documents([{"path": path, "text": text}], zone_id="sharedzone")
        assert indexed["indexed"] == 1, indexed
        hits = await service.semantic_search(
            "marigold", path=path, search_mode="keyword", context=context
        )
        assert [hit["path"] for hit in hits] == [path], hits
        assert "marigold" in hits[0]["chunk_text"], hits
        assert hits[0]["score"] > 0, hits
        stats = await service.semantic_search_stats()
        assert stats["backend"] == "rust-plugin", stats
        stats = await daemon.get_stats(zone_id="sharedzone")
        assert stats["fts_doc_count"] >= 1, stats
        assert (
            await service.semantic_search(
                "absentuniquetoken", path=path, search_mode="keyword", context=context
            )
            == []
        )

        await asyncio.to_thread(transport.delete_file, path)
        created = False
        await daemon.notify_file_change(path, "delete", zone_id="sharedzone")
        assert (
            await service.semantic_search(
                "marigold", path=path, search_mode="keyword", context=context
            )
            == []
        )

        print("SearchService live contract passed: index/query/stats/delete")
    finally:
        if created:
            await asyncio.to_thread(transport.delete_file, path)
            await daemon.notify_file_change(path, "delete", zone_id="sharedzone")
        await daemon.shutdown()
        transport.close()
        request_api_key.reset(credential_scope)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))

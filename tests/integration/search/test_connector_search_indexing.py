"""Integration test: connector sync → search indexing → semantic search.

Validates the full e2e flow from Issue #3148:
  mount connector → sync → _index_mount_content → search_daemon.index_documents → search

Uses mock connector backend + real SearchDaemon with mock txtai backend.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.contracts.constants import ROOT_ZONE_ID

# ── Helpers ──────────────────────────────────────────────────────────


def _make_search_daemon(search_results=None):
    """Create a SearchDaemon with a mock txtai backend."""
    daemon = SearchDaemon()
    daemon._initialized = True
    daemon._backend = AsyncMock()
    daemon._backend.upsert.return_value = 3
    daemon._backend.last_rerank_ms = 0.0

    if search_results is not None:
        daemon._backend.search.return_value = search_results
    else:
        from nexus.bricks.search.results import BaseSearchResult

        daemon._backend.search.return_value = [
            BaseSearchResult(
                path="/mnt/gmail/INBOX/tid1-mid1.yaml",
                chunk_text="subject: Project Update\nfrom: alice@example.com\nsnippet: Q1 results",
                score=0.92,
            ),
        ]
    return daemon


def _make_mock_backend(dir_tree: dict[str, list[str]]):
    """Create a mock connector backend with list_dir returning the given tree.

    Args:
        dir_tree: Mapping of backend_path → list of entries.
            Directories end with '/'. Root is ''.
            Example: {'': ['INBOX/', 'SENT/'], 'INBOX': ['msg1.yaml', 'msg2.yaml']}
    """
    backend = MagicMock()

    def list_dir(path="", context=None):
        return dir_tree.get(path.strip("/") if path else "", [])

    backend.list_dir = list_dir
    return backend


def _make_mock_router(backend):
    """Create a mock router that returns the given backend for any path."""
    route = MagicMock()
    route.backend = backend

    router = MagicMock()
    router.route.return_value = route
    return router


def _make_mock_nexus_fs(file_contents):
    """Create a mock NexusFS that returns content for sys_read."""

    async def mock_sys_read(path, context=None):
        content = file_contents.get(path, "")
        if isinstance(content, str):
            content = content.encode("utf-8")
        return content

    nx = AsyncMock()
    nx.sys_read = mock_sys_read
    return nx


# ── Tests ────────────────────────────────────────────────────────────


class TestConnectorSearchIndexing:
    """Test connector content gets indexed and is searchable."""

    @pytest.mark.asyncio
    async def test_index_mount_content_indexes_connector_files(self):
        """_index_mount_content uses list_dir BFS and indexes via search daemon."""
        from nexus.bricks.mount.mount_service import MountService

        file_contents = {
            "/mnt/gmail/INBOX/tid1-mid1.yaml": (
                "subject: Project Update\nfrom: alice@example.com\nsnippet: Q1 results are in"
            ),
            "/mnt/gmail/INBOX/tid2-mid2.yaml": (
                "subject: Meeting Notes\nfrom: bob@example.com\nsnippet: Action items from standup"
            ),
            "/mnt/gmail/SENT/tid3-mid3.yaml": (
                "subject: Re: Budget\nto: carol@example.com\nsnippet: Approved the budget request"
            ),
        }

        backend = _make_mock_backend(
            {
                "": ["INBOX/", "SENT/"],
                "INBOX": ["tid1-mid1.yaml", "tid2-mid2.yaml"],
                "SENT": ["tid3-mid3.yaml"],
            }
        )
        router = _make_mock_router(backend)
        nx = _make_mock_nexus_fs(file_contents)
        daemon = _make_search_daemon()

        search_svc = MagicMock()
        search_svc._search_daemon = daemon

        mount_svc = MountService(
            router=router,
            mount_manager=MagicMock(),
            nexus_fs=nx,
            gateway=MagicMock(),
            sync_service=MagicMock(),
            search_service=search_svc,
        )

        await mount_svc._index_mount_content(
            "/mnt/gmail",
        )

        # Assert: search daemon received 3 documents in correct format
        daemon._backend.upsert.assert_awaited_once()
        call_args = daemon._backend.upsert.call_args
        documents = call_args[0][0]

        assert len(documents) == 3
        for doc in documents:
            assert "id" in doc
            assert "text" in doc
            assert "path" in doc
            assert doc["id"] == doc["path"]
            assert doc["id"].startswith("/mnt/gmail/")
            assert len(doc["text"]) > 10

        inbox_docs = [d for d in documents if "INBOX" in d["id"]]
        assert len(inbox_docs) == 2
        assert any("Project Update" in d["text"] for d in inbox_docs)
        assert any("Meeting Notes" in d["text"] for d in inbox_docs)

    @pytest.mark.asyncio
    async def test_index_mount_content_skips_non_text_files(self):
        """Only .yaml/.json/.md/.txt files should be indexed."""
        from nexus.bricks.mount.mount_service import MountService

        file_contents = {
            "/mnt/gmail/INBOX/msg1.yaml": "subject: Test email\nsnippet: Hello world",
            "/mnt/gmail/INBOX/notes.md": "# Meeting notes\nDiscussed project timeline",
        }

        backend = _make_mock_backend(
            {
                "": ["INBOX/"],
                "INBOX": ["msg1.yaml", "msg2.png", "msg3.bin", "notes.md"],
            }
        )
        router = _make_mock_router(backend)
        nx = _make_mock_nexus_fs(file_contents)
        daemon = _make_search_daemon()

        search_svc = MagicMock()
        search_svc._search_daemon = daemon

        mount_svc = MountService(
            router=router,
            mount_manager=MagicMock(),
            nexus_fs=nx,
            gateway=MagicMock(),
            sync_service=MagicMock(),
            search_service=search_svc,
        )

        await mount_svc._index_mount_content(
            "/mnt/gmail",
        )

        call_args = daemon._backend.upsert.call_args
        documents = call_args[0][0]
        assert len(documents) == 2
        paths = {d["id"] for d in documents}
        assert "/mnt/gmail/INBOX/msg1.yaml" in paths
        assert "/mnt/gmail/INBOX/notes.md" in paths

    @pytest.mark.asyncio
    async def test_index_then_search_finds_connector_content(self):
        """Full round-trip: index connector content → search finds it."""
        from nexus.bricks.search.results import BaseSearchResult

        daemon = _make_search_daemon()

        docs = [
            {
                "id": "/mnt/gmail/INBOX/tid1-mid1.yaml",
                "text": "subject: Q1 Budget Review\nfrom: cfo@company.com\nsnippet: Please review the Q1 budget numbers",
                "path": "/mnt/gmail/INBOX/tid1-mid1.yaml",
            },
            {
                "id": "/mnt/gmail/INBOX/tid2-mid2.yaml",
                "text": "subject: Standup Notes\nfrom: pm@company.com\nsnippet: Sprint retrospective action items",
                "path": "/mnt/gmail/INBOX/tid2-mid2.yaml",
            },
            {
                "id": "/mnt/gmail/SENT/tid3-mid3.yaml",
                "text": "subject: Re: Q1 Budget\nto: cfo@company.com\nsnippet: Budget approved with modifications",
                "path": "/mnt/gmail/SENT/tid3-mid3.yaml",
            },
        ]

        count = await daemon.index_documents(docs)
        assert count == 3

        daemon._backend.search.return_value = [
            BaseSearchResult(
                path="/mnt/gmail/INBOX/tid1-mid1.yaml",
                chunk_text="Q1 Budget Review",
                score=0.95,
            ),
            BaseSearchResult(
                path="/mnt/gmail/SENT/tid3-mid3.yaml",
                chunk_text="Budget approved with modifications",
                score=0.88,
            ),
        ]

        results = await daemon.search("budget review Q1", zone_id=ROOT_ZONE_ID)
        assert len(results) == 2
        assert results[0].path == "/mnt/gmail/INBOX/tid1-mid1.yaml"
        assert results[0].score > results[1].score

        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_delta_sync_new_emails_get_indexed(self):
        """After delta sync adds new emails, they should be indexed."""
        from nexus.bricks.mount.mount_service import MountService

        file_contents = {
            "/mnt/gmail/INBOX/tid1-mid1.yaml": "subject: Old email\nsnippet: Already indexed content",
            "/mnt/gmail/INBOX/tid2-mid2.yaml": "subject: Another old email\nsnippet: Previously indexed",
            "/mnt/gmail/INBOX/tid3-mid3.yaml": "subject: New email just arrived\nsnippet: Fresh content from delta sync",
        }

        # Backend now lists 3 files (including the new one after delta sync)
        backend = _make_mock_backend(
            {
                "": ["INBOX/"],
                "INBOX": ["tid1-mid1.yaml", "tid2-mid2.yaml", "tid3-mid3.yaml"],
            }
        )
        router = _make_mock_router(backend)
        nx = _make_mock_nexus_fs(file_contents)
        daemon = _make_search_daemon()

        search_svc = MagicMock()
        search_svc._search_daemon = daemon

        mount_svc = MountService(
            router=router,
            mount_manager=MagicMock(),
            nexus_fs=nx,
            gateway=MagicMock(),
            sync_service=MagicMock(),
            search_service=search_svc,
        )

        await mount_svc._index_mount_content(
            "/mnt/gmail",
        )

        call_args = daemon._backend.upsert.call_args
        documents = call_args[0][0]
        assert len(documents) == 3
        new_doc = [d for d in documents if "tid3-mid3" in d["id"]]
        assert len(new_doc) == 1
        assert "Fresh content from delta sync" in new_doc[0]["text"]

    @pytest.mark.asyncio
    async def test_fallback_to_semantic_search_index_when_no_daemon(self):
        """When _search_daemon is None, falls back to semantic_search_index."""
        from nexus.bricks.mount.mount_service import MountService

        search_svc = MagicMock()
        search_svc._search_daemon = None
        search_svc.semantic_search_index = AsyncMock(return_value={"/mnt/gmail": 50})

        mount_svc = MountService(
            router=MagicMock(),
            mount_manager=MagicMock(),
            gateway=MagicMock(),
            sync_service=MagicMock(),
            search_service=search_svc,
        )

        await mount_svc._index_mount_content(
            "/mnt/gmail",
        )

        search_svc.semantic_search_index.assert_awaited_once_with("/mnt/gmail", recursive=True)

    @pytest.mark.asyncio
    async def test_index_mount_content_no_backend_returns_early(self):
        """When router can't resolve a backend, indexing returns without error."""
        from nexus.bricks.mount.mount_service import MountService

        router = MagicMock()
        router.route.return_value = None  # No route found

        daemon = _make_search_daemon()
        search_svc = MagicMock()
        search_svc._search_daemon = daemon

        mount_svc = MountService(
            router=router,
            mount_manager=MagicMock(),
            nexus_fs=_make_mock_nexus_fs({}),
            gateway=MagicMock(),
            sync_service=MagicMock(),
            search_service=search_svc,
        )

        await mount_svc._index_mount_content(
            "/mnt/gmail",
        )

        # Should not crash; upsert should not be called
        daemon._backend.upsert.assert_not_awaited()

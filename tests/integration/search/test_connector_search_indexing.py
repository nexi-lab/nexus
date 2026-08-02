"""Integration test: connector sync → search indexing submission.

Validates the mount-side half of the Issue #3148 flow:
  mount connector → _index_mount_content → SearchDaemon.index_documents

Rewritten for the post-#3699/#4566 architecture (was asserting the old
txtai ``_backend.upsert`` daemon shape and a MountService constructor that
no longer exists):

- files are enumerated via the Rust kernel's ``sys_readdir`` BFS, read via
  the SYNC ``nx.sys_read``, and submitted to ``SearchDaemon.index_documents``;
- the daemon returns ``ExplicitIndexResult {indexed, skipped}`` (#4566) —
  connector-backed paths with no ``file_paths`` row come back in ``skipped``
  and the mount hook logs them instead of failing (best-effort contract);
- what happens INSIDE the daemon (path_id resolution, bounded projection
  wait, pipeline write) is covered by
  ``tests/unit/bricks/search/test_daemon_explicit_index_path_wait.py``.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.daemon import ExplicitIndexResult
from nexus.contracts.constants import ROOT_ZONE_ID

DT_DIR = 1  # kernel entry_type constant mirrored in mount_service BFS

# ── Helpers ──────────────────────────────────────────────────────────


def _make_daemon(result: ExplicitIndexResult | None = None) -> MagicMock:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(
        return_value=result if result is not None else ExplicitIndexResult(indexed=0, skipped=[])
    )
    return daemon


def _make_search_service(daemon: Any) -> MagicMock:
    search_svc = MagicMock()
    search_svc._search_daemon = daemon
    return search_svc


def _make_nexus_fs(
    dir_tree: dict[str, list[tuple[str, int]]],
    file_contents: dict[str, str],
) -> MagicMock:
    """Mock NexusFS with a kernel readdir BFS and a SYNC sys_read.

    Args:
        dir_tree: Mapping of directory path → [(child_path, entry_type)].
        file_contents: Mapping of file path → text content.
    """
    nx = MagicMock()

    def _sys_readdir(prefix: str, zone_id: str) -> list[tuple[str, int]]:
        if prefix not in dir_tree:
            raise FileNotFoundError(prefix)
        return dir_tree[prefix]

    nx._kernel.sys_readdir = MagicMock(side_effect=_sys_readdir)

    def _sys_read(path: str, context: Any = None) -> bytes:
        return file_contents.get(path, "").encode("utf-8")

    nx.sys_read = MagicMock(side_effect=_sys_read)
    return nx


def _make_mount_service(nx: Any, search_svc: Any) -> Any:
    from nexus.bricks.mount.mount_service import MountService

    return MountService(
        mount_manager=MagicMock(),
        nexus_fs=nx,
        search_service=search_svc,
    )


_GMAIL_TREE: dict[str, list[tuple[str, int]]] = {
    "/mnt/gmail": [("/mnt/gmail/INBOX", DT_DIR), ("/mnt/gmail/SENT", DT_DIR)],
    "/mnt/gmail/INBOX": [
        ("/mnt/gmail/INBOX/tid1-mid1.yaml", 0),
        ("/mnt/gmail/INBOX/tid2-mid2.yaml", 0),
    ],
    "/mnt/gmail/SENT": [("/mnt/gmail/SENT/tid3-mid3.yaml", 0)],
}

_GMAIL_CONTENTS = {
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

# ── Tests ────────────────────────────────────────────────────────────


class TestConnectorSearchIndexing:
    """Mount-content indexing submits the right documents to the daemon."""

    @pytest.mark.asyncio
    async def test_index_mount_content_indexes_connector_files(self) -> None:
        """BFS enumeration + content read → one index_documents call."""
        daemon = _make_daemon(ExplicitIndexResult(indexed=3, skipped=[]))
        nx = _make_nexus_fs(_GMAIL_TREE, _GMAIL_CONTENTS)
        mount_svc = _make_mount_service(nx, _make_search_service(daemon))

        await mount_svc._index_mount_content("/mnt/gmail")

        daemon.index_documents.assert_awaited_once()
        documents = daemon.index_documents.call_args[0][0]
        assert daemon.index_documents.call_args.kwargs["zone_id"] == ROOT_ZONE_ID
        assert {d["id"] for d in documents} == set(_GMAIL_CONTENTS)
        assert all(d["path"] == d["id"] for d in documents)
        inbox_docs = [d for d in documents if "INBOX" in d["id"]]
        assert any("Meeting Notes" in d["text"] for d in inbox_docs)

    @pytest.mark.asyncio
    async def test_index_mount_content_skips_non_text_files(self) -> None:
        """Only .yaml/.json/.md/.txt files should be submitted."""
        tree = {
            "/mnt/gmail": [("/mnt/gmail/INBOX", DT_DIR)],
            "/mnt/gmail/INBOX": [
                ("/mnt/gmail/INBOX/msg1.yaml", 0),
                ("/mnt/gmail/INBOX/msg2.png", 0),
                ("/mnt/gmail/INBOX/msg3.bin", 0),
                ("/mnt/gmail/INBOX/notes.md", 0),
            ],
        }
        contents = {
            "/mnt/gmail/INBOX/msg1.yaml": "subject: Test email\nsnippet: Hello world",
            "/mnt/gmail/INBOX/notes.md": "# Meeting notes\nDiscussed project timeline",
        }
        daemon = _make_daemon(ExplicitIndexResult(indexed=2, skipped=[]))
        mount_svc = _make_mount_service(
            _make_nexus_fs(tree, contents), _make_search_service(daemon)
        )

        await mount_svc._index_mount_content("/mnt/gmail")

        documents = daemon.index_documents.call_args[0][0]
        assert {d["id"] for d in documents} == {
            "/mnt/gmail/INBOX/msg1.yaml",
            "/mnt/gmail/INBOX/notes.md",
        }

    @pytest.mark.asyncio
    async def test_zone_id_threaded_to_daemon(self) -> None:
        """The sync-context zone must reach index_documents so zone-isolated
        search queries can find the indexed content."""
        daemon = _make_daemon(ExplicitIndexResult(indexed=3, skipped=[]))
        nx = _make_nexus_fs(_GMAIL_TREE, _GMAIL_CONTENTS)
        mount_svc = _make_mount_service(nx, _make_search_service(daemon))

        await mount_svc._index_mount_content("/mnt/gmail", zone_id="corp")

        assert daemon.index_documents.call_args.kwargs["zone_id"] == "corp"
        # BFS must also enumerate in the same zone.
        assert nx._kernel.sys_readdir.call_args_list[0][0][1] == "corp"

    @pytest.mark.asyncio
    async def test_skipped_documents_are_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#4566: connector paths without file_paths rows come back in
        ``skipped``. The mount hook is best-effort — it must log the skip
        count and keep the mount alive, never raise."""
        daemon = _make_daemon(
            ExplicitIndexResult(indexed=1, skipped=["/mnt/gmail/INBOX/tid2-mid2.yaml"])
        )
        nx = _make_nexus_fs(_GMAIL_TREE, _GMAIL_CONTENTS)
        mount_svc = _make_mount_service(nx, _make_search_service(daemon))

        with caplog.at_level(logging.INFO, logger="nexus.bricks.mount.mount_service"):
            await mount_svc._index_mount_content("/mnt/gmail")

        assert any("skipped=1" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_files_found_returns_without_indexing(self) -> None:
        """Unreadable mount root (readdir raises) → no daemon call, no crash."""
        daemon = _make_daemon()
        nx = _make_nexus_fs({}, {})  # every readdir raises FileNotFoundError
        mount_svc = _make_mount_service(nx, _make_search_service(daemon))

        await mount_svc._index_mount_content("/mnt/gmail")

        daemon.index_documents.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_to_semantic_search_index_when_no_daemon(self) -> None:
        """When _search_daemon is None, falls back to semantic_search_index."""
        search_svc = MagicMock()
        search_svc._search_daemon = None
        search_svc.semantic_search_index = AsyncMock(return_value={"/mnt/gmail": 50})
        mount_svc = _make_mount_service(_make_nexus_fs({}, {}), search_svc)

        await mount_svc._index_mount_content("/mnt/gmail")

        search_svc.semantic_search_index.assert_awaited_once_with("/mnt/gmail", recursive=True)

    @pytest.mark.asyncio
    async def test_delta_sync_new_emails_get_submitted(self) -> None:
        """After delta sync adds a new email, the next indexing pass submits
        all listed files — the daemon's chunk upsert is idempotent, so
        re-submitting already-indexed docs is safe."""
        tree = {
            "/mnt/gmail": [("/mnt/gmail/INBOX", DT_DIR)],
            "/mnt/gmail/INBOX": [
                ("/mnt/gmail/INBOX/tid1-mid1.yaml", 0),
                ("/mnt/gmail/INBOX/tid2-mid2.yaml", 0),
                ("/mnt/gmail/INBOX/tid3-mid3.yaml", 0),
            ],
        }
        contents = {
            "/mnt/gmail/INBOX/tid1-mid1.yaml": "subject: Old email\nsnippet: Already indexed",
            "/mnt/gmail/INBOX/tid2-mid2.yaml": "subject: Another old\nsnippet: Previously done",
            "/mnt/gmail/INBOX/tid3-mid3.yaml": (
                "subject: New email just arrived\nsnippet: Fresh content from delta sync"
            ),
        }
        daemon = _make_daemon(ExplicitIndexResult(indexed=3, skipped=[]))
        mount_svc = _make_mount_service(
            _make_nexus_fs(tree, contents), _make_search_service(daemon)
        )

        await mount_svc._index_mount_content("/mnt/gmail")

        documents = daemon.index_documents.call_args[0][0]
        assert len(documents) == 3
        new_doc = [d for d in documents if "tid3-mid3" in d["id"]]
        assert len(new_doc) == 1
        assert "Fresh content from delta sync" in new_doc[0]["text"]

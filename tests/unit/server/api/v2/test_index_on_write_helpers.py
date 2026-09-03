"""Unit tests for ``_index_on_write`` — the write-to-searchable helpers (#4736)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from nexus.server.api.v2.routers._index_on_write import (
    REASON_EMPTY,
    REASON_NON_TEXT,
    REINDEX_BATCH_DOCS,
    REINDEX_MAX_DOC_BYTES,
    IndexOnWrite,
    ReindexSearchOutcome,
    effective_index_option,
    index_after_batch_write,
    index_requested,
    index_text_for,
    reindex_paths,
    to_epoch_ms,
    verdict_for_path,
)


def test_to_epoch_ms_accepts_every_shape_the_vfs_hands_back() -> None:
    aware = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    ms = int(aware.timestamp() * 1000)
    assert to_epoch_ms(aware) == ms
    assert to_epoch_ms(aware.replace(tzinfo=None)) == ms, "naive datetimes are UTC"
    assert to_epoch_ms(aware.isoformat()) == ms
    assert to_epoch_ms(ms) == ms
    assert to_epoch_ms(ms / 1000) == ms, "epoch seconds are scaled to ms"
    assert to_epoch_ms(None) is None
    assert to_epoch_ms("not a date") is None
    assert to_epoch_ms(True) is None


def test_index_requested_and_effective_option() -> None:
    assert index_requested(True)
    assert index_requested(IndexOnWrite())
    assert not index_requested(False)
    assert not index_requested(None)
    assert effective_index_option(None, True) is True
    assert effective_index_option(False, True) is False
    assert isinstance(effective_index_option(IndexOnWrite(text="t"), None), IndexOnWrite)


def test_index_text_for_prefers_the_override_then_strict_utf8() -> None:
    assert index_text_for(IndexOnWrite(text="alt"), b"\xff") == "alt"
    assert index_text_for(IndexOnWrite(), b"hi") == "hi"
    assert index_text_for(True, b"hi") == "hi"
    assert index_text_for(True, b"\xff\xfe") is None


def test_verdict_for_path_reads_dicts_and_legacy_doubles() -> None:
    result = {"indexed": 1, "skipped_paths": ["/e.md"], "index_seq": 9}
    indexed = verdict_for_path(result, "/a.md")
    assert (indexed.status, indexed.index_seq) == ("indexed", 9)
    skipped = verdict_for_path(result, "/e.md")
    assert (skipped.status, skipped.reason) == ("skipped", REASON_EMPTY)
    legacy = verdict_for_path(SimpleNamespace(indexed=1), "/a.md")
    assert (legacy.status, legacy.index_seq) == ("indexed", None)


def test_index_after_batch_write_indexes_only_what_was_asked_in_one_call() -> None:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(
        return_value={"indexed": 2, "skipped_paths": ["/empty.md"], "index_seq": 3}
    )
    entries = [
        ("/a.md", b"alpha", True, None),
        ("/blob", b"\xff\xfe", True, None),
        ("/no.md", b"nope", False, None),
        ("/empty.md", b"   ", True, None),
        ("/scan.pdf", b"\x00", IndexOnWrite(text="pdf words"), 1_700_000_000_000),
    ]

    verdicts = asyncio.run(index_after_batch_write(daemon, entries, zone_id="z"))

    assert set(verdicts) == {"/a.md", "/blob", "/empty.md", "/scan.pdf"}
    assert (verdicts["/a.md"].status, verdicts["/a.md"].index_seq) == ("indexed", 3)
    assert (verdicts["/blob"].status, verdicts["/blob"].reason) == ("skipped", REASON_NON_TEXT)
    assert (verdicts["/empty.md"].status, verdicts["/empty.md"].reason) == ("skipped", REASON_EMPTY)
    assert verdicts["/scan.pdf"].status == "indexed"

    daemon.index_documents.assert_awaited_once()
    docs = daemon.index_documents.await_args.args[0]
    assert docs == [
        {"path": "/a.md", "text": "alpha"},
        {"path": "/empty.md", "text": "   "},
        {"path": "/scan.pdf", "text": "pdf words", "mtime_ms": 1_700_000_000_000},
    ]
    assert daemon.index_documents.await_args.kwargs == {"zone_id": "z"}


def test_index_after_batch_write_plugin_failure_marks_every_sent_doc_error() -> None:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(side_effect=RuntimeError("plugin down"))
    entries = [("/a.md", b"alpha", True, None), ("/blob", b"\xff", True, None)]

    verdicts = asyncio.run(index_after_batch_write(daemon, entries, zone_id=None))

    assert verdicts["/a.md"].status == "error"
    assert "plugin down" in (verdicts["/a.md"].error or "")
    assert verdicts["/blob"].status == "skipped", "local skips never reach the plugin"


def test_index_after_batch_write_with_nothing_requested_skips_the_plugin() -> None:
    daemon = MagicMock()
    daemon.index_documents = AsyncMock()
    verdicts = asyncio.run(
        index_after_batch_write(daemon, [("/a.md", b"alpha", None, None)], zone_id=None)
    )
    assert verdicts == {}
    daemon.index_documents.assert_not_awaited()


# ── reindex_paths (admin /reindex, #4241) ─────────────────────────────


class _FS:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def read(self, path: str, *, context=None, **_):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        if path not in self.files:
            raise NexusFileNotFoundError(path)
        return self.files[path]

    def sys_stat(self, path: str, *, context=None, **_):
        return {"modified_at_ms": 1_700_000_000_000}


def _reindex_daemon(*, skipped_paths: list[str] | None = None, seq: int = 7) -> MagicMock:
    daemon = MagicMock()
    calls: list[list[dict]] = []

    async def _index(docs, *, zone_id):
        calls.append(list(docs))
        return {
            "indexed": len(docs),
            "skipped_paths": skipped_paths or [],
            "index_seq": seq + len(calls),
        }

    async def _notify(path, change_type, *, zone_id):
        return {"status": "accepted", "index_seq": 100}

    daemon.index_documents = AsyncMock(side_effect=_index)
    daemon.notify_file_change = AsyncMock(side_effect=_notify)
    daemon.calls = calls
    return daemon


def test_reindex_paths_buckets_every_path_exactly_once() -> None:
    fs = _FS(
        {
            "/a.md": b"alpha text",
            "/b.md": b"bravo text",
            "/blob.bin": b"\xff\xfe\x00",
            "/big.txt": b"x" * (REINDEX_MAX_DOC_BYTES + 1),
            "/empty.md": b"   ",
        }
    )
    daemon = _reindex_daemon(skipped_paths=["/empty.md"])
    changes = {
        "/a.md": "update",
        "/b.md": "update",
        "/blob.bin": "update",
        "/big.txt": "update",
        "/empty.md": "update",
        "/missing.md": "update",
        "/gone.md": "delete",
    }

    out = asyncio.run(reindex_paths(daemon, fs, context=None, changes=changes, zone_id="eng"))

    assert out.indexed == 2
    assert out.deleted == 1
    assert out.skipped == 3
    assert out.skip_reasons == {"non_text": 1, "oversize": 1, "empty": 1}
    assert sorted(out.skipped_paths) == ["/big.txt", "/blob.bin", "/empty.md"]
    assert out.errors == 1 and out.failed_paths == ["/missing.md"]
    assert "404" in (out.first_error or "")
    assert out.indexed + out.deleted + out.skipped + out.errors == len(changes)
    # Highest seq wins: the evict returned 100, the batch 8.
    assert out.index_seq == 100
    assert out.completed_at is not None

    # One IndexDocuments call for the three readable text docs, in the
    # token zone, with the stat mtime attached; the binary and oversize
    # files never reached the plugin.
    assert len(daemon.calls) == 1
    assert [d["path"] for d in daemon.calls[0]] == ["/a.md", "/b.md", "/empty.md"]
    assert daemon.index_documents.await_args.kwargs == {"zone_id": "eng"}
    assert all(d["mtime_ms"] == 1_700_000_000_000 for d in daemon.calls[0])
    daemon.notify_file_change.assert_awaited_once_with("/gone.md", "delete", zone_id="eng")


def test_reindex_paths_batches_large_replays() -> None:
    n = REINDEX_BATCH_DOCS * 2 + 5
    fs = _FS({f"/doc{i}.md": f"document {i}".encode() for i in range(n)})
    daemon = _reindex_daemon()

    out = asyncio.run(
        reindex_paths(
            daemon, fs, context=None, changes=dict.fromkeys(fs.files, "update"), zone_id="z"
        )
    )

    assert out.indexed == n
    assert [len(c) for c in daemon.calls] == [REINDEX_BATCH_DOCS, REINDEX_BATCH_DOCS, 5]


def test_reindex_paths_plugin_failure_marks_the_batch_failed_not_the_pass() -> None:
    fs = _FS({"/a.md": b"alpha", "/b.md": b"bravo"})
    daemon = MagicMock()
    daemon.index_documents = AsyncMock(side_effect=RuntimeError("plugin down"))
    daemon.notify_file_change = AsyncMock(side_effect=RuntimeError("plugin down"))

    out = asyncio.run(
        reindex_paths(
            daemon,
            fs,
            context=None,
            changes={"/a.md": "update", "/b.md": "update", "/c.md": "delete"},
            zone_id="z",
        )
    )

    assert out.errors == 3 and out.indexed == 0 and out.deleted == 0
    assert sorted(out.failed_paths) == ["/a.md", "/b.md", "/c.md"]
    assert "plugin down" in (out.first_error or "")


def test_reindex_outcome_unavailable_fails_every_path_with_the_reason() -> None:
    out = ReindexSearchOutcome.unavailable({"/a": "update", "/b": "delete"}, "search unavailable")
    assert out.errors == 2
    assert out.failed_paths == ["/a", "/b"]
    assert out.first_error == "search unavailable"
    assert out.indexed == out.deleted == out.skipped == 0


def test_reindex_paths_skips_non_vfs_paths_without_touching_the_plugin() -> None:
    daemon = _reindex_daemon()
    out = asyncio.run(
        reindex_paths(
            daemon,
            _FS({}),
            context=None,
            changes={"urn:nexus:file:root:abc": "update", "relative.md": "delete"},
            zone_id="z",
        )
    )
    assert out.skipped == 2 and out.errors == 0
    assert out.skip_reasons == {"not_a_path": 2}
    daemon.index_documents.assert_not_awaited()
    daemon.notify_file_change.assert_not_awaited()


def test_reindex_paths_read_permission_error_is_a_per_path_failure() -> None:
    class _DenyFS(_FS):
        def read(self, path, *, context=None, **_):
            raise HTTPException(status_code=403, detail="nope")

    daemon = _reindex_daemon()
    out = asyncio.run(
        reindex_paths(daemon, _DenyFS({}), context=None, changes={"/a.md": "update"}, zone_id="z")
    )
    assert out.errors == 1
    assert "403" in (out.first_error or "")
    daemon.index_documents.assert_not_awaited()

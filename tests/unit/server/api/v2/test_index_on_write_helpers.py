"""Unit tests for ``_index_on_write`` — the write-to-searchable helpers (#4736)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from nexus.server.api.v2.routers._index_on_write import (
    REASON_EMPTY,
    REASON_NON_TEXT,
    IndexOnWrite,
    effective_index_option,
    index_after_batch_write,
    index_requested,
    index_text_for,
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

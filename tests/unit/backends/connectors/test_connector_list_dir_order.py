"""Regression tests: gmail + calendar list_dir must default to newest-first.

Message/event filenames are date-prefixed (``YYYY-MM-DD_...``), so
reverse-lex = reverse-chronological = what every email/calendar client
shows — most recent at the top.  Agent flows like "read the latest
email from X" rely on ``fs.ls(...)[0]`` being the newest item; falling
back to ascending sort silently returns the oldest item and breaks
that contract.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.connectors.calendar.connector import PathCalendarBackend
from nexus.backends.connectors.gmail.connector import PathGmailBackend
from nexus.contracts.types import OperationContext


def _ctx() -> OperationContext:
    return OperationContext(user_id="tester", groups=[])


def _sample_gmail_keys(folder: str) -> list[str]:
    """Three date-prefixed message keys, given in arbitrary order."""
    return [
        f"{folder}/2025-01-15_middle-subject__abc-111.yaml",
        f"{folder}/2025-02-20_newest-subject__abc-222.yaml",
        f"{folder}/2024-12-01_oldest-subject__abc-333.yaml",
    ]


def _make_gmail_backend(transport: MagicMock) -> PathGmailBackend:
    backend = PathGmailBackend.__new__(PathGmailBackend)
    backend._pool = None
    backend._transport = transport
    return backend


def _make_calendar_backend(transport: MagicMock) -> PathCalendarBackend:
    backend = PathCalendarBackend.__new__(PathCalendarBackend)
    backend._transport = transport
    return backend


def test_gmail_date_prefix_includes_intra_day_time() -> None:
    """Same-day messages must sort by actual time, not by subject.
    The ``_date_prefix_for_key`` helper emits UTC-normalized
    ``YYYY-MM-DDTHH:MM:SSZ`` so reverse-lex order reflects real recency
    across time-zone offsets, not local wall-clock time."""
    from nexus.backends.connectors.gmail.transport import GmailTransport

    # ISO-8601 with UTC offset stays UTC.
    assert (
        GmailTransport._date_prefix_for_key("2026-04-21T14:32:10+00:00") == "2026-04-21T14:32:10Z"
    )
    # Naive / space-separated assumed UTC.
    assert GmailTransport._date_prefix_for_key("2026-04-21 09:15:00") == "2026-04-21T09:15:00Z"
    # Date-only stays as-is (all-day case — no time component).
    assert GmailTransport._date_prefix_for_key("2026-04-21") == "2026-04-21"
    # RFC-2822 parses into UTC-normalized ISO with the Z marker.
    out = GmailTransport._date_prefix_for_key("Mon, 21 Apr 2026 08:15:03 +0000")
    assert out == "2026-04-21T08:15:03Z", out


def test_gmail_date_prefix_normalizes_timezone_offsets() -> None:
    """Two messages at the same UTC instant from different offsets must
    produce the same sort prefix — otherwise reverse-lex newest-first
    would disagree with real chronology when mailboxes mix offsets."""
    from nexus.backends.connectors.gmail.transport import GmailTransport

    # Same instant, three ways of expressing it.
    tokyo = GmailTransport._date_prefix_for_key("2026-04-21T23:30:00+09:00")
    utc = GmailTransport._date_prefix_for_key("2026-04-21T14:30:00+00:00")
    la = GmailTransport._date_prefix_for_key("2026-04-21T07:30:00-07:00")
    assert tokyo == utc == la == "2026-04-21T14:30:00Z"

    # Across-midnight: 23:00 -08:00 is 07:00 next-day UTC.  Reverse-lex
    # must sort it AFTER a 12:00 UTC message on the prior day.
    late_la = GmailTransport._date_prefix_for_key("2026-04-21T23:00:00-08:00")
    early_utc = GmailTransport._date_prefix_for_key("2026-04-22T07:00:00+00:00")
    assert late_la == early_utc == "2026-04-22T07:00:00Z"


def test_gmail_list_dir_sorts_same_day_by_time() -> None:
    """End-to-end: two same-day messages with different times sort
    by time desc, not by subject string."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend

    transport = MagicMock()
    transport.list_keys.return_value = (
        [
            "INBOX/2026-04-21T08:00:00_aaa-alpha__a1-a1.yaml",
            "INBOX/2026-04-21T17:30:00_zzz-late-message__z1-z1.yaml",
            "INBOX/2026-04-21T12:45:00_mmm-mid__m1-m1.yaml",
        ],
        [],
    )
    backend = PathGmailBackend.__new__(PathGmailBackend)
    backend._pool = None
    backend._transport = transport

    with patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None):
        entries = backend.list_dir("INBOX", _ctx())

    assert entries[0].startswith("2026-04-21T17:30:00_"), f"expected 17:30 first, got {entries}"
    assert entries[1].startswith("2026-04-21T12:45:00_"), entries
    assert entries[2].startswith("2026-04-21T08:00:00_"), entries


def test_gmail_list_dir_is_newest_first() -> None:
    transport = MagicMock()
    transport.list_keys.return_value = (_sample_gmail_keys("INBOX"), [])
    backend = _make_gmail_backend(transport)

    with patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None):
        entries = backend.list_dir("INBOX", _ctx())

    assert entries[0].startswith("2025-02-20_newest-"), f"expected newest first, got {entries}"
    assert entries[-1].startswith("2024-12-01_oldest-"), f"expected oldest last, got {entries}"


def test_gmail_list_dir_separates_dirs_and_files() -> None:
    """Category sub-labels (directories) stay alphabetical; message
    leaves sort newest-first.  Mixing them in a single reverse-sort
    would surface CATEGORY_UPDATES/ above the most recent message,
    which is confusing."""
    transport = MagicMock()
    transport.list_keys.return_value = (
        _sample_gmail_keys("INBOX"),
        ["INBOX/PRIMARY/", "INBOX/social/"],
    )
    backend = _make_gmail_backend(transport)

    with patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None):
        entries = backend.list_dir("INBOX", _ctx())

    dirs = [e for e in entries if e.endswith("/")]
    leaves = [e for e in entries if not e.endswith("/")]
    assert dirs == ["PRIMARY/", "social/"], f"dirs should be alphabetical, got {dirs}"
    assert leaves[0].startswith("2025-02-20_"), leaves


def test_gmail_list_dir_accepts_inbox_category() -> None:
    """Category listing path must be reachable end-to-end: list_dir
    routes ``INBOX/<category>`` to the transport without the "Directory
    not found" guard that used to reject these paths before the
    category-dir work landed."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend

    transport = MagicMock()
    transport.list_keys.return_value = (
        [
            "INBOX/PRIMARY/2025-02-20_newest__msg-1.yaml",
            "INBOX/PRIMARY/2024-12-01_oldest__msg-2.yaml",
        ],
        [],
    )
    backend = PathGmailBackend.__new__(PathGmailBackend)
    backend._pool = None
    backend._transport = transport

    with patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None):
        entries = backend.list_dir("INBOX/PRIMARY", _ctx())

    # Leaves only, no sub-dirs — newest-first.
    assert entries[0].startswith("2025-02-20_newest"), entries
    assert entries[-1].startswith("2024-12-01_oldest"), entries
    transport.list_keys.assert_called_once_with(prefix="INBOX/PRIMARY", delimiter="/")


def test_gmail_list_dir_rejects_unknown_inbox_category() -> None:
    """Unknown category names must still raise FileNotFoundError — the
    relaxed `is_label OR is_inbox_category` guard must not let arbitrary
    two-segment paths through."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend

    backend = PathGmailBackend.__new__(PathGmailBackend)
    backend._pool = None
    backend._transport = MagicMock()

    with (
        patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None),
        pytest.raises(FileNotFoundError),
    ):
        backend.list_dir("INBOX/not_a_real_category", _ctx())


def test_gmail_list_dir_propagates_backend_errors() -> None:
    """Transport-layer failures while listing a category must surface
    — swallowing BackendError here would look like "empty primary" to
    the agent, hiding throttling / outage / auth incidents."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend
    from nexus.contracts.exceptions import BackendError

    transport = MagicMock()
    transport.list_keys.side_effect = BackendError("gmail API 503", backend="gmail")
    backend = PathGmailBackend.__new__(PathGmailBackend)
    backend._pool = None
    backend._transport = transport

    with (
        patch.object(PathGmailBackend, "_bind_transport", lambda self, ctx: None),
        pytest.raises(BackendError, match="gmail API 503"),
    ):
        backend.list_dir("INBOX/PRIMARY", _ctx())


def test_gmail_is_directory_matches_list_dir_acceptance() -> None:
    """is_directory() and list_dir() must agree on which virtual paths
    are directories.  Category sub-labels (INBOX/PRIMARY etc.) are
    listable — they must also report as directories to keep stat and
    traversal behavior consistent."""
    from nexus.backends.connectors.gmail.connector import PathGmailBackend
    from nexus.backends.connectors.gmail.transport import _GMAIL_CATEGORY_FOLDERS

    backend = PathGmailBackend.__new__(PathGmailBackend)

    assert backend.is_directory("/") is True
    assert backend.is_directory("/INBOX") is True
    for category in _GMAIL_CATEGORY_FOLDERS:
        assert backend.is_directory(f"/INBOX/{category}") is True, category
    # Non-INBOX labels don't get category sub-folders.
    assert backend.is_directory("/SENT/primary") is False
    # Arbitrary deeper paths are not directories.
    assert backend.is_directory("/INBOX/PRIMARY/deeper") is False
    assert backend.is_directory("/INBOX/bogus_category") is False


def test_sys_readdir_propagates_connector_backend_error(tmp_path: Path) -> None:
    """End-to-end: a connector list failure must surface through the
    kernel's sys_readdir (the layer the slim facade and the server API
    both call) instead of being silently collapsed into an empty list
    by the metastore fallback."""
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.exceptions import BackendError
    from nexus.contracts.metadata import DT_EXTERNAL_STORAGE, DT_MOUNT
    from nexus.contracts.types import OperationContext
    from nexus.core.config import PermissionConfig
    from nexus.core.nexus_fs import NexusFS
    from nexus.fs import _make_mount_entry
    from nexus.fs._sqlite_meta import SQLiteMetastore

    class _ExplodingBackend:
        name = "exploding_connector"
        has_root_path = False

        def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
            raise BackendError("connector 503", backend="exploding_connector")

        def read_content(self, cid: str, context: OperationContext | None = None) -> bytes:
            raise BackendError("not needed", backend="exploding_connector")

    metastore = SQLiteMetastore(str(tmp_path / "m.db"))
    backend = _ExplodingBackend()
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        init_cred=OperationContext(user_id="u", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True),
    )
    kernel.sys_setattr("/ext", entry_type=DT_MOUNT, backend=backend, is_external=True)
    metastore.put(_make_mount_entry("/ext", backend.name, entry_type=DT_EXTERNAL_STORAGE))

    with pytest.raises(BackendError, match="connector 503"):
        kernel.sys_readdir("/ext", context=kernel._init_cred)


def test_gmail_internal_date_outranks_sender_date_header() -> None:
    """``internalDate`` is Gmail's own receive-time; when present it
    must take precedence over the sender-controlled ``Date`` header
    so a sender cannot spoof the sort order by backdating their
    outbound clock."""
    from nexus.backends.connectors.gmail.transport import GmailTransport

    # Sender claims 2020 but server received in 2025 — must sort as 2025.
    prefix = GmailTransport._date_prefix_from_internal_date("1745246700000")
    # 2025-04-21T14:45:00.000Z in UTC epoch-ms (ms precision preserved).
    assert prefix == "2025-04-21T14:45:00.000Z", prefix
    # Missing / empty / junk: caller falls back to Date-header parsing.
    assert GmailTransport._date_prefix_from_internal_date("") == ""
    assert GmailTransport._date_prefix_from_internal_date("not-a-number") == ""
    assert GmailTransport._date_prefix_from_internal_date("-1") == ""


def test_gmail_internal_date_degrades_on_overflow(caplog: pytest.LogCaptureFixture) -> None:
    """An oversized or junk ``internalDate`` must not nuke the whole
    folder listing.  ``datetime.fromtimestamp`` raises OverflowError /
    OSError for values past the platform time_t limit, which would
    otherwise bubble all the way up through ``list_keys``.  Guard:
    fall back to empty prefix + emit a structured warning so ops can
    spot provider drift instead of losing ordering integrity silently."""
    import logging

    from nexus.backends.connectors.gmail.transport import GmailTransport

    with caplog.at_level(logging.WARNING, logger="nexus.backends.connectors.gmail.transport"):
        # Way past year 9999 — triggers OverflowError on CPython.
        assert GmailTransport._date_prefix_from_internal_date("99999999999999999") == ""
        # A huge but still positive-integer string in a different shape.
        assert GmailTransport._date_prefix_from_internal_date(str(10**20)) == ""
    # Each failed parse must produce a warning so silent degradation
    # doesn't hide ordering instability.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2, warnings


def test_gmail_internal_date_warns_on_non_integer_and_negative(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any malformed ``internalDate`` — not just overflow — must emit
    a warning so silent fallback to the Date header doesn't mask
    upstream payload drift."""
    import logging

    from nexus.backends.connectors.gmail.transport import GmailTransport

    with caplog.at_level(logging.WARNING, logger="nexus.backends.connectors.gmail.transport"):
        assert GmailTransport._date_prefix_from_internal_date("not-a-number") == ""
        assert GmailTransport._date_prefix_from_internal_date("-1") == ""
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2, warnings
    # The empty-input path must NOT warn — that's the normal "no
    # internalDate available" case and would be pure noise.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="nexus.backends.connectors.gmail.transport"):
        assert GmailTransport._date_prefix_from_internal_date("") == ""
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_gmail_internal_date_preserves_millisecond_precision() -> None:
    """Two messages arriving in the same second must sort by ms, not
    by subject — otherwise burst traffic can invert ``ls()[0]``."""
    from nexus.backends.connectors.gmail.transport import GmailTransport

    earlier = GmailTransport._date_prefix_from_internal_date("1745246700123")
    later = GmailTransport._date_prefix_from_internal_date("1745246700456")
    assert earlier == "2025-04-21T14:45:00.123Z", earlier
    assert later == "2025-04-21T14:45:00.456Z", later
    # Lex-sort reflects real ms order.
    assert earlier < later


def test_gmail_format_readable_key_prefers_internal_date() -> None:
    """End-to-end: when both Date header and internalDate are supplied,
    the sort prefix must come from internalDate."""
    from nexus.backends.connectors.gmail.transport import GmailTransport

    meta = {
        "subject": "hello",
        # Sender-supplied Date claims 2020.
        "date": "Wed, 1 Jan 2020 00:00:00 +0000",
        # Server received on 2025-04-21 14:45 UTC.
        "internal_date_ms": "1745246700000",
    }
    key = GmailTransport._format_readable_key("INBOX", "t1", "m1", meta)
    assert key.startswith("INBOX/2025-04-21T14:45:00.000Z_"), key
    # With only Date (no internalDate), falls back to RFC-2822 parsing.
    meta2 = {"subject": "x", "date": "Wed, 1 Jan 2020 00:00:00 +0000"}
    key2 = GmailTransport._format_readable_key("INBOX", "t2", "m2", meta2)
    assert key2.startswith("INBOX/2020-01-01T00:00:00Z_"), key2


def test_calendar_sort_fallback_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Silent fallback to coarse keys hides provider-payload drift.
    A warning must fire whenever date/zone parsing fails — including
    the naive dateTime path that previously dropped to UTC silently."""
    import logging

    from nexus.backends.connectors.calendar.transport import _utc_sort_prefix

    with caplog.at_level(logging.WARNING, logger="nexus.backends.connectors.calendar.transport"):
        # All-day with no usable zone → date-only fallback, must warn.
        _utc_sort_prefix("2026-04-21", timezone_hint="Not/Real", fallback_timezone="Also/Bad")
        # Unparseable dateTime → warns too.
        _utc_sort_prefix("garbage", timezone_hint="UTC")
        # Naive dateTime with no usable zones → UTC fallback warns too;
        # this path was silent before R10.
        _utc_sort_prefix(
            "2026-04-21T09:00:00", timezone_hint="Not/Real", fallback_timezone="Also/Bad"
        )
        _utc_sort_prefix("2026-04-21T09:00:00")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 4, warnings


def test_calendar_invalid_event_tz_falls_back_to_calendar_default() -> None:
    """A bad ``start.timeZone`` must not shadow a valid calendar-level
    default.  The fallback chain is: event zone → calendar default →
    UTC.  Dropping straight to UTC on an invalid event zone would
    misdate events by hours when the calendar itself has a valid zone."""
    from nexus.backends.connectors.calendar.transport import _utc_sort_prefix

    # All-day path: bad event zone, good calendar default.
    prefix = _utc_sort_prefix(
        "2026-04-21", timezone_hint="Not/Real", fallback_timezone="Asia/Tokyo"
    )
    # Tokyo midnight 2026-04-21 = 2026-04-20T15:00:00Z.
    assert prefix == "2026-04-20T15:00:00Z", prefix

    # Naive dateTime path: bad event zone, good calendar default.
    prefix2 = _utc_sort_prefix(
        "2026-04-21T09:00:00",
        timezone_hint="Not/Real",
        fallback_timezone="America/Los_Angeles",
    )
    # 09:00 LA (PDT -07:00) = 16:00 UTC.
    assert prefix2 == "2026-04-21T16:00:00Z", prefix2

    # Both invalid: degrades to UTC (naive) or YYYY-MM-DD (all-day).
    assert (
        _utc_sort_prefix(
            "2026-04-21T09:00:00",
            timezone_hint="Not/Real",
            fallback_timezone="Also/Bad",
        )
        == "2026-04-21T09:00:00Z"
    )
    assert (
        _utc_sort_prefix("2026-04-21", timezone_hint="Not/Real", fallback_timezone="Also/Bad")
        == "2026-04-21"
    )


def test_calendar_naive_datetime_uses_timezone_hint() -> None:
    """Google Calendar can send ``dateTime`` without an offset when
    ``timeZone`` is set separately (common for recurring-event instances).
    A naive timestamp must be anchored to ``timezone_hint`` — treating
    it as UTC would misdate the event by the hint's offset."""
    from nexus.backends.connectors.calendar.transport import _utc_sort_prefix

    # 09:00 LA = 16:00 UTC (or 17:00 during DST — April 21 is PDT, -07:00).
    prefix = _utc_sort_prefix("2026-04-21T09:00:00", timezone_hint="America/Los_Angeles")
    assert prefix == "2026-04-21T16:00:00Z", prefix

    # Tokyo 09:00 = 00:00 UTC same day.
    assert (
        _utc_sort_prefix("2026-04-21T09:00:00", timezone_hint="Asia/Tokyo")
        == "2026-04-21T00:00:00Z"
    )

    # Naive input with a bad zone hint falls back to UTC rather than
    # crashing, so one misconfigured event doesn't break a whole list.
    assert (
        _utc_sort_prefix("2026-04-21T09:00:00", timezone_hint="Not/Real") == "2026-04-21T09:00:00Z"
    )


def test_calendar_all_day_event_anchors_to_event_timezone() -> None:
    """All-day events in non-UTC calendars must anchor to midnight in
    that zone — otherwise a Tokyo all-day event sorts before a Tokyo
    23:00 timed event on the same local day, even though the timed
    event's UTC instant is earlier."""
    from nexus.backends.connectors.calendar.transport import _utc_sort_prefix

    # Tokyo 2026-04-21 all-day = 2026-04-20T15:00:00Z (00:00+09:00).
    all_day_tokyo = _utc_sort_prefix("2026-04-21", timezone_hint="Asia/Tokyo")
    assert all_day_tokyo == "2026-04-20T15:00:00Z", all_day_tokyo

    # A Tokyo 23:00 timed event = 2026-04-21T14:00:00Z
    timed_tokyo = _utc_sort_prefix("2026-04-21T23:00:00+09:00")
    # Real chronological order:
    #   all-day (starts at 00:00 Tokyo) < 23:00 timed event
    # Lex-sort of prefix reflects that: earlier instant has smaller prefix.
    assert all_day_tokyo < timed_tokyo, (all_day_tokyo, timed_tokyo)

    # Unknown / bad zone falls back to raw YYYY-MM-DD (monotonic within
    # the bad group, best we can do without a real zone).
    assert _utc_sort_prefix("2026-04-21", timezone_hint="Not/Real") == "2026-04-21"
    # Missing zone also falls back.
    assert _utc_sort_prefix("2026-04-21") == "2026-04-21"


def test_calendar_utc_sort_prefix_normalizes_offsets() -> None:
    """Events at the same UTC instant from different offsets must share
    a sort prefix — otherwise cross-timezone calendars misorder in
    reverse-lex ``list_dir``."""
    from nexus.backends.connectors.calendar.transport import _utc_sort_prefix

    tokyo = _utc_sort_prefix("2026-04-21T23:30:00+09:00")
    utc = _utc_sort_prefix("2026-04-21T14:30:00+00:00")
    la = _utc_sort_prefix("2026-04-21T07:30:00-07:00")
    assert tokyo == utc == la == "2026-04-21T14:30:00Z"
    # All-day events stay as YYYY-MM-DD (no time to normalize).
    assert _utc_sort_prefix("2026-04-21") == "2026-04-21"
    # Empty / unparseable stays empty — caller falls back to id-only key.
    assert _utc_sort_prefix("") == ""
    assert _utc_sort_prefix("not-a-date") == ""


def test_calendar_list_dir_is_newest_first() -> None:
    transport = MagicMock()
    transport.list_keys.return_value = (
        [
            "primary/2025-01-15_standup__event-111.yaml",
            "primary/2025-02-20_demo__event-222.yaml",
            "primary/2024-12-01_planning__event-333.yaml",
        ],
        [],
    )
    backend = _make_calendar_backend(transport)

    with patch.object(PathCalendarBackend, "_bind_transport", lambda self, ctx: None):
        entries = backend.list_dir("primary", _ctx())

    assert entries[0].startswith("2025-02-20_demo"), f"expected newest first, got {entries}"
    assert entries[-1].startswith("2024-12-01_planning"), f"expected oldest last, got {entries}"

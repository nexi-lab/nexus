"""E2E tests for GWS CLI connector through the slim package (nexus-fs) layer.

Exercises the full wiring:
    gws:// URI  →  _create_connector_backend  →  ConnectorRegistry
    →  GmailConnector / CalendarConnector  →  list_dir / read_content

All gws CLI subprocess calls are mocked — no real OAuth tokens or gws
binary required.  The goal is to verify that:

1. list_dir paginates correctly (not capped at 50).
2. read_content returns structured body from the MIME tree.
3. CalendarConnector root listing derives month folders.
4. User identity is included in the Gmail ID-list cache key (no cross-user leak).
5. as_yaml / as_json on CLIResult handle preamble, arrays, and scalar rejection.

The slim package entrypoint is nexus.fs._backend_factory._create_connector_backend,
which is the same code path used when a user mounts gws://gmail in the TUI or CLI.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nexus.backends.connectors.cli.result import CLIResult, CLIResultStatus
from nexus.backends.connectors.gws.connector import CalendarConnector, GmailConnector
from nexus.contracts.types import OperationContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str, command: list[str] | None = None) -> CLIResult:
    return CLIResult(
        status=CLIResultStatus.SUCCESS,
        exit_code=0,
        stdout=stdout,
        command=command or ["gws"],
    )


def _err(stderr: str = "error", command: list[str] | None = None) -> CLIResult:
    return CLIResult(
        status=CLIResultStatus.EXIT_ERROR,
        exit_code=1,
        stderr=stderr,
        command=command or ["gws"],
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _gmail_connector() -> GmailConnector:
    c = GmailConnector.__new__(GmailConnector)
    c._backend_name = "cli:gws:gmail"
    c._id_list_cache = {}
    return c


def _calendar_connector() -> CalendarConnector:
    c = CalendarConnector.__new__(CalendarConnector)
    c._backend_name = "cli:gws:calendar"
    return c


def _ctx(user: str = "alice@example.com", zone: str | None = None) -> OperationContext:
    return OperationContext(user_id=user, groups=[], zone_id=zone)


# ---------------------------------------------------------------------------
# 1. Slim package URI → connector instantiation
# ---------------------------------------------------------------------------


class TestSlimPackageWiring:
    """Verify the slim package resolves gws:// URIs to the correct connector."""

    def test_gws_gmail_uri_resolves_to_gmail_connector(self) -> None:
        """_create_connector_backend('gws://gmail') must return GmailConnector.

        ``check_runtime_deps`` is patched to return no missing deps so the
        test exercises URI routing in isolation — Issue #3830 added a
        ``BinaryDep("gws")`` check that would otherwise raise
        MissingDependencyError on CI runners without the gws CLI.
        """
        from nexus.fs._backend_factory import _create_connector_backend

        class _FakeSpec:
            scheme = "gws"
            authority = "gmail"
            path = ""
            uri = "gws://gmail"

        with (
            patch("nexus.fs._backend_factory._discover_connector_module"),
            patch("nexus.backends.base.runtime_deps.check_runtime_deps", return_value=[]),
            patch("nexus.fs._backend_factory._instantiate_connector_backend") as mock_inst,
        ):
            mock_inst.return_value = MagicMock(spec=GmailConnector)
            _create_connector_backend(_FakeSpec())

        mock_inst.assert_called_once()
        cls_arg = mock_inst.call_args.args[0]
        assert cls_arg is GmailConnector

    def test_gws_calendar_uri_resolves_to_calendar_connector(self) -> None:
        """_create_connector_backend('gws://calendar') must return CalendarConnector.

        See ``test_gws_gmail_uri_resolves_to_gmail_connector`` — the
        ``check_runtime_deps`` patch isolates URI routing from the
        environment's gws CLI availability.
        """
        from nexus.fs._backend_factory import _create_connector_backend

        class _FakeSpec:
            scheme = "gws"
            authority = "calendar"
            path = ""
            uri = "gws://calendar"

        with (
            patch("nexus.fs._backend_factory._discover_connector_module"),
            patch("nexus.backends.base.runtime_deps.check_runtime_deps", return_value=[]),
            patch("nexus.fs._backend_factory._instantiate_connector_backend") as mock_inst,
        ):
            mock_inst.return_value = MagicMock(spec=CalendarConnector)
            _create_connector_backend(_FakeSpec())

        cls_arg = mock_inst.call_args.args[0]
        assert cls_arg is CalendarConnector


# ---------------------------------------------------------------------------
# 2. Gmail list_dir pagination
# ---------------------------------------------------------------------------


class TestGmailListDirPagination:
    """list_dir must paginate through nextPageToken instead of capping at 50."""

    def test_single_page_returns_all_messages(self) -> None:
        c = _gmail_connector()
        msgs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(10)]
        payload = yaml.dump({"messages": msgs})
        c._execute_cli = MagicMock(
            return_value=_ok(payload, ["gws", "gmail", "users", "messages", "list"])
        )

        # SENT is a leaf label (no category subfolders), so list_dir returns files.
        result = c.list_dir("SENT")

        assert len(result) == 10
        assert result[0] == "t0-m0.yaml"

    def test_paginate_across_multiple_pages(self) -> None:
        c = _gmail_connector()

        page1_msgs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(3)]
        page2_msgs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(3, 5)]

        page1 = yaml.dump({"messages": page1_msgs, "nextPageToken": "tok2"})
        page2 = yaml.dump({"messages": page2_msgs})

        c._execute_cli = MagicMock(
            side_effect=[
                _ok(page1, ["gws", "gmail", "users", "messages", "list"]),
                _ok(page2, ["gws", "gmail", "users", "messages", "list"]),
            ]
        )

        result = c.list_dir("SENT")

        assert len(result) == 5
        assert c._execute_cli.call_count == 2
        # Second call must include pageToken
        second_params = json.loads(c._execute_cli.call_args_list[1].args[0][6])
        assert second_params["pageToken"] == "tok2"

    def test_stops_at_max_list_results(self) -> None:
        c = _gmail_connector()
        # Simulate a huge mailbox: always returns 500 messages with a next token.
        large_page = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(500)]
        payload = yaml.dump({"messages": large_page, "nextPageToken": "more"})
        c._execute_cli = MagicMock(
            return_value=_ok(payload, ["gws", "gmail", "users", "messages", "list"])
        )

        result = c.list_dir("SENT")

        # Must stop at MAX_LIST_RESULTS (500), not loop forever.
        assert len(result) == GmailConnector.MAX_LIST_RESULTS
        assert c._execute_cli.call_count == 1

    def test_cli_failure_returns_empty(self) -> None:
        c = _gmail_connector()
        c._execute_cli = MagicMock(return_value=_err("503 Service Unavailable"))

        result = c.list_dir("SENT")

        assert result == []

    def test_preamble_in_cli_output_is_stripped(self) -> None:
        """list_dir must tolerate gws CLI banner lines before the YAML payload."""
        c = _gmail_connector()
        msgs = [{"id": "m1", "threadId": "t1"}]
        preamble = "Using keyring backend: SecretService\n"
        payload = preamble + yaml.dump({"messages": msgs})
        c._execute_cli = MagicMock(return_value=_ok(payload))

        result = c.list_dir("SENT")

        assert result == ["t1-m1.yaml"]


# ---------------------------------------------------------------------------
# 3. Gmail read_content body extraction
# ---------------------------------------------------------------------------


class TestGmailReadContent:
    """read_content must return structured YAML with MIME body extracted."""

    def _make_msg_yaml(
        self,
        subject: str = "Hello",
        from_: str = "sender@example.com",
        body_text: str = "Plain text body",
        body_html: str | None = None,
        msg_id: str = "msgABC",
        thread_id: str = "thXYZ",
    ) -> str:
        plain_part: dict[str, Any] = {
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        }
        parts = [plain_part]
        if body_html:
            parts.append({"mimeType": "text/html", "body": {"data": _b64(body_html)}})
        msg: dict[str, Any] = {
            "id": msg_id,
            "threadId": thread_id,
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": from_},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Date", "value": "Fri, 11 Apr 2026 10:00:00 +0000"},
                ],
                "parts": parts,
            },
        }
        return "Using keyring backend: SecretService\n" + yaml.dump(msg)

    def test_returns_plain_text_body(self) -> None:
        c = _gmail_connector()
        raw = self._make_msg_yaml(body_text="Hello from plain text")
        c._execute_cli = MagicMock(return_value=_ok(raw))

        result = c.read_content("thXYZ-msgABC", context=_ctx())

        parsed = yaml.safe_load(result.decode())
        assert parsed["body"] == "Hello from plain text"
        assert parsed["subject"] == "Hello"
        assert parsed["from"] == "sender@example.com"

    def test_prefers_plain_over_html(self) -> None:
        c = _gmail_connector()
        raw = self._make_msg_yaml(body_text="Plain preferred", body_html="<b>HTML</b>")
        c._execute_cli = MagicMock(return_value=_ok(raw))

        result = c.read_content("thXYZ-msgABC", context=_ctx())

        parsed = yaml.safe_load(result.decode())
        assert parsed["body"] == "Plain preferred"

    def test_falls_back_to_html_when_no_plain(self) -> None:
        c = _gmail_connector()
        msg: dict[str, Any] = {
            "id": "msgABC",
            "threadId": "thXYZ",
            "payload": {
                "mimeType": "text/html",
                "body": {"data": _b64("<p>HTML only</p>")},
                "headers": [
                    {"name": "Subject", "value": "HTML mail"},
                    {"name": "From", "value": "html@example.com"},
                ],
            },
        }
        c._execute_cli = MagicMock(return_value=_ok(yaml.dump(msg)))

        result = c.read_content("thXYZ-msgABC", context=_ctx())

        parsed = yaml.safe_load(result.decode())
        assert "<p>HTML only</p>" in parsed["body"]

    def test_cli_failure_raises_backend_error(self) -> None:
        from nexus.contracts.exceptions import BackendError

        c = _gmail_connector()
        c._execute_cli = MagicMock(return_value=_err("404 Not Found"))

        with pytest.raises(BackendError):
            c.read_content("thXYZ-msgABC", context=_ctx())

    def test_missing_payload_returns_empty_body(self) -> None:
        """A message with no payload key returns content with empty body (no crash)."""
        c = _gmail_connector()
        # Message with no payload key — valid YAML but no body to extract.
        c._execute_cli = MagicMock(
            return_value=_ok(yaml.dump({"id": "msgABC", "threadId": "thXYZ"}))
        )

        result = c.read_content("thXYZ-msgABC", context=_ctx())

        parsed = yaml.safe_load(result.decode())
        # Body should be empty string or absent, not crash.
        assert parsed.get("body", "") == ""


# ---------------------------------------------------------------------------
# 4. Gmail cache user-scoping (no cross-user leak)
# ---------------------------------------------------------------------------


class TestGmailCacheUserScoping:
    """ID-list cache must be scoped per user — different users must not share entries."""

    def test_different_users_get_separate_cache_entries(self) -> None:
        c = _gmail_connector()

        alice_msgs = [{"id": "alice_msg", "threadId": "alice_thread"}]
        bob_msgs = [{"id": "bob_msg", "threadId": "bob_thread"}]

        c._execute_cli = MagicMock(
            side_effect=[
                _ok(yaml.dump({"messages": alice_msgs})),
                _ok(yaml.dump({"messages": bob_msgs})),
            ]
        )

        alice_result = c.list_dir("SENT", context=_ctx("alice@example.com"))
        bob_result = c.list_dir("SENT", context=_ctx("bob@example.com"))

        # Both users must see their own messages
        assert alice_result == ["alice_thread-alice_msg.yaml"]
        assert bob_result == ["bob_thread-bob_msg.yaml"]
        # Two CLI calls — no cache reuse across users
        assert c._execute_cli.call_count == 2

    def test_same_user_reuses_cached_result(self) -> None:
        c = _gmail_connector()
        msgs = [{"id": "m1", "threadId": "t1"}]
        c._execute_cli = MagicMock(return_value=_ok(yaml.dump({"messages": msgs})))

        ctx = _ctx("alice@example.com")
        first = c.list_dir("SENT", context=ctx)
        second = c.list_dir("SENT", context=ctx)

        assert first == second
        # Second call hits cache — only one CLI invocation
        assert c._execute_cli.call_count == 1

    def test_different_zones_get_separate_cache_entries(self) -> None:
        c = _gmail_connector()

        zone_a_msgs = [{"id": "za_msg", "threadId": "za_thread"}]
        zone_b_msgs = [{"id": "zb_msg", "threadId": "zb_thread"}]

        c._execute_cli = MagicMock(
            side_effect=[
                _ok(yaml.dump({"messages": zone_a_msgs})),
                _ok(yaml.dump({"messages": zone_b_msgs})),
            ]
        )

        ra = c.list_dir("SENT", context=_ctx("alice@example.com", zone="zone_a"))
        rb = c.list_dir("SENT", context=_ctx("alice@example.com", zone="zone_b"))

        assert ra == ["za_thread-za_msg.yaml"]
        assert rb == ["zb_thread-zb_msg.yaml"]
        assert c._execute_cli.call_count == 2


# ---------------------------------------------------------------------------
# 5. Calendar list_dir — root month discovery
# ---------------------------------------------------------------------------


class TestCalendarListDirRoot:
    """Root calendar listing must derive month subfolders from event start dates."""

    def _make_event(self, event_id: str, start_date: str) -> dict[str, Any]:
        return {
            "id": event_id,
            "summary": f"Event {event_id}",
            "start": {"dateTime": f"{start_date}T10:00:00Z"},
            "end": {"dateTime": f"{start_date}T11:00:00Z"},
        }

    def test_root_returns_calendars(self) -> None:
        c = _calendar_connector()
        cal_list = yaml.dump({"items": [{"id": "primary", "summary": "Primary Calendar"}]})
        c._execute_cli = MagicMock(return_value=_ok(cal_list))

        result = c.list_dir("/")

        assert "primary/" in result

    def test_calendar_root_returns_month_folders(self) -> None:
        c = _calendar_connector()

        # Root calendar listing
        cal_list = yaml.dump({"items": [{"id": "primary", "summary": "Primary Calendar"}]})
        events = yaml.dump(
            {
                "items": [
                    self._make_event("e1", "2026-04-10"),
                    self._make_event("e2", "2026-03-15"),
                    self._make_event("e3", "2026-04-22"),  # same month as e1
                ]
            }
        )
        c._execute_cli = MagicMock(side_effect=[_ok(cal_list), _ok(events)])

        result = c.list_dir("primary")

        assert "2026-04/" in result
        assert "2026-03/" in result
        assert len(result) == 2  # deduplicated

    def test_calendar_month_listing_returns_event_files(self) -> None:
        c = _calendar_connector()

        cal_list = yaml.dump({"items": [{"id": "primary", "summary": "Primary Calendar"}]})
        events = yaml.dump(
            {
                "items": [
                    self._make_event("event_alpha", "2026-04-10"),
                    self._make_event("event_beta", "2026-04-22"),
                ]
            }
        )
        c._execute_cli = MagicMock(side_effect=[_ok(cal_list), _ok(events)])

        result = c.list_dir("primary/2026-04")

        assert "event_alpha.yaml" in result
        assert "event_beta.yaml" in result

    def test_calendar_pagination_respects_budget(self) -> None:
        """Calendar list_dir must never exceed MAX_LIST_RESULTS even across pages."""
        c = _calendar_connector()

        cal_list = yaml.dump({"items": [{"id": "primary", "summary": "Primary Calendar"}]})
        # Page 1: 400 events + nextPageToken
        page1_events = [self._make_event(f"e{i}", "2026-04-01") for i in range(400)]
        page1 = yaml.dump({"items": page1_events, "nextPageToken": "tok2"})
        # Page 2: 400 more events (budget should clip to 100 remaining)
        page2_events = [self._make_event(f"e{i}", "2026-04-02") for i in range(400, 800)]
        page2 = yaml.dump({"items": page2_events})

        c._execute_cli = MagicMock(side_effect=[_ok(cal_list), _ok(page1), _ok(page2)])

        result = c.list_dir("primary/2026-04")

        assert len(result) == CalendarConnector.MAX_LIST_RESULTS


# ---------------------------------------------------------------------------
# 6. CLIResult parsing helpers
# ---------------------------------------------------------------------------


class TestCLIResultSlimParsing:
    """CLIResult.as_json / as_yaml preamble stripping and edge cases."""

    def test_as_json_strips_preamble_object(self) -> None:
        result = _ok('Using keyring\n{"key": "value"}')
        assert result.as_json() == {"key": "value"}

    def test_as_json_handles_top_level_array(self) -> None:
        result = _ok('Using keyring\n[{"id": 1}, {"id": 2}]')
        parsed = result.as_json()
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_as_yaml_strips_preamble_mapping(self) -> None:
        result = _ok("Using keyring backend: SecretService\nkey: value\n")
        assert result.as_yaml() == {"key": "value"}

    def test_as_yaml_handles_sequence(self) -> None:
        result = _ok("Using keyring\n- a\n- b\n")
        assert result.as_yaml() == ["a", "b"]

    def test_as_yaml_handles_document_marker(self) -> None:
        result = _ok("Using keyring\n---\nkey: value\n")
        assert result.as_yaml() == {"key": "value"}

    def test_as_yaml_rejects_scalar(self) -> None:
        """If CLI output parses to a plain string, raise ValueError not AttributeError.

        Input has no YAML key/sequence/doc-marker so preamble stripping leaves
        the text as-is; yaml.safe_load returns a bare string, which must be
        rejected rather than letting callers hit AttributeError on .get().
        """
        # "just a plain string" — no colon-key, no dash, no ---
        result = _ok("just a plain string")
        with pytest.raises(ValueError, match="scalar"):
            result.as_yaml()

    def test_as_json_raises_on_invalid(self) -> None:
        result = _ok("not json at all")
        with pytest.raises(ValueError, match="Failed to parse"):
            result.as_json()

    def test_as_yaml_raises_on_invalid(self) -> None:
        result = _ok("key: [unclosed")
        with pytest.raises(ValueError, match="Failed to parse"):
            result.as_yaml()

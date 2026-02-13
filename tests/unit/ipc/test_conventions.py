"""Unit tests for path conventions."""

from __future__ import annotations

from datetime import UTC, datetime

from nexus.ipc.conventions import (
    AGENTS_ROOT,
    agent_card_path,
    agent_dir,
    dead_letter_path,
    inbox_path,
    message_filename,
    message_path_in_dead_letter,
    message_path_in_inbox,
    message_path_in_outbox,
    message_path_in_processed,
    outbox_path,
    processed_path,
)


class TestPathConventions:
    """Tests for path generation helpers."""

    def test_agents_root(self) -> None:
        assert AGENTS_ROOT == "/agents"

    def test_agent_dir(self) -> None:
        assert agent_dir("reviewer") == "/agents/reviewer"

    def test_inbox_path(self) -> None:
        assert inbox_path("reviewer") == "/agents/reviewer/inbox"

    def test_outbox_path(self) -> None:
        assert outbox_path("reviewer") == "/agents/reviewer/outbox"

    def test_processed_path(self) -> None:
        assert processed_path("reviewer") == "/agents/reviewer/processed"

    def test_dead_letter_path(self) -> None:
        assert dead_letter_path("reviewer") == "/agents/reviewer/dead_letter"

    def test_agent_card_path(self) -> None:
        assert agent_card_path("reviewer") == "/agents/reviewer/AGENT.json"


class TestMessageFilename:
    """Tests for message filename generation."""

    def test_format(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        name = message_filename("msg_7f3a9b2c", ts)
        assert name == "20260212T100000_msg_7f3a9b2c.json"

    def test_sortable_by_timestamp(self) -> None:
        ts1 = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 2, 12, 10, 0, 1, tzinfo=UTC)
        name1 = message_filename("msg_aaa", ts1)
        name2 = message_filename("msg_bbb", ts2)
        assert name1 < name2  # Lexicographic sort = chronological

    def test_unique_with_different_ids(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        name1 = message_filename("msg_aaa", ts)
        name2 = message_filename("msg_bbb", ts)
        assert name1 != name2


class TestFullPaths:
    """Tests for composed message paths."""

    def test_message_path_in_inbox(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        path = message_path_in_inbox("reviewer", "msg_abc", ts)
        assert path == "/agents/reviewer/inbox/20260212T100000_msg_abc.json"

    def test_message_path_in_outbox(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        path = message_path_in_outbox("analyst", "msg_abc", ts)
        assert path == "/agents/analyst/outbox/20260212T100000_msg_abc.json"

    def test_message_path_in_processed(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        path = message_path_in_processed("reviewer", "msg_abc", ts)
        assert path == "/agents/reviewer/processed/20260212T100000_msg_abc.json"

    def test_message_path_in_dead_letter(self) -> None:
        ts = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)
        path = message_path_in_dead_letter("reviewer", "msg_abc", ts)
        assert path == "/agents/reviewer/dead_letter/20260212T100000_msg_abc.json"

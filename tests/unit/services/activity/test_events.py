"""Unit tests for ActivityEvent schema and enums."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.services.activity.events import (
    ActivityEvent,
    Actor,
    EventKind,
    Result,
    Subject,
)


def test_event_kinds_match_issue_schema() -> None:
    expected = {"search", "fetch", "mcp_tool_call", "zone_access", "policy_block", "approval"}
    assert {k.value for k in EventKind} == expected


def test_results_match_issue_schema() -> None:
    expected = {"ok", "blocked", "pending_approval"}
    assert {r.value for r in Result} == expected


def test_actor_subject_default_none() -> None:
    actor = Actor()
    assert actor.token_hash is None
    assert actor.agent is None
    assert actor.user is None
    subject = Subject()
    assert subject.zone is None
    assert subject.extra is None


def test_activity_event_minimal_construction() -> None:
    ev = ActivityEvent(
        id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        ts="2026-04-30T12:00:00Z",
        kind=EventKind.SEARCH,
        result=Result.OK,
    )
    assert ev.kind is EventKind.SEARCH
    assert ev.result is Result.OK
    assert ev.actor.token_hash is None
    assert ev.subject.zone is None
    assert ev.latency_ms is None
    assert ev.trace_id is None
    assert ev.meta is None


def test_activity_event_full_construction() -> None:
    ev = ActivityEvent(
        id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        ts="2026-04-30T12:00:00Z",
        kind=EventKind.MCP_TOOL_CALL,
        result=Result.OK,
        latency_ms=42,
        trace_id="trace-1",
        actor=Actor(token_hash="abc1234567890def", agent="claude", user="alice"),
        subject=Subject(zone="eng", extra={"tool": "search"}),
        meta={"k": "v"},
    )
    assert ev.actor.token_hash == "abc1234567890def"
    assert ev.subject.extra == {"tool": "search"}
    assert ev.meta == {"k": "v"}


def test_activity_event_is_frozen() -> None:
    ev = ActivityEvent(
        id="x",
        ts="t",
        kind=EventKind.SEARCH,
        result=Result.OK,
    )
    # object.__setattr__ bypasses frozen on CPython 3.14+; direct assignment
    # goes through the descriptor machinery and correctly raises FrozenInstanceError.
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.kind = EventKind.FETCH

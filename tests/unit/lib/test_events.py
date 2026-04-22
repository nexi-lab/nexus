"""Tests for the audit event helper (Issue #3792)."""

from nexus.lib.events import emit_audit_event, register_audit_sink


def test_emit_audit_event_delivers_to_sink() -> None:
    captured: list[tuple[str, dict]] = []

    def sink(name: str, payload: dict) -> None:
        captured.append((name, payload))

    handle = register_audit_sink(sink)
    try:
        emit_audit_event(
            "security.ssrf_blocked",
            {"url": "http://10.0.0.1/", "reason": "blocked_network"},
        )
    finally:
        handle.remove()

    assert captured == [
        ("security.ssrf_blocked", {"url": "http://10.0.0.1/", "reason": "blocked_network"}),
    ]


def test_emit_audit_event_no_sinks_does_not_raise() -> None:
    # Must be safe to call even when nothing is listening.
    emit_audit_event("security.ssrf_blocked", {"url": "x"})


def test_sink_exception_does_not_propagate() -> None:
    def bad_sink(name: str, payload: dict) -> None:
        raise RuntimeError("boom")

    handle = register_audit_sink(bad_sink)
    try:
        emit_audit_event("test.event", {})
    finally:
        handle.remove()

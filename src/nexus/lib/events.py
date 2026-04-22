"""Minimal in-process audit event bus (Issue #3792).

The existing nexus event-log service handles durable delivery; this
module is a tiny synchronous sink registry for security-signal events
that need to fan out to whatever the current deployment uses for
audit (log exporter, activity feed, etc.).

Audit emits must never raise. Failures are logged and swallowed.
"""

from __future__ import annotations

import contextlib
import logging
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_Sink = Callable[[str, dict[str, Any]], None]

# Payload keys whose values are logged verbatim by the default sink.
# Anything else is omitted (but still delivered in full to explicitly
# registered sinks). Keeps the default log line small and avoids
# leaking attacker-supplied query/token material into centralized logs.
_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "reason",
        "integration",
        "mount_name",
        "hostname",
        "cidr",
        "transport",
    }
)


def _redact_url(url: Any) -> str | None:
    """Return ``scheme://host[:port]/path`` with query+fragment+userinfo
    stripped. Returns None for non-string input so the default sink can
    omit the field entirely.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return url  # non-URL literal (e.g. test fixture) — pass through
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{port}{path}"


def _default_logger_sink(name: str, payload: dict[str, Any]) -> None:
    """Default sink so security events always reach at least one observable
    surface even when no operator-configured sink is registered.

    Emits at WARNING for names under ``security.`` (blocks/policy decisions
    are operationally important) and INFO otherwise. Payload is projected
    to a small safe-key set and URLs are redacted of query/userinfo so
    attacker-supplied tokens are not mirrored into centralized logs.
    """
    level = logging.WARNING if name.startswith("security.") else logging.INFO
    safe: dict[str, Any] = {k: v for k, v in payload.items() if k in _SAFE_PAYLOAD_KEYS}
    if "url" in payload:
        redacted = _redact_url(payload["url"])
        if redacted is not None:
            safe["url"] = redacted
    logger.log(level, "audit_event name=%s payload=%s", name, safe)


_sinks: list[_Sink] = [_default_logger_sink]


@dataclass
class SinkHandle:
    sink: _Sink

    def remove(self) -> None:
        with contextlib.suppress(ValueError):
            _sinks.remove(self.sink)


def register_audit_sink(sink: _Sink) -> SinkHandle:
    """Register a sink that receives (name, payload) for each event."""
    _sinks.append(sink)
    return SinkHandle(sink)


def emit_audit_event(name: str, payload: dict[str, Any]) -> None:
    """Emit an audit event to all registered sinks.

    Never raises — sink failures are logged at WARNING.
    """
    for sink in list(_sinks):
        try:
            sink(name, payload)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Audit sink %r raised on event %r: %s", sink, name, exc)

"""Shared test fixtures for auth brick tests.

Includes:
  - no_network: blocks all socket access (decision 12A)
  - sqlite_store: SqliteAuthProfileStore backed by :memory:
  - make_profile: helper to create AuthProfile instances
"""

from __future__ import annotations

import socket as _socket_mod
from datetime import datetime

import pytest

from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    ProfileUsageStats,
)
from nexus.bricks.auth.profile_store import SqliteAuthProfileStore

# ---------------------------------------------------------------------------
# no_network fixture (decision 12A: monkeypatch socket.socket)
# ---------------------------------------------------------------------------


class _NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all outbound network access at the socket level.

    Raises _NetworkBlockedError if any code attempts to create a socket.
    Catches HTTP, DNS, raw TCP — everything.
    """

    def _blocked_socket(*_args, **_kwargs):
        raise _NetworkBlockedError(
            "Network access is disallowed in no_network tests. "
            "If this test needs network, remove the no_network fixture."
        )

    monkeypatch.setattr(_socket_mod, "socket", _blocked_socket)


# ---------------------------------------------------------------------------
# SQLite in-memory store fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_store() -> SqliteAuthProfileStore:
    """Fresh SqliteAuthProfileStore backed by :memory: for each test."""
    store = SqliteAuthProfileStore(":memory:")
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Profile factory helper
# ---------------------------------------------------------------------------


def make_profile(
    profile_id: str,
    provider: str = "openai",
    account_identifier: str | None = None,
    *,
    backend: str = "nexus-token-manager",
    backend_key: str | None = None,
    cooldown_until: datetime | None = None,
    disabled_until: datetime | None = None,
    success_count: int = 0,
    failure_count: int = 0,
    cooldown_reason: AuthProfileFailureReason | None = None,
    raw_error: str | None = None,
) -> AuthProfile:
    stats = ProfileUsageStats(
        cooldown_until=cooldown_until,
        disabled_until=disabled_until,
        success_count=success_count,
        failure_count=failure_count,
        cooldown_reason=cooldown_reason,
        raw_error=raw_error,
    )
    return AuthProfile(
        id=profile_id,
        provider=provider,
        account_identifier=account_identifier or profile_id,
        backend=backend,
        backend_key=backend_key or f"{provider}/{profile_id}",
        usage_stats=stats,
    )

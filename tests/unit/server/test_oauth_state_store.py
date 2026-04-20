"""Unit tests for the stateless OAuth state service."""

from __future__ import annotations

import time

import pytest

from nexus.server.auth import oauth_state_store
from nexus.server.auth.oauth_state_store import (
    OAuthStateService,
    get_oauth_state_service,
    initialize_oauth_state_service,
)


@pytest.fixture(autouse=True)
def _reset_module_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_state_store, "_state_service", None, raising=False)


def test_issue_then_verify_returns_true() -> None:
    svc = OAuthStateService(signing_secret="secret")
    state = svc.issue("nonce-1")
    assert svc.verify(state, "nonce-1") is True


def test_issue_produces_unique_state_each_call() -> None:
    """Two /authorize calls with the same binding nonce must still yield
    different state tokens, otherwise an attacker who observes one flow's
    state could predict others."""
    svc = OAuthStateService(signing_secret="secret")
    assert svc.issue("nonce-1") != svc.issue("nonce-1")


def test_verify_rejects_unbound_callback() -> None:
    """Callback without the binding cookie must fail even if state is valid."""
    svc = OAuthStateService(signing_secret="secret")
    state = svc.issue("nonce-1")
    assert svc.verify(state, None) is False
    assert svc.verify(state, "") is False


def test_verify_rejects_mismatched_binding() -> None:
    """Attacker forwards (code, state) to victim; victim's cookie differs."""
    svc = OAuthStateService(signing_secret="secret")
    state = svc.issue("attacker-nonce")
    assert svc.verify(state, "victim-nonce") is False


def test_verify_rejects_unsigned_or_forged_state() -> None:
    svc = OAuthStateService(signing_secret="secret")
    assert svc.verify("not-a-signed-token", "nonce-1") is False
    assert svc.verify(None, "nonce-1") is False
    assert svc.verify("", "nonce-1") is False


def test_verify_rejects_state_signed_with_different_secret() -> None:
    """Tokens from another server / tenant must not verify here."""
    svc_a = OAuthStateService(signing_secret="secret-a")
    svc_b = OAuthStateService(signing_secret="secret-b")
    state_from_a = svc_a.issue("nonce-1")
    assert svc_b.verify(state_from_a, "nonce-1") is False


def test_verify_rejects_expired_state() -> None:
    # itsdangerous stamps integer-second timestamps and enforces ``age > max_age``,
    # so we need the wall-clock sleep to exceed TTL by at least one whole second.
    svc = OAuthStateService(signing_secret="secret", ttl_seconds=1)
    state = svc.issue("nonce-1")
    time.sleep(2.5)
    assert svc.verify(state, "nonce-1") is False


def test_issue_rejects_empty_binding() -> None:
    svc = OAuthStateService(signing_secret="secret")
    with pytest.raises(ValueError):
        svc.issue("")


def test_constructor_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        OAuthStateService(signing_secret="")


def test_multi_worker_safe_verification() -> None:
    """State issued by 'worker A' must verify against a freshly-built
    'worker B' service sharing the same signing secret — no shared memory
    needed.
    """
    worker_a = OAuthStateService(signing_secret="shared-jwt-secret")
    state = worker_a.issue("nonce-multi")

    worker_b = OAuthStateService(signing_secret="shared-jwt-secret")
    assert worker_b.verify(state, "nonce-multi") is True


def test_get_service_raises_when_not_initialized() -> None:
    with pytest.raises(RuntimeError):
        get_oauth_state_service()


def test_initialize_and_get_returns_service() -> None:
    svc = initialize_oauth_state_service("secret")
    assert get_oauth_state_service() is svc

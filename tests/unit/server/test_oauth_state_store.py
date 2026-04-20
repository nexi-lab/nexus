"""Unit tests for the OAuth CSRF state store."""

from __future__ import annotations

import time

import pytest

from nexus.server.auth.oauth_state_store import OAuthStateStore, get_oauth_state_store


def test_register_then_consume_returns_true() -> None:
    store = OAuthStateStore()
    store.register("abc123", "nonce-1")
    assert store.consume("abc123", "nonce-1") is True


def test_consume_is_single_use() -> None:
    store = OAuthStateStore()
    store.register("once", "nonce-1")
    assert store.consume("once", "nonce-1") is True
    assert store.consume("once", "nonce-1") is False


def test_consume_unknown_state_returns_false() -> None:
    store = OAuthStateStore()
    assert store.consume("never-registered", "nonce-1") is False


def test_consume_none_or_empty_returns_false() -> None:
    store = OAuthStateStore()
    store.register("s", "nonce-1")
    assert store.consume(None, "nonce-1") is False
    assert store.consume("", "nonce-1") is False
    assert store.consume("s", None) is False
    assert store.consume("s", "") is False


def test_consume_wrong_binding_nonce_returns_false_and_invalidates() -> None:
    """A replay with a mismatched cookie nonce must fail AND consume the state.

    Popping-on-read prevents an attacker from guessing repeatedly — one shot
    at the nonce and the state is gone.
    """
    store = OAuthStateStore()
    store.register("state-1", "cookie-nonce")
    # Attacker presents the right state but wrong cookie binding
    assert store.consume("state-1", "wrong-nonce") is False
    # Legitimate browser follow-up with the correct nonce now also fails
    assert store.consume("state-1", "cookie-nonce") is False


def test_register_rejects_empty_state() -> None:
    store = OAuthStateStore()
    with pytest.raises(ValueError):
        store.register("", "nonce-1")


def test_register_rejects_empty_binding_nonce() -> None:
    store = OAuthStateStore()
    with pytest.raises(ValueError):
        store.register("state-1", "")


def test_ttl_expiry_rejects_stale_state() -> None:
    store = OAuthStateStore(ttl_seconds=1)
    store.register("short-lived", "nonce-1")
    time.sleep(1.1)
    assert store.consume("short-lived", "nonce-1") is False


def test_maxsize_evicts_oldest_entries() -> None:
    store = OAuthStateStore(ttl_seconds=60, maxsize=2)
    store.register("a", "n-a")
    store.register("b", "n-b")
    store.register("c", "n-c")  # evicts "a"
    assert store.consume("a", "n-a") is False
    assert store.consume("b", "n-b") is True
    assert store.consume("c", "n-c") is True


def test_get_oauth_state_store_returns_singleton() -> None:
    first = get_oauth_state_store()
    second = get_oauth_state_store()
    assert first is second

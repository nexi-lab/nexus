"""Unit tests for the OAuth CSRF state store."""

from __future__ import annotations

import time

import pytest

from nexus.server.auth.oauth_state_store import OAuthStateStore, get_oauth_state_store


def test_register_then_consume_returns_true() -> None:
    store = OAuthStateStore()
    store.register("abc123")
    assert store.consume("abc123") is True


def test_consume_is_single_use() -> None:
    store = OAuthStateStore()
    store.register("once")
    assert store.consume("once") is True
    assert store.consume("once") is False


def test_consume_unknown_state_returns_false() -> None:
    store = OAuthStateStore()
    assert store.consume("never-registered") is False


def test_consume_none_or_empty_returns_false() -> None:
    store = OAuthStateStore()
    assert store.consume(None) is False
    assert store.consume("") is False


def test_register_rejects_empty_state() -> None:
    store = OAuthStateStore()
    with pytest.raises(ValueError):
        store.register("")


def test_ttl_expiry_rejects_stale_state() -> None:
    store = OAuthStateStore(ttl_seconds=1)
    store.register("short-lived")
    time.sleep(1.1)
    assert store.consume("short-lived") is False


def test_maxsize_evicts_oldest_entries() -> None:
    store = OAuthStateStore(ttl_seconds=60, maxsize=2)
    store.register("a")
    store.register("b")
    store.register("c")  # evicts "a"
    assert store.consume("a") is False
    assert store.consume("b") is True
    assert store.consume("c") is True


def test_get_oauth_state_store_returns_singleton() -> None:
    first = get_oauth_state_store()
    second = get_oauth_state_store()
    assert first is second

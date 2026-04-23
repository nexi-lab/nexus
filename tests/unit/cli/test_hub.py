"""Unit tests for `nexus hub` CLI."""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

from nexus.cli.commands.hub import hub


def _mock_session_ctx(session):
    """Session factory that yields the given session from a context manager."""
    from contextlib import contextmanager

    @contextmanager
    def factory():
        yield session

    return factory


def test_token_create_prints_raw_key_and_row(monkeypatch):
    created = {}

    def fake_create_api_key(session, **kwargs):
        created.update(kwargs)
        return ("kid_abc", "sk-root_alice_abcd_1234")

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(MagicMock()),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "alice", "--zone", "root"],
    )
    assert result.exit_code == 0, result.output
    assert "sk-root_alice_abcd_1234" in result.output
    assert "kid_abc" in result.output
    assert created["name"] == "alice"
    assert created["zone_id"] == "root"
    assert created["is_admin"] is False


def test_token_create_admin_flag_sets_is_admin(monkeypatch):
    monkeypatch.setattr(
        "nexus.cli.commands.hub.create_api_key",
        lambda session, **kw: ("kid", "sk-x")
        if kw["is_admin"]
        else (_ for _ in ()).throw(AssertionError("is_admin=False")),
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(MagicMock()),
    )
    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "root", "--zone", "root", "--admin"],
    )
    assert result.exit_code == 0, result.output


def test_token_create_rejects_duplicate_name(monkeypatch):
    def fake_create(session, **kw):
        raise AssertionError("should not be called when duplicate detected")

    existing = MagicMock()
    existing.name = "alice"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = existing

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "alice", "--zone", "root"],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_token_create_expires_sets_expires_at(monkeypatch):
    captured = {}

    def fake_create(session, **kw):
        captured.update(kw)
        return ("kid", "sk-x")

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create)
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = None
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "t", "--zone", "root", "--expires", "7d"],
    )
    assert result.exit_code == 0, result.output
    assert captured["expires_at"] is not None

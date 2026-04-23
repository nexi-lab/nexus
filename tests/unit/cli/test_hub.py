"""Unit tests for `nexus hub` CLI."""

from __future__ import annotations

import json
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
    def _assert_admin(session, **kw):
        assert kw["is_admin"] is True, "is_admin flag should be set"
        return ("kid", "sk-x")

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", _assert_admin)
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


def test_token_create_duplicate_filter_ignores_revoked(monkeypatch):
    """Integration: a revoked token with the same name should NOT block creation."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import APIKeyModel, Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    # Seed: one REVOKED token named "alice"
    with Session() as s, s.begin():
        s.add(
            APIKeyModel(
                key_hash="deadbeef",
                user_id="alice",
                name="alice",
                zone_id="root",
                is_admin=0,
                revoked=1,
            )
        )

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "alice", "--zone", "root"],
    )
    assert result.exit_code == 0, result.output
    assert "sk-" in result.output  # real create_api_key ran


def _fake_row(**overrides):
    row = MagicMock()
    row.key_id = overrides.get("key_id", "kid_xxxx")
    row.name = overrides.get("name", "alice")
    row.zone_id = overrides.get("zone_id", "root")
    row.is_admin = overrides.get("is_admin", 0)
    row.created_at = overrides.get("created_at")
    row.last_used_at = overrides.get("last_used_at")
    row.revoked = overrides.get("revoked", 0)
    row.revoked_at = overrides.get("revoked_at")
    return row


def test_token_list_hides_revoked_by_default(monkeypatch):
    active = _fake_row(name="alice", key_id="kid_a")

    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [active]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "bob" not in result.output


def test_token_list_show_revoked_includes_revoked(monkeypatch):
    revoked = _fake_row(name="bob", key_id="kid_b", revoked=1)
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [revoked]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--show-revoked"])
    assert result.exit_code == 0, result.output
    assert "bob" in result.output


def test_token_list_json(monkeypatch):
    row = _fake_row(name="alice", key_id="kid_a")
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [row]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tokens"][0]["name"] == "alice"


def test_token_list_filters_revoked_with_real_sql(monkeypatch):
    """Integration: default query actually filters revoked rows via WHERE revoked == 0."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import APIKeyModel, Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    with Session() as s, s.begin():
        s.add(
            APIKeyModel(
                key_hash="hash_active",
                user_id="alice",
                name="alice",
                zone_id="root",
                is_admin=0,
                revoked=0,
            )
        )
        s.add(
            APIKeyModel(
                key_hash="hash_revoked",
                user_id="bob",
                name="bob",
                zone_id="root",
                is_admin=0,
                revoked=1,
            )
        )

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    # Default: only "alice" should appear.
    default_result = runner.invoke(hub, ["token", "list"])
    assert default_result.exit_code == 0, default_result.output
    assert "alice" in default_result.output
    assert "bob" not in default_result.output

    # --show-revoked: both appear.
    all_result = runner.invoke(hub, ["token", "list", "--show-revoked"])
    assert all_result.exit_code == 0, all_result.output
    assert "alice" in all_result.output
    assert "bob" in all_result.output

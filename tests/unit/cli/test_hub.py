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
    assert created["zones"] == ["root"]
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


def test_token_create_rejects_unknown_zone_when_zones_exist(monkeypatch):
    """If the zones table has rows, --zone must match an existing zone_id."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import Base, ZoneModel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    # Seed: a real "prod" zone — a "proud" typo must be rejected.
    with Session() as s, s.begin():
        s.add(ZoneModel(zone_id="prod", name="prod"))

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "t", "--zone", "proud"],
    )
    assert result.exit_code == 1
    assert "not active" in result.output
    assert "prod" in result.output  # known zones listed for the operator


def test_token_create_bootstrap_allows_any_zone_when_empty(monkeypatch):
    """Bootstrap escape: zones table empty → first admin token may use any --zone."""
    from sqlalchemy import create_engine
    from sqlalchemy import select as _select
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import APIKeyModel, Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "root", "--zone", "root", "--admin"],
    )
    assert result.exit_code == 0, result.output
    # Token actually got created in the DB.
    with Session() as s:
        rows = s.execute(_select(APIKeyModel)).scalars().all()
        assert len(rows) == 1
        assert rows[0].zone_id == "root"


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


def test_token_revoke_by_name_sets_revoked(monkeypatch):
    row = _fake_row(name="alice", key_id="kid_alice", revoked=0)
    row.revoked = 0
    row.revoked_at = None
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [row]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "revoke", "alice"])
    assert result.exit_code == 0, result.output
    assert row.revoked == 1
    assert row.revoked_at is not None


def test_token_revoke_not_found_exits_1(monkeypatch):
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "revoke", "nosuch"])
    assert result.exit_code == 1


def test_token_revoke_ambiguous_exits_2(monkeypatch):
    a = _fake_row(name="alice", key_id="kid_a")
    b = _fake_row(name="alice", key_id="kid_b")
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [a, b]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "revoke", "alice"])
    assert result.exit_code == 2
    assert "ambiguous" in result.output.lower()


def test_token_revoke_with_real_sql(monkeypatch):
    """Integration: revoke sets revoked=1 and revoked_at, and only affects matching row."""
    from sqlalchemy import create_engine
    from sqlalchemy import select as _select
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import APIKeyModel, Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    with Session() as s, s.begin():
        s.add(
            APIKeyModel(
                key_hash="h_alice",
                user_id="alice",
                name="alice",
                zone_id="root",
                is_admin=0,
                revoked=0,
            )
        )
        s.add(
            APIKeyModel(
                key_hash="h_bob",
                user_id="bob",
                name="bob",
                zone_id="root",
                is_admin=0,
                revoked=0,
            )
        )

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "revoke", "alice"])
    assert result.exit_code == 0, result.output

    with Session() as s:
        rows = {r.name: r for r in s.execute(_select(APIKeyModel)).scalars().all()}
        assert rows["alice"].revoked == 1
        assert rows["alice"].revoked_at is not None
        assert rows["bob"].revoked == 0
        assert rows["bob"].revoked_at is None


def test_hub_zone_list_delegates_to_zone_list(monkeypatch):
    called = {}

    def fake_zone_list_callback(**kwargs):
        called["ok"] = True

    from nexus.cli.commands import zone as zone_module

    list_cmd = zone_module.zone.commands.get("list")
    assert list_cmd is not None, "precondition: `nexus zone list` must exist"
    monkeypatch.setattr(list_cmd, "callback", fake_zone_list_callback)

    runner = CliRunner()
    result = runner.invoke(hub, ["zone", "list"])
    assert result.exit_code == 0, result.output
    assert called.get("ok") is True


def test_hub_status_json_includes_expected_fields(monkeypatch):
    monkeypatch.setenv("NEXUS_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("NEXUS_MCP_PORT", "8081")
    monkeypatch.setenv("NEXUS_PROFILE", "full")

    session = MagicMock()
    # Token counts: 5 active + 2 revoked
    session.execute.return_value.scalar.side_effect = [5, 2]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": 3.5, "connections": 4, "redis": "ok"},
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["profile"] == "full"
    assert payload["endpoint"] == "http://0.0.0.0:8081/mcp"
    assert payload["tokens"] == {"active": 5, "revoked": 2}
    assert payload["qps_5m"] == 3.5
    assert payload["connections"] == 4
    assert payload["postgres"] == "ok"


def test_hub_status_postgres_unreachable_marks_err(monkeypatch):
    """Broken auth DB must exit non-zero so shell-style health guards fail
    closed. JSON payload still emits `postgres: err` for parseable consumers."""

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("nexus.cli.commands.hub.get_session_factory", boom)
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": None, "connections": None, "redis": "n/a"},
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["status", "--json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["postgres"] == "err"


def test_hub_status_exits_zero_when_postgres_ok(monkeypatch):
    """Sanity check: the new non-zero-on-err behavior doesn't regress the
    success path — a healthy Postgres still exits 0."""
    session = MagicMock()
    session.execute.return_value.scalar.side_effect = [3, 1]
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub._read_redis_stats",
        lambda: {"qps_5m": 0.0, "connections": 0, "redis": "ok"},
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["status", "--json"])
    assert result.exit_code == 0, result.output


def test_token_create_rejects_terminating_zone(monkeypatch):
    """A zone in phase != 'Active' (e.g. 'Terminating') must not accept new tokens."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import Base, ZoneModel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    with Session() as s, s.begin():
        s.add(ZoneModel(zone_id="prod", name="prod", phase="Terminating"))
        s.add(ZoneModel(zone_id="staging", name="staging", phase="Active"))

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "t", "--zone", "prod"],
    )
    assert result.exit_code == 1
    assert "not active" in result.output
    # Only the active zone is listed as a replacement suggestion.
    assert "staging" in result.output
    assert "prod" not in result.output.split("Active zones:")[1]


def test_token_create_rejects_soft_deleted_zone(monkeypatch):
    """A zone with deleted_at set (soft-delete) must not accept new tokens."""
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import Base, ZoneModel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    with Session() as s, s.begin():
        s.add(
            ZoneModel(
                zone_id="prod",
                name="prod",
                phase="Active",
                deleted_at=datetime.now(UTC),
            )
        )

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: Session,
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "t", "--zone", "prod"],
    )
    assert result.exit_code == 1
    assert "not active" in result.output


def test_token_create_zones_csv(monkeypatch):
    """--zones eng,ops creates a token bound to both zones (#3785)."""
    captured = {}

    def fake_create_api_key(session, **kwargs):
        captured.update(kwargs)
        return ("kid_xyz", "sk-eng_alice_xx_yy")

    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,  # no existing token by name
        active_zone,  # any_zone exists (skip bootstrap escape)
        active_zone,  # zone "eng" Active
        active_zone,  # zone "ops" Active
    ]
    session.execute.return_value.scalars.return_value.all.return_value = []

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "alice", "--zones", "eng,ops"],
    )
    assert result.exit_code == 0, result.output
    assert captured["zones"] == ["eng", "ops"]


def test_token_create_zone_alias_still_works(monkeypatch):
    """Backward-compat: --zone single still mints a token (#3785)."""
    captured = {}

    def fake_create_api_key(session, **kwargs):
        captured.update(kwargs)
        return ("kid_x", "sk-x")

    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,
        active_zone,
        active_zone,
    ]

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "create", "--name", "svc", "--zone", "eng"])
    assert result.exit_code == 0, result.output
    assert captured["zones"] == ["eng"]


def test_token_create_rejects_empty_zones(monkeypatch):
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(MagicMock()),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "create", "--name", "alice", "--zones", ""])
    assert result.exit_code != 0
    assert "zone" in result.output.lower()


def test_token_create_rejects_inactive_zone_in_list(monkeypatch):
    """If any zone in --zones is not Active, the whole mint fails."""
    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    # Sequence: no existing, any_zone exists, eng Active, ops not found
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,
        active_zone,
        active_zone,
        None,
    ]
    session.execute.return_value.scalars.return_value.all.return_value = [active_zone]

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "create", "--name", "alice", "--zones", "eng,ops"])
    assert result.exit_code != 0
    assert "ops" in result.output


def test_token_list_json_includes_zones(monkeypatch):
    """`token list --json` emits 'zones': ['eng','ops'] per row (#3785)."""
    from datetime import UTC, datetime

    from nexus.storage.models import APIKeyModel, APIKeyZoneModel

    row = APIKeyModel(
        key_id="kid_a",
        key_hash="h",
        user_id="alice",
        name="alice",
        zone_id="eng",
        is_admin=0,
        revoked=0,
        created_at=datetime.now(UTC),
    )

    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.side_effect = [
        [row],  # APIKeyModel rows
        [
            APIKeyZoneModel(key_id="kid_a", zone_id="eng"),
            APIKeyZoneModel(key_id="kid_a", zone_id="ops"),
        ],  # junction rows
    ]

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tokens"][0]["zones"] == ["eng", "ops"]


def test_token_zones_add_invokes_helper(monkeypatch):
    row = MagicMock()
    row.key_id = "kid_a"
    session = MagicMock()
    # Sequence: zone Active check (zone exists), then resolve token by name
    active_zone = MagicMock()
    active_zone.zone_id = "ops"
    session.execute.return_value.scalars.return_value.first.side_effect = [
        active_zone,  # zone Active
        row,  # token by name
    ]

    captured = {}

    def fake_add(s, key_id, zone_id):
        captured.update(key_id=key_id, zone_id=zone_id)
        return True

    monkeypatch.setattr("nexus.cli.commands.hub.add_zone_to_key", fake_add)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "zones", "add", "--name", "alice", "--zone", "ops"])
    assert result.exit_code == 0, result.output
    assert captured == {"key_id": "kid_a", "zone_id": "ops"}


def test_token_zones_remove_refuses_last_zone(monkeypatch):
    row = MagicMock()
    row.key_id = "kid_a"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = row

    def fake_remove(s, key_id, zone_id):
        raise ValueError(f"refusing to remove last zone {zone_id!r} from key {key_id!r}")

    monkeypatch.setattr("nexus.cli.commands.hub.remove_zone_from_key", fake_remove)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "zones", "remove", "--name", "alice", "--zone", "eng"])
    assert result.exit_code != 0
    assert "last zone" in result.output


def test_token_zones_show_lists_zones(monkeypatch):
    row = MagicMock()
    row.key_id = "kid_a"
    row.zone_id = "eng"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = row

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_zones_for_key",
        lambda s, kid: ["eng", "ops"],
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "zones", "show", "--name", "alice"])
    assert result.exit_code == 0, result.output
    assert "eng" in result.output
    assert "ops" in result.output

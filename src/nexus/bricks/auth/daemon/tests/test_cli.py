"""Lightweight tests for the `nexus daemon` CLI wiring (#3804).

Only covers things not already exercised by integration tests:
the ``_build_encryption_provider`` fallback path and profile helpers.
The individual subcommands are smoke-tested via ``nexus daemon --help``
and covered end-to-end by tests/integration/auth.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from nexus.bricks.auth.daemon import cli as daemon_cli


def test_build_encryption_provider_defaults_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env → memory provider (the MVP default), wrapped in _DaemonEnvelope."""
    import uuid

    monkeypatch.delenv("NEXUS_KMS_PROVIDER", raising=False)
    ep = daemon_cli._build_encryption_provider()
    # _DaemonEnvelope exposes .encrypt(plaintext, tenant_id, aad)
    env = ep.encrypt(b"hello", tenant_id=uuid.uuid4(), aad=b"tenant|principal|id")
    assert env.ciphertext != b"hello"
    assert len(env.nonce) == 12
    assert env.kek_version >= 1
    assert len(env.wrapped_dek) > 0


def test_build_encryption_provider_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any non-'memory' value raises ClickException with the offending name."""
    monkeypatch.setenv("NEXUS_KMS_PROVIDER", "aws-kms")
    with pytest.raises(click.ClickException) as excinfo:
        daemon_cli._build_encryption_provider()
    assert "aws-kms" in str(excinfo.value.message)


def test_daemon_group_exposes_expected_subcommands() -> None:
    """All 7 MVP subcommands are registered on the group (incl. `list`, `bootstrap`)."""
    expected = {"join", "run", "status", "install", "uninstall", "list", "bootstrap"}
    registered = set(daemon_cli.daemon.commands.keys())
    assert expected <= registered, f"missing: {expected - registered}"


def test_keyring_service_scoped_per_profile() -> None:
    assert daemon_cli._keyring_service_for("work") == "com.nexus.daemon.work"
    assert daemon_cli._keyring_service_for("home") == "com.nexus.daemon.home"


def test_profile_paths_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All per-profile paths live under NEXUS_HOME/daemons/<profile>/."""
    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    paths = daemon_cli._profile_paths("work")
    assert paths["dir"] == tmp_path / "daemons" / "work"
    assert paths["config"] == tmp_path / "daemons" / "work" / "daemon.toml"
    assert paths["key"] == tmp_path / "daemons" / "work" / "machine.key"
    assert paths["jwt_cache"] == tmp_path / "daemons" / "work" / "jwt.cache"
    assert paths["server_pubkey"] == tmp_path / "daemons" / "work" / "server.pub.pem"


def test_resolve_profile_auto_picks_single(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    d = tmp_path / "daemons" / "work"
    d.mkdir(parents=True)
    (d / "daemon.toml").write_text("x")
    assert daemon_cli._resolve_profile(None, required_action="run") == "work"


def test_resolve_profile_errors_when_none_enrolled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    with pytest.raises(click.ClickException, match="no daemon profiles enrolled"):
        daemon_cli._resolve_profile(None, required_action="run")


def test_resolve_profile_errors_when_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    for name in ("work", "home"):
        d = tmp_path / "daemons" / name
        d.mkdir(parents=True)
        (d / "daemon.toml").write_text("x")
    with pytest.raises(click.ClickException, match="multiple profiles"):
        daemon_cli._resolve_profile(None, required_action="run")
    # Explicit pick still works.
    assert daemon_cli._resolve_profile("home", required_action="run") == "home"

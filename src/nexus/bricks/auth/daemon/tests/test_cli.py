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


def test_build_encryption_provider_memory_with_unsafe_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory provider requires NEXUS_UNSAFE_DEV_MEMORY_KMS=true acknowledgement."""
    import uuid

    monkeypatch.delenv("NEXUS_KMS_PROVIDER", raising=False)
    monkeypatch.setenv("NEXUS_UNSAFE_DEV_MEMORY_KMS", "true")
    ep = daemon_cli._build_encryption_provider()
    # _DaemonEnvelope exposes .encrypt(plaintext, tenant_id, aad)
    env = ep.encrypt(b"hello", tenant_id=uuid.uuid4(), aad=b"tenant|principal|id")
    assert env.ciphertext != b"hello"
    assert len(env.nonce) == 12
    assert env.kek_version >= 1
    assert len(env.wrapped_dek) > 0


def test_build_encryption_provider_memory_rejects_without_unsafe_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the unsafe flag the memory provider is refused — fail closed."""
    monkeypatch.delenv("NEXUS_KMS_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_UNSAFE_DEV_MEMORY_KMS", raising=False)
    with pytest.raises(click.ClickException) as excinfo:
        daemon_cli._build_encryption_provider()
    assert "NEXUS_UNSAFE_DEV_MEMORY_KMS" in str(excinfo.value.message)
    assert "not durable" in str(excinfo.value.message)


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


def test_install_cmd_errors_when_nexus_missing_from_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_cmd must refuse to wire units pointing at a Python interpreter.

    Regression: previously passed ``sys.executable`` which produced a broken
    ``python daemon run ...`` ExecStart. With no nexus binary on PATH and no
    --executable override, the command must error loudly rather than install
    a bogus unit.
    """
    from click.testing import CliRunner

    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    d = tmp_path / "daemons" / "work"
    d.mkdir(parents=True)
    (d / "daemon.toml").write_text("x")

    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _name: None)

    result = CliRunner().invoke(
        daemon_cli.daemon, ["install", "--profile", "work"], catch_exceptions=False
    )
    assert result.exit_code != 0
    assert "nexus" in result.output.lower()


def test_install_cmd_resolves_nexus_from_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When PATH contains nexus, installer receives that binary, not sys.executable."""
    from click.testing import CliRunner

    from nexus.bricks.auth.daemon import installer as installer_mod

    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    d = tmp_path / "daemons" / "work"
    d.mkdir(parents=True)
    (d / "daemon.toml").write_text("x")

    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which", lambda name: "/usr/local/bin/nexus" if name == "nexus" else None
    )

    captured: dict[str, object] = {}

    def fake_install(
        *,
        executable: str,
        config_path: Path,  # noqa: ARG001 - signature parity w/ real install()
        profile: str,
    ) -> Path:
        captured["executable"] = executable
        captured["profile"] = profile
        return tmp_path / "fake.plist"

    monkeypatch.setattr(installer_mod, "install", fake_install)

    result = CliRunner().invoke(
        daemon_cli.daemon, ["install", "--profile", "work"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert captured["executable"] == "/usr/local/bin/nexus"
    assert captured["profile"] == "work"


def test_validate_server_url_accepts_https() -> None:
    """https is always permitted regardless of host."""
    daemon_cli._validate_server_url("https://nexus.example.com", allow_insecure_localhost=False)
    daemon_cli._validate_server_url("https://localhost:2026", allow_insecure_localhost=False)


def test_validate_server_url_rejects_plain_http_by_default() -> None:
    """Refuse http:// even on localhost when the opt-in flag is not set.

    Regression: auth material over cleartext http permits MITM impersonation.
    The default MUST be https; localhost http requires an explicit opt-in.
    """
    with pytest.raises(click.ClickException, match="cleartext http"):
        daemon_cli._validate_server_url("http://nexus.example.com", allow_insecure_localhost=False)
    with pytest.raises(click.ClickException, match="cleartext http"):
        daemon_cli._validate_server_url("http://localhost:2026", allow_insecure_localhost=False)


def test_validate_server_url_allows_http_on_localhost_with_opt_in() -> None:
    """Opt-in flag narrowly permits http:// only for localhost/127.0.0.1/::1."""
    daemon_cli._validate_server_url("http://localhost:2026", allow_insecure_localhost=True)
    daemon_cli._validate_server_url("http://127.0.0.1:2026", allow_insecure_localhost=True)
    daemon_cli._validate_server_url("http://[::1]:2026", allow_insecure_localhost=True)
    # Opt-in does NOT relax policy for non-local hosts.
    with pytest.raises(click.ClickException, match="cleartext http"):
        daemon_cli._validate_server_url("http://nexus.example.com", allow_insecure_localhost=True)


def test_validate_server_url_rejects_unknown_scheme() -> None:
    """Schemes other than http/https (e.g. ftp, ws) fail hard."""
    with pytest.raises(click.ClickException, match="must be https"):
        daemon_cli._validate_server_url("ftp://nexus.example.com", allow_insecure_localhost=False)


def test_install_cmd_executable_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--executable overrides PATH lookup and is passed through verbatim."""
    from click.testing import CliRunner

    from nexus.bricks.auth.daemon import installer as installer_mod

    monkeypatch.setattr(daemon_cli, "_NEXUS_HOME", tmp_path)
    d = tmp_path / "daemons" / "work"
    d.mkdir(parents=True)
    (d / "daemon.toml").write_text("x")

    captured: dict[str, object] = {}

    def fake_install(
        *,
        executable: str,
        config_path: Path,  # noqa: ARG001 - signature parity w/ real install()
        profile: str,  # noqa: ARG001 - signature parity w/ real install()
    ) -> Path:
        captured["executable"] = executable
        return tmp_path / "fake.plist"

    monkeypatch.setattr(installer_mod, "install", fake_install)

    result = CliRunner().invoke(
        daemon_cli.daemon,
        ["install", "--profile", "work", "--executable", "/opt/nexus/bin/nexus"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert captured["executable"] == "/opt/nexus/bin/nexus"

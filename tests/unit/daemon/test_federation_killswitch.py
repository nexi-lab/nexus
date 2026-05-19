"""Regression-safety tests for the sandbox federation kill-switch (Issue #4126).

The federation defect fix lives in ``src/nexus/daemon/main.py``: the daemon
sets ``os.environ.setdefault("NEXUS_FEDERATION_DISABLED", "1")`` *only* when
``deployment_profile == "sandbox"`` and none of
``NEXUS_PEERS`` / ``NEXUS_HOSTNAME`` / ``NEXUS_BOOTSTRAP_NEW`` are set.

The Rust ``distributed_coordinator::install()`` early-returns (keeps the
``NoopDistributedCoordinator``) iff ``NEXUS_FEDERATION_DISABLED`` is truthy.

The "zero-cluster/full regression" argument rests entirely on the env var
being set in *exactly this one Python place*, gated to ``profile=="sandbox"``.
These tests turn that structural argument into a TESTED invariant:

1. sandbox (clean env) → kill-switch set to "1".
2. every other profile that reaches the daemon boot path → kill-switch is
   NEVER set (cluster/full/lite/embedded/cloud/auto are byte-identical to
   before). THIS is the regression-safety assertion.
3. sandbox + explicit NEXUS_PEERS / NEXUS_HOSTNAME → operator opt-in
   preserved (kill-switch not forced).
4. sandbox + operator-set NEXUS_FEDERATION_DISABLED="0" → ``setdefault``
   honors the explicit override and leaves it "0".

These are hermetic unit tests: they exercise the real ``main`` boot code
path via Click's ``CliRunner`` with ``nexus.connect`` and the FastAPI server
module stubbed (mirroring ``tests/unit/daemon/test_sandbox_flags.py``), so no
daemon is started and no Rust is involved. All env mutation goes through
``monkeypatch`` so it is auto-restored and order-independent.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.daemon.main import main

# Env vars this invariant touches. Every test starts from a clean slate for
# all of them so the tests are order-independent and never leak.
_FEDERATION_ENV_VARS = (
    "NEXUS_FEDERATION_DISABLED",
    "NEXUS_PEERS",
    "NEXUS_HOSTNAME",
    "NEXUS_BOOTSTRAP_NEW",
)

# Non-sandbox profiles that nexusd accepts AND that reach the federation
# gate. ``remote`` is intentionally excluded: it exits early (CONFIG_ERROR)
# before the gate, so it can never set the kill-switch either. These are the
# real ``DeploymentProfile`` values (see nexus.contracts.deployment_profile)
# plus ``auto`` (the daemon's documented default sentinel).
_NON_SANDBOX_PROFILES = ("full", "cloud", "lite", "embedded", "cluster", "auto")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server_mocks(monkeypatch):
    """Inject fake nexus + fastapi_server modules so CLI can reach the gate.

    Mirrors ``tests/unit/daemon/test_sandbox_flags.py::_make_server_mocks``.
    """
    mock_nx = MagicMock()
    mock_connect = MagicMock(return_value=mock_nx)

    mock_app = MagicMock()
    mock_create_app = MagicMock(return_value=mock_app)
    mock_run_server = MagicMock()

    fake_mod = types.ModuleType("nexus.server.fastapi_server")
    fake_mod.create_app = mock_create_app
    fake_mod.run_server = mock_run_server
    monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_mod)

    return mock_connect, mock_nx, mock_create_app, mock_run_server


def _clean_federation_env(monkeypatch) -> None:
    """Ensure none of the federation-relevant vars leak in from the runner.

    Uses ``monkeypatch.delenv`` so the real environment is auto-restored
    after the test, keeping the suite order-independent.
    """
    for var in _FEDERATION_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _run_daemon(
    profile: str | None,
    tmp_path: Path,
    monkeypatch,
    workspace: Path | None = None,
    config_path: Path | None = None,
):
    """Drive the real ``main`` boot path far enough to execute the gate.

    Stubs the heavy bits exactly like ``test_sandbox_flags.py``:
    ``nexus.connect`` and ``nexus.daemon.main.SandboxBootstrapper`` are
    patched and the FastAPI server module is faked, so no daemon boots.

    ``profile=None`` omits ``--profile`` entirely (config-sourced boots).
    ``config_path`` passes ``--config <file>`` so the daemon resolves the
    profile from the YAML, exercising the effective-profile path.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    mock_connect, _mock_nx, _mock_create_app, _mock_run_server = _make_server_mocks(monkeypatch)
    bootstrapper_mock = MagicMock()
    mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

    # ``load_config`` is real here so the daemon's config-file branch
    # genuinely parses the YAML (the effective-profile resolver reads the
    # same file). ``nexus.connect`` is still stubbed so no kernel boots.
    args: list[str] = []
    if profile is not None:
        args += ["--profile", profile]
    if config_path is not None:
        args += ["--config", str(config_path)]
    if workspace is not None:
        args += ["--workspace", str(workspace)]

    with (
        patch("nexus.connect", mock_connect),
        patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
    ):
        runner = CliRunner()
        result = runner.invoke(main, args)

    return result


# ---------------------------------------------------------------------------
# Test 1: sandbox profile (clean env) sets the kill-switch
# ---------------------------------------------------------------------------


def test_sandbox_profile_sets_federation_killswitch(tmp_path: Path, monkeypatch) -> None:
    """profile=sandbox + clean env → NEXUS_FEDERATION_DISABLED == "1"."""
    _clean_federation_env(monkeypatch)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1", (
        "Sandbox boot must set the Raft federation kill-switch so the Rust "
        "distributed_coordinator::install() keeps NoopDistributedCoordinator."
    )


# ---------------------------------------------------------------------------
# Test 2: THE regression-safety assertion — non-sandbox profiles never gated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", _NON_SANDBOX_PROFILES)
def test_non_sandbox_profiles_never_set_killswitch(
    profile: str, tmp_path: Path, monkeypatch
) -> None:
    """cluster/full/lite/embedded/cloud/auto → kill-switch is NEVER set.

    This is THE zero-regression guard: for every non-sandbox profile the
    boot path is byte-identical to before the Issue #4126 fix — the env var
    must be entirely absent so Rust installs the real Raft coordinator
    exactly as it always did.
    """
    _clean_federation_env(monkeypatch)

    result = _run_daemon(profile, tmp_path, monkeypatch)

    assert result.exit_code == 0, f"Unexpected exit for {profile!r}: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        f"profile={profile!r} must NOT touch NEXUS_FEDERATION_DISABLED — the "
        f"kill-switch is scoped STRICTLY to the sandbox profile."
    )


# ---------------------------------------------------------------------------
# Test 3: sandbox + explicit federation env → operator opt-in preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("opt_in_var", "opt_in_value"),
    [
        ("NEXUS_PEERS", "peer-a:2126,peer-b:2126"),
        ("NEXUS_HOSTNAME", "node-7.cluster.internal"),
        ("NEXUS_BOOTSTRAP_NEW", "1"),
    ],
)
def test_sandbox_with_explicit_federation_env_is_respected(
    opt_in_var: str, opt_in_value: str, tmp_path: Path, monkeypatch
) -> None:
    """sandbox + operator-set peers/hostname/bootstrap → gate must NOT force.

    If an operator deliberately opts into zone federation in sandbox (by
    setting any of NEXUS_PEERS / NEXUS_HOSTNAME / NEXUS_BOOTSTRAP_NEW), the
    gate must NOT set the kill-switch — their intent is preserved.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv(opt_in_var, opt_in_value)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        f"{opt_in_var} set → operator opted into federation; the gate must "
        f"NOT force NEXUS_FEDERATION_DISABLED."
    )


# ---------------------------------------------------------------------------
# Test 4: sandbox + operator-set kill-switch value is not overridden
# ---------------------------------------------------------------------------


def test_explicit_killswitch_value_not_overridden(tmp_path: Path, monkeypatch) -> None:
    """sandbox + NEXUS_FEDERATION_DISABLED="0" → setdefault leaves it "0".

    The gate uses ``os.environ.setdefault``, so an operator who explicitly
    set the kill-switch to a falsy "0" (re-enabling federation in sandbox)
    must have that honored, not stomped to "1".
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv("NEXUS_FEDERATION_DISABLED", "0")

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "0", (
        'setdefault must not override an operator-supplied NEXUS_FEDERATION_DISABLED="0".'
    )


# ---------------------------------------------------------------------------
# Test 5: config-sourced sandbox boot (no --profile) still gated (Issue #4126)
# ---------------------------------------------------------------------------


def test_config_file_sandbox_profile_sets_killswitch(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --config <yaml with profile: sandbox>`` + no --profile → gated.

    THE regression for the HIGH finding: with ``--config`` the CLI
    ``--profile`` value is never passed to ``nexus.connect`` — the kernel
    runs the file's ``profile: sandbox``. The kill-switch must gate on that
    EFFECTIVE profile, not the raw ``--profile`` (which is the ``"auto"``
    default here). Before the fix the env var stayed unset and the sandbox
    daemon installed the real Raft coordinator (bound :2126).
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")

    result = _run_daemon(None, tmp_path, monkeypatch, config_path=cfg)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1", (
        "A config-file sandbox boot (no --profile) must still set the Raft "
        "kill-switch — the gate must use the EFFECTIVE profile."
    )


def test_config_file_non_sandbox_profile_does_not_set_killswitch(
    tmp_path: Path, monkeypatch
) -> None:
    """``nexusd --config <yaml with profile: full>`` → kill-switch NOT set.

    The effective-profile resolver must not over-gate: a non-sandbox config
    profile keeps the byte-identical boot path (no env var).
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    cfg = tmp_path / "prod.yaml"
    cfg.write_text("profile: full\n")

    result = _run_daemon(None, tmp_path, monkeypatch, config_path=cfg)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        "A non-sandbox config profile must NOT touch NEXUS_FEDERATION_DISABLED."
    )

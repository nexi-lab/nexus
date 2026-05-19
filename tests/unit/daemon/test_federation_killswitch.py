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
    data_dir: Path | None = None,
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
    if data_dir is not None:
        args += ["--data-dir", str(data_dir)]
    if workspace is not None:
        args += ["--workspace", str(workspace)]

    with (
        patch("nexus.connect", mock_connect),
        patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
    ):
        runner = CliRunner()
        result = runner.invoke(main, args)

    return result, mock_bootstrapper_cls


# ---------------------------------------------------------------------------
# Test 1: sandbox profile (clean env) sets the kill-switch
# ---------------------------------------------------------------------------


def test_sandbox_profile_sets_federation_killswitch(tmp_path: Path, monkeypatch) -> None:
    """profile=sandbox + clean env → NEXUS_FEDERATION_DISABLED == "1"."""
    _clean_federation_env(monkeypatch)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result, _ = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

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

    result, _ = _run_daemon(profile, tmp_path, monkeypatch)

    assert result.exit_code == 0, f"Unexpected exit for {profile!r}: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        f"profile={profile!r} must NOT touch NEXUS_FEDERATION_DISABLED — the "
        f"kill-switch is scoped STRICTLY to the sandbox profile."
    )


# ---------------------------------------------------------------------------
# Test 3: sandbox + ambient NEXUS_PEERS/HOSTNAME/BOOTSTRAP_NEW → kill-switch
# STILL forced (Issue #4126 review r9 HIGH supersedes the r2/r3 opt-in).
#
# Pre-r9 this asserted the OPPOSITE (ambient federation env was treated as an
# implicit sandbox-federation opt-in and suppressed the kill-switch). r9
# removed that source-blind exclusion: a STALE / inherited NEXUS_HOSTNAME or
# NEXUS_PEERS must NOT re-open the no-federation/no-:2126 sandbox invariant.
# The ONLY supported sandbox-federation opt-out is now an EXPLICIT
# NEXUS_FEDERATION_DISABLED (covered by Test 4 +
# test_sandbox_explicit_optout_preserved_even_with_ambient_env below).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ambient_var", "ambient_value"),
    [
        ("NEXUS_PEERS", "peer-a:2126,peer-b:2126"),
        ("NEXUS_HOSTNAME", "node-7.cluster.internal"),
        ("NEXUS_BOOTSTRAP_NEW", "1"),
    ],
)
def test_sandbox_with_ambient_federation_env_still_forces_killswitch(
    ambient_var: str, ambient_value: str, tmp_path: Path, monkeypatch
) -> None:
    """sandbox + ambient peers/hostname/bootstrap → kill-switch STILL forced.

    Issue #4126 review r9 (HIGH): ambient NEXUS_PEERS / NEXUS_HOSTNAME /
    NEXUS_BOOTSTRAP_NEW is NOT a supported sandbox-federation opt-in. A
    stale/inherited value must NOT leave NEXUS_FEDERATION_DISABLED unset (that
    re-opened the :2126 Raft bind this branch closes). The kill-switch is
    UNCONDITIONAL for effective sandbox; the sole opt-out is an explicit
    NEXUS_FEDERATION_DISABLED.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv(ambient_var, ambient_value)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result, _ = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1", (
        f"ambient {ambient_var} must NOT implicitly re-enable sandbox "
        f"federation — the kill-switch is set UNCONDITIONALLY for effective "
        f"sandbox (only an explicit NEXUS_FEDERATION_DISABLED opts out)."
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

    result, _ = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

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

    result, _ = _run_daemon(None, tmp_path, monkeypatch, config_path=cfg)

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

    result, _ = _run_daemon(None, tmp_path, monkeypatch, config_path=cfg)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        "A non-sandbox config profile must NOT touch NEXUS_FEDERATION_DISABLED."
    )


# ---------------------------------------------------------------------------
# Review r2 (Issue #4126 HIGH A): centralize the EFFECTIVE profile across
# ALL daemon profile gates — sandbox flag-validation, SandboxBootstrapper
# gating, remote rejection, kill-switch — not just the kill-switch.
#
# Before r2 the sandbox flag-validation and the SandboxBootstrapper gate
# still used the raw CLI ``deployment_profile`` while the kill-switch used
# the effective profile. That inconsistency is the trust-boundary bug:
#   * ``--config sandbox.yaml --workspace W`` (no --profile) was REJECTED by
#     flag-validation (raw profile is "auto") even though the kernel IS
#     sandbox, and the bootstrapper never ran.
#   * ``--profile sandbox --config full.yaml --workspace W`` passed
#     flag-validation + ran SandboxBootstrapper against a NON-sandbox kernel.
# Each test below FAILS against the pre-r2 raw-profile gates.
# ---------------------------------------------------------------------------


def test_config_sandbox_workspace_passes_validation_and_runs_bootstrapper(
    tmp_path: Path, monkeypatch
) -> None:
    """``nexusd --config <sandbox.yaml> --workspace W`` (no --profile).

    Locks Finding A case (a): sandbox flag-validation must PASS (the
    effective profile is sandbox), the SandboxBootstrapper path must be
    taken (asserted via the patched class), and the kill-switch set.

    Pre-r2 this FAILS: flag-validation gated on the raw ``deployment_profile``
    (``"auto"`` here, since --config means --profile is never passed), so the
    daemon exited USAGE_ERROR and never instantiated the bootstrapper.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")

    result, bootstrapper_cls = _run_daemon(
        None, tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 0, (
        f"sandbox flag-validation must PASS for a --config sandbox boot "
        f"(effective profile is sandbox); got exit {result.exit_code}: "
        f"{result.output}"
    )
    bootstrapper_cls.assert_called_once()
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1", (
        "A config-file sandbox boot with --workspace must still set the "
        "Raft kill-switch (effective profile gate)."
    )


def test_cli_profile_conflicts_with_config_profile_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --profile sandbox --config <full.yaml>`` → clean CONFLICT.

    Locks Finding A case (b): an explicit CLI ``--profile`` that disagrees
    with the config file's ``profile:`` is rejected with a usage error
    BEFORE any sandbox-only side effect — NO SandboxBootstrapper, NO
    kill-switch. This prevents "sandbox behavior on a non-sandbox kernel"
    (and the inverse). The conflict check did not exist pre-r2 (config
    silently won), so this FAILS pre-fix.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "full.yaml"
    cfg.write_text("profile: full\n")

    result, bootstrapper_cls = _run_daemon(
        "sandbox", tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 64, (  # ExitCode.USAGE_ERROR
        f"--profile/--config profile conflict must be a clean usage error; "
        f"got exit {result.exit_code}: {result.output}"
    )
    assert "conflict" in result.output.lower()
    bootstrapper_cls.assert_not_called()
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        "A rejected conflicting invocation must not have set the kill-switch."
    )


def test_profile_full_with_workspace_no_config_still_rejected(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --profile full --workspace W`` (no config) → still rejected.

    Locks Finding A case (c) — the zero-regression guard: with NO --config
    the effective profile equals the raw CLI profile, so a non-sandbox
    profile + a sandbox-only flag must still be a usage error exactly as
    before r2 (no behavior change for the common no-config path).
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result, bootstrapper_cls = _run_daemon("full", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 64, (  # ExitCode.USAGE_ERROR
        f"--workspace with --profile full (no config) must remain a usage "
        f"error; got exit {result.exit_code}: {result.output}"
    )
    assert "only valid with --profile sandbox" in result.output
    bootstrapper_cls.assert_not_called()
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ


# ---------------------------------------------------------------------------
# Review r3 (Issue #4126 HIGH A): an explicit CLI --profile that disagrees
# with the config's EFFECTIVE profile must be rejected even when the config
# file OMITS ``profile:``. Pre-r3 the conflict check only fired when the file
# had a DIFFERENT explicit ``profile:``; an omitted ``profile:`` let
# load_config silently fall back to the default ("full"), so
# ``--profile sandbox --config <no-profile.yaml>`` ran a FULL kernel while
# silently dropping the operator's explicit sandbox intent (and kill-switch).
# ---------------------------------------------------------------------------


def test_cli_profile_conflicts_with_config_default_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --profile sandbox --config <yaml WITHOUT profile:>`` → REJECT.

    Locks Finding A (r3): the config omits ``profile:`` so ``load_config``
    resolves it to the ``NexusConfig`` default (``"full"``). An explicit CLI
    ``--profile sandbox`` therefore conflicts with the config's EFFECTIVE
    profile and must be a clean usage error BEFORE any sandbox side effect —
    no SandboxBootstrapper, no kill-switch.

    Pre-r3 this FAILS: the conflict check only compared against an EXPLICIT
    ``profile:`` key, so an omitted key did not conflict and the daemon
    silently ran ``full`` (exit 0, no bootstrapper) — the bug this locks.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "noprofile.yaml"
    cfg.write_text("backend: path_local\n")  # NO profile: key

    result, bootstrapper_cls = _run_daemon(
        "sandbox", tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 64, (  # ExitCode.USAGE_ERROR
        f"--profile sandbox with a --config that omits profile: (effective "
        f"'full') must be a clean usage error; got exit {result.exit_code}: "
        f"{result.output}"
    )
    assert "conflict" in result.output.lower()
    bootstrapper_cls.assert_not_called()
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        "A rejected conflicting invocation must not have set the kill-switch."
    )


def test_cli_profile_matches_config_default_is_allowed(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --profile full --config <yaml WITHOUT profile:>`` → ALLOWED.

    Locks Finding A (r3) the non-regression edge: when the explicit CLI
    ``--profile`` equals the config's EFFECTIVE profile (here the default
    ``"full"``, because the file omits ``profile:``) there is NO conflict —
    the daemon boots normally and (full is non-sandbox) the kill-switch is
    NEVER set.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    cfg = tmp_path / "noprofile.yaml"
    cfg.write_text("backend: path_local\n")  # NO profile: key

    result, bootstrapper_cls = _run_daemon("full", tmp_path, monkeypatch, config_path=cfg)

    assert result.exit_code == 0, (
        f"--profile full matching the config's effective default must NOT "
        f"conflict; got exit {result.exit_code}: {result.output}"
    )
    assert "conflict" not in result.output.lower()
    bootstrapper_cls.assert_not_called()
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ


def test_config_sandbox_no_cli_profile_unchanged(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --config <sandbox.yaml>`` (no --profile) → sandbox, unchanged.

    Zero-regression guard for Finding A (r3): with NO command-line
    ``--profile`` the conflict check never fires (it is gated on
    ParameterSource.COMMANDLINE), so a ``--config sandbox.yaml`` boot still
    resolves to the sandbox effective profile and sets the kill-switch
    exactly as before.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")

    result, bootstrapper_cls = _run_daemon(
        None, tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 0, (
        f"--config sandbox.yaml with no --profile must boot cleanly; got "
        f"exit {result.exit_code}: {result.output}"
    )
    assert "conflict" not in result.output.lower()
    bootstrapper_cls.assert_called_once()
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1"


# ---------------------------------------------------------------------------
# Review r7 (Issue #4126 HIGH): an EXPLICIT command-line ``--data-dir``
# combined with ``--config`` must be rejected as a usage error — never
# silently ignored. On the ``--config`` branch ``main`` calls
# ``load_config(Path(config_path))`` only; the Click ``--data-dir`` value is
# never forwarded, so pre-r7 ``nexusd --config sandbox.yaml --data-dir DIR``
# silently dropped DIR and shared the config/default data dir → PID +
# readiness collisions and state/data mixing across "isolated" per-agent
# sandboxes (defeating the r4–r6 isolation hardening). Mirrors exactly the
# r3 ``--profile``/``--config`` conflict precedent (same exit code, style,
# COMMANDLINE-only gating). ``$NEXUS_DATA_DIR`` env + ``--config`` is
# documented load_config precedence (NOT a conflict) and must stay allowed.
# ---------------------------------------------------------------------------


def test_cli_data_dir_conflicts_with_config_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --config <sandbox.yaml> --data-dir <dir>`` → clean CONFLICT.

    Locks the r7 HIGH: an explicit command-line ``--data-dir`` together with
    ``--config`` is a clean usage error BEFORE any sandbox side effect — no
    SandboxBootstrapper, no kill-switch — because the daemon would otherwise
    SILENTLY ignore the ``--data-dir`` (it is never forwarded on the
    ``--config`` branch) and share the config/default data dir.

    Pre-r7 this FAILS: today the daemon silently proceeds (exit 0, the
    bootstrapper runs against the config-file/default data dir) — the exact
    silent-ignore bug this locks.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")
    agent_dir = tmp_path / "agent-a"

    result, bootstrapper_cls = _run_daemon(
        None, tmp_path, monkeypatch, workspace=workspace, config_path=cfg, data_dir=agent_dir
    )

    assert result.exit_code == 64, (  # ExitCode.USAGE_ERROR
        f"--data-dir + --config must be a clean usage error (never silently "
        f"ignored); got exit {result.exit_code}: {result.output}"
    )
    assert "--data-dir cannot be combined with --config" in result.output
    bootstrapper_cls.assert_not_called()
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        "A rejected conflicting invocation must not have set the kill-switch."
    )


def test_config_sandbox_no_data_dir_unchanged(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --config <sandbox.yaml>`` (NO --data-dir) → unchanged.

    Zero-regression guard: with no command-line ``--data-dir`` the r7
    conflict check never fires (gated on ParameterSource.COMMANDLINE), so a
    ``--config sandbox.yaml`` boot still resolves normally and sets the
    kill-switch exactly as before.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")

    result, bootstrapper_cls = _run_daemon(
        None, tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 0, (
        f"--config sandbox.yaml with no --data-dir must boot cleanly; got "
        f"exit {result.exit_code}: {result.output}"
    )
    assert "cannot be combined" not in result.output
    bootstrapper_cls.assert_called_once()
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1"


def test_env_data_dir_with_config_is_allowed(tmp_path: Path, monkeypatch) -> None:
    """``NEXUS_DATA_DIR=<dir>`` + ``--config <sandbox.yaml>`` → ALLOWED.

    Locks the r7 non-regression edge: ``$NEXUS_DATA_DIR`` + ``--config`` is
    DOCUMENTED ``load_config`` precedence (config file > env), NOT a user
    conflict — only an EXPLICIT command-line ``--data-dir`` triggers the
    rejection (ParameterSource.COMMANDLINE), exactly analogous to env
    ``NEXUS_PROFILE`` + ``--config`` being allowed. The env value reaches
    Click but its parameter source is ENVIRONMENT, not COMMANDLINE, so the
    boot must proceed normally.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "sandbox.yaml"
    cfg.write_text("profile: sandbox\n")
    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("NEXUS_DATA_DIR", str(env_dir))

    result, bootstrapper_cls = _run_daemon(
        None, tmp_path, monkeypatch, workspace=workspace, config_path=cfg
    )

    assert result.exit_code == 0, (
        f"$NEXUS_DATA_DIR + --config is documented precedence, not a "
        f"conflict; got exit {result.exit_code}: {result.output}"
    )
    assert "cannot be combined" not in result.output
    bootstrapper_cls.assert_called_once()
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1"


def test_profile_sandbox_data_dir_no_config_unchanged(tmp_path: Path, monkeypatch) -> None:
    """``nexusd --profile sandbox --data-dir <dir>`` (NO --config) → unchanged.

    Zero-regression guard: with NO ``--config`` the explicit ``--data-dir``
    is the legitimate isolated-data-dir override (r4–r6) and must keep
    working exactly as before — the r7 conflict check is gated on
    ``config_path`` being set, so it never fires here.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.delenv("NEXUS_PROFILE", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent_dir = tmp_path / "agent-a"

    result, bootstrapper_cls = _run_daemon(
        "sandbox", tmp_path, monkeypatch, workspace=workspace, data_dir=agent_dir
    )

    assert result.exit_code == 0, (
        f"--profile sandbox --data-dir with no --config must remain the "
        f"isolated-override (unchanged); got exit {result.exit_code}: "
        f"{result.output}"
    )
    assert "cannot be combined" not in result.output
    bootstrapper_cls.assert_called_once()
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1"


# ---------------------------------------------------------------------------
# Review r9 (Issue #4126 HIGH): the sandbox federation kill-switch must be
# set UNCONDITIONALLY for the effective-sandbox profile. The earlier r2/r3
# exclusion (skip the kill-switch when any of NEXUS_PEERS / NEXUS_HOSTNAME /
# NEXUS_BOOTSTRAP_NEW was present) was SOURCE-BLIND: a STALE / inherited
# NEXUS_HOSTNAME (commonly exported by other tooling/shells) left the
# kill-switch UNSET → the Rust install()/init_from_env path ran and
# ZoneManager bound the Raft gRPC server on 0.0.0.0:2126 — re-opening the
# exact no-federation/no-:2126 sandbox invariant this branch closes. The ONLY
# supported opt-out is an EXPLICIT operator-set NEXUS_FEDERATION_DISABLED
# (setdefault preserves it). Ambient NEXUS_HOSTNAME/PEERS/BOOTSTRAP_NEW must
# NOT implicitly re-enable federation. Cases (a)/(b) FAIL pre-r9.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ambient_var", "ambient_value"),
    [
        ("NEXUS_HOSTNAME", "somehost"),
        ("NEXUS_PEERS", "peer-a:2126,peer-b:2126"),
        ("NEXUS_BOOTSTRAP_NEW", "1"),
    ],
)
def test_sandbox_ambient_federation_env_does_not_bypass_killswitch(
    ambient_var: str, ambient_value: str, tmp_path: Path, monkeypatch
) -> None:
    """sandbox + ambient NEXUS_HOSTNAME/PEERS/BOOTSTRAP_NEW (no explicit
    NEXUS_FEDERATION_DISABLED) → kill-switch STILL forced to "1".

    Pre-r9 this FAILS: the source-blind exclusion left
    NEXUS_FEDERATION_DISABLED unset whenever any of these ambient vars was
    present, so a sandbox daemon installed the real Raft coordinator (bound
    :2126) purely from a stale/inherited env var. Post-r9 the kill-switch is
    unconditional for effective sandbox.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv(ambient_var, ambient_value)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result, _ = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "1", (
        f"Ambient {ambient_var} must NOT implicitly re-enable sandbox "
        f"federation — the kill-switch is set UNCONDITIONALLY for effective "
        f"sandbox (only an explicit NEXUS_FEDERATION_DISABLED opts out)."
    )


def test_sandbox_explicit_optout_preserved_even_with_ambient_env(
    tmp_path: Path, monkeypatch
) -> None:
    """sandbox + explicit NEXUS_FEDERATION_DISABLED="0" (plus ambient
    NEXUS_HOSTNAME) → setdefault leaves it "0".

    The sole supported sandbox-federation opt-out is the EXPLICIT
    NEXUS_FEDERATION_DISABLED env var; ``setdefault`` must not stomp it even
    though the kill-switch is otherwise unconditional and an ambient
    NEXUS_HOSTNAME is also present.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv("NEXUS_FEDERATION_DISABLED", "0")
    monkeypatch.setenv("NEXUS_HOSTNAME", "somehost")

    workspace = tmp_path / "ws"
    workspace.mkdir()

    result, _ = _run_daemon("sandbox", tmp_path, monkeypatch, workspace=workspace)

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert os.environ.get("NEXUS_FEDERATION_DISABLED") == "0", (
        'An explicit NEXUS_FEDERATION_DISABLED="0" is the sole supported '
        "sandbox opt-out and must survive setdefault."
    )


@pytest.mark.parametrize("profile", _NON_SANDBOX_PROFILES)
def test_non_sandbox_with_ambient_federation_env_unchanged(
    profile: str, tmp_path: Path, monkeypatch
) -> None:
    """Regression: non-sandbox effective profile + ambient NEXUS_HOSTNAME →
    NEXUS_FEDERATION_DISABLED still NOT set (byte-identical boot path).

    The r9 unconditional kill-switch is scoped STRICTLY to effective sandbox;
    non-sandbox profiles never enter the branch regardless of ambient env.
    """
    _clean_federation_env(monkeypatch)
    monkeypatch.setenv("NEXUS_HOSTNAME", "somehost")

    result, _ = _run_daemon(profile, tmp_path, monkeypatch)

    assert result.exit_code == 0, f"Unexpected exit for {profile!r}: {result.output}"
    assert "NEXUS_FEDERATION_DISABLED" not in os.environ, (
        f"profile={profile!r} must NOT touch NEXUS_FEDERATION_DISABLED even "
        f"with ambient NEXUS_HOSTNAME — kill-switch is sandbox-only."
    )

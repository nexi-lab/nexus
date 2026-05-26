"""``nexusd`` entry point — Nexus node daemon + node-local commands.

Subcommands:
- (default, no subcommand) — start the daemon
- ``nexusd share`` — share a local subtree as a federation zone
- ``nexusd join``  — join a peer's federation zone
"""

from __future__ import annotations

import json as json_mod
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from nexus.cli.exit_codes import ExitCode
from nexus.daemon.sandbox_bootstrap import SandboxBootstrapper

logger = logging.getLogger("nexusd")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _JsonLogFormatter(logging.Formatter):
    """Structured JSON log formatter for production use."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json_mod.dumps(entry)


def _is_nexusd_process(pid: int) -> bool:
    """Check whether *pid* belongs to a running ``nexusd`` process.

    On Linux we inspect ``/proc/<pid>/cmdline``; elsewhere we fall back to
    ``os.kill(pid, 0)`` which only tells us *some* process is alive.  The
    cmdline check prevents false positives after PID reuse — common in Docker
    containers with small PID namespaces after a segfault/crash restart.
    """
    # Fast path: process doesn't exist at all. On Windows, os.kill(pid, 0)
    # raises plain OSError (e.g. WinError 87 "invalid parameter") rather
    # than ProcessLookupError when the PID is dead, so catch OSError too.
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False

    # On Linux, verify the process is actually nexusd
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes()
        # /proc/PID/cmdline uses NUL separators; join for easy substring search
        cmdline_str = cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        return "nexusd" in cmdline_str or "nexus.daemon" in cmdline_str
    except (FileNotFoundError, PermissionError, OSError):
        # Not Linux or can't read — conservatively assume it's nexusd
        return True


def _scoped_pid_path(effective_data_dir: str | None) -> Path | None:
    """Per-instance PID path derived from the effective data dir.

    Mirrors ``_scoped_readiness_path`` EXACTLY (Issue #4126 review r5,
    Finding A): returns ``<effective_data_dir>/.nexusd.pid`` when a data dir
    is known and creatable; falls back to a HOME-scoped hashed filename
    (``~/.nexus/nexusd-<sha256[:12]>.pid``) if the data dir is unusable;
    returns ``None`` when no data dir is given (single-daemon default — only
    the legacy global ``~/.nexus/nexusd.pid`` is used, exactly as before, so
    single-daemon double-start prevention is byte-for-byte unchanged).
    """
    if not effective_data_dir:
        return None
    try:
        d = Path(effective_data_dir).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d / ".nexusd.pid"
    except OSError:
        import hashlib

        digest = hashlib.sha256(str(effective_data_dir).encode()).hexdigest()[:12]
        return Path.home() / ".nexus" / f"nexusd-{digest}.pid"


def _pid_gate_check(pid_path: Path, *, blocking: bool) -> None:
    """Stale/running check for a single PID file, then write our pid.

    A live ``nexusd`` recorded in *pid_path* means a genuine double-start;
    when *blocking* is ``True`` we reject (``sys.exit``). When *blocking* is
    ``False`` (the legacy global path while an INSTANCE-scoped path is the
    real gate) a foreign live pid is left intact and NOT overwritten so a
    still-running sibling daemon's record survives — the scoped path alone
    decides "already running" for that instance. A stale/dead/non-nexusd
    pid is always cleared so we can take ownership.
    """
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
        else:
            if _is_nexusd_process(old_pid):
                if blocking:
                    click.echo(f"Error: nexusd is already running (PID {old_pid}).", err=True)
                    click.echo(f"PID file: {pid_path}", err=True)
                    sys.exit(ExitCode.CONFIG_ERROR)
                # Non-blocking (legacy global) path: a different-data-dir
                # sandbox is allowed to start. Do NOT clobber the live
                # sibling's global pid record — leave it for its owner.
                return
            # PID doesn't exist or belongs to a different process — stale.
            pid_path.unlink(missing_ok=True)

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def _manage_pid_file(effective_data_dir: str | None) -> tuple[Path | None, Path]:
    """Check for stale PID files and write current PID (Issue #4126 r5).

    Returns ``(scoped_pid_path_or_None, legacy_pid_path)``.

    The PID gate's *blocking* (already-running) decision is made PER
    EFFECTIVE DATA DIR so two sandboxes under the SAME HOME with distinct
    ``--data-dir`` values can BOTH start (the previous single global
    ``~/.nexus/nexusd.pid`` gate exited the second sandbox before it ever
    reached the r4 scoped-readiness write, defeating per-agent isolation).

    Back-compat — single-daemon default (no data dir, scoped path ``None``):
    the legacy global ``~/.nexus/nexusd.pid`` is the BLOCKING gate exactly as
    before, so a genuine second ``nexusd`` is still rejected. When a scoped
    data dir IS given, the SCOPED file is the blocking gate (a genuine
    double-start on the SAME data dir is still rejected) and the legacy
    global file is written best-effort but is NON-blocking — a stale/foreign
    global pid must never block a different-data-dir sandbox.
    """
    legacy_path = Path.home() / ".nexus" / "nexusd.pid"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)

    scoped_path = _scoped_pid_path(effective_data_dir)

    if scoped_path is None:
        # Single-daemon default: legacy global path IS the blocking gate
        # (identical behavior + artifact to the pre-r5 implementation).
        _pid_gate_check(legacy_path, blocking=True)
        return None, legacy_path

    # Instance-scoped: the SCOPED file gates this instance (genuine
    # double-start on the same data dir still rejected); the legacy global
    # file is kept for single-daemon back-compat readers but is NON-blocking
    # so a foreign/stale global pid never blocks a different-data-dir start.
    _pid_gate_check(scoped_path, blocking=True)
    _pid_gate_check(legacy_path, blocking=False)
    return scoped_path, legacy_path


def _remove_pid_file(pid_path: Path) -> None:
    """Remove a PID file on shutdown — ownership-aware (Issue #4126 r5).

    Only unlink a pid file that records THIS process, or whose recorded pid
    is stale/dead/not-nexusd. A live sibling daemon's pid file (the shared
    legacy global path that a still-running concurrent sandbox re-wrote with
    ITS pid) must survive: removing it would erase that daemon's
    double-start guard while it is still serving. Mirrors the readiness
    ``_remove_readiness_if_owned`` ownership contract.
    """
    try:
        recorded = pid_path.read_text().strip()
    except (FileNotFoundError, OSError):
        return
    if recorded == str(os.getpid()):
        pid_path.unlink(missing_ok=True)
        return
    try:
        other = int(recorded)
    except ValueError:
        # Corrupt/garbage content we no longer recognize — safe to clear.
        pid_path.unlink(missing_ok=True)
        return
    if not _is_nexusd_process(other):
        # Stale/dead/non-nexusd: clear it.
        pid_path.unlink(missing_ok=True)
    # Else: a live sibling nexusd owns it — leave it intact.


# ---------------------------------------------------------------------------
# Readiness file (atomic write + ownership-checked unlink) — Issue #4126 r4
# ---------------------------------------------------------------------------
#
# All daemons under one HOME historically shared a single global
# ``~/.nexus/nexusd.ready``. Two sandboxes under the same HOME (the real
# per-agent production case) caused last-writer-wins + premature-unlink: the
# first daemon to exit ``unlink``ed the readiness file out from under a
# still-running sibling, breaking ``nexus ready`` discovery.
#
# Fix: (1) atomic write via temp + ``os.replace`` (no torn reads); (2) an
# identifying token (host:port AND this process's pid) so a daemon only
# removes a readiness file that still belongs to IT; (3) an ADDITIONAL
# data-dir-scoped readiness file so two sandboxes with distinct data dirs get
# distinct, deterministically-addressable readiness records. The legacy
# global path is still written for single-daemon back-compat.
#
# File format (back-compat preserved): the FIRST line is ``host:port`` exactly
# as before so old readers / ``nexus ready``'s ``_parse_endpoint`` keep
# working; a second ``pid=<pid>`` line carries the ownership token.


def _readiness_token(host: str, port: int) -> str:
    """The full identifying content for THIS process's readiness file.

    First line is ``host:port`` (unchanged wire format for back-compat with
    pre-r4 single-line readiness files and ``nexus ready``); the second line
    binds the record to this process so a sibling daemon's ``finally`` never
    removes our file (and vice versa).
    """
    return f"{host}:{port}\npid={os.getpid()}\n"


def _write_readiness_atomic(path: Path, host: str, port: int) -> None:
    """Atomically write the readiness file (temp + ``os.replace``).

    Prevents a concurrent ``nexus ready`` from observing a torn/half-written
    file when two daemons race on the same path.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    content = _readiness_token(host, port)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_name, str(path))
    except BaseException:
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _remove_readiness_if_owned(path: Path, host: str, port: int) -> None:
    """Unlink *path* ONLY if its on-disk content is still THIS process's token.

    A different daemon (different pid, or a different host:port that replaced
    our record) must keep its readiness file: removing it would make
    ``nexus ready`` falsely time out / resolve a dead endpoint for a daemon
    that is still serving.
    """
    expected = _readiness_token(host, port)
    try:
        actual = path.read_text()
    except (FileNotFoundError, OSError):
        return
    if actual == expected:
        path.unlink(missing_ok=True)


def _scoped_readiness_path(effective_data_dir: str | None) -> Path | None:
    """Per-instance readiness path derived from the effective data dir.

    Uses the shared ``scoped_readiness_path`` SSOT from ``cli.state`` for
    the filename convention. Creates the parent directory if absent and
    falls back to a HOME-scoped hashed filename on OSError. Returns
    ``None`` when no data dir is given (single-daemon default — only the
    legacy global path is used, exactly as before).
    """
    from nexus.cli.state import scoped_readiness_path

    if not effective_data_dir:
        return None
    try:
        d = Path(effective_data_dir).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return scoped_readiness_path(d)
    except OSError:
        import hashlib

        digest = hashlib.sha256(str(effective_data_dir).encode()).hexdigest()[:12]
        return Path.home() / ".nexus" / f"nexusd-{digest}.ready"


def _redact_url(url: str) -> str:
    """Redact password from database URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url


def _print_lifecycle_summary(nx: Any) -> None:
    """Print one-line service lifecycle summary at startup (Issue #1578).

    Shown for every profile so operators know at a glance whether the
    daemon has persistent workers and hot-swappable services.
    """
    try:
        coordinator = getattr(nx, "_lifecycle_coordinator", None)
        if coordinator is None:
            return

        quadrants = coordinator.classify_all()
        if not quadrants:
            return

        n_persistent = sum(1 for q in quadrants.values() if q.is_persistent)
        n_hot = sum(1 for q in quadrants.values() if q.is_hot_swappable)

        parts: list[str] = [f"{len(quadrants)} services"]
        if n_hot:
            parts.append(f"{n_hot} hot-swappable")
        if n_persistent:
            parts.append(f"{n_persistent} persistent")
        distro = "persistent" if n_persistent else "on-demand"
        parts.append(f"distro={distro}")

        click.echo(f"  Lifecycle: {', '.join(parts)}")
    except Exception:
        pass  # best-effort — never block startup


def _read_config_file_key(config_path: str, key: str) -> str | None:
    """Return a top-level string *key* from a YAML config file, or ``None``.

    Best-effort: any read/parse failure returns ``None`` (the real failure is
    deferred to ``load_config`` for a clearer error).
    """
    try:
        import yaml

        path = Path(config_path)
        if path.exists() and path.suffix in (".yaml", ".yml"):
            with open(path) as fh:
                loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                raw = loaded.get(key)
                if isinstance(raw, str) and raw:
                    return raw
    except Exception:
        return None
    return None


def _resolve_effective_profile(
    deployment_profile: str,
    config_path: str | None,
) -> str:
    """Resolve the profile the kernel will ACTUALLY run (Issue #4126 HIGH).

    The federation kill-switch must gate on the *effective* profile, not the
    raw CLI ``--profile`` value. The daemon honors three profile sources, and
    when ``--config`` is given the CLI ``--profile`` value is NOT passed to
    ``nexus.connect`` at all (see the ``config_path`` branch in ``main``):
    ``nexus.connect(config=load_config(Path(config_path)))``.

    ``nexus.config.load_config`` resolves the profile with this precedence
    (see ``_load_from_dict``: ``merged = _build_env_overrides(); merged.update
    (config_dict)``):

      1. the config file's ``profile:`` key (highest — overrides env)
      2. the ``NEXUS_PROFILE`` env var
      3. the ``NexusConfig`` default (``"full"``)

    When NO ``--config`` is given the daemon passes ``{"profile":
    deployment_profile}`` directly, so the CLI value (or its ``"auto"``
    default / ``NEXUS_PROFILE`` envvar wired by Click) is authoritative.

    This mirrors that resolution so the kill-switch sees the same profile the
    kernel will. Best-effort: any failure reading the config file falls back
    to ``deployment_profile`` (``load_config`` itself will raise later with a
    clearer error, preserving prior behavior).
    """
    if not config_path:
        # No --config: nexus.connect gets {"profile": deployment_profile}
        # verbatim. Click already folds NEXUS_PROFILE into this value.
        return deployment_profile

    # --config given: replicate load_config's profile precedence.
    # 1. config file profile wins (config_dict.update over env overrides).
    file_profile = _read_config_file_key(config_path, "profile")
    if file_profile is not None:
        return file_profile
    # 2. then NEXUS_PROFILE env.
    env_profile = os.environ.get("NEXUS_PROFILE")
    if env_profile:
        return env_profile
    # 3. then the NexusConfig default.
    return "full"


def _resolve_effective_data_dir(
    cli_data_dir: str | None,
    config_path: str | None,
    effective_profile: str,
) -> str | None:
    """Resolve the data dir the kernel will ACTUALLY use (Issue #4126
    review r6, Finding B). Sibling of ``_resolve_effective_profile``.

    PID + readiness scoping must key off the SAME data dir
    ``load_config``/``_apply_sandbox_defaults`` will resolve, NOT just the
    Click ``--data-dir`` option (which is computed BEFORE ``--config`` is
    loaded). If a sandbox config FILE supplies ``data_dir`` and the user
    did NOT pass ``--data-dir``, scoping previously fell back to the shared
    ``~/.nexus`` globals → concurrent same-HOME sandboxes via distinct
    config files still blocked each other and ``nexus ready --data-dir``
    couldn't target them.

    Returns the EFFECTIVE data dir, or ``None`` to PRESERVE legacy
    single-daemon behavior (no ``--data-dir``, no ``--config`` ⇒ shared
    global PID/readiness paths, unchanged back-compat).

    Replicates ``nexus.config`` precedence EXACTLY so this never diverges
    from how the daemon later resolves it:

      * NO ``--config`` (daemon passes ``{"profile": …, "data_dir":
        cli_data_dir?}`` to ``nexus.connect``): the Click ``--data-dir`` /
        ``$NEXUS_DATA_DIR`` value wins when set; else the profile default
        (sandbox ⇒ ``~/.nexus/sandbox`` per ``_apply_sandbox_defaults``;
        any other profile ⇒ ``None`` ⇒ keep legacy globals).
      * ``--config`` given (daemon calls ``load_config(Path(config_path))``
        — the Click ``--data-dir`` value is NOT forwarded on this branch;
        only ``$NEXUS_DATA_DIR`` reaches it, via ``_build_env_overrides``):
        ``load_config`` does ``merged = _build_env_overrides();
        merged.update(config_dict)`` so the config FILE's ``data_dir``
        overrides ``$NEXUS_DATA_DIR``; if neither is set,
        ``_apply_sandbox_defaults`` fills ``~/.nexus/sandbox`` for sandbox
        (else ``None`` ⇒ legacy globals).

    Best-effort: any failure reading the config file degrades to the
    no-config rule (``load_config`` itself raises later with a clearer
    error), preserving prior behavior.
    """
    sandbox_default = str(Path.home() / ".nexus" / "sandbox")

    if not config_path:
        # No --config: nexus.connect gets data_dir=cli_data_dir verbatim
        # (Click already folds $NEXUS_DATA_DIR into it). Explicit wins.
        if cli_data_dir:
            return cli_data_dir
        # Unset: _apply_sandbox_defaults supplies ~/.nexus/sandbox ONLY for
        # the sandbox profile. Any other profile keeps data_dir=None →
        # legacy global PID/readiness (single-daemon back-compat).
        if effective_profile == "sandbox":
            return sandbox_default
        return None

    # --config given: the Click --data-dir value is NOT forwarded on this
    # branch (main() calls load_config(Path(config_path)) only). Replicate
    # load_config: config-file data_dir overrides $NEXUS_DATA_DIR.
    file_data_dir = _read_config_file_key(config_path, "data_dir")
    if file_data_dir:
        return file_data_dir
    env_data_dir = os.environ.get("NEXUS_DATA_DIR")
    if env_data_dir:
        return env_data_dir
    # Neither set: _apply_sandbox_defaults fills ~/.nexus/sandbox for the
    # sandbox profile; any other profile keeps None → legacy globals.
    if effective_profile == "sandbox":
        return sandbox_default
    return None


def _will_use_static_admin_fallback(
    auth_type: str | None,
    api_key: str | None,
) -> bool:
    """Predict the "single trusted operator key" boot path (Issue #4237).

    The daemon falls back to ``StaticAPIKeyAuth`` with an implicit
    ``subject_id="admin", is_admin=True`` principal whenever ``auth_type``
    is unset or ``"static"`` AND an API key is reachable — either via the
    ``--api-key`` / ``$NEXUS_API_KEY`` value or via ``$NEXUS_API_KEY_FILE``.

    In that mode the ReBAC filter on the search read path will deny 100%
    of results unless ``allow_admin_bypass=True`` (the static admin has
    no ReBAC tuples). This predicate gates the auto-default applied by
    ``_should_default_admin_bypass``.
    """
    if auth_type not in (None, "static"):
        return False
    if api_key:
        return True
    key_file = os.environ.get("NEXUS_API_KEY_FILE", "")
    return bool(key_file and Path(key_file).is_file())


def _should_default_admin_bypass(
    auth_type: str | None,
    api_key: str | None,
    *,
    already_set: bool,
) -> bool:
    """Decide whether to default ``allow_admin_bypass=True`` for static-auth
    single-key deployments (Issue #4237).

    Returns False when the operator has explicitly chosen a value
    (``already_set=True`` from the config file, or any
    ``$NEXUS_ALLOW_ADMIN_BYPASS`` env value), or when the static-auth
    fallback won't fire.

    The static-auth fallback at line ~951 creates an implicit
    ``is_admin=True`` principal; without admin bypass the ReBAC filter
    on the search read path denies every result. Auto-defaulting for
    this deployment shape restores parity with the previous edge.
    """
    if already_set:
        return False
    if os.environ.get("NEXUS_ALLOW_ADMIN_BYPASS") is not None:
        return False
    return _will_use_static_admin_fallback(auth_type, api_key)


# ---------------------------------------------------------------------------
# CLI group — bare ``nexusd`` starts the daemon, subcommands are node-local ops
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option(
    "--host",
    default=None,
    envvar="NEXUS_HOST",
    help="Bind address (default: 0.0.0.0).",
    show_default=True,
)
@click.option(
    "--port",
    type=int,
    default=None,
    envvar="NEXUS_PORT",
    help="Listen port (default: 2026).",
    show_default=True,
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    envvar="NEXUS_CONFIG_FILE",
    help="Path to YAML config file.",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    envvar="NEXUS_DATA_DIR",
    help="Local data directory (default: ~/.nexus/data).",
)
@click.option(
    "--profile",
    "deployment_profile",
    default=None,
    envvar="NEXUS_PROFILE",
    help="Deployment profile: full, lite, embedded, cloud, auto (default: auto).",
)
@click.option(
    "--api-key",
    default=None,
    envvar="NEXUS_API_KEY",
    help="Static API key for authentication.",
)
@click.option(
    "--database-url",
    default=None,
    envvar="NEXUS_DATABASE_URL",
    help="PostgreSQL connection URL for RecordStore.",
)
@click.option(
    "--auth-type",
    type=click.Choice(["static", "database", "none"]),
    default=None,
    help="Authentication backend type.",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default=None,
    envvar="NEXUS_LOG_LEVEL",
    help="Logging level (default: info).",
)
@click.option(
    "--log-format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    envvar="NEXUS_LOG_FORMAT",
    help="Log output format (default: text).",
)
@click.option(
    "--workers",
    type=int,
    default=None,
    envvar="NEXUS_WORKERS",
    help="Number of uvicorn workers (default: 1).",
)
@click.option(
    "--workspace",
    "workspace",
    type=click.Path(),
    default=None,
    envvar="NEXUS_WORKSPACE",
    help="Local directory to index and mount as 'local' zone (sandbox profile only).",
)
@click.option(
    "--hub-url",
    "hub_url",
    default=None,
    envvar="NEXUS_HUB_URL",
    help="Hub gRPC endpoint for sandbox federation (sandbox profile only).",
)
@click.option(
    "--hub-token",
    "hub_token",
    default=None,
    envvar="NEXUS_HUB_TOKEN",
    help="Bearer token for hub authentication (sandbox profile only; prefer env var).",
)
@click.version_option(package_name="nexus-ai-fs", prog_name="nexusd")
@click.pass_context
def main(
    ctx: click.Context,
    host: str | None,
    port: int | None,
    config_path: str | None,
    data_dir: str | None,
    deployment_profile: str | None,
    api_key: str | None,
    database_url: str | None,
    auth_type: str | None,
    log_level: str | None,
    log_format: str,
    workers: int | None,
    workspace: str | None,
    hub_url: str | None,
    hub_token: str | None,
) -> None:
    """Nexus node daemon.

    Start a long-running process that exposes gRPC/HTTP APIs for file
    operations, search, permissions, and federation.

    \b
    Examples:
        nexusd                                  # start daemon (defaults)
        nexusd --port 2026 --host 0.0.0.0       # explicit bind
        nexusd --config /etc/nexus/config.yaml   # from config file
        nexusd share /data/shared                # share a subtree
        nexusd join peer1:2126 /shared /local    # join a peer's zone
        nexusd --profile sandbox --workspace ~/code --hub-url grpc://hub:443
    """
    # If a subcommand was invoked, skip daemon startup
    if ctx.invoked_subcommand is not None:
        return

    # --- Defaults -----------------------------------------------------------
    # Capture whether --port was user-supplied BEFORE we apply the default,
    # so the gRPC port derivation logic below knows which signal to trust.
    _port_explicit = ctx.get_parameter_source("port") == click.core.ParameterSource.COMMANDLINE
    host = host or "0.0.0.0"
    port = port or 2026
    log_level = log_level or "info"

    # Issue #4238: normalize the canonical ``postgres://`` scheme that
    # cloud providers (Railway, Render, Supabase, Heroku) emit by default.
    # NexusConfig.database_url has the same validator for the
    # ``--config`` branch, but the env/CLI branch passes ``database_url``
    # straight into ``SQLAlchemyRecordStore`` / ``DatabaseAPIKeyAuth``.
    from nexus.core.db_utils import normalize_database_url as _norm_db_url

    if database_url:
        database_url = _norm_db_url(database_url)

    # gRPC port resolution (Issue #3980 follow-up): shared with the
    # ``nexus up`` sandbox branch via ``derive_grpc_port`` so persisted
    # state matches the port nexusd actually binds (Issue #4144 MINOR 6).
    from nexus.cli.state import derive_grpc_port

    os.environ["NEXUS_GRPC_PORT"] = str(derive_grpc_port(port, _port_explicit))

    deployment_profile = deployment_profile or "auto"

    # --- Effective profile (Issue #4126 HIGH, review r2) --------------------
    # Compute the profile the kernel will ACTUALLY run ONCE, here, before any
    # profile-dependent gate. ``load_config`` ignores the raw CLI ``--profile``
    # when ``--config`` is given (config-file ``profile:`` > ``NEXUS_PROFILE``
    # env > CLI ``--profile``), so gating on the raw CLI value is both unsafe
    # (sandbox flag-validation + SandboxBootstrapper could run against a
    # non-sandbox kernel) and inconsistent (a ``--config sandbox.yaml`` boot
    # would be wrongly rejected). EVERY gate below uses ``effective_profile``.
    #
    # Conflict precedence (Issue #4126 review r3, Finding A): an EXPLICIT
    # command-line ``--profile X`` together with ``--config`` is rejected with
    # a usage error whenever ``X`` does not equal the profile ``load_config``
    # will TRULY use for that config. ``load_config`` resolves the
    # config-derived profile as: the file's ``profile:`` if present, else
    # ``NEXUS_PROFILE`` env, else the ``NexusConfig`` default (``"full"`` —
    # see ``src/nexus/config.py``); the raw CLI ``--profile`` is never passed
    # to ``nexus.connect`` on the ``--config`` branch. So comparing only
    # against an EXPLICIT ``profile:`` in the file (the pre-r3 rule) left a
    # silent hole: ``nexusd --profile sandbox --config c.yaml`` where
    # ``c.yaml`` OMITS ``profile:`` did NOT conflict, yet the kernel ran
    # ``full`` (the config default) — the operator's explicit ``--profile
    # sandbox`` (and its kill-switch) silently ignored. We now compare the
    # explicit CLI value against the config's EFFECTIVE profile
    # (``_resolve_effective_profile`` with ``config_path`` set already
    # replicates exactly load_config's file→env→default precedence and
    # deliberately ignores the CLI value), so the omitted-``profile:`` case
    # is correctly rejected. Only a COMMANDLINE ``--profile`` triggers this:
    # ``NEXUS_PROFILE`` env (or the ``"auto"`` default) losing to the config
    # file is documented ``load_config`` precedence, not a user conflict.
    _profile_src = ctx.get_parameter_source("deployment_profile")
    if config_path and _profile_src == click.core.ParameterSource.COMMANDLINE:
        _config_effective = _resolve_effective_profile(deployment_profile, config_path)
        if _config_effective != deployment_profile:
            _file_profile = _read_config_file_key(config_path, "profile")
            if _file_profile is not None:
                _src_desc = f"sets profile: {_file_profile!r}"
            else:
                _src_desc = f"omits profile: (load_config resolves it to {_config_effective!r})"
            click.echo(
                f"Error: conflicting profile — CLI --profile {deployment_profile!r} "
                f"but --config {config_path} {_src_desc}. "
                "Remove one (the daemon will not silently pick a profile).",
                err=True,
            )
            sys.exit(ExitCode.USAGE_ERROR)

    # Conflict precedence (Issue #4126 review r7): an EXPLICIT command-line
    # ``--data-dir`` together with ``--config`` is rejected with a usage
    # error — same location, exit code and style as the r3 ``--profile``/
    # ``--config`` conflict above. On the ``--config`` branch ``main`` calls
    # ``load_config(Path(config_path))`` only; the Click ``--data-dir`` value
    # is NEVER forwarded, so a ``nexusd --config sandbox.yaml --data-dir
    # /tmp/agent-a`` invocation would SILENTLY ignore ``/tmp/agent-a`` and
    # fall back to the config-file ``data_dir`` / ``$NEXUS_DATA_DIR`` /
    # sandbox default — operators believe they launched isolated per-agent
    # sandboxes but invocations share one data dir → PID/readiness collisions
    # + state/data mixing (defeats the r4–r6 isolation hardening). Reject
    # rather than silently ignore (the established, safe r3 precedent; an
    # invasive load_config merge is deliberately avoided). Only a COMMANDLINE
    # ``--data-dir`` triggers this: ``$NEXUS_DATA_DIR`` env + ``--config`` is
    # DOCUMENTED ``load_config`` precedence (config file > env), not a user
    # conflict — exactly analogous to env ``NEXUS_PROFILE`` + ``--config``
    # being allowed while an explicit conflicting ``--profile`` is rejected.
    _data_dir_src = ctx.get_parameter_source("data_dir")
    if config_path and _data_dir_src == click.core.ParameterSource.COMMANDLINE:
        click.echo(
            "Error: --data-dir cannot be combined with --config; "
            "set 'data_dir:' in the config file instead.",
            err=True,
        )
        sys.exit(ExitCode.USAGE_ERROR)

    effective_profile = _resolve_effective_profile(deployment_profile, config_path)

    # --- Sandbox flag validation --------------------------------------------
    # --workspace, --hub-url, --hub-token are ONLY valid with --profile
    # sandbox.
    #
    # Issue #4126 review r8 (the DAEMON-side parallel of the r3 ``nexus up``
    # fix in ``src/nexus/cli/commands/stack.py``): the rejection must fire
    # ONLY for flags actually set ON THE COMMAND LINE, NOT for values sourced
    # from env vars or the option default. These three options are
    # envvar-backed (``NEXUS_WORKSPACE``/``NEXUS_HUB_URL``/``NEXUS_HUB_TOKEN``)
    # and this branch's ``nexus env`` now EMITS ``NEXUS_WORKSPACE`` (and
    # friends), so after ``eval "$(nexus env)"`` a later ``nexusd --profile
    # full`` (or ``nexusd --config full.yaml``) would otherwise spuriously
    # fail USAGE_ERROR purely from the stale environment despite passing NO
    # sandbox flag on the command line. Click's ``get_parameter_source``
    # distinguishes COMMANDLINE from ENVIRONMENT/DEFAULT, so we gate strictly
    # on COMMANDLINE — exactly mirroring the r3 stack.py pattern. An EXPLICIT
    # command-line ``--workspace``/``--hub-url``/``--hub-token`` without an
    # effective sandbox profile STILL errors (regression preserved). The
    # decision uses ``effective_profile`` (the r3/r7 ``_resolve_effective_
    # profile`` value already used by the kill-switch + the
    # ``--profile``/``--config`` conflict), NOT the raw ``deployment_profile``,
    # so a ``--config sandbox.yaml`` boot with a command-line ``--workspace``
    # stays allowed (r3 established).
    _CMDLINE = click.core.ParameterSource.COMMANDLINE
    _sandbox_flag_params = {
        "workspace": "--workspace",
        "hub_url": "--hub-url",
        "hub_token": "--hub-token",
    }
    _cmdline_sandbox_flags = [
        _flag
        for _param, _flag in _sandbox_flag_params.items()
        if ctx.get_parameter_source(_param) == _CMDLINE
    ]
    if _cmdline_sandbox_flags and effective_profile != "sandbox":
        click.echo(
            f"Error: {', '.join(_cmdline_sandbox_flags)} "
            f"{'is' if len(_cmdline_sandbox_flags) == 1 else 'are'} only valid "
            "with --profile sandbox.",
            err=True,
        )
        sys.exit(ExitCode.USAGE_ERROR)

    # --hub-url without any token is an error. Issue #4126 review r9
    # (MEDIUM): the pairing requirement holds for EVERY effective-SANDBOX boot
    # whenever the RESOLVED ``hub_url`` is non-empty (from EITHER source —
    # command line OR env ``NEXUS_HUB_URL``) and ``hub_token`` is absent. The
    # r8 fix gated this on a COMMANDLINE-sourced ``hub_url`` only, which was
    # too loose: an effective-sandbox boot with ``NEXUS_HUB_URL`` from env and
    # no token bypassed pairing → ``SandboxBootstrapper`` got ``hub_url`` with
    # ``hub_token=None`` (silent local-only degrade, or anonymous hub
    # federation if the hub accepts) — diverging from ``nexus up``, which
    # still rejects hub-url-without-token. For SANDBOX the rule is now
    # SOURCE-INDEPENDENT: ``hub_url`` present + no token → reject. The token
    # value itself may legitimately come from env (``NEXUS_HUB_TOKEN``
    # satisfies the pairing, unchanged). For NON-sandbox effective profiles
    # the pairing check stays fully disabled so a stale env ``NEXUS_HUB_URL``
    # never poisons a non-sandbox boot (r8 regression preserved); only this
    # sandbox sub-check changed — the r8 source-aware sandbox-only-FLAG
    # rejection above is unchanged.
    if effective_profile == "sandbox" and hub_url and not hub_token:
        click.echo(
            "Error: --hub-url requires a token. Pass --hub-token or set NEXUS_HUB_TOKEN.",
            err=True,
        )
        sys.exit(ExitCode.USAGE_ERROR)

    # Configure logging early
    _log_level = getattr(logging, log_level.upper())
    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonLogFormatter())
        logging.basicConfig(level=_log_level, handlers=[handler])
    else:
        logging.basicConfig(
            level=_log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    # Resolve the EFFECTIVE data dir BEFORE PID/readiness scoping (Issue
    # #4126 review r6, Finding B). The Click ``--data-dir`` option is
    # computed BEFORE ``--config`` is loaded, so a sandbox config FILE that
    # supplies ``data_dir`` (with no ``--data-dir`` flag) would otherwise
    # fall back to the shared ``~/.nexus`` globals — re-blocking concurrent
    # same-HOME sandboxes and breaking ``nexus ready --data-dir``. Mirror
    # the r3 ``_resolve_effective_profile`` pattern: replicate
    # ``load_config``/``_apply_sandbox_defaults`` precedence so this never
    # diverges from how the daemon later resolves the data dir. Returns
    # ``None`` for the legacy no-data-dir / no-config single-daemon case,
    # preserving the shared global PID/readiness back-compat unchanged.
    effective_data_dir = _resolve_effective_data_dir(data_dir, config_path, effective_profile)

    # --- PID file -----------------------------------------------------------
    # Scope the PID gate per EFFECTIVE data dir (Issue #4126 review r5,
    # Finding A; r6 Finding B now feeds the config-file-aware effective
    # value) using the SAME data dir the r4 scoped readiness uses below —
    # so two sandboxes under one HOME with distinct data dirs (whether via
    # ``--data-dir`` OR distinct ``--config`` files) BOTH pass the PID gate
    # and reach the scoped readiness write (the old single global PID gate
    # exited the second sandbox first, defeating r4). The legacy global
    # ``~/.nexus/nexusd.pid`` is still written for single-daemon
    # back-compat; the BLOCKING decision is instance-scoped.
    scoped_pid_path, legacy_pid_path = _manage_pid_file(effective_data_dir)
    # Legacy global readiness file (single-daemon default / back-compat) and
    # an ADDITIONAL data-dir-scoped one so two sandboxes under the same HOME
    # with distinct data dirs each get a deterministically-addressable,
    # non-clobbering readiness record (Issue #4126 review r4, Finding A;
    # r6 Finding B: scoped off the config-file-aware effective data dir).
    ready_path = Path.home() / ".nexus" / "nexusd.ready"
    scoped_ready_path = _scoped_readiness_path(effective_data_dir)

    # Guard: daemon cannot run in remote profile (gate on the EFFECTIVE
    # profile so a ``--config remote.yaml`` boot is rejected too).
    if effective_profile == "remote":
        if scoped_pid_path is not None:
            _remove_pid_file(scoped_pid_path)
        _remove_pid_file(legacy_pid_path)
        click.echo(
            "Error: nexusd cannot run with profile='remote'. "
            "A daemon cannot be a thin client of another daemon.",
            err=True,
        )
        sys.exit(ExitCode.CONFIG_ERROR)

    try:
        # --- Print banner ---------------------------------------------------
        click.echo("")
        click.echo("nexusd — Nexus Node Daemon")
        click.echo(f"  Host:    {host}")
        click.echo(f"  Port:    {port}")
        click.echo(f"  Profile: {effective_profile}")
        if data_dir:
            click.echo(f"  Data:    {data_dir}")
        if config_path:
            click.echo(f"  Config:  {config_path}")
        if database_url:
            click.echo(f"  DB:      {_redact_url(database_url)}")

        click.echo("")

        # --- Create local NexusFS -------------------------------------------
        try:
            import nexus

            connect_config: dict[str, object] = {"profile": deployment_profile}

            if data_dir:
                connect_config["data_dir"] = data_dir

            # Forward --database-url to NexusFS so SecretsService /
            # PasswordVaultService / ReBAC etc. get a wired record_store.
            # Previously the flag was only consumed by DatabaseAPIKeyAuth
            # below, which surprised callers who expected the obvious
            # "wire the DB" semantics.
            if database_url:
                connect_config["database_url"] = database_url

            # Respect NEXUS_ENFORCE_PERMISSIONS env var
            import os as _os

            _enforce = _os.environ.get("NEXUS_ENFORCE_PERMISSIONS", "")
            if _enforce.lower() in ("true", "1", "yes"):
                connect_config["enforce_permissions"] = True
            if connect_config.get("enforce_permissions"):
                click.echo("  Perms:   enforce=True")
            if config_path:
                from nexus.config import load_config

                config_obj = load_config(Path(config_path))
                # Issue #4237: static-auth single-key deployments need
                # allow_admin_bypass=True or the new ReBAC search filter
                # denies 100% of results. Honor explicit config-file /
                # env operator choices.
                if _should_default_admin_bypass(
                    auth_type,
                    api_key,
                    already_set="allow_admin_bypass" in config_obj.model_fields_set,
                ):
                    config_obj.allow_admin_bypass = True
                    logger.info(
                        "[#4237] static-auth single-key mode: defaulting "
                        "allow_admin_bypass=True "
                        "(override via allow_admin_bypass in config or "
                        "NEXUS_ALLOW_ADMIN_BYPASS env)",
                    )
                nx = nexus.connect(config=config_obj)
            else:
                # Issue #4237 (env-only branch): same auto-default. The
                # connect_config dict overrides $NEXUS_ALLOW_ADMIN_BYPASS in
                # load_config precedence, so we only inject when the env
                # didn't explicitly choose.
                if _should_default_admin_bypass(
                    auth_type,
                    api_key,
                    already_set="allow_admin_bypass" in connect_config,
                ):
                    connect_config["allow_admin_bypass"] = True
                    logger.info(
                        "[#4237] static-auth single-key mode: defaulting "
                        "allow_admin_bypass=True "
                        "(override via NEXUS_ALLOW_ADMIN_BYPASS env)",
                    )
                nx = nexus.connect(config=connect_config)

        except Exception as e:
            click.echo(f"Error: Failed to initialize NexusFS: {e}", err=True)
            logger.exception("NexusFS initialization failed")
            sys.exit(ExitCode.INTERNAL_ERROR)

        # --- Service lifecycle summary (Issue #1578) -------------------------
        _print_lifecycle_summary(nx)

        # --- Sandbox boot sequence (Issue #3786) ----------------------------
        # Gate on the EFFECTIVE profile (review r2): a ``--config
        # sandbox.yaml --workspace W`` boot must run the bootstrapper, and a
        # ``--profile sandbox --config full.yaml --workspace W`` boot must
        # NOT (the conflict check above already rejects that case, but the
        # effective-profile gate is the defense-in-depth invariant).
        if effective_profile == "sandbox" and workspace is not None:
            _workspace_path = Path(workspace)
            _search_registry = getattr(nx, "_search_registry", None)
            if _search_registry is None:
                _search_registry = getattr(nx, "zone_search_registry", None)
            _search_daemon = getattr(nx, "_search_daemon", None)
            if _search_daemon is None:
                _search_daemon = getattr(nx, "search_daemon", None)
            _health_state: dict[str, Any] = {"status": "indexing"}
            bootstrapper = SandboxBootstrapper(
                workspace=_workspace_path,
                hub_url=hub_url,
                hub_token=hub_token,
                nexus_fs=nx,
                search_registry=_search_registry,
                search_daemon=_search_daemon,
                health_state=_health_state,
            )
            bootstrapper.run()
            logger.info(
                "[nexusd] SandboxBootstrapper.run() complete (workspace=%s, hub=%s)",
                _workspace_path,
                hub_url or "none",
            )

        # --- Resolve auth ---------------------------------------------------
        auth_provider: Any = None
        if auth_type == "database":
            if not database_url:
                # Issue #4238: POSTGRES_URL may also use ``postgres://``.
                database_url = _norm_db_url(os.getenv("POSTGRES_URL"))
            if database_url:
                try:
                    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
                    from nexus.storage.record_store import SQLAlchemyRecordStore

                    record_store = SQLAlchemyRecordStore(database_url)
                    auth_provider = DatabaseAPIKeyAuth(record_store)
                    logger.info("Using database authentication")
                except Exception:
                    logger.warning("DatabaseAPIKeyAuth not available, falling back to static")

        # Resolve API key: explicit flag > env var (handled by Click) > key file
        if not api_key:
            key_file = os.getenv("NEXUS_API_KEY_FILE", "")
            if key_file and Path(key_file).is_file():
                api_key = Path(key_file).read_text().strip()

        # Fallback: StaticAPIKeyAuth when NEXUS_API_KEY is set but no DB auth
        if auth_provider is None and api_key:
            from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth

            static_provider = StaticAPIKeyAuth(
                {api_key: {"subject_type": "user", "subject_id": "admin", "is_admin": True}}
            )

            # Chain with DatabaseAPIKeyAuth so agent keys generated at
            # registration are also validated (Issue #3250).
            _record_store = getattr(nx, "_record_store", None) if nx else None
            logger.info(
                "Auth chain: nx=%s, _record_store=%s",
                type(nx).__name__ if nx else None,
                type(_record_store).__name__ if _record_store else None,
            )
            if _record_store is not None:
                try:
                    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
                    from nexus.server.auth.factory import _ChainedAPIKeyAuth

                    db_provider = DatabaseAPIKeyAuth(_record_store, require_expiry=False)
                    auth_provider = _ChainedAPIKeyAuth(static_provider, db_provider)
                    logger.info(
                        "Using static + database API key authentication (agent key fallback)"
                    )
                except Exception as exc:
                    auth_provider = static_provider
                    logger.warning("Auth chain fallback failed: %s", exc, exc_info=True)
            else:
                auth_provider = static_provider
                logger.info("Using static API key authentication (no database)")

        # --- Create FastAPI app + run ---------------------------------------
        from nexus.server.fastapi_server import create_app, run_server

        nx_fs: Any = nx
        app = create_app(
            nexus_fs=nx_fs,
            api_key=api_key,
            auth_provider=auth_provider,
            database_url=database_url,
        )

        # Expose health_state on app.state so the /health endpoint can read
        # it without reaching into NexusFS internals (boundary hygiene).
        # Only sandbox profile uses _health_state; other profiles get None.
        app.state.health_state = locals().get("_health_state")

        # --- Ready file -----------------------------------------------------
        # Atomic write (temp + os.replace) so a concurrent ``nexus ready``
        # under the same HOME never sees a torn file. Both the legacy global
        # path and the data-dir-scoped path carry this process's ownership
        # token (Issue #4126 review r4, Finding A).
        _write_readiness_atomic(ready_path, host, port)
        if scoped_ready_path is not None:
            _write_readiness_atomic(scoped_ready_path, host, port)

        click.echo(f"Starting nexusd on {host}:{port}")
        click.echo("Press Ctrl+C to stop")
        click.echo("")

        run_server(app, host=host, port=port, log_level=log_level, workers=workers)

    except KeyboardInterrupt:
        click.echo("\nnexusd stopped")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        logger.exception("nexusd failed")
        sys.exit(ExitCode.INTERNAL_ERROR)
    finally:
        # Ownership-aware PID cleanup mirrors the readiness contract: a
        # still-running sibling sandbox under the same HOME that re-wrote the
        # legacy global pid (last-writer-wins) keeps its double-start guard;
        # we only remove a pid file that still records THIS process (or a
        # stale/dead one) — Issue #4126 review r5, Finding A.
        if scoped_pid_path is not None:
            _remove_pid_file(scoped_pid_path)
        _remove_pid_file(legacy_pid_path)
        # Ownership-checked unlink: only remove a readiness file that still
        # holds THIS process's token. A sibling sandbox daemon under the same
        # HOME that re-wrote the legacy global path (last-writer-wins) keeps
        # its readiness record — we must never time out ``nexus ready`` for a
        # daemon that is still serving (Issue #4126 review r4, Finding A).
        _remove_readiness_if_owned(ready_path, host, port)
        if scoped_ready_path is not None:
            _remove_readiness_if_owned(scoped_ready_path, host, port)


# ---------------------------------------------------------------------------
# nexusd share — share a local subtree as a federation zone
# ---------------------------------------------------------------------------


@main.command("share")
@click.argument("path", type=str)
@click.option(
    "--zone-id",
    type=str,
    default=None,
    help="Explicit zone ID for the shared subtree (auto-generated if omitted).",
)
@click.option("--remote-url", default=None, envvar="NEXUS_URL", help="Running nexusd URL.")
@click.option("--remote-api-key", default=None, envvar="NEXUS_API_KEY", help="API key.")
def share_cmd(
    path: str,
    zone_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Share a local subtree as a federation zone.

    Tells the running nexusd to create a new zone from a local path
    so that peers can join it.

    \b
    Examples:
        nexusd share /data/shared
        nexusd share /data/shared --zone-id my-shared-zone
    """
    from nexus.cli.utils import console, rpc_call

    try:
        data = rpc_call(
            remote_url, remote_api_key, "federation_share", local_path=path, zone_id=zone_id
        )
        new_zone = data.get("zone_id", "unknown")
        console.print(f"[nexus.success]Shared '{path}' as federation zone[/nexus.success]")
        console.print(f"  Zone ID: [nexus.reference]{new_zone}[/nexus.reference]")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# nexusd join — join a peer's federation zone
# ---------------------------------------------------------------------------


@main.command("join")
@click.argument("peer_addr", type=str)
@click.argument("remote_path", type=str)
@click.argument("local_path", type=str)
@click.option("--remote-url", default=None, envvar="NEXUS_URL", help="Running nexusd URL.")
@click.option("--remote-api-key", default=None, envvar="NEXUS_API_KEY", help="API key.")
def join_cmd(
    peer_addr: str,
    remote_path: str,
    local_path: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Join a peer's federation zone.

    Tells the running nexusd to connect to a remote peer and replicate
    a shared subtree locally.

    \b
    Examples:
        nexusd join peer1:2126 /shared /local/shared
        nexusd join 10.0.0.5:2126 /data /mnt/data
    """
    from nexus.cli.utils import console, rpc_call

    try:
        data = rpc_call(
            remote_url,
            remote_api_key,
            "federation_join",
            peer_addr=peer_addr,
            remote_path=remote_path,
            local_path=local_path,
        )
        joined_zone = data.get("zone_id", "unknown")
        console.print(f"[nexus.success]Joined federation zone from {peer_addr}[/nexus.success]")
        console.print(f"  Zone ID:     [nexus.reference]{joined_zone}[/nexus.reference]")
        console.print(f"  Remote path: {remote_path}")
        console.print(f"  Local path:  {local_path}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

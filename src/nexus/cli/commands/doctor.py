"""``nexus doctor`` — comprehensive diagnostic tool.

Checks five categories: connectivity, storage, federation, security, and
dependencies.  Each check is an independent function returning a structured
:class:`CheckResult`.  Supports ``--json`` and ``--fix`` modes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import console, handle_error

# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------


class CheckStatus(Enum):
    """Severity levels for diagnostic checks."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """Structured result from a single diagnostic check."""

    name: str
    status: CheckStatus
    message: str
    fix_hint: str | None = None
    fixable: bool = False


# ---------------------------------------------------------------------------
# Individual checks — connectivity
# ---------------------------------------------------------------------------


def check_docker_available() -> CheckResult:
    """Check that the Docker CLI is installed."""
    if shutil.which("docker") is None:
        return CheckResult(
            name="docker",
            status=CheckStatus.ERROR,
            message="Docker CLI not found on PATH.",
            fix_hint="Install Docker: https://docs.docker.com/get-docker/",
        )
    return CheckResult(name="docker", status=CheckStatus.OK, message="Docker CLI found.")


def check_docker_daemon() -> CheckResult:
    """Check that the Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            if "Cannot connect" in stderr or "Is the docker daemon running" in stderr:
                return CheckResult(
                    name="docker-daemon",
                    status=CheckStatus.ERROR,
                    message="Docker daemon is not running.",
                    fix_hint="Start Docker Desktop or run: sudo systemctl start docker",
                )
            return CheckResult(
                name="docker-daemon",
                status=CheckStatus.WARNING,
                message=f"Docker info returned error: {stderr[:120]}",
            )
    except FileNotFoundError:
        return CheckResult(
            name="docker-daemon",
            status=CheckStatus.ERROR,
            message="Docker CLI not found.",
            fix_hint="Install Docker: https://docs.docker.com/get-docker/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="docker-daemon",
            status=CheckStatus.ERROR,
            message="Docker daemon timed out (10s).",
            fix_hint="Restart Docker: docker restart or restart Docker Desktop.",
        )
    return CheckResult(
        name="docker-daemon", status=CheckStatus.OK, message="Docker daemon is running."
    )


def check_server_reachable() -> CheckResult:
    """Check that the Nexus HTTP server is reachable."""
    url = os.getenv("NEXUS_URL", "http://localhost:2026")
    try:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{url}/health")
            if resp.status_code == 200:
                return CheckResult(
                    name="server-http",
                    status=CheckStatus.OK,
                    message=f"Server reachable at {url}.",
                )
            return CheckResult(
                name="server-http",
                status=CheckStatus.WARNING,
                message=f"Server returned HTTP {resp.status_code}.",
            )
    except Exception:
        return CheckResult(
            name="server-http",
            status=CheckStatus.WARNING,
            message=f"Server not reachable at {url}.",
            fix_hint="Start the server: nexusd",
        )


def check_grpc_port() -> CheckResult:
    """Check whether the gRPC port env var is configured."""
    port = os.getenv("NEXUS_GRPC_PORT", "0")
    if port == "0" or not port:
        return CheckResult(
            name="grpc-port",
            status=CheckStatus.WARNING,
            message="gRPC disabled (NEXUS_GRPC_PORT=0 or unset).",
            fix_hint="Set NEXUS_GRPC_PORT=2126 to enable gRPC.",
        )
    return CheckResult(
        name="grpc-port",
        status=CheckStatus.OK,
        message=f"gRPC configured on port {port}.",
    )


# ---------------------------------------------------------------------------
# Individual checks — storage
# ---------------------------------------------------------------------------


def check_disk_space() -> CheckResult:
    """Warn if free disk space is below 1 GB."""
    data_dir = os.getenv("NEXUS_DATA_DIR", ".")
    try:
        usage = shutil.disk_usage(data_dir)
        free_gb = usage.free / (1024**3)
        if free_gb < 1.0:
            return CheckResult(
                name="disk-space",
                status=CheckStatus.WARNING,
                message=f"Low disk space: {free_gb:.1f} GB free.",
                fix_hint="Free up disk space or move NEXUS_DATA_DIR to a larger volume.",
            )
        return CheckResult(
            name="disk-space",
            status=CheckStatus.OK,
            message=f"{free_gb:.1f} GB free.",
        )
    except OSError as exc:
        return CheckResult(
            name="disk-space",
            status=CheckStatus.WARNING,
            message=f"Could not check disk space: {exc}",
        )


def check_data_dir_writable() -> CheckResult:
    """Check that the data directory exists and is writable."""
    import nexus

    data_dir = Path(os.getenv("NEXUS_DATA_DIR", str(Path(nexus.NEXUS_STATE_DIR) / "data")))
    if not data_dir.exists():
        return CheckResult(
            name="data-dir",
            status=CheckStatus.WARNING,
            message=f"Data directory does not exist: {data_dir}",
            fix_hint=f"Create it: mkdir -p {data_dir}",
            fixable=True,
        )
    if not os.access(data_dir, os.W_OK):
        return CheckResult(
            name="data-dir",
            status=CheckStatus.ERROR,
            message=f"Data directory is not writable: {data_dir}",
            fix_hint=f"Fix permissions: chmod u+w {data_dir}",
        )
    return CheckResult(
        name="data-dir",
        status=CheckStatus.OK,
        message=f"Data directory OK: {data_dir}",
    )


# ---------------------------------------------------------------------------
# Individual checks — federation
# ---------------------------------------------------------------------------


def check_tls_certs() -> CheckResult:
    """Check whether TLS certificates are initialized."""
    data_dir = Path(os.getenv("NEXUS_DATA_DIR", "."))
    tls_dir = data_dir / "tls"
    ca_cert = tls_dir / "ca.pem"
    node_cert = tls_dir / "node.pem"

    if not tls_dir.exists():
        return CheckResult(
            name="tls-certs",
            status=CheckStatus.WARNING,
            message="TLS not initialized (no tls/ directory).",
            fix_hint="Run: nexus tls init",
            fixable=True,
        )
    if not ca_cert.exists() or not node_cert.exists():
        return CheckResult(
            name="tls-certs",
            status=CheckStatus.WARNING,
            message="TLS partially initialized (missing ca.pem or node.pem).",
            fix_hint="Run: nexus tls init",
            fixable=True,
        )
    return CheckResult(
        name="tls-certs",
        status=CheckStatus.OK,
        message="TLS certificates found.",
    )


def check_tls_expiry() -> CheckResult:
    """Warn if TLS certificates expire within 30 days."""
    data_dir = Path(os.getenv("NEXUS_DATA_DIR", "."))
    ca_cert_path = data_dir / "tls" / "ca.pem"
    if not ca_cert_path.exists():
        return CheckResult(
            name="tls-expiry",
            status=CheckStatus.WARNING,
            message="No TLS certificate to check.",
        )
    try:
        from datetime import UTC, datetime

        from nexus.security.tls.certgen import load_pem_cert

        cert = load_pem_cert(ca_cert_path)
        expires = cert.not_valid_after_utc
        days_left = (expires - datetime.now(UTC)).days
        if days_left < 0:
            return CheckResult(
                name="tls-expiry",
                status=CheckStatus.ERROR,
                message=f"CA certificate EXPIRED {abs(days_left)} days ago.",
                fix_hint="Regenerate: nexus tls init (after removing tls/ directory).",
            )
        if days_left < 30:
            return CheckResult(
                name="tls-expiry",
                status=CheckStatus.WARNING,
                message=f"CA certificate expires in {days_left} days.",
                fix_hint="Regenerate soon: nexus tls init",
            )
        return CheckResult(
            name="tls-expiry",
            status=CheckStatus.OK,
            message=f"CA certificate valid for {days_left} days.",
        )
    except Exception as exc:
        return CheckResult(
            name="tls-expiry",
            status=CheckStatus.WARNING,
            message=f"Could not check TLS expiry: {exc}",
        )


# ---------------------------------------------------------------------------
# Individual checks — security
# ---------------------------------------------------------------------------


def check_zone_isolation() -> CheckResult:
    """Alert if zone isolation is disabled."""
    env_val = os.getenv("NEXUS_ENFORCE_ZONE_ISOLATION", "").lower()
    if env_val in ("false", "0", "no", "off"):
        return CheckResult(
            name="zone-isolation",
            status=CheckStatus.WARNING,
            message="Zone isolation is DISABLED (NEXUS_ENFORCE_ZONE_ISOLATION=false).",
            fix_hint="Enable: export NEXUS_ENFORCE_ZONE_ISOLATION=true",
        )
    return CheckResult(
        name="zone-isolation",
        status=CheckStatus.OK,
        message="Zone isolation enabled (default or explicitly set).",
    )


def check_database_url() -> CheckResult:
    """Check that NEXUS_DATABASE_URL is set when database auth is expected."""
    db_url = os.getenv("NEXUS_DATABASE_URL")
    if not db_url:
        return CheckResult(
            name="database-url",
            status=CheckStatus.WARNING,
            message="NEXUS_DATABASE_URL not set.",
            fix_hint="Set it: export NEXUS_DATABASE_URL='postgresql://...'",
        )
    # Don't log the full URL (may contain password)
    return CheckResult(
        name="database-url",
        status=CheckStatus.OK,
        message="NEXUS_DATABASE_URL is configured.",
    )


# ---------------------------------------------------------------------------
# Individual checks — dependencies
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    """Verify Python >= 3.12."""
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 12):
        return CheckResult(
            name="python-version",
            status=CheckStatus.ERROR,
            message=f"Python {major}.{minor} detected; 3.12+ required.",
            fix_hint="Install Python 3.12 or later.",
        )
    return CheckResult(
        name="python-version",
        status=CheckStatus.OK,
        message=f"Python {major}.{minor}.",
    )


def check_docker_compose_version() -> CheckResult:
    """Check that ``docker compose`` (v2) is available."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return CheckResult(
                name="compose-version",
                status=CheckStatus.ERROR,
                message="docker compose v2 not available.",
                fix_hint="Update Docker or install Docker Compose v2.",
            )
        version = result.stdout.strip()
        return CheckResult(
            name="compose-version",
            status=CheckStatus.OK,
            message=f"Docker Compose {version}.",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult(
            name="compose-version",
            status=CheckStatus.ERROR,
            message="docker compose not found.",
            fix_hint="Install Docker with Compose v2.",
        )


def check_pgvector() -> CheckResult:
    """Check if pgvector extension is likely available (via Docker image)."""
    # We can only check environment hints since we may not have a DB connection
    db_url = os.getenv("NEXUS_DATABASE_URL", "")
    if not db_url:
        return CheckResult(
            name="pgvector",
            status=CheckStatus.WARNING,
            message="Cannot verify pgvector (no database URL configured).",
        )
    return CheckResult(
        name="pgvector",
        status=CheckStatus.OK,
        message="Database URL configured (pgvector verified at connection time).",
    )


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CHECKS: dict[str, list[Any]] = {
    "connectivity": [
        check_docker_available,
        check_docker_daemon,
        check_server_reachable,
        check_grpc_port,
    ],
    "storage": [
        check_disk_space,
        check_data_dir_writable,
    ],
    "federation": [
        check_tls_certs,
        check_tls_expiry,
    ],
    "security": [
        check_zone_isolation,
        check_database_url,
    ],
    "dependencies": [
        check_python_version,
        check_docker_compose_version,
        check_pgvector,
    ],
}


# ---------------------------------------------------------------------------
# Auto-fix support
# ---------------------------------------------------------------------------


def _try_fix(result: CheckResult) -> CheckResult | None:
    """Attempt to auto-fix a failing check.  Returns new result or None."""
    if result.name == "data-dir" and result.fixable:
        import nexus

        data_dir = Path(os.getenv("NEXUS_DATA_DIR", str(Path(nexus.NEXUS_STATE_DIR) / "data")))
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            return CheckResult(
                name=result.name,
                status=CheckStatus.OK,
                message=f"Created data directory: {data_dir}",
            )
        except OSError as exc:
            return CheckResult(
                name=result.name,
                status=CheckStatus.ERROR,
                message=f"Failed to create {data_dir}: {exc}",
            )
    if result.name == "tls-certs" and result.fixable:
        try:
            from nexus.security.tls.certgen import (
                generate_node_cert,
                generate_zone_ca,
                save_pem,
            )

            data_dir = Path(os.getenv("NEXUS_DATA_DIR", "."))
            tls_dir = data_dir / "tls"
            ca_cert, ca_key = generate_zone_ca("default")
            save_pem(tls_dir / "ca.pem", ca_cert)
            save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)
            node_cert, node_key = generate_node_cert(1, "default", ca_cert, ca_key)
            save_pem(tls_dir / "node.pem", node_cert)
            save_pem(tls_dir / "node-key.pem", node_key, is_private=True)
            return CheckResult(
                name=result.name,
                status=CheckStatus.OK,
                message=f"Generated TLS certificates in {tls_dir}.",
            )
        except Exception as exc:
            return CheckResult(
                name=result.name,
                status=CheckStatus.ERROR,
                message=f"Failed to generate TLS certs: {exc}",
            )
    return None


# ---------------------------------------------------------------------------
# Runner and display
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    CheckStatus.OK: "[green]ok[/green]",
    CheckStatus.WARNING: "[yellow]warning[/yellow]",
    CheckStatus.ERROR: "[red]ERROR[/red]",
}


async def _run_all_checks_async(fix: bool = False) -> dict[str, list[CheckResult]]:
    """Execute all checks concurrently across categories."""

    async def _run_category(
        category: str,
        checks: list[Any],
    ) -> tuple[str, list[CheckResult]]:
        tasks = [asyncio.to_thread(fn) for fn in checks]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        category_results: list[CheckResult] = []
        for i, result in enumerate(raw_results):
            if isinstance(result, BaseException):
                result = CheckResult(
                    name=checks[i].__name__.removeprefix("check_"),
                    status=CheckStatus.ERROR,
                    message=f"Check failed unexpectedly: {result}",
                )
            if fix and result.status != CheckStatus.OK and result.fixable:
                fixed = _try_fix(result)
                if fixed is not None:
                    result = fixed
            category_results.append(result)
        return (category, category_results)

    tasks = [_run_category(cat, fns) for cat, fns in CHECKS.items()]
    pairs = await asyncio.gather(*tasks)
    return dict(pairs)


def _display_results(results: dict[str, list[CheckResult]]) -> int:
    """Print results to console.  Returns exit code (1 if any errors)."""
    has_error = False
    for category, checks in results.items():
        console.print(f"\n[bold]{category.title()}[/bold]")
        for check in checks:
            icon = _STATUS_ICONS[check.status]
            console.print(f"  {icon}  {check.name}: {check.message}")
            if check.fix_hint and check.status != CheckStatus.OK:
                console.print(f"       [dim]Fix: {check.fix_hint}[/dim]")
            if check.status == CheckStatus.ERROR:
                has_error = True

    # Summary
    total = sum(len(v) for v in results.values())
    ok_count = sum(1 for checks in results.values() for c in checks if c.status == CheckStatus.OK)
    warn_count = sum(
        1 for checks in results.values() for c in checks if c.status == CheckStatus.WARNING
    )
    err_count = sum(
        1 for checks in results.values() for c in checks if c.status == CheckStatus.ERROR
    )
    console.print()
    console.print(
        f"[bold]{total} checks:[/bold] "
        f"[green]{ok_count} ok[/green], "
        f"[yellow]{warn_count} warnings[/yellow], "
        f"[red]{err_count} errors[/red]"
    )
    return 1 if has_error else 0


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(name="doctor")
@click.option("--fix", "auto_fix", is_flag=True, help="Attempt to auto-fix issues.")
@add_output_options
def doctor(output_opts: OutputOptions, auto_fix: bool) -> None:
    """Run diagnostic checks on your Nexus environment.

    Checks connectivity, storage, federation, security, and dependencies.

    Examples:
        nexus doctor
        nexus doctor --json
        nexus doctor --fix
    """
    try:
        timing = CommandTiming()
        with timing.phase("checks"):
            results = asyncio.run(_run_all_checks_async(fix=auto_fix))

        # Serialize CheckResults for JSON output
        serializable = {cat: [asdict(c) for c in checks] for cat, checks in results.items()}
        for checks in serializable.values():
            for c in checks:
                c["status"] = c["status"].value

        def _human_display(_data: dict[str, list[dict[str, Any]]]) -> None:  # noqa: ARG002
            exit_code = _display_results(results)
            if exit_code:
                sys.exit(exit_code)

        render_output(
            data=serializable,
            output_opts=output_opts,
            timing=timing,
            human_formatter=_human_display,
        )

        # For JSON mode, exit with error if any checks failed
        if output_opts.json_output:
            has_error = any(
                c.status == CheckStatus.ERROR for checks in results.values() for c in checks
            )
            if has_error:
                sys.exit(1)
    except Exception as exc:
        handle_error(exc)


def register_commands(cli: click.Group) -> None:
    """Register doctor command."""
    cli.add_command(doctor)

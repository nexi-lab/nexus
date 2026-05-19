"""``nexus ready`` — probe daemon readiness for sandbox-profile deployments.

Unlike ``nexus status`` (which is Docker/compose-oriented), this command
answers a single question: *is the local nexusd up and serving?* It does so
by waiting for the daemon's readiness file, then polling ``/health`` until it
returns 200, all within one total time budget. Designed for boot scripts and
CI gates where a clean exit code matters.

Exit codes (sysexits.h):
    0  — ready
    75 — TEMPFAIL: timed out waiting for readiness file or /health
    65 — DATA_ERROR: readiness file present but malformed
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import click

from nexus.cli.exit_codes import ExitCode
from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.state import normalize_connect_host
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import handle_error

_FILE_POLL_INTERVAL = 0.25
_HEALTH_POLL_INTERVAL = 0.5


def _default_readiness_file() -> Path:
    return Path.home() / ".nexus" / "nexusd.ready"


def _wait_for_file(path: Path, deadline: float) -> bool:
    """Poll until *path* exists or the *deadline* (monotonic) elapses."""
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(_FILE_POLL_INTERVAL)
    return path.exists()


def _parse_endpoint(content: str) -> tuple[str, int] | None:
    """Parse a ``host:port`` readiness line; None if malformed."""
    content = content.strip()
    if not content:
        return None
    host, sep, port_s = content.rpartition(":")
    if not sep or not host:
        return None
    try:
        port = int(port_s)
    except ValueError:
        return None
    return host, port


def _poll_health(base_url: str, deadline: float) -> bool:
    """Poll ``GET {base_url}/health`` until 200 or the deadline elapses."""
    import httpx

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with httpx.Client(timeout=min(2.0, remaining)) as client:
                resp = client.get(f"{base_url}/health")
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(_HEALTH_POLL_INTERVAL)
    return False


def _fetch_features(base_url: str, deadline: float) -> dict[str, Any] | None:
    """Best-effort fetch of ``/api/v2/features``; None on any failure.

    Deadline-aware: if the total budget is already spent, skip the call
    entirely rather than letting a stalled server overrun ``--timeout``.
    """
    import httpx

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None

    try:
        with httpx.Client(timeout=min(2.0, remaining)) as client:
            resp = client.get(f"{base_url}/api/v2/features")
        if resp.status_code == 200:
            payload: dict[str, Any] = resp.json()
            return payload
    except Exception:
        return None
    return None


def _render_ready(data: dict[str, Any]) -> None:
    """Human output: a small table summarising readiness."""
    from rich.table import Table

    if data.get("ready"):
        table = Table(title="Nexus Daemon Readiness")
        table.add_column("Field", style="nexus.value", no_wrap=True)
        table.add_column("Value")
        table.add_row("ready", "[nexus.success]yes[/nexus.success]")
        table.add_row("endpoint", str(data.get("endpoint", "")))
        table.add_row("health", str(data.get("health", "")))
        table.add_row("profile", str(data.get("profile") or "—"))
        bricks = data.get("enabled_bricks") or []
        table.add_row("enabled_bricks", ", ".join(bricks) if bricks else "—")
        console.print(table)
    else:
        console.print()
        console.print(
            f"[nexus.error]Nexus is not ready:[/nexus.error] {data.get('reason', 'unknown')}"
        )
        endpoint = data.get("endpoint")
        if endpoint:
            console.print(f"  endpoint: {endpoint}")
        console.print()


@click.command(name="ready")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=60.0,
    help="Total seconds to wait for the readiness file and /health (default: 60).",
)
@click.option(
    "--readiness-file",
    "readiness_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the daemon readiness file (default: ~/.nexus/nexusd.ready).",
)
@add_output_options
def ready(
    output_opts: OutputOptions,
    timeout: float,
    readiness_file: Path | None,
) -> None:
    """Probe daemon readiness for non-Docker (sandbox-profile) deployments.

    Waits for the daemon readiness file, then polls ``/health`` until it
    returns 200, all within a single ``--timeout`` budget. Intended for boot
    scripts and CI gates.

    Examples:
        nexus ready                 # wait up to 60s, print a table
        nexus ready --json          # machine-readable
        nexus ready --timeout 30    # wait up to 30s
    """
    path = readiness_file if readiness_file is not None else _default_readiness_file()

    try:
        timing = CommandTiming()
        deadline = time.monotonic() + timeout

        # (1) Wait for the readiness file to appear.
        with timing.phase("readiness-file"):
            if not _wait_for_file(path, deadline):
                render_output(
                    data={
                        "ready": False,
                        "reason": "timeout waiting for readiness file",
                        "endpoint": None,
                    },
                    output_opts=output_opts,
                    timing=timing,
                    human_formatter=_render_ready,
                )
                sys.exit(ExitCode.TEMPFAIL)

        # (2) Parse the host:port endpoint.
        endpoint = _parse_endpoint(path.read_text())
        if endpoint is None:
            render_output(
                data={
                    "ready": False,
                    "reason": "malformed readiness file",
                    "endpoint": None,
                },
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render_ready,
            )
            sys.exit(ExitCode.DATA_ERROR)

        host, port = endpoint
        # The readiness file records the daemon's *bind* host, whose default
        # is the wildcard ``0.0.0.0`` — not connectable. Normalize to a
        # loopback address before polling, using the same SSOT helper as
        # ``resolve_connection_env`` so ``ready`` and ``eval $(nexus env)``
        # agree on the host (Issue #4126 review r1 / #4144).
        host = normalize_connect_host(host)
        endpoint_str = f"{host}:{port}"
        base_url = f"http://{host}:{port}"

        # (3) Poll /health until 200 within the remaining budget.
        with timing.phase("health"):
            if not _poll_health(base_url, deadline):
                render_output(
                    data={
                        "ready": False,
                        "reason": "health endpoint not ready",
                        "endpoint": endpoint_str,
                    },
                    output_opts=output_opts,
                    timing=timing,
                    human_formatter=_render_ready,
                )
                sys.exit(ExitCode.TEMPFAIL)

        # (4) Best-effort feature probe (profile + bricks).
        features = _fetch_features(base_url, deadline) or {}

        # (5) Success.
        render_output(
            data={
                "ready": True,
                "endpoint": endpoint_str,
                "health": "healthy",
                "profile": features.get("profile"),
                "enabled_bricks": features.get("enabled_bricks") or [],
            },
            output_opts=output_opts,
            timing=timing,
            human_formatter=_render_ready,
        )
        sys.exit(ExitCode.SUCCESS)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        handle_error(exc)


def register_commands(cli: click.Group) -> None:
    """Register ready command."""
    cli.add_command(ready)

"""``nexus status`` — service health overview.

Displays a Rich table of service health, latency, and connection details.
Supports ``--json`` for machine-readable output and ``--watch`` for
auto-refresh every 2 seconds.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import console, handle_error

if TYPE_CHECKING:
    from rich.table import Table


# ---------------------------------------------------------------------------
# Project config helpers
# ---------------------------------------------------------------------------


def _load_project_config_optional() -> dict[str, Any]:
    """Try loading nexus.yaml; return empty dict if not found."""
    from nexus.cli.state import load_project_config_optional

    return load_project_config_optional()


def _enrich_with_image_info(data: dict[str, Any]) -> dict[str, Any]:
    """Add the *effective* image_ref into *data*.

    Uses the same precedence logic as ``nexus up`` (env vars > config ref >
    deprecated config tag) so ``nexus status`` always shows the image that
    would actually run, not just the raw config value.
    """
    from nexus.cli.commands.stack import _resolve_image_ref_from_config

    project_cfg = _load_project_config_optional()
    if project_cfg:
        data["image_ref"] = _resolve_image_ref_from_config(project_cfg)
        data["image_channel"] = project_cfg.get("image_channel", "")
        data["image_accelerator"] = project_cfg.get("image_accelerator", "")
    return data


# ---------------------------------------------------------------------------
# Health data collection
# ---------------------------------------------------------------------------


def _server_health(
    base_url: str, api_key: str | None = None, timeout: float = 1.5
) -> dict[str, Any] | None:
    """Query the running server's ``/health/detailed`` endpoint.

    Returns the JSON payload or *None* if the server is unreachable.
    Falls back to the public ``/health`` endpoint when the detailed
    endpoint requires authentication and no *api_key* is provided.
    """
    try:
        import httpx

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=timeout, headers=headers) as client:
            resp = client.get(f"{base_url}/health/detailed")
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            # Detailed endpoint may require admin auth — fall back to
            # the public /health endpoint so we still report "healthy".
            if resp.status_code in (401, 403):
                fallback = client.get(f"{base_url}/health")
                if fallback.status_code == 200:
                    fb_result: dict[str, Any] = fallback.json()
                    return fb_result
            return {"status": "error", "http_status": resp.status_code}
    except Exception:
        return None


def _docker_services(profiles: list[str] | None = None) -> list[dict[str, str]]:
    """Return container status via ``docker compose ps``.

    Returns an empty list if Docker is unavailable or compose file is missing.
    """
    try:
        from nexus.cli.compose import ComposeRunner

        runner = ComposeRunner()
        result: list[dict[str, str]] = runner.ps(profiles=profiles)
        return result
    except Exception:
        return []


async def _collect_status_async(
    server_url: str,
    api_key: str | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Collect all status data concurrently."""
    health_task = asyncio.to_thread(_server_health, server_url, api_key)
    docker_task = asyncio.to_thread(_docker_services, profiles)
    health, docker = await asyncio.gather(health_task, docker_task)
    return {
        "server_url": server_url,
        "server_reachable": health is not None,
        "server_health": health,
        "docker_services": docker,
    }


def _collect_status(
    server_url: str,
    api_key: str | None = None,
    profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Collect all status data (dual-path: server health + Docker state)."""
    return asyncio.run(_collect_status_async(server_url, api_key, profiles))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _build_table(data: dict[str, Any]) -> Table:
    """Build a Rich Table object from status data."""
    from rich.table import Table as RichTable

    table = RichTable(title="Nexus Service Status")
    table.add_column("Service", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Health")
    table.add_column("Details", style="dim")

    # Image row (from nexus.yaml project config)
    image_ref = data.get("image_ref", "")
    if image_ref:
        channel = data.get("image_channel", "")
        accelerator = data.get("image_accelerator", "")
        detail_parts = [p for p in (channel, accelerator) if p]
        table.add_row("image", image_ref, "", ", ".join(detail_parts) if detail_parts else "")

    # Server row
    if data["server_reachable"]:
        health = data["server_health"] or {}
        status_str = "[green]running[/green]"
        health_status = health.get("status", "unknown")
        if health_status in ("healthy", "ok"):
            health_str = f"[green]{health_status}[/green]"
        elif health_status == "error":
            health_str = f"[red]{health_status}[/red]"
        else:
            health_str = f"[yellow]{health_status}[/yellow]"
        components = health.get("components", {})
        details_parts: list[str] = []
        for name, info in components.items():
            if isinstance(info, dict):
                comp_status = info.get("status", "unknown")
                if comp_status in ("healthy", "disabled"):
                    continue
                details_parts.append(f"{name}={comp_status}")
        detail = ", ".join(details_parts) if details_parts else "all components ok"
    else:
        status_str = "[red]unreachable[/red]"
        health_str = "[red]--[/red]"
        detail = data["server_url"]

    table.add_row("nexus-server (HTTP)", status_str, health_str, detail)

    # Docker service rows
    for svc in data["docker_services"]:
        name = svc.get("Name", svc.get("Service", "unknown"))
        state = svc.get("State", svc.get("Status", "unknown"))
        health_val = svc.get("Health", "")

        if state == "running":
            s = "[green]running[/green]"
        elif state == "exited":
            s = "[red]exited[/red]"
        else:
            s = f"[yellow]{state}[/yellow]"

        if health_val == "healthy":
            h = "[green]healthy[/green]"
        elif health_val:
            h = f"[yellow]{health_val}[/yellow]"
        else:
            h = "[dim]--[/dim]"

        ports = svc.get("Publishers", svc.get("Ports", ""))
        port_detail = ""
        if isinstance(ports, list):
            published = [
                str(p.get("PublishedPort", "")) for p in ports if p.get("PublishedPort", 0) > 0
            ]
            port_detail = ", ".join(published)
        elif isinstance(ports, str):
            port_detail = ports

        table.add_row(name, s, h, port_detail)

    if not data["docker_services"] and not data["server_reachable"]:
        table.add_row("[dim]no services detected[/dim]", "", "", "")

    return table


def _render_table(data: dict[str, Any]) -> None:
    """Print a Rich table summarising service status."""
    console.print(_build_table(data))


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(name="status")
@click.option(
    "--watch",
    is_flag=True,
    help="Auto-refresh every 2 seconds.",
)
@click.option(
    "--url",
    "--remote-url",
    default=None,
    envvar="NEXUS_URL",
    help="Server URL to check (default: http://localhost:2026).",
)
@click.option(
    "--remote-api-key",
    default=None,
    envvar="NEXUS_API_KEY",
    hidden=True,
    help="API key for authenticated health checks.",
)
@click.option(
    "--profile",
    "profiles",
    multiple=True,
    default=(),
    help="Compose profiles to include in Docker status.",
)
@add_output_options
def status(
    output_opts: OutputOptions,
    watch: bool,
    url: str | None,
    remote_api_key: str | None,
    profiles: tuple[str, ...],
) -> None:
    """Display Nexus service health, latency, and connection details.

    Examples:
        nexus status               # Rich table
        nexus status --json        # machine-readable
        nexus status --watch       # auto-refresh every 2s
    """
    # Resolve server URL from state.json / nexus.yaml / default
    if url:
        server_url = url
    else:
        cfg = _load_project_config_optional()
        if cfg:
            from nexus.cli.state import load_runtime_state

            data_dir = cfg.get("data_dir", "./nexus-data")
            state = load_runtime_state(data_dir)
            ports = state.get("ports", cfg.get("ports", {}))
            http_port = ports.get("http", 2026)
            server_url = f"http://localhost:{http_port}"
        else:
            server_url = "http://localhost:2026"
    profile_list = list(profiles) if profiles else None

    try:
        if watch and not output_opts.json_output:
            _watch_loop(server_url, remote_api_key, profile_list)
        else:
            timing = CommandTiming()
            with timing.phase("collect"):
                data = _enrich_with_image_info(
                    _collect_status(server_url, remote_api_key, profile_list)
                )
            render_output(
                data=data,
                output_opts=output_opts,
                timing=timing,
                human_formatter=_render_table,
            )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        handle_error(exc)


def _watch_loop(server_url: str, api_key: str | None, profiles: list[str] | None) -> None:
    """Continuously refresh the status table every 2 seconds."""
    from rich.live import Live

    data = _enrich_with_image_info(_collect_status(server_url, api_key, profiles))

    with Live(_build_table(data), refresh_per_second=1, console=console) as live:
        while True:
            time.sleep(2)
            live.update(
                _build_table(
                    _enrich_with_image_info(_collect_status(server_url, api_key, profiles))
                )
            )


def register_commands(cli: click.Group) -> None:
    """Register status command."""
    cli.add_command(status)

"""``nexus status`` — service health overview.

Displays a Rich table of service health, latency, and connection details.
Supports ``--json`` for machine-readable output and ``--watch`` for
auto-refresh every 2 seconds.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any

import click

from nexus.cli.output import OutputOptions, add_output_options, render_output
from nexus.cli.theme import console
from nexus.cli.timing import CommandTiming
from nexus.cli.utils import handle_error

if TYPE_CHECKING:
    from rich.table import Table


# ---------------------------------------------------------------------------
# Project config helpers
# ---------------------------------------------------------------------------


def _load_project_config_optional() -> dict[str, Any]:
    """Try loading nexus.yaml; return empty dict if not found."""
    from nexus.cli.state import load_project_config_optional

    return load_project_config_optional()


def _fetch_deployment_profile_from_features(server_url: str) -> str | None:
    """Best-effort fetch of *profile* from ``GET /api/v2/features``.

    Returns the profile string on success, or *None* on any failure.
    Never raises; uses a short timeout so ``nexus status`` stays responsive
    even when the server is offline.
    """
    try:
        from nexus.cli.api_client import NexusApiClient

        features = NexusApiClient(url=server_url, timeout=2.0).get("/api/v2/features")
        if isinstance(features, dict):
            profile = features.get("profile")
            if profile and isinstance(profile, str):
                return profile
    except Exception:
        pass
    return None


def _resolve_deployment_profile(server_url: str) -> str:
    """Resolve the *running hub's* deployment profile.

    `nexus status` reports the hub at *server_url*, so the live hub's
    ``/api/v2/features`` value is authoritative and MUST win over a local
    ``NEXUS_PROFILE`` env var (otherwise ``NEXUS_PROFILE=full nexus status
    --url <other-hub>`` would mislabel a different hub).

    Hierarchy (single source of truth):
    1. Live ``GET /api/v2/features`` for *server_url* (authoritative when
       a server URL is available and reachable).
    2. ``NEXUS_PROFILE`` env var — offline/local fallback only (used when
       there is no server URL or the hub is unreachable).
    3. ``"unknown"`` fallback.
    """
    fetched = _fetch_deployment_profile_from_features(server_url) if server_url else None
    if fetched:
        return fetched
    profile_env = os.environ.get("NEXUS_PROFILE", "").strip()
    if profile_env:
        return profile_env
    return "unknown"


def _same_endpoint(a: str, b: str) -> bool:
    """True if two URLs point at the same scheme://host:port (path/trailing
    slash ignored). Used to decide whether a status target is the local
    stack (so local nexus.yaml auth applies) or a different remote hub."""
    if not a or not b:
        return False
    from urllib.parse import urlparse

    pa, pb = urlparse(a.rstrip("/")), urlparse(b.rstrip("/"))
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _enrich_with_image_info(data: dict[str, Any]) -> dict[str, Any]:
    """Add the *effective* image_ref, connection env, and project info into *data*.

    Uses the same precedence logic as ``nexus up`` (env vars > config ref >
    deprecated config tag) so ``nexus status`` always shows the image that
    would actually run, not just the raw config value.

    Also adds two new additive keys (Gap 2 of #4132):
    - ``auth_mode``: from project config's ``auth`` key (default ``"none"``).
    - ``deployment_profile``: resolved via env ``NEXUS_PROFILE`` →
      best-effort ``GET /api/v2/features`` → ``"unknown"``.
    """
    from nexus.cli.commands.stack import _resolve_image_ref_from_config
    from nexus.cli.state import load_runtime_state, resolve_connection_env

    project_cfg = _load_project_config_optional()
    if project_cfg:
        data["image_ref"] = _resolve_image_ref_from_config(project_cfg)
        data["image_channel"] = project_cfg.get("image_channel", "")
        data["image_accelerator"] = project_cfg.get("image_accelerator", "")

        # Add connection env vars and project metadata
        data_dir = project_cfg.get("data_dir", "./nexus-data")
        state = load_runtime_state(data_dir)
        conn_env = resolve_connection_env(project_cfg, state)
        data["connection_env"] = conn_env
        data["project_name"] = state.get("project_name", "")
        data["data_dir"] = data_dir

        # `nexus status` reports the hub at the *effective status target*
        # (`data["server_url"]`, which honors an explicit --url). The local
        # nexus.yaml only describes the locally-managed stack, so its
        # `auth` and the local stack URL must NOT be reported for a
        # different remote target.
        target_url = data.get("server_url", "")
        local_stack_url = conn_env.get("NEXUS_URL", "")
        is_local_stack = (not target_url) or _same_endpoint(target_url, local_stack_url)

        # auth_mode: local nexus.yaml auth only when the target IS the
        # locally-managed stack; otherwise it does not describe that hub.
        data["auth_mode"] = project_cfg.get("auth", "none") if is_local_stack else "unknown"

        # deployment_profile: resolved against the actual status target
        # (features-first; env is offline fallback only).
        data["deployment_profile"] = _resolve_deployment_profile(target_url or local_stack_url)
    else:
        # No project config: auth defaults to "none"
        data["auth_mode"] = "none"

        # deployment_profile: single hierarchy via helper
        data["deployment_profile"] = _resolve_deployment_profile(data.get("server_url", ""))

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

    def public_health(client: Any) -> dict[str, Any] | None:
        try:
            fallback = client.get(f"{base_url}/health")
        except Exception:
            return None
        if fallback.status_code == 200:
            fb_result: dict[str, Any] = fallback.json()
            return fb_result
        return {"status": "error", "http_status": fallback.status_code}

    try:
        import httpx

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        with httpx.Client(timeout=timeout, headers=headers) as client:
            if not api_key:
                return public_health(client)

            try:
                resp = client.get(f"{base_url}/health/detailed")
            except (httpx.TimeoutException, httpx.RequestError):
                return public_health(client)
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            # Detailed endpoint may require admin auth — fall back to
            # the public /health endpoint so we still report "healthy".
            if resp.status_code in (401, 403):
                return public_health(client)
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
    table.add_column("Service", style="nexus.value", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Health")
    table.add_column("Details", style="nexus.muted")

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
        status_str = "[nexus.success]running[/nexus.success]"
        health_status = health.get("status", "unknown")
        if health_status in ("healthy", "ok"):
            health_str = f"[nexus.success]{health_status}[/nexus.success]"
        elif health_status == "error":
            health_str = f"[nexus.error]{health_status}[/nexus.error]"
        else:
            health_str = f"[nexus.warning]{health_status}[/nexus.warning]"
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
        status_str = "[nexus.error]unreachable[/nexus.error]"
        health_str = "[nexus.error]--[/nexus.error]"
        detail = data["server_url"]

    table.add_row("nexus-server (HTTP)", status_str, health_str, detail)

    # Docker service rows
    for svc in data["docker_services"]:
        name = svc.get("Name", svc.get("Service", "unknown"))
        state = svc.get("State", svc.get("Status", "unknown"))
        health_val = svc.get("Health", "")

        if state == "running":
            s = "[nexus.success]running[/nexus.success]"
        elif state == "exited":
            s = "[nexus.error]exited[/nexus.error]"
        else:
            s = f"[nexus.warning]{state}[/nexus.warning]"

        if health_val == "healthy":
            h = "[nexus.success]healthy[/nexus.success]"
        elif health_val:
            h = f"[nexus.warning]{health_val}[/nexus.warning]"
        else:
            h = "[nexus.muted]--[/nexus.muted]"

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
        table.add_row("[nexus.muted]no services detected[/nexus.muted]", "", "", "")

    return table


def _render_table(data: dict[str, Any]) -> None:
    """Print a Rich table summarising service status, plus connection info."""
    is_running = data["server_reachable"] or bool(data["docker_services"])

    if not is_running:
        console.print()
        console.print("[nexus.warning]Nexus is not running.[/nexus.warning]")
        console.print("  Run `nexus up` to start the stack.")
        console.print()
        return

    console.print(_build_table(data))

    # Connection info from state.json / nexus.yaml
    conn_env = data.get("connection_env")
    if conn_env:
        console.print()
        console.print("[bold]Connection:[/bold]")
        for key, value in sorted(conn_env.items()):
            console.print(f"  export {key}='{value}'")

    # Project metadata
    project_name = data.get("project_name", "")
    data_dir = data.get("data_dir", "")
    if project_name or data_dir:
        console.print()
        console.print("[bold]Project:[/bold]")
        if project_name:
            console.print(f"  Name:     {project_name}")
        if data_dir:
            console.print(f"  Data dir: {data_dir}")


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
        from nexus.cli.state import load_runtime_state, resolve_connection_env

        cfg = _load_project_config_optional()
        if cfg:
            data_dir = cfg.get("data_dir", "./nexus-data")
            state = load_runtime_state(data_dir)
            conn = resolve_connection_env(cfg, state)
            # resolve_connection_env handles http vs https based on TLS state
            server_url = conn.get("NEXUS_URL", "http://localhost:2026")
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

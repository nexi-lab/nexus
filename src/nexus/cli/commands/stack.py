"""Stack lifecycle commands — up, down, logs, restart, upgrade, stop, start.

These commands manage the Docker Compose stack for ``shared`` and ``demo``
presets.  They wrap ``docker compose`` via subprocess, adding pre-flight
port conflict detection, parallel health polling, and rich status output.

Runtime state (resolved ports, API key, image used) is written to
``{data_dir}/.state.json`` — **not** back to ``nexus.yaml`` — so that the
declarative config stays clean and concurrent worktrees don't collide.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.commands.init_cmd import (
    ADDON_PROFILE_MAP,
    DEFAULT_IMAGE_REGISTRY,
    _resolve_image_ref,
)
from nexus.cli.port_utils import (
    VALID_STRATEGIES,
    check_port_available,
    resolve_ports,
)
from nexus.cli.state import (
    load_project_config as _load_project_config,
)
from nexus.cli.state import (
    load_project_config_optional as _load_project_config_optional,
)
from nexus.cli.state import (
    load_runtime_state,
    resolve_connection_env,
    save_runtime_state,
)
from nexus.cli.state import (
    save_project_config as _save_project_config,
)
from nexus.cli.theme import console


def _resolve_image_ref_from_config(config: dict[str, Any]) -> str:
    """Resolve the effective image reference from config + env overrides.

    Precedence (highest to lowest):
      1. NEXUS_IMAGE_REF environment variable
      2. config["image_ref"]
      3. NEXUS_IMAGE_TAG environment variable (deprecated compat)
      4. config["image_tag"] (deprecated compat — maps to full ref)
      5. Empty string (no image pinning)
    """
    # New model: NEXUS_IMAGE_REF env var wins
    env_ref = os.environ.get("NEXUS_IMAGE_REF", "")
    if env_ref:
        return env_ref

    # Config image_ref (set by nexus init)
    config_ref = config.get("image_ref", "")
    if config_ref:
        return config_ref

    # Deprecated: NEXUS_IMAGE_TAG env var → expand to full ref
    env_tag = os.environ.get("NEXUS_IMAGE_TAG", "")
    if env_tag:
        return f"{DEFAULT_IMAGE_REGISTRY}:{env_tag}"

    # Deprecated: config image_tag → expand to full ref
    config_tag = config.get("image_tag", "")
    if config_tag:
        return f"{DEFAULT_IMAGE_REGISTRY}:{config_tag}"

    return ""


def _derive_project_env(
    config: dict[str, Any],
    resolved_ports: dict[str, int] | None = None,
) -> dict[str, str]:
    """Build the compose environment from nexus.yaml config.

    Returns a dict with COMPOSE_PROJECT_NAME, NEXUS_HOST_DATA_DIR,
    port vars, auth type, image ref, and TLS settings — everything
    compose commands need to target the correct project.

    When *resolved_ports* is provided (e.g. after conflict resolution),
    those values are used instead of config["ports"].
    """
    data_dir = str(Path(config.get("data_dir", "./nexus-data")).resolve())
    project_hash = hashlib.md5(data_dir.encode()).hexdigest()[:8]
    project_name = f"nexus-{project_hash}"

    ports = resolved_ports or config.get("ports", {})
    env: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": project_name,
        "NEXUS_PORT": str(ports.get("http", 2026)),
        "NEXUS_GRPC_PORT": str(ports.get("grpc", 2028)),
        "POSTGRES_PORT": str(ports.get("postgres", 5432)),
        "DRAGONFLY_PORT": str(ports.get("dragonfly", 6379)),
        "NEXUS_HOST_DATA_DIR": data_dir,
        "NEXUS_ADMIN_USER": str(config.get("admin_user", "admin")),
        "NEXUS_AUTH_TYPE": config.get("auth", "none"),
    }

    # Pass the API key to the container so the production entrypoint
    # can register it without generating a new one.
    api_key = config.get("api_key", "")
    if api_key:
        env["NEXUS_API_KEY"] = api_key

    # Forward optional search embedding env so Docker stacks can use
    # provider-backed txtai embeddings without local model warmup.
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "NEXUS_TXTAI_MODEL",
        "NEXUS_TXTAI_USE_API_EMBEDDINGS",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            env[key] = value

    # Resolve the image reference (supports new image_ref and deprecated image_tag)
    image_ref = _resolve_image_ref_from_config(config)
    if image_ref:
        env["NEXUS_IMAGE_REF"] = image_ref

    # TLS is provisioned automatically by 2-phase TLS bootstrap.
    # Certs are auto-detected from disk inside the container.
    # NEXUS_GRPC_TLS is set explicitly so the gRPC server doesn't
    # rely on auto-detection, which can surprise users.
    if config.get("tls"):
        env["NEXUS_GRPC_TLS"] = "true"
        env.pop("NEXUS_GRPC_BIND_ALL", None)
    else:
        env["NEXUS_GRPC_TLS"] = "false"
        # Standalone demo/shared stacks expose gRPC through a published Docker
        # port, so the server must not stay bound to container loopback.
        env["NEXUS_GRPC_BIND_ALL"] = "true"

    return env


def _docker_build_args(extra_env: dict[str, str]) -> list[str]:
    """Return build args for local Docker builds.

    Keep the contract explicit so image builds can drop unnecessary local
    embedding dependencies when API-backed embeddings are requested.
    """
    api_embeddings_requested = extra_env.get(
        "NEXUS_TXTAI_USE_API_EMBEDDINGS", ""
    ).strip().lower() in (
        "true",
        "1",
        "yes",
    )
    has_openai_key = bool(extra_env.get("OPENAI_API_KEY", "").strip())
    api_embeddings = api_embeddings_requested and has_openai_key
    return [
        "--build-arg",
        f"NEXUS_TXTAI_USE_API_EMBEDDINGS={'true' if api_embeddings else 'false'}",
    ]


def _resolve_profiles(
    config: dict[str, Any],
    cli_addons: tuple[str, ...] = (),
) -> list[str]:
    """Build the complete list of compose profiles from config + CLI addons.

    Single source of truth for addon → profile mapping. Used by up, down,
    logs, and restart.
    """
    profiles = list(config.get("compose_profiles", []))
    # Add CLI add-ons
    for addon in cli_addons:
        profile = ADDON_PROFILE_MAP.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)
    # Add configured add-ons from nexus.yaml
    for addon in config.get("addons", []):
        profile = ADDON_PROFILE_MAP.get(addon, addon)
        if profile not in profiles:
            profiles.append(profile)
    return profiles


# ---------------------------------------------------------------------------
# Docker Compose helpers
# ---------------------------------------------------------------------------


def _compose_profiles(compose_file: str) -> set[str]:
    """Return the set of profiles defined across all services."""
    try:
        with open(compose_file) as f:
            stack = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return set()
    except yaml.YAMLError as exc:
        console.print(
            f"[nexus.warning]Warning:[/nexus.warning] Failed to parse {compose_file}: {exc}"
        )
        return set()

    if not isinstance(stack, dict):
        console.print(
            f"[nexus.warning]Warning:[/nexus.warning] {compose_file} does not contain a valid mapping"
        )
        return set()

    profiles: set[str] = set()
    for svc in (stack.get("services") or {}).values():
        if isinstance(svc, dict):
            for p in svc.get("profiles") or []:
                profiles.add(p)
    return profiles


def _find_repo_dockerfile() -> Path | None:
    """Walk up from CWD to find a Dockerfile in a nexus repo checkout."""
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "Dockerfile"
        if candidate.exists() and (parent / "pyproject.toml").exists():
            # Verify it's actually a nexus repo (not some random Dockerfile)
            try:
                text = (parent / "pyproject.toml").read_text(errors="ignore")
                if "nexus" in text.lower():
                    return candidate
            except OSError:
                pass
        if parent == parent.parent:
            break
    return None


def _compose_has_build(compose_file: str) -> bool:
    """Return True if the nexus service in the compose file has a ``build:`` directive."""
    try:
        with open(compose_file) as f:
            stack = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return False
    nexus_svc = (stack.get("services") or {}).get("nexus")
    return isinstance(nexus_svc, dict) and "build" in nexus_svc


def _resolve_pgvector_init_sql(compose_file: str) -> str | None:
    """Resolve an absolute pgvector init SQL path for portable compose stacks."""
    sibling = Path(compose_file).with_name("001-enable-pgvector.sql")
    if sibling.exists():
        return str(sibling.resolve())

    bundled = Path(__file__).resolve().parent.parent / "data" / "001-enable-pgvector.sql"
    if bundled.exists():
        return str(bundled.resolve())

    return None


def _is_channel_following(config: dict[str, Any]) -> bool:
    """Return True if the config follows a mutable channel (stable/edge) rather than a pinned ref."""
    return "image_channel" in config and "image_pin" not in config


@functools.lru_cache(maxsize=1)
def _find_docker_compose() -> str:
    """Return the docker compose command prefix (cached for the process lifetime)."""
    # Prefer `docker compose` (Compose V2, plugin)
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "docker compose"
    # Fallback to standalone `docker-compose`
    if shutil.which("docker-compose"):
        return "docker-compose"
    console.print("[nexus.error]Error:[/nexus.error] Docker Compose is not installed.")
    console.print(
        "[nexus.warning]Hint:[/nexus.warning] Install Docker Desktop or the compose plugin: "
        "https://docs.docker.com/compose/install/"
    )
    raise SystemExit(1)


def _compose_cmd(
    compose_file: str,
    profiles: list[str],
    *args: str,
) -> list[str]:
    """Build the full docker compose command list."""
    base = _find_docker_compose().split()
    cmd = [*base, "-f", compose_file]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    cmd.extend(args)
    return cmd


def _run_compose(
    compose_file: str,
    profiles: list[str],
    *args: str,
    extra_env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a docker compose command."""
    cmd = _compose_cmd(compose_file, profiles, *args)
    run_env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        cmd,
        env=run_env,
        text=True,
        capture_output=capture,
    )


# ---------------------------------------------------------------------------
# Health polling
# ---------------------------------------------------------------------------

# Service health endpoints (service_name -> (url_template, timeout_seconds))
HEALTH_ENDPOINTS: dict[str, tuple[str, int]] = {
    "nexus": ("http://localhost:{http}/healthz/ready", 120),
    "postgres": ("", 30),  # checked via pg_isready in container
    "dragonfly": ("", 15),
}


async def _poll_service_health(
    service: str,
    ports: dict[str, int],
    timeout: int,
) -> tuple[str, float, bool]:
    """Poll a single service until healthy or timeout.

    Returns (service_name, elapsed_seconds, healthy).
    """
    start = time.monotonic()
    url_template, default_timeout = HEALTH_ENDPOINTS.get(service, ("", timeout))
    effective_timeout = min(timeout, default_timeout) if default_timeout else timeout

    # For services without HTTP health endpoints, just check port availability
    port_key_map = {
        "nexus": "http",
        "postgres": "postgres",
        "dragonfly": "dragonfly",
    }
    port_key = port_key_map.get(service)
    if not port_key or port_key not in ports:
        return service, 0.0, True

    port = ports[port_key]
    delay = 0.5

    while (time.monotonic() - start) < effective_timeout:
        # Check if port is accepting connections (i.e. NOT available = service is up)
        if not check_port_available(port):
            # Port is in use — service is likely healthy
            if url_template:
                # Also verify HTTP health endpoint
                url = url_template.format(**ports)
                try:
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        if resp.status == 200:
                            return service, time.monotonic() - start, True
                except Exception:
                    pass
            else:
                # No HTTP endpoint — port connectivity is enough
                return service, time.monotonic() - start, True

        await asyncio.sleep(delay)
        delay = min(delay * 2, 4.0)  # exponential backoff, cap at 4s

    return service, time.monotonic() - start, False


async def _poll_all_services(
    services: list[str],
    ports: dict[str, int],
    timeout: int,
) -> list[tuple[str, float, bool]]:
    """Poll all services in parallel."""
    tasks = [_poll_service_health(service, ports, timeout) for service in services]
    return list(await asyncio.gather(*tasks))


# ---------------------------------------------------------------------------
# Container state detection
# ---------------------------------------------------------------------------


def _detect_container_state(
    project_name: str,
) -> str:
    """Detect the state of containers for a compose project.

    Returns one of:
        "running"  — all containers are running (or healthy)
        "stopped"  — containers exist but are stopped/exited
        "absent"   — no containers found for this project
    """
    try:
        # Get all containers (including stopped) for this project
        result = subprocess.run(
            [
                *_find_docker_compose().split(),
                "-p",
                project_name,
                "ps",
                "-a",
                "--format",
                "{{.State}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "absent"

        states = [s.strip().lower() for s in result.stdout.strip().splitlines() if s.strip()]
        if not states:
            return "absent"

        # If all containers are running, the stack is up
        if all(s in ("running",) for s in states):
            return "running"

        # If any container exists (running, exited, paused, etc.), it's "stopped"
        return "stopped"

    except (subprocess.TimeoutExpired, OSError):
        return "absent"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register_commands(cli: click.Group) -> None:
    """Register stack lifecycle commands."""
    cli.add_command(up)
    cli.add_command(down)
    cli.add_command(logs)
    cli.add_command(restart)
    cli.add_command(upgrade)
    cli.add_command(stop)
    cli.add_command(start)


@click.command()
@click.option(
    "--detach/--no-detach", "-d", default=True, show_default=True, help="Run in background."
)
@click.option(
    "--with",
    "addons",
    multiple=True,
    type=click.Choice(
        ["nats", "mcp", "frontend", "langgraph", "observability"],
        case_sensitive=False,
    ),
    help="Optional add-on services (repeatable).",
)
@click.option(
    "--port-strategy",
    type=click.Choice(VALID_STRATEGIES),
    default="auto",
    show_default=True,
    help="How to handle port conflicts: auto (pick next free), prompt, fail.",
)
@click.option(
    "--compose-file",
    type=click.Path(exists=True),
    default=None,
    help="Override the compose file path.",
)
@click.option(
    "--build/--no-build",
    default=None,
    help="Build images locally instead of pulling from GHCR (default: pull).",
)
@click.option(
    "--pull/--no-pull",
    "force_pull",
    default=None,
    help="Force image pull from remote (clears local build mode).",
)
@click.option(
    "--timeout", type=int, default=180, show_default=True, help="Health check timeout in seconds."
)
def up(
    detach: bool,
    addons: tuple[str, ...],
    port_strategy: str,
    compose_file: str | None,
    build: bool | None,
    force_pull: bool | None,
    timeout: int,
) -> None:
    """Start the Nexus stack.

    Reads nexus.yaml, resolves port conflicts, starts Docker Compose
    services, waits for health, and prints the service table.

    Portable stacks (installed via pip) pull the prebuilt image from
    GHCR — no local Docker build required.  Repo-checkout stacks
    (with ``build:`` directives) rebuild automatically.

    Use ``--build`` to force a local build, or ``--no-build`` to skip.
    After a ``--build``, subsequent ``nexus up`` reuses the local image.
    Use ``--pull`` to discard the local build and pull from remote.

    Examples:
        nexus up                        # start from nexus.yaml
        nexus up --with nats            # add NATS event bus
        nexus up --port-strategy prompt # ask on conflicts
        nexus up --build                # force rebuild images
        nexus up --pull                 # discard local build, pull remote
    """
    config = _load_project_config_optional()

    # Auto-init: if no nexus.yaml in CWD, search parent directories first
    # to avoid creating a nested project inside an existing workspace.
    if not config:
        from nexus.cli.state import CONFIG_SEARCH_PATHS

        cwd = Path.cwd()
        for parent in cwd.parents:
            for name in CONFIG_SEARCH_PATHS:
                candidate = parent / Path(name).name
                if candidate.exists():
                    console.print(
                        f"[nexus.warning]No nexus.yaml in current directory, "
                        f"but found {candidate}[/nexus.warning]"
                    )
                    console.print(
                        f"  Run `nexus up` from {parent} or "
                        f"`nexus init --preset shared` here to create a new project."
                    )
                    raise SystemExit(1)

        console.print("[bold]No nexus.yaml found — initializing with preset 'shared'...[/bold]")
        from click.testing import CliRunner

        from nexus.cli.commands.init_cmd import init as init_cmd

        init_result = CliRunner().invoke(
            init_cmd, ["--preset", "shared", "--force"], catch_exceptions=False
        )
        if init_result.exit_code != 0:
            console.print("[nexus.error]Error:[/nexus.error] Auto-init failed.")
            if init_result.output:
                console.print(init_result.output)
            raise SystemExit(1)
        console.print(init_result.output)
        config = _load_project_config()

    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' does not use Docker.[/nexus.warning] "
            "Use `nexus serve` to start a local server, or re-init with "
            "`nexus init --preset shared`."
        )
        raise SystemExit(0)

    # Determine compose file
    cf = compose_file or config.get("compose_file", "./nexus-stack.yml")
    if not Path(cf).exists():
        console.print(f"[nexus.error]Error:[/nexus.error] Compose file not found: {cf}")
        console.print(
            "[nexus.warning]Hint:[/nexus.warning] Ensure nexus-stack.yml is in the project root."
        )
        raise SystemExit(1)

    data_dir = str(Path(config.get("data_dir", "./nexus-data")).resolve())

    # Check previous runtime state for local build reuse
    prev_state = load_runtime_state(data_dir)

    # ---------------------------------------------------------------
    # Smart resume: detect existing container state and take fast path
    # when no flags request a rebuild/pull/force-recreate AND the
    # config has not changed since the last `nexus up`.
    # ---------------------------------------------------------------
    prev_project = prev_state.get("project_name", "")
    no_force_flags = build is None and force_pull is None and not addons

    # Detect config drift: compare the compose environment that would be
    # generated from current nexus.yaml against what was used last time.
    # This catches changes to image, auth, ports, TLS, API key, etc.
    config_changed = False
    if prev_project and no_force_flags:
        curr_compose_env = _derive_project_env(config)
        prev_compose_env = prev_state.get("compose_env", {})
        if prev_compose_env and curr_compose_env != prev_compose_env:
            config_changed = True

    if prev_project and no_force_flags and not config_changed:
        container_state = _detect_container_state(prev_project)

        if container_state == "running":
            # Already running — print status and exit
            console.print()
            console.print("[nexus.success]Nexus stack is already running.[/nexus.success]")
            conn_env = resolve_connection_env(config, prev_state)
            console.print()
            console.print("[bold]Connection:[/bold]")
            for key, value in sorted(conn_env.items()):
                console.print(f"  export {key}='{value}'")
            console.print()
            console.print(
                "[nexus.muted]Use `nexus up --pull` to update, "
                "or `nexus down && nexus up` to recreate.[/nexus.muted]"
            )
            return

        if container_state == "stopped":
            # Containers exist but stopped — fast resume via docker compose up
            # (not `start`) so config changes are reconciled, then health-poll.
            console.print()
            console.print("[bold]Resuming stopped Nexus stack...[/bold]")
            profiles = _resolve_profiles(config)
            prev_ports = prev_state.get("ports", config.get("ports", {}))
            compose_env = _derive_project_env(config, resolved_ports=prev_ports)
            result = _run_compose(cf, profiles, "up", "-d", extra_env=compose_env)
            if result.returncode != 0:
                console.print("[nexus.error]Error:[/nexus.error] Failed to resume stack.")
                raise SystemExit(result.returncode)

            # Health polling — same guarantees as a fresh start
            active_services = config.get("services", [])
            health_services = [s for s in active_services if s in HEALTH_ENDPOINTS]
            console.print("[bold]Waiting for services...[/bold]")
            health_results = asyncio.run(_poll_all_services(health_services, prev_ports, timeout))
            all_healthy = True
            for service, elapsed, healthy in health_results:
                if healthy:
                    console.print(f"  [nexus.success]✓[/nexus.success] {service} ({elapsed:.1f}s)")
                else:
                    console.print(
                        f"  [nexus.error]✗[/nexus.error] {service} (timed out after {elapsed:.0f}s)"
                    )
                    all_healthy = False
            if not all_healthy:
                console.print()
                console.print(
                    "[nexus.warning]Some services did not become healthy.[/nexus.warning]"
                )
                console.print("  Run `nexus logs` to investigate.")
                raise SystemExit(1)

            console.print("[nexus.success]✓[/nexus.success] Stack resumed.")
            conn_env = resolve_connection_env(config, prev_state)
            console.print()
            console.print("[bold]Connection:[/bold]")
            for key, value in sorted(conn_env.items()):
                console.print(f"  export {key}='{value}'")
            return

    # Fall through: containers absent or force flags set — full start
    using_local_build = False

    # Default: pull prebuilt image.  Only build when explicitly requested
    # via --build (local dev iteration).
    if build is None:
        # Reuse local build if previous state says so (and not --pull)
        if prev_state.get("build_mode") == "local" and force_pull is not True:
            using_local_build = True
        build = False

    # --pull clears local build mode
    if force_pull:
        using_local_build = False

    # Build profiles list (single source of truth via _resolve_profiles)
    profiles = _resolve_profiles(config, addons)

    # Validate profiles against what the compose file actually defines.
    available_profiles = _compose_profiles(cf)
    if available_profiles:
        missing = [p for p in profiles if p not in available_profiles]
        if missing:
            for p in missing:
                console.print(
                    f"  [nexus.warning]Warning: profile '{p}' not found in {Path(cf).name}, skipping[/nexus.warning]"
                )
            profiles = [p for p in profiles if p in available_profiles]

    # Port resolution: reuse previous state.json ports if OUR containers
    # still own them, otherwise resolve from config defaults.
    ports = config.get("ports", {})
    active_services = config.get("services", [])

    prev_ports = prev_state.get("ports", {})
    prev_project = prev_state.get("project_name", "")
    reuse_ports = False

    if prev_ports and prev_project:
        # Verify ownership: check if our compose project has running containers.
        # This avoids the false-positive where an unrelated process binds
        # one of our remembered ports after our stack was stopped.
        try:
            ownership_check = subprocess.run(
                [*_find_docker_compose().split(), "-p", prev_project, "ps", "-q"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            has_running_containers = bool(ownership_check.stdout.strip())
            reuse_ports = has_running_containers
        except (subprocess.TimeoutExpired, OSError):
            # Can't verify — fall through to re-resolve
            pass

    if reuse_ports:
        resolved_ports = prev_ports
        port_messages: list[str] = []
    else:
        resolved_ports, port_messages = resolve_ports(ports, strategy=port_strategy)

    # Print header
    console.print()
    console.print(f"[bold]Starting Nexus preset: {preset}[/bold]")
    console.print(f"  Using stack: {cf}")
    if build:
        console.print("  Image: [nexus.success]local build[/nexus.success] (from Dockerfile)")
    elif using_local_build:
        console.print(
            f"  Image: [nexus.success]{prev_state.get('image_used', 'local')}[/nexus.success] (reusing local build)"
        )
    else:
        image_ref = _resolve_image_ref_from_config(config)
        if image_ref:
            console.print(f"  Image: {image_ref}")
    if addons:
        console.print(f"  Add-ons: {', '.join(addons)}")
    console.print()

    # Print port resolution messages
    for msg in port_messages:
        console.print(f"  [nexus.warning]{msg}[/nexus.warning]")

    # NOTE: resolved ports are NOT written back to nexus.yaml.
    # They go into .state.json (written after health check).

    # Build environment from config (project name, ports, data dir, auth, image, TLS).
    compose_env = _derive_project_env(config, resolved_ports=resolved_ports)
    pgvector_init_sql = _resolve_pgvector_init_sql(cf)
    if pgvector_init_sql:
        compose_env["NEXUS_PGVECTOR_INIT_SQL"] = pgvector_init_sql

    # Track effective image and build mode for state.json
    effective_build_mode = "remote"
    effective_image_used = compose_env.get("NEXUS_IMAGE_REF", "")

    # When reusing a local build, set the local image tag and skip pull
    if using_local_build and not build:
        local_image = prev_state.get("image_used", "")
        if local_image:
            compose_env["NEXUS_IMAGE_REF"] = local_image
            effective_image_used = local_image
            effective_build_mode = "local"

    # When --build is requested, build with a local-only tag
    if build:
        project_hash = compose_env["COMPOSE_PROJECT_NAME"].split("-")[-1]
        local_tag = f"nexus:local-{project_hash}"

        if _compose_has_build(cf):
            compose_env.pop("NEXUS_IMAGE_REF", None)
            effective_build_mode = "local"
            effective_image_used = local_tag
        else:
            # No build: directive in compose file.  Fall back to building
            # the Docker image from the repo Dockerfile if one exists.
            repo_dockerfile = _find_repo_dockerfile()
            if repo_dockerfile:
                console.print(
                    f"[nexus.path]Nexus:[/nexus.path] building image from {repo_dockerfile.relative_to(repo_dockerfile.parent.parent)} "
                    f"→ {local_tag}"
                )
                build_result = subprocess.run(
                    [
                        "docker",
                        "build",
                        *_docker_build_args(compose_env),
                        "-t",
                        local_tag,
                        "-f",
                        str(repo_dockerfile),
                        str(repo_dockerfile.parent),
                    ],
                    env={**os.environ, **compose_env},
                )
                if build_result.returncode != 0:
                    console.print("[nexus.error]Error:[/nexus.error] Docker build failed.")
                    raise SystemExit(1)
                console.print(
                    f"[nexus.success]Nexus:[/nexus.success] built image {local_tag} from source"
                )
                compose_env["NEXUS_IMAGE_REF"] = local_tag
                effective_image_used = local_tag
                effective_build_mode = "local"
                build = False  # don't pass --build to compose (no build: directive)
            else:
                console.print(
                    "[nexus.warning]Warning:[/nexus.warning] --build ignored — compose file "
                    f"({Path(cf).name}) has no build: directive and no Dockerfile found."
                )
                build = False

    # Start compose
    compose_args: list[str] = ["up"]
    if detach:
        compose_args.append("-d")
    if build:
        compose_args.append("--build")

    # Pull logic:
    # - --pull flag: always pull
    # - Channel-following + not local build: pull to get latest mutable tag
    # - Local build mode: skip pull (preserve local image)
    if (
        force_pull
        or not build
        and effective_build_mode != "local"
        and _is_channel_following(config)
    ):
        compose_args.extend(["--pull", "always"])

    result = _run_compose(cf, profiles, *compose_args, extra_env=compose_env)
    if result.returncode != 0:
        console.print("[nexus.error]Error:[/nexus.error] Docker Compose failed to start.")
        raise SystemExit(result.returncode)

    if not detach:
        # Foreground mode — compose handles output
        return

    # Health polling
    console.print("[bold]Waiting for services...[/bold]")
    health_services = [s for s in active_services if s in HEALTH_ENDPOINTS]
    results = asyncio.run(_poll_all_services(health_services, resolved_ports, timeout))

    # Print results
    console.print()
    all_healthy = True
    for service, elapsed, healthy in results:
        if healthy:
            console.print(f"  [nexus.success]✓[/nexus.success] {service} ({elapsed:.1f}s)")
        else:
            console.print(
                f"  [nexus.error]✗[/nexus.error] {service} (timed out after {elapsed:.0f}s)"
            )
            all_healthy = False

    if not all_healthy:
        console.print()
        console.print("[nexus.warning]Some services did not become healthy.[/nexus.warning]")
        console.print("  Run `nexus logs` to investigate.")
        raise SystemExit(1)

    # Bootstrap auth: prefer the key from nexus.yaml (generated by nexus init),
    # fall back to .admin-api-key written by docker-entrypoint.sh.
    admin_api_key: str | None = config.get("api_key") or None
    if not admin_api_key:
        api_key_file = Path(data_dir) / ".admin-api-key"
        if api_key_file.exists():
            admin_api_key = api_key_file.read_text().strip() or None
            if admin_api_key:
                config["api_key"] = admin_api_key

    # Auto-discover TLS certs only for TLS-enabled stacks. Demo/shared stacks
    # can create a tls/ directory for internal services without exposing
    # gRPC TLS on the host port, and advertising those files to the host CLI
    # causes clients to attempt TLS against a plain-text port.
    tls_state: dict[str, str] = {}
    tls_dir = Path(data_dir) / "tls"
    if config.get("tls") and tls_dir.exists():
        # Raft-style certs
        if (tls_dir / "ca.pem").exists():
            tls_state = {
                "cert": str(tls_dir / "node.pem"),
                "key": str(tls_dir / "node-key.pem"),
                "ca": str(tls_dir / "ca.pem"),
            }
        # OpenSSL-style certs (from nexus init --tls)
        elif (tls_dir / "ca.crt").exists():
            tls_state = {
                "cert": str(tls_dir / "server.crt"),
                "key": str(tls_dir / "server.key"),
                "ca": str(tls_dir / "ca.crt"),
            }

    if tls_state:
        config["tls"] = True
        config["tls_cert"] = tls_state["cert"]
        config["tls_key"] = tls_state["key"]
        config["tls_ca"] = tls_state["ca"]

    _save_project_config(config)

    # Write runtime state to {data_dir}/.state.json (NOT nexus.yaml)
    # compose_env snapshot enables config-drift detection on next `nexus up`.
    runtime_state: dict[str, Any] = {
        "ports": resolved_ports,
        "api_key": admin_api_key or "",
        "image_used": effective_image_used,
        "build_mode": effective_build_mode,
        "project_name": compose_env["COMPOSE_PROJECT_NAME"],
        "compose_env": compose_env,
        "started_at": datetime.now(UTC).isoformat(),
    }
    if tls_state:
        runtime_state["tls"] = tls_state
    save_runtime_state(data_dir, runtime_state)

    # Build connection env vars for display
    conn_env = resolve_connection_env(config, runtime_state)

    # Print final status table
    console.print()
    console.print("[bold]Healthy services:[/bold]")
    http_port = resolved_ports.get("http", 2026)
    grpc_port = resolved_ports.get("grpc", 2028)
    pg_port = resolved_ports.get("postgres", 5432)
    df_port = resolved_ports.get("dragonfly", 6379)

    console.print(f"  nexus       http://localhost:{http_port}")
    console.print(f"  grpc        localhost:{grpc_port}")
    if "postgres" in active_services:
        console.print(f"  postgres    localhost:{pg_port}")
    if "dragonfly" in active_services:
        console.print(f"  dragonfly   localhost:{df_port}")

    # Surface connection info
    console.print()
    console.print("[bold]Connection:[/bold]")
    for key, value in sorted(conn_env.items()):
        console.print(f"  export {key}='{value}'")

    # Print next steps
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  eval $(nexus env)        # load env vars into shell")
    if preset == "demo":
        console.print("  nexus demo init")
    console.print("  nexus status")


@click.command()
@click.option("--volumes", "-v", is_flag=True, default=False, help="Also remove volumes.")
def down(volumes: bool) -> None:
    """Stop the Nexus stack.

    Stops all Docker Compose services started by `nexus up`.

    Examples:
        nexus down             # stop services
        nexus down --volumes   # stop and remove volumes
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' has no Docker services to stop.[/nexus.warning]"
        )
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)

    args: list[str] = ["down"]
    if volumes:
        args.append("--volumes")

    compose_env = _derive_project_env(config)

    console.print(f"[bold]Stopping Nexus preset: {preset}[/bold]")
    result = _run_compose(cf, profiles, *args, extra_env=compose_env)
    if result.returncode == 0:
        # When --volumes is used, also clear Raft state from the host-mounted
        # data directory.  Docker volumes are cleaned by `docker compose down -v`
        # but host-mounted dirs (nexus-data/) are not.  Stale Raft logs contain
        # old node IDs that prevent single-node leader election on restart.
        if volumes:
            import shutil

            data_dir = Path(config.get("data_dir", "./nexus-data")).resolve()
            raft_dirs = list(data_dir.glob("*/raft")) + list(data_dir.glob("*/sm"))
            for rd in raft_dirs:
                if rd.exists():
                    shutil.rmtree(rd) if rd.is_dir() else rd.unlink()
            if raft_dirs:
                console.print(
                    f"[nexus.muted]  Cleared {len(raft_dirs)} Raft state path(s) from {data_dir}[/nexus.muted]"
                )
        console.print("[nexus.success]✓[/nexus.success] Stack stopped.")
    else:
        console.print("[nexus.error]Error:[/nexus.error] Failed to stop stack.")
        raise SystemExit(result.returncode)


@click.command()
@click.option(
    "--follow/--no-follow", "-f", default=True, show_default=True, help="Follow log output."
)
@click.option("--tail", type=int, default=100, show_default=True, help="Number of lines to show.")
@click.argument("service", required=False, default=None)
def logs(follow: bool, tail: int, service: str | None) -> None:
    """View logs from the Nexus stack.

    Examples:
        nexus logs              # all services
        nexus logs nexus        # single service
        nexus logs --tail 50    # last 50 lines
    """
    config = _load_project_config()
    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    args: list[str] = ["logs", "--tail", str(tail)]
    if follow:
        args.append("--follow")
    if service:
        args.append(service)

    _run_compose(cf, profiles, *args, extra_env=compose_env)


@click.command()
@click.option(
    "--build/--no-build",
    default=None,
    help="Build images (auto-detected from compose file).",
)
def restart(build: bool | None) -> None:
    """Restart the Nexus stack.

    Equivalent to `nexus down && nexus up`.

    Examples:
        nexus restart           # restart services
        nexus restart --build   # force rebuild and restart
        nexus restart --no-build  # skip rebuild
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' has no Docker services to restart.[/nexus.warning]"
        )
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    # Default: pull prebuilt image (same as `up`)
    if build is None:
        build = False

    # Same --build / --pull logic as `up`
    if build:
        if _compose_has_build(cf):
            compose_env.pop("NEXUS_IMAGE_REF", None)
        else:
            console.print(
                "[nexus.warning]Warning:[/nexus.warning] --build ignored — compose file "
                f"({Path(cf).name}) has no build: directive."
            )
            build = False

    console.print(f"[bold]Restarting Nexus preset: {preset}[/bold]")
    _run_compose(cf, profiles, "down", extra_env=compose_env)

    args: list[str] = ["up", "-d"]
    if build:
        args.append("--build")
    if not build and _is_channel_following(config):
        args.extend(["--pull", "always"])
    result = _run_compose(cf, profiles, *args, extra_env=compose_env)
    if result.returncode == 0:
        console.print("[nexus.success]✓[/nexus.success] Stack restarted.")
    else:
        console.print("[nexus.error]Error:[/nexus.error] Failed to restart stack.")
        raise SystemExit(result.returncode)


@click.command()
@click.option(
    "--channel",
    default=None,
    help="Override the channel to resolve from (default: use config channel).",
)
@click.option(
    "--image-tag",
    default=None,
    help="Pin to an explicit tag instead of resolving from channel.",
)
@click.option(
    "--image-digest",
    default=None,
    help="Pin to an explicit digest (sha256:...).",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def upgrade(
    channel: str | None,
    image_tag: str | None,
    image_digest: str | None,
    yes: bool,
) -> None:
    """Upgrade the pinned image reference.

    Re-resolves the release channel to a new concrete image ref and
    updates nexus.yaml.  Does NOT restart the stack — run ``nexus restart``
    after reviewing the change.

    Examples:
        nexus upgrade                        # re-resolve stable channel
        nexus upgrade --channel edge         # switch to edge
        nexus upgrade --image-tag 0.10.0     # pin to specific version
        nexus upgrade --image-digest sha256:abc123...
    """
    from nexus.cli.commands.init_cmd import VALID_CHANNELS

    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' does not use a prebuilt image.[/nexus.warning]"
        )
        raise SystemExit(0)

    # Validate --channel against known channels
    if channel and channel not in VALID_CHANNELS:
        console.print(
            f"[nexus.error]Error:[/nexus.error] Unknown channel '{channel}'. "
            f"Valid channels: {', '.join(VALID_CHANNELS)}"
        )
        raise SystemExit(1)

    # Warn if config is explicitly pinned (--image-tag or --image-digest at init)
    pin_mode = config.get("image_pin", "")
    if pin_mode and not (image_tag or image_digest or channel):
        console.print(
            f"[nexus.warning]Warning:[/nexus.warning] This config is pinned via {pin_mode}. "
            "Use --image-tag or --image-digest to change the pin, "
            "or --channel to switch to channel-following mode."
        )
        return

    current_ref = config.get("image_ref", config.get("image_tag", "(unknown)"))
    effective_channel = channel or config.get("image_channel", "stable")
    effective_accel = config.get("image_accelerator", "cpu")

    new_ref = _resolve_image_ref(
        effective_channel,
        effective_accel,
        image_tag=image_tag,
        image_digest=image_digest,
    )

    # For channel-following configs (stable/edge), the ref string doesn't
    # change between releases — the tag is mutable.  Pull the latest image
    # and restart instead of comparing strings.
    if new_ref == current_ref and _is_channel_following(config) and not (image_tag or image_digest):
        console.print(f"[bold]Pulling latest image for channel '{effective_channel}'...[/bold]")
        cf = config.get("compose_file", "./nexus-stack.yml")
        profiles = _resolve_profiles(config)
        compose_env = _derive_project_env(config)
        pull_result = _run_compose(cf, profiles, "pull", "nexus", extra_env=compose_env)
        if pull_result.returncode != 0:
            console.print("[nexus.error]Error:[/nexus.error] Failed to pull image.")
            raise SystemExit(pull_result.returncode)
        console.print(f"[nexus.success]✓[/nexus.success] Pulled latest {new_ref}")
        console.print("  Run `nexus restart` to apply.")
        return

    if new_ref == current_ref:
        console.print(f"[nexus.success]Already up to date:[/nexus.success] {current_ref}")
        return

    console.print("[bold]Image upgrade:[/bold]")
    console.print(f"  Current: {current_ref}")
    console.print(f"  New:     {new_ref}")
    console.print(f"  Channel: {effective_channel}")

    if not yes and not click.confirm("  Apply this change?", default=True):
        console.print("  Cancelled.")
        return

    config["image_ref"] = new_ref
    # Update pin/channel tracking
    if image_digest:
        config["image_pin"] = "digest"
        config.pop("image_channel", None)
    elif image_tag:
        config["image_pin"] = "tag"
        config.pop("image_channel", None)
    else:
        config["image_channel"] = effective_channel
        config.pop("image_pin", None)
    # Clean up deprecated image_tag if present
    config.pop("image_tag", None)

    _save_project_config(config)
    console.print(f"[nexus.success]✓[/nexus.success] Updated nexus.yaml → {new_ref}")
    console.print("  Run `nexus restart` to apply.")


@click.command()
def stop() -> None:
    """Pause the Nexus stack (keep containers and volumes).

    Containers are paused but not removed.  Resume with ``nexus start``.
    This is faster than ``nexus down`` + ``nexus up`` because it skips
    port resolution, image pulls, and health checks.

    Examples:
        nexus stop
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' has no Docker services to stop.[/nexus.warning]"
        )
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    result = _run_compose(cf, profiles, "stop", extra_env=compose_env)
    if result.returncode == 0:
        console.print("[nexus.success]✓[/nexus.success] Stack paused. Resume with `nexus start`.")
    else:
        console.print("[nexus.error]Error:[/nexus.error] Failed to stop stack.")
        raise SystemExit(result.returncode)


@click.command()
def start() -> None:
    """Resume a paused Nexus stack.

    Resumes containers that were stopped with ``nexus stop``.
    Does not perform port checks, image pulls, or health polling.

    Examples:
        nexus start
    """
    config = _load_project_config()
    preset = config.get("preset", "local")

    if preset == "local":
        console.print(
            "[nexus.warning]Preset 'local' has no Docker services to start.[/nexus.warning]"
        )
        raise SystemExit(0)

    cf = config.get("compose_file", "./nexus-stack.yml")
    profiles = _resolve_profiles(config)
    compose_env = _derive_project_env(config)

    result = _run_compose(cf, profiles, "start", extra_env=compose_env)
    if result.returncode == 0:
        console.print("[nexus.success]✓[/nexus.success] Stack resumed.")
    else:
        console.print("[nexus.error]Error:[/nexus.error] Failed to start stack.")
        raise SystemExit(result.returncode)

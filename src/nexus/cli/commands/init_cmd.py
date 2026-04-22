"""Nexus init command — initialize a project with a preset.

Presets:
  local   — embedded-only quickstart, no daemon, no Docker
  shared  — one shared node with Postgres, Dragonfly, Zoekt
  demo    — shared node + demo-ready seed data settings

Writes a project-local ``nexus.yaml`` with all defaults materialized.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.port_utils import DEFAULT_PORTS, derive_ports
from nexus.cli.theme import console

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default image registry / repo
# ---------------------------------------------------------------------------

DEFAULT_IMAGE_REGISTRY = "ghcr.io/nexi-lab/nexus"

# ---------------------------------------------------------------------------
# Preset definitions (frozen dataclass — single source of truth per preset)
# ---------------------------------------------------------------------------

VALID_PRESETS = ("local", "shared", "demo")
VALID_CHANNELS = ("stable", "edge")
VALID_ACCELERATORS = ("cpu", "cuda")


@dataclass(frozen=True)
class PresetConfig:
    """Immutable preset configuration — one entry per preset."""

    services: tuple[str, ...]
    auth: str
    compose_profiles: tuple[str, ...]
    port_keys: tuple[str, ...]
    image_channel: str = "edge"
    image_accelerator: str = "cpu"


PRESETS: dict[str, PresetConfig] = {
    "local": PresetConfig(
        services=(),
        auth="none",
        compose_profiles=(),
        port_keys=(),
    ),
    "shared": PresetConfig(
        services=("nexus", "postgres", "dragonfly"),
        auth="static",
        compose_profiles=("core", "cache"),
        port_keys=("http", "grpc", "postgres", "dragonfly"),
    ),
    "demo": PresetConfig(
        services=("nexus", "postgres", "dragonfly"),
        auth="database",
        compose_profiles=("core", "cache"),
        port_keys=("http", "grpc", "postgres", "dragonfly"),
    ),
}

# Addon-to-compose-profile mapping (single source of truth — Issue #2961)
ADDON_PROFILE_MAP: dict[str, str] = {
    "nats": "events",
    "mcp": "mcp",
    "frontend": "frontend",
    "langgraph": "langgraph",
    "observability": "observability",
}


def _find_compose_file() -> Path | None:
    """Locate ``nexus-stack.yml`` by searching the CWD and ancestor directories."""
    candidate = Path.cwd()
    for _ in range(10):  # avoid infinite walk
        f = candidate / "nexus-stack.yml"
        if f.exists():
            return f.resolve()
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def _bundled_compose_file() -> Path | None:
    """Return the path to the compose file bundled inside the package."""
    bundled = Path(__file__).resolve().parent.parent / "data" / "nexus-stack.yml"
    if bundled.exists():
        return bundled
    return None


def _resolve_image_ref(
    channel: str,
    accelerator: str,
    image_tag: str | None = None,
    image_digest: str | None = None,
) -> str:
    """Resolve a concrete image reference from channel/accelerator/overrides.

    Priority:
      1. Explicit digest → ghcr.io/nexi-lab/nexus@sha256:...
      2. Explicit tag → ghcr.io/nexi-lab/nexus:<tag>
      3. Channel resolution:
         - ``stable``: use mutable ``stable`` tag (updated on every release)
         - ``edge``: use mutable ``edge`` tag (updated on every develop push)
    """
    registry = DEFAULT_IMAGE_REGISTRY

    if image_digest:
        return f"{registry}@{image_digest}"

    # Both channels use mutable tags managed by CI:
    #   stable → pushed by release.yml on every git tag (v*)
    #   edge   → pushed by docker-publish.yml on every develop push
    tag = image_tag or channel

    if accelerator == "cuda" and not image_digest:
        tag = f"{tag}-cuda"

    return f"{registry}:{tag}"


def _build_config(
    preset: str,
    data_dir: str,
    tls: bool,
    ports: dict[str, int],
    addons: tuple[str, ...],
    compose_file_override: str | None = None,
    *,
    channel: str = "edge",
    accelerator: str = "cpu",
    image_tag: str | None = None,
    image_digest: str | None = None,
) -> dict[str, Any]:
    """Build the materialized nexus.yaml config dict.

    All filesystem paths (data_dir, compose_file, tls_*) are stored as
    **absolute** paths so that ``nexus up`` works regardless of the
    working directory at runtime.
    """
    preset_cfg = PRESETS[preset]
    abs_data_dir = str(Path(data_dir).resolve())
    config: dict[str, Any] = {
        "preset": preset,
        "data_dir": abs_data_dir,
        "auth": preset_cfg.auth,
        "tls": tls,
    }

    if preset != "local":
        config["services"] = list(preset_cfg.services)
        config["ports"] = {k: v for k, v in ports.items() if k in preset_cfg.port_keys}
        config["compose_profiles"] = list(preset_cfg.compose_profiles)

        # Generate a random admin API key so the CLI can authenticate
        # against the stack without relying on docker-entrypoint.sh
        # to write .admin-api-key (portable image runs nexusd directly).
        import secrets

        config["api_key"] = f"sk-{secrets.token_urlsafe(32)}"

        # Compose file: CLI override → search CWD + ancestors → bundled copy
        if compose_file_override:
            config["compose_file"] = str(Path(compose_file_override).resolve())
        else:
            found = _find_compose_file()
            if found is not None:
                config["compose_file"] = str(found)
            else:
                # Mark as needing the bundled copy (resolved later in init())
                config["compose_file"] = ""

        # Resolve and pin the full image reference (Issue #2961)
        effective_channel = channel or preset_cfg.image_channel
        effective_accel = accelerator or preset_cfg.image_accelerator
        config["image_accelerator"] = effective_accel
        config["image_ref"] = _resolve_image_ref(
            effective_channel,
            effective_accel,
            image_tag=image_tag,
            image_digest=image_digest,
        )

        # Track whether the ref is channel-following or explicitly pinned.
        # When pinned (--image-tag or --image-digest), nexus upgrade should
        # warn instead of silently re-resolving the channel.
        if image_digest:
            config["image_pin"] = "digest"
        elif image_tag:
            config["image_pin"] = "tag"
        else:
            config["image_channel"] = effective_channel

    if addons:
        config.setdefault("addons", []).extend(addons)

    if tls:
        config["tls_dir"] = os.path.join(abs_data_dir, "tls")
        config["tls_cert"] = os.path.join(abs_data_dir, "tls", "server.crt")
        config["tls_key"] = os.path.join(abs_data_dir, "tls", "server.key")
        config["tls_ca"] = os.path.join(abs_data_dir, "tls", "ca.crt")

    return config


def _write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write the nexus.yaml config file."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _scaffold_tls(tls_dir: Path) -> None:
    """Create self-signed CA + server certs in *tls_dir* using openssl.

    If ``openssl`` is not on ``$PATH``, the directory is created but
    left empty and the user is told which files to provide.
    """
    tls_dir.mkdir(parents=True, exist_ok=True)

    ca_key = tls_dir / "ca.key"
    ca_crt = tls_dir / "ca.crt"
    srv_key = tls_dir / "server.key"
    srv_crt = tls_dir / "server.crt"

    # Already populated — skip
    if srv_crt.exists() and ca_crt.exists():
        console.print(f"  TLS certs already exist in {tls_dir}")
        return

    if not shutil.which("openssl"):
        console.print(f"  [nexus.warning]openssl not found[/nexus.warning] — created {tls_dir}")
        console.print("  Please provide: ca.crt, server.crt, server.key")
        return

    try:
        # Generate CA key + self-signed CA cert
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_crt),
                "-days",
                "365",
                "-nodes",
                "-subj",
                "/CN=Nexus Dev CA",
            ],
            capture_output=True,
            check=True,
        )
        # Generate server key + CSR, sign with CA
        srv_csr = tls_dir / "server.csr"
        subprocess.run(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(srv_key),
                "-out",
                str(srv_csr),
                "-subj",
                "/CN=localhost",
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(srv_csr),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(srv_crt),
                "-days",
                "365",
            ],
            capture_output=True,
            check=True,
        )
        # Clean up transient files
        srv_csr.unlink(missing_ok=True)
        (tls_dir / "ca.srl").unlink(missing_ok=True)

        console.print(f"  [nexus.success]TLS certificates generated in {tls_dir}[/nexus.success]")
    except subprocess.CalledProcessError:
        console.print(f"  [nexus.warning]openssl failed[/nexus.warning] — created {tls_dir}")
        console.print("  Please provide: ca.crt, server.crt, server.key")


def _create_data_dirs(data_dir: Path, *, tls: bool = False) -> None:
    """Create the data directory structure.

    When *tls* is True, also scaffolds the ``tls/`` subdirectory and
    generates a self-signed CA + server certificate using ``openssl``.
    If ``openssl`` is not available, the directory is still created and
    the user is told how to populate it manually.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "cas").mkdir(exist_ok=True)

    if tls:
        _scaffold_tls(data_dir / "tls")


def _print_local_summary(config_path: Path, data_dir: Path) -> None:
    """Print summary for local preset."""
    console.print()
    console.print("[nexus.success]Initialized Nexus preset: local[/nexus.success]")
    console.print(f"  Config:   {config_path}")
    console.print(f"  Data dir: {data_dir}")
    console.print()
    console.print("[bold]Next step:[/bold]")
    console.print("  nexus doctor")


def _print_shared_summary(config: dict[str, Any], config_path: Path, data_dir: Path) -> None:
    """Print summary for shared/demo presets."""
    preset = config["preset"]
    services = ", ".join(config.get("services", []))
    ports = config.get("ports", {})

    console.print()
    console.print(f"[nexus.success]Initialized Nexus preset: {preset}[/nexus.success]")
    console.print(f"  Config:     {config_path}")
    console.print(f"  Data dir:   {data_dir}")
    console.print(f"  Services:   {services}")
    if config.get("image_ref"):
        console.print(f"  Image:      {config['image_ref']}")
    if config.get("image_channel"):
        console.print(f"  Channel:    {config['image_channel']}")
    if config.get("tls"):
        console.print("  TLS:        enabled")
        console.print(f"  TLS dir:    {config.get('tls_dir', '')}")
    console.print(f"  Auth:       {config.get('auth', 'static')}")
    if ports:
        console.print(f"  HTTP port:  {ports.get('http', DEFAULT_PORTS['http'])}")
        console.print(f"  gRPC port:  {ports.get('grpc', DEFAULT_PORTS['grpc'])}")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  nexus up")
    if preset == "demo":
        console.print("  nexus demo init")


@click.command(name="init")
@click.argument("workspace", required=False, type=click.Path())
@click.option(
    "--preset",
    type=click.Choice(VALID_PRESETS),
    default="local",
    show_default=True,
    help="Initialization preset: local (embedded), shared (one node), demo (shared + seed data).",
)
@click.option(
    "--data-dir",
    type=click.Path(),
    default="./nexus-data",
    show_default=True,
    help="Path to the data directory.",
)
@click.option(
    "--tls",
    is_flag=True,
    default=False,
    help="Enable TLS (scaffolds certificate material).",
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
    "--config-path",
    type=click.Path(),
    default="./nexus.yaml",
    show_default=True,
    help="Path for the generated config file.",
)
@click.option(
    "--compose-file",
    type=click.Path(),
    default=None,
    help="Path to nexus-stack.yml (auto-detected from CWD by default).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing nexus.yaml without prompting.",
)
@click.option(
    "--channel",
    type=click.Choice(VALID_CHANNELS),
    default="edge",
    show_default=True,
    help="Release channel for the prebuilt image.",
)
@click.option(
    "--accelerator",
    type=click.Choice(VALID_ACCELERATORS),
    default="cpu",
    show_default=True,
    help="Hardware accelerator variant: cpu (default) or cuda.",
)
@click.option(
    "--image-tag",
    default=None,
    help="Explicit image tag override (e.g. 0.9.2, pr-123). Overrides channel resolution.",
)
@click.option(
    "--image-digest",
    default=None,
    help="Explicit image digest (sha256:...). Overrides tag and channel.",
)
def init(
    workspace: str | None,
    preset: str,
    data_dir: str,
    tls: bool,
    addons: tuple[str, ...],
    config_path: str,
    compose_file: str | None,
    force: bool,
    channel: str,
    accelerator: str,
    image_tag: str | None,
    image_digest: str | None,
) -> None:
    """Initialize a new Nexus project with a preset.

    Creates data directories, writes a nexus.yaml config file, and prints
    the next command to run.

    Examples:
        nexus init                                  # local embedded (default)
        nexus init --preset shared                  # one shared node
        nexus init --preset shared --tls            # secure shared node
        nexus init --preset demo                    # demo-ready with seed settings
        nexus init --preset shared --with nats --with mcp
        nexus init --preset shared --channel edge   # pre-release image
        nexus init --preset shared --accelerator cuda
        nexus init --preset shared --image-tag 0.9.2
    """
    cfg_path = Path(config_path)
    d_dir = Path(data_dir)

    if workspace is not None:
        workspace_path = Path(workspace)
        cfg_path = workspace_path / "nexus.yaml"
        d_dir = workspace_path / "nexus-data"

    # Guard: don't overwrite existing config without --force
    if cfg_path.exists() and not force:
        console.print(
            f"[nexus.warning]Warning:[/nexus.warning] {cfg_path} already exists. Use --force to overwrite."
        )
        raise SystemExit(1)

    # Derive deterministic ports from data_dir so each project directory
    # gets stable, non-conflicting ports without manual coordination.
    ports = derive_ports(str(d_dir))

    # Build and write config
    config = _build_config(
        preset,
        str(d_dir),
        tls,
        ports,
        addons,
        compose_file,
        channel=channel,
        accelerator=accelerator,
        image_tag=image_tag,
        image_digest=image_digest,
    )

    # Resolve compose file for non-local presets:
    # 1) Explicit --compose-file passed → must exist, error if not
    # 2) Auto-detected (non-empty, exists) → use as-is
    # 3) Auto-detection missed → copy bundled version next to nexus.yaml
    # 4) No bundled version → hard error
    if preset != "local":
        cf = config.get("compose_file", "")
        if compose_file and not Path(cf).exists():
            # User explicitly passed --compose-file but it doesn't exist
            console.print(f"[nexus.error]Error:[/nexus.error] Compose file not found: {cf}")
            raise SystemExit(1)
        if not cf or not Path(cf).exists():
            bundled = _bundled_compose_file()
            if bundled is not None:
                dest_dir = cfg_path.parent.resolve()
                dest = dest_dir / "nexus-stack.yml"
                shutil.copy2(str(bundled), str(dest))
                config["compose_file"] = str(dest)
                console.print(f"  Copied bundled nexus-stack.yml → {dest}")

                # Copy pgvector init SQL (needed by the postgres service)
                bundled_dir = bundled.parent
                sql_file = bundled_dir / "001-enable-pgvector.sql"
                if sql_file.exists():
                    shutil.copy2(str(sql_file), str(dest_dir / sql_file.name))
            else:
                console.print(f"[nexus.error]Error:[/nexus.error] Compose file not found: {cf}")
                console.print("  Run `nexus init` from the Nexus repo root, or pass")
                console.print("  --compose-file /absolute/path/to/nexus-stack.yml")
                raise SystemExit(1)

    _write_config(config, cfg_path)

    # Create data directories (and TLS certs when --tls is passed)
    _create_data_dirs(d_dir, tls=tls)

    # Initialize local Nexus workspace for 'local' preset
    if preset == "local":

        async def _init_local_workspace() -> None:
            import nexus

            nx = nexus.connect(config={"data_dir": str(d_dir)})
            nx.mkdir("/workspace", exist_ok=True)
            nx.mkdir("/shared", exist_ok=True)
            nx.close()

        try:
            asyncio.run(_init_local_workspace())
        except ImportError:
            logger.debug("nexus package not available for local workspace init")
        except FileExistsError:
            pass  # Directories already created — expected on re-init
        except Exception:
            logger.warning("Failed to initialize local workspace", exc_info=True)
        _print_local_summary(cfg_path, d_dir)
    else:
        _print_shared_summary(config, cfg_path, d_dir)

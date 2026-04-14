"""Runtime state and project config management.

Centralizes nexus.yaml loading/saving and ``{data_dir}/.state.json``
read/write so every CLI command shares a single source of truth.

``nexus.yaml`` is the **declarative** project config (checked into git).
``.state.json`` is **ephemeral** runtime state (gitignored under data_dir):
resolved ports, active API key, image used, build mode, TLS paths.

Resolution order for any value:
  1. ``.state.json`` (runtime truth from last ``nexus up``)
  2. ``nexus.yaml`` (declarative defaults)
  3. Built-in defaults
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nexus.cli.theme import console

# ---------------------------------------------------------------------------
# Shared config search paths (single source of truth — was duplicated in
# stack.py, status.py, demo_data.py)
# ---------------------------------------------------------------------------

CONFIG_SEARCH_PATHS = ("./nexus.yaml", "./nexus.yml")

STATE_FILENAME = ".state.json"
STATE_VERSION = 1


# ---------------------------------------------------------------------------
# Project config (nexus.yaml) — declarative, version-controlled
# ---------------------------------------------------------------------------


def load_project_config() -> dict[str, Any]:
    """Load the project-local nexus.yaml.

    Searches ``CONFIG_SEARCH_PATHS`` in order.  Prints an error and
    exits if no config file is found.
    """
    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    console.print("[nexus.error]Error:[/nexus.error] No nexus.yaml found. Run `nexus init` first.")
    raise SystemExit(1)


def load_project_config_optional() -> dict[str, Any]:
    """Load nexus.yaml, returning an empty dict if not found."""
    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    return {}


def save_project_config(config: dict[str, Any], path: str | None = None) -> None:
    """Persist config back to nexus.yaml.

    Only ``nexus init`` and ``nexus upgrade`` should call this.
    ``nexus up`` writes runtime state to ``.state.json`` instead.
    """
    target = Path(path) if path else None
    if target is None:
        for candidate in CONFIG_SEARCH_PATHS:
            if Path(candidate).exists():
                target = Path(candidate)
                break
    if target is None:
        target = Path(CONFIG_SEARCH_PATHS[0])
    with open(target, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Runtime state ({data_dir}/.state.json) — ephemeral, gitignored
# ---------------------------------------------------------------------------


def load_runtime_state(data_dir: str | Path) -> dict[str, Any]:
    """Load ``{data_dir}/.state.json``.

    Returns an empty dict if the file does not exist or is malformed.
    """
    state_path = Path(data_dir) / STATE_FILENAME
    if not state_path.exists():
        return {}
    try:
        with open(state_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def save_runtime_state(data_dir: str | Path, state: dict[str, Any]) -> None:
    """Atomically write ``{data_dir}/.state.json``.

    Uses write-to-temp + ``os.replace()`` to prevent partial reads from
    concurrent worktrees or interrupted writes.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / STATE_FILENAME

    state["version"] = STATE_VERSION
    if "started_at" not in state:
        state["started_at"] = datetime.now(UTC).isoformat()

    fd, tmp_path = tempfile.mkstemp(dir=str(data_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, str(state_path))
    except BaseException:
        # Clean up temp file on any error
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def resolve_connection_env(
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the connection env var dict from config + runtime state.

    Used by ``nexus env``, ``nexus run``, and the ``nexus up`` output block.

    Resolution: state.json values win over nexus.yaml values.
    """
    if state is None:
        data_dir = config.get("data_dir", "./nexus-data")
        state = load_runtime_state(data_dir)

    ports = state.get("ports", config.get("ports", {}))
    api_key = state.get("api_key", config.get("api_key", ""))
    http_port = ports.get("http", 2026)
    grpc_port = ports.get("grpc", 2028)

    # NEXUS_URL is always http:// — the HTTP server does not serve TLS.
    # TLS is gRPC-only (mTLS for zone federation). The TLS env vars
    # (NEXUS_TLS_CERT/KEY/CA) are emitted separately for gRPC clients.
    env_vars: dict[str, str] = {
        "NEXUS_URL": f"http://localhost:{http_port}",
        "NEXUS_GRPC_HOST": f"localhost:{grpc_port}",
        "NEXUS_GRPC_PORT": str(grpc_port),
    }

    if api_key:
        env_vars["NEXUS_API_KEY"] = api_key

    # TLS paths for gRPC — prefer state.json (runtime-discovered), fall back to config.
    # Always emit NEXUS_GRPC_TLS so `eval $(nexus env)` clears stale overrides
    # when switching between TLS and plaintext stacks.
    tls = state.get("tls", {})
    if tls.get("cert"):
        env_vars["NEXUS_TLS_CERT"] = tls["cert"]
        env_vars["NEXUS_TLS_KEY"] = tls.get("key", "")
        env_vars["NEXUS_TLS_CA"] = tls.get("ca", "")
        env_vars["NEXUS_GRPC_TLS"] = "true"
    elif config.get("tls_cert"):
        env_vars["NEXUS_TLS_CERT"] = config["tls_cert"]
        env_vars["NEXUS_TLS_KEY"] = config.get("tls_key", "")
        env_vars["NEXUS_TLS_CA"] = config.get("tls_ca", "")
        # Emit NEXUS_GRPC_TLS=true only if cert files actually exist on disk
        import pathlib

        if pathlib.Path(config["tls_cert"]).exists():
            env_vars["NEXUS_GRPC_TLS"] = "true"
    else:
        # Non-TLS stack: clear any stale TLS override from a previous session
        env_vars["NEXUS_GRPC_TLS"] = "false"
        env_vars["NEXUS_TLS_CERT"] = ""
        env_vars["NEXUS_TLS_KEY"] = ""
        env_vars["NEXUS_TLS_CA"] = ""

    # DATABASE_URL if postgres is in the service list
    services = config.get("services", [])
    if "postgres" in services:
        pg_port = ports.get("postgres", 5432)
        env_vars["DATABASE_URL"] = f"postgresql://postgres:nexus@localhost:{pg_port}/nexus"

    return env_vars

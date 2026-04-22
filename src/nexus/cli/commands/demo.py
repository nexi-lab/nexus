"""Demo management commands — nexus demo init / reset.

Seeds a running Nexus instance with demo data: users, agents, files,
permissions, version history, audit events, and search corpus.

Seeding is idempotent by default.  A manifest file
(``<data-dir>/.demo-manifest.json``) tracks what was seeded and when.
``--reset`` tears down all demo data before re-seeding.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from nexus.cli.commands.demo_data import (
    CONFIG_SEARCH_PATHS,
    DEMO_AGENT_PERMISSIONS,
    DEMO_AGENTS,
    DEMO_DELEGATIONS,
    DEMO_DIRS,
    DEMO_FILES,
    DEMO_IPC_DEAD_LETTER,
    DEMO_IPC_MESSAGES,
    DEMO_IPC_PROCESSED,
    DEMO_LINEAGE,
    DEMO_PERMISSION_TUPLES,
    DEMO_USERS,
    DEMO_ZONES,
    HERB_CORPUS,
    MANIFEST_FILENAME,
    PLAN_VERSIONS,
)
from nexus.cli.theme import console
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# REST API client for demo seeding (Issue #3250)
#
# When a Nexus server is running, demo init should write files through the
# REST API so they appear in the server's VFS index. This client implements
# the minimal NexusFS interface used by the seeding functions.
# ---------------------------------------------------------------------------


class _RestApiNexusClient:
    """Minimal NexusFS-compatible client that writes via REST API.

    Implements: write, mkdir, access, read, sys_readdir, flush_write_observer.
    Used by demo seeding when a running server is detected.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        from nexus.cli.api_client import NexusApiClient

        self._client = NexusApiClient(url=base_url, api_key=api_key)
        self._base_url = base_url

    async def write(self, path: str, content: bytes) -> None:
        text = content.decode("utf-8", errors="replace")
        self._client.post("/api/v2/files/write", json_body={"path": path, "content": text})

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:  # noqa: ARG002
        try:
            self._client.post("/api/v2/files/mkdir", json_body={"path": path})
        except Exception:
            if not exist_ok:
                raise

    async def access(self, path: str) -> bool:
        try:
            self._client.get(f"/api/v2/files/metadata?path={path}")
            return True
        except Exception:
            return False

    async def read(self, path: str) -> bytes:
        result = self._client.get(f"/api/v2/files/read?path={path}")
        content = result.get("content", "") if isinstance(result, dict) else str(result)
        return content.encode("utf-8")

    def sys_readdir(self, path: str) -> list[str]:
        try:
            result = self._client.get(f"/api/v2/files/list?path={path}")
            items = result.get("items", []) if isinstance(result, dict) else []
            return [item.get("name", "") for item in items]
        except Exception:
            return []

    def flush_write_observer(self) -> None:
        pass  # REST writes are synchronous — no buffering


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_runtime_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve runtime connection info from state.json, falling back to config.

    Returns a dict with ``http_port``, ``grpc_port``, ``base_url``, and ``api_key``.
    All downstream demo helpers should use this instead of reading config["ports"]
    directly, because ``nexus up`` may have resolved to different ports.
    """
    from nexus.cli.state import load_runtime_state, resolve_connection_env

    data_dir = config.get("data_dir", "./nexus-data")
    state = load_runtime_state(data_dir)
    conn = resolve_connection_env(config, state)
    ports = state.get("ports", config.get("ports", {}))

    # Set env vars so downstream RPCTransport / SDK picks up TLS and port
    if conn.get("NEXUS_TLS_CERT"):
        os.environ["NEXUS_TLS_CERT"] = conn["NEXUS_TLS_CERT"]
        os.environ["NEXUS_TLS_KEY"] = conn.get("NEXUS_TLS_KEY", "")
        os.environ["NEXUS_TLS_CA"] = conn.get("NEXUS_TLS_CA", "")

    grpc_port = ports.get("grpc", 2028)
    os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)

    api_key = conn.get("NEXUS_API_KEY", config.get("api_key", ""))
    if not api_key:
        api_key = _resolve_admin_key(config)

    return {
        "http_port": ports.get("http", 2026),
        "grpc_port": grpc_port,
        "base_url": conn.get("NEXUS_URL", f"http://localhost:{ports.get('http', 2026)}"),
        "api_key": api_key,
    }


def _resolve_admin_key(config: dict[str, Any]) -> str:
    """Resolve the admin API key from state.json, config, or on-disk key file."""
    from nexus.cli.state import load_runtime_state

    data_dir: str = config.get("data_dir", "./nexus-data")
    state = load_runtime_state(data_dir)

    # State.json first (runtime truth), then nexus.yaml, then .admin-api-key file
    api_key: str = state.get("api_key", "")
    if not api_key:
        api_key = config.get("api_key", "")
    if not api_key:
        key_file = Path(data_dir) / ".admin-api-key"
        if key_file.exists():
            api_key = key_file.read_text().strip()
    return api_key


def _load_project_config() -> dict[str, Any]:
    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    console.print("[nexus.error]Error:[/nexus.error] No nexus.yaml found. Run `nexus init` first.")
    raise SystemExit(1)


def _manifest_path(data_dir: str) -> Path:
    return Path(data_dir) / MANIFEST_FILENAME


def _load_manifest(data_dir: str) -> dict[str, Any]:
    mp = _manifest_path(data_dir)
    if mp.exists():
        with open(mp) as f:
            result: dict[str, Any] = json.load(f)
            return result
    return {}


def _save_manifest(data_dir: str, manifest: dict[str, Any]) -> None:
    mp = _manifest_path(data_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    with open(mp, "w") as f:
        json.dump(manifest, f, indent=2)


async def _get_nexus_client(config: dict[str, Any]) -> Any:
    """Connect to a running Nexus server via gRPC, or fall back to local.

    For remote presets (shared/demo), reads runtime state from
    ``.state.json`` for the actual resolved ports and TLS paths,
    falling back to ``nexus.yaml`` config values.

    Raises on failure — does not silently fall back to a separate local instance
    when a remote preset is expected.
    """
    import nexus

    preset = config.get("preset", "local")

    if preset in ("shared", "demo"):
        from nexus.cli.state import load_runtime_state, resolve_connection_env

        data_dir = config.get("data_dir", "./nexus-data")
        state = load_runtime_state(data_dir)
        conn = resolve_connection_env(config, state)

        # Use runtime-resolved ports (from state.json) over config defaults
        http_port = state.get("ports", config.get("ports", {})).get("http", 2026)
        grpc_port = state.get("ports", config.get("ports", {})).get("grpc", 2028)

        # Set NEXUS_GRPC_PORT so nexus.connect() uses the right port
        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)

        # Set TLS env vars if available so RPCTransport picks them up
        if conn.get("NEXUS_TLS_CERT"):
            os.environ["NEXUS_TLS_CERT"] = conn["NEXUS_TLS_CERT"]
            os.environ["NEXUS_TLS_KEY"] = conn.get("NEXUS_TLS_KEY", "")
            os.environ["NEXUS_TLS_CA"] = conn.get("NEXUS_TLS_CA", "")

        api_key = _resolve_admin_key(config)

        # Use the scheme from resolve_connection_env (https if TLS)
        url = conn.get("NEXUS_URL", f"http://localhost:{http_port}")

        try:
            nx = nexus.connect(
                config={
                    "profile": "remote",
                    "url": url,
                    "api_key": api_key,
                }
            )
            # Verify connectivity with a lightweight read-only call.
            nx.sys_readdir("/")
            return nx
        except Exception as e:
            console.print(
                f"[nexus.error]Error:[/nexus.error] Could not connect to Nexus server: {e}"
            )
            console.print(
                f"[nexus.warning]Hint:[/nexus.warning] Is `nexus up` running? Expected gRPC on port {grpc_port}."
            )
            raise

    # Local preset — try running server first, fall back to local data dir.
    # Issue #3250: local NexusFS writes bypass the server's VFS index,
    # causing demo files to be invisible in the TUI/API. Always prefer
    # the server REST API when one is reachable.
    rt = _resolve_runtime_connection(config)
    base_url = rt["base_url"]
    api_key = rt["api_key"]

    try:
        from nexus.cli.api_client import NexusApiClient

        test_client = NexusApiClient(url=base_url, api_key=api_key)
        test_client.get("/healthz/ready")
        logger.info("Local server detected at %s — using REST API for demo seeding", base_url)
        return _RestApiNexusClient(base_url, api_key)
    except Exception:
        pass

    # No server running — fall back to local data dir
    data_dir = config.get("data_dir", "./nexus-data")
    return nexus.connect(config={"data_dir": data_dir})


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


async def _seed_files(
    nx: Any,
    manifest: dict[str, Any],
) -> int:
    """Seed demo files. Returns count of files created."""
    seeded = manifest.get("files", [])
    created = 0

    # Seed both core demo files and HERB-derived corpus
    all_files = list(DEMO_FILES) + list(HERB_CORPUS)
    for path, content, _description in all_files:
        if path in seeded:
            try:
                if nx.access(path):
                    continue
            except Exception:
                # Stale manifest entry; fall through and recreate the file.
                pass
        try:
            # Ensure parent directory exists
            parent = "/".join(path.split("/")[:-1])
            if parent:
                nx.mkdir(parent, parents=True, exist_ok=True)
            nx.write(path, content.encode())
            seeded.append(path)
            created += 1
        except Exception as e:
            console.print(f"  [nexus.warning]Warning:[/nexus.warning] Could not create {path}: {e}")

    manifest["files"] = seeded
    return created


async def _seed_versions(nx: Any, manifest: dict[str, Any]) -> int:
    """Create version history for plan.md. Returns count of versions created."""
    created = 0
    plan_path = "/workspace/demo/plan.md"
    if manifest.get("versions_seeded"):
        try:
            if nx.access(plan_path):
                return 0
        except Exception:
            pass
    for version_content in PLAN_VERSIONS:
        try:
            nx.write(plan_path, version_content.encode())
            created += 1
        except Exception:
            break

    # Write the final version (from DEMO_FILES)
    final = next((c for p, c, _ in DEMO_FILES if p == plan_path), None)
    if final:
        try:
            nx.write(plan_path, final.encode())
            created += 1
        except Exception:
            pass

    manifest["versions_seeded"] = True
    return created


async def _seed_directories(nx: Any) -> int:
    """Create base demo directories. Returns count created."""
    created = 0
    for d in DEMO_DIRS:
        try:
            nx.mkdir(d, parents=True, exist_ok=True)
            created += 1
        except Exception:
            pass
    return created


def _seed_permissions(nx: Any, config: dict[str, Any], manifest: dict[str, Any]) -> int:
    """Seed demo ReBAC permissions.

    For remote presets (shared/demo), uses ``docker compose exec`` to run
    a script inside the container that writes tuples via ReBACManager.
    This works with any server image version (no new RPC required).
    Falls back to the ``admin_write_permission`` RPC for non-Docker
    remote deployments.  For local presets, accesses rebac_manager directly.

    Writes relationship tuples for the demo workspace:
    - admin gets direct_owner on /workspace/demo
    - demo_user gets viewer on /workspace/demo
    - demo_agent gets editor on /workspace/demo
    """
    if manifest.get("permissions_seeded"):
        return 0

    tuples = DEMO_PERMISSION_TUPLES + DEMO_AGENT_PERMISSIONS
    preset = config.get("preset", "local")

    if preset in ("shared", "demo"):
        # Primary: docker compose exec (works with any server image)
        created = _seed_permissions_docker(config, tuples)
        if created < 0:
            # Fallback: admin RPC (requires server built from this branch)
            created = _seed_permissions_rpc(config, tuples)
    else:
        # Local path — rebac_manager via ServiceRegistry
        rebac = nx.service("rebac") if nx else None
        if rebac is None:
            logger.debug("No rebac_manager available — skipping permission seeding")
            manifest["permissions_seeded"] = True
            manifest["permissions_count"] = 0
            return 0

        created = 0
        for t in tuples:
            try:
                rebac.rebac_write(
                    subject=tuple(t["subject"]),
                    relation=t["relation"],
                    object=tuple(t["object"]),
                    zone_id=t["zone_id"],
                )
                created += 1
            except Exception as e:
                logger.debug("Could not seed permission %s: %s", t, e)

    manifest["permissions_seeded"] = True
    manifest["permissions_count"] = max(created, 0)
    return max(created, 0)


# Python script executed inside the Docker container via ``docker compose exec``.
# It creates a standalone ReBACManager connected to the container's database
# and writes the permission tuples.  This approach works with any server image
# because it uses the container's own installed packages — no new RPC needed.
_DOCKER_SEED_SCRIPT = """\
import json, os, sys
sys.path.insert(0, '/app/src')
db_url = os.environ.get('NEXUS_DATABASE_URL', '')
if not db_url:
    print('0')
    sys.exit(0)
try:
    from sqlalchemy import create_engine
    from nexus.bricks.rebac.manager import ReBACManager
    engine = create_engine(db_url)
    mgr = ReBACManager(engine=engine, is_postgresql=not db_url.startswith('sqlite'))
    created = 0
    for s, r, o, z in json.loads(sys.stdin.read()):
        try:
            mgr.rebac_write(subject=tuple(s), relation=r, object=tuple(o), zone_id=z)
            created += 1
        except Exception:
            pass
    print(created)
except Exception:
    print('0')
"""


def _seed_permissions_docker(config: dict[str, Any], tuples: list[dict[str, Any]]) -> int:
    """Seed permissions by executing a script inside the Docker container.

    Returns the number of tuples created, or -1 if docker exec is unavailable.
    """
    compose_file = config.get("compose_file", "")
    if not compose_file or not Path(compose_file).exists():
        return -1

    # Locate docker compose binary
    compose_cmd = _find_compose_cmd()
    if compose_cmd is None:
        return -1

    # Build compose project env so we target the correct stack
    from nexus.cli.commands.stack import _derive_project_env

    compose_env = _derive_project_env(config)

    # Serialise tuples as [[subject, relation, object, zone_id], ...]
    payload = json.dumps(
        [[t["subject"], t["relation"], t["object"], t.get("zone_id", ROOT_ZONE_ID)] for t in tuples]
    )

    cmd = [
        *compose_cmd,
        "-f",
        compose_file,
        "exec",
        "-T",
        "nexus",
        "python3",
        "-c",
        _DOCKER_SEED_SCRIPT,
    ]
    env = {**os.environ, **compose_env}

    try:
        result = subprocess.run(
            cmd,
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
        logger.debug("docker exec failed (rc=%d): %s", result.returncode, result.stderr)
        return -1
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        logger.debug("docker exec error: %s", e)
        return -1


def _seed_permissions_rpc(config: dict[str, Any], tuples: list[dict[str, Any]]) -> int:
    """Write permission tuples via the admin_write_permission RPC.

    Fallback for non-Docker remote deployments where docker exec is
    unavailable but the server has the admin_write_permission handler.
    """
    rt = _resolve_runtime_connection(config)
    api_key = rt["api_key"]

    if not api_key:
        logger.debug("No admin API key — skipping permission seeding via RPC")
        return 0

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        call_rpc = get_admin_rpc(rt["base_url"], api_key)
        result = call_rpc("admin_write_permission", {"tuples": tuples})
        created: int = result.get("created", 0) if isinstance(result, dict) else 0
        return created
    except Exception as e:
        logger.debug("Could not seed permissions via RPC: %s", e)
        return 0


def _find_compose_cmd() -> list[str] | None:
    """Return docker compose command prefix, or None if unavailable."""
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _seed_identities(config: dict[str, Any], manifest: dict[str, Any]) -> int:
    """Provision demo users and agents via the admin RPC.

    Uses the same ``admin_create_key`` RPC as ``nexus admin create-user``
    so that the identities are registered in the database and can
    authenticate with their own API keys.

    Returns the number of identities created.
    """
    if manifest.get("identities_seeded"):
        return 0

    # Only applicable for presets that have database auth
    if config.get("auth") not in ("database",):
        manifest["identities_seeded"] = True
        return 0

    rt = _resolve_runtime_connection(config)
    api_key = rt["api_key"]

    if not api_key:
        logger.debug("No admin API key available — skipping identity seeding")
        return 0

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        call_rpc = get_admin_rpc(rt["base_url"], api_key)
    except Exception as e:
        logger.debug("Could not connect admin RPC for identity seeding: %s", e)
        return 0

    created = 0
    identity_keys: dict[str, dict[str, str]] = {}

    # Provision users (skip "admin" — already created by entrypoint)
    for user in DEMO_USERS:
        if user["id"] == "admin":
            continue
        try:
            result = call_rpc(
                "admin_create_key",
                {
                    "user_id": user["id"],
                    "name": user["display_name"],
                    "is_admin": False,
                    "zone_id": "root",
                    "subject_type": "user",
                },
            )
            identity_keys[user["id"]] = {
                "api_key": result.get("api_key", ""),
                "key_id": result.get("key_id", ""),
            }
            created += 1
        except Exception as e:
            logger.debug("Could not create user %s: %s", user["id"], e)

    # Provision agents
    for agent in DEMO_AGENTS:
        try:
            result = call_rpc(
                "admin_create_key",
                {
                    "user_id": agent["id"],
                    "name": agent["display_name"],
                    "is_admin": False,
                    "zone_id": "root",
                    "subject_type": "agent",
                },
            )
            identity_keys[agent["id"]] = {
                "api_key": result.get("api_key", ""),
                "key_id": result.get("key_id", ""),
            }
            created += 1
        except Exception as e:
            logger.debug("Could not create agent %s: %s", agent["id"], e)

    manifest["identities_seeded"] = True
    manifest["identity_keys"] = identity_keys
    return created


def _seed_zones(config: dict[str, Any], manifest: dict[str, Any]) -> int:
    """Seed demo zones and populate them with data.

    Creates a 'research' zone with HERB employee/product data, separate
    from customer data in the root zone. This enables cross-zone semantic
    search demos.
    """
    if manifest.get("zones_seeded"):
        return 0

    import urllib.request

    rt = _resolve_runtime_connection(config)
    base_url = rt["base_url"]
    admin_key = rt["api_key"]
    if not admin_key:
        return 0

    created = 0
    for zone in DEMO_ZONES:
        # Create zone record in DB via direct SQL (zone creation API requires
        # DatabaseLocalAuth which may not be configured)
        try:
            # Use the files/write endpoint with zone header to seed data
            # First check if zone already exists in the zones list
            req = urllib.request.Request(
                f"{base_url}/api/zones",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                zones_data = json.loads(resp.read())
                existing_ids = [z.get("zone_id") for z in zones_data.get("zones", [])]
                if zone["zone_id"] in existing_ids:
                    created += 1
                    continue
        except Exception:
            pass

        # Insert zone record via docker exec on the postgres container.
        # The zone metadata record needs to exist in the zones table first.
        # Use docker exec to insert if in Docker mode, otherwise skip.
        # Insert zone record via docker exec on the postgres container.
        # Find the postgres container by name pattern (handles any compose project).
        preset = config.get("preset", "local")
        if preset in ("shared", "demo"):
            try:
                # Find postgres container for this stack
                # Find postgres container matching our stack's port
                from nexus.cli.state import load_runtime_state

                _state = load_runtime_state(config.get("data_dir", "./nexus-data"))
                _ports = _state.get("ports", config.get("ports", {}))
                pg_port = str(_ports.get("postgres", 5433))
                ps_result = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "--filter",
                        "name=postgres",
                        "--filter",
                        f"publish={pg_port}",
                        "--format",
                        "{{.Names}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                pg_containers = [
                    c.strip() for c in ps_result.stdout.strip().split("\n") if c.strip()
                ]
                if not pg_containers:
                    # Fallback: any postgres container
                    ps_result = subprocess.run(
                        ["docker", "ps", "--filter", "name=postgres", "--format", "{{.Names}}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    pg_containers = [
                        c.strip() for c in ps_result.stdout.strip().split("\n") if c.strip()
                    ]
                pg_container = pg_containers[0] if pg_containers else None
                if pg_container:
                    insert_sql = (
                        f"INSERT INTO zones (zone_id, name, description, phase, finalizers, created_at, updated_at) "
                        f"VALUES ('{zone['zone_id']}', '{zone['name']}', "
                        f"'{zone['description'][:200]}', 'Active', '[]', NOW(), NOW()) "
                        f"ON CONFLICT (zone_id) DO NOTHING;"
                    )
                    result = subprocess.run(
                        [
                            "docker",
                            "exec",
                            pg_container,
                            "psql",
                            "-U",
                            "postgres",
                            "-d",
                            "nexus",
                            "-c",
                            insert_sql,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        created += 1
                        logger.debug(
                            "Created zone '%s' via docker exec on %s", zone["zone_id"], pg_container
                        )
            except Exception as e:
                logger.debug("Could not create zone via docker: %s", e)

    # Seed HERB employees + products into /workspace/research/
    # In standalone mode, all files live in the same backend — zone isolation
    # is enforced via ReBAC permissions, not storage boundaries.
    research_files_written = 0
    if created > 0:
        # Create research directories with zone header.
        # Use parents=false to avoid retagging /workspace (root zone) to research.
        for dirname in [
            "/workspace/research",
            "/workspace/research/employees",
            "/workspace/research/products",
        ]:
            try:
                body = json.dumps({"path": dirname, "parents": False}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/v2/files/mkdir",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {admin_key}",
                        "Content-Type": "application/json",
                        "X-Nexus-Zone-ID": "research",
                    },
                    data=body,
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass

        herb_research_files = [
            item
            for item in HERB_CORPUS
            if "/herb/employees/" in item[0] or "/herb/products/" in item[0]
        ]
        for path, content, _desc in herb_research_files:
            research_path = path.replace("/workspace/demo/herb/", "/workspace/research/")
            try:
                body = json.dumps({"path": research_path, "content": content}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/v2/files/write",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {admin_key}",
                        "Content-Type": "application/json",
                        "X-Nexus-Zone-ID": "research",
                    },
                    data=body,
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        research_files_written += 1
            except Exception as e:
                logger.debug("Could not write %s: %s", research_path, e)
        logger.debug("Wrote %d files to /workspace/research/", research_files_written)

    # Only mark as seeded if we actually created zones — otherwise future
    # runs will skip zone seeding even if the docker path was unavailable.
    if created > 0:
        manifest["zones_seeded"] = True
    return created


def _seed_agent_coordination(config: dict[str, Any], manifest: dict[str, Any]) -> dict[str, int]:
    """Seed agent coordination scenario: IPC provisioning, delegations, messages.

    Uses REST API calls with the agents' own API keys so that delegation
    and IPC endpoints accept the requests (they require subject_type=agent).

    Returns counts of provisioned/delegated/messaged items.
    """
    if manifest.get("agent_coordination_seeded"):
        return {"provisioned": 0, "delegated": 0, "messages": 0}

    import urllib.request

    rt = _resolve_runtime_connection(config)
    base_url = rt["base_url"]
    admin_key = rt["api_key"]
    if not admin_key:
        logger.debug("No admin API key — skipping agent coordination seeding")
        return {"provisioned": 0, "delegated": 0, "messages": 0}

    identity_keys = manifest.get("identity_keys", {})

    # 1. Provision IPC for coordinator (top-level agent only)
    provisioned = 0
    try:
        req = urllib.request.Request(
            f"{base_url}/api/v2/ipc/provision/coordinator",
            method="POST",
            headers={
                "Authorization": f"Bearer {admin_key}",
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                provisioned += 1
    except Exception as e:
        logger.debug("Could not provision IPC for coordinator: %s", e)

    # 2. Delegate: coordinator → researcher, coordinator → coder
    #    Delegation creates the worker agents (API keys + grants + IPC dirs).
    #    researcher and coder are NOT top-level registered agents — they exist
    #    only via delegation from coordinator.
    delegated = 0
    worker_keys: dict[str, str] = {}
    coordinator_key = identity_keys.get("coordinator", {}).get("api_key", "")
    if coordinator_key:
        for deleg in DEMO_DELEGATIONS:
            try:
                body = json.dumps(deleg).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/v2/agents/delegate",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {coordinator_key}",
                        "Content-Type": "application/json",
                    },
                    data=body,
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        result = json.loads(resp.read())
                        delegated += 1
                        worker_id = str(deleg["worker_id"])
                        api_key = result.get("api_key", "")
                        if api_key:
                            worker_keys[worker_id] = api_key
                        # Provision IPC for the delegated worker
                        try:
                            prov_req = urllib.request.Request(
                                f"{base_url}/api/v2/ipc/provision/{worker_id}",
                                method="POST",
                                headers={
                                    "Authorization": f"Bearer {admin_key}",
                                    "Content-Type": "application/json",
                                },
                                data=b"{}",
                            )
                            with urllib.request.urlopen(prov_req, timeout=10):
                                provisioned += 1
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Could not create delegation for %s: %s", deleg["worker_id"], e)
    else:
        logger.debug("No coordinator API key — skipping delegations")

    # 3. Seed IPC messages via /api/v2/ipc/send (inbox messages)
    messages_sent = 0
    for msg in DEMO_IPC_MESSAGES:
        send_body = {
            "sender": msg["sender"],
            "recipient": msg["recipient"],
            "type": msg["type"],
            "payload": msg["payload"],
        }
        if msg.get("correlation_id"):
            send_body["correlation_id"] = msg["correlation_id"]
        try:
            body = json.dumps(send_body).encode()
            req = urllib.request.Request(
                f"{base_url}/api/v2/ipc/send",
                method="POST",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                data=body,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    messages_sent += 1
        except Exception as e:
            logger.debug("Could not send IPC message: %s", e)

    # 4. Seed processed messages (consumed by agent — shows delivery lifecycle)
    for msg in DEMO_IPC_PROCESSED:
        recipient = msg["recipient"]
        corr_id = msg.get("correlation_id", f"processed-{messages_sent}")
        sender = msg["sender"]
        filename = f"{corr_id}-from-{sender}-processed.json"
        file_path = f"/agents/{recipient}/processed/{filename}"
        content = json.dumps(msg, indent=2) + "\n"
        try:
            body = json.dumps({"path": file_path, "content": content}).encode()
            req = urllib.request.Request(
                f"{base_url}/api/v2/ipc/send",
                method="POST",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                data=body,
            )
            # ipc/send only writes to inbox — write processed via sys_write
            # by sending a task that the "agent already handled"
            # Actually, write directly: processed/ messages are just files
            req2 = urllib.request.Request(
                f"{base_url}/api/v2/files/write",
                method="POST",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps({"path": file_path, "content": content}).encode(),
            )
            with urllib.request.urlopen(req2, timeout=10) as resp:
                if resp.status == 200:
                    messages_sent += 1
        except Exception as e:
            logger.debug("Could not write processed message to %s: %s", file_path, e)

    # 5. Seed dead_letter messages (expired — shows error handling)
    for msg in DEMO_IPC_DEAD_LETTER:
        recipient = msg["recipient"]
        corr_id = msg.get("correlation_id", f"dead-{messages_sent}")
        sender = msg["sender"]
        filename = f"{corr_id}-from-{sender}-expired.json"
        file_path = f"/agents/{recipient}/dead_letter/{filename}"
        content = json.dumps(msg, indent=2) + "\n"
        try:
            body = json.dumps({"path": file_path, "content": content}).encode()
            req = urllib.request.Request(
                f"{base_url}/api/v2/files/write",
                method="POST",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                data=body,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    messages_sent += 1
        except Exception as e:
            logger.debug("Could not write dead_letter message to %s: %s", file_path, e)

    manifest["agent_coordination_seeded"] = True
    return {"provisioned": provisioned, "delegated": delegated, "messages": messages_sent}


def _revoke_identities(config: dict[str, Any], manifest: dict[str, Any]) -> int:
    """Revoke API keys created by ``_seed_identities``.

    Reads ``key_id`` values from the manifest's ``identity_keys`` and
    calls ``admin_revoke_key`` via the admin RPC.
    """
    identity_keys = manifest.get("identity_keys", {})
    if not identity_keys:
        return 0

    rt = _resolve_runtime_connection(config)
    api_key = rt["api_key"]
    if not api_key:
        return 0

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        call_rpc = get_admin_rpc(rt["base_url"], api_key)
    except Exception:
        return 0

    revoked = 0
    for _identity_id, key_info in identity_keys.items():
        key_id = key_info.get("key_id", "") if isinstance(key_info, dict) else ""
        if not key_id:
            continue
        try:
            call_rpc("admin_revoke_key", {"key_id": key_id})
            revoked += 1
        except Exception as e:
            logger.debug("Could not revoke key %s: %s", key_id, e)

    return revoked


async def _delete_demo_files(nx: Any, manifest: dict[str, Any]) -> int:
    """Delete all demo files tracked in the manifest. Returns count deleted."""
    files = manifest.get("files", [])
    removed = 0

    # Delete files in reverse order (deepest first)
    for path in reversed(files):
        try:
            nx.sys_unlink(path)
            removed += 1
        except Exception:
            pass

    # Delete directories in reverse order (deepest first)
    for d in reversed(DEMO_DIRS):
        with contextlib.suppress(Exception):
            nx.rmdir(d)

    return removed


# Python script executed inside the Docker container to delete permission tuples.
_DOCKER_DELETE_PERMS_SCRIPT = """\
import json, os, sys
sys.path.insert(0, '/app/src')
db_url = os.environ.get('NEXUS_DATABASE_URL', '')
if not db_url:
    print('0')
    sys.exit(0)
try:
    from sqlalchemy import create_engine, text
    engine = create_engine(db_url)
    deleted = 0
    for s, r, o, z in json.loads(sys.stdin.read()):
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        "DELETE FROM rebac_tuples "
                        "WHERE subject_type = :st AND subject_id = :si "
                        "AND relation = :rel "
                        "AND object_type = :ot AND object_id = :oi "
                        "AND zone_id = :zid"
                    ),
                    {"st": s[0], "si": s[1], "rel": r, "ot": o[0], "oi": o[1], "zid": z},
                )
                conn.commit()
                deleted += result.rowcount
        except Exception:
            pass
    print(deleted)
except Exception:
    print('0')
"""


def _delete_permissions_docker(config: dict[str, Any]) -> int:
    """Delete demo permission tuples via docker compose exec.

    Returns the number of tuples deleted, or -1 if docker exec is unavailable.
    """
    compose_file = config.get("compose_file", "")
    if not compose_file or not Path(compose_file).exists():
        return -1

    compose_cmd = _find_compose_cmd()
    if compose_cmd is None:
        return -1

    from nexus.cli.commands.stack import _derive_project_env

    compose_env = _derive_project_env(config)

    tuples = DEMO_PERMISSION_TUPLES
    payload = json.dumps(
        [[t["subject"], t["relation"], t["object"], t.get("zone_id", ROOT_ZONE_ID)] for t in tuples]
    )

    cmd = [
        *compose_cmd,
        "-f",
        compose_file,
        "exec",
        "-T",
        "nexus",
        "python3",
        "-c",
        _DOCKER_DELETE_PERMS_SCRIPT,
    ]
    env = {**os.environ, **compose_env}

    try:
        result = subprocess.run(
            cmd,
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
        logger.debug(
            "docker exec (delete perms) failed (rc=%d): %s", result.returncode, result.stderr
        )
        return -1
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        logger.debug("docker exec (delete perms) error: %s", e)
        return -1


def _delete_permissions(nx: Any, config: dict[str, Any]) -> int:
    """Delete demo ReBAC permission tuples. Returns count deleted."""
    preset = config.get("preset", "local")

    if preset in ("shared", "demo"):
        deleted = _delete_permissions_docker(config)
        return max(deleted, 0)

    # Local path — rebac_manager via ServiceRegistry (Issue #1771)
    rebac = nx.service("rebac_manager") if nx else None
    if rebac is None:
        return 0

    deleted = 0
    for t in DEMO_PERMISSION_TUPLES:
        try:
            # Use the sync delete_tuple if available (ReBACManager)
            if hasattr(rebac, "delete_tuple"):
                if rebac.delete_tuple(
                    subject=tuple(t["subject"]),
                    relation=t["relation"],
                    object=tuple(t["object"]),
                    zone_id=t["zone_id"],
                ):
                    deleted += 1
            elif hasattr(rebac, "rebac_delete_by_subject"):
                # Fallback: delete by subject (coarser)
                deleted += rebac.rebac_delete_by_subject(
                    subject_type=t["subject"][0],
                    subject_id=t["subject"][1],
                    zone_id=t["zone_id"],
                )
        except Exception as e:
            logger.debug("Could not delete permission %s: %s", t, e)

    return deleted


def _seed_catalog(nx: Any, config: dict[str, Any], manifest: dict[str, Any]) -> int:  # noqa: ARG001
    """Extract and store catalog schemas for data files.

    Calls the catalog REST API to extract schemas from CSV/JSON demo files.
    Returns count of schemas extracted.
    """
    if manifest.get("schemas_extracted"):
        return 0

    from nexus.cli.api_client import NexusApiClient

    rt = _resolve_runtime_connection(config)
    client = NexusApiClient(url=rt["base_url"], api_key=rt["api_key"])

    data_files = [
        "/workspace/demo/data/sales.csv",
        "/workspace/demo/data/metrics.json",
        "/workspace/demo/data/sample.json",
    ]

    extracted = 0
    for path in data_files:
        try:
            encoded = path.lstrip("/").replace("/", "%2F")
            client.get(f"/api/v2/catalog/schema/{encoded}")
            extracted += 1
        except Exception as e:
            logger.debug("Could not extract schema for %s: %s", path, e)

    manifest["schemas_extracted"] = extracted > 0
    manifest["schemas_count"] = extracted
    return extracted


def _seed_aspects(nx: Any, config: dict[str, Any], manifest: dict[str, Any]) -> int:  # noqa: ARG001
    """Attach governance aspects to restricted files.

    Calls the aspects REST API to attach a governance.classification aspect
    to the restricted/internal.md file.
    Returns count of aspects created.
    """
    if manifest.get("aspects_created"):
        return 0

    from nexus.cli.api_client import NexusApiClient
    from nexus.contracts.urn import NexusURN

    rt = _resolve_runtime_connection(config)
    client = NexusApiClient(url=rt["base_url"], api_key=rt["api_key"])

    aspects_to_seed = [
        (
            "/workspace/demo/restricted/internal.md",
            "governance.classification",
            {
                "level": "restricted",
                "owner": "admin",
                "reason": "Contains confidential operational data",
                "review_date": "2026-06-01",
            },
        ),
    ]

    created = 0
    for path, aspect_name, payload in aspects_to_seed:
        try:
            urn = str(NexusURN.for_file("root", path))
            encoded_urn = urn.replace(":", "%3A")
            encoded_name = aspect_name.replace(".", "%2E")
            client.put(
                f"/api/v2/aspects/{encoded_urn}/{encoded_name}",
                json_body={"payload": payload, "created_by": "demo_seed"},
            )
            created += 1
        except Exception as e:
            logger.debug("Could not seed aspect %s on %s: %s", aspect_name, path, e)

    manifest["aspects_created"] = created > 0
    manifest["aspects_count"] = created
    return created


def _seed_lineage(
    nx: Any,  # noqa: ARG001
    config: dict[str, Any],
    manifest: dict[str, Any],
) -> int:
    """Seed agent lineage data for demo files (Issue #3417).

    Calls the lineage REST API to declare upstream dependencies for
    demo output files, demonstrating the agent lineage tracking feature.
    Returns count of lineage entries created.
    """
    if manifest.get("lineage_created"):
        return 0

    from nexus.cli.api_client import NexusApiClient
    from nexus.contracts.urn import NexusURN

    rt = _resolve_runtime_connection(config)
    client = NexusApiClient(url=rt["base_url"], api_key=rt["api_key"])

    created = 0
    for output_path, upstream_inputs, agent_id in DEMO_LINEAGE:
        try:
            urn = str(NexusURN.for_file("root", output_path))
            encoded_urn = urn.replace(":", "%3A")
            client.put(
                f"/api/v2/lineage/{encoded_urn}",
                json_body={
                    "upstream": [
                        {
                            "path": u["path"],
                            "version": u.get("version", 1),
                            "etag": u.get("etag", ""),
                        }
                        for u in upstream_inputs
                    ],
                    "agent_id": agent_id,
                },
            )
            created += 1
        except Exception as e:
            logger.debug("Could not seed lineage for %s: %s", output_path, e)

    manifest["lineage_created"] = created > 0
    manifest["lineage_count"] = created
    return created


# ---------------------------------------------------------------------------
# Semantic search initialization
# ---------------------------------------------------------------------------

_DOCKER_PGVECTOR_SCRIPT = (
    "psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-nexus} "
    "-c 'CREATE EXTENSION IF NOT EXISTS vector;'"
)


def _ensure_pgvector_extension(config: dict[str, Any]) -> bool:
    """Create pgvector extension via docker exec on the postgres container.

    Returns True if the extension was created (or already existed).
    """
    compose_file = config.get("compose_file", "")
    if not compose_file:
        return False

    compose_cmd = ["docker", "compose"] if shutil.which("docker") else ["docker-compose"]
    from nexus.cli.commands.stack import _derive_project_env

    compose_env = _derive_project_env(config)

    cmd = [
        *compose_cmd,
        "-f",
        compose_file,
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        _DOCKER_PGVECTOR_SCRIPT,
    ]
    env = {**os.environ, **compose_env}

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("docker exec (pgvector) error: %s", e)
        return False


_DOCKER_SEED_CHUNKS_SCRIPT = """\
import json, os, sys, uuid
from datetime import datetime, timezone
db_url = os.environ.get('NEXUS_DATABASE_URL', '')
if not db_url:
    print('0')
    sys.exit(0)
try:
    from sqlalchemy import create_engine, text
    engine = create_engine(db_url)
    docs = json.loads(sys.stdin.read())
    inserted = 0
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        for doc in docs:
            path = doc['path']
            content = doc['content']
            size = len(content.encode('utf-8'))
            # Check if file_paths entry already exists
            row = conn.execute(text(
                "SELECT path_id FROM file_paths "
                "WHERE zone_id = 'root' AND virtual_path = :vp AND deleted_at IS NULL"
            ), {"vp": path}).fetchone()
            if row:
                path_id = row[0]
            else:
                path_id = str(uuid.uuid4())
                conn.execute(text(
                    "INSERT INTO file_paths "
                    "(path_id, zone_id, virtual_path, backend_id, physical_path, "
                    " size_bytes, created_at, updated_at, current_version) "
                    "VALUES (:pid, 'root', :vp, 'demo', :vp, :sz, :now, :now, 1)"
                ), {"pid": path_id, "vp": path, "sz": size, "now": now})
            # Delete old chunks for this path_id
            conn.execute(text(
                "DELETE FROM document_chunks WHERE path_id = :pid"
            ), {"pid": path_id})
            # Insert content as a single chunk
            chunk_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO document_chunks "
                "(chunk_id, path_id, chunk_index, chunk_text, chunk_tokens, "
                " start_offset, end_offset, line_start, line_end, created_at) "
                "VALUES (:cid, :pid, 0, :txt, :tokens, 0, :end, 1, :lines, :now)"
            ), {
                "cid": chunk_id, "pid": path_id,
                "txt": content, "tokens": len(content.split()),
                "end": len(content), "lines": content.count(chr(10)) + 1,
                "now": now,
            })
            inserted += 1
        conn.commit()
    print(inserted)
except Exception as e:
    print(f'0 error: {e}', file=sys.stderr)
    print('0')
"""


async def _seed_search_chunks_docker(nx: Any, config: dict[str, Any]) -> bool:
    """Seed document_chunks by executing a script inside the Docker container.

    Reads demo file content via RPC, then inserts file_paths + document_chunks
    entries via docker exec into the PostgreSQL database.
    """
    compose_file = config.get("compose_file", "")
    if not compose_file or not Path(compose_file).exists():
        return False

    compose_cmd = _find_compose_cmd()
    if compose_cmd is None:
        return False

    from nexus.cli.commands.stack import _derive_project_env

    compose_env = _derive_project_env(config)

    # Read file contents via RPC (include HERB corpus for semantic search)
    docs = []
    all_files = list(DEMO_FILES) + list(HERB_CORPUS)
    for path, _content, _desc in all_files:
        try:
            raw = nx.sys_read(path)
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            if text.strip():
                docs.append({"path": path, "content": text})
        except Exception:
            pass

    if not docs:
        return False

    payload = json.dumps(docs)
    cmd = [
        *compose_cmd,
        "-f",
        compose_file,
        "exec",
        "-T",
        "nexus",
        "python3",
        "-c",
        _DOCKER_SEED_CHUNKS_SCRIPT,
    ]
    env = {**os.environ, **compose_env}

    try:
        result = subprocess.run(
            cmd,
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        count = int(result.stdout.strip().split()[0]) if result.stdout.strip() else 0
        logger.debug("Seeded %d document chunks (stderr: %s)", count, result.stderr.strip())
        return count > 0
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        logger.debug("docker exec (search chunks) error: %s", e)
        return False


async def _init_semantic_search(nx: Any, config: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Initialize semantic search by triggering the real indexing pipeline.

    Attempts to index demo files through the server's semantic_search_index
    RPC, which runs the full embedding pipeline (embeddings → pgvector HNSW).
    Falls back to direct document_chunks insertion via docker exec if the
    RPC-based pipeline is unavailable.

    Records the engine used in the manifest so tests can verify which path ran.

    Returns True if semantic search is ready (by either path).
    """
    preset = config.get("preset", "local")
    if preset not in ("shared", "demo"):
        return False

    _ensure_pgvector_extension(config)

    # Try the real indexing pipeline first (Issue #2961: use real embeddings)
    try:
        search_svc = nx.service("search")
        results = search_svc.semantic_search_index("/workspace/demo", recursive=True)
        # RPC handler wraps results as {"indexed": {path: count, ...}, ...}
        if isinstance(results, dict) and "indexed" in results:
            indexed_map = results["indexed"]
            total_chunks = results.get("total_chunks", 0)
            indexed = sum(1 for v in indexed_map.values() if isinstance(v, int) and v > 0)
        else:
            # Direct call (non-RPC) returns dict[str, int]
            indexed_map = results
            indexed = sum(1 for v in results.values() if isinstance(v, int) and v > 0)
            total_chunks = sum(v for v in results.values() if isinstance(v, int) and v > 0)
        if indexed > 0:
            logger.info(
                "Semantic search: indexed %d files (%d chunks) via real pipeline",
                indexed,
                total_chunks,
            )
            manifest["semantic_engine"] = "vector"
            manifest["semantic_indexed_files"] = indexed
            return True
    except Exception as e:
        logger.debug("Real indexing pipeline unavailable: %s", e)

    # Fallback: insert document_chunks directly for SQL-based text search
    logger.info("Falling back to direct document_chunks insertion (SQL text search)")
    manifest["semantic_engine"] = "sql_fallback"
    return await _seed_search_chunks_docker(nx, config)


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.group(name="demo")
def demo() -> None:
    """Manage demo data.

    Seed a running Nexus instance with sample users, files, permissions,
    version history, audit events, and search corpus.
    """
    pass


@demo.command(name="init")
@click.option("--reset", is_flag=True, default=False, help="Delete all demo data before seeding.")
@click.option(
    "--skip-semantic", is_flag=True, default=False, help="Skip semantic search corpus seeding."
)
def demo_init(reset: bool, skip_semantic: bool) -> None:
    """Seed demo data into a running Nexus instance.

    Idempotent by default — safe to run multiple times.
    Use --reset to tear down existing demo data first.

    Examples:
        nexus demo init                # seed data (idempotent)
        nexus demo init --reset        # clean slate re-seed
        nexus demo init --skip-semantic
    """
    import asyncio

    asyncio.run(_async_demo_init(reset, skip_semantic))


async def _async_demo_init(reset: bool, skip_semantic: bool) -> None:
    config = _load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")

    # Connect to Nexus
    try:
        nx = await _get_nexus_client(config)
    except Exception as e:
        console.print(f"[nexus.error]Error:[/nexus.error] Could not connect to Nexus: {e}")
        console.print(
            "[nexus.warning]Hint:[/nexus.warning] Is the server running? Try `nexus up` first."
        )
        raise SystemExit(1) from e

    # Load or reset manifest
    if reset:
        old_manifest = _load_manifest(data_dir)
        if old_manifest:
            console.print("[nexus.warning]Resetting demo data...[/nexus.warning]")
            revoked = _revoke_identities(config, old_manifest)
            if revoked:
                console.print(f"  Revoked {revoked} identity API keys.")
            perms_del = _delete_permissions(nx, config)
            if perms_del:
                console.print(f"  Deleted {perms_del} permission tuples.")
            removed = await _delete_demo_files(nx, old_manifest)
            console.print(f"  Removed {removed} files.")
        manifest: dict[str, Any] = {}
    else:
        manifest = _load_manifest(data_dir)

    console.print("[bold]Seeding Nexus demo data...[/bold]")
    console.print()

    # 1. Create directories
    await _seed_directories(nx)

    # 2. Seed files
    files_created = await _seed_files(nx, manifest)
    total_files = len(manifest.get("files", []))
    console.print(f"  Files:        {total_files} ({files_created} new)")

    # 3. Seed version history
    versions_created = await _seed_versions(nx, manifest)

    # Flush the async write observer so version records are committed to the
    # database before any subsequent query (e.g. `nexus versions history`).
    # Without this, the RecordStoreWriteObserver may not have flushed yet.
    try:
        if hasattr(nx, "flush_write_observer"):
            nx.flush_write_observer()
    except Exception:
        pass  # best-effort; sync observer is a no-op

    console.print(f"  Versions:     {versions_created} (plan.md history)")

    # 4. Seed demo identities via admin RPC (best-effort)
    identities_created = _seed_identities(config, manifest)

    # 5. Seed permissions (best-effort)
    perms_created = _seed_permissions(nx, config, manifest)

    # 5b. Seed agent coordination: IPC, delegations, messages (best-effort)
    coord_counts = {"provisioned": 0, "delegated": 0, "messages": 0}
    try:
        coord_counts = _seed_agent_coordination(config, manifest)
    except Exception as e:
        logger.debug("Agent coordination seeding failed: %s", e)

    # 5c. Seed zones (best-effort)
    zones_created = 0
    try:
        zones_created = _seed_zones(config, manifest)
    except Exception as e:
        logger.debug("Zone seeding failed: %s", e)

    # 6. Initialize semantic search and index demo files (best-effort)
    semantic_ready = False
    if not skip_semantic:
        semantic_ready = await _init_semantic_search(nx, config, manifest)

    # 7. Seed catalog schemas (best-effort)
    schemas_extracted = 0
    try:
        schemas_extracted = _seed_catalog(nx, config, manifest)
    except Exception as e:
        logger.debug("Catalog seeding failed: %s", e)

    # 8. Seed aspects (best-effort)
    aspects_created = 0
    try:
        aspects_created = _seed_aspects(nx, config, manifest)
    except Exception as e:
        logger.debug("Aspect seeding failed: %s", e)

    # 9. Seed lineage (best-effort, Issue #3417)
    lineage_created = 0
    try:
        lineage_created = _seed_lineage(nx, config, manifest)
    except Exception as e:
        logger.debug("Lineage seeding failed: %s", e)

    # 10. Record seed metadata
    manifest["seeded_at"] = datetime.now(tz=UTC).isoformat()
    manifest["preset"] = config.get("preset", "unknown")
    manifest["skip_semantic"] = skip_semantic
    manifest["semantic_ready"] = semantic_ready
    manifest["write_mode_used"] = "ec"

    # Save manifest
    _save_manifest(data_dir, manifest)

    # Close connection
    with contextlib.suppress(Exception):
        nx.close()

    # Print summary
    if identities_created > 0:
        console.print(f"  Identities:   {identities_created} provisioned via admin RPC")
    else:
        console.print(
            f"  Identities:   {len(DEMO_USERS)} users, {len(DEMO_AGENTS)} agents (pre-existing or skipped)"
        )
    existing_perms = int(manifest.get("permissions_count", 0) or 0)
    if perms_created > 0:
        console.print(f"  Permissions:  {perms_created} tuples")
    elif manifest.get("permissions_seeded") and existing_perms > 0:
        console.print(f"  Permissions:  {existing_perms} tuples (already present)")
    else:
        console.print("  Permissions:  skipped (not available)")
    if any(coord_counts.get(k, 0) > 0 for k in ("provisioned", "delegated", "messages")):
        console.print(
            f"  Agents:       {coord_counts['provisioned']} provisioned, "
            f"{coord_counts['delegated']} delegations, "
            f"{coord_counts['messages']} messages"
        )
    else:
        console.print("  Agents:       skipped (coordination not available)")
    if zones_created > 0:
        console.print(f"  Zones:        {zones_created} created (research zone with HERB data)")
    else:
        console.print("  Zones:        skipped")
    if skip_semantic:
        console.print("  Semantic:     skipped")
    elif semantic_ready:
        console.print("  Semantic:     ready")
    else:
        console.print("  Semantic:     failed (check server logs)")
    console.print("  Grep corpus:  ready")
    if schemas_extracted > 0:
        console.print(f"  Catalog:      {schemas_extracted} schemas extracted")
    else:
        console.print("  Catalog:      skipped (server unavailable)")
    if aspects_created > 0:
        console.print(f"  Aspects:      {aspects_created} aspects seeded")
    else:
        console.print("  Aspects:      skipped (server unavailable)")
    if lineage_created > 0:
        console.print(f"  Lineage:      {lineage_created} lineage entries seeded")
    else:
        console.print("  Lineage:      skipped (server unavailable)")

    # Print suggested commands — for shared/demo presets, tell the user to
    # export env vars so all CLI commands authenticate against the running stack.
    preset = config.get("preset", "local")
    console.print()

    if preset in ("shared", "demo"):
        console.print("[bold]Load env vars:[/bold]")
        console.print("  eval $(nexus env)")
        console.print()

    console.print("[bold]Try these commands:[/bold]")
    console.print("  nexus ls /workspace/demo")
    console.print("  nexus cat /workspace/demo/README.md")
    console.print("  nexus versions history /workspace/demo/plan.md")
    console.print('  nexus grep "vector index" /workspace/demo')
    if semantic_ready:
        console.print('  nexus search query "How does the demo authentication flow work?"')
    console.print()
    console.print("[bold]Data catalog:[/bold]")
    console.print("  nexus catalog schema /workspace/demo/data/sales.csv")
    console.print("  nexus catalog search --column amount")
    console.print()
    console.print("[bold]Metadata aspects:[/bold]")
    console.print("  nexus aspects list /workspace/demo/restricted/internal.md")
    console.print()
    console.print("[bold]Operations & replay:[/bold]")
    console.print("  nexus ops replay --limit 5")
    console.print("  nexus reindex --target search --dry-run")


@demo.command(name="reset")
def demo_reset() -> None:
    """Remove all demo data and the manifest.

    This is a destructive operation — demo files, users, and agents
    will be deleted from the running Nexus instance.

    Example:
        nexus demo reset
    """
    import asyncio

    asyncio.run(_async_demo_reset())


async def _async_demo_reset() -> None:
    config = _load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")

    manifest = _load_manifest(data_dir)
    if not manifest:
        console.print("[nexus.warning]No demo data found (no manifest).[/nexus.warning]")
        raise SystemExit(0)

    # Revoke identity API keys (best-effort)
    revoked = _revoke_identities(config, manifest)
    if revoked:
        console.print(f"[nexus.success]✓[/nexus.success] Revoked {revoked} identity API keys.")

    # Delete permission tuples (best-effort, before file deletion)
    try:
        nx = await _get_nexus_client(config)
    except Exception as e:
        console.print(f"[nexus.warning]Warning:[/nexus.warning] Could not connect to Nexus: {e}")
        nx = None

    perms_deleted = _delete_permissions(nx, config) if nx else 0
    if perms_deleted > 0:
        console.print(
            f"[nexus.success]✓[/nexus.success] Deleted {perms_deleted} permission tuples."
        )

    # Delete demo files
    if nx is not None:
        try:
            removed = await _delete_demo_files(nx, manifest)
            console.print(f"[nexus.success]✓[/nexus.success] Removed {removed} demo files.")
        except Exception as e:
            console.print(f"[nexus.warning]Warning:[/nexus.warning] Could not remove files: {e}")
        with contextlib.suppress(Exception):
            nx.close()

    # Remove manifest
    mp = _manifest_path(data_dir)
    if mp.exists():
        mp.unlink()
        console.print("[nexus.success]✓[/nexus.success] Manifest removed.")

    console.print("[nexus.success]Demo data reset complete.[/nexus.success]")

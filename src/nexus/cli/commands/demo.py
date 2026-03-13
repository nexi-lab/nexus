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

from nexus.cli.utils import console

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_SEARCH_PATHS = ("./nexus.yaml", "./nexus.yml")
MANIFEST_FILENAME = ".demo-manifest.json"

# Demo identities
DEMO_USERS = [
    {"type": "user", "id": "admin", "display_name": "Admin"},
    {"type": "user", "id": "demo_user", "display_name": "Demo User"},
]
DEMO_AGENTS = [
    {"type": "agent", "id": "demo_agent", "display_name": "Demo Agent"},
]

# Demo file tree — (path, content, description)
DEMO_FILES: list[tuple[str, str, str]] = [
    (
        "/workspace/demo/README.md",
        "# Nexus Demo Workspace\n\n"
        "This workspace contains sample files for exploring Nexus features.\n\n"
        "## Features demonstrated\n\n"
        "- File CRUD operations\n"
        "- Version history and rollback\n"
        "- Permission-based access control\n"
        "- Agent registry and coordination\n"
        "- Audit logging\n"
        "- Full-text search (grep)\n"
        "- Semantic search\n",
        "Demo workspace README",
    ),
    (
        "/workspace/demo/plan.md",
        "# Project Plan\n\n"
        "## Phase 1: Setup\n- Initialize workspace\n- Configure authentication\n\n"
        "## Phase 2: Development\n- Build vector index pipeline\n- Implement search API\n\n"
        "## Phase 3: Deployment\n- Deploy to production\n- Monitor and iterate\n",
        "Versioned project plan",
    ),
    (
        "/workspace/demo/auth-flow.md",
        "# Authentication Flow\n\n"
        "## Overview\n"
        "The demo authentication flow uses database-backed credentials.\n\n"
        "## Steps\n"
        "1. Client sends API key in `Authorization` header\n"
        "2. Server validates key against the database\n"
        "3. Server resolves the associated user/agent identity\n"
        "4. OperationContext is populated with subject, zone, and capabilities\n"
        "5. ReBAC checks are applied on every file operation\n\n"
        "## API Key Format\n"
        "- Live keys: `nx_live_<agent_id>`\n"
        "- Test keys: `nx_test_<agent_id>`\n",
        "Auth flow documentation (semantic-search friendly)",
    ),
    (
        "/workspace/demo/notes/meeting-2026-03.md",
        "# Meeting Notes — March 2026\n\n"
        "## Attendees\n- Alice, Bob, Demo Agent\n\n"
        "## Discussion\n"
        "- Reviewed vector index performance benchmarks\n"
        "- Decided to use HNSW with ef_construction=200\n"
        "- Demo Agent will index the workspace nightly\n\n"
        "## Action Items\n"
        "- [ ] Alice: finalize schema migration\n"
        "- [ ] Bob: benchmark Dragonfly cache hit rates\n"
        "- [ ] Demo Agent: run nightly indexing job\n",
        "Meeting notes with grep-friendly content",
    ),
    (
        "/workspace/demo/notes/architecture.md",
        "# Architecture Overview\n\n"
        "## Storage Layer\n"
        "Content-addressable storage (CAS) backed by local disk or GCS.\n"
        "Each file's content is hashed (SHA-256) and stored by hash.\n\n"
        "## Metadata Layer\n"
        "Raft consensus for distributed metadata (sled state machine).\n"
        "Supports single-node embedded mode (like SQLite) and multi-node federation.\n\n"
        "## Search\n"
        "- Grep: direct CAS scan or Zoekt trigram index\n"
        "- Semantic: pgvector HNSW index with embedding cache in Dragonfly\n\n"
        "## Permissions\n"
        "Relationship-based access control (ReBAC) with Zanzibar-style tuples.\n"
        "Zone isolation ensures cross-zone data cannot leak.\n",
        "Architecture documentation (search-friendly)",
    ),
    (
        "/workspace/demo/code/example.py",
        '"""Example Python module for grep testing."""\n\n'
        "import hashlib\n"
        "from pathlib import Path\n\n\n"
        "def compute_hash(data: bytes) -> str:\n"
        '    """Compute SHA-256 hash of data."""\n'
        "    return hashlib.sha256(data).hexdigest()\n\n\n"
        "def build_vector_index(documents: list[str]) -> dict:\n"
        '    """Build a simple vector index from documents.\n\n'
        "    This function demonstrates semantic indexing by computing\n"
        "    document embeddings and storing them in an HNSW index.\n"
        '    """\n'
        "    index = {}\n"
        "    for i, doc in enumerate(documents):\n"
        "        index[i] = compute_hash(doc.encode())\n"
        "    return index\n",
        "Python code file (grep-friendly)",
    ),
    (
        "/workspace/demo/code/config.yaml",
        "# Demo configuration\n"
        "server:\n"
        "  host: 0.0.0.0\n"
        "  port: 2026\n"
        "  workers: 4\n\n"
        "cache:\n"
        "  backend: dragonfly\n"
        "  ttl: 3600\n"
        "  max_memory: 512mb\n\n"
        "search:\n"
        "  engine: zoekt\n"
        "  semantic_enabled: true\n"
        "  embedding_model: text-embedding-3-small\n",
        "YAML config file (grep-friendly)",
    ),
    (
        "/workspace/demo/data/sample.json",
        json.dumps(
            {
                "agents": [
                    {
                        "id": "demo_agent",
                        "status": "active",
                        "capabilities": ["read", "write", "search"],
                    },
                    {"id": "indexer", "status": "idle", "capabilities": ["read", "index"]},
                ],
                "workspace": {"path": "/workspace/demo", "files": 18, "version": 1},
            },
            indent=2,
        )
        + "\n",
        "JSON data file",
    ),
    (
        "/workspace/demo/restricted/internal.md",
        "# Internal Document\n\n"
        "This file has restricted permissions.\n"
        "Only admin and authorized agents can read it.\n\n"
        "## Confidential Notes\n"
        "- Database credentials are rotated weekly\n"
        "- API rate limits: 1000 req/min for agents, 100 req/min for users\n",
        "Permission-restricted file",
    ),
]

# Version history entries for plan.md
PLAN_VERSIONS = [
    "# Project Plan v1\n\n## Phase 1: Setup\n- Initialize workspace\n",
    "# Project Plan v2\n\n## Phase 1: Setup\n- Initialize workspace\n- Configure auth\n\n"
    "## Phase 2: Development\n- Build search API\n",
    "# Project Plan v3\n\n## Phase 1: Setup\n- Initialize workspace\n- Configure authentication\n\n"
    "## Phase 2: Development\n- Build vector index pipeline\n- Implement search API\n",
]

# Demo directories (ordered parents-first for creation, reversed for deletion)
DEMO_DIRS = [
    "/workspace",
    "/workspace/demo",
    "/workspace/demo/notes",
    "/workspace/demo/code",
    "/workspace/demo/data",
    "/workspace/demo/restricted",
]

# ReBAC permission tuples seeded by demo init (used by both seed and reset)
DEMO_PERMISSION_TUPLES = [
    {
        "subject": ["user", "admin"],
        "relation": "direct_owner",
        "object": ["file", "/workspace/demo"],
        "zone_id": "root",
    },
    {
        "subject": ["user", "demo_user"],
        "relation": "viewer",
        "object": ["file", "/workspace/demo"],
        "zone_id": "root",
    },
    {
        "subject": ["agent", "demo_agent"],
        "relation": "editor",
        "object": ["file", "/workspace/demo"],
        "zone_id": "root",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_project_config() -> dict[str, Any]:
    for candidate in CONFIG_SEARCH_PATHS:
        p = Path(candidate)
        if p.exists():
            with open(p) as f:
                return yaml.safe_load(f) or {}
    console.print("[red]Error:[/red] No nexus.yaml found. Run `nexus init` first.")
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


def _get_nexus_client(config: dict[str, Any]) -> Any:
    """Connect to a running Nexus server via gRPC, or fall back to local.

    For remote presets (shared/demo), sets NEXUS_GRPC_PORT from nexus.yaml
    so the SDK connects to the correct gRPC port published by the compose stack.

    Raises on failure — does not silently fall back to a separate local instance
    when a remote preset is expected.
    """
    import nexus

    preset = config.get("preset", "local")
    ports = config.get("ports", {})
    http_port = ports.get("http", 2026)
    grpc_port = ports.get("grpc", 2028)

    if preset in ("shared", "demo"):
        # Set NEXUS_GRPC_PORT so nexus.connect() uses the right port
        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)

        # Resolve the admin API key — prefer nexus.yaml, fall back to the
        # key file written by the container entrypoint.
        api_key = config.get("api_key", "")
        if not api_key:
            data_dir = config.get("data_dir", "./nexus-data")
            key_file = Path(data_dir) / ".admin-api-key"
            if key_file.exists():
                api_key = key_file.read_text().strip()

        # When TLS is enabled, set env vars so nexus.connect() builds
        # an RPCTransport with mTLS credentials (dev certs double as
        # both server and client certs).
        if config.get("tls"):
            tls_cert = config.get("tls_cert", "")
            tls_key = config.get("tls_key", "")
            tls_ca = config.get("tls_ca", "")
            if tls_cert and tls_key and tls_ca:
                os.environ["NEXUS_TLS_CERT"] = tls_cert
                os.environ["NEXUS_TLS_KEY"] = tls_key
                os.environ["NEXUS_TLS_CA"] = tls_ca

        try:
            nx = nexus.connect(
                config={
                    "profile": "remote",
                    "url": f"http://localhost:{http_port}",
                    "api_key": api_key,
                }
            )
            # Verify connectivity with a lightweight read-only call.
            # Use sys_readdir (returns list) instead of sys_stat (goes through
            # MetadataMapper.from_json which can fail on schema mismatches).
            nx.sys_readdir("/")
            return nx
        except Exception as e:
            console.print(f"[red]Error:[/red] Could not connect to Nexus server: {e}")
            console.print(
                f"[yellow]Hint:[/yellow] Is `nexus up` running? Expected gRPC on port {grpc_port}."
            )
            raise

    # Local preset — connect directly to data dir
    data_dir = config.get("data_dir", "./nexus-data")
    return nexus.connect(config={"data_dir": data_dir})


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


def _seed_files(
    nx: Any,
    manifest: dict[str, Any],
) -> int:
    """Seed demo files. Returns count of files created."""
    seeded = manifest.get("files", [])
    created = 0

    for path, content, _description in DEMO_FILES:
        if path in seeded:
            continue
        try:
            # Ensure parent directory exists
            parent = "/".join(path.split("/")[:-1])
            if parent:
                nx.sys_mkdir(parent, parents=True, exist_ok=True)
            nx.sys_write(path, content.encode())
            seeded.append(path)
            created += 1
        except Exception as e:
            console.print(f"  [yellow]Warning:[/yellow] Could not create {path}: {e}")

    manifest["files"] = seeded
    return created


def _seed_versions(nx: Any, manifest: dict[str, Any]) -> int:
    """Create version history for plan.md. Returns count of versions created."""
    if manifest.get("versions_seeded"):
        return 0

    created = 0
    plan_path = "/workspace/demo/plan.md"
    for version_content in PLAN_VERSIONS:
        try:
            nx.sys_write(plan_path, version_content.encode())
            created += 1
        except Exception:
            break

    # Write the final version (from DEMO_FILES)
    final = next((c for p, c, _ in DEMO_FILES if p == plan_path), None)
    if final:
        try:
            nx.sys_write(plan_path, final.encode())
            created += 1
        except Exception:
            pass

    manifest["versions_seeded"] = True
    return created


def _seed_directories(nx: Any) -> int:
    """Create base demo directories. Returns count created."""
    created = 0
    for d in DEMO_DIRS:
        try:
            nx.sys_mkdir(d, parents=True, exist_ok=True)
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

    tuples = DEMO_PERMISSION_TUPLES
    preset = config.get("preset", "local")

    if preset in ("shared", "demo"):
        # Primary: docker compose exec (works with any server image)
        created = _seed_permissions_docker(config, tuples)
        if created < 0:
            # Fallback: admin RPC (requires server built from this branch)
            created = _seed_permissions_rpc(config, tuples)
    else:
        # Local path — direct rebac_manager access
        rebac = getattr(nx, "_rebac_manager", None) or getattr(nx, "rebac_manager", None)
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
        [[t["subject"], t["relation"], t["object"], t.get("zone_id", "root")] for t in tuples]
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
    ports = config.get("ports", {})
    http_port = ports.get("http", 2026)
    grpc_port = ports.get("grpc", 2028)

    api_key = config.get("api_key", "")
    if not api_key:
        data_dir = config.get("data_dir", "./nexus-data")
        key_file = Path(data_dir) / ".admin-api-key"
        if key_file.exists():
            api_key = key_file.read_text().strip()

    if not api_key:
        logger.debug("No admin API key — skipping permission seeding via RPC")
        return 0

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)
        call_rpc = get_admin_rpc(f"http://localhost:{http_port}", api_key)
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

    ports = config.get("ports", {})
    http_port = ports.get("http", 2026)
    grpc_port = ports.get("grpc", 2028)

    # Resolve admin API key
    api_key = config.get("api_key", "")
    if not api_key:
        data_dir = config.get("data_dir", "./nexus-data")
        key_file = Path(data_dir) / ".admin-api-key"
        if key_file.exists():
            api_key = key_file.read_text().strip()

    if not api_key:
        logger.debug("No admin API key available — skipping identity seeding")
        return 0

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)
        call_rpc = get_admin_rpc(f"http://localhost:{http_port}", api_key)
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


def _revoke_identities(config: dict[str, Any], manifest: dict[str, Any]) -> int:
    """Revoke API keys created by ``_seed_identities``.

    Reads ``key_id`` values from the manifest's ``identity_keys`` and
    calls ``admin_revoke_key`` via the admin RPC.
    """
    identity_keys = manifest.get("identity_keys", {})
    if not identity_keys:
        return 0

    ports = config.get("ports", {})
    http_port = ports.get("http", 2026)
    grpc_port = ports.get("grpc", 2028)

    api_key = config.get("api_key", "")
    if not api_key:
        data_dir = config.get("data_dir", "./nexus-data")
        key_file = Path(data_dir) / ".admin-api-key"
        if key_file.exists():
            api_key = key_file.read_text().strip()
    if not api_key:
        return 0

    # Set TLS env vars so admin RPC uses mTLS when TLS is enabled
    if config.get("tls"):
        tls_cert = config.get("tls_cert", "")
        tls_key = config.get("tls_key", "")
        tls_ca = config.get("tls_ca", "")
        if tls_cert and tls_key and tls_ca:
            os.environ["NEXUS_TLS_CERT"] = tls_cert
            os.environ["NEXUS_TLS_KEY"] = tls_key
            os.environ["NEXUS_TLS_CA"] = tls_ca

    try:
        from nexus.cli.commands.admin import get_admin_rpc

        os.environ["NEXUS_GRPC_PORT"] = str(grpc_port)
        call_rpc = get_admin_rpc(f"http://localhost:{http_port}", api_key)
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


def _delete_demo_files(nx: Any, manifest: dict[str, Any]) -> int:
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
            nx.sys_rmdir(d)

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
        [[t["subject"], t["relation"], t["object"], t.get("zone_id", "root")] for t in tuples]
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

    # Local path — direct rebac_manager access
    rebac = getattr(nx, "_rebac_manager", None) or getattr(nx, "rebac_manager", None)
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


def _seed_search_chunks_docker(nx: Any, config: dict[str, Any]) -> bool:
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

    # Read file contents via RPC
    docs = []
    for path, _content, _desc in DEMO_FILES:
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


def _init_semantic_search(nx: Any, config: dict[str, Any]) -> bool:
    """Initialize semantic search by inserting demo content into document_chunks.

    The IndexingPipeline/PipelineIndexer paths are broken for the demo because
    file_paths table is empty (NexusFS uses its own internal metastore).
    Instead, we directly insert file_paths + document_chunks via docker exec
    so the SQL fallback search can find demo content.

    Returns True if semantic search is ready.
    """
    preset = config.get("preset", "local")
    if preset not in ("shared", "demo"):
        return False

    _ensure_pgvector_extension(config)
    return _seed_search_chunks_docker(nx, config)


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
    config = _load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")

    # Connect to Nexus
    try:
        nx = _get_nexus_client(config)
    except Exception as e:
        console.print(f"[red]Error:[/red] Could not connect to Nexus: {e}")
        console.print("[yellow]Hint:[/yellow] Is the server running? Try `nexus up` first.")
        raise SystemExit(1) from e

    # Load or reset manifest
    if reset:
        old_manifest = _load_manifest(data_dir)
        if old_manifest:
            console.print("[yellow]Resetting demo data...[/yellow]")
            revoked = _revoke_identities(config, old_manifest)
            if revoked:
                console.print(f"  Revoked {revoked} identity API keys.")
            perms_del = _delete_permissions(nx, config)
            if perms_del:
                console.print(f"  Deleted {perms_del} permission tuples.")
            removed = _delete_demo_files(nx, old_manifest)
            console.print(f"  Removed {removed} files.")
        manifest: dict[str, Any] = {}
    else:
        manifest = _load_manifest(data_dir)

    console.print("[bold]Seeding Nexus demo data...[/bold]")
    console.print()

    # 1. Create directories
    _seed_directories(nx)

    # 2. Seed files
    files_created = _seed_files(nx, manifest)
    total_files = len(manifest.get("files", []))
    console.print(f"  Files:        {total_files} ({files_created} new)")

    # 3. Seed version history
    _seed_versions(nx, manifest)
    console.print(f"  Versions:     {len(PLAN_VERSIONS) + 1} (plan.md history)")

    # 4. Seed demo identities via admin RPC (best-effort)
    identities_created = _seed_identities(config, manifest)

    # 5. Seed permissions (best-effort)
    perms_created = _seed_permissions(nx, config, manifest)

    # 6. Initialize semantic search and index demo files (best-effort)
    semantic_ready = False
    if not skip_semantic:
        semantic_ready = _init_semantic_search(nx, config)

    # 7. Record seed metadata
    manifest["seeded_at"] = datetime.now(tz=UTC).isoformat()
    manifest["preset"] = config.get("preset", "unknown")
    manifest["skip_semantic"] = skip_semantic
    manifest["semantic_ready"] = semantic_ready

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
    if perms_created > 0:
        console.print(f"  Permissions:  {perms_created} tuples")
    else:
        console.print("  Permissions:  skipped (not available)")
    if skip_semantic:
        console.print("  Semantic:     skipped")
    elif semantic_ready:
        console.print("  Semantic:     ready")
    else:
        console.print("  Semantic:     failed (check server logs)")
    console.print("  Grep corpus:  ready")

    # Print suggested commands — for shared/demo presets, tell the user to
    # export env vars so all CLI commands authenticate against the running stack.
    preset = config.get("preset", "local")
    console.print()

    if preset in ("shared", "demo"):
        ports = config.get("ports", {})
        http_port = ports.get("http", 2026)
        grpc_port = ports.get("grpc", 2028)
        api_key = config.get("api_key", "")
        console.print("[bold]Set these env vars to talk to the running stack:[/bold]")
        console.print(f"  export NEXUS_URL=http://localhost:{http_port}")
        console.print(f"  export NEXUS_API_KEY={api_key}")
        console.print(f"  export NEXUS_GRPC_PORT={grpc_port}")
        console.print()

    console.print("[bold]Try these commands:[/bold]")
    console.print("  nexus ls /workspace/demo")
    console.print("  nexus cat /workspace/demo/README.md")
    console.print("  nexus versions history /workspace/demo/plan.md")
    console.print('  nexus grep "vector index" /workspace/demo')
    if semantic_ready:
        console.print('  nexus search query "How does the demo authentication flow work?"')


@demo.command(name="reset")
def demo_reset() -> None:
    """Remove all demo data and the manifest.

    This is a destructive operation — demo files, users, and agents
    will be deleted from the running Nexus instance.

    Example:
        nexus demo reset
    """
    config = _load_project_config()
    data_dir = config.get("data_dir", "./nexus-data")

    manifest = _load_manifest(data_dir)
    if not manifest:
        console.print("[yellow]No demo data found (no manifest).[/yellow]")
        raise SystemExit(0)

    # Revoke identity API keys (best-effort)
    revoked = _revoke_identities(config, manifest)
    if revoked:
        console.print(f"[green]✓[/green] Revoked {revoked} identity API keys.")

    # Delete permission tuples (best-effort, before file deletion)
    try:
        nx = _get_nexus_client(config)
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not connect to Nexus: {e}")
        nx = None

    perms_deleted = _delete_permissions(nx, config) if nx else 0
    if perms_deleted > 0:
        console.print(f"[green]✓[/green] Deleted {perms_deleted} permission tuples.")

    # Delete demo files
    if nx is not None:
        try:
            removed = _delete_demo_files(nx, manifest)
            console.print(f"[green]✓[/green] Removed {removed} demo files.")
        except Exception as e:
            console.print(f"[yellow]Warning:[/yellow] Could not remove files: {e}")
        with contextlib.suppress(Exception):
            nx.close()

    # Remove manifest
    mp = _manifest_path(data_dir)
    if mp.exists():
        mp.unlink()
        console.print("[green]✓[/green] Manifest removed.")

    console.print("[green]Demo data reset complete.[/green]")

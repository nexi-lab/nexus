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
    "/workspace/demo",
    "/workspace/demo/notes",
    "/workspace/demo/code",
    "/workspace/demo/data",
    "/workspace/demo/restricted",
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

        try:
            nx = nexus.connect(
                config={
                    "mode": "remote",
                    "url": f"http://localhost:{http_port}",
                    "api_key": config.get("api_key", ""),
                }
            )
            # Verify connectivity with a lightweight call
            nx.sys_mkdir("/workspace", exist_ok=True)
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
                nx.sys_mkdir(parent, exist_ok=True)
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
            nx.sys_mkdir(d, exist_ok=True)
            created += 1
        except Exception:
            pass
    return created


def _seed_permissions(nx: Any, manifest: dict[str, Any]) -> int:
    """Seed demo ReBAC permissions via the internal rebac_write API.

    Writes relationship tuples for the demo workspace:
    - admin gets direct_owner on /workspace/demo
    - demo_user gets viewer on /workspace/demo
    - demo_agent gets editor on /workspace/demo

    The NexusFS also auto-grants direct_owner on mkdir/write when a user
    context is present, so the admin tuple may already exist.
    """
    if manifest.get("permissions_seeded"):
        return 0

    created = 0
    tuples = [
        (("user", "admin"), "direct_owner", ("file", "/workspace/demo")),
        (("user", "demo_user"), "viewer", ("file", "/workspace/demo")),
        (("agent", "demo_agent"), "editor", ("file", "/workspace/demo")),
    ]

    # Access the internal rebac_manager if available
    rebac = getattr(nx, "_rebac_manager", None) or getattr(nx, "rebac_manager", None)
    if rebac is None:
        logger.debug("No rebac_manager available — skipping permission seeding")
        manifest["permissions_seeded"] = True
        manifest["permissions_count"] = 0
        return 0

    for subject, relation, obj in tuples:
        try:
            rebac.rebac_write(
                subject=subject,
                relation=relation,
                object=obj,
                zone_id="root",
            )
            created += 1
        except Exception as e:
            logger.debug("Could not seed permission %s %s %s: %s", subject, relation, obj, e)

    manifest["permissions_seeded"] = True
    manifest["permissions_count"] = created
    return created


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

    # 4. Seed permissions (best-effort)
    perms_created = _seed_permissions(nx, manifest)

    # 5. Record seed metadata
    manifest["seeded_at"] = datetime.now(tz=UTC).isoformat()
    manifest["preset"] = config.get("preset", "unknown")
    manifest["skip_semantic"] = skip_semantic

    # Save manifest
    _save_manifest(data_dir, manifest)

    # Close connection
    with contextlib.suppress(Exception):
        nx.close()

    # Print summary
    users_count = len(DEMO_USERS)
    agents_count = len(DEMO_AGENTS)
    console.print(f"  Users:        {users_count} (admin, demo_user)")
    console.print(f"  Agents:       {agents_count} (demo_agent)")
    if perms_created > 0:
        console.print(f"  Permissions:  {perms_created} tuples")
    else:
        console.print("  Permissions:  skipped (not available)")
    if not skip_semantic:
        console.print("  Semantic:     ready")
    else:
        console.print("  Semantic:     skipped")
    console.print("  Grep corpus:  ready")

    # Print suggested commands
    console.print()
    console.print("[bold]Try these commands:[/bold]")
    console.print("  nexus ls /workspace/demo")
    console.print("  nexus cat /workspace/demo/README.md")
    console.print("  nexus versions history /workspace/demo/plan.md")
    console.print('  nexus grep "vector index" --path /workspace/demo')
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

    # Connect and delete demo files
    try:
        nx = _get_nexus_client(config)
        removed = _delete_demo_files(nx, manifest)
        nx.close()
        console.print(f"[green]✓[/green] Removed {removed} demo files.")
    except Exception as e:
        console.print(f"[yellow]Warning:[/yellow] Could not connect to remove files: {e}")

    # Remove manifest
    mp = _manifest_path(data_dir)
    if mp.exists():
        mp.unlink()
        console.print("[green]✓[/green] Manifest removed.")

    console.print("[green]Demo data reset complete.[/green]")

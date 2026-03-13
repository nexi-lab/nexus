"""Constant data for ``nexus demo init / reset``.

Extracted from ``demo.py`` to keep command logic and seed data separate.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_SEARCH_PATHS = ("./nexus.yaml", "./nexus.yml")
MANIFEST_FILENAME = ".demo-manifest.json"

# ---------------------------------------------------------------------------
# Demo identities
# ---------------------------------------------------------------------------

DEMO_USERS = [
    {"type": "user", "id": "admin", "display_name": "Admin"},
    {"type": "user", "id": "demo_user", "display_name": "Demo User"},
]
DEMO_AGENTS = [
    {"type": "agent", "id": "demo_agent", "display_name": "Demo Agent"},
]

# ---------------------------------------------------------------------------
# Demo file tree — (path, content, description)
# ---------------------------------------------------------------------------

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
    (
        "/workspace/demo/data/sales.csv",
        "date,region,amount,currency\n"
        "2026-01-15,us-west,12500.00,USD\n"
        "2026-02-20,eu-central,8750.50,EUR\n"
        "2026-03-10,apac,15300.75,USD\n"
        "2026-03-12,us-east,9200.00,USD\n",
        "CSV dataset for catalog schema extraction",
    ),
    (
        "/workspace/demo/data/metrics.json",
        json.dumps(
            [
                {
                    "timestamp": "2026-03-01T00:00:00Z",
                    "metric": "latency_p99",
                    "value": 42.5,
                    "unit": "ms",
                },
                {
                    "timestamp": "2026-03-02T00:00:00Z",
                    "metric": "latency_p99",
                    "value": 38.1,
                    "unit": "ms",
                },
                {
                    "timestamp": "2026-03-03T00:00:00Z",
                    "metric": "throughput",
                    "value": 1250.0,
                    "unit": "rps",
                },
            ],
            indent=2,
        )
        + "\n",
        "JSON metrics for catalog schema extraction",
    ),
]

# ---------------------------------------------------------------------------
# Version history entries for plan.md
# ---------------------------------------------------------------------------

PLAN_VERSIONS = [
    "# Project Plan v1\n\n## Phase 1: Setup\n- Initialize workspace\n",
    "# Project Plan v2\n\n## Phase 1: Setup\n- Initialize workspace\n- Configure auth\n\n"
    "## Phase 2: Development\n- Build search API\n",
    "# Project Plan v3\n\n## Phase 1: Setup\n- Initialize workspace\n- Configure authentication\n\n"
    "## Phase 2: Development\n- Build vector index pipeline\n- Implement search API\n",
]

# ---------------------------------------------------------------------------
# Demo directories (ordered parents-first for creation, reversed for deletion)
# ---------------------------------------------------------------------------

DEMO_DIRS = [
    "/workspace",
    "/workspace/demo",
    "/workspace/demo/notes",
    "/workspace/demo/code",
    "/workspace/demo/data",
    "/workspace/demo/restricted",
]

# ---------------------------------------------------------------------------
# ReBAC permission tuples seeded by demo init (used by both seed and reset)
# ---------------------------------------------------------------------------

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

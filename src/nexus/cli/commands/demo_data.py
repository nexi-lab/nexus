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
    # HERB corpus directories (Issue #2961)
    "/workspace/demo/herb",
    "/workspace/demo/herb/customers",
    "/workspace/demo/herb/employees",
    "/workspace/demo/herb/products",
]

# ---------------------------------------------------------------------------
# ReBAC permission tuples seeded by demo init (used by both seed and reset)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HERB-derived demo corpus (Issue #2961, Section G.5-7)
#
# Small deterministic subset of the HERB enterprise-context benchmark
# (nexi-lab/nexus-test benchmarks/herb/enterprise-context/).
# Provides realistic retrieval test data for semantic search quality checks.
# Pinned to fixed IDs — no runtime network dependency.
# ---------------------------------------------------------------------------

HERB_CORPUS: list[tuple[str, str, str]] = [
    # Customers (5 records)
    (
        "/workspace/demo/herb/customers/cust-001.md",
        "# Customer: Acme Corporation\n\n"
        "- **ID**: CUST-001\n"
        "- **Industry**: Manufacturing\n"
        "- **Region**: North America\n"
        "- **Annual Revenue**: $2.4B\n"
        "- **Contact**: Jane Chen, VP Engineering\n"
        "- **Status**: Active since 2019\n"
        "- **Notes**: Primary use case is supply chain optimization. "
        "Deployed Nexus for real-time inventory tracking across 12 warehouses. "
        "Key integration with SAP ERP and Snowflake data warehouse.\n",
        "HERB customer record — Acme Corporation",
    ),
    (
        "/workspace/demo/herb/customers/cust-002.md",
        "# Customer: Meridian Health Systems\n\n"
        "- **ID**: CUST-002\n"
        "- **Industry**: Healthcare\n"
        "- **Region**: Europe\n"
        "- **Annual Revenue**: $890M\n"
        "- **Contact**: Dr. Lars Eriksson, CTO\n"
        "- **Status**: Active since 2021\n"
        "- **Notes**: Uses Nexus for medical document management and compliance. "
        "HIPAA-compliant deployment with zone isolation per hospital network. "
        "Semantic search for clinical trial documents across 45 facilities.\n",
        "HERB customer record — Meridian Health",
    ),
    (
        "/workspace/demo/herb/customers/cust-003.md",
        "# Customer: Stellar Dynamics\n\n"
        "- **ID**: CUST-003\n"
        "- **Industry**: Aerospace\n"
        "- **Region**: Asia-Pacific\n"
        "- **Annual Revenue**: $5.1B\n"
        "- **Contact**: Kenji Tanaka, Director of AI\n"
        "- **Status**: Active since 2022\n"
        "- **Notes**: Nexus used for engineering design document search. "
        "GPU-accelerated embeddings for CAD file descriptions. "
        "Federated deployment across Tokyo, Singapore, and Sydney offices.\n",
        "HERB customer record — Stellar Dynamics",
    ),
    (
        "/workspace/demo/herb/customers/cust-004.md",
        "# Customer: GreenLeaf Energy\n\n"
        "- **ID**: CUST-004\n"
        "- **Industry**: Renewable Energy\n"
        "- **Region**: North America\n"
        "- **Annual Revenue**: $1.2B\n"
        "- **Contact**: Maria Santos, Head of Data Science\n"
        "- **Status**: Active since 2023\n"
        "- **Notes**: Uses Nexus for sensor data aggregation and anomaly detection. "
        "Deployed across 200+ solar farm sites. "
        "Real-time event streaming via NATS integration for predictive maintenance.\n",
        "HERB customer record — GreenLeaf Energy",
    ),
    (
        "/workspace/demo/herb/customers/cust-005.md",
        "# Customer: Atlas Financial Group\n\n"
        "- **ID**: CUST-005\n"
        "- **Industry**: Financial Services\n"
        "- **Region**: Europe\n"
        "- **Annual Revenue**: $3.7B\n"
        "- **Contact**: Sophie Laurent, CISO\n"
        "- **Status**: Active since 2020\n"
        "- **Notes**: Regulatory compliance document management. "
        "Strict permission model with multi-tenant zone isolation. "
        "Audit logging for all document access with immutable trail. "
        "Integration with Bloomberg data feeds for market research retrieval.\n",
        "HERB customer record — Atlas Financial",
    ),
    # Employees (5 records)
    (
        "/workspace/demo/herb/employees/emp-001.md",
        "# Employee: Sarah Kim\n\n"
        "- **ID**: EMP-001\n"
        "- **Role**: Senior Solutions Architect\n"
        "- **Department**: Customer Success\n"
        "- **Location**: San Francisco\n"
        "- **Joined**: 2020-03-15\n"
        "- **Expertise**: Distributed systems, Kubernetes, data pipelines\n"
        "- **Current Projects**: Acme Corporation deployment optimization, "
        "GreenLeaf Energy NATS integration\n"
        "- **Certifications**: AWS Solutions Architect, CKA\n",
        "HERB employee record — Sarah Kim",
    ),
    (
        "/workspace/demo/herb/employees/emp-002.md",
        "# Employee: Marcus Weber\n\n"
        "- **ID**: EMP-002\n"
        "- **Role**: Staff Engineer, Search\n"
        "- **Department**: Engineering\n"
        "- **Location**: Berlin\n"
        "- **Joined**: 2019-08-01\n"
        "- **Expertise**: Information retrieval, vector databases, pgvector\n"
        "- **Current Projects**: Semantic search quality improvements, "
        "HNSW index tuning for Meridian Health\n"
        "- **Publications**: 'Efficient Approximate Nearest Neighbor Search "
        "for Enterprise Document Retrieval' (SIGIR 2024)\n",
        "HERB employee record — Marcus Weber",
    ),
    (
        "/workspace/demo/herb/employees/emp-003.md",
        "# Employee: Priya Patel\n\n"
        "- **ID**: EMP-003\n"
        "- **Role**: Engineering Manager, Permissions\n"
        "- **Department**: Engineering\n"
        "- **Location**: London\n"
        "- **Joined**: 2021-01-10\n"
        "- **Expertise**: Authorization systems, ReBAC, Zanzibar, compliance\n"
        "- **Current Projects**: Atlas Financial zone isolation audit, "
        "permission cache optimization with TigerBeetle\n"
        "- **Team Size**: 6 engineers\n",
        "HERB employee record — Priya Patel",
    ),
    # Products (5 records)
    (
        "/workspace/demo/herb/products/prod-001.md",
        "# Product: Nexus Core\n\n"
        "- **ID**: PROD-001\n"
        "- **Category**: Platform\n"
        "- **Version**: 0.9.x\n"
        "- **Pricing**: Usage-based, starting at $0.10/GB/month\n"
        "- **Description**: Content-addressable filesystem with built-in "
        "versioning, metadata, and search. Supports local embedded mode "
        "(SQLite-like) and shared daemon mode with PostgreSQL backend.\n"
        "- **Key Features**: CAS storage, Raft metadata, zone isolation, "
        "ReBAC permissions, gRPC API, REST API\n"
        "- **Target Audience**: AI teams needing shared context across agents\n",
        "HERB product record — Nexus Core",
    ),
    (
        "/workspace/demo/herb/products/prod-002.md",
        "# Product: Nexus Semantic Search\n\n"
        "- **ID**: PROD-002\n"
        "- **Category**: Add-on\n"
        "- **Version**: 0.9.x\n"
        "- **Pricing**: Included with Nexus Core\n"
        "- **Description**: Vector-based semantic search powered by pgvector "
        "HNSW indices. Embedding cache in Dragonfly for sub-millisecond "
        "repeated queries. Supports custom embedding models via API.\n"
        "- **Key Features**: pgvector HNSW, Dragonfly embedding cache, "
        "hybrid keyword+semantic search, configurable embedding models\n"
        "- **Integration**: Works with Zoekt for combined keyword and semantic results\n",
        "HERB product record — Semantic Search",
    ),
    (
        "/workspace/demo/herb/products/prod-003.md",
        "# Product: Nexus Federation\n\n"
        "- **ID**: PROD-003\n"
        "- **Category**: Add-on\n"
        "- **Version**: 0.8.x (beta)\n"
        "- **Pricing**: Enterprise tier\n"
        "- **Description**: Multi-node Raft-based federation for distributed "
        "deployments. SSH-style TOFU mTLS for zero-config zone peering. "
        "Supports cross-zone queries with permission-aware routing.\n"
        "- **Key Features**: Raft consensus, mTLS TOFU, zone peering, "
        "cross-zone search, automatic failover\n"
        "- **Requirements**: Minimum 3 nodes for fault tolerance\n",
        "HERB product record — Federation",
    ),
]

# Directories needed for HERB corpus
HERB_DIRS = [
    "/workspace/demo/herb",
    "/workspace/demo/herb/customers",
    "/workspace/demo/herb/employees",
    "/workspace/demo/herb/products",
]

# ---------------------------------------------------------------------------
# HERB-derived QA evaluation set (Issue #2961, Section G.8-9)
#
# Curated questions whose answers are present in the seeded HERB subset.
# Used for semantic search quality gate: assert answer-bearing record
# appears in top-5 results.
# ---------------------------------------------------------------------------

HERB_QA_SET: list[dict[str, str]] = [
    {
        "question": "Which customer uses Nexus for medical document management?",
        "expected_file": "/workspace/demo/herb/customers/cust-002.md",
        "expected_substring": "Meridian Health",
    },
    {
        "question": "Who is the staff engineer working on semantic search quality?",
        "expected_file": "/workspace/demo/herb/employees/emp-002.md",
        "expected_substring": "Marcus Weber",
    },
    {
        "question": "What is the pricing model for Nexus Core?",
        "expected_file": "/workspace/demo/herb/products/prod-001.md",
        "expected_substring": "Usage-based",
    },
    {
        "question": "Which customer deployed across 200 solar farm sites?",
        "expected_file": "/workspace/demo/herb/customers/cust-004.md",
        "expected_substring": "GreenLeaf",
    },
    {
        "question": "Who manages the permissions engineering team?",
        "expected_file": "/workspace/demo/herb/employees/emp-003.md",
        "expected_substring": "Priya Patel",
    },
    {
        "question": "What product provides multi-node Raft-based federation?",
        "expected_file": "/workspace/demo/herb/products/prod-003.md",
        "expected_substring": "Federation",
    },
    {
        "question": "Which customer has a HIPAA-compliant deployment?",
        "expected_file": "/workspace/demo/herb/customers/cust-002.md",
        "expected_substring": "HIPAA",
    },
    {
        "question": "What does Atlas Financial use Nexus for?",
        "expected_file": "/workspace/demo/herb/customers/cust-005.md",
        "expected_substring": "compliance",
    },
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

# Nexus Architecture

This document provides a comprehensive overview of the Nexus architecture, including component diagrams, data flows, and design decisions.

## Table of Contents

- [Overview](#overview)
- [High-Level Architecture](#high-level-architecture)
- [Deployment Modes](#deployment-modes)
- [Core Components](#core-components)
- [Storage Layer](#storage-layer)
- [Permission System (ReBAC)](#permission-system-rebac)
- [Skills System](#skills-system)
- [Search & Intelligence](#search--intelligence)
- [MCP Integration](#mcp-integration)
- [Server Architecture](#server-architecture)
- [CLI Architecture](#cli-architecture)
- [Data Flow Examples](#data-flow-examples)
- [Database Schema](#database-schema)
- [Security Architecture](#security-architecture)
- [Deployment Architecture](#deployment-architecture)

---

## Overview

Nexus is an AI-native filesystem that unifies files, memory, and permissions into one programmable layer. It provides:

- **Unified Virtual Filesystem**: Abstract storage backends behind a consistent API
- **AI Agent Memory**: Persistent learning and context across sessions
- **Fine-Grained Permissions**: Google Zanzibar-inspired ReBAC with multi-tenancy
- **Semantic Search**: Vector-based search with hybrid keyword/semantic capabilities
- **Skills System**: Three-tier skill hierarchy with MCP integration
- **Workflow Automation**: Event-driven pipelines triggered by file operations

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AI Agents Layer                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │   Claude    │  │   GPT-4     │  │  LangGraph  │  │   Custom    │        │
│  │   Agent     │  │   Agent     │  │   Agent     │  │   Agent     │        │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘        │
└─────────┼────────────────┼────────────────┼────────────────┼────────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Access Layer                                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  Python SDK │  │    CLI      │  │  MCP Server │  │  REST API   │        │
│  │ nexus.sdk   │  │   nexus     │  │  (stdio/http)│  │  /api/nfs   │        │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘        │
└─────────┼────────────────┼────────────────┼────────────────┼────────────────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Nexus Core (NexusFS)                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      NexusFilesystem Interface                       │   │
│  │  read() write() delete() list() glob() grep() semantic_search()     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │
│  │   Memory     │ │   Search     │ │   Skills     │ │  Workflows   │       │
│  │     API      │ │   Engine     │ │   System     │ │   Engine     │       │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘       │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │
│  │   Router     │ │  Versioning  │ │   ReBAC      │ │  Metadata    │       │
│  │ (Namespaces) │ │   System     │ │ Permissions  │ │   Store      │       │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Storage Backends                                     │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐│
│  │   Local    │ │    GCS     │ │    S3      │ │  Google    │ │  Twitter   ││
│  │  Backend   │ │  Backend   │ │  Backend   │ │   Drive    │ │    (X)     ││
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Database Layer                                       │
│  ┌────────────────────────────────┐ ┌────────────────────────────────┐     │
│  │        PostgreSQL              │ │          SQLite                │     │
│  │  - Metadata Store              │ │  - Metadata Store              │     │
│  │  - pgvector (embeddings)       │ │  - sqlite-vec (embeddings)     │     │
│  │  - tsvector (full-text)        │ │  - FTS5 (full-text)            │     │
│  │  - ReBAC tuples                │ │  - ReBAC tuples                │     │
│  └────────────────────────────────┘ └────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## High-Level Architecture

### Component Interaction Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                CLIENT                                        │
│                                                                              │
│    SDK (nexus.connect())     CLI (nexus)      MCP Server      Remote Client │
│           │                      │                │                │        │
│           ▼                      ▼                ▼                ▼        │
│    ┌──────────────────────────────────────────────────────────────────┐    │
│    │                    Connection Factory                             │    │
│    │  - Auto-detects mode (embedded vs server)                        │    │
│    │  - Creates NexusFS (local) or RemoteNexusFS (HTTP client)        │    │
│    └──────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │                          │                          │
         ▼                          ▼                          ▼
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│   Embedded Mode │      │   Server Mode   │      │  Distributed    │
│                 │      │                 │      │  (Planned)      │
│  ┌───────────┐  │      │  ┌───────────┐  │      │                 │
│  │  NexusFS  │  │      │  │ RemoteFS  │  │      │  Kubernetes     │
│  │  (local)  │  │      │  │  (HTTP)   │  │      │  deployment     │
│  └───────────┘  │      │  └─────┬─────┘  │      │                 │
│        │        │      │        │        │      │                 │
│        ▼        │      │        ▼        │      │                 │
│  Local Backend  │      │  RPC Server     │      │                 │
│  + SQLite       │      │  (JSON-RPC)     │      │                 │
└─────────────────┘      └─────────────────┘      └─────────────────┘
```

---

## Deployment Modes

Nexus supports multiple deployment modes with the same codebase:

| Mode | Description | Use Case | Database |
|------|-------------|----------|----------|
| **Embedded** | Library mode, no server | Development, testing, CLI tools | SQLite |
| **Server** | Single HTTP server | Teams, multi-tenant production | PostgreSQL |
| **Distributed** | Kubernetes-ready (planned) | Enterprise scale | PostgreSQL cluster |

### Mode Selection Logic

```python
# Connection priority (in nexus.connect())
1. If NEXUS_URL or config.url is set → RemoteNexusFS (server mode)
2. If mode="embedded" explicitly set → NexusFS (local mode)
3. Default → NexusFS with embedded warning
```

---

## Core Components

### Source Code Structure

```
src/nexus/
├── __init__.py           # Main entry point, connect() factory
├── config.py             # Configuration loading (env, yaml, dict)
├── core/
│   ├── filesystem.py     # NexusFilesystem abstract interface
│   ├── nexus_fs.py       # Main NexusFS implementation
│   ├── router.py         # Namespace routing and path resolution
│   ├── exceptions.py     # Custom exceptions
│   └── ace/              # ACE learning system
│       ├── trajectory.py # Agent action tracking
│       ├── reflection.py # Pattern analysis
│       └── learning.py   # Knowledge consolidation
├── backends/
│   ├── backend.py        # Backend abstract interface
│   ├── local.py          # LocalBackend (filesystem)
│   ├── gcs.py            # GCSBackend (Google Cloud Storage CAS)
│   ├── gcs_connector.py  # GCS direct path mapping
│   ├── s3_connector.py   # AWS S3 connector
│   ├── gdrive_connector.py # Google Drive OAuth
│   └── x_connector.py    # Twitter/X OAuth
├── storage/
│   ├── metadata.py       # MetadataStore (SQLAlchemy)
│   ├── models.py         # Database models
│   └── rebac.py          # ReBAC permission engine
├── server/
│   ├── rpc_server.py     # NexusRPCServer (HTTP JSON-RPC)
│   ├── protocol.py       # RPC message encoding/decoding
│   └── auth/             # Authentication providers
│       ├── static.py     # Static API key auth
│       ├── database.py   # Database-backed keys
│       └── oidc.py       # OpenID Connect
├── remote/
│   └── remote_fs.py      # RemoteNexusFS (HTTP client)
├── cli/
│   ├── main.py           # CLI entry point
│   └── commands/         # CLI command modules
│       ├── file_ops.py   # read, write, delete, cat, ls
│       ├── search.py     # grep, glob, semantic-search
│       ├── memory.py     # memory store/query
│       ├── rebac.py      # permission management
│       ├── skills.py     # skill operations
│       ├── mcp.py        # MCP server management
│       └── ...           # Other commands
├── mcp/
│   └── server.py         # MCP server implementation
├── search/
│   ├── semantic.py       # SemanticSearch engine
│   ├── embeddings.py     # Embedding providers (OpenAI, Voyage)
│   ├── chunking.py       # Document chunking strategies
│   └── vector_db.py      # Vector database abstraction
├── skills/
│   ├── registry.py       # SkillRegistry (three-tier)
│   ├── manager.py        # SkillManager (lifecycle)
│   ├── parser.py         # SKILL.md parser
│   ├── mcp_mount.py      # MCP server mounting
│   └── mcp_exporter.py   # Export tools as skills
├── workflows/
│   ├── engine.py         # WorkflowEngine
│   ├── triggers.py       # Event triggers
│   ├── actions.py        # Built-in actions
│   └── api.py            # WorkflowAPI
├── plugins/
│   ├── base.py           # Plugin base class
│   └── loader.py         # Auto-discovery via entry points
└── llm/
    └── reader.py         # LLM-powered document reading
```

### Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| **NexusFS** | Main filesystem implementation with all operations |
| **Backend** | Abstract storage interface (content-addressable) |
| **MetadataStore** | SQLAlchemy-based file metadata and indexes |
| **Router** | Path → namespace resolution and validation |
| **ReBAC** | Permission checking with tuple-based authorization |
| **MemoryAPI** | Agent memory store/query with embeddings |
| **SemanticSearch** | Hybrid vector + keyword search |
| **SkillRegistry** | Three-tier skill discovery and loading |
| **WorkflowEngine** | Event-driven automation pipelines |
| **MCP Server** | Model Context Protocol for AI agents |

---

## Storage Layer

### Backend Interface

All storage backends implement the `Backend` abstract class:

```python
class Backend(ABC):
    @abstractmethod
    async def store(self, content: bytes) -> str:
        """Store content, return content hash (CAS)"""

    @abstractmethod
    async def retrieve(self, content_hash: str) -> bytes:
        """Retrieve content by hash"""

    @abstractmethod
    async def delete(self, content_hash: str) -> bool:
        """Delete content by hash"""

    @abstractmethod
    async def exists(self, content_hash: str) -> bool:
        """Check if content exists"""
```

### Backend Types

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Storage Backend Types                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    Content-Addressable (CAS)                         │   │
│  │  Store by hash, automatic deduplication, 30-50% storage savings      │   │
│  │                                                                       │   │
│  │  ┌──────────────┐  ┌──────────────┐                                  │   │
│  │  │ LocalBackend │  │  GCSBackend  │                                  │   │
│  │  │ (filesystem) │  │ (GCS blobs)  │                                  │   │
│  │  └──────────────┘  └──────────────┘                                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Connector Backends                               │   │
│  │  Direct path mapping, preserve original structure, OAuth support     │   │
│  │                                                                       │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │   │
│  │  │GCSConnector  │  │ S3Connector  │  │GoogleDrive   │  │XConnector│ │   │
│  │  │(direct GCS)  │  │(direct S3)   │  │Connector     │  │(Twitter) │ │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  └──────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Mount System

Multiple backends can be mounted at different paths:

```yaml
# config.yaml
mounts:
  - mount_point: /workspace
    backend: local
    config:
      root_path: ./data

  - mount_point: /cloud/gcs
    backend: gcs_connector
    config:
      bucket: my-bucket
      project_id: my-project

  - mount_point: /cloud/s3
    backend: s3_connector
    config:
      bucket: my-s3-bucket
      region: us-east-1
```

---

## Permission System (ReBAC)

Nexus implements Relationship-Based Access Control inspired by Google Zanzibar.

### Permission Model

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ReBAC Permission Model                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Tuple Format: (subject, relation, object)                                   │
│  Example: (user:alice, direct_editor, file:/workspace/project1)              │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      Relation Hierarchy                              │   │
│  │                                                                       │   │
│  │    direct_owner ──────────────────────────────────────────┐          │   │
│  │         │                                                  │          │   │
│  │         ▼                                                  ▼          │   │
│  │    direct_editor ──────────┬─────────────────────────► owner        │   │
│  │         │                  │                              │          │   │
│  │         ▼                  ▼                              ▼          │   │
│  │    direct_viewer      editor ◄───────────────────────────┤          │   │
│  │         │                  │                              │          │   │
│  │         ▼                  ▼                              ▼          │   │
│  │       viewer ◄─────────────┴─────────────────────────────┘          │   │
│  │         │                                                            │   │
│  │         ▼                                                            │   │
│  │  Permissions: read, write, delete, admin                             │   │
│  │                                                                       │   │
│  │  viewer → read                                                        │   │
│  │  editor → read, write                                                 │   │
│  │  owner  → read, write, delete, admin                                 │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      Multi-Tenancy                                   │   │
│  │                                                                       │   │
│  │  tenant:acme ──────────────────────────────────────────┐             │   │
│  │       │                                                 │             │   │
│  │       ├── user:alice (member)                           │             │   │
│  │       ├── user:bob (member)                             │             │   │
│  │       │                                                 │             │   │
│  │       └── file:/workspace/acme/* (tenant isolation)     │             │   │
│  │                                                         │             │   │
│  │  tenant:bigcorp ────────────────────────────────────────┘             │   │
│  │       │                                                               │   │
│  │       ├── user:charlie (member)                                       │   │
│  │       └── file:/workspace/bigcorp/*                                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Permission Check Flow

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│   Request    │      │    ReBAC     │      │   Decision   │
│              │ ──▶  │   Engine     │ ──▶  │              │
│ user: alice  │      │              │      │  ✓ GRANTED   │
│ action: write│      │ 1. Check     │      │  ✗ DENIED    │
│ path: /doc   │      │    cache     │      │              │
│              │      │ 2. Check     │      │              │
└──────────────┘      │    tuples    │      └──────────────┘
                      │ 3. Resolve   │
                      │    hierarchy │
                      │ 4. Check     │
                      │    parent    │
                      └──────────────┘
```

---

## Skills System

The Skills System provides a three-tier hierarchy for managing reusable AI agent capabilities.

### Skill Hierarchy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Skills Hierarchy                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Tier 1: Agent Skills (Highest Priority)                            │   │
│  │  Path: /skills/agents/{agent_id}/                                   │   │
│  │  - Agent-specific customizations                                    │   │
│  │  - Override tenant and system skills                                │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Tier 2: Tenant Skills (Medium Priority)                            │   │
│  │  Path: /skills/tenants/{tenant_id}/                                 │   │
│  │  - Organization-specific skills                                     │   │
│  │  - Shared across tenant's agents                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Tier 3: System Skills (Lowest Priority)                            │   │
│  │  Path: /skills/system/                                              │   │
│  │  - Built-in Nexus capabilities                                      │   │
│  │  - MCP tool definitions                                             │   │
│  │  - Default behaviors                                                │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### MCP Integration

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       MCP Tool Integration                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  External MCP Servers                   Nexus Skills System                  │
│  ┌──────────────────┐                  ┌──────────────────┐                 │
│  │ @github/server   │ ──mount──────▶   │ /skills/system/  │                 │
│  │ @slack/server    │                  │   mcp-tools/     │                 │
│  │ @filesystem/     │                  │     github/      │                 │
│  │   server         │                  │     slack/       │                 │
│  └──────────────────┘                  │     filesystem/  │                 │
│                                        └──────────────────┘                 │
│                                                 │                            │
│                                                 ▼                            │
│                                        ┌──────────────────┐                 │
│                                        │  Dynamic Tool    │                 │
│                                        │  Discovery       │                 │
│                                        │                  │                 │
│                                        │  grep, glob →    │                 │
│                                        │  find relevant   │                 │
│                                        │  tools           │                 │
│                                        └──────────────────┘                 │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Search & Intelligence

### Semantic Search Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Semantic Search Pipeline                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. Document Ingestion                                                       │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                                                                        │  │
│  │  Document ──▶ Chunker ──▶ Embedder ──▶ Vector DB                      │  │
│  │                  │            │            │                           │  │
│  │            ┌─────┴─────┐ ┌────┴────┐ ┌────┴─────┐                     │  │
│  │            │ Strategies│ │Providers│ │ Backends │                     │  │
│  │            │ - fixed   │ │- OpenAI │ │- pgvector│                     │  │
│  │            │ - semantic│ │- Voyage │ │- sqlite  │                     │  │
│  │            │ - markdown│ │- Local  │ │  -vec    │                     │  │
│  │            └───────────┘ └─────────┘ └──────────┘                     │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  2. Query Processing (Hybrid Search)                                         │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                                                                        │  │
│  │  Query ───┬──▶ Keyword Search (FTS5/tsvector)                         │  │
│  │           │                                                            │  │
│  │           └──▶ Vector Search (cosine similarity)                       │  │
│  │                         │                                              │  │
│  │                         ▼                                              │  │
│  │                 Reciprocal Rank Fusion (RRF)                           │  │
│  │                         │                                              │  │
│  │                         ▼                                              │  │
│  │                 Ranked Results                                         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Memory API Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Memory API                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      memory.store(content)                           │   │
│  │                             │                                         │   │
│  │                             ▼                                         │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │                    Memory Processing                          │   │   │
│  │  │                                                                │   │   │
│  │  │  1. Generate embedding vector                                  │   │   │
│  │  │  2. Store in /memory/{tenant}/{user}/                         │   │   │
│  │  │  3. Index for semantic search                                  │   │   │
│  │  │  4. Associate metadata (timestamp, scope, tags)               │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │               memory.query(query, user_id, scope)                    │   │
│  │                             │                                         │   │
│  │                             ▼                                         │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │                    Memory Retrieval                           │   │   │
│  │  │                                                                │   │   │
│  │  │  1. Embed query                                                │   │   │
│  │  │  2. Vector similarity search                                   │   │   │
│  │  │  3. Filter by scope (user, tenant, global)                    │   │   │
│  │  │  4. Return ranked memories                                     │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## MCP Integration

Nexus provides a Model Context Protocol server for AI agent integration.

### MCP Server Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MCP Server Architecture                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  AI Agent (Claude, GPT, etc.)                                                │
│       │                                                                      │
│       │  MCP Protocol (stdio or HTTP)                                        │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      Nexus MCP Server                                │   │
│  │                                                                       │   │
│  │  Tools Exposed:                                                       │   │
│  │  ┌────────────────────────────────────────────────────────────────┐ │   │
│  │  │ File Operations        │ Search           │ Memory             │ │   │
│  │  │ - nexus_read          │ - nexus_grep     │ - nexus_memory_    │ │   │
│  │  │ - nexus_write         │ - nexus_glob     │   store            │ │   │
│  │  │ - nexus_delete        │ - nexus_semantic_│ - nexus_memory_    │ │   │
│  │  │ - nexus_list          │   search         │   query            │ │   │
│  │  │ - nexus_tree          │                  │                     │ │   │
│  │  └────────────────────────────────────────────────────────────────┘ │   │
│  │                                                                       │   │
│  │  Resources:                                                           │   │
│  │  ┌────────────────────────────────────────────────────────────────┐ │   │
│  │  │ - nexus://workspace/*  (file browsing)                         │ │   │
│  │  │ - nexus://memory/*     (memory browsing)                       │ │   │
│  │  │ - nexus://skills/*     (skill browsing)                        │ │   │
│  │  └────────────────────────────────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│       │                                                                      │
│       │  JSON-RPC                                                            │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      NexusFS / RemoteNexusFS                         │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Transport Modes

| Transport | Use Case | Command |
|-----------|----------|---------|
| **stdio** | Claude Desktop, local agents | `nexus mcp serve --transport stdio` |
| **HTTP** | Remote agents, web services | `nexus mcp serve --transport http --port 8081` |

---

## Server Architecture

### RPC Server Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         RPC Server Architecture                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  HTTP Request                                                                │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Authentication Layer                             │   │
│  │                                                                       │   │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐    │   │
│  │  │Static Key  │  │Database Key│  │   OIDC     │  │   OAuth    │    │   │
│  │  │ (env var)  │  │(PostgreSQL)│  │ (JWT/JWKS) │  │(Google,etc)│    │   │
│  │  └────────────┘  └────────────┘  └────────────┘  └────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Request Handler                                  │   │
│  │                                                                       │   │
│  │  1. Parse JSON-RPC request                                           │   │
│  │  2. Validate method and params                                       │   │
│  │  3. Set operation context (user, tenant)                             │   │
│  │  4. Dispatch to NexusFS method                                       │   │
│  │  5. Encode JSON-RPC response                                         │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         NexusFS                                      │   │
│  │                                                                       │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │                   Operation Context                           │   │   │
│  │  │  - user_id: "alice"                                           │   │   │
│  │  │  - tenant_id: "acme"                                          │   │   │
│  │  │  - is_admin: false                                            │   │   │
│  │  │  - request_id: "uuid-123"                                     │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  │                          │                                           │   │
│  │                          ▼                                           │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │  Permission Check → Backend Operation → Response              │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /api/nfs` | JSON-RPC endpoint for all operations |
| `GET /health` | Health check |
| `GET /api/stats` | Server statistics |

---

## CLI Architecture

### Command Structure

```
nexus
├── File Operations
│   ├── ls          # List directory contents
│   ├── cat         # Display file contents
│   ├── read        # Read file to stdout
│   ├── write       # Write content to file
│   ├── rm          # Delete file or directory
│   ├── mkdir       # Create directory
│   ├── cp          # Copy files
│   └── mv          # Move files
│
├── Search
│   ├── grep        # Pattern search in files
│   ├── glob        # Find files by pattern
│   └── semantic-search  # Vector-based search
│
├── Memory
│   ├── memory store   # Store memory
│   └── memory query   # Query memories
│
├── Permissions
│   ├── rebac create   # Create permission tuple
│   ├── rebac check    # Check permission
│   ├── rebac delete   # Delete tuple
│   └── rebac explain  # Explain permission path
│
├── Skills
│   ├── skills list    # List available skills
│   ├── skills create  # Create new skill
│   ├── skills info    # Show skill details
│   └── skills mcp     # MCP integration commands
│
├── Administration
│   ├── admin create-key  # Create API key
│   ├── admin list-keys   # List API keys
│   ├── oauth setup-*     # Configure OAuth
│   └── serve             # Start RPC server
│
└── MCP
    └── mcp serve      # Start MCP server
```

---

## Data Flow Examples

### File Write Operation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         File Write Data Flow                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. nx.write("/workspace/doc.txt", b"content")                               │
│       │                                                                      │
│       ▼                                                                      │
│  2. Router: Resolve namespace ("/workspace" → default mount)                 │
│       │                                                                      │
│       ▼                                                                      │
│  3. ReBAC: Check write permission                                            │
│     - Subject: user:alice                                                    │
│     - Action: write                                                          │
│     - Object: file:/workspace/doc.txt                                        │
│       │                                                                      │
│       ├── ✗ DENIED → Raise NexusPermissionError                             │
│       │                                                                      │
│       ▼ ✓ GRANTED                                                           │
│  4. Backend: Store content (hash = sha256(content))                          │
│       │                                                                      │
│       ▼                                                                      │
│  5. MetadataStore: Create/update file record                                 │
│     - path: /workspace/doc.txt                                               │
│     - content_hash: abc123...                                                │
│     - size: 7                                                                │
│     - version: 2                                                             │
│       │                                                                      │
│       ▼                                                                      │
│  6. WorkflowEngine: Check triggers                                           │
│     - Event: file.written                                                    │
│     - Pattern: /workspace/*.txt                                              │
│     - → Execute matching workflows                                           │
│       │                                                                      │
│       ▼                                                                      │
│  7. Return: FileMetadata                                                     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Semantic Search Operation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Semantic Search Data Flow                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. nx.semantic_search("/docs/**/*.md", "authentication setup")              │
│       │                                                                      │
│       ▼                                                                      │
│  2. Glob: Find matching files                                                │
│     → [/docs/api/auth.md, /docs/setup.md, ...]                              │
│       │                                                                      │
│       ▼                                                                      │
│  3. Filter: Check read permissions for each file                             │
│       │                                                                      │
│       ▼                                                                      │
│  4. EmbeddingProvider: Generate query embedding                              │
│     → [0.1, -0.3, 0.5, ...]  (1536 dimensions)                              │
│       │                                                                      │
│       ▼                                                                      │
│  5. VectorDB: Cosine similarity search                                       │
│     - pgvector: SELECT ... ORDER BY embedding <=> query_vec                  │
│     - sqlite-vec: SELECT ... ORDER BY vec_distance_cosine(...)              │
│       │                                                                      │
│       ▼                                                                      │
│  6. FTS: Keyword search (parallel)                                           │
│     - PostgreSQL: to_tsvector @@ to_tsquery                                  │
│     - SQLite: FTS5 MATCH                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  7. RRF: Merge and rank results                                              │
│     score = 1/(k + rank_vector) + 1/(k + rank_keyword)                       │
│       │                                                                      │
│       ▼                                                                      │
│  8. Return: List[SemanticSearchResult]                                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema

### Core Tables

```sql
-- File metadata
CREATE TABLE files (
    id UUID PRIMARY KEY,
    path VARCHAR NOT NULL UNIQUE,
    content_hash VARCHAR NOT NULL,
    size BIGINT NOT NULL,
    mime_type VARCHAR,
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    tenant_id VARCHAR,
    metadata JSONB
);

-- Version history
CREATE TABLE file_versions (
    id UUID PRIMARY KEY,
    file_id UUID REFERENCES files(id),
    version INTEGER NOT NULL,
    content_hash VARCHAR NOT NULL,
    created_at TIMESTAMP,
    created_by VARCHAR
);

-- ReBAC permission tuples
CREATE TABLE rebac_tuples (
    id UUID PRIMARY KEY,
    subject_type VARCHAR NOT NULL,
    subject_id VARCHAR NOT NULL,
    relation VARCHAR NOT NULL,
    object_type VARCHAR NOT NULL,
    object_id VARCHAR NOT NULL,
    tenant_id VARCHAR,
    created_at TIMESTAMP,
    UNIQUE(subject_type, subject_id, relation, object_type, object_id, tenant_id)
);

-- API keys
CREATE TABLE api_keys (
    id UUID PRIMARY KEY,
    key_hash VARCHAR NOT NULL UNIQUE,
    key_prefix VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    tenant_id VARCHAR,
    permissions JSONB,
    expires_at TIMESTAMP,
    created_at TIMESTAMP
);

-- Vector embeddings (pgvector)
CREATE TABLE embeddings (
    id UUID PRIMARY KEY,
    file_id UUID REFERENCES files(id),
    chunk_index INTEGER,
    content TEXT,
    embedding vector(1536),
    metadata JSONB
);

CREATE INDEX ON embeddings USING ivfflat (embedding vector_cosine_ops);
```

---

## Security Architecture

### Authentication Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Authentication Flow                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Request                                                                     │
│    │                                                                         │
│    ▼                                                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    Auth Middleware                                   │   │
│  │                                                                       │   │
│  │  1. Extract token from Authorization header                          │   │
│  │     - Bearer sk-xxx (API key)                                        │   │
│  │     - Bearer eyJ... (JWT)                                            │   │
│  │                                                                       │   │
│  │  2. Try auth providers in order:                                     │   │
│  │     ┌────────────┐  ┌────────────┐  ┌────────────┐                  │   │
│  │     │Static Key  │→ │Database Key│→ │   OIDC     │                  │   │
│  │     │(env match) │  │(hash lookup│  │(JWT verify)│                  │   │
│  │     └────────────┘  └────────────┘  └────────────┘                  │   │
│  │                                                                       │   │
│  │  3. Success → Set user context                                       │   │
│  │     - user_id                                                         │   │
│  │     - tenant_id                                                       │   │
│  │     - permissions                                                     │   │
│  │                                                                       │   │
│  │  4. Failure → 401 Unauthorized                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Security Layers

| Layer | Protection |
|-------|-----------|
| **Transport** | HTTPS (TLS 1.3) |
| **Authentication** | API keys, OIDC, OAuth |
| **Authorization** | ReBAC permission checks |
| **Tenant Isolation** | Namespace separation, query filtering |
| **Input Validation** | Path sanitization, size limits |
| **Audit** | Operation logging with versioning |

---

## Deployment Architecture

### Docker Compose Stack

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Docker Compose Deployment                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                        External Access                                 │  │
│  │                                                                        │  │
│  │   Frontend (5173)    API (8080)    MCP (8081)    LangGraph (2024)    │  │
│  └────────┬─────────────────┬─────────────┬─────────────┬───────────────┘  │
│           │                 │             │             │                   │
│           ▼                 ▼             ▼             ▼                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     Docker Network (nexus-network)                   │   │
│  │                                                                       │   │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐     │   │
│  │  │  Frontend  │  │   Nexus    │  │    MCP     │  │  LangGraph │     │   │
│  │  │  (React)   │  │  (Server)  │  │  (Server)  │  │  (Agent)   │     │   │
│  │  │  :5173     │  │  :8080     │  │  :8081     │  │  :2024     │     │   │
│  │  └────────────┘  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘     │   │
│  │                        │               │               │             │   │
│  │                        └───────────────┴───────────────┘             │   │
│  │                                    │                                  │   │
│  │                                    ▼                                  │   │
│  │                          ┌────────────────┐                          │   │
│  │                          │   PostgreSQL   │                          │   │
│  │                          │    (15)        │                          │   │
│  │                          │   :5432        │                          │   │
│  │                          └────────────────┘                          │   │
│  │                                    │                                  │   │
│  │                         ┌──────────┴──────────┐                      │   │
│  │                         ▼                     ▼                      │   │
│  │                  postgres-data           nexus-data                  │   │
│  │                   (volume)                (volume)                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### GCP Production Deployment

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      GCP Production Architecture                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│                              Internet                                        │
│                                 │                                            │
│                                 ▼                                            │
│                     ┌─────────────────────┐                                 │
│                     │   Caddy (HTTPS)     │                                 │
│                     │ nexus.sudorouter.ai │                                 │
│                     └──────────┬──────────┘                                 │
│                                │                                            │
│                                ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                 GCE VM: nexus-server-spot                            │   │
│  │                 (e2-standard-2, Static IP)                           │   │
│  │                                                                       │   │
│  │  ┌───────────────────────────────────────────────────────────────┐  │   │
│  │  │                   Docker Compose Stack                         │  │   │
│  │  │                                                                 │  │   │
│  │  │  Frontend (5173) ─── Nexus (8080) ─── LangGraph (2024)        │  │   │
│  │  │                          │                                      │  │   │
│  │  │                          ▼                                      │  │   │
│  │  │                    PostgreSQL (5432)                            │  │   │
│  │  └───────────────────────────────────────────────────────────────┘  │   │
│  │                                                                       │   │
│  │  Storage:                                                             │   │
│  │  - /app/data (local)                                                  │   │
│  │  - GCS bucket (optional)                                              │   │
│  │  - S3 bucket (optional)                                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Future Roadmap

Based on open issues and planned features:

| Area | Planned Features |
|------|-----------------|
| **Performance** | Daemon mode (#447), stream optimization (#480) |
| **Search** | Hot daemon for semantic search (#490), RRF hybrid search (#489) |
| **Skills** | Claude Code integration (#487), Claude skills (#488) |
| **Testing** | MCPMark benchmark integration (#492) |
| **Agents** | Multi-agent research system (#446) |

---

## References

- [README.md](README.md) - Quick start guide
- [docs/api/](docs/api/) - API documentation
- [CONTRIBUTING.md](CONTRIBUTING.md) - Development guidelines
- [Google Zanzibar Paper](https://research.google/pubs/pub48190/) - ReBAC inspiration

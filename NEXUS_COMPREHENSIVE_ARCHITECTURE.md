# Nexus: AI-Native Distributed Filesystem Architecture

**Version**: 5.0
**Date**: 2025-10-12
**Status**: Production Specification

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. ["Everything as a File" Paradigm](#everything-as-a-file-paradigm)
3. [System Architecture Overview](#system-architecture-overview)
4. [Core Design Principles](#core-design-principles)
5. [Core Components](#core-components)
6. [Complete Database Schema](#complete-database-schema)
7. [Authentication & Authorization](#authentication--authorization)
8. [Complete API Specification](#complete-api-specification)
   - [Rich Format Parsing](#rich-format-parsing)
   - [Parser System Architecture](#parser-system-architecture)
   - [LLM-Powered Semantic Read with KV Cache](#llm-powered-semantic-read-with-kv-cache)
9. [Backend Implementations](#backend-implementations)
10. [Agent Memory System (ReasoningBank)](#agent-memory-system-reasoningbank)
11. [Content Processing Pipeline (Supermemory)](#content-processing-pipeline-supermemory)
12. [Job System with Checkpointing (Timelinize)](#job-system-with-checkpointing-timelinize)
13. [Model Context Protocol (MCP) Integration](#model-context-protocol-mcp-integration)
14. [OAuth & Runtime Credentials](#oauth--runtime-credentials)
15. [Backend Recognition & Auto-Mount](#backend-recognition--auto-mount)
16. [Performance & Optimization](#performance--optimization)
17. [Clustering & Distribution](#clustering--distribution)
18. [Technology Stack](#technology-stack)
19. [Testing Strategy](#testing-strategy)
20. [Deployment Architecture: Three Modes, One Codebase](#deployment-architecture-three-modes-one-codebase)
    - [Mode 1: Embedded/Library Mode](#mode-1-embeddedlibrary-mode)
    - [Mode 2: Monolithic Server Mode](#mode-2-monolithic-server-mode)
    - [Mode 3: Distributed Mode](#mode-3-distributed-mode)
    - [Implementation: Pluggable Components](#implementation-pluggable-components)
    - [Migration Path: Seamless Growth](#migration-path-seamless-growth)
    - [Decision Matrix](#decision-matrix-which-mode-to-use)
    - [Competitive Advantage: Library Mode](#competitive-advantage-library-mode)
21. [Python SDK Interface](#python-sdk-interface)

---

## Executive Summary

### What is Nexus 5.0?

Nexus is now a **complete AI agent infrastructure platform** that combines:

**Foundation** (Original Nexus + CallFS + Alluxio + Spacedrive):
- Distributed unified filesystem
- Multi-backend abstraction
- Tiered storage with lineage
- Content-addressable deduplication

**Agent Intelligence**:
- Self-evolving agent memory with consolidation
- Memory versioning and lineage tracking
- Multi-agent memory sharing
- Memory effectiveness analytics

**Content Processing**:
- Intelligent document processing pipeline
- Extensible rich format parsing (PDF, Excel, CSV, JSON, etc.)
- LLM KV cache management for 50-90% cost savings
- Semantic chunking for better search
- Model Context Protocol (MCP) server
- Document type detection and routing

**Operations**:
- Resumable job system with checkpointing
- Auto-refreshing OAuth token management
- Backend recognition and auto-mounting
- CPU throttling and resource management

**Three Deployment Modes** (Unique Architecture):
- Embedded/Library mode for zero-deployment (like SQLite)
- Monolithic server for teams (single Docker container)
- Distributed mode for enterprise scale (Kubernetes-ready)
- Seamless migration path from dev to production

### Key Differentiators

```mermaid
graph TB
    subgraph "Nexus 5.0 Unique Value"
        A[Unified File System] --> B[Agent Workspaces]
        A --> C[Semantic Memory]
        B --> D[Self-Evolving Memory]
        C --> D
        D --> E[Agent Intelligence]

        F[Content Pipeline] --> C
        G[MCP Integration] --> B
        H[Job System] --> F
        I[OAuth Management] --> A

        style E fill:#90EE90
        style D fill:#87CEEB
        style A fill:#FFD700
    end
```

### Performance & Capabilities

| Capability | Target | Impact |
|------------|--------|--------|
| **Write Throughput** | 500-1000 MB/s | 10-50× vs direct backend |
| **Read Latency** | <10ms | 10-50× vs remote storage |
| **Memory Search** | <100ms | Vector search across all agent memories |
| **Storage Savings** | 30-50% | CAS deduplication |
| **Job Resumability** | 100% | Survives restarts |
| **Agent Memory Evolution** | Automatic | Self-improving from experience |
| **MCP Tool Access** | Native | Claude/Cursor direct integration |
| **Rich Format Parsing** | Extensible | PDF, Excel, CSV → structured JSON |
| **LLM KV Cache** | Automatic | 50-90% cost savings on AI queries |
| **Deployment Flexibility** | 3 Modes | Library, Monolithic, or Distributed |

---

## "Everything as a File" Paradigm

### Learnings from Codex & Claude Code

**Insight**: OpenAI's Codex and Anthropic's Claude Code both implement "everything as a file" architectures where configuration, commands, agents, memory, and state are **all stored as files**. This enables:

1. **Zero-deployment extensibility** - Drop a markdown file, get a new command
2. **Version-controllable AI behavior** - Git tracks agent evolution
3. **Human-readable state** - Engineers can inspect and edit agent knowledge
4. **MCP-native** - File-based tools integrate seamlessly

**Nexus adopts this paradigm** to become the **canonical storage layer for AI agents**.

---

### Agent Workspace Structure

Every agent gets a structured workspace at `/workspace/{tenant}/{agent}/`:

```
/workspace/acme-corp/research-agent/
├── .nexus/                          # Nexus metadata (Git-trackable)
│   ├── agent.yaml                   # Agent configuration
│   ├── commands/                    # Custom commands (markdown files)
│   │   ├── analyze-codebase.md
│   │   └── summarize-docs.md
│   ├── jobs/                        # Background job definitions (YAML)
│   │   └── daily-summary.yaml
│   ├── memory/                      # File-based memory (human-readable)
│   │   ├── project-knowledge.md
│   │   └── recent-tasks.jsonl
│   └── secrets.encrypted            # KMS-encrypted credentials
├── data/                            # Agent's working data
│   ├── inputs/
│   └── outputs/
└── INSTRUCTIONS.md                  # Agent instructions (auto-loaded)
```

**Key Principles**:
- **`.nexus/` directory**: Stores all Nexus metadata (like `.git`, `.vscode`)
- **Configuration as files**: Agent config in YAML, human-readable
- **Commands as markdown**: Custom agent commands with frontmatter
- **Memory as files**: Knowledge in markdown, auto-indexed for semantic search
- **Jobs as YAML**: Declarative workflow definitions

---

### Path Namespace Conventions

```
/                                # Root
├── workspace/                   # Agent scratch space (hot tier, ephemeral)
│   └── {tenant}/
│       └── {agent}/
│           ├── .nexus/          # Configuration & metadata
│           ├── data/            # Working data
│           └── INSTRUCTIONS.md  # Agent instructions
├── shared/                      # Shared tenant data (warm tier, persistent)
│   └── {tenant}/
│       ├── datasets/
│       ├── models/
│       └── documents/
├── external/                    # Pass-through backends (no content storage)
│   ├── gdrive/                  # Google Drive mount
│   ├── s3/                      # S3 mount
│   └── sharepoint/              # SharePoint mount
├── system/                      # System metadata (admin-only, immutable)
│   ├── tenants/
│   ├── agents/
│   └── api_keys/
└── archives/                    # Cold storage (S3, read-only)
    └── {tenant}/
        └── {year}/{month}/
```

**Access Control**:
- `/workspace/{tenant}/{agent}/` → Agent owns (full access)
- `/shared/{tenant}/` → Tenant-wide read, selective write
- `/external/` → Backed by external ACLs
- `/system/` → Admin-only
- `/archives/` → Read-only (immutable archives)

---

### Example: Agent Configuration File

**File**: `/workspace/acme-corp/research-agent/.nexus/agent.yaml`

```yaml
agent:
  name: research-agent
  version: 1.0
  model: claude-sonnet-4

  # Permissions
  scopes: [read, write, semantic_search]

  # Tool policies
  tools:
    allowed: [glob, grep, semantic_read, read_pdf, read_excel]
    denied: [delete, bash]

  # Backend mounts
  mounts:
    - mount_point: /papers
      backend: s3
      bucket: research-papers
      readonly: true
    - mount_point: /notes
      backend: workspace
      tier: hot

  # Memory configuration
  memory:
    consolidation:
      enabled: true
      schedule: "0 2 * * *"  # 2 AM daily
      max_memories: 1000
    pii_redaction: true

  # Scheduled jobs
  jobs:
    daily_summary:
      enabled: true
      schedule: "0 18 * * *"  # 6 PM
```

**Benefits**:
- ✅ Human-readable, editable
- ✅ Version-controlled (Git)
- ✅ Self-documenting
- ✅ Declarative (describe state, not imperative commands)

---

### Example: Command as Markdown

**File**: `/workspace/acme-corp/research-agent/.nexus/commands/semantic-search.md`

```markdown
---
name: semantic-search
description: Search codebase semantically
allowed-tools: [semantic_read, glob, grep]
required-scopes: [read]
model: sonnet
---

## Context

- Current directory: !`pwd`
- File count: !`find . -type f | wc -l`
- Git branch: !`git branch --show-current`

## Your task

Given query: {{query}}

1. Use `glob` to find relevant files by pattern
2. Use `semantic_read` to extract relevant sections
3. Summarize findings with file:line citations

Output format: Markdown with citations
```

**Features**:
- **Frontmatter**: YAML metadata (tools, scopes, model)
- **`!backtick` syntax**: Execute shell commands and embed results
- **`{{variable}}` syntax**: Template variables from API call
- **Human-readable**: Engineers can read, edit, and version control

**Usage**:
```python
result = await client.execute_command(
    "semantic-search",
    context={"query": "authentication implementation"}
)
```

---

### Example: Memory as Files

**File**: `/workspace/acme-corp/research-agent/.nexus/memory/project-knowledge.md`

```markdown
# Project: Acme E-Commerce

## Architecture Decisions
- Using microservices (auth, cart, payment)
- PostgreSQL for transactional data
- Redis for sessions

## Recent Changes
- 2025-10-10: Migrated auth to OAuth2 (see #commit-abc123)
- 2025-10-09: Added cart persistence (see /docs/cart-design.md)

## Known Issues
- Cart abandonment rate high (investigate)
- Auth token refresh failure handling
```

**Auto-Indexing**:
1. On write, Nexus chunks the markdown
2. Generates embeddings (e.g., text-embedding-3-large)
3. Stores in Qdrant with `source_file="project-knowledge.md"` metadata
4. Semantic search returns: vector matches + source file references

**Benefits**:
- ✅ **Human-readable**: Engineers can edit agent knowledge
- ✅ **Version-controlled**: Git tracks knowledge evolution
- ✅ **Semantic search**: Auto-indexed for vector search
- ✅ **Citeable**: Agent can reference `project-knowledge.md:12-15`

---

### Example: Declarative Job Definition

**File**: `/workspace/acme-corp/research-agent/.nexus/jobs/daily-report.yaml`

```yaml
name: daily-research-report
description: Generate daily summary of new research papers
schedule: "0 18 * * *"  # 6 PM daily
enabled: true
model: sonnet

workflow:
  steps:
    - name: fetch-recent-papers
      command: /search-papers
      args:
        date_range: today
        topics: ["machine-learning", "nlp"]

    - name: summarize
      command: /summarize
      args:
        input: "{{ steps.fetch-recent-papers.output }}"
        format: markdown
        max_length: 500

    - name: email-report
      tool: email
      args:
        to: "team@acme.com"
        subject: "Daily Research Summary - {{ date }}"
        body: "{{ steps.summarize.output }}"

  on_failure:
    notify: ["slack://research-alerts"]
```

**Features**:
- **Cron schedule**: Standard cron syntax
- **Multi-step workflows**: Steps can reference previous outputs
- **Command execution**: Calls custom commands defined in `.nexus/commands/`
- **Error handling**: Configurable failure notifications

---

### Competitive Advantages

By adopting "everything as a file", Nexus becomes:

1. **The File System for AI Agents**
   - Not just storage, but **agent infrastructure**
   - Configuration, memory, jobs, commands—all as files

2. **Zero-Deployment Extensibility**
   - Drop a `.md` file → new command available
   - Drop a `.yaml` file → new job scheduled
   - No code deployment, no downtime

3. **Version-Controllable AI Behavior**
   - Agent configs in Git → audit trail
   - Memory files in Git → knowledge evolution
   - Commands in Git → behavior history

4. **MCP-Native Integration**
   - Claude Desktop / Cursor see Nexus as MCP server
   - Agents use Nexus tools (`nexus_read`, `nexus_write`, etc.)
   - No custom SDK needed

5. **Human-In-The-Loop**
   - Engineers inspect agent memory (markdown files)
   - Edit configurations directly (YAML)
   - Debug by reading files, not querying APIs

---

## System Architecture Overview

### Three Deployment Modes

Nexus supports three deployment modes from a single codebase. The architecture below shows the **Distributed Mode** (full production stack). Embedded and Monolithic modes use simplified variants of these components.

| Mode | Storage | Cache | Vector | Use Case |
|------|---------|-------|--------|----------|
| **Embedded** | SQLite | In-memory | Qdrant embedded | Individual developers, CLI tools |
| **Monolithic** | SQLite/Postgres | Memory/Redis | Qdrant embedded/cloud | Small teams (1-20 users) |
| **Distributed** | Postgres cluster | Redis cluster | Qdrant cluster | Enterprises (100+ users) |

See [Section 21: Deployment Architecture](#deployment-architecture-three-modes-one-codebase) for detailed comparison.

---

### High-Level System Architecture

**Component Overview** (all modes share these core components):

```mermaid
graph TB
    subgraph "Client Interfaces"
        CLI[Python Library<br/>nexus.Embedded]
        SDK[Python SDK<br/>REST Client]
        MCP[MCP Clients<br/>Claude/Cursor]
        Agents[AI Agents<br/>LangChain/AutoGPT]
    end

    subgraph "Entry Points"
        EmbeddedAPI[Embedded API<br/>Direct Function Calls]
        RestAPI[REST API<br/>/v1/*]
        MCPServer[MCP Server<br/>stdio/SSE]
    end

    subgraph "Core Engine - Always Present"
        Engine[Core Engine<br/>Request Orchestrator]

        subgraph "Routing & Storage"
            PathRouter[Path Router<br/>Virtual → Physical]
            BackendMgr[Backend Manager<br/>LocalFS/S3/GDrive]
            MetadataStore[Metadata Store<br/>SQLite or Postgres]
        end

        subgraph "AI Features"
            Parser[Parser Registry<br/>PDF/Excel/CSV]
            LLM[LLM Manager<br/>Claude/OpenAI/Gemini]
            KVCache[KV Cache Manager<br/>50-90% savings]
            SemanticSearch[Semantic Search<br/>Vector DB]
        end

        subgraph "Agent Intelligence"
            Memory[Agent Memory<br/>ReasoningBank]
            Consolidation[Memory Consolidation]
        end

        subgraph "Operations"
            Jobs[Job System<br/>Checkpointing]
            OAuth[OAuth Manager<br/>Token Refresh]
            Locks[Lock Manager<br/>File/Redis]
        end
    end

    subgraph "Storage Backends"
        LocalFS[Local Filesystem]
        S3[Amazon S3]
        GDrive[Google Drive]
        SharePoint[SharePoint]
    end

    subgraph "Infrastructure - Mode Dependent"
        DB[(Database<br/>SQLite → Postgres)]
        Cache[(Cache<br/>Memory → Redis)]
        Vector[(Vector DB<br/>Qdrant)]
    end

    %% Client to Entry
    CLI --> EmbeddedAPI
    SDK --> RestAPI
    MCP --> MCPServer
    Agents --> SDK

    %% Entry to Engine
    EmbeddedAPI --> Engine
    RestAPI --> Engine
    MCPServer --> Engine

    %% Engine to Routing
    Engine --> PathRouter
    PathRouter --> BackendMgr
    PathRouter --> MetadataStore

    %% Engine to AI
    Engine --> Parser
    Engine --> LLM
    LLM --> KVCache
    Engine --> SemanticSearch

    %% Engine to Agent Intelligence
    Engine --> Memory
    Memory --> Consolidation

    %% Engine to Operations
    Engine --> Jobs
    Engine --> OAuth
    Engine --> Locks

    %% Backend Manager to Storage
    BackendMgr --> LocalFS
    BackendMgr --> S3
    BackendMgr --> GDrive
    BackendMgr --> SharePoint

    %% Infrastructure Connections
    MetadataStore --> DB
    Locks --> Cache
    KVCache --> Cache
    SemanticSearch --> Vector
    Memory --> Vector

    %% Styling
    style Engine fill:#90EE90
    style Parser fill:#DDA0DD
    style LLM fill:#FFA07A
    style KVCache fill:#FFA07A
    style Memory fill:#87CEEB
    style SemanticSearch fill:#87CEEB
```

**Key Architecture Principles**:

1. **Core Engine is Mode-Agnostic**: Same components in embedded, monolithic, and distributed
2. **Entry Points Vary**: Embedded = function calls, Server = REST/MCP
3. **Infrastructure Scales**: SQLite → Postgres, Memory → Redis, Single → Cluster
4. **Pluggable Backends**: LocalFS, S3, GDrive all use same interface

**Request Flow (Simplified)**:
```
User Request
  → Entry Point (Embedded API / REST API / MCP)
  → Core Engine
  → Path Router (resolve virtual path)
  → Metadata Store (lookup backend)
  → Backend Manager (select backend)
  → Storage Backend (LocalFS/S3/etc)
  → [Optional] Parser (if format=parsed)
  → [Optional] LLM Manager (if semantic query)
  → Response
```

---

### Detailed Architecture by Deployment Mode

Now let's see how these components are configured in each mode:



#### Embedded Mode Architecture (Python Library)

**Example 1: Remote Storage (GCS) accessed like local files**

```mermaid
sequenceDiagram
    participant User as Python Code
    participant Engine as Core Engine
    participant PathRouter as Path Router
    participant BackendMgr as Backend Manager
    participant GCS as GCS Backend<br/>(Remote)
    participant LocalFS as LocalFS Backend<br/>(Local)
    participant Parser as Parser Registry
    participant LLM as LLM Manager
    participant Cache as KV Cache
    participant SQLite as SQLite<br/>(embedded)

    Note over User: User queries file on GCS (transparent!)
    User->>Engine: nx.llm_read("/cloud/reports/q4.pdf", "summarize")

    Note over Engine,SQLite: 1. Resolve Virtual Path
    Engine->>PathRouter: route("/cloud/reports/q4.pdf")
    PathRouter->>SQLite: get_metadata("/cloud/reports/q4.pdf")
    SQLite-->>PathRouter: {backend: "gcs", bucket: "my-bucket", path: "reports/q4.pdf"}

    Note over PathRouter,GCS: 2. Backend Selection & File Retrieval
    PathRouter->>BackendMgr: get_backend("gcs")
    BackendMgr-->>PathRouter: GCS backend (with credentials)

    PathRouter->>GCS: read_file("reports/q4.pdf")
    Note over GCS: Fetches from gs://my-bucket/reports/q4.pdf
    GCS-->>PathRouter: file_bytes (PDF from cloud)

    Note over Engine,Parser: 3. Content Processing (same for all backends!)
    Engine->>Parser: detect_format("q4.pdf")
    Parser-->>Engine: "application/pdf"

    Engine->>Parser: parse_pdf(file_bytes)
    Parser-->>Engine: {type: "document", text: "Q4 revenue..."}

    Note over Engine,Cache: 4. LLM Query with Caching
    Engine->>LLM: semantic_read(content, "summarize")
    LLM->>Cache: check_cache(file_hash + query)

    alt Cache Hit
        Cache-->>LLM: cached_response (90% savings)
    else Cache Miss
        LLM->>LLM: call_claude_api(content, query)
        LLM->>Cache: store_in_provider_cache(response)
    end

    LLM-->>Engine: answer
    Engine-->>User: "Summary: Q4 revenue grew 25%..."

    Note over User: Now access local file - SAME interface!
    User->>Engine: nx.read_file("/local/data/input.csv")
    Engine->>PathRouter: route("/local/data/input.csv")
    PathRouter->>SQLite: get_metadata("/local/data/input.csv")
    SQLite-->>PathRouter: {backend: "local", path: "data/input.csv"}

    PathRouter->>BackendMgr: get_backend("local")
    BackendMgr-->>PathRouter: LocalFS backend

    PathRouter->>LocalFS: read_file("data/input.csv")
    LocalFS-->>PathRouter: file_bytes (CSV from disk)
    PathRouter-->>Engine: content
    Engine-->>User: CSV data
```

**Configuration Example** (Embedded Mode with Multiple Backends):

```python
import nexus

# Configure embedded mode with both local and remote storage
nx = nexus.Embedded(
    data_dir="./nexus-data",
    backends=[
        {
            "name": "local",
            "type": "localfs",
            "path": "/Users/alice/Documents",
            "mount_point": "/local"  # Virtual path prefix
        },
        {
            "name": "gcs",
            "type": "gcs",
            "bucket": "my-company-data",
            "credentials": "~/.gcp/credentials.json",
            "mount_point": "/cloud"  # Virtual path prefix
        },
        {
            "name": "s3",
            "type": "s3",
            "bucket": "research-papers",
            "region": "us-west-2",
            "mount_point": "/papers"  # Virtual path prefix
        }
    ]
)

# All backends accessed with same interface!
local_file = nx.read_file("/local/notes.txt")           # From local disk
cloud_file = nx.read_file("/cloud/reports/q4.pdf")     # From GCS
papers = nx.read_file("/papers/ml/attention.pdf")      # From S3

# LLM queries work across ALL backends
answer = nx.llm_read("/cloud/reports/q4.pdf", "key findings?")  # GCS file
summary = nx.llm_read("/papers/ml/attention.pdf", "explain")    # S3 file
```

**Virtual Path Mapping**:

```
Virtual Path              Backend Type    Physical Location
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
/local/notes.txt      →   LocalFS      →  /Users/alice/Documents/notes.txt
/cloud/reports/q4.pdf →   GCS          →  gs://my-company-data/reports/q4.pdf
/papers/ml/paper.pdf  →   S3           →  s3://research-papers/ml/paper.pdf
```

**Key Points for Embedded Mode**:
- ✅ **Unified Interface**: Local and remote storage accessed the same way
- ✅ **No API Server**: Direct Python function calls
- ✅ **SQLite Metadata**: Maps virtual paths → backend locations
- ✅ **Multiple Backends**: GCS, S3, LocalFS - all in one library
- ✅ **Transparent Access**: User doesn't care where files are stored
- ✅ **File Locks**: Local flock (no Redis needed)
- ✅ **LLM Cache**: Provider's cloud cache (Claude/OpenAI)
- ✅ **In-Process**: Everything in one Python process

---

#### Monolithic Server Architecture

```mermaid
sequenceDiagram
    participant Client as Python SDK / Agent
    participant API as REST API Server
    participant Engine as Core Engine
    participant PathRouter as Path Router
    participant BackendMgr as Backend Manager
    participant Backend as Storage Backend<br/>(S3/LocalFS)
    participant Parser as Parser Registry
    participant LLM as LLM Manager
    participant Redis as Redis<br/>(locks/cache)
    participant DB as SQLite/Postgres

    Client->>API: POST /v1/files/docs/report.pdf/semantic/llm<br/>{query: "summarize"}
    API->>API: authenticate(bearer_token)
    API->>API: check_rate_limit()

    API->>Engine: semantic_read("/docs/report.pdf", "summarize")

    Engine->>PathRouter: route("/docs/report.pdf")
    PathRouter->>DB: get_metadata(path)
    DB-->>PathRouter: {backend: "s3", bucket: "..."}

    PathRouter->>BackendMgr: get_backend("s3")
    BackendMgr-->>PathRouter: S3 backend

    Note over PathRouter: Acquire distributed lock
    PathRouter->>Redis: acquire_lock("/docs/report.pdf")
    Redis-->>PathRouter: lock_acquired

    PathRouter->>Backend: read_file("report.pdf")
    Backend-->>PathRouter: file_bytes

    PathRouter->>Redis: release_lock()

    Engine->>Parser: parse_pdf(file_bytes)
    Parser-->>Engine: structured_content

    Engine->>LLM: query(content, "summarize")
    LLM-->>Engine: answer

    Engine->>DB: record_usage(tokens, cost)

    Engine-->>API: {content: "...", cost_analysis: {...}}
    API-->>Client: JSON response
```

**Key Points for Monolithic**:
- ✅ **REST API**: Multi-user access
- ✅ **SQLite or Postgres**: Configurable
- ✅ **Redis**: Distributed locks + rate limiting
- ✅ **Multiple Backends**: S3, GDrive, etc.
- ✅ **Single Process**: Still one Docker container

---

#### Distributed Mode Architecture

```mermaid
graph TB
    subgraph "Load Balancer Layer"
        LB[Load Balancer<br/>HAProxy/Nginx]
    end

    subgraph "API Server Pool (Stateless)"
        API1[API Server 1]
        API2[API Server 2]
        API3[API Server 3]
    end

    subgraph "Core Engine (in each API server)"
        Engine[Core Engine]
        PathRouter[Path Router]
        BackendMgr[Backend Manager]
        Parser[Parser Registry]
        LLM[LLM Manager]
    end

    subgraph "Shared State Layer"
        PG[(Postgres Cluster<br/>Metadata + RLS)]
        Redis[(Redis Cluster<br/>Locks + Cache)]
        Qdrant[(Qdrant Cluster<br/>Vector Search)]
    end

    subgraph "Storage Backends"
        S3[S3 / GCS]
        GDrive[Google Drive]
        Local[Local FS<br/>(NFS/EBS)]
    end

    LB --> API1
    LB --> API2
    LB --> API3

    API1 --> Engine
    API2 --> Engine
    API3 --> Engine

    Engine --> PathRouter
    PathRouter --> BackendMgr
    Engine --> Parser
    Engine --> LLM

    PathRouter --> PG
    PathRouter --> Redis
    LLM --> Redis
    Engine --> Qdrant

    BackendMgr --> S3
    BackendMgr --> GDrive
    BackendMgr --> Local

    style Engine fill:#90EE90
    style Parser fill:#DDA0DD
    style LLM fill:#FFA07A
    style PG fill:#87CEEB
    style Redis fill:#FFB6C1
```

**Key Points for Distributed**:
- ✅ **Horizontal Scaling**: Multiple API servers
- ✅ **Postgres Cluster**: High availability
- ✅ **Redis Cluster**: Distributed locks with fencing
- ✅ **Qdrant Cluster**: Scalable vector search
- ✅ **Load Balanced**: Geographic distribution

---

### Deployment Model Clarification

Nexus has **three distinct deployment modes**. Here's what gets deployed in each:

#### Mode 1: Embedded (Library)

**What Gets Deployed**: Nothing! It's a library.

```
Developer's Machine
├─ Python environment
│  └─ nexus package (pip install nexus-sdk)
├─ Local data directory (./nexus-data/)
│  ├─ nexus.db (SQLite - metadata)
│  ├─ qdrant/ (embedded vector store)
│  └─ backends/ (file storage)
└─ Application code
   └─ import nexus
```

**Process Model**: Everything runs in the Python process.

**Use When**:
- Single developer/user
- CLI tools and scripts
- Jupyter notebooks
- Local development
- Offline work (except LLM API calls)

---

#### Mode 2: Monolithic Server

**What Gets Deployed**: Single process, usually in Docker.

```
Docker Container (nexus:latest)
├─ Nexus API Server
│  ├─ REST API (/v1/*)
│  ├─ MCP Server (optional)
│  ├─ Core Engine
│  └─ All components (parser, LLM, etc.)
├─ SQLite or Postgres (your choice)
│  └─ nexus.db (if SQLite)
├─ Redis (optional, recommended)
│  └─ Locks + rate limiting
└─ Qdrant (embedded or separate)
   └─ Vector search
```

**External Dependencies** (optional):
- **Postgres**: Use if > 20 users or > 100GB data
- **Redis**: Use for distributed locks across restarts
- **Qdrant Cloud**: Use instead of embedded for better performance

**Process Model**: Single process handles all requests.

**Use When**:
- Small teams (1-20 users)
- Startups / MVPs
- Staging environments
- < 10K requests/day
- < 100GB data

**Example Docker Compose**:
```yaml
version: '3.8'
services:
  nexus:
    image: nexus/nexus:latest
    command: server --mode=monolithic
    ports:
      - "8080:8080"
    environment:
      NEXUS_MODE: monolithic
      NEXUS_DB_TYPE: postgres  # or sqlite
      POSTGRES_URL: postgres://postgres:5432/nexus
      REDIS_URL: redis://redis:6379
    volumes:
      - ./nexus-data:/data

  postgres:  # Optional: use SQLite instead
    image: postgres:15
    environment:
      POSTGRES_DB: nexus

  redis:  # Optional but recommended
    image: redis:7
```

---

#### Mode 3: Distributed (Production)

**What Gets Deployed**: Full Kubernetes infrastructure.

```
Kubernetes Cluster
├─ Load Balancer (HAProxy/Nginx)
│  └─ SSL termination, routing
│
├─ Nexus API Pods (3-10+ replicas)
│  ├─ REST API server
│  ├─ MCP server (optional)
│  ├─ Core Engine
│  └─ Stateless (no local state)
│
├─ Postgres Cluster (Primary + Replicas)
│  ├─ Primary (writes)
│  ├─ Read replicas (2+)
│  └─ Metadata + RLS multi-tenancy
│
├─ Redis Cluster (3+ nodes)
│  ├─ Distributed locks with fencing
│  ├─ Rate limiting counters
│  └─ LLM response cache
│
├─ Qdrant Cluster (3+ nodes)
│  ├─ Vector search
│  └─ Agent memory embeddings
│
└─ Observability Stack
   ├─ Prometheus (metrics)
   ├─ Grafana (dashboards)
   ├─ Loki (logs)
   └─ Jaeger (tracing)
```

**Process Model**: Horizontally scaled, stateless API servers.

**Use When**:
- Enterprises (100+ users)
- High availability requirements
- > 10K requests/day
- Multi-region deployment
- Production SaaS

**Example Helm Install**:
```bash
helm install nexus nexus/nexus-distributed \
  --set replicas=5 \
  --set postgres.enabled=true \
  --set postgres.replication=true \
  --set redis.cluster.enabled=true \
  --set qdrant.cluster.nodes=3 \
  --set observability.enabled=true
```

---

### What About Clients?

**Clients are NOT deployed** - they connect to Nexus:

| Client Type | Connects To | Installation |
|-------------|-------------|--------------|
| **Python Embedded** | N/A (library mode) | `pip install nexus-sdk` |
| **Python SDK** | REST API | `pip install nexus-sdk` |
| **MCP Clients** | MCP Server | Built into Claude Desktop/Cursor |
| **AI Agents** | REST API via SDK | Agent framework + SDK |
| **Web Apps** | REST API | HTTP library |

**Client Installation Example**:
```python
# Embedded mode (no server needed)
import nexus
nx = nexus.Embedded("./data")

# OR connect to server
from nexus import NexusClient
async with NexusClient(
    base_url="https://nexus.company.com",
    api_key="nexus_xxx"
) as client:
    await client.read_file("/docs/handbook.pdf")
```

### Component Interaction Flow

```mermaid
sequenceDiagram
    participant Agent
    participant API
    participant PathRouter
    participant MemoryMgr as Memory Manager
    participant VectorDB
    participant JobQueue
    participant Backend

    Agent->>API: Write file + record memory
    API->>PathRouter: Route path
    PathRouter->>Backend: Write content
    Backend-->>API: FileInfo

    API->>MemoryMgr: Record memory lineage
    MemoryMgr->>VectorDB: Store metadata

    API->>JobQueue: Submit indexing job
    JobQueue->>JobQueue: Extract → Chunk → Embed
    JobQueue->>VectorDB: Index vectors

    Note over Agent,Backend: Later: Memory retrieval

    Agent->>API: Semantic search
    API->>MemoryMgr: Search memories
    MemoryMgr->>VectorDB: Vector search
    VectorDB-->>MemoryMgr: Top-K results
    MemoryMgr->>MemoryMgr: Track access
    MemoryMgr-->>Agent: Ranked memories
```

### Data Flow Across Layers

This table summarizes the input/output contracts between core components. Essential for onboarding new engineers.

| **Component** | **Input** | **Processing** | **Output** | **Side Effects** |
|---------------|-----------|----------------|------------|------------------|
| **PathRouter** | `(path: str, tenant_id: UUID)` | 1. Parse mount point<br/>2. Resolve backend<br/>3. Validate permissions | `(backend: Backend, relative_path: str)` | - Logs access to `file_access_log`<br/>- Updates path stats |
| **MetadataStore** | `(operation: str, path: str, tenant_id: UUID, metadata: Dict)` | 1. Acquire fencing lock<br/>2. Check RLS policy<br/>3. Update `file_paths` table<br/>4. Record lineage if applicable | `FileInfo` object | - Increments `lock_version` fencing token<br/>- Inserts `audit_log` entry<br/>- Updates `file_lineage` if operation has inputs |
| **Backend (S3)** | `(operation: str, path: str, content: bytes, etag: str)` | 1. Upload to S3 bucket<br/>2. Handle multipart if >5GB<br/>3. Retrieve `storage_etag` from S3 | `(storage_etag: str, size: int, modified_time: datetime)` | - S3 object created/updated<br/>- CloudWatch metrics emitted |
| **Backend (GDrive)** | `(operation: str, path: str, content: bytes, oauth_token: str)` | 1. Refresh OAuth token if expired<br/>2. Call Drive API<br/>3. Handle rate limits (exponential backoff) | `(storage_etag: str, size: int, modified_time: datetime)` | - Drive file created/updated<br/>- Token refresh logged<br/>- Rate limit counters updated |
| **Lock Manager** | `(path: str, tenant_id: UUID, timeout: float)` | 1. Acquire Redis lock<br/>2. Acquire Postgres advisory lock<br/>3. Increment fencing token in DB | `FencedLock` (with token) | - Redis key set with TTL<br/>- Postgres advisory lock held<br/>- `file_paths.lock_version += 1` |
| **Job System** | `(job_type: str, config: Dict, priority: int)` | 1. Insert into `jobs` table<br/>2. Worker picks up job<br/>3. Execute with checkpointing<br/>4. Update progress every 2s | Job status updates | - Job state transitions logged<br/>- Checkpoints written to `jobs.checkpoint`<br/>- Progress tracked in `jobs.progress` |
| **Vector Store (Qdrant)** | `(tenant_id: UUID, operation: str, path_id: UUID, embedding: List[float])` | 1. Select collection `{tenant_id}__file_chunks`<br/>2. Upsert/delete vector point<br/>3. Return search results if query | `List[SearchResult]` (with score, payload) | - Qdrant collection updated<br/>- No tenant_id in payload (already namespaced) |

**Key Insights for Engineers:**
- **PathRouter** is stateless and lightweight (no DB writes)
- **MetadataStore** is the consistency bottleneck (uses locks and fencing tokens)
- **Backend** operations are pass-through (no content caching for external backends)
- **Lock Manager** guarantees per-path linearizability via fencing tokens
- **Job System** is eventually consistent (workers poll every 1s)
- **Vector Store** is tenant-isolated at collection level (not query filter level)

---

## Core Design Principles

### 1. Consistency Model

**Nexus Guarantees**: Per-path linearizability with eventual cross-node consistency

```yaml
consistency:
  # Strong guarantees (same path)
  single_path:
    model: linearizable
    guarantee: "Operations on same path are totally ordered"

  # Eventual consistency (cross-path)
  cross_path:
    model: eventual
    max_staleness: 5s

  # Read-after-write
  read_after_write: true
  same_session: true

  # Conflict resolution
  conflict_detection: etag
  conflict_resolution: last_writer_wins
```

### 2. Storage Tiers

**Three-tier storage model** (from Alluxio):

```
┌─────────────────────────────────────────┐
│ Hot Tier (Local SSD)                    │
│ • <1 hour old files                     │
│ • Optional: RAM tier (faster)           │
│ • <10ms latency                         │
└─────────────────────────────────────────┘
           ↓ Age > 1h
┌─────────────────────────────────────────┐
│ Warm Tier (Local Disk)                  │
│ • 1-24 hours old                        │
│ • Selective checkpointing               │
│ • <50ms latency                         │
└─────────────────────────────────────────┘
           ↓ Age > 24h
┌─────────────────────────────────────────┐
│ Cold Tier (S3/Durable)                  │
│ • >24 hours old                         │
│ • Always checkpointed                   │
│ • 100-500ms latency                     │
└─────────────────────────────────────────┘
```

### 3. Namespace Structure

```
/                           # Root
├── workspace/              # Current agent's workspace (auto-mapped)
│   ├── tmp/                # Temporary files (auto-cleanup)
│   ├── logs/               # Agent logs
│   ├── memories/           # Agent memories
│   └── output/             # Generated outputs
│
├── workspaces/             # All agent workspaces
│   ├── agent-a/            # Agent A's private space
│   ├── agent-b/            # Agent B's private space
│   └── shared/             # Shared workspace
│
├── gdrive/                 # Google Drive mount
├── s3-data/                # S3 bucket mount
├── sharepoint/             # SharePoint mount
└── ...                     # Other backend mounts
```

### 4. Data Storage Options

**Critical Design Decision**: Does Nexus store file content?

**Answer**: IT DEPENDS on the mount type:

**Option A: Pass-Through (No Content Storage)**
- Use Case: External backends (GDrive, SharePoint, Dropbox)
- Data Flow:
  - User writes: `/gdrive/Documents/report.pdf`
  - Nexus stores ONLY metadata in PostgreSQL
  - File content goes DIRECTLY to Google Drive API
  - Nexus acts as router/proxy
- Benefits: No storage costs, data stays in user account, compliance-friendly
- Drawbacks: Every read hits external API (slower), rate limits

**Option B: Workspace (Content Storage)**
- Use Case: Agent scratch space, temporary files
- Data Flow:
  - User writes: `/workspace/temp.txt`
  - File content stored in Nexus infrastructure (Hot/Warm/Cold tiers)
  - Metadata in PostgreSQL
- Benefits: Ultra-fast access, no API calls, CAS deduplication works
- Drawbacks: Storage costs, hot tier data lost on restart (recoverable via lineage)

**Option C: Hybrid Caching (Future)**
- Use Case: Frequently accessed external files
- Cache external files in hot tier with TTL
- Status: NOT in v5.0 (future consideration)

**Recommendation**:
- External backends (GDrive, S3, SharePoint): Pass-Through
- Workspace: Content Storage (hot/warm/cold tiers)
- Clear data ownership, no compliance issues

---

## Core Components

### 1. Path Router with Backend Recognition

**Purpose**: Intelligent routing with auto-detection of backend types.

```mermaid
graph LR
    A[Virtual Path] --> B{Backend<br/>Recognition}
    B -->|Confidence > 0.9| C[Google Drive]
    B -->|Confidence > 0.9| D[SharePoint]
    B -->|Confidence > 0.9| E[S3]
    B -->|Fallback| F[Manual Mount]

    C --> G[Route Request]
    D --> G
    E --> G
    F --> G
```

**Interface**:

```python
class BackendRecognition(BaseModel):
    """Backend recognition result"""
    confidence: float  # 0.0-1.0
    backend_type: str  # "gdrive", "s3", "sharepoint"
    mount_point: str
    readonly: bool
    suggested_config: Dict

class StorageBackend(ABC):
    @abstractmethod
    async def recognize_path(
        self,
        path: str,
        metadata: Optional[Dict] = None
    ) -> BackendRecognition:
        """
        Determine if this backend can handle this path.

        Recognition strategies:
        1. URL pattern matching (fast, reliable)
        2. OAuth token inspection (medium confidence)
        3. Path structure analysis (low confidence)
        """
        pass

# Example: Google Drive recognition
class GoogleDriveBackend(StorageBackend):
    async def recognize_path(self, path: str, metadata: Optional[Dict]):
        # URL patterns
        gdrive_patterns = [
            r"drive\.google\.com",
            r"docs\.google\.com",
            r"^/gdrive/",
        ]

        for pattern in gdrive_patterns:
            if re.search(pattern, path):
                return BackendRecognition(
                    confidence=0.95,
                    backend_type="gdrive",
                    mount_point="/gdrive",
                    readonly=False,
                    suggested_config={"scope": "drive.file"}
                )

        # OAuth token inspection
        if metadata and metadata.get("oauth_provider") == "google":
            return BackendRecognition(
                confidence=0.7,
                backend_type="gdrive",
                mount_point="/gdrive",
                readonly=False
            )

        return BackendRecognition(confidence=0.0, ...)
```

### 2. Metadata Store with Enhanced Tracking

**Purpose**: Comprehensive file metadata with processing state.

**Schema Additions**:

```python
# file_paths extensions
ALTER TABLE file_paths ADD COLUMN:
    container_tags TEXT[]              # Flexible grouping
    document_type VARCHAR(50)          # Better filtering
    processing_status VARCHAR(50)      # Pipeline tracking
    processing_job_id UUID             # Link to jobs
    chunk_count INTEGER                # Semantic chunks
    indexed_at TIMESTAMP               # Last index time
```

**Interface**:

```python
class MetadataStore:
    """Enhanced metadata with processing state"""

    async def get_with_processing_info(
        self,
        path: str
    ) -> FileMetadataWithProcessing:
        """Get file metadata including processing status"""
        return FileMetadataWithProcessing(
            # Standard metadata
            path=path,
            size=...,
            etag=...,

            # Processing info
            processing_status="indexed",  # queued, extracting, chunking, etc
            chunk_count=15,
            indexed_at=datetime(...),

            # Classification
            document_type="pdf",
            container_tags=["project_budget", "agent_alpha"]
        )
```

---

## Complete Database Schema

### Production-Ready PostgreSQL Schema

```sql
-- ============================================
-- Core Tables (from NEXUS_FINAL_DESIGN)
-- ============================================

-- Tenants (multi-tenancy)
CREATE TABLE tenants (
    tenant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    quota_bytes BIGINT NOT NULL DEFAULT 107374182400,  -- 100GB
    settings JSONB
);

CREATE INDEX idx_tenants_status ON tenants(status);

-- Agents (within tenants)
CREATE TABLE agents (
    agent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMP,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    quota_bytes BIGINT NOT NULL DEFAULT 10737418240,  -- 10GB
    metadata JSONB,

    CONSTRAINT agents_tenant_name_unique UNIQUE (tenant_id, name)
);

CREATE INDEX idx_agents_tenant ON agents(tenant_id);
CREATE INDEX idx_agents_status ON agents(status);

-- API Keys (authentication)
CREATE TABLE api_keys (
    key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE CASCADE,

    key_hash VARCHAR(255) NOT NULL UNIQUE,
    key_prefix VARCHAR(20) NOT NULL,

    name VARCHAR(255),
    scopes JSONB,  -- ["read", "write", "delete", "mount", "runtime_credentials"]

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP,
    last_used_at TIMESTAMP,
    status VARCHAR(50) NOT NULL DEFAULT 'active'
);

CREATE INDEX idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX idx_api_keys_prefix ON api_keys(key_prefix);

-- File Metadata (Enhanced with Supermemory additions + Dual-ETag)
CREATE TABLE file_paths (
    path_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE SET NULL,

    path TEXT NOT NULL,
    parent_path TEXT,
    name TEXT NOT NULL,

    content_id VARCHAR(64),  -- Blake2 hash (CAS)

    is_directory BOOLEAN NOT NULL DEFAULT FALSE,
    size BIGINT NOT NULL DEFAULT 0,
    mime_type VARCHAR(255),

    -- DUAL-ETAG MODEL
    nexus_etag VARCHAR(64) NOT NULL,      -- Nexus-stable SHA-256 for conflict detection
    storage_etag VARCHAR(255),            -- Backend's native ETag (when available)
    etag_computed_at TIMESTAMP,           -- When nexus_etag was last computed

    version INTEGER NOT NULL DEFAULT 1,

    backend_type VARCHAR(50) NOT NULL,
    backend_path TEXT NOT NULL,
    instance_id VARCHAR(100),  -- Owning node

    -- FENCING TOKENS
    lock_version BIGINT NOT NULL DEFAULT 0,  -- Monotonic fencing token
    locked_by VARCHAR(100),                  -- Which node holds lock
    locked_at TIMESTAMP,                     -- When lock acquired

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    accessed_at TIMESTAMP,
    deleted_at TIMESTAMP,

    -- Supermemory additions
    container_tags TEXT[] DEFAULT '{}',
    document_type VARCHAR(50),
    processing_status VARCHAR(50),
    processing_job_id UUID,
    chunk_count INTEGER,
    indexed_at TIMESTAMP,

    metadata JSONB
);

CREATE INDEX idx_file_paths_tenant ON file_paths(tenant_id);
CREATE INDEX idx_file_paths_agent ON file_paths(agent_id);
CREATE INDEX idx_file_paths_parent ON file_paths(parent_path);
CREATE INDEX idx_file_paths_content ON file_paths(content_id);
CREATE INDEX idx_file_paths_backend ON file_paths(backend_type, backend_path);
CREATE INDEX idx_file_paths_container_tags ON file_paths USING gin(container_tags);
CREATE INDEX idx_file_paths_document_type ON file_paths(document_type);
CREATE INDEX idx_file_paths_nexus_etag ON file_paths(nexus_etag);
CREATE INDEX idx_file_paths_locked ON file_paths(locked_by) WHERE locked_by IS NOT NULL;

-- Partial unique index for soft delete (NULL != NULL in Postgres)
CREATE UNIQUE INDEX idx_file_paths_tenant_path_live
    ON file_paths(tenant_id, path)
    WHERE deleted_at IS NULL;

-- Content Objects (CAS) - Enhanced with hash metadata
CREATE TABLE content_objects (
    content_id VARCHAR(64) PRIMARY KEY,  -- Blake2 hash (our computation)
    size BIGINT NOT NULL,
    mime_type VARCHAR(255),

    -- Hash metadata (separate from backend ETag)
    hash_algo VARCHAR(20) NOT NULL DEFAULT 'blake2b',  -- blake2b, sha256, etc.
    hash_value BYTEA NOT NULL,                          -- Raw hash bytes

    -- Backend info
    backend_type VARCHAR(50) NOT NULL,
    backend_path TEXT NOT NULL,
    backend_etag VARCHAR(255),                          -- Backend's ETag (may differ from content_id)

    ref_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    protected_until TIMESTAMP,
    metadata JSONB
);

CREATE INDEX idx_content_objects_hash ON content_objects(hash_algo, hash_value);

CREATE INDEX idx_content_objects_ref_count ON content_objects(ref_count);
CREATE INDEX idx_content_objects_last_accessed ON content_objects(last_accessed_at);

-- File Versions (history)
CREATE TABLE file_versions (
    version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_id UUID NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content_id VARCHAR(64),
    size BIGINT NOT NULL,
    changed_by_agent_id UUID REFERENCES agents(agent_id),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    change_description TEXT
);

CREATE INDEX idx_file_versions_path ON file_versions(path_id, version DESC);

-- Mounts
CREATE TABLE mounts (
    mount_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE CASCADE,

    mount_point TEXT NOT NULL,
    backend_type VARCHAR(50) NOT NULL,
    backend_config_encrypted TEXT NOT NULL,

    readonly BOOLEAN NOT NULL DEFAULT FALSE,
    shared BOOLEAN NOT NULL DEFAULT FALSE,
    auto_mounted BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMP,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    error_message TEXT,

    CONSTRAINT mounts_tenant_mount_point_unique UNIQUE (tenant_id, mount_point)
);

CREATE INDEX idx_mounts_tenant ON mounts(tenant_id);
CREATE INDEX idx_mounts_status ON mounts(status);

-- ACL Entries
-- ACL Entries (FIXED SCHEMA - subject_type/subject_id/path_id model)
CREATE TABLE acl_entries (
    acl_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

    -- Subject: who is granted permission
    subject_type VARCHAR(20) NOT NULL DEFAULT 'agent',  -- 'agent' | 'group'
    subject_id UUID NOT NULL,                            -- agent_id or group_id

    -- Path: what is being accessed
    path_id UUID REFERENCES file_paths(path_id) ON DELETE CASCADE,  -- For exact ACLs (fast join)
    path TEXT,                                                       -- For prefix ACLs (e.g., "/shared/%")
    is_prefix BOOLEAN NOT NULL DEFAULT FALSE,

    -- Permissions
    can_read BOOLEAN NOT NULL DEFAULT FALSE,
    can_write BOOLEAN NOT NULL DEFAULT FALSE,
    can_delete BOOLEAN NOT NULL DEFAULT FALSE,
    can_list BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES agents(agent_id) ON DELETE SET NULL,
    expires_at TIMESTAMP,

    -- Consistency: EITHER path_id (exact) OR path (prefix), not both
    CONSTRAINT acl_path_xor CHECK (
        (path_id IS NOT NULL AND path IS NULL) OR
        (path_id IS NULL AND path IS NOT NULL)
    )
);

-- Prevent duplicate grants
CREATE UNIQUE INDEX idx_acl_unique
ON acl_entries(tenant_id, subject_type, subject_id, COALESCE(path_id::text, path), is_prefix)
WHERE expires_at IS NULL;

-- Fast exact ACL lookups (path_id-based)
CREATE INDEX idx_acl_exact
ON acl_entries(tenant_id, path_id, subject_type, subject_id)
WHERE path_id IS NOT NULL;

-- Prefix ACL lookups (slower, for /shared/% style rules)
CREATE INDEX idx_acl_prefix
ON acl_entries(tenant_id, path text_pattern_ops)
WHERE is_prefix = true;

-- Upload Sessions (resumable uploads)
CREATE TABLE upload_sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,

    path TEXT NOT NULL,
    backend_upload_id TEXT,
    backend_type VARCHAR(50) NOT NULL,

    total_size BIGINT,
    uploaded_bytes BIGINT NOT NULL DEFAULT 0,
    part_size BIGINT NOT NULL,

    parts_uploaded INTEGER NOT NULL DEFAULT 0,
    parts_info JSONB,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,

    status VARCHAR(50) NOT NULL DEFAULT 'active'
);

CREATE INDEX idx_upload_sessions_status ON upload_sessions(status);
CREATE INDEX idx_upload_sessions_expires ON upload_sessions(expires_at) WHERE status = 'active';

-- File Lineage (from Alluxio)
CREATE TABLE file_lineage (
    lineage_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_id UUID NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,

    derived_from UUID[] NOT NULL DEFAULT '{}',
    operation VARCHAR(255) NOT NULL,
    operation_params JSONB,
    relationship_type VARCHAR(50) DEFAULT 'derives',

    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    task_id VARCHAR(255),

    can_recompute BOOLEAN NOT NULL DEFAULT TRUE,
    recomputation_cost_seconds FLOAT,

    is_checkpointed BOOLEAN NOT NULL DEFAULT FALSE,
    checkpoint_priority INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_file_lineage_path ON file_lineage(path_id);
CREATE INDEX idx_file_lineage_checkpoint ON file_lineage(is_checkpointed);

-- Audit Log
CREATE TABLE audit_log (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE SET NULL,
    api_key_id UUID REFERENCES api_keys(key_id) ON DELETE SET NULL,

    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50) NOT NULL,
    resource_path TEXT,

    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),

    ip_address INET,
    user_agent TEXT,
    request_id VARCHAR(100),

    before_state JSONB,
    after_state JSONB,
    metadata JSONB,

    redacted BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_audit_log_tenant ON audit_log(tenant_id, timestamp DESC);
CREATE INDEX idx_audit_log_action ON audit_log(action);

-- Cluster Nodes
CREATE TABLE cluster_nodes (
    node_id VARCHAR(100) PRIMARY KEY,
    instance_id VARCHAR(100) NOT NULL UNIQUE,
    public_url TEXT NOT NULL,
    internal_url TEXT NOT NULL,
    region VARCHAR(100),

    status VARCHAR(50) NOT NULL DEFAULT 'active',
    last_heartbeat_at TIMESTAMP NOT NULL DEFAULT NOW(),

    max_connections INTEGER NOT NULL DEFAULT 1000,
    current_connections INTEGER NOT NULL DEFAULT 0,

    version VARCHAR(50),
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    metadata JSONB
);

CREATE INDEX idx_cluster_nodes_status ON cluster_nodes(status);
CREATE INDEX idx_cluster_nodes_heartbeat ON cluster_nodes(last_heartbeat_at);

-- ============================================
-- Triggers & Functions
-- ============================================

-- Auto-update modified_at
CREATE OR REPLACE FUNCTION update_modified_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.modified_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER file_paths_modified_at
    BEFORE UPDATE ON file_paths
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_at();

CREATE TRIGGER tenants_modified_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_at();

CREATE TRIGGER mounts_modified_at
    BEFORE UPDATE ON mounts
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_at();

-- Auto-update content ref counts
CREATE OR REPLACE FUNCTION update_content_ref_count()
RETURNS TRIGGER AS $$
BEGIN
    -- INSERT: Increment ref count
    IF TG_OP = 'INSERT' AND NEW.content_id IS NOT NULL THEN
        UPDATE content_objects
        SET ref_count = ref_count + 1, last_accessed_at = NOW()
        WHERE content_id = NEW.content_id;
        RETURN NEW;
    END IF;

    -- UPDATE: Change content_id (decrement old, increment new)
    IF TG_OP = 'UPDATE' THEN
        IF OLD.content_id IS NOT NULL AND (NEW.content_id IS NULL OR NEW.content_id != OLD.content_id) THEN
            UPDATE content_objects
            SET ref_count = GREATEST(0, ref_count - 1)
            WHERE content_id = OLD.content_id;
        END IF;

        IF NEW.content_id IS NOT NULL AND (OLD.content_id IS NULL OR NEW.content_id != OLD.content_id) THEN
            UPDATE content_objects
            SET ref_count = ref_count + 1, last_accessed_at = NOW()
            WHERE content_id = NEW.content_id;
        END IF;
        RETURN NEW;
    END IF;

    -- DELETE: Decrement ref count
    IF TG_OP = 'DELETE' AND OLD.content_id IS NOT NULL THEN
        UPDATE content_objects
        SET ref_count = GREATEST(0, ref_count - 1)
        WHERE content_id = OLD.content_id;
        RETURN OLD;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER file_paths_content_ref_count
    AFTER INSERT OR UPDATE OR DELETE ON file_paths
    FOR EACH ROW
    EXECUTE FUNCTION update_content_ref_count();

-- ============================================
-- Agent Groups (for ACL subject_type='group')
-- ============================================

CREATE TABLE agent_groups (
    group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    group_name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES agents(agent_id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX idx_agent_groups_unique ON agent_groups(tenant_id, group_name);

CREATE TABLE agent_group_members (
    group_id UUID NOT NULL REFERENCES agent_groups(group_id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    added_at TIMESTAMP NOT NULL DEFAULT NOW(),
    added_by UUID REFERENCES agents(agent_id) ON DELETE SET NULL,
    PRIMARY KEY (group_id, agent_id)
);

CREATE INDEX idx_agent_group_members_agent ON agent_group_members(agent_id);

-- ============================================
-- Row-Level Security (RLS)
-- ============================================

-- Enable RLS with FORCE (prevent bypass even for table owner)
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents FORCE ROW LEVEL SECURITY;

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;

ALTER TABLE file_paths ENABLE ROW LEVEL SECURITY;
ALTER TABLE file_paths FORCE ROW LEVEL SECURITY;

ALTER TABLE mounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mounts FORCE ROW LEVEL SECURITY;

ALTER TABLE acl_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE acl_entries FORCE ROW LEVEL SECURITY;

ALTER TABLE upload_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE upload_sessions FORCE ROW LEVEL SECURITY;

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

ALTER TABLE agent_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_groups FORCE ROW LEVEL SECURITY;

-- Revoke public access (prevent bypass)
REVOKE ALL ON tenants FROM PUBLIC;
REVOKE ALL ON agents FROM PUBLIC;
REVOKE ALL ON api_keys FROM PUBLIC;
REVOKE ALL ON file_paths FROM PUBLIC;
REVOKE ALL ON mounts FROM PUBLIC;
REVOKE ALL ON acl_entries FROM PUBLIC;
REVOKE ALL ON upload_sessions FROM PUBLIC;
REVOKE ALL ON audit_log FROM PUBLIC;
REVOKE ALL ON agent_groups FROM PUBLIC;

-- RLS Policies (enforce tenant isolation at database level)
CREATE POLICY tenant_isolation ON file_paths
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON agents
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON api_keys
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON mounts
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON acl_entries
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON upload_sessions
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE POLICY tenant_isolation ON audit_log
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- ============================================
-- Encryption Keys (KMS Envelope Encryption)
-- ============================================

CREATE TABLE tenant_encryption_keys (
    key_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    encrypted_dek BYTEA NOT NULL,  -- KMS-encrypted data encryption key
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    rotated_at TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX idx_tenant_encryption_keys_active ON tenant_encryption_keys(tenant_id, active);
```

---


## Authentication & Authorization

### Zero Trust Design Summary

Nexus implements **defense-in-depth** multi-tenant isolation using three complementary layers. This ensures that even if one layer is compromised, the other layers prevent cross-tenant data leaks.

```mermaid
graph TB
    Request[Incoming Request] --> Layer1[Layer 1: API Key Auth]
    Layer1 --> Layer2[Layer 2: Row-Level Security RLS]
    Layer2 --> Layer3[Layer 3: ACL Permission Check]
    Layer3 --> Data[(Data Access)]

    Layer1 -->|Fail| Reject1[401 Unauthorized]
    Layer2 -->|Fail| Reject2[403 Forbidden - RLS]
    Layer3 -->|Fail| Reject3[403 Forbidden - ACL]

    style Layer1 fill:#FFE5E5
    style Layer2 fill:#E5F5FF
    style Layer3 fill:#E5FFE5
    style Data fill:#90EE90
```

#### Layer 1: API Key Authentication (Application Layer)

**Purpose**: Identify the tenant and agent making the request.

**Mechanism**:
- API key format: `nexus_{20_char_prefix}_{32_char_secret}`
- Key prefix indexed in database for fast lookup
- Full key verified with bcrypt (constant-time comparison prevents timing attacks)
- Sets `request.state.auth` with `tenant_id`, `agent_id`, `scopes`

**Security Properties**:
- ✅ Prevents unauthenticated access
- ✅ Rate limiting per key prefix (prevents brute force)
- ✅ Audit logging (who accessed what)
- ❌ Does NOT prevent tenant misconfiguration bugs in application code

**Example Failure**: Application bug accidentally uses wrong `tenant_id` in query.

---

#### Layer 2: Row-Level Security (Database Layer)

**Purpose**: Database-level enforcement that **only** allows access to rows belonging to the current tenant.

**Mechanism**:
```sql
-- Set tenant context at request start
SET LOCAL app.current_tenant_id = '<tenant_uuid>';

-- RLS policy auto-filters ALL queries
CREATE POLICY tenant_isolation ON file_paths
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
```

**Security Properties**:
- ✅ Prevents cross-tenant access even if application code has bugs
- ✅ Works with connection pooling (SET LOCAL is transaction-scoped)
- ✅ Applies to ALL queries (SELECT, UPDATE, DELETE) automatically
- ⚠️ Requires RLS cleanup middleware to prevent context leakage

**Example Protection**: Application bug tries `SELECT * FROM file_paths WHERE path = '/data'` without tenant filter → RLS automatically adds `WHERE tenant_id = current_tenant_id`.

**Critical Middleware**:
```python
class RLSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        finally:
            # ALWAYS reset tenant context
            await db.execute("RESET app.current_tenant_id")
```

---

#### Layer 3: ACL Permission Checks (Authorization Layer)

**Purpose**: Fine-grained access control within a tenant (agent-to-agent sharing, read-only shares).

**Mechanism**:
```python
async def check_permission(auth: AuthContext, path: str, permission: str) -> bool:
    # 1. Check if agent owns the file
    if await is_owner(auth.agent_id, path):
        return True

    # 2. Check ACL entries
    acl = await db.execute("""
        SELECT permissions FROM acl_entries
        WHERE tenant_id = ? AND path = ?
          AND grantee_type = 'agent' AND grantee_id = ?
    """, auth.tenant_id, path, auth.agent_id)

    return permission in acl.permissions
```

**ACL Precedence** (most specific wins):
1. **Explicit DENY** on exact path → ❌ Denied
2. **Explicit ALLOW** on exact path → ✅ Allowed
3. **Inherited DENY** from parent → ❌ Denied
4. **Inherited ALLOW** from parent → ✅ Allowed
5. **No ACL** → Owner allowed, others denied

**Security Properties**:
- ✅ Enables agent-to-agent collaboration within tenant
- ✅ Supports read-only shares and temporary access
- ✅ Path-based permissions (e.g., `/shared/` vs `/private/`)
- ❌ Does NOT protect against tenant_id spoofing (handled by RLS)

---

#### Why Three Layers?

| Attack Vector | Layer 1 | Layer 2 | Layer 3 | Result |
|---------------|---------|---------|---------|--------|
| **Unauthenticated request** | ❌ Blocked | - | - | 401 Unauthorized |
| **Valid API key, wrong tenant_id in query** | ✅ Pass | ❌ Blocked | - | 0 rows returned (silent) |
| **Valid agent, accessing teammate's file** | ✅ Pass | ✅ Pass | ❌ Blocked | 403 Forbidden |
| **Malicious SQL injection** | ✅ Pass | ❌ Blocked | - | RLS prevents cross-tenant data |
| **Connection pool tenant leak** | ✅ Pass | ❌ Blocked | - | RLS middleware cleanup |

**Key Insight**: Even if application code is buggy or malicious, RLS provides a **hard database-level boundary** that cannot be bypassed without database credentials.

---

#### Additional Isolation: Qdrant Tenant Namespacing

**Vector Store Isolation**: Qdrant collections are tenant-namespaced, NOT query-filtered.

```python
# WRONG (security risk - relies on query filter)
collection = "file_chunks"
results = qdrant.search(
    collection=collection,
    query_filter={"tenant_id": tenant_id}  # Can be bypassed
)

# CORRECT (physical isolation)
collection = f"{tenant_id}__file_chunks"
results = qdrant.search(
    collection=collection,
    # No tenant_id filter needed - wrong collection = 0 results
)
```

**Why Physical Isolation?**
- Prevents query filter bypass bugs
- Makes cross-tenant access impossible even with Qdrant API access
- Simplifies queries (no need to add tenant_id to every filter)

---

### Complete Auth Implementation

```python
from fastapi import Request, Header, Depends, HTTPException
from typing import Optional
import bcrypt
from datetime import datetime, timedelta
import uuid

class AuthContext:
    """Request authentication context"""
    tenant_id: UUID
    agent_id: UUID
    api_key_id: UUID
    scopes: List[str]
    ip_address: str
    user_agent: str
    request_id: str

async def get_auth_context(
    request: Request,
    authorization: str = Header(...),
    x_request_id: Optional[str] = Header(None)
) -> AuthContext:
    """
    Extract and validate auth context.

    Process:
    1. Extract API key from Bearer token
    2. Rate limit by key prefix (prevent oracle attacks)
    3. Query database for key
    4. Verify bcrypt hash (constant-time comparison)
    5. Check expiration
    6. Return AuthContext
    """

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    api_key = authorization[7:]
    key_prefix = api_key[:20]

    # Rate limit by prefix to prevent oracle attacks
    await rate_limiter.check(f"auth:{key_prefix}", limit=10, window=60)

    # Query database
    result = await db.execute(
        select(api_keys, agents, tenants)
        .join(agents, api_keys.agent_id == agents.agent_id)
        .join(tenants, agents.tenant_id == tenants.tenant_id)
        .where(api_keys.key_prefix == key_prefix)
        .where(api_keys.status == "active")
    )

    row = result.first()
    if not row:
        raise HTTPException(401, "Invalid API key")

    # Verify hash (constant-time to prevent timing attacks)
    if not bcrypt.checkpw(api_key.encode(), row.key_hash.encode()):
        raise HTTPException(401, "Invalid API key")

    # Check expiration
    if row.expires_at and datetime.utcnow() > row.expires_at:
        raise HTTPException(401, "API key expired")

    auth = AuthContext(
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        api_key_id=row.key_id,
        scopes=row.scopes or ["read", "write", "delete"],
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent", ""),
        request_id=x_request_id or str(uuid.uuid4())
    )

    # Attach to request.state for rate limiting
    request.state.auth = auth

    # Set tenant context for RLS (Row-Level Security)
    await db.execute(
        "SET LOCAL app.current_tenant_id = ?",
        str(auth.tenant_id)
    )

    return auth
```

### RFC 7807 Error Handling

```python
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class ProblemDetail(BaseModel):
    """RFC 7807 Problem Details for HTTP APIs"""
    type: str
    title: str
    status: int
    detail: Optional[str] = None
    instance: Optional[str] = None

def problem_response(
    status: int,
    title: str,
    detail: str = "",
    type_: str = "about:blank",
    instance: Optional[str] = None
) -> JSONResponse:
    """Create RFC 7807 problem response"""

    body = {
        "type": type_,
        "title": title,
        "status": status,
        "detail": detail
    }

    if instance:
        body["instance"] = instance

    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json"
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return problem_response(
        status=exc.status_code,
        title=str(exc.detail or "Error"),
        detail=str(exc.detail or ""),
        type_=f"https://nexus.example.com/errors/{exc.status_code}",
        instance=getattr(request.state, "request_id", None)
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception", exc_info=exc)

    return problem_response(
        status=500,
        title="Internal Server Error",
        detail="An unexpected error occurred",
        type_="https://nexus.example.com/errors/internal",
        instance=getattr(request.state, "request_id", None)
    )
```

### Rate Limiting

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

class RateLimitManager:
    """Redis-based rate limiting"""

    def __init__(self, redis_url: str):
        self.redis = aioredis.from_url(redis_url)
        self.limiter = Limiter(
            key_func=self.get_rate_limit_key,
            storage_uri=redis_url
        )

    def get_rate_limit_key(self, request: Request) -> str:
        """Rate limit by API key or IP"""
        auth = getattr(request.state, "auth", None)
        return f"api_key:{auth.api_key_id}" if auth else f"ip:{request.client.host}"

    def rate_limit_read(self):
        """Read operations: 1000/minute"""
        return self.limiter.limit("1000/minute")

    def rate_limit_write(self):
        """Write operations: 100/minute"""
        return self.limiter.limit("100/minute")

    def rate_limit_search(self):
        """Search operations: 60/minute"""
        return self.limiter.limit("60/minute")
```

### Rate Limiting Configuration Presets

Different deployment scenarios require different rate limiting strategies.

#### Development Mode (No Rate Limiting)

```yaml
# config/dev.yaml
rate_limiting:
  enabled: false

# Or set very high limits
rate_limiting:
  enabled: true
  presets:
    read: "1000000/minute"
    write: "1000000/minute"
    search: "1000000/minute"
```

#### Single-Tenant / Self-Hosted (High Limits)

**Use Case**: Company deploys Nexus for internal use, trusts all agents.

```yaml
# config/single-tenant.yaml
rate_limiting:
  enabled: true
  presets:
    read: "10000/minute"      # High throughput for trusted environment
    write: "5000/minute"
    search: "1000/minute"

  # Still protect against runaway agents
  per_agent_limits:
    enabled: true
    max_requests_per_second: 100  # Circuit breaker for infinite loops
```

#### Multi-Tenant SaaS (Strict Limits)

**Use Case**: Public SaaS offering with free/paid tiers.

```yaml
# config/multi-tenant.yaml
rate_limiting:
  enabled: true

  # Default limits (free tier)
  presets:
    read: "100/minute"
    write: "50/minute"
    search: "20/minute"

  # Paid tier overrides (stored in tenant_metadata)
  tier_overrides:
    pro:
      read: "1000/minute"
      write: "500/minute"
      search: "200/minute"
    enterprise:
      read: "10000/minute"
      write: "5000/minute"
      search: "1000/minute"

  # Security limits (always enforced)
  auth_attempts: "10/minute"     # Prevent brute force
  failed_requests: "50/minute"   # Prevent resource exhaustion
```

**Implementation**:
```python
async def get_rate_limits(auth: AuthContext) -> Dict[str, str]:
    """Get rate limits based on tenant tier"""
    tier = await db.execute(
        "SELECT metadata->>'tier' as tier FROM tenants WHERE tenant_id = ?",
        auth.tenant_id
    )

    tier_name = tier.get("tier", "free")
    return config.rate_limiting.tier_overrides.get(tier_name, config.rate_limiting.presets)
```

#### External API Protection Mode

**Use Case**: Heavy use of pass-through backends (GDrive, SharePoint) with strict API quotas.

```yaml
# config/api-protection.yaml
rate_limiting:
  enabled: true

  # Aggressive limits to stay under external API quotas
  backend_limits:
    gdrive:
      queries_per_100_seconds: 900  # Google's limit: 1000/100s
      buffer: 0.9                    # Use 90% to leave safety margin

    sharepoint:
      queries_per_minute: 600        # Microsoft's limit varies by tenant

    s3:
      enabled: false                 # No rate limit on S3

  # Distribute quota across all agents
  fair_share: true
  backpressure: true  # Return 429 when external API is rate limited
```

---

### JWT / OIDC Extension Path (Federated Auth)

**Current State**: Nexus uses API keys for agent authentication.

**Future Extension**: Support JWT tokens from external identity providers (Auth0, Okta, Google Workspace) for MCP-integrated systems.

#### Why JWT/OIDC?

Many MCP-integrated environments (Claude Desktop in enterprise, Cursor with SSO) already use OIDC for authentication. Supporting JWT avoids requiring users to manage separate API keys.

#### Proposed Design

```mermaid
sequenceDiagram
    participant User
    participant Claude
    participant OIDC as Auth0/Okta
    participant Nexus

    User->>Claude: Open workspace
    Claude->>OIDC: Authenticate (OIDC flow)
    OIDC-->>Claude: JWT token (id_token)

    Claude->>Nexus: GET /files/workspace/data<br/>Authorization: Bearer <JWT>
    Nexus->>Nexus: Validate JWT signature (JWKS)
    Nexus->>Nexus: Extract tenant_id from claims
    Nexus->>Nexus: Map JWT sub to agent_id (or create)
    Nexus-->>Claude: File data
```

#### Implementation Interface

```python
from jose import jwt, JWTError
from typing import Optional

class JWTAuthProvider:
    """
    JWT authentication provider (OIDC extension).

    Validates JWT tokens from external IDPs and maps them to Nexus identities.
    """

    def __init__(self, jwks_url: str, issuer: str, audience: str):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self._jwks_cache = None
        self._jwks_cache_time = 0

    async def validate_jwt(self, token: str) -> Dict:
        """
        Validate JWT token and return claims.

        Verifies:
        - Signature using JWKS (fetched and cached)
        - Issuer matches expected IDP
        - Audience matches Nexus audience
        - Token not expired
        """
        pass

    async def map_to_agent(self, claims: Dict) -> AuthContext:
        """
        Map JWT claims to Nexus agent identity.

        Mapping strategy:
        1. Extract tenant_id from custom claim (e.g., claims['https://nexus.ai/tenant_id'])
        2. Extract user email from 'email' claim
        3. Lookup or create agent:
           - SELECT agent_id FROM agents WHERE tenant_id = ? AND email = ?
           - If not exists, auto-provision agent (if allowed by tenant)
        4. Return AuthContext with tenant_id, agent_id, scopes
        """
        pass

# Updated auth dependency
async def get_auth_context(
    request: Request,
    authorization: str = Header(...)
) -> AuthContext:
    """
    Extract auth context from API key OR JWT.

    Supports two auth methods:
    - Bearer nexus_xxx: API key (existing)
    - Bearer eyJhbGc...: JWT token (new)
    """

    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]

    # Detect token type
    if token.startswith("nexus_"):
        # API key authentication (existing)
        return await authenticate_api_key(token, request)

    elif token.startswith("eyJ"):  # JWT starts with base64-encoded '{"alg"'
        # JWT authentication (new)
        if not config.auth.jwt_enabled:
            raise HTTPException(401, "JWT authentication not enabled")

        jwt_provider = JWTAuthProvider(
            jwks_url=config.auth.jwks_url,
            issuer=config.auth.issuer,
            audience=config.auth.audience
        )

        claims = await jwt_provider.validate_jwt(token)
        return await jwt_provider.map_to_agent(claims)

    else:
        raise HTTPException(401, "Invalid token format")
```

#### Configuration

```yaml
# config/production.yaml
auth:
  # API keys (always enabled)
  api_keys:
    enabled: true

  # JWT/OIDC (optional)
  jwt:
    enabled: true
    jwks_url: "https://your-tenant.auth0.com/.well-known/jwks.json"
    issuer: "https://your-tenant.auth0.com/"
    audience: "https://nexus.your-company.com"

    # Auto-provisioning
    auto_provision_agents: true  # Create agents on first JWT login
    require_tenant_claim: true   # JWT must include 'https://nexus.ai/tenant_id'

    # Scope mapping (optional)
    scope_claim: "permissions"   # Extract scopes from JWT claim
    default_scopes: ["read", "write"]
```

#### Security Considerations

1. **Tenant Claim Required**: JWT MUST include a custom claim with `tenant_id` to prevent cross-tenant access.
   - Auth0: Use "Rules" to add custom claim
   - Okta: Use "Claims" in authorization server

2. **JWKS Caching**: Cache JWKS for 1 hour, refresh on signature validation failure.

3. **Agent Auto-Provisioning**: Only enable if IDP is trusted and tenant isolation is enforced via custom claim.

4. **Scope Mapping**: Map IDP roles to Nexus scopes:
   ```python
   # Example: Auth0 role → Nexus scope
   role_scope_map = {
       "nexus:admin": ["read", "write", "delete", "admin"],
       "nexus:writer": ["read", "write"],
       "nexus:reader": ["read"]
   }
   ```

---

## Complete API Specification

### Helper Functions

```python
import re
import base64
import hashlib
from typing import Optional, Tuple, List

# Regex for parsing Range header
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

def encode_cursor(key: Optional[str]) -> Optional[str]:
    """Encode pagination cursor"""
    if not key:
        return None
    return base64.urlsafe_b64encode(key.encode()).decode()

def decode_cursor(token: Optional[str]) -> Optional[str]:
    """Decode pagination cursor"""
    if not token:
        return None
    try:
        return base64.urlsafe_b64decode(token.encode()).decode()
    except Exception:
        raise HTTPException(400, "Invalid page token")

async def compute_nexus_etag(content: bytes) -> str:
    """Compute Nexus-stable SHA-256 ETag"""
    return hashlib.sha256(content).hexdigest()

async def compute_nexus_etag_streaming(stream: AsyncIterator[bytes]) -> str:
    """Compute ETag from stream (for large files)"""
    hasher = hashlib.sha256()
    async for chunk in stream:
        hasher.update(chunk)
    return hasher.hexdigest()

# ETag computation policy
EAGER_COMPUTE_THRESHOLD = 10 * 1024 * 1024  # 10MB
```

### File Operations with ETag, Range, Pagination

```python
# ============= GET with ETag/Range/304/206 =============

@app.get("/api/v1/files/{path:path}")
@rate_limit_manager.rate_limit_read()
async def get_file(
    path: str,
    request: Request,
    range: Optional[str] = Header(None),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    auth: AuthContext = Depends(get_auth_context),
    user_credential: Optional[Dict] = Depends(get_user_credential)
) -> StreamingResponse | Response:
    """
    Download file with ETag caching and Range requests.

    Supports:
    - If-None-Match: Returns 304 if ETag matches
    - Range: Returns 206 with Content-Range
    """

    virtual_path = f"/{path}"

    # Runtime credential path (ephemeral backend)
    if user_credential:
        if "runtime_credentials" not in auth.scopes:
            raise HTTPException(403, "API key does not allow runtime credentials")

        backend_type = virtual_path.split("/")[1]
        backend = await create_ephemeral_backend(
            backend_type=backend_type,
            credential=user_credential,
            mount_point=f"/{backend_type}"
        )
        backend_path = "/".join(virtual_path.split("/")[2:])

        # Enforce ACL even with runtime creds
        if not await acl_manager.can_access_with_runtime_creds(
            virtual_path, auth, user_credential
        ):
            raise HTTPException(403, "Path access denied")

    # Standard path
    else:
        backend, backend_path = path_router.route(virtual_path)

        if not await acl_manager.can_read(virtual_path, auth):
            raise HTTPException(403, "Permission denied")

    # Get metadata (includes dual ETags)
    meta = await metadata_store.get(virtual_path)
    if not meta:
        # First access - get backend info
        file_info = await backend.stat(backend_path)
        storage_etag = file_info.storage_etag

        # Compute Nexus ETag based on size
        if file_info.size < EAGER_COMPUTE_THRESHOLD:
            # Small file: compute now
            content = await backend.read_file(backend_path)
            nexus_etag = await compute_nexus_etag(content)
        else:
            # Large file: defer to background job
            nexus_etag = "pending"
            await job_queue.enqueue(
                "compute_etag_job",
                path=virtual_path,
                backend_path=backend_path
            )

        meta = await metadata_store.create(
            path=virtual_path,
            size=file_info.size,
            mime_type=file_info.mime_type,
            nexus_etag=nexus_etag,
            storage_etag=storage_etag,
            backend_type=backend.type,
            backend_path=backend_path
        )

    # Check If-None-Match (304 Not Modified) - accept both ETags
    if if_none_match:
        client_etags = [e.strip(' "W/') for e in if_none_match.split(",")]
        # Check both Nexus ETag and Storage ETag
        if meta.nexus_etag in client_etags or (meta.storage_etag and meta.storage_etag in client_etags):
            return Response(status_code=304, headers={
                "ETag": f'W/"{meta.nexus_etag}"',
                "X-Storage-ETag": meta.storage_etag or ""
            })

    # Parse Range header
    size = meta.size
    start, end = 0, size - 1
    status_code = 200
    headers = {
        "ETag": f'W/"{meta.nexus_etag}"',          # Weak ETag (Nexus-stable)
        "X-Storage-ETag": meta.storage_etag or "",  # Backend's native ETag
        "Accept-Ranges": "bytes",
        "Content-Type": meta.mime_type or "application/octet-stream"
    }

    if range:
        m = RANGE_RE.match(range)
        if not m:
            raise HTTPException(416, "Invalid Range header")

        s, e = m.groups()
        if s:
            start = int(s)
        if e:
            end = int(e)

        # Validate range
        if start > end or end >= size:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"}
            )

        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
    else:
        headers["Content-Length"] = str(size)

    # Stream file
    async def file_generator():
        async for chunk in backend.open_stream_range(backend_path, start, end):
            yield chunk

    # Audit log
    await audit_logger.log(
        action="read",
        resource_type="file",
        resource_path=virtual_path,
        auth=auth,
        metadata={"range": bool(range), "runtime_credential": bool(user_credential)},
        redacted=bool(user_credential)
    )

    return StreamingResponse(
        file_generator(),
        status_code=status_code,
        media_type=headers["Content-Type"],
        headers=headers
    )


# ============= PUT with If-Match, Content-MD5 =============

@app.put("/api/v1/files/{path:path}")
@rate_limit_manager.rate_limit_write()
async def upload_file(
    path: str,
    request: Request,
    content_md5: Optional[str] = Header(None, alias="Content-MD5"),
    if_match: Optional[str] = Header(None, alias="If-Match"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    user_credential: Optional[Dict] = Depends(get_user_credential)
) -> Dict:
    """
    Upload file with conflict detection and checksum validation.

    Supports:
    - Content-MD5: Base64-encoded MD5 for integrity
    - If-Match: ETag for conflict detection (409 on mismatch)
    """

    virtual_path = f"/{path}"

    # Check idempotency key
    if idempotency_key:
        if await idem_cache.seen(idempotency_key, auth):
            cached_response = await idem_cache.get_response(idempotency_key, auth)
            if cached_response:
                logger.info(f"Idempotency key hit: {idempotency_key}")
                return cached_response

    content = await request.body()

    # Validate Content-MD5 (base64, not hex)
    if content_md5:
        actual_md5 = base64.b64encode(hashlib.md5(content).digest()).decode()
        if actual_md5 != content_md5:
            raise HTTPException(400, "Content-MD5 checksum mismatch")

    # Runtime credential path
    if user_credential:
        if "runtime_credentials" not in auth.scopes:
            raise HTTPException(403, "API key does not allow runtime credentials")

        backend_type = virtual_path.split("/")[1]
        backend = await create_ephemeral_backend(
            backend_type=backend_type,
            credential=user_credential,
            mount_point=f"/{backend_type}"
        )
        backend_path = "/".join(virtual_path.split("/")[2:])

        if not await acl_manager.can_write_with_runtime_creds(
            virtual_path, auth, user_credential
        ):
            raise HTTPException(403, "Write access denied")

    # Standard path
    else:
        backend, backend_path = path_router.route(virtual_path)

        if not await acl_manager.can_write(virtual_path, auth):
            raise HTTPException(403, "Permission denied")

    # Acquire distributed lock with fencing token
    async with lock_manager.lock(virtual_path) as fenced_lock:
        # Check If-Match (conflict detection) - accept both ETags
        if if_match:
            existing_meta = await metadata_store.get(virtual_path)
            if existing_meta:
                client_etags = [e.strip(' "W/') for e in if_match.split(",")]
                # Check both Nexus ETag and Storage ETag
                if (existing_meta.nexus_etag not in client_etags and
                    (not existing_meta.storage_etag or existing_meta.storage_etag not in client_etags)):
                    raise HTTPException(409, "ETag conflict: file was modified")

        # Write to backend
        file_info = await backend.write_file(backend_path, content)

        # Compute Nexus ETag (we have content)
        nexus_etag = await compute_nexus_etag(content)

        # Check fencing token before committing
        await fenced_lock.check_fence()

        # Update metadata
        meta = await metadata_store.upsert(
            path=virtual_path,
            size=len(content),
            mime_type=file_info.mime_type,
            nexus_etag=nexus_etag,
            storage_etag=file_info.storage_etag,  # Backend's ETag
            version_increment=True,
            backend_type=backend.type,
            backend_path=backend_path
        )

        # Audit log
        await audit_logger.log(
            action="write",
            resource_type="file",
            resource_path=virtual_path,
            auth=auth,
            metadata={"size": len(content), "runtime_credential": bool(user_credential)},
            redacted=bool(user_credential)
        )

        response = {
            "path": virtual_path,
            "size": meta.size,
            "nexus_etag": meta.nexus_etag,
            "storage_etag": meta.storage_etag,
            "version": meta.version,
            "modified_at": meta.modified_at.isoformat()
        }

        # Cache response for idempotency
        if idempotency_key:
            await idem_cache.store(idempotency_key, auth, response)

        return response


# ============= LIST with Pagination =============

@app.get("/api/v1/files/{path:path}/list")
async def list_directory(
    path: str,
    request: Request,
    recursive: bool = False,
    page_token: Optional[str] = None,
    page_size: int = 200,
    auth: AuthContext = Depends(get_auth_context),
    user_credential: Optional[Dict] = Depends(get_user_credential)
) -> Dict:
    """
    List directory with cursor-based pagination.

    Args:
        page_token: Opaque cursor for next page
        page_size: Items per page (1-1000)
    """

    if page_size < 1 or page_size > 1000:
        raise HTTPException(400, "page_size must be 1-1000")

    virtual_path = f"/{path}"
    cursor = decode_cursor(page_token)

    # Runtime credential path
    if user_credential:
        if "runtime_credentials" not in auth.scopes:
            raise HTTPException(403, "API key does not allow runtime credentials")

        backend_type = virtual_path.split("/")[1]
        backend = await create_ephemeral_backend(
            backend_type=backend_type,
            credential=user_credential,
            mount_point=f"/{backend_type}"
        )
        backend_path = "/".join(virtual_path.split("/")[2:])

        if not await acl_manager.can_list_with_runtime_creds(
            virtual_path, auth, user_credential
        ):
            raise HTTPException(403, "List access denied")

    # Standard path
    else:
        backend, backend_path = path_router.route(virtual_path)

        if not await acl_manager.can_list(virtual_path, auth):
            raise HTTPException(403, "Permission denied")

    # List with pagination
    files, next_key = await backend.list_directory_page(
        backend_path,
        cursor=cursor,
        page_size=page_size,
        recursive=recursive
    )

    return {
        "path": virtual_path,
        "files": [
            {
                "path": f.path,
                "size": f.size,
                "modified_at": f.modified_at.isoformat() if f.modified_at else None,
                "is_directory": f.is_directory,
                "nexus_etag": f.nexus_etag,
                "storage_etag": f.storage_etag
            }
            for f in files
        ],
        "next_page_token": encode_cursor(next_key),
        "page_size": len(files)
    }


# ============= Resumable Upload =============

@app.post("/api/v1/uploads/init")
async def init_upload(
    path: str,
    total_size: int,
    part_size: int = 67108864,  # 64MB
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Initiate resumable multipart upload"""

    session_id = await upload_manager.initiate_upload(
        path=path,
        total_size=total_size,
        part_size=part_size,
        auth=auth
    )

    return {
        "session_id": session_id,
        "path": path,
        "part_size": part_size,
        "upload_endpoint": f"/api/v1/uploads/{session_id}/parts"
    }


@app.put("/api/v1/uploads/{session_id}/parts/{part_number}")
async def upload_part(
    session_id: str,
    part_number: int,
    request: Request,
    content_md5: Optional[str] = Header(None, alias="Content-MD5"),
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Upload a part"""

    content = await request.body()

    return await upload_manager.upload_part(
        session_id=session_id,
        part_number=part_number,
        content=content,
        content_md5=content_md5,
        auth=auth
    )


@app.post("/api/v1/uploads/{session_id}/complete")
async def complete_upload(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Complete multipart upload"""

    return await upload_manager.complete_upload(
        session_id=session_id,
        auth=auth
    )


# ============= Rich Format Parsing =============

@app.get("/api/v1/files/{path:path}")
async def get_file_with_parsing(
    path: str,
    format: Optional[str] = Query(None, regex="^(raw|parsed)$"),
    request: Request,
    range: Optional[str] = Header(None),
    if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    auth: AuthContext = Depends(get_auth_context),
    user_credential: Optional[Dict] = Depends(get_user_credential)
) -> StreamingResponse | Dict:
    """
    Download file with optional rich format parsing.

    Query Parameters:
        format: Optional output format
            - "raw" (default): Binary content (existing behavior)
            - "parsed": Structured JSON for rich formats (PDF, Excel, CSV, etc.)

    Parsing Support:
        - PDF: Extract text per page, metadata
        - Excel (XLSX/XLS): Extract all sheets, cells, rows
        - CSV: Parse as table with headers
        - JSON/YAML: Return structured data
        - Markdown: Parse structure, headers, code blocks
        - Binary: Return metadata only

    Returns:
        - format=raw: StreamingResponse with binary content
        - format=parsed: JSON with ParsedContent structure

    Example:
        GET /api/v1/files/documents/report.pdf?format=parsed

        Response:
        {
            "type": "document",
            "metadata": {
                "filename": "/documents/report.pdf",
                "parser": "pdf",
                "pages": 42,
                "size": 1048576
            },
            "data": {
                "pages": [
                    {"page_number": 1, "text": "..."},
                    {"page_number": 2, "text": "..."}
                ],
                "page_count": 42
            },
            "text": "Full extracted text from all pages...",
            "mime_type": "application/pdf",
            "size": 1048576
        }
    """

    # If format not specified or "raw", use existing streaming behavior
    if not format or format == "raw":
        return await get_file(path, request, range, if_none_match, auth, user_credential)

    # format=parsed: Parse content into structured JSON
    virtual_path = f"/{path}"

    # Runtime credential path (ephemeral backend)
    if user_credential:
        if "runtime_credentials" not in auth.scopes:
            raise HTTPException(403, "API key does not allow runtime credentials")

        backend_type = virtual_path.split("/")[1]
        backend = await create_ephemeral_backend(
            backend_type=backend_type,
            credential=user_credential,
            mount_point=f"/{backend_type}"
        )
        backend_path = "/".join(virtual_path.split("/")[2:])

        if not await acl_manager.can_access_with_runtime_creds(
            virtual_path, auth, user_credential
        ):
            raise HTTPException(403, "Path access denied")

    # Standard path
    else:
        backend, backend_path = path_router.route(virtual_path)

        if not await acl_manager.can_read(virtual_path, auth):
            raise HTTPException(403, "Permission denied")

    # Get metadata
    meta = await metadata_store.get(virtual_path)
    if not meta:
        file_info = await backend.stat(backend_path)
        meta = await metadata_store.create(
            path=virtual_path,
            size=file_info.size,
            mime_type=file_info.mime_type,
            backend_type=backend.type,
            backend_path=backend_path
        )

    # Read file content
    content = await backend.read_file(backend_path)

    # Parse using parser registry
    parsed = await parser_registry.parse(
        content=content,
        mime_type=meta.mime_type,
        filename=virtual_path,
        size=meta.size
    )

    # Audit log
    await audit_logger.log(
        action="read_parsed",
        resource_type="file",
        resource_path=virtual_path,
        auth=auth,
        metadata={
            "parser": parsed.metadata.get("parser"),
            "content_type": parsed.type,
            "runtime_credential": bool(user_credential)
        },
        redacted=bool(user_credential)
    )

    return parsed.dict()
```

---

### Parser System Architecture

The parser system provides extensible support for reading rich file formats. All parsers implement a common interface and are automatically selected based on MIME type or file extension.

#### Parser Interface

```go
package parsers

import (
    "context"
    "io"
)

// ParsedContent represents structured output from a parser
type ParsedContent struct {
    // Type describes the content structure
    Type string `json:"type"` // "document", "spreadsheet", "table", "structured_data", "binary"

    // Metadata about the parsed content
    Metadata map[string]interface{} `json:"metadata"`

    // Data contains the parsed content in a structured format
    // For tables: [][]string or []map[string]interface{}
    // For documents: map with sections/paragraphs
    // For structured data: native structure
    Data interface{} `json:"data"`

    // Raw text extraction (if applicable)
    Text string `json:"text,omitempty"`

    // MimeType of the original file
    MimeType string `json:"mime_type"`

    // Size of original content in bytes
    Size int64 `json:"size"`
}

// Parser interface for extensible format parsing
type Parser interface {
    // Name returns the parser name
    Name() string

    // SupportedMimeTypes returns MIME types this parser can handle
    SupportedMimeTypes() []string

    // SupportedExtensions returns file extensions this parser can handle
    SupportedExtensions() []string

    // Parse reads content and returns structured data
    Parse(ctx context.Context, reader io.Reader, filename string) (*ParsedContent, error)

    // CanParse checks if this parser can handle the given file
    CanParse(mimeType string, extension string) bool
}

// Registry manages available parsers
type Registry struct {
    parsers map[string]Parser
    mimeTypeMap map[string][]string
    extensionMap map[string][]string
}
```

#### Supported Format Types

| Content Type | Formats | Parser Output |
|-------------|---------|---------------|
| **Document** | PDF, Word, Markdown | Pages/sections with extracted text |
| **Spreadsheet** | Excel (XLSX/XLS) | All sheets with cells, formulas |
| **Table** | CSV, TSV | Headers and rows |
| **Structured Data** | JSON, YAML, XML, TOML | Native structure |
| **Binary** | Other formats | Metadata only |

#### Parser Examples

**PDF Parser:**
```json
{
  "type": "document",
  "metadata": {
    "filename": "/documents/report.pdf",
    "parser": "pdf",
    "pages": 42,
    "size": 1048576
  },
  "data": {
    "pages": [
      {"page_number": 1, "text": "Page 1 content..."},
      {"page_number": 2, "text": "Page 2 content..."}
    ],
    "page_count": 42
  },
  "text": "Full extracted text from all pages...",
  "mime_type": "application/pdf",
  "size": 1048576
}
```

**Excel Parser:**
```json
{
  "type": "spreadsheet",
  "metadata": {
    "filename": "/data/sales.xlsx",
    "parser": "excel",
    "sheet_count": 3,
    "sheet_names": ["Q1", "Q2", "Q3"]
  },
  "data": {
    "sheets": [
      {
        "name": "Q1",
        "rows": [
          ["Month", "Revenue", "Profit"],
          ["Jan", "100000", "25000"],
          ["Feb", "120000", "30000"]
        ],
        "row_count": 3,
        "col_count": 3
      }
    ],
    "sheet_count": 3
  },
  "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "size": 45678
}
```

**CSV Parser:**
```json
{
  "type": "table",
  "metadata": {
    "filename": "/data/users.csv",
    "parser": "csv",
    "row_count": 1000
  },
  "data": {
    "headers": ["id", "name", "email"],
    "rows": [
      ["1", "Alice", "alice@example.com"],
      ["2", "Bob", "bob@example.com"]
    ],
    "row_count": 1000
  },
  "mime_type": "text/csv",
  "size": 50000
}
```

**JSON Parser:**
```json
{
  "type": "structured_data",
  "metadata": {
    "filename": "/config/app.json",
    "parser": "json"
  },
  "data": {
    "database": {
      "host": "localhost",
      "port": 5432
    },
    "features": {
      "parsing": true
    }
  },
  "mime_type": "application/json",
  "size": 256
}
```

#### Implementing Custom Parsers

To add support for a new format, implement the `Parser` interface and register it:

```go
package parsers

import (
    "context"
    "io"
)

type MyCustomParser struct{}

func (p *MyCustomParser) Name() string {
    return "my-custom"
}

func (p *MyCustomParser) SupportedMimeTypes() []string {
    return []string{"application/x-custom"}
}

func (p *MyCustomParser) SupportedExtensions() []string {
    return []string{"custom", "cst"}
}

func (p *MyCustomParser) CanParse(mimeType string, extension string) bool {
    // Check if this parser can handle the format
    for _, mt := range p.SupportedMimeTypes() {
        if mt == mimeType {
            return true
        }
    }
    return false
}

func (p *MyCustomParser) Parse(ctx context.Context, reader io.Reader, filename string) (*ParsedContent, error) {
    // Read and parse your custom format
    content, err := io.ReadAll(reader)
    if err != nil {
        return nil, err
    }

    // Parse the content
    parsed := parseMyFormat(content)

    return &ParsedContent{
        Type: "structured_data",
        Metadata: map[string]interface{}{
            "filename": filename,
            "parser": "my-custom",
        },
        Data: parsed,
        MimeType: "application/x-custom",
        Size: int64(len(content)),
    }, nil
}

// Register the parser at initialization
func init() {
    RegisterParser(&MyCustomParser{})
}
```

#### AI Agent Integration

The parsed output is designed for AI agent consumption:

1. **Semantic Search**: The `text` field can be indexed for vector search
2. **Structured Analysis**: The `data` field provides structured access
3. **Metadata**: The `metadata` field provides context

**MCP Server Integration:**
```typescript
// Example MCP tool for parsed file reading
{
  name: "read_parsed_file",
  description: "Read and parse a file into structured format",
  inputSchema: {
    type: "object",
    properties: {
      path: { type: "string" },
    },
  },
  handler: async (args) => {
    const response = await fetch(
      `${NEXUS_URL}/api/v1/files/${args.path}?format=parsed`,
      {
        headers: { Authorization: `Bearer ${token}` },
      }
    );
    return response.json();
  },
}
```

#### Performance Considerations

- **Streaming**: Large files use streaming parsers when possible
- **Temp Files**: Some libraries require temp files (cleaned up properly)
- **Context Cancellation**: All parsers respect context cancellation
- **Size Limits**: Reasonable size limits per format type
- **Timeouts**: Parsing operations have configurable timeouts

#### Future Parser Extensions

Planned parser implementations:
- [ ] Image OCR (extract text from images)
- [ ] Video metadata (duration, resolution, codec)
- [ ] Audio transcription (speech-to-text)
- [ ] Archive files (zip, tar) - list contents
- [ ] Code parsers (AST extraction)
- [ ] Database dumps (SQL, MongoDB)

---

## Backend Implementations

### Storage Backend Interface

```python
from abc import ABC, abstractmethod
from typing import BinaryIO, List, Optional, AsyncIterator, Tuple
from pydantic import BaseModel
from datetime import datetime

class FileInfo(BaseModel):
    """File metadata with dual-ETag support"""
    path: str
    size: int
    modified_at: Optional[datetime] = None
    mime_type: Optional[str] = None
    is_directory: bool

    # DUAL-ETAG MODEL
    nexus_etag: str                       # Nexus-stable SHA-256
    storage_etag: Optional[str] = None    # Backend's native ETag

class StorageBackend(ABC):
    """Common interface all backends must implement"""

    @abstractmethod
    async def initialize(self):
        """Initialize backend (called at startup)"""
        pass

    @abstractmethod
    async def shutdown(self):
        """Shutdown backend (called at app shutdown)"""
        pass

    @abstractmethod
    async def stat(self, path: str) -> FileInfo:
        """Get file/directory metadata"""
        pass

    @abstractmethod
    async def read_file(self, path: str) -> bytes:
        """Read file content (for small files <10MB)"""
        pass

    @abstractmethod
    async def write_file(
        self,
        path: str,
        content: bytes,
        size: Optional[int] = None
    ) -> FileInfo:
        """Write file content"""
        pass

    @abstractmethod
    async def list_directory_page(
        self,
        path: str,
        cursor: Optional[str],
        page_size: int,
        recursive: bool = False
    ) -> Tuple[List[FileInfo], Optional[str]]:
        """
        List directory with pagination.

        Returns:
            (files, next_cursor)
        """
        pass

    @abstractmethod
    async def delete(self, path: str, recursive: bool = False) -> None:
        """Delete file or directory"""
        pass

    @abstractmethod
    async def open_stream(self, path: str) -> AsyncIterator[bytes]:
        """Open file as async stream (for zero-copy)"""
        pass

    @abstractmethod
    async def open_stream_range(
        self,
        path: str,
        start: int,
        end: int
    ) -> AsyncIterator[bytes]:
        """Open file as async stream with byte range"""
        pass

    # Optional: Multipart upload support
    async def initiate_multipart_upload(self, path: str, session_id: str) -> str:
        """Start multipart upload (optional)"""
        raise NotImplementedError()

    async def upload_part(
        self,
        path: str,
        upload_id: str,
        part_number: int,
        data: bytes
    ) -> Dict:
        """Upload one part (optional)"""
        raise NotImplementedError()

    async def complete_multipart_upload(
        self,
        path: str,
        upload_id: str,
        parts: List[Dict]
    ) -> FileInfo:
        """Complete multipart upload (optional)"""
        raise NotImplementedError()

    # Optional: Backend recognition
    @classmethod
    def create_recognizer(cls) -> "StorageBackend":
        """Create instance for recognition (no credentials needed)"""
        return cls()

    async def recognize_path(
        self,
        path: str,
        metadata: Optional[Dict] = None
    ) -> "BackendRecognition":
        """
        Determine if this backend can handle this path.

        Returns BackendRecognition with confidence score.
        """
        return BackendRecognition(confidence=0.0, backend_type=self.type)
```

### S3 Backend (Complete Implementation)

```python
import aioboto3
from typing import AsyncIterator, Optional, Tuple, List
from botocore.exceptions import ClientError

class S3Backend(StorageBackend):
    """AWS S3 backend with proper async lifecycle"""

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = ""
    ):
        self.type = "s3"
        self.bucket = bucket
        self.region = region
        self.prefix = prefix.rstrip("/")
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key

        self._session: Optional[aioboto3.Session] = None
        self._client = None
        self._upload_sessions: Dict[str, str] = {}

    # Proper async lifecycle
    async def initialize(self):
        """Initialize S3 client"""
        self._session = aioboto3.Session()
        self._client = await self._session.client(
            's3',
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key
        ).__aenter__()

    async def shutdown(self):
        """Shutdown S3 client"""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    def _full_key(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.prefix}/{path}" if self.prefix else path

    async def stat(self, path: str) -> FileInfo:
        """Get file metadata from S3"""
        key = self._full_key(path)

        # Check if it's a directory
        if path.endswith("/"):
            response = await self._client.list_objects_v2(
                Bucket=self.bucket,
                Prefix=key,
                MaxKeys=1
            )

            if response.get('KeyCount', 0) > 0:
                return FileInfo(
                    path=path,
                    size=0,
                    modified_at=None,
                    mime_type=None,
                    is_directory=True,
                    nexus_etag="",
                    storage_etag=None
                )
            else:
                raise FileNotFoundError(f"Directory not found: {path}")

        # Regular file
        try:
            response = await self._client.head_object(
                Bucket=self.bucket,
                Key=key
            )

            return FileInfo(
                path=path,
                size=response['ContentLength'],
                modified_at=response['LastModified'],
                mime_type=response.get('ContentType'),
                is_directory=False,
                nexus_etag="pending",  # Computed on first access
                storage_etag=response['ETag'].strip('"')  # S3's native ETag
            )
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                raise FileNotFoundError(f"File not found: {path}")
            raise

    async def read_file(self, path: str) -> bytes:
        """Read entire file (for small files)"""
        key = self._full_key(path)

        response = await self._client.get_object(
            Bucket=self.bucket,
            Key=key
        )

        async with response['Body'] as stream:
            return await stream.read()

    async def open_stream(self, path: str) -> AsyncIterator[bytes]:
        """Stream from S3 with proper cleanup"""
        key = self._full_key(path)

        response = await self._client.get_object(
            Bucket=self.bucket,
            Key=key
        )

        body = response['Body']
        try:
            async for chunk in body.iter_chunks():
                yield chunk
        finally:
            body.close()

    async def open_stream_range(
        self,
        path: str,
        start: int,
        end: int
    ) -> AsyncIterator[bytes]:
        """Stream byte range from S3"""
        key = self._full_key(path)

        response = await self._client.get_object(
            Bucket=self.bucket,
            Key=key,
            Range=f"bytes={start}-{end}"
        )

        body = response['Body']
        try:
            async for chunk in body.iter_chunks():
                yield chunk
        finally:
            body.close()

    async def list_directory_page(
        self,
        path: str,
        cursor: Optional[str],
        page_size: int,
        recursive: bool = False
    ) -> Tuple[List[FileInfo], Optional[str]]:
        """List directory with CORRECT S3 pagination """
        prefix = self._full_key(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        kwargs = {
            'Bucket': self.bucket,
            'Prefix': prefix,
            'MaxKeys': page_size
        }

        if not recursive:
            kwargs['Delimiter'] = '/'


        if cursor:
            kwargs['ContinuationToken'] = cursor

        response = await self._client.list_objects_v2(**kwargs)

        files = []

        # Files
        for obj in response.get('Contents', []):
            relative_path = obj['Key']
            if self.prefix:
                relative_path = relative_path[len(self.prefix):].lstrip("/")

            files.append(FileInfo(
                path=f"/{relative_path}",
                size=obj['Size'],
                modified_at=obj['LastModified'],
                mime_type=None,
                is_directory=False,
                nexus_etag="pending",  # Computed lazily
                storage_etag=obj.get('ETag', '').strip('"')  # S3's ETag
            ))

        # Directories (prefixes)
        for prefix_obj in response.get('CommonPrefixes', []):
            relative_path = prefix_obj['Prefix']
            if self.prefix:
                relative_path = relative_path[len(self.prefix):].lstrip("/")

            files.append(FileInfo(
                path=f"/{relative_path}",
                size=0,
                modified_at=None,
                mime_type=None,
                is_directory=True,
                nexus_etag="",
                storage_etag=None
            ))


        next_cursor = response.get('NextContinuationToken')

        return files, next_cursor

    async def write_file(
        self,
        path: str,
        content: bytes,
        size: Optional[int] = None
    ) -> FileInfo:
        """Write file to S3"""
        key = self._full_key(path)

        await self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content
        )

        return await self.stat(path)

    async def delete(self, path: str, recursive: bool = False) -> None:
        """Delete from S3"""
        key = self._full_key(path)

        if recursive:
            objects_to_delete = []
            paginator = self._client.get_paginator('list_objects_v2')

            async for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
                for obj in page.get('Contents', []):
                    objects_to_delete.append({'Key': obj['Key']})

            if objects_to_delete:
                await self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={'Objects': objects_to_delete}
                )
        else:
            await self._client.delete_object(
                Bucket=self.bucket,
                Key=key
            )

    # Multipart upload (wired to database)
    async def initiate_multipart_upload(self, path: str, session_id: str) -> str:
        """Start multipart upload"""
        key = self._full_key(path)

        response = await self._client.create_multipart_upload(
            Bucket=self.bucket,
            Key=key
        )

        upload_id = response['UploadId']
        self._upload_sessions[upload_id] = key

        # Store in database
        await db.execute(
            update(upload_sessions)
            .where(upload_sessions.c.session_id == session_id)
            .values(backend_upload_id=upload_id)
        )

        return upload_id

    async def upload_part(
        self,
        path: str,
        upload_id: str,
        part_number: int,
        data: bytes
    ) -> Dict:
        """Upload one part"""
        key = self._upload_sessions.get(upload_id)
        if not key:
            raise ValueError(f"Upload session not found: {upload_id}")

        response = await self._client.upload_part(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data
        )

        return {
            "part_number": part_number,
            "etag": response['ETag'].strip('"')
        }

    async def complete_multipart_upload(
        self,
        path: str,
        upload_id: str,
        parts: List[Dict]
    ) -> FileInfo:
        """Complete multipart upload"""
        key = self._upload_sessions.get(upload_id)
        if not key:
            raise ValueError(f"Upload session not found: {upload_id}")

        await self._client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                'Parts': [
                    {'PartNumber': p['part_number'], 'ETag': p['etag']}
                    for p in parts
                ]
            }
        )

        del self._upload_sessions[upload_id]

        relative_path = key
        if self.prefix:
            relative_path = key[len(self.prefix):].lstrip("/")

        return await self.stat(f"/{relative_path}")

    # Backend recognition
    async def recognize_path(
        self,
        path: str,
        metadata: Optional[Dict] = None
    ) -> BackendRecognition:
        """Recognize S3 URLs and paths"""
        s3_patterns = [
            r"s3://",
            r"\.s3\.amazonaws\.com",
            r"\.s3-[a-z0-9-]+\.amazonaws\.com",
            r"^/s3/",
        ]

        for pattern in s3_patterns:
            if re.search(pattern, path):
                return BackendRecognition(
                    confidence=0.95,
                    backend_type="s3",
                    mount_point="/s3",
                    readonly=False,
                    suggested_config={}
                )

        # Check for AWS credentials in metadata
        if metadata and metadata.get("aws_credentials"):
            return BackendRecognition(
                confidence=0.7,
                backend_type="s3",
                mount_point="/s3",
                readonly=False
            )

        return BackendRecognition(confidence=0.0, backend_type="s3")
```

### Backend Lifecycle Management

```python
class BackendManager:
    """Manage backend lifecycle"""

    def __init__(self):
        self.backends: Dict[str, StorageBackend] = {}

    async def register(self, backend_id: str, backend: StorageBackend):
        """Register and initialize backend"""
        await backend.initialize()
        self.backends[backend_id] = backend
        logger.info(f"Registered backend: {backend_id} ({backend.type})")

    async def get(self, backend_id: str) -> StorageBackend:
        """Get backend by ID"""
        return self.backends.get(backend_id)

    async def shutdown_all(self):
        """Shutdown all backends"""
        for backend_id, backend in self.backends.items():
            try:
                await backend.shutdown()
                logger.info(f"Shutdown backend: {backend_id}")
            except Exception as e:
                logger.error(f"Error shutting down backend {backend_id}: {e}")

# FastAPI lifecycle
backend_manager = BackendManager()

@app.on_event("startup")
async def startup():
    """Initialize backends on startup"""
    for backend_config in config.backends:
        backend = create_backend(backend_config)
        await backend_manager.register(backend_config.id, backend)

@app.on_event("shutdown")
async def shutdown():
    """Shutdown backends on shutdown"""
    await backend_manager.shutdown_all()
```

### Fenced Lock Manager

```python
from contextlib import asynccontextmanager
import hashlib
import uuid
from fastapi import HTTPException

class FencedLock:
    """Lock with fencing token for idempotency"""

    def __init__(self, path: str, fencing_token: int, lock_manager: "FencedLockManager"):
        self.path = path
        self.fencing_token = fencing_token
        self.lock_manager = lock_manager

    async def check_fence(self):
        """
        Verify fencing token before commit.

        Raises HTTPException if token is stale (another node acquired lock).
        """
        current_token = await self.lock_manager.db.fetch_val(
            """SELECT lock_version
               FROM file_paths
               WHERE path = ? AND tenant_id = ?""",
            self.path,
            self.lock_manager.tenant_id
        )

        if current_token != self.fencing_token:
            raise HTTPException(
                409,
                f"Fencing token mismatch: expected {self.fencing_token}, got {current_token}. "
                "File was modified by another process."
            )

class FencedLockManager:
    """
    Distributed locks with fencing tokens .

    Protocol:
    1. Acquire Redis lock (fast path, prevents contention)
    2. Acquire Postgres advisory lock (strong consistency)
    3. Increment fencing token in Postgres (monotonic)
    4. All operations check token before committing

    Why this works:
    - Redis prevents concurrent attempts (performance)
    - Postgres advisory lock ensures atomicity
    - Fencing token prevents split-brain (safety)
    """

    def __init__(self, redis: Redis, db: Database, instance_id: str, tenant_id: UUID):
        self.redis = redis
        self.db = db
        self.instance_id = instance_id
        self.tenant_id = tenant_id

    @asynccontextmanager
    async def lock(self, path: str, timeout: float = 30.0):
        """
        Acquire fenced lock on path.

        Returns FencedLock with token for idempotency checks.
        """

        lock_key = f"lock:{path}"
        lock_id = f"{self.instance_id}:{uuid.uuid4()}"

        # Step 1: Acquire Redis lock (prevent contention)
        redis_lock = await self.redis.set(
            lock_key,
            lock_id,
            nx=True,  # Only if not exists
            ex=int(timeout)
        )

        if not redis_lock:
            raise HTTPException(409, "Path is locked by another request")

        # Convert path to lock number (Postgres bigint)
        path_hash = hashlib.md5(f"{self.tenant_id}:{path}".encode()).hexdigest()
        lock_num = int(path_hash[:16], 16) % (2**63 - 1)

        try:
            # Step 2: Acquire Postgres advisory lock (strong consistency)
            acquired = await self.db.fetch_val(
                "SELECT pg_try_advisory_lock(?)",
                lock_num
            )

            if not acquired:
                raise HTTPException(409, "Could not acquire database lock")

            # Step 3: Increment fencing token
            result = await self.db.fetch_one(
                """UPDATE file_paths
                   SET
                       lock_version = lock_version + 1,
                       locked_by = ?,
                       locked_at = NOW()
                   WHERE path = ? AND tenant_id = ?
                   RETURNING lock_version""",
                self.instance_id,
                path,
                self.tenant_id
            )

            if not result:
                # Path doesn't exist yet - create placeholder
                result = await self.db.fetch_one(
                    """INSERT INTO file_paths
                       (path, tenant_id, lock_version, locked_by, locked_at,
                        nexus_etag, backend_type, backend_path, is_directory, size)
                       VALUES (?, ?, 1, ?, NOW(), 'pending', 'workspace', ?, false, 0)
                       ON CONFLICT (tenant_id, path) WHERE deleted_at IS NULL
                       DO UPDATE SET
                           lock_version = file_paths.lock_version + 1,
                           locked_by = EXCLUDED.locked_by,
                           locked_at = NOW()
                       RETURNING lock_version""",
                    path,
                    self.tenant_id,
                    self.instance_id,
                    path  # backend_path = path for workspace
                )

            fencing_token = result["lock_version"]

            # Return fenced lock context
            yield FencedLock(
                path=path,
                fencing_token=fencing_token,
                lock_manager=self
            )

        finally:
            # Release Postgres advisory lock
            await self.db.execute(
                "SELECT pg_advisory_unlock(?)",
                lock_num
            )

            # Release Redis lock (with lock_id check to prevent releasing wrong lock)
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            await self.redis.eval(script, keys=[lock_key], args=[lock_id])

            # Clear locked_by
            await self.db.execute(
                """UPDATE file_paths
                   SET locked_by = NULL, locked_at = NULL
                   WHERE path = ? AND tenant_id = ? AND locked_by = ?""",
                path,
                self.tenant_id,
                self.instance_id
            )

# Global instance
lock_manager: Optional[FencedLockManager] = None
```

### Idempotency Cache

```python
import json
from typing import Optional
from datetime import timedelta

class IdempotencyCache:
    """
    Redis-based idempotency cache .

    Stores responses for 24 hours to enable safe client retries.
    """

    def __init__(self, redis: Redis):
        self.redis = redis
        self.ttl = timedelta(hours=24)

    def _cache_key(self, key: str, auth: AuthContext) -> str:
        """Generate cache key with tenant isolation"""
        return f"idem:{auth.tenant_id}:{key}"

    async def seen(self, key: str, auth: AuthContext) -> bool:
        """Check if idempotency key was used"""
        cache_key = self._cache_key(key, auth)
        return await self.redis.exists(cache_key) > 0

    async def get_response(self, key: str, auth: AuthContext) -> Optional[Dict]:
        """Get cached response"""
        cache_key = self._cache_key(key, auth)
        data = await self.redis.get(cache_key)

        if not data:
            return None

        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.error(f"Failed to decode cached response for key: {key}")
            await self.redis.delete(cache_key)
            return None

    async def store(self, key: str, auth: AuthContext, response: Dict):
        """Store response for future requests"""
        cache_key = self._cache_key(key, auth)

        await self.redis.setex(
            cache_key,
            int(self.ttl.total_seconds()),
            json.dumps(response)
        )

        logger.info(f"Cached idempotent response: {key} (expires in 24h)")

# Global instance
idem_cache: Optional[IdempotencyCache] = None

# Initialize in startup
@app.on_event("startup")
async def startup():
    global lock_manager, idem_cache

    # Initialize backends
    for backend_config in config.backends:
        backend = create_backend(backend_config)
        await backend_manager.register(backend_config.id, backend)

    # Initialize lock manager
    lock_manager = FencedLockManager(
        redis=redis_client,
        db=database,
        instance_id=config.instance_id,
        tenant_id=None  # Set per-request
    )

    # Initialize idempotency cache
    idem_cache = IdempotencyCache(redis=redis_client)
```

### Path Validation

```python
import os
import urllib.parse
from fastapi import HTTPException

class PathValidator:
    """
    Path validation and canonicalization .

    Prevents path traversal attacks.
    """

    @staticmethod
    def canonicalize(path: str) -> str:
        """
        Canonicalize path to prevent traversal.

        Rules:
        1. Decode any URL encoding
        2. Normalize (remove .., ., //)
        3. Ensure absolute path
        4. Reject if escapes root
        """

        # Decode URL encoding (prevent %2e%2e attacks)
        decoded = urllib.parse.unquote(path)

        # Reject double-encoded paths
        if urllib.parse.unquote(decoded) != decoded:
            raise HTTPException(400, "Double-encoded paths not allowed")

        # Normalize
        normalized = os.path.normpath(decoded)

        # Ensure absolute
        if not normalized.startswith("/"):
            normalized = "/" + normalized

        # Check for traversal
        parts = normalized.split("/")
        if ".." in parts:
            raise HTTPException(400, "Path traversal detected")

        return normalized

    @staticmethod
    def validate_workspace_path(path: str, workspace_prefix: str = "/workspace") -> str:
        """
        Validate path is within workspace (for MCP).

        MCP should only access /workspace paths.
        """

        canonical = PathValidator.canonicalize(path)

        if not canonical.startswith(workspace_prefix):
            raise HTTPException(
                403,
                f"MCP access restricted to {workspace_prefix} paths"
            )

        return canonical

# Apply in all path handlers
def validate_path(path: str) -> str:
    """Apply to all incoming paths"""
    return PathValidator.canonicalize(f"/{path}")
```

### Resource Limits

```python
class TenantResourceManager:
    """
    Per-tenant resource limits .

    Prevents one tenant from starving others.
    """

    def __init__(self):
        self.tenant_semaphores: Dict[UUID, asyncio.Semaphore] = {}
        self.embedding_semaphores: Dict[UUID, asyncio.Semaphore] = {}

    def get_semaphore(
        self,
        tenant_id: UUID,
        resource_type: str
    ) -> asyncio.Semaphore:
        """Get or create semaphore for tenant"""

        if resource_type == "embeddings":
            if tenant_id not in self.embedding_semaphores:
                # Limit: 10 concurrent embeddings per tenant
                self.embedding_semaphores[tenant_id] = asyncio.Semaphore(10)
            return self.embedding_semaphores[tenant_id]

        elif resource_type == "uploads":
            if tenant_id not in self.tenant_semaphores:
                # Limit: 50 concurrent uploads per tenant
                self.tenant_semaphores[tenant_id] = asyncio.Semaphore(50)
            return self.tenant_semaphores[tenant_id]

        else:
            raise ValueError(f"Unknown resource type: {resource_type}")

    @asynccontextmanager
    async def acquire(
        self,
        tenant_id: UUID,
        resource_type: str
    ):
        """Acquire resource for tenant"""
        sem = self.get_semaphore(tenant_id, resource_type)
        async with sem:
            yield

# Global instance
resource_manager = TenantResourceManager()

# Usage in handlers
@app.post("/api/v1/search/semantic")
async def search_semantic(
    query: str,
    auth: AuthContext = Depends(get_auth_context),
    ...
):
    async with resource_manager.acquire(auth.tenant_id, "embeddings"):
        # Generate embedding (rate-limited per tenant)
        embedding = await embedding_service.embed(query)
        results = await vector_db.search(...)
        return results
```

---

## Agent Memory System (ReasoningBank)

### Memory Architecture

```mermaid
graph TB
    subgraph "Memory Lifecycle"
        A[Agent Task Execution] --> B[Trajectory Generation]
        B --> C[Memory Extraction<br/>LLM-based]
        C --> D[Memory Storage<br/>PostgreSQL + Qdrant]
        D --> E[Memory Consolidation<br/>Automatic]
        E --> F[Memory Retrieval<br/>Test-time]
        F --> A
    end

    subgraph "Memory Structure"
        G[Memory Entry] --> H[Title<br/>Description<br/>Content]
        G --> I[Embedding<br/>768-dim vector]
        G --> J[Metadata<br/>task_type, success, etc]
        G --> K[Lineage<br/>trajectory_id, version]
    end

    subgraph "Memory Operations"
        L[Add Memory] --> M[Duplicate Detection]
        M --> N{Similar?}
        N -->|Yes| O[Update/Merge]
        N -->|No| P[Create New]

        Q[Search Memory] --> R[Vector Search<br/>+ Filters]
        R --> S[Rank Results]
        S --> T[Track Access]
    end
```

### Memory Data Model

**Complete schema for agent memories**:

```sql
-- Agent Memories (PostgreSQL for ACID + lineage)
CREATE TABLE agent_memories (
    id UUID PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,

    -- Memory content
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    content TEXT NOT NULL,

    -- Source tracking
    trajectory_id UUID,            -- Which trajectory created this
    task_type VARCHAR(100),        -- "web_browsing", "coding", etc
    success BOOLEAN NOT NULL,      -- Did task succeed?

    -- Versioning
    version INTEGER NOT NULL DEFAULT 1,
    parent_memory_id UUID REFERENCES agent_memories(id),
    root_memory_id UUID REFERENCES agent_memories(id),
    superseded_by UUID REFERENCES agent_memories(id),
    superseded_at TIMESTAMP,

    -- Lifecycle
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMP,

    -- Usage analytics
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMP,
    helpful_count INTEGER NOT NULL DEFAULT 0,  -- How many times marked helpful

    -- Consolidation metadata
    merged_from UUID[],            -- IDs of memories merged into this
    split_from UUID,               -- ID of memory this was split from

    -- Metadata
    metadata JSONB
);

CREATE INDEX idx_agent_memories_agent ON agent_memories(agent_id);
CREATE INDEX idx_agent_memories_active ON agent_memories(active) WHERE active = TRUE;
CREATE INDEX idx_agent_memories_task_type ON agent_memories(task_type);
CREATE INDEX idx_agent_memories_trajectory ON agent_memories(trajectory_id);
CREATE INDEX idx_agent_memories_root ON agent_memories(root_memory_id);

-- Memory Relationships
CREATE TABLE memory_relationships (
    id UUID PRIMARY KEY,
    source_memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
    target_memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
    relationship_type VARCHAR(50) NOT NULL,  -- 'updates', 'extends', 'derives', 'contradicts'
    confidence FLOAT,                        -- How confident is this relationship
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by UUID REFERENCES agents(agent_id),

    CONSTRAINT memory_relationships_unique UNIQUE (source_memory_id, target_memory_id, relationship_type)
);

CREATE INDEX idx_memory_relationships_source ON memory_relationships(source_memory_id);
CREATE INDEX idx_memory_relationships_target ON memory_relationships(target_memory_id);

-- Memory Access Log (for analytics)
CREATE TABLE memory_access_log (
    id UUID PRIMARY KEY,
    memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,

    accessed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    query_text TEXT,
    similarity_score REAL,

    -- Context
    task_type VARCHAR(100),
    trajectory_id UUID,

    -- Feedback
    memory_helpful BOOLEAN,
    feedback_text TEXT
);

CREATE INDEX idx_memory_access_memory ON memory_access_log(memory_id);
CREATE INDEX idx_memory_access_agent ON memory_access_log(agent_id);
CREATE INDEX idx_memory_access_time ON memory_access_log(accessed_at);

-- Memory Sharing (multi-agent)
CREATE TABLE memory_shares (
    id UUID PRIMARY KEY,
    memory_id UUID NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
    source_agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    target_agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,

    share_mode VARCHAR(50) NOT NULL,  -- 'copy', 'reference', 'move'
    shared_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Permissions
    can_modify BOOLEAN NOT NULL DEFAULT FALSE,
    can_reshare BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_memory_shares_target ON memory_shares(target_agent_id);
```

### Memory Manager Interface

```python
class AgentMemoryManager:
    """
    Production-grade memory system for ReasoningBank-style agents.

    Enhancements over basic ReasoningBank:
    - Automatic versioning
    - Duplicate detection
    - Memory consolidation (merge/prune/decay)
    - Multi-agent isolation and sharing
    - Memory effectiveness analytics
    """

    def __init__(
        self,
        vector_db: AsyncQdrantClient,
        db: Database,
        embedding_model: str = "text-embedding-3-small"
    ):
        self.vector_db = vector_db
        self.db = db
        self.embedding_model = embedding_model
        self.collection_name = "agent_memories"

    # Core operations
    async def add_memory(
        self,
        agent_id: UUID,
        tenant_id: UUID,
        title: str,
        description: str,
        content: str,
        trajectory_id: Optional[UUID] = None,
        task_type: Optional[str] = None,
        success: bool = True,
        metadata: Optional[Dict] = None
    ) -> UUID:
        """
        Add memory with automatic duplicate detection.

        Process:
        1. Generate embedding
        2. Check for duplicates (similarity > 0.95)
        3. If duplicate: update existing
        4. If new: create new entry
        5. Store in both PostgreSQL (ACID) and Qdrant (search)
        """
        pass

    async def search_memories(
        self,
        agent_id: UUID,
        query: str,
        limit: int = 5,
        task_type: Optional[str] = None,
        success_only: bool = False,
        time_window: Optional[timedelta] = None,
        similarity_threshold: float = 0.0
    ) -> List[MemoryResult]:
        """
        Search memories with rich filtering.

        Enhancements:
        - Metadata filtering (task type, success, time)
        - Multi-agent isolation
        - Configurable k and threshold
        - Access tracking (for analytics)
        """
        pass

    async def consolidate_memories(
        self,
        agent_id: UUID,
        strategy: str = "merge_similar"
    ) -> ConsolidationReport:
        """
        Consolidate memories to prevent unbounded growth.

        Strategies:
        - merge_similar: Combine semantically similar memories
        - prune_low_usage: Remove rarely accessed memories
        - temporal_decay: Reduce weight of old memories

        Returns:
            ConsolidationReport with stats on merged/pruned memories
        """
        pass

    async def version_memory(
        self,
        memory_id: UUID,
        new_content: str,
        reason: str
    ) -> UUID:
        """
        Create new version of existing memory.

        Maintains full lineage:
        - parent_memory_id → previous version
        - root_memory_id → first version in chain
        - superseded_by/at → tracking
        """
        pass

    async def share_memory(
        self,
        memory_id: UUID,
        source_agent_id: UUID,
        target_agent_id: UUID,
        share_mode: str = "copy"
    ) -> UUID:
        """
        Share memory between agents.

        Modes:
        - copy: Create independent copy
        - reference: Link to original
        - move: Transfer ownership
        """
        pass

    async def get_memory_analytics(
        self,
        agent_id: UUID,
        time_window: timedelta = timedelta(days=7)
    ) -> MemoryAnalytics:
        """
        Memory effectiveness analytics.

        Metrics:
        - Total/active memories
        - Access frequency
        - Helpfulness rate
        - Task completion correlation
        - Memory growth rate
        """
        pass
```

### Memory Consolidation Flow

```mermaid
graph TB
    A[Memory Bank Growing] --> B{Consolidation<br/>Trigger?}
    B -->|Yes| C[Select Strategy]

    C --> D[Merge Similar]
    C --> E[Prune Low Usage]
    C --> F[Temporal Decay]

    D --> G[Find Similar Clusters<br/>similarity > 0.9]
    G --> H[LLM Synthesis<br/>Merge content]
    H --> I[Create Merged Memory]
    I --> J[Deactivate Originals]

    E --> K[Query Access Stats]
    K --> L{Access < 2<br/>in 30 days?}
    L -->|Yes| M[Deactivate Memory]

    F --> N[Apply Time Decay]
    N --> O[Reduce Search Weight]

    J --> P[Update Analytics]
    M --> P
    O --> P

    style G fill:#FFD700
    style H fill:#87CEEB
```

---

## Content Processing Pipeline (Supermemory)

### Document Processing State Machine

```mermaid
stateDiagram-v2
    [*] --> unknown: File uploaded
    unknown --> queued: Submit to pipeline
    queued --> extracting: Job starts

    extracting --> chunking: Text extracted
    extracting --> failed: Extraction error

    chunking --> embedding: Chunks created
    chunking --> failed: Chunking error

    embedding --> indexing: Embeddings generated
    embedding --> failed: Embedding error

    indexing --> done: Vectors stored
    indexing --> failed: Indexing error

    failed --> [*]
    done --> [*]

    note right of extracting
        Extract text from:
        - PDF (pdfplumber)
        - DOCX (python-docx)
        - Webpage (BeautifulSoup)
        - Image (OCR)
    end note

    note right of chunking
        Strategies:
        - Fixed size (1000 tokens)
        - Semantic (paragraph)
        - Sentence-based
    end note
```

### Processing Pipeline Data Model

```sql
-- Document Processing Jobs
CREATE TABLE document_processing_jobs (
    job_id UUID PRIMARY KEY,
    file_path_id UUID NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,

    -- Pipeline status
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    -- queued, extracting, chunking, embedding, indexing, done, failed

    -- Timing
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds FLOAT,

    -- Error tracking
    error_message TEXT,
    error_step VARCHAR(50),        -- Which step failed
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,

    -- Processing metadata
    processing_steps JSONB,
    -- [
    --   {"name": "extraction", "startTime": ..., "status": "completed", "duration": 1.2},
    --   {"name": "chunking", "startTime": ..., "status": "in_progress"}
    -- ]

    -- Content statistics
    token_count INTEGER,
    word_count INTEGER,
    chunk_count INTEGER,

    -- Chunking configuration
    chunking_strategy VARCHAR(100),  -- "fixed_size", "semantic", "sentence"
    chunk_size INTEGER,
    chunk_overlap INTEGER,

    -- Priority
    priority INTEGER NOT NULL DEFAULT 50,  -- 0-100, higher = process sooner

    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_processing_jobs_status ON document_processing_jobs(status);
CREATE INDEX idx_processing_jobs_priority ON document_processing_jobs(priority DESC, created_at);
CREATE INDEX idx_processing_jobs_file ON document_processing_jobs(file_path_id);

-- File Chunks (for semantic search) - QDRANT-ONLY
CREATE TABLE file_chunks (
    chunk_id UUID PRIMARY KEY,
    path_id UUID NOT NULL REFERENCES file_paths(path_id) ON DELETE CASCADE,

    -- Chunk content
    content TEXT NOT NULL,
    position INTEGER NOT NULL,      -- Order in file (0-indexed)
    type VARCHAR(50) DEFAULT 'text',  -- text, image, code, table

    -- Chunk boundaries
    start_offset INTEGER,           -- Byte offset in original
    end_offset INTEGER,
    start_line INTEGER,             -- Line number
    end_line INTEGER,

    -- Qdrant reference (No pgvector, Qdrant is source of truth)
    qdrant_point_id UUID NOT NULL,       -- Reference to Qdrant point
    qdrant_collection VARCHAR(100) NOT NULL DEFAULT 'file_chunks',  -- Which collection
    embedding_model VARCHAR(100) NOT NULL DEFAULT 'text-embedding-3-small',

    -- Statistics
    token_count INTEGER,
    char_count INTEGER,

    -- Metadata
    metadata JSONB,
    -- Can include: heading, section, language, etc

    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_file_chunks_path ON file_chunks(path_id);
CREATE INDEX idx_file_chunks_position ON file_chunks(path_id, position);
CREATE INDEX idx_file_chunks_type ON file_chunks(type);
CREATE INDEX idx_file_chunks_qdrant ON file_chunks(qdrant_point_id);

-- NOTE: NO pgvector index - Qdrant is the source of truth for vectors
```

### Document Pipeline Manager

```python
class DocumentProcessingPipeline:
    """
    Multi-stage document processing pipeline.

    Based on Supermemory's pipeline design.
    """

    def __init__(
        self,
        db: Database,
        vector_db: AsyncQdrantClient,
        job_queue: JobQueue,
        embedding_service: EmbeddingService
    ):
        self.db = db
        self.vector_db = vector_db
        self.job_queue = job_queue
        self.embedding_service = embedding_service

    async def submit_for_processing(
        self,
        file_path: str,
        path_id: UUID,
        priority: int = 50,
        chunking_strategy: str = "semantic"
    ) -> UUID:
        """
        Submit document for async processing.

        Returns:
            job_id for tracking progress
        """

        job_id = await self.db.execute(
            """INSERT INTO document_processing_jobs
               (file_path_id, status, priority, chunking_strategy)
               VALUES (?, 'queued', ?, ?)
               RETURNING job_id""",
            path_id, priority, chunking_strategy
        )

        await self.job_queue.enqueue(
            "process_document_job",
            job_id=job_id
        )

        return job_id

    async def process_document(self, job_id: UUID):
        """
        Execute full processing pipeline.

        Steps:
        1. Update status to 'extracting'
        2. Extract text from document
        3. Update status to 'chunking'
        4. Create semantic chunks
        5. Update status to 'embedding'
        6. Generate embeddings
        7. Update status to 'indexing'
        8. Store in vector DB
        9. Update status to 'done'

        On error: Update status to 'failed', log error
        """

        steps = []

        try:
            # Step 1: Extraction
            await self._update_job_status(job_id, "extracting")
            step_start = time.time()

            text, metadata = await self._extract_text(job_id)

            steps.append({
                "name": "extraction",
                "status": "completed",
                "duration": time.time() - step_start,
                "metadata": metadata
            })

            # Step 2: Chunking
            await self._update_job_status(job_id, "chunking")
            step_start = time.time()

            chunks = await self._create_chunks(text, job_id)

            steps.append({
                "name": "chunking",
                "status": "completed",
                "duration": time.time() - step_start,
                "chunk_count": len(chunks)
            })

            # Step 3: Embedding
            await self._update_job_status(job_id, "embedding")
            step_start = time.time()

            embeddings = await self._generate_embeddings(chunks)

            steps.append({
                "name": "embedding",
                "status": "completed",
                "duration": time.time() - step_start
            })

            # Step 4: Indexing
            await self._update_job_status(job_id, "indexing")
            step_start = time.time()

            await self._index_chunks(chunks, embeddings, job_id)

            steps.append({
                "name": "indexing",
                "status": "completed",
                "duration": time.time() - step_start
            })

            # Complete
            await self._update_job_status(
                job_id,
                "done",
                processing_steps=steps
            )

        except Exception as e:
            logger.error(f"Processing failed for job {job_id}", exc_info=e)

            await self._update_job_status(
                job_id,
                "failed",
                error_message=str(e),
                processing_steps=steps
            )
```

### Chunking Strategies

```python
class ChunkingStrategy(ABC):
    """Base class for chunking strategies"""

    @abstractmethod
    async def chunk(self, text: str) -> List[Chunk]:
        pass

class FixedSizeChunking(ChunkingStrategy):
    """Fixed token/character chunks with overlap"""

    def __init__(
        self,
        chunk_size: int = 1000,
        overlap: int = 200
    ):
        self.chunk_size = chunk_size
        self.overlap = overlap

    async def chunk(self, text: str) -> List[Chunk]:
        chunks = []
        start = 0

        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunk_text = text[start:end]

            chunks.append(Chunk(
                content=chunk_text,
                position=len(chunks),
                start_offset=start,
                end_offset=end
            ))

            start = end - self.overlap

        return chunks

class SemanticChunking(ChunkingStrategy):
    """Chunk by semantic boundaries (paragraphs, sections)"""

    async def chunk(self, text: str) -> List[Chunk]:
        # Split by double newlines (paragraphs)
        paragraphs = text.split("\n\n")

        chunks = []
        offset = 0

        for i, para in enumerate(paragraphs):
            if para.strip():
                chunks.append(Chunk(
                    content=para.strip(),
                    position=i,
                    start_offset=offset,
                    end_offset=offset + len(para),
                    metadata={"type": "paragraph"}
                ))

            offset += len(para) + 2  # +2 for \n\n

        return chunks
```

---

## Job System with Checkpointing (Timelinize)

### Job State Machine

```mermaid
stateDiagram-v2
    [*] --> queued: Create job
    queued --> started: Worker picks up

    started --> paused: User pause
    paused --> started: User resume

    started --> interrupted: Server restart
    interrupted --> started: Resume from checkpoint

    started --> succeeded: Complete
    started --> failed: Error (unrecoverable)
    started --> aborted: User cancel

    succeeded --> [*]
    failed --> [*]
    aborted --> [*]

    note right of started
        Checkpoints every:
        - 100 items processed
        - 2 seconds elapsed
        - On pause request
    end note
```

### Job System Data Model

```sql
-- Jobs (unified job tracking)
CREATE TABLE jobs (
    job_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE SET NULL,

    -- Job configuration
    type VARCHAR(100) NOT NULL,        -- "semantic_index", "thumbnail_generate", etc
    config JSONB NOT NULL,             -- Job-specific parameters
    config_hash VARCHAR(64) NOT NULL,  -- For deduplication

    -- State
    state VARCHAR(50) NOT NULL DEFAULT 'queued',
    -- queued, started, paused, interrupted, succeeded, failed, aborted

    -- Progress tracking
    progress INTEGER,                  -- Items completed
    total INTEGER,                     -- Total items (if known)
    checkpoint JSONB,                  -- Resumption state

    -- Timing
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Error tracking
    error_message TEXT,
    error_details JSONB,

    -- Execution
    instance_id VARCHAR(100),          -- Which node is running this
    worker_id VARCHAR(100),            -- Which worker process

    -- Priority
    priority INTEGER NOT NULL DEFAULT 50,  -- 0-100

    -- Metadata
    metadata JSONB
);

CREATE INDEX idx_jobs_tenant ON jobs(tenant_id);
CREATE INDEX idx_jobs_state ON jobs(state);
CREATE INDEX idx_jobs_config_hash ON jobs(config_hash);
CREATE INDEX idx_jobs_priority ON jobs(priority DESC, created_at);
CREATE INDEX idx_jobs_instance ON jobs(instance_id);

-- Prevent duplicate jobs
CREATE UNIQUE INDEX idx_jobs_running_unique
    ON jobs(config_hash)
    WHERE state IN ('queued', 'started');
```

### Job System Implementation

```python
class JobState(str, Enum):
    QUEUED = "queued"
    STARTED = "started"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"

class ActiveJob:
    """
    Running job with progress tracking and checkpointing.

    Based on Timelinize's job system design.
    """

    def __init__(
        self,
        job_id: UUID,
        action: "JobAction",
        db: Database
    ):
        self.job_id = job_id
        self.action = action
        self.db = db

        # Progress
        self._progress = 0
        self._total: Optional[int] = None
        self._checkpoint: Optional[Dict] = None

        # Throttling (don't write to DB too often)
        self._last_sync = time.time()
        self._sync_interval = 2.0  # seconds

        # Control
        self._lock = asyncio.Lock()
        self.paused = asyncio.Event()
        self.paused.set()  # Start unpaused
        self._cancelled = False

    async def progress(self, delta: int = 1):
        """
        Increment progress counter.

        Throttled DB sync (only every 2 seconds).
        """
        async with self._lock:
            self._progress += delta
            await self._maybe_sync()

    async def set_total(self, total: int):
        """Set expected total work (enables % complete)"""
        async with self._lock:
            self._total = total
            await self._sync(force=True)  # Always sync total changes

    async def checkpoint(self, checkpoint: Dict):
        """
        Save checkpoint for resumability.

        Checkpoint should contain everything needed to resume:
        - Current offset/position
        - Completed item IDs
        - Any accumulated state

        Example checkpoint:
        {
            "processed_files": ["file1.txt", "file2.txt"],
            "current_offset": 50,
            "last_file_path": "/workspace/data/file50.txt"
        }
        """
        async with self._lock:
            self._checkpoint = checkpoint
            await self._maybe_sync()

    async def _maybe_sync(self):
        """Sync to DB if throttle interval elapsed"""
        if time.time() - self._last_sync >= self._sync_interval:
            await self._sync()

    async def _sync(self, force: bool = False):
        """Write progress/checkpoint to database"""
        if not force and time.time() - self._last_sync < self._sync_interval:
            return

        await self.db.execute(
            """UPDATE jobs
               SET progress=?, total=?, checkpoint=?, updated_at=?
               WHERE job_id=?""",
            self._progress,
            self._total,
            json.dumps(self._checkpoint) if self._checkpoint else None,
            datetime.utcnow(),
            self.job_id
        )

        self._last_sync = time.time()

    async def check_should_continue(self):
        """
        Call this frequently in job loops.

        Handles:
        - Pause requests (blocks until unpaused)
        - Cancellation (raises CancelledError)
        """
        # Check pause
        if not self.paused.is_set():
            await self._sync(force=True)  # Save before pausing
            await self.paused.wait()      # Block until unpaused

        # Check cancellation
        if self._cancelled:
            await self._sync(force=True)
            raise asyncio.CancelledError()

    async def pause(self):
        """Pause job execution"""
        self.paused.clear()
        await self.db.execute(
            """UPDATE jobs
               SET state=?, updated_at=?
               WHERE job_id=?""",
            JobState.PAUSED,
            datetime.utcnow(),
            self.job_id
        )

    async def unpause(self):
        """Resume job execution"""
        self.paused.set()
        await self.db.execute(
            """UPDATE jobs
               SET state=?, updated_at=?
               WHERE job_id=?""",
            JobState.STARTED,
            datetime.utcnow(),
            self.job_id
        )

    async def cancel(self):
        """Cancel job (will raise CancelledError in job loop)"""
        self._cancelled = True

class JobAction(ABC):
    """Base class for job actions"""

    @abstractmethod
    async def run(
        self,
        job: ActiveJob,
        checkpoint: Optional[Dict]
    ):
        """
        Execute job, honoring checkpoint.

        Args:
            job: Active job handle (for progress/checkpoint)
            checkpoint: Previous checkpoint (or None for fresh start)

        Example implementation:

        async def run(self, job, checkpoint):
            # Resume from checkpoint
            start_offset = checkpoint.get("offset", 0) if checkpoint else 0

            # Get work items
            files = await self._list_files()
            await job.set_total(len(files))

            # Process items
            for i, file_path in enumerate(files[start_offset:], start=start_offset):
                # Check if should continue (pause/cancel)
                await job.check_should_continue()

                # Do work
                await self._process_file(file_path)
                await job.progress(1)

                # Checkpoint every 100 items
                if i % 100 == 0:
                    await job.checkpoint({"offset": i + 1})
        """
        pass

class SemanticIndexJob(JobAction):
    """Example job: Index files for semantic search"""

    def __init__(self, path_prefix: str):
        self.path_prefix = path_prefix

    async def run(self, job: ActiveJob, checkpoint: Optional[Dict]):
        start_offset = checkpoint.get("offset", 0) if checkpoint else 0

        # Get files to index
        files = await self._list_files(self.path_prefix)
        await job.set_total(len(files))

        for i, file_path in enumerate(files[start_offset:], start=start_offset):
            # Pausable/cancellable
            await job.check_should_continue()

            # Index file
            await self._index_file(file_path)
            await job.progress(1)

            # Checkpoint
            if i % 100 == 0:
                await job.checkpoint({"offset": i + 1})

class JobManager:
    """Manage job lifecycle"""

    async def create_job(
        self,
        action: JobAction,
        tenant_id: UUID,
        agent_id: UUID,
        priority: int = 50
    ) -> UUID:
        """
        Create and start job.

        Includes deduplication:
        - Hash job config
        - Check for duplicate running job
        - If exists: return existing job_id
        - If new: create and start
        """

        # Hash config for deduplication
        config = action.to_dict()
        config_json = json.dumps(config, sort_keys=True)
        config_hash = hashlib.blake3(config_json.encode()).hexdigest()

        # Check for duplicate
        existing = await self.db.fetch_one(
            """SELECT job_id FROM jobs
               WHERE config_hash=? AND state IN ('queued', 'started')
               LIMIT 1""",
            config_hash
        )

        if existing:
            logger.info(f"Duplicate job detected: {existing['job_id']}")
            return existing["job_id"]

        # Create new job
        job_id = await self.db.execute(
            """INSERT INTO jobs
               (tenant_id, agent_id, type, config, config_hash, state, priority)
               VALUES (?, ?, ?, ?, ?, 'queued', ?)
               RETURNING job_id""",
            tenant_id,
            agent_id,
            action.__class__.__name__,
            config_json,
            config_hash,
            priority
        )

        # Start job
        await self._start_job(job_id, action)

        return job_id

    async def resume_interrupted_jobs(self):
        """
        Resume jobs that were interrupted by server restart.

        Called at startup.
        """

        interrupted_jobs = await self.db.fetch_all(
            """SELECT job_id, type, config, checkpoint
               FROM jobs
               WHERE state IN ('started', 'interrupted')
               AND instance_id=?""",
            self.instance_id
        )

        for job in interrupted_jobs:
            action = self._deserialize_action(job["type"], job["config"])
            await self._start_job(
                job["job_id"],
                action,
                checkpoint=job.get("checkpoint")
            )

            logger.info(f"Resumed interrupted job: {job['job_id']}")
```

### CPU Throttling

```python
class ThrottledExecutor:
    """
    CPU throttling with semaphore.

    Based on Timelinize's CPU throttling pattern.
    """

    def __init__(self, max_concurrent: Optional[int] = None):
        if max_concurrent is None:
            # 75% of CPU cores
            max_concurrent = max(int(os.cpu_count() * 0.75), 1)

        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    async def run(self, coro):
        """Run coroutine with throttling"""
        async with self.semaphore:
            return await coro

# Usage in semantic indexing
executor = ThrottledExecutor()

async def index_files(files: List[str]):
    """Index files with CPU throttling"""
    tasks = [
        executor.run(index_file(f))
        for f in files
    ]
    await asyncio.gather(*tasks)
```

---

## Model Context Protocol (MCP) Integration

### MCP Architecture

```mermaid
graph TB
    subgraph "AI Tools"
        A[Claude Desktop]
        B[Cursor IDE]
        C[Other MCP Clients]
    end

    subgraph "MCP Server (Nexus)"
        D[MCP Protocol Handler]
        E[Tool: nexus_file]
        F[Command Router]
    end

    subgraph "Nexus Core"
        G[Path Router]
        H[Backend Manager]
        I[ACL Manager]
    end

    A -->|MCP| D
    B -->|MCP| D
    C -->|MCP| D

    D --> E
    E --> F

    F -->|view| G
    F -->|create| H
    F -->|str_replace| H
    F -->|insert| H
    F -->|delete| I
    F -->|rename| H

    style D fill:#FFD700
    style E fill:#87CEEB
```

### MCP Tool Specification

```python
class NexusMCPServer:
    """
    Model Context Protocol server exposing Nexus as filesystem.

    Based on Supermemory's MCP integration.

    Enables Claude Desktop, Cursor, and other AI tools to directly
    read/write Nexus files.
    """

    def __init__(
        self,
        nexus_client: NexusClient,
        workspace_prefix: str = "/workspace"
    ):
        self.nexus = nexus_client
        self.workspace_prefix = workspace_prefix

    # MCP Tool Definition
    @mcp_tool
    async def nexus_file(
        self,
        command: str,
        path: str,
        **kwargs
    ) -> Union[str, Dict]:
        """
        Nexus file operations tool.

        Commands:
        - view: Read file or list directory
        - create: Create/overwrite file
        - str_replace: Edit file (search/replace)
        - insert: Insert at line number
        - delete: Delete file
        - rename: Move/rename file

        Security:
        - Only /workspace paths allowed
        - ACL enforced
        - Path traversal prevention

        Examples:

        # Read file
        nexus_file(command="view", path="/workspace/report.txt")

        # List directory
        nexus_file(command="view", path="/workspace/")

        # Create file
        nexus_file(
            command="create",
            path="/workspace/output.txt",
            file_text="Analysis complete"
        )

        # Edit file
        nexus_file(
            command="str_replace",
            path="/workspace/config.yaml",
            old_str="debug: false",
            new_str="debug: true"
        )
        """

        # Security: Enforce workspace prefix
        if not path.startswith(self.workspace_prefix):
            raise ValueError(
                f"MCP access restricted to {self.workspace_prefix} paths"
            )

        # Normalize path (prevent traversal)
        path = self._normalize_path(path)

        match command:
            case "view":
                return await self._handle_view(path, kwargs)

            case "create":
                return await self._handle_create(path, kwargs)

            case "str_replace":
                return await self._handle_str_replace(path, kwargs)

            case "insert":
                return await self._handle_insert(path, kwargs)

            case "delete":
                return await self._handle_delete(path, kwargs)

            case "rename":
                return await self._handle_rename(path, kwargs)

            case _:
                raise ValueError(f"Unknown command: {command}")

    async def _handle_view(
        self,
        path: str,
        kwargs: Dict
    ) -> str:
        """Handle view command (read file or list dir)"""

        # Directory listing
        if path.endswith("/"):
            files = await self.nexus.list(path, recursive=False)

            result = f"Directory: {path}\n\n"
            for f in files:
                icon = "📁" if f.is_directory else "📄"
                size = f"({f.size} bytes)" if not f.is_directory else ""
                result += f"{icon} {f.name} {size}\n"

            return result

        # File read (with optional range)
        view_range = kwargs.get("view_range")  # e.g., "1-100" for lines 1-100

        content = await self.nexus.read(path)

        if view_range:
            lines = content.decode('utf-8').split('\n')
            start, end = self._parse_range(view_range)
            lines = lines[start-1:end]  # 1-indexed to 0-indexed
            return '\n'.join(f"{i+start:4d} | {line}" for i, line in enumerate(lines))
        else:
            return content.decode('utf-8')

    async def _handle_create(
        self,
        path: str,
        kwargs: Dict
    ) -> Dict:
        """Handle create command (write file)"""

        file_text = kwargs.get("file_text", "")

        await self.nexus.write(path, file_text.encode('utf-8'))

        return {
            "status": "created",
            "path": path,
            "size": len(file_text)
        }

    async def _handle_str_replace(
        self,
        path: str,
        kwargs: Dict
    ) -> Dict:
        """Handle str_replace command (edit file)"""

        old_str = kwargs.get("old_str")
        new_str = kwargs.get("new_str")

        if not old_str:
            raise ValueError("old_str required for str_replace")

        # Read file
        content = await self.nexus.read(path)
        text = content.decode('utf-8')

        # Check if old_str exists
        if old_str not in text:
            raise ValueError(f"String not found: {old_str[:50]}...")

        # Replace
        new_text = text.replace(old_str, new_str, 1)  # Replace first occurrence

        # Write back
        await self.nexus.write(path, new_text.encode('utf-8'))

        return {
            "status": "replaced",
            "path": path,
            "old_size": len(text),
            "new_size": len(new_text)
        }

    async def _handle_insert(
        self,
        path: str,
        kwargs: Dict
    ) -> Dict:
        """Handle insert command (insert at line)"""

        insert_line = kwargs.get("insert_line")
        insert_text = kwargs.get("insert_text", "")

        if insert_line is None:
            raise ValueError("insert_line required")

        # Read file
        content = await self.nexus.read(path)
        lines = content.decode('utf-8').split('\n')

        # Insert
        lines.insert(insert_line - 1, insert_text)  # 1-indexed to 0-indexed

        new_text = '\n'.join(lines)

        # Write back
        await self.nexus.write(path, new_text.encode('utf-8'))

        return {
            "status": "inserted",
            "path": path,
            "line": insert_line
        }

    async def _handle_delete(
        self,
        path: str,
        kwargs: Dict
    ) -> Dict:
        """Handle delete command"""

        await self.nexus.delete(path)

        return {
            "status": "deleted",
            "path": path
        }

    async def _handle_rename(
        self,
        path: str,
        kwargs: Dict
    ) -> Dict:
        """Handle rename command"""

        new_path = kwargs.get("new_path")

        if not new_path:
            raise ValueError("new_path required for rename")

        # Security: Enforce workspace prefix for new_path
        if not new_path.startswith(self.workspace_prefix):
            raise ValueError("new_path must be in workspace")

        # Read, write, delete (move)
        content = await self.nexus.read(path)
        await self.nexus.write(new_path, content)
        await self.nexus.delete(path)

        return {
            "status": "renamed",
            "old_path": path,
            "new_path": new_path
        }

    def _normalize_path(self, path: str) -> str:
        """Normalize path and prevent traversal"""
        # Remove ../ and ./ components
        parts = []
        for part in path.split('/'):
            if part == '..':
                if parts:
                    parts.pop()
            elif part and part != '.':
                parts.append(part)

        return '/' + '/'.join(parts)

    def _parse_range(self, range_str: str) -> Tuple[int, int]:
        """Parse range string like '1-100' to (1, 100)"""
        start, end = range_str.split('-')
        return int(start), int(end)

# MCP server configuration
# .claude/mcp-servers.json
{
  "nexus": {
    "command": "python",
    "args": ["-m", "nexus.mcp.server"],
    "env": {
      "NEXUS_API_URL": "http://localhost:8000",
      "NEXUS_API_KEY": "nexus_..."
    }
  }
}
```

---

## OAuth Token Management

### Token Refresh Architecture

```mermaid
graph TB
    A[Backend Request] --> B{Token Valid?}
    B -->|Yes| C[Use Token]
    B -->|No| D[Refresh Token]

    D --> E[Call OAuth Provider]
    E --> F[Get New Token]
    F --> G[Persist to Database]
    G --> C

    C --> H[Make API Call]

    style D fill:#FFD700
    style G fill:#87CEEB
```

### Token Persistence Implementation

```python
class PersistingTokenSource:
    """
    Auto-persist refreshed OAuth tokens.

    Based on Timelinize's persistedTokenSource pattern.

    Wraps OAuth2 token source to automatically save
    refreshed tokens to database.
    """

    def __init__(
        self,
        db: Database,
        account_id: UUID,
        oauth_config: OAuth2Config,
        initial_token: Dict
    ):
        self.db = db
        self.account_id = account_id
        self.oauth_config = oauth_config
        self._token = initial_token
        self._lock = asyncio.Lock()

    async def get_token(self) -> Dict:
        """
        Get valid token, refreshing if needed.

        Thread-safe with lock.
        Auto-persists on refresh.
        """
        async with self._lock:
            # Check expiration
            if self._is_expired():
                logger.info(f"Token expired for account {self.account_id}, refreshing")
                self._token = await self._refresh_token()
                await self._persist_token()
                logger.info(f"Token refreshed and persisted for account {self.account_id}")

            return self._token

    def _is_expired(self) -> bool:
        """Check if token is expired"""
        if "expires_at" not in self._token:
            return False

        expires_at = datetime.fromtimestamp(self._token["expires_at"])

        # Refresh 5 minutes early
        return datetime.utcnow() >= expires_at - timedelta(minutes=5)

    async def _refresh_token(self) -> Dict:
        """Refresh using OAuth2 provider"""

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.oauth_config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token["refresh_token"],
                    "client_id": self.oauth_config.client_id,
                    "client_secret": self.oauth_config.client_secret,
                },
                timeout=30.0
            )

            response.raise_for_status()
            new_token = response.json()

            # Calculate expires_at if not provided
            if "expires_in" in new_token and "expires_at" not in new_token:
                new_token["expires_at"] = (
                    datetime.utcnow() + timedelta(seconds=new_token["expires_in"])
                ).timestamp()

            # Preserve refresh token if not returned
            if "refresh_token" not in new_token:
                new_token["refresh_token"] = self._token["refresh_token"]

            return new_token

    async def _persist_token(self):
        """Save to database"""

        await self.db.execute(
            """UPDATE accounts
               SET oauth_token=?, modified_at=?
               WHERE account_id=?""",
            json.dumps(self._token),
            datetime.utcnow(),
            self.account_id
        )

        logger.debug(f"Persisted token for account {self.account_id}")

# OAuth Accounts Table
CREATE TABLE oauth_accounts (
    account_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID REFERENCES agents(agent_id) ON DELETE CASCADE,

    provider VARCHAR(50) NOT NULL,  -- "google", "microsoft", etc
    provider_user_id VARCHAR(255),  -- User ID from provider
    email VARCHAR(255),

    oauth_token JSONB NOT NULL,     -- Full token object
    -- {
    --   "access_token": "...",
    --   "refresh_token": "...",
    --   "expires_at": 1234567890,
    --   "scope": "drive.file",
    --   "token_type": "Bearer"
    -- }

    scopes TEXT[],                  -- Granted scopes

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    modified_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_refreshed_at TIMESTAMP,

    status VARCHAR(50) NOT NULL DEFAULT 'active',

    metadata JSONB
);

CREATE INDEX idx_oauth_accounts_tenant ON oauth_accounts(tenant_id);
CREATE INDEX idx_oauth_accounts_provider ON oauth_accounts(provider, provider_user_id);
CREATE INDEX idx_oauth_accounts_agent ON oauth_accounts(agent_id);

-- OAuth State (CSRF prevention)
CREATE TABLE oauth_states (
    state_token VARCHAR(255) PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,

    redirect_url TEXT,
    metadata JSONB,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL  -- Expire after 10 minutes
);

CREATE INDEX idx_oauth_states_expires ON oauth_states(expires_at);

-- Auto-cleanup expired states
CREATE OR REPLACE FUNCTION delete_expired_oauth_states()
RETURNS trigger AS $$
BEGIN
    DELETE FROM oauth_states WHERE expires_at < NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER cleanup_expired_oauth_states
    AFTER INSERT ON oauth_states
    EXECUTE FUNCTION delete_expired_oauth_states();
```

### Ephemeral Backend with Token Source

```python
class EphemeralBackendManager:
    """Create temporary backends with auto-refreshing tokens"""

    async def create_backend_with_token(
        self,
        backend_type: str,
        account_id: UUID,
        oauth_config: OAuth2Config,
        token: Dict
    ) -> StorageBackend:
        """
        Create ephemeral backend with persisting token source.

        The backend will automatically refresh tokens as needed.
        """

        # Create token source
        token_source = PersistingTokenSource(
            self.db,
            account_id,
            oauth_config,
            token
        )

        # Create backend with token getter
        if backend_type == "gdrive":
            backend = GoogleDriveBackend(
                token_getter=token_source.get_token
            )
        elif backend_type == "sharepoint":
            backend = SharePointBackend(
                token_getter=token_source.get_token
            )
        else:
            raise ValueError(f"Unknown backend type: {backend_type}")

        await backend.initialize()

        return backend
```

---

## Backend Recognition & Auto-Mount

### Recognition Flow

```mermaid
graph TB
    A[Agent provides URL/Path] --> B[Recognition Phase]

    B --> C{URL Pattern?}
    C -->|drive.google.com| D[Google Drive<br/>Confidence: 0.95]
    C -->|sharepoint.com| E[SharePoint<br/>Confidence: 0.95]
    C -->|s3.amazonaws.com| F[S3<br/>Confidence: 0.95]
    C -->|No match| G{OAuth Token?}

    G -->|Google token| D
    G -->|Microsoft token| E
    G -->|AWS creds| F
    G -->|No token| H[Manual Mount Required]

    D --> I{Auto-mount?}
    E --> I
    F --> I

    I -->|Yes| J[Create Mount]
    I -->|No| K[Suggest to User]

    J --> L[Route Request]
    K --> H

    style D fill:#90EE90
    style E fill:#90EE90
    style F fill:#90EE90
```

### Backend Registry with Recognition

```python
class BackendRegistry:
    """Registry of backend types with recognition"""

    def __init__(self):
        self._backends: Dict[str, Type[StorageBackend]] = {}
        self._recognizers: List[StorageBackend] = []

    def register(
        self,
        backend_type: str,
        backend_class: Type[StorageBackend]
    ):
        """Register backend type"""
        self._backends[backend_type] = backend_class

        # Create recognizer instance (no credentials needed)
        recognizer = backend_class.create_recognizer()
        self._recognizers.append(recognizer)

    async def recognize(
        self,
        path: str,
        metadata: Optional[Dict] = None
    ) -> List[BackendRecognition]:
        """
        Run recognition across all backends.

        Returns list sorted by confidence (highest first).
        """

        results = []

        for recognizer in self._recognizers:
            recognition = await recognizer.recognize_path(path, metadata)

            if recognition.confidence > 0.0:
                results.append(recognition)

        # Sort by confidence
        results.sort(key=lambda r: r.confidence, reverse=True)

        return results

    async def auto_mount(
        self,
        path: str,
        tenant_id: UUID,
        agent_id: UUID,
        metadata: Optional[Dict] = None
    ) -> Optional[str]:
        """
        Automatically mount backend if recognized with high confidence.

        Returns:
            mount_point if auto-mounted, None otherwise
        """

        recognitions = await self.recognize(path, metadata)

        if not recognitions:
            return None

        best = recognitions[0]

        # Only auto-mount if very confident
        if best.confidence < 0.9:
            logger.info(f"Recognition confidence too low: {best.confidence}")
            return None

        # Check if already mounted
        existing = await mount_registry.get_mount(best.mount_point, tenant_id)
        if existing:
            logger.info(f"Mount already exists: {best.mount_point}")
            return best.mount_point

        # Create backend
        backend_class = self._backends[best.backend_type]
        backend = backend_class(**best.suggested_config)
        await backend.initialize()

        # Register mount
        await mount_registry.register(
            mount_point=best.mount_point,
            backend=backend,
            tenant_id=tenant_id,
            agent_id=agent_id,
            readonly=best.readonly,
            auto_mounted=True
        )

        logger.info(f"Auto-mounted {best.backend_type} at {best.mount_point}")

        return best.mount_point
```

---

## Complete Data Model

### Full Database Schema

```mermaid
erDiagram
    TENANTS ||--o{ AGENTS : has
    TENANTS ||--o{ API_KEYS : has
    TENANTS ||--o{ FILE_PATHS : owns
    TENANTS ||--o{ MOUNTS : has
    TENANTS ||--o{ OAUTH_ACCOUNTS : has

    AGENTS ||--o{ API_KEYS : uses
    AGENTS ||--o{ FILE_PATHS : creates
    AGENTS ||--o{ AGENT_MEMORIES : has
    AGENTS ||--o{ JOBS : runs

    FILE_PATHS ||--o{ FILE_CHUNKS : chunks_into
    FILE_PATHS ||--o{ FILE_LINEAGE : lineage
    FILE_PATHS ||--o{ CONTENT_OBJECTS : references
    FILE_PATHS ||--o{ DOCUMENT_PROCESSING_JOBS : processes

    AGENT_MEMORIES ||--o{ MEMORY_RELATIONSHIPS : relates
    AGENT_MEMORIES ||--o{ MEMORY_ACCESS_LOG : tracks
    AGENT_MEMORIES ||--o{ MEMORY_SHARES : shares

    TENANTS {
        uuid tenant_id PK
        varchar name
        bigint quota_bytes
        varchar status
    }

    AGENTS {
        uuid agent_id PK
        uuid tenant_id FK
        varchar name
        bigint quota_bytes
        varchar status
    }

    FILE_PATHS {
        uuid path_id PK
        uuid tenant_id FK
        text path
        varchar content_id
        boolean is_directory
        bigint size
        varchar etag
        varchar document_type
        text[] container_tags
        varchar processing_status
    }

    AGENT_MEMORIES {
        uuid id PK
        uuid agent_id FK
        varchar title
        text description
        text content
        integer version
        boolean active
        integer access_count
    }

    DOCUMENT_PROCESSING_JOBS {
        uuid job_id PK
        uuid file_path_id FK
        varchar status
        jsonb processing_steps
        integer chunk_count
    }

    FILE_CHUNKS {
        uuid chunk_id PK
        uuid path_id FK
        text content
        integer position
        vector embedding
    }

    JOBS {
        uuid job_id PK
        uuid tenant_id FK
        varchar type
        jsonb config
        varchar state
        integer progress
        jsonb checkpoint
    }
```

### Key Table Additions Summary

**From ReasoningBank**:
- `agent_memories` - Agent memory storage
- `memory_relationships` - Memory graph
- `memory_access_log` - Analytics
- `memory_shares` - Multi-agent sharing

**From Supermemory**:
- `document_processing_jobs` - Pipeline tracking
- `file_chunks` - Semantic chunks
- `oauth_states` - CSRF prevention
- Document type & container tags in `file_paths`

**From Timelinize**:
- `jobs` - Unified job system
- `oauth_accounts` - Token persistence
- Checkpoint support in jobs

**Original Nexus**:
- `file_paths` - File metadata
- `content_objects` - CAS deduplication
- `file_lineage` - Recomputation
- `mounts` - Backend mounts

---

## API Design Patterns

### Enhanced File Operations

```python
# ============= Semantic Read (NEW) =============

@app.get("/api/v1/files/{path:path}/semantic")
async def semantic_read(
    path: str,
    query: str,
    limit: int = 5,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """
    Read file with semantic search within content.

    Use case: Find relevant sections of large document.

    Example:
        GET /api/v1/files/workspace/report.pdf/semantic?query=budget%20analysis

    Returns:
        Top-K chunks matching query with scores.
    """

    virtual_path = f"/{path}"

    # Get file metadata
    meta = await metadata_store.get(virtual_path)
    if not meta:
        raise HTTPException(404, "File not found")

    # Search within file chunks
    chunks = await chunk_store.search_in_file(
        path_id=meta.path_id,
        query=query,
        limit=limit
    )

    return {
        "path": virtual_path,
        "query": query,
        "chunks": [
            {
                "content": c.content,
                "score": c.score,
                "position": c.position,
                "start_line": c.start_line,
                "end_line": c.end_line
            }
            for c in chunks
        ]
    }


# ============= LLM-Powered Semantic Read with KV Cache (NEW) =============

@app.post("/api/v1/files/{path:path}/semantic/llm")
async def llm_semantic_read(
    path: str,
    query: str,
    provider: Optional[str] = None,  # "claude", "openai", "gemini", or auto
    model: Optional[str] = None,
    cache_strategy: str = "auto",  # "auto", "enabled", "disabled"
    max_tokens: int = 4096,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """
    AI-powered semantic read with automatic LLM KV caching.

    **Cost Optimization**: Automatically manages LLM prompt caching for 50-90% cost savings.

    Query Parameters:
        - query: Question or instruction for the LLM
        - provider: LLM provider (auto-selected if not specified)
        - model: Specific model (e.g., "claude-3-5-sonnet", "gpt-4o")
        - cache_strategy: "auto" (default), "enabled", or "disabled"

    Returns:
        - content: LLM response
        - cost_analysis: Token usage and cost savings
        - cache_performance: Cache hit/miss metrics

    Example:
        POST /api/v1/files/workspace/handbook.pdf/semantic/llm
        {
            "query": "What is the remote work policy?",
            "cache_strategy": "auto"
        }

        Response:
        {
            "content": "The remote work policy allows...",
            "cost_analysis": {
                "input_tokens": 50234,
                "output_tokens": 150,
                "cached_tokens": 49500,
                "base_cost": 0.375,
                "actual_cost": 0.030,
                "savings_percent": 92.0
            },
            "cache_performance": {
                "cache_hit": true,
                "cache_age_seconds": 120,
                "provider": "claude",
                "model": "claude-3-5-sonnet-20241022"
            }
        }

    **How It Works**:

    1. File Content Caching:
       - Nexus sends file content to LLM with cache control headers
       - LLM provider (Claude/OpenAI/Gemini) caches the file content
       - Cache TTL: 5-10 minutes depending on provider

    2. Subsequent Queries:
       - Same file content is retrieved from provider's KV cache
       - Only new query is processed (incremental cost)
       - Cost reduction: 50-90% depending on provider

    3. Cache Invalidation:
       - Automatic on file updates (tracked via ETag)
       - Manual via cache_strategy="disabled"

    **Provider Comparison (2024-2025)**:

    | Provider | Cache Write | Cache Read | TTL | Minimum Size |
    |----------|-------------|------------|-----|--------------|
    | Claude   | 1.25× base  | 0.1× base  | 5 min | None |
    | OpenAI   | 1.0× base   | 0.5× base  | 5-10 min | 1024 tokens |
    | Gemini   | Similar     | Similar    | Variable | 32K tokens |

    **Cost Example** (100K token document):
    - First query: $0.375 (cache write)
    - Next 10 queries: $0.30 total (cache reads)
    - Without caching: $3.75 total
    - **Savings: 92%**
    """

    virtual_path = f"/{path}"

    # Check permissions
    if not await acl_manager.can_read(virtual_path, auth):
        raise HTTPException(403, "Permission denied")

    # Get file metadata
    meta = await metadata_store.get(virtual_path)
    if not meta:
        raise HTTPException(404, "File not found")

    # Route to backend and read file
    backend, backend_path = path_router.route(virtual_path)
    content = await backend.read_file(backend_path)

    # Determine cache strategy
    if cache_strategy == "auto":
        # Enable caching for files > 4KB (~1K tokens)
        cache_enabled = meta.size >= 4096
    else:
        cache_enabled = (cache_strategy == "enabled")

    # Auto-select provider if not specified
    if not provider:
        provider = select_optimal_provider(meta.size, cache_enabled)

    # Execute LLM semantic read with caching
    response = await llm_manager.semantic_read(
        provider=provider,
        model=model,
        file_content=content,
        file_path=virtual_path,
        file_etag=meta.nexus_etag,
        query=query,
        cache_enabled=cache_enabled,
        max_tokens=max_tokens
    )

    # Audit log
    await audit_logger.log(
        action="llm_semantic_read",
        resource_type="file",
        resource_path=virtual_path,
        auth=auth,
        metadata={
            "provider": response.provider,
            "cache_hit": response.cache_hit,
            "cost_saved": response.cost_analysis.savings_percent
        }
    )

    return {
        "content": response.content,
        "cost_analysis": {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cached_tokens": response.cached_tokens,
            "base_cost": response.base_cost,
            "actual_cost": response.actual_cost,
            "savings_percent": response.savings_percent
        },
        "cache_performance": {
            "cache_hit": response.cache_hit,
            "cache_age_seconds": response.cache_age.total_seconds() if response.cache_age else None,
            "provider": response.provider,
            "model": response.model
        },
        "metadata": {
            "file_size": meta.size,
            "file_etag": meta.nexus_etag,
            "mime_type": meta.mime_type
        }
    }


@app.post("/api/v1/files/{path:path}/semantic/warm-cache")
async def warm_llm_cache(
    path: str,
    provider: Optional[str] = None,
    priority: str = "normal",  # "high", "normal", "low"
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """
    Pre-warm LLM cache for a file before queries arrive.

    Use case: Batch processing or scheduled operations.

    Example:
        POST /api/v1/files/workspace/monthly-report.pdf/semantic/warm-cache
        {
            "provider": "claude",
            "priority": "high"
        }

        Response:
        {
            "status": "warming",
            "provider": "claude",
            "estimated_ready": "2024-10-11T10:23:45Z",
            "cache_ttl_seconds": 300
        }
    """

    virtual_path = f"/{path}"

    if not await acl_manager.can_read(virtual_path, auth):
        raise HTTPException(403, "Permission denied")

    # Queue cache warming job
    job_id = await cache_warmer.schedule_warm(
        path=virtual_path,
        provider=provider,
        priority=priority,
        tenant_id=auth.tenant_id
    )

    return {
        "status": "warming",
        "job_id": str(job_id),
        "provider": provider or "auto",
        "estimated_ready": datetime.utcnow() + timedelta(seconds=30),
        "cache_ttl_seconds": 300  # 5 minutes
    }


@app.get("/api/v1/analytics/cache")
async def get_cache_analytics(
    period: str = "last_7_days",  # "last_24h", "last_7_days", "last_30_days"
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """
    Get LLM cache performance analytics.

    Returns cost savings, hit rates, and top cached files.

    Example Response:
    {
        "period": "last_7_days",
        "total_requests": 15234,
        "cache_hit_rate": 0.78,
        "total_cost": 127.45,
        "total_savings": 456.23,
        "cost_reduction_percent": 78.2,
        "top_cached_files": [
            {
                "path": "/shared/docs/handbook.pdf",
                "access_count": 234,
                "cache_hit_rate": 0.95,
                "savings_usd": 89.50
            }
        ],
        "by_provider": {
            "claude": {
                "requests": 10000,
                "cache_hit_rate": 0.82,
                "savings_usd": 380.45
            },
            "openai": {
                "requests": 5234,
                "cache_hit_rate": 0.70,
                "savings_usd": 75.78
            }
        }
    }
    """

    # Get analytics from cache manager
    analytics = await cache_analytics.get_stats(
        tenant_id=auth.tenant_id,
        period=period
    )

    return analytics.to_dict()


def select_optimal_provider(file_size: int, cache_enabled: bool) -> str:
    """
    Select optimal LLM provider based on file characteristics.

    Strategy:
    - Claude: Best cache savings (90%), no minimum size
    - OpenAI: Good savings (50%), automatic, requires 1024+ tokens
    - Gemini: Only for very large files (32K+ tokens)
    """

    if not cache_enabled:
        return "openai"  # Cheapest without caching

    # Gemini threshold: 32K tokens ≈ 128KB
    if file_size >= 131072:
        return "gemini"

    # Default to Claude for best cache economics
    return "claude"


# ============= Memory Operations (NEW) =============

@app.post("/api/v1/agents/{agent_id}/memories")
async def add_agent_memory(
    agent_id: UUID,
    title: str,
    description: str,
    content: str,
    task_type: Optional[str] = None,
    success: bool = True,
    trajectory_id: Optional[UUID] = None,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Add memory to agent's memory bank"""

    memory_id = await memory_manager.add_memory(
        agent_id=agent_id,
        tenant_id=auth.tenant_id,
        title=title,
        description=description,
        content=content,
        task_type=task_type,
        success=success,
        trajectory_id=trajectory_id
    )

    return {
        "memory_id": str(memory_id),
        "status": "created"
    }

@app.post("/api/v1/agents/{agent_id}/memories/search")
async def search_agent_memories(
    agent_id: UUID,
    query: str,
    limit: int = 5,
    task_type: Optional[str] = None,
    success_only: bool = False,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Search agent's memory bank"""

    memories = await memory_manager.search_memories(
        agent_id=agent_id,
        query=query,
        limit=limit,
        task_type=task_type,
        success_only=success_only
    )

    return {
        "query": query,
        "results": memories,
        "count": len(memories)
    }

@app.post("/api/v1/agents/{agent_id}/memories/consolidate")
async def consolidate_agent_memories(
    agent_id: UUID,
    strategy: str = "merge_similar",
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Consolidate agent's memories"""

    if "admin" not in auth.scopes:
        raise HTTPException(403, "Admin scope required")

    report = await memory_manager.consolidate_memories(
        agent_id=agent_id,
        strategy=strategy
    )

    return report.to_dict()


# ============= Job Management (NEW) =============

@app.post("/api/v1/jobs")
async def create_job(
    type: str,
    config: Dict,
    priority: int = 50,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Create background job"""

    action = job_registry.create_action(type, config)

    job_id = await job_manager.create_job(
        action=action,
        tenant_id=auth.tenant_id,
        agent_id=auth.agent_id,
        priority=priority
    )

    return {
        "job_id": str(job_id),
        "status": "queued"
    }

@app.get("/api/v1/jobs/{job_id}")
async def get_job_status(
    job_id: UUID,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Get job status and progress"""

    job = await job_manager.get_job(job_id, auth.tenant_id)

    return {
        "job_id": str(job_id),
        "state": job.state,
        "progress": job.progress,
        "total": job.total,
        "percent_complete": (
            int((job.progress / job.total) * 100)
            if job.total else None
        ),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "error_message": job.error_message
    }

@app.post("/api/v1/jobs/{job_id}/pause")
async def pause_job(
    job_id: UUID,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Pause running job"""

    await job_manager.pause_job(job_id, auth.tenant_id)

    return {"status": "paused"}

@app.post("/api/v1/jobs/{job_id}/resume")
async def resume_job(
    job_id: UUID,
    auth: AuthContext = Depends(get_auth_context)
) -> Dict:
    """Resume paused job"""

    await job_manager.resume_job(job_id, auth.tenant_id)

    return {"status": "resumed"}
```

---

## Observability & Monitoring

### Three Pillars of Observability

Nexus implements comprehensive observability using the industry-standard three pillars:

```mermaid
graph LR
    App[Nexus API] --> Metrics[Prometheus Metrics]
    App --> Traces[OpenTelemetry Traces]
    App --> Logs[Structured JSON Logs]

    Metrics --> Grafana[Grafana Dashboards]
    Traces --> Jaeger[Jaeger UI]
    Logs --> Loki[Grafana Loki]

    Grafana --> Alerts[Alert Manager]
    Alerts --> PagerDuty[PagerDuty/Slack]
```

---

### 1. Metrics (Prometheus + Grafana)

#### Instrumentation

```python
from prometheus_client import Counter, Histogram, Gauge, Summary
import time
from functools import wraps

# ============ RED Metrics (Rate, Errors, Duration) ============

# Request counters
http_requests_total = Counter(
    'nexus_http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status_code', 'tenant_id']
)

http_request_duration_seconds = Histogram(
    'nexus_http_request_duration_seconds',
    'HTTP request latency',
    ['method', 'endpoint'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# File operation metrics
file_operations_total = Counter(
    'nexus_file_operations_total',
    'Total file operations',
    ['operation', 'backend', 'status']  # operation: read/write/delete
)

file_operation_duration_seconds = Histogram(
    'nexus_file_operation_duration_seconds',
    'File operation latency',
    ['operation', 'backend'],
    buckets=[0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]
)

file_size_bytes = Summary(
    'nexus_file_size_bytes',
    'File size distribution',
    ['operation']
)

# ============ USE Metrics (Utilization, Saturation, Errors) ============

# Lock contention
lock_acquisitions_total = Counter(
    'nexus_lock_acquisitions_total',
    'Total lock acquisitions',
    ['status']  # status: success/timeout/error
)

lock_wait_duration_seconds = Histogram(
    'nexus_lock_wait_duration_seconds',
    'Time waiting for lock',
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0]
)

lock_queue_depth = Gauge(
    'nexus_lock_queue_depth',
    'Number of requests waiting for locks'
)

# Job queue
job_queue_depth = Gauge(
    'nexus_job_queue_depth',
    'Number of jobs in queue',
    ['state']  # state: queued/started
)

job_processing_duration_seconds = Histogram(
    'nexus_job_processing_duration_seconds',
    'Job processing time',
    ['job_type', 'status']
)

# Vector store
vector_search_duration_seconds = Histogram(
    'nexus_vector_search_duration_seconds',
    'Vector search latency',
    buckets=[0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0]
)

vector_count = Gauge(
    'nexus_vector_count',
    'Total vectors indexed',
    ['tenant_id']
)

# Database connection pool
db_connections_active = Gauge(
    'nexus_db_connections_active',
    'Active database connections'
)

db_connections_idle = Gauge(
    'nexus_db_connections_idle',
    'Idle database connections'
)

# ============ Middleware for Auto-Instrumentation ============

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()

        # Extract tenant_id if authenticated
        tenant_id = "unknown"
        if hasattr(request.state, "auth"):
            tenant_id = str(request.state.auth.tenant_id)

        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.time() - start_time

            # Record metrics
            http_requests_total.labels(
                method=request.method,
                endpoint=request.url.path,
                status_code=response.status_code if response else 500,
                tenant_id=tenant_id
            ).inc()

            http_request_duration_seconds.labels(
                method=request.method,
                endpoint=request.url.path
            ).observe(duration)

# Decorator for file operations
def instrument_file_operation(operation: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            backend = kwargs.get('backend', 'unknown')
            start_time = time.time()
            status = "success"

            try:
                result = await func(*args, **kwargs)

                # Track file size if available
                if hasattr(result, 'size'):
                    file_size_bytes.labels(operation=operation).observe(result.size)

                return result

            except Exception as e:
                status = "error"
                raise

            finally:
                duration = time.time() - start_time

                file_operations_total.labels(
                    operation=operation,
                    backend=backend,
                    status=status
                ).inc()

                file_operation_duration_seconds.labels(
                    operation=operation,
                    backend=backend
                ).observe(duration)

        return wrapper
    return decorator

# Usage
@instrument_file_operation("read")
async def read_file(path: str, backend: Backend) -> bytes:
    return await backend.read(path)
```

#### Grafana Dashboard (Golden Signals)

**Dashboard 1: Request Overview**
- **Latency (p50, p95, p99)**: `histogram_quantile(0.99, rate(nexus_http_request_duration_seconds_bucket[5m]))`
- **Throughput**: `rate(nexus_http_requests_total[5m])`
- **Error Rate**: `rate(nexus_http_requests_total{status_code=~"5.."}[5m]) / rate(nexus_http_requests_total[5m])`
- **Saturation**: `nexus_lock_queue_depth`, `nexus_job_queue_depth{state="queued"}`

**Dashboard 2: File Operations**
- File ops by backend (S3 vs GDrive vs Workspace)
- Operation latency breakdown (read vs write vs delete)
- File size distribution

**Dashboard 3: Database & Infrastructure**
- Connection pool utilization
- Lock contention rate
- Vector search latency

#### Alerts (Prometheus AlertManager)

```yaml
# alerts.yaml
groups:
  - name: nexus_slos
    interval: 30s
    rules:
      # High error rate
      - alert: HighErrorRate
        expr: |
          rate(nexus_http_requests_total{status_code=~"5.."}[5m])
          / rate(nexus_http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High error rate: {{ $value | humanizePercentage }}"
          description: "Error rate above 5% for 5 minutes"

      # High p99 latency
      - alert: HighLatency
        expr: |
          histogram_quantile(0.99,
            rate(nexus_http_request_duration_seconds_bucket[5m])
          ) > 1.0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "p99 latency above 1s"

      # Lock contention
      - alert: LockContention
        expr: nexus_lock_queue_depth > 100
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "High lock contention: {{ $value }} requests waiting"

      # Job queue backlog
      - alert: JobBacklog
        expr: nexus_job_queue_depth{state="queued"} > 10000
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "Job queue backlog: {{ $value }} jobs queued"

      # Database connection exhaustion
      - alert: DBConnectionsExhausted
        expr: |
          nexus_db_connections_active
          / (nexus_db_connections_active + nexus_db_connections_idle) > 0.9
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "90% of database connections in use"
```

---

### 2. Distributed Tracing (OpenTelemetry + Jaeger)

#### Instrumentation

```python
from opentelemetry import trace
from opentelemetry.exporter.jaeger import JaegerExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

# Configure OpenTelemetry
jaeger_exporter = JaegerExporter(
    agent_host_name="jaeger",
    agent_port=6831,
)

provider = TracerProvider()
processor = BatchSpanProcessor(jaeger_exporter)
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

# Auto-instrument FastAPI
FastAPIInstrumentor.instrument_app(app)

# Auto-instrument external clients
HTTPXClientInstrumentor().instrument()  # S3, GDrive API calls
RedisInstrumentor().instrument()        # Lock manager
SQLAlchemyInstrumentor().instrument()   # Database queries

# Manual instrumentation for custom spans
async def read_file_with_tracing(path: str, tenant_id: UUID):
    with tracer.start_as_current_span("read_file") as span:
        span.set_attribute("file.path", path)
        span.set_attribute("tenant.id", str(tenant_id))

        # 1. Resolve backend
        with tracer.start_as_current_span("resolve_backend"):
            backend = await path_router.resolve(path, tenant_id)
            span.set_attribute("backend.type", backend.backend_type)

        # 2. Check permissions
        with tracer.start_as_current_span("check_acl"):
            await acl_manager.check_permission(tenant_id, path, "read")

        # 3. Read from backend
        with tracer.start_as_current_span("backend_read"):
            content = await backend.read(path)
            span.set_attribute("file.size", len(content))

        return content
```

**Trace Example in Jaeger**:
```
read_file (500ms)
├── resolve_backend (5ms)
├── check_acl (10ms)
│   └── postgres:query (8ms)
└── backend_read (480ms)
    ├── s3:get_object (450ms)
    └── compute_etag (25ms)
```

**Key Insights from Traces**:
- **Slow S3 calls**: Identify cold storage retrieval
- **N+1 queries**: Detect missing batch loading
- **Lock contention**: See which requests wait for locks

---

### 3. Structured Logging (JSON + Grafana Loki)

#### Log Format

```python
import structlog
import logging

# Configure structlog for JSON output
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Example log entries
@app.get("/api/v1/files/{path:path}")
async def read_file(path: str, auth: AuthContext = Depends(get_auth_context)):
    logger.info(
        "file_read_started",
        path=path,
        tenant_id=str(auth.tenant_id),
        agent_id=str(auth.agent_id),
        request_id=auth.request_id
    )

    try:
        content = await file_manager.read(path, auth.tenant_id)

        logger.info(
            "file_read_completed",
            path=path,
            tenant_id=str(auth.tenant_id),
            size=len(content),
            duration_ms=duration * 1000
        )

        return content

    except FileNotFoundError:
        logger.warning(
            "file_not_found",
            path=path,
            tenant_id=str(auth.tenant_id)
        )
        raise HTTPException(404, "File not found")

    except Exception as e:
        logger.error(
            "file_read_failed",
            path=path,
            tenant_id=str(auth.tenant_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True  # Include stack trace
        )
        raise
```

**JSON Output**:
```json
{
  "event": "file_read_completed",
  "path": "/workspace/data.txt",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent_id": "660e8400-e29b-41d4-a716-446655440001",
  "size": 1024,
  "duration_ms": 45.2,
  "timestamp": "2025-10-11T10:30:45.123Z",
  "level": "info",
  "logger": "nexus.api"
}
```

#### Log Aggregation (Grafana Loki)

**docker-compose.yaml**:
```yaml
services:
  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"
    volumes:
      - ./loki-config.yaml:/etc/loki/config.yaml
      - loki_data:/loki

  promtail:
    image: grafana/promtail:latest
    volumes:
      - /var/log:/var/log
      - ./promtail-config.yaml:/etc/promtail/config.yaml
    command: -config.file=/etc/promtail/config.yaml

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - grafana_data:/var/lib/grafana
```

**Common Log Queries (Loki)**:
```logql
# All errors for a tenant
{job="nexus"} |= "error" | json | tenant_id="550e8400-e29b-41d4-a716-446655440000"

# Slow file operations (>1s)
{job="nexus"} | json | duration_ms > 1000 | line_format "{{.path}} took {{.duration_ms}}ms"

# Lock contention events
{job="nexus"} |= "lock_acquisition_failed"

# OAuth token refresh failures
{job="nexus"} |= "token_refresh_failed" | json | table tenant_id, backend, error
```

---

### Deployment Configuration

**docker-compose.yaml** (with observability):
```yaml
version: '3.8'

services:
  nexus-api:
    image: nexus:5.0
    environment:
      PROMETHEUS_PORT: 9090
      JAEGER_AGENT_HOST: jaeger
      JAEGER_AGENT_PORT: 6831
      LOG_LEVEL: info
      LOG_FORMAT: json

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=30d'

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
      - ./grafana/datasources:/etc/grafana/provisioning/datasources

  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"  # Jaeger UI
      - "6831:6831/udp"  # Agent port
    environment:
      COLLECTOR_ZIPKIN_HOST_PORT: :9411

  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"
    volumes:
      - loki_data:/loki

volumes:
  prometheus_data:
  grafana_data:
  loki_data:
```

---

### SLOs & SLIs

| **SLI** | **SLO Target** | **Measurement** |
|---------|----------------|-----------------|
| **Availability** | 99.9% (43min downtime/month) | `sum(rate(nexus_http_requests_total{status_code!~"5.."}[30d])) / sum(rate(nexus_http_requests_total[30d]))` |
| **Read Latency (p99)** | <100ms | `histogram_quantile(0.99, rate(nexus_file_operation_duration_seconds_bucket{operation="read"}[5m]))` |
| **Write Latency (p99)** | <500ms | `histogram_quantile(0.99, rate(nexus_file_operation_duration_seconds_bucket{operation="write"}[5m]))` |
| **Error Rate** | <1% | `rate(nexus_http_requests_total{status_code=~"5.."}[5m]) / rate(nexus_http_requests_total[5m])` |
| **Job Success Rate** | >99% | `sum(nexus_job_processing_duration_seconds_count{status="success"}) / sum(nexus_job_processing_duration_seconds_count)` |

---

## Deployment Architecture: Three Modes, One Codebase

### Strategic Overview

Nexus supports **three deployment modes** from a single codebase, following the proven "Grafana Loki" pattern:

1. **Embedded/Library Mode** - Zero-deployment for developers
2. **Monolithic Server Mode** - Single-process for small teams
3. **Distributed Mode** - Full architecture for enterprises

**Adoption Funnel**:
```
Developer (Library)  →  Team (Monolithic)  →  Enterprise (Distributed)
    60 seconds              10 minutes              Production scale
```

### Mode 1: Embedded/Library Mode

**Target**: Individual developers, CLI tools, desktop apps, Jupyter notebooks

**Architecture**:
```go
import "github.com/ebogdum/nexus"

// Use Nexus like any Go library - no server needed
nx := nexus.NewEmbedded(&nexus.EmbeddedConfig{
    DataDir: "./nexus-data",
    CacheSize: 100 * 1024 * 1024, // 100MB
})

// All features work locally
file, _ := nx.ReadFile("/workspace/data.csv")
results, _ := nx.SemanticSearch("/docs/handbook.pdf", "remote work")
answer, _ := nx.LLMRead("/reports/q4.pdf", "summarize", nexus.WithClaude())
```

**Tech Stack**:
- **Database**: SQLite (single file, ~10GB capacity)
- **Cache**: In-memory LRU (no Redis)
- **Vector DB**: Qdrant embedded mode
- **Locks**: File-based (flock)
- **Backends**: LocalFS only

**Use Case Example** (Data Scientist):
```python
import nexus

# Analyze research papers locally
nx = nexus.Embedded("./research-data")
nx.index_directory("./papers", recursive=True)

# Ask questions across all papers (90% cache savings)
findings = nx.llm_read(
    "/papers/**/*.pdf",
    "What are common findings about LLM hallucinations?"
)
```

**Benefits**:
- ✅ Zero deployment complexity
- ✅ No infrastructure costs
- ✅ Works offline (except LLM API calls)
- ✅ Perfect for development/testing
- ✅ Fast (no network overhead)

**Limitations**:
- ❌ Single user only
- ❌ No multi-tenancy
- ❌ No cloud backends (S3, GDrive)
- ❌ Limited to ~10GB data

---

### Mode 2: Monolithic Server Mode

**Target**: Small teams (2-20 users), startups, staging environments

**Architecture**:
```bash
# Single Docker container, all-in-one
docker run -d -p 8080:8080 \
  -v /data:/nexus/data \
  -e NEXUS_MODE=monolithic \
  nexus/nexus:latest server

# Or via config
nexus server --mode=monolithic --config=config.yaml
```

**Tech Stack**:
- **Database**: SQLite or Postgres (configurable)
- **Cache**: In-memory or Redis (optional)
- **Vector DB**: Qdrant embedded or separate
- **API**: REST + gRPC
- **Auth**: JWT + API keys
- **Backends**: LocalFS + S3 + GDrive

**Docker Compose Example**:
```yaml
version: '3.8'
services:
  nexus:
    image: nexus/nexus:latest
    command: server --mode=monolithic
    ports:
      - "8080:8080"
    volumes:
      - ./nexus-data:/data
    environment:
      NEXUS_MODE: monolithic
      NEXUS_DB_PATH: /data/nexus.db
      NEXUS_S3_BUCKET: my-company-docs
```

**Capacity**:
- **Users**: 1-20 concurrent
- **Requests**: Up to 10K/day
- **Data**: Up to 100GB
- **Uptime**: 99% (single instance)

**Benefits**:
- ✅ Simple deployment (one container)
- ✅ Multi-user support
- ✅ Basic multi-tenancy
- ✅ External backends (S3, GDrive)
- ✅ Easy backups (single data directory)

**Limitations**:
- ❌ Single point of failure
- ❌ Vertical scaling only
- ❌ No geographic distribution

---

### Mode 3: Distributed Mode

**Target**: Enterprises (100+ users), SaaS platforms, production at scale

**Architecture**:
```
┌──────────────────────────────────────────┐
│  Load Balancer (HAProxy/Nginx)           │
└─────────────┬────────────────────────────┘
              │
     ┌────────┴────────┐
     │                 │
┌────▼─────┐    ┌─────▼────┐
│  Nexus   │    │  Nexus   │  (Stateless API Servers)
│  API #1  │    │  API #2  │  (Auto-scaling)
└────┬─────┘    └─────┬────┘
     │                │
     └────────┬───────┘
              │
     ┌────────▼─────────────┐
     │                      │
┌────▼──────┐     ┌────────▼────┐
│ Postgres  │     │   Redis     │
│ Primary   │     │   Cluster   │
│ + Replica │     │             │
└───────────┘     └─────┬───────┘
                        │
                  ┌─────▼──────┐
                  │  Qdrant    │
                  │  Cluster   │
                  └────────────┘
```

**Kubernetes Deployment**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexus-api
spec:
  replicas: 5  # Auto-scaling
  template:
    spec:
      containers:
      - name: nexus
        image: nexus/nexus:latest
        args: ["server", "--mode=distributed"]
        env:
        - name: POSTGRES_URL
          valueFrom:
            secretKeyRef:
              name: nexus-db
              key: url
        - name: REDIS_URL
          value: "redis://redis-cluster:6379"
        - name: QDRANT_URL
          value: "http://qdrant:6333"
```

**Helm Chart**:
```bash
helm install nexus nexus/nexus-distributed \
  --set postgres.enabled=true \
  --set redis.enabled=true \
  --set qdrant.enabled=true \
  --set replicas=3
```

**Tech Stack**:
- **Database**: Postgres with replication
- **Cache**: Redis Cluster
- **Vector DB**: Qdrant cluster
- **Locks**: Redis distributed locks with fencing tokens
- **API**: Load-balanced REST + gRPC
- **Observability**: Prometheus + Grafana

**Capacity**:
- **Users**: Unlimited
- **Requests**: Millions/day
- **Data**: Petabyte-scale
- **Uptime**: 99.99% (multi-region)

**Benefits**:
- ✅ Horizontal scaling
- ✅ High availability
- ✅ Multi-region deployment
- ✅ Enterprise multi-tenancy
- ✅ Production observability

---

### Implementation: Pluggable Components

**Single Codebase with Runtime Mode Selection**:

```go
// Core abstraction - mode-agnostic interface
type MetadataStore interface {
    Get(ctx context.Context, path string) (*Metadata, error)
    Create(ctx context.Context, md *Metadata) error
    Update(ctx context.Context, md *Metadata) error
    Delete(ctx context.Context, path string) error
    ListChildren(ctx context.Context, path string) ([]*Metadata, error)
}

// SQLite implementation (embedded + monolithic)
type SQLiteMetadataStore struct {
    db *sql.DB
}

// Postgres implementation (distributed)
type PostgresMetadataStore struct {
    pool *pgxpool.Pool
}

// Factory pattern selects based on mode
func NewMetadataStore(config Config) MetadataStore {
    switch config.Mode {
    case Embedded, Monolithic:
        return NewSQLiteMetadataStore(config.SQLitePath)
    case Distributed:
        return NewPostgresMetadataStore(config.PostgresURL)
    }
}
```

**Pluggable Components**:

| Component | Embedded | Monolithic | Distributed |
|-----------|----------|------------|-------------|
| **Database** | SQLite | SQLite/Postgres | Postgres Cluster |
| **Cache** | In-memory LRU | Memory/Redis | Redis Cluster |
| **Vector Store** | Qdrant embedded | Qdrant embedded/cloud | Qdrant cluster |
| **Locks** | File-based (flock) | File/Redis | Redis distributed |
| **Backends** | LocalFS only | LocalFS + S3/GDrive | All backends |

**Unified Configuration**:

```yaml
nexus:
  mode: monolithic  # embedded | monolithic | distributed

  storage:
    type: sqlite  # sqlite | postgres
    sqlite:
      path: ./nexus.db
    postgres:
      url: postgres://user:pass@localhost/nexus

  cache:
    type: memory  # memory | redis
    memory:
      size_mb: 100
    redis:
      url: redis://localhost:6379

  vector:
    type: qdrant-embedded  # qdrant-embedded | qdrant-cloud
    qdrant_embedded:
      path: ./qdrant-data
    qdrant_cloud:
      url: http://qdrant:6333

  locks:
    type: file  # file | redis
    file:
      dir: ./locks
    redis:
      url: redis://localhost:6379
```

---

### Migration Path: Seamless Growth

**Stage 1: Developer Discovery (Embedded)**
```go
// Try Nexus locally - 60 seconds to value
nx := nexus.NewEmbedded(nil)
result, _ := nx.LLMRead("/docs/api.pdf", "how to authenticate?")
```

**Stage 2: Team Adoption (Monolithic)**
```bash
# One-command team deployment
docker run -d -p 8080:8080 nexus/nexus:latest server --mode=monolithic

# Export data from embedded
nexus export --from=./nexus.db --to=team-server.db
```

**Stage 3: Enterprise Scale (Distributed)**
```bash
# Production deployment with Kubernetes
helm install nexus nexus/distributed --set replicas=5

# Migrate from monolithic
nexus migrate \
  --from-sqlite=./monolithic.db \
  --to-postgres=postgres://prod-db/nexus
```

---

### Decision Matrix: Which Mode to Use?

| Criteria | Embedded | Monolithic | Distributed |
|----------|----------|------------|-------------|
| **Users** | 1 | 1-20 | 20+ |
| **Requests/day** | < 1K | < 10K | Unlimited |
| **Data size** | < 10GB | < 100GB | Unlimited |
| **Uptime SLA** | N/A | 99% | 99.99% |
| **Budget** | $0 | $50-200/mo | $500+/mo |
| **Complexity** | Very low | Low | Medium-High |
| **Setup time** | 1 min | 10 min | 1-2 hours |
| **Backends** | LocalFS | LocalFS + S3/GDrive | All |
| **Multi-tenancy** | No | Basic | Full |

---

### Competitive Advantage: Library Mode

**Why Library Mode is Strategic**:

1. **Lowest Barrier to Entry**: `import nexus` vs deploying infrastructure
2. **Developer Love**: Works like SQLite, DuckDB (proven pattern)
3. **Trojan Horse for Enterprise**: Individual → Team → Enterprise
4. **Unique in Market**: No other distributed FS offers embedded mode

**Market Positioning**:

| | Nexus (Embedded) | Alluxio | Spacedrive |
|---|------------------|---------|------------|
| **Import as library** | ✅ Yes | ❌ No | ❌ No |
| **Zero infrastructure** | ✅ Yes | ❌ No | ❌ No |
| **LLM integration** | ✅ Yes | ❌ No | ❌ No |
| **Works offline** | ✅ Yes | ❌ No | ⚠️  Partial |
| **Multi-language** | ✅ Go, Python, JS | ❌ Java only | ❌ Rust only |

**Marketing Messages**:
- **Developers**: "Try Nexus in 60 seconds - no server needed"
- **Teams**: "Deploy Nexus in one Docker command"
- **Enterprises**: "Scale to millions of requests with Kubernetes"

---

### Code Organization

```
nexus/
├── cmd/
│   └── nexus/              # Single CLI binary
│       └── main.go         # Mode selection logic
│
├── pkg/
│   ├── core/               # Core engine (mode-agnostic)
│   │   ├── engine.go
│   │   └── semantic.go
│   │
│   ├── embedded/           # Embedded mode API
│   │   ├── embedded.go
│   │   └── config.go
│   │
│   ├── server/             # Server modes (monolithic + distributed)
│   │   ├── handlers/
│   │   └── router.go
│   │
│   ├── storage/            # Pluggable storage
│   │   ├── interface.go
│   │   ├── sqlite.go
│   │   └── postgres.go
│   │
│   ├── cache/              # Pluggable cache
│   │   ├── interface.go
│   │   ├── memory.go
│   │   └── redis.go
│   │
│   ├── locks/              # Pluggable locks
│   │   ├── interface.go
│   │   ├── file.go
│   │   └── redis.go
│   │
│   └── vector/             # Pluggable vector store
│       ├── interface.go
│       ├── embedded.go
│       └── cloud.go
```

---

### Implementation Roadmap

**Phase 1: Core Abstractions (Weeks 1-2)**
- [ ] Define storage interface (SQLite + Postgres)
- [ ] Define cache interface (Memory + Redis)
- [ ] Define lock interface (File + Redis)
- [ ] Mode selection logic

**Phase 2: Embedded Mode (Weeks 3-4)**
- [ ] Embedded API design
- [ ] SQLite implementation
- [ ] In-memory cache
- [ ] Python/JS bindings (via FFI/WASM)

**Phase 3: Unified CLI (Week 5)**
- [ ] `nexus run --mode=embedded`
- [ ] `nexus server --mode=monolithic`
- [ ] `nexus server --mode=distributed`

**Phase 4: Migration Tools (Week 6)**
- [ ] SQLite → Postgres migration
- [ ] Data export/import tools
- [ ] Backup/restore utilities

---

## Python SDK Interface

### Client Interface

```python
from typing import Optional, List, Dict, AsyncIterator
from pydantic import BaseModel
from datetime import datetime

class FileInfo(BaseModel):
    """File metadata"""
    path: str
    size: int
    nexus_etag: str
    storage_etag: Optional[str]
    mime_type: Optional[str]
    is_directory: bool
    modified_at: Optional[datetime]
    version: int

class SearchResult(BaseModel):
    """Semantic search result"""
    path: str
    score: float
    snippet: str
    position: int
    byte_range: Optional[Dict[str, int]]

class MountInfo(BaseModel):
    """Mount information"""
    mount_point: str
    backend_type: str
    readonly: bool
    status: str

class NexusClient:
    """
    Async-first Nexus client.

    Example:
        async with NexusClient(api_key="nexus_...") as client:
            await client.write("/workspace/file.txt", b"content")
            data = await client.read("/workspace/file.txt")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://nexus.example.com",
        user_credential: Optional[Dict] = None
    ):
        """
        Args:
            api_key: Nexus API key (nexus_{tenant}_{agent}_{secret})
            base_url: Nexus server URL
            user_credential: Optional OAuth credential for runtime access
        """
        pass

    # Context manager
    async def __aenter__(self) -> "NexusClient": ...
    async def __aexit__(self, *args): ...

    # File Operations (Raw Bytes)
    async def read(self, path: str) -> bytes:
        """Read entire file as raw bytes"""
        pass

    async def semantic_read(
        self,
        path: str,
        query: str,
        max_chunks: int = 3
    ) -> bytes:
        """
        Read file with semantic filtering.

        Searches file content semantically and returns only relevant chunks,
        concatenated together. Useful for extracting relevant information
        from large documents.

        Args:
            path: Virtual path
            query: Semantic query (e.g., "sections about pricing")
            max_chunks: Maximum number of relevant chunks to return

        Returns:
            Concatenated content of relevant chunks

        Example:
            # Read only budget-related sections from a large report
            content = await client.semantic_read(
                "/workspace/annual-report.pdf",
                "budget and financial projections",
                max_chunks=5
            )
        """
        pass

    # Typed File Reading (Structured Data)
    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """
        Read file as text string.

        Args:
            path: Virtual path
            encoding: Character encoding (default: utf-8)

        Returns:
            Decoded text content

        Example:
            text = await client.read_text("/workspace/config.txt")
        """
        pass

    async def read_json(self, path: str) -> Dict | List:
        """
        Read and parse JSON file.

        Returns:
            Parsed JSON object (dict or list)

        Raises:
            JSONDecodeError: If file is not valid JSON

        Example:
            config = await client.read_json("/workspace/config.json")
            print(config["database"]["host"])
        """
        pass

    async def read_csv(
        self,
        path: str,
        header: bool = True,
        delimiter: str = ",",
        max_rows: Optional[int] = None
    ) -> List[Dict]:
        """
        Read CSV file as list of dicts.

        Args:
            path: Virtual path
            header: First row is header (default: True)
            delimiter: Column delimiter (default: ",")
            max_rows: Limit rows returned (useful for large files)

        Returns:
            List of dicts (each dict is a row with column names as keys)

        Example:
            rows = await client.read_csv("/workspace/data.csv")
            for row in rows:
                print(row["name"], row["age"])

            # Read TSV file
            rows = await client.read_csv("/workspace/data.tsv", delimiter="\\t")

            # Read first 1000 rows only
            sample = await client.read_csv("/workspace/large.csv", max_rows=1000)
        """
        pass

    async def read_excel(
        self,
        path: str,
        sheet_name: Optional[str | int] = 0,
        header: bool = True,
        max_rows: Optional[int] = None
    ) -> List[Dict]:
        """
        Read Excel file (.xlsx, .xls) as list of dicts.

        Args:
            path: Virtual path
            sheet_name: Sheet name (str) or index (int, 0-based). Default: first sheet
            header: First row is header (default: True)
            max_rows: Limit rows returned

        Returns:
            List of dicts (each dict is a row)

        Example:
            # Read first sheet
            rows = await client.read_excel("/workspace/report.xlsx")

            # Read specific sheet by name
            rows = await client.read_excel("/workspace/report.xlsx", sheet_name="Summary")

            # Read second sheet
            rows = await client.read_excel("/workspace/report.xlsx", sheet_name=1)
        """
        pass

    async def read_parquet(
        self,
        path: str,
        columns: Optional[List[str]] = None,
        max_rows: Optional[int] = None
    ) -> List[Dict]:
        """
        Read Parquet file as list of dicts.

        Args:
            path: Virtual path
            columns: Subset of columns to read (default: all)
            max_rows: Limit rows returned

        Returns:
            List of dicts

        Example:
            # Read all columns
            rows = await client.read_parquet("/workspace/data.parquet")

            # Read specific columns only
            rows = await client.read_parquet(
                "/workspace/data.parquet",
                columns=["user_id", "timestamp", "event"]
            )
        """
        pass

    async def read_yaml(self, path: str) -> Dict:
        """
        Read and parse YAML file.

        Returns:
            Parsed YAML object (typically dict)

        Example:
            config = await client.read_yaml("/workspace/config.yaml")
            print(config["services"]["database"]["port"])
        """
        pass

    async def read_toml(self, path: str) -> Dict:
        """
        Read and parse TOML file.

        Returns:
            Parsed TOML object (dict)

        Example:
            config = await client.read_toml("/workspace/pyproject.toml")
            print(config["tool"]["poetry"]["dependencies"])
        """
        pass

    async def read_xml(self, path: str) -> Dict:
        """
        Read and parse XML file to dict.

        Returns:
            Parsed XML as nested dicts

        Example:
            data = await client.read_xml("/workspace/config.xml")
        """
        pass

    async def read_markdown(self, path: str) -> Dict:
        """
        Read and parse Markdown file.

        Returns:
            {
                "content": str,      # Raw markdown
                "html": str,         # Rendered HTML
                "metadata": Dict     # Frontmatter (if present)
            }

        Example:
            doc = await client.read_markdown("/workspace/README.md")
            print(doc["metadata"]["title"])
            print(doc["html"])
        """
        pass

    async def read_pdf(
        self,
        path: str,
        pages: Optional[List[int]] = None,
        extract_images: bool = False
    ) -> Dict:
        """
        Extract text from PDF file.

        Args:
            path: Virtual path
            pages: Page numbers to extract (1-indexed). Default: all pages
            extract_images: Extract images as base64 (default: False)

        Returns:
            {
                "text": str,                    # Full extracted text
                "pages": [                       # Per-page breakdown
                    {"page": 1, "text": "...", "images": [...]},
                    ...
                ],
                "metadata": {                    # PDF metadata
                    "title": str,
                    "author": str,
                    "pages": int
                }
            }

        Example:
            pdf = await client.read_pdf("/workspace/report.pdf")
            print(f"Total pages: {pdf['metadata']['pages']}")
            print(pdf["text"])

            # Extract specific pages
            pdf = await client.read_pdf("/workspace/report.pdf", pages=[1, 2, 3])
        """
        pass

    async def read_image(
        self,
        path: str,
        format: str = "base64"
    ) -> Dict:
        """
        Read image file with metadata.

        Args:
            path: Virtual path
            format: Return format ("base64" | "pil" | "numpy")

        Returns:
            {
                "data": str | PILImage | ndarray,  # Depends on format
                "width": int,
                "height": int,
                "format": str,                       # "PNG", "JPEG", etc.
                "mode": str,                         # "RGB", "RGBA", etc.
                "exif": Dict                         # EXIF metadata (if present)
            }

        Example:
            img = await client.read_image("/workspace/photo.jpg")
            print(f"Size: {img['width']}x{img['height']}")
            print(f"Base64: {img['data'][:50]}...")

            # For image processing libraries
            img = await client.read_image("/workspace/photo.jpg", format="numpy")
            array = img["data"]  # numpy.ndarray
        """
        pass

    # Typed File Writing (Structured Data)
    async def write_json(
        self,
        path: str,
        data: Dict | List,
        indent: Optional[int] = 2
    ) -> FileInfo:
        """
        Write dict/list as JSON file.

        Example:
            await client.write_json("/workspace/config.json", {
                "database": {"host": "localhost", "port": 5432}
            })
        """
        pass

    async def write_csv(
        self,
        path: str,
        rows: List[Dict],
        delimiter: str = ","
    ) -> FileInfo:
        """
        Write list of dicts as CSV file.

        Example:
            await client.write_csv("/workspace/data.csv", [
                {"name": "Alice", "age": 30},
                {"name": "Bob", "age": 25}
            ])
        """
        pass

    async def write_excel(
        self,
        path: str,
        data: Dict[str, List[Dict]]  # sheet_name -> rows
    ) -> FileInfo:
        """
        Write multi-sheet Excel file.

        Example:
            await client.write_excel("/workspace/report.xlsx", {
                "Summary": [{"metric": "revenue", "value": 1000000}],
                "Details": [{"item": "A", "qty": 10}, {"item": "B", "qty": 20}]
            })
        """
        pass

    async def write_parquet(
        self,
        path: str,
        rows: List[Dict],
        compression: str = "snappy"
    ) -> FileInfo:
        """
        Write list of dicts as Parquet file.

        Args:
            compression: "snappy" | "gzip" | "brotli" | "none"

        Example:
            await client.write_parquet("/workspace/data.parquet", rows)
        """
        pass

    async def write(
        self,
        path: str,
        content: bytes,
        if_match: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> FileInfo:
        """
        Write file with conflict detection.

        Args:
            path: Virtual path
            content: File content
            if_match: ETag for conflict detection (409 if mismatch)
            idempotency_key: Unique key for safe retries
        """
        pass

    async def delete(self, path: str) -> None:
        """Delete file"""
        pass

    async def stat(self, path: str) -> FileInfo:
        """Get file metadata"""
        pass

    async def list(
        self,
        path: str,
        recursive: bool = False,
        page_token: Optional[str] = None,
        page_size: int = 200
    ) -> Dict:
        """
        List directory with pagination.

        Returns:
            {
                "files": [FileInfo, ...],
                "next_page_token": str | None
            }
        """
        pass

    # File Discovery
    async def glob(
        self,
        pattern: str,
        path: Optional[str] = None
    ) -> List[FileInfo]:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "*.txt", "data/**/*.csv")
            path: Root path to search (defaults to workspace root)

        Returns:
            List of matching files

        Examples:
            # Find all Python files
            files = await client.glob("**/*.py")

            # Find all CSVs in data directory
            files = await client.glob("*.csv", path=client.workspace("data"))

            # Find all markdown files in current directory (non-recursive)
            files = await client.glob("*.md", path=client.workspace(""))
        """
        pass

    async def grep(
        self,
        pattern: str,
        path: Optional[str] = None,
        file_pattern: Optional[str] = None,
        context_lines: int = 0,
        case_sensitive: bool = True
    ) -> List[Dict]:
        """
        Search file contents using regex.

        Args:
            pattern: Regex pattern to search for
            path: Root path to search (defaults to workspace root)
            file_pattern: Glob pattern to filter files (e.g., "*.py")
            context_lines: Number of context lines before/after match
            case_sensitive: Whether search is case-sensitive

        Returns:
            List of matches:
            [
                {
                    "path": str,
                    "line_number": int,
                    "line": str,
                    "context_before": List[str],
                    "context_after": List[str]
                },
                ...
            ]

        Examples:
            # Find TODO comments in Python files
            matches = await client.grep(
                r"TODO:|FIXME:",
                file_pattern="*.py"
            )

            # Find function definitions with context
            matches = await client.grep(
                r"def\s+\w+",
                file_pattern="**/*.py",
                context_lines=2
            )

            # Case-insensitive search
            matches = await client.grep(
                "error",
                case_sensitive=False
            )
        """
        pass

    # Streaming (for large files)
    async def stream_read(self, path: str) -> AsyncIterator[bytes]:
        """Stream file download (zero-copy)"""
        pass

    async def stream_write(
        self,
        path: str,
        chunks: AsyncIterator[bytes],
        total_size: Optional[int] = None
    ) -> FileInfo:
        """Stream file upload (multipart for large files)"""
        pass

    # Semantic Search
    async def search_semantic(
        self,
        query: str,
        path_prefix: Optional[str] = None,
        document_type: Optional[str] = None,
        limit: int = 10
    ) -> List[SearchResult]:
        """
        Semantic search across files.

        Args:
            query: Natural language query
            path_prefix: Limit search to path prefix
            document_type: Filter by document type (pdf, docx, etc)
            limit: Max results
        """
        pass

    async def search_in_file(
        self,
        path: str,
        query: str,
        limit: int = 5
    ) -> List[SearchResult]:
        """
        Search within a specific file.

        Returns chunks with byte offsets and signed URLs.
        """
        pass

    # Lineage (for agent files)
    async def record_lineage(
        self,
        output_path: str,
        operation: str,
        input_paths: List[str],
        operation_params: Optional[Dict] = None,
        task_id: Optional[str] = None,
        recomputation_cost_seconds: float = 0.0
    ) -> None:
        """
        Record how file was created for recovery.

        Args:
            output_path: File that was created
            operation: Operation name (e.g., "summarize", "analyze")
            input_paths: Input files used
            operation_params: Parameters for recomputation
            task_id: Agent task identifier
            recomputation_cost_seconds: Estimated recomputation time
        """
        pass

    # Mount Management
    async def create_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: Dict,
        readonly: bool = False
    ) -> MountInfo:
        """
        Dynamically mount a backend.

        Example:
            await client.create_mount(
                "/my-gdrive",
                "gdrive",
                {"credentials": oauth_token}
            )
        """
        pass

    async def list_mounts(self) -> List[MountInfo]:
        """List all mounts for this tenant"""
        pass

    async def unmount(self, mount_point: str) -> None:
        """Unmount backend"""
        pass

    # Agent Memory
    async def add_memory(
        self,
        title: str,
        description: str,
        content: str,
        task_type: Optional[str] = None,
        success: bool = True
    ) -> str:
        """
        Add memory to agent's memory bank.

        Returns:
            memory_id
        """
        pass

    async def search_memories(
        self,
        query: str,
        task_type: Optional[str] = None,
        limit: int = 5
    ) -> List[Dict]:
        """Search agent's memory bank"""
        pass

    # Jobs
    async def create_job(
        self,
        job_type: str,
        config: Dict,
        priority: int = 50
    ) -> str:
        """
        Create background job.

        Returns:
            job_id
        """
        pass

    async def get_job_status(self, job_id: str) -> Dict:
        """
        Get job status and progress.

        Returns:
            {
                "state": "queued" | "started" | "succeeded" | "failed",
                "progress": int,
                "total": int,
                "error_message": str | None
            }
        """
        pass

    # Helper: Path construction
    def workspace(self, relative_path: str) -> str:
        """
        Construct workspace path.

        Example:
            client.workspace("logs/debug.txt")
            → "/workspace/{tenant_id}/{agent_id}/logs/debug.txt"
        """
        pass
```

### Usage Examples

#### Basic File Operations

```python
import asyncio
from nexus import NexusClient

async def main():
    async with NexusClient(api_key="nexus_abc_def_xyz") as client:
        # Write file
        await client.write(
            client.workspace("output/report.txt"),
            b"Analysis complete"
        )

        # Read file
        data = await client.read(client.workspace("output/report.txt"))
        print(data.decode())

        # Semantic read (only relevant parts)
        relevant_content = await client.semantic_read(
            client.workspace("documents/long-report.pdf"),
            "executive summary and key findings",
            max_chunks=3
        )
        print(relevant_content.decode())

        # List directory
        result = await client.list(client.workspace("output/"))
        for file in result["files"]:
            print(f"{file.path}: {file.size} bytes")

asyncio.run(main())
```

#### File Discovery (Glob & Grep)

```python
async def discovery_example():
    async with NexusClient(api_key="...") as client:
        # Find all Python files
        py_files = await client.glob("**/*.py")
        print(f"Found {len(py_files)} Python files")

        # Find all CSVs in data directory
        csv_files = await client.glob(
            "*.csv",
            path=client.workspace("data")
        )

        # Find TODO comments
        todos = await client.grep(
            r"TODO:|FIXME:",
            file_pattern="**/*.py"
        )

        for match in todos:
            print(f"{match['path']}:{match['line_number']}")
            print(f"  {match['line']}")

        # Find function definitions with context
        functions = await client.grep(
            r"async def\s+(\w+)",
            file_pattern="**/*.py",
            context_lines=2
        )

        for match in functions:
            print(f"\n{match['path']}:{match['line_number']}")
            # Show context before
            for line in match["context_before"]:
                print(f"  {line}")
            # Show match
            print(f"> {match['line']}")
            # Show context after
            for line in match["context_after"]:
                print(f"  {line}")
```

#### Semantic Search

```python
async def search_example():
    async with NexusClient(api_key="...") as client:
        # Search across all files
        results = await client.search_semantic(
            "budget optimization techniques",
            path_prefix=client.workspace("")
        )

        for result in results:
            print(f"{result.path}: {result.score:.2f}")
            print(f"  {result.snippet}")

        # Search within specific file
        file_results = await client.search_in_file(
            client.workspace("documents/report.pdf"),
            "quarterly revenue"
        )

        for result in file_results:
            print(f"Position {result.position}: {result.snippet}")
            if result.byte_range:
                print(f"  Bytes {result.byte_range['start']}-{result.byte_range['end']}")
```

#### Streaming Large Files

```python
async def streaming_example():
    async with NexusClient(api_key="...") as client:
        # Stream upload
        async def file_chunks():
            async with aiofiles.open("large_file.bin", "rb") as f:
                while chunk := await f.read(64 * 1024):  # 64KB chunks
                    yield chunk

        await client.stream_write(
            client.workspace("uploads/large_file.bin"),
            file_chunks(),
            total_size=1024 * 1024 * 1024  # 1GB
        )

        # Stream download
        async with aiofiles.open("downloaded.bin", "wb") as f:
            async for chunk in client.stream_read(client.workspace("uploads/large_file.bin")):
                await f.write(chunk)
```

#### Recording Lineage

```python
async def lineage_example():
    async with NexusClient(api_key="...") as client:
        # Process files
        input_data = await client.read(client.workspace("data/raw.csv"))
        summary = await summarize(input_data)  # Your processing function

        # Write output
        output_path = client.workspace("output/summary.txt")
        await client.write(output_path, summary.encode())

        # Record lineage (enables recovery)
        await client.record_lineage(
            output_path=output_path,
            operation="summarize",
            input_paths=[client.workspace("data/raw.csv")],
            operation_params={"max_length": 500, "model": "gpt-4"},
            recomputation_cost_seconds=30.0
        )
```

#### Agent Memory

```python
async def memory_example():
    async with NexusClient(api_key="...") as client:
        # Record successful task
        memory_id = await client.add_memory(
            title="Database query optimization",
            description="Learned to add index on user_id column",
            content="Adding B-tree index on frequently queried user_id column reduced query time from 2s to 50ms",
            task_type="database_optimization",
            success=True
        )

        # Search memories
        memories = await client.search_memories(
            "how to optimize database queries",
            task_type="database_optimization"
        )

        for memory in memories:
            print(f"{memory['title']}: {memory['score']:.2f}")
            print(f"  {memory['description']}")
```

#### Runtime Credentials (Per-User Access)

```python
async def runtime_creds_example():
    # User's OAuth token
    user_oauth = {
        "provider": "google",
        "access_token": "ya29.xxx",
        "refresh_token": "1//xxx",
        "expires_at": 1234567890
    }

    async with NexusClient(
        api_key="nexus_...",
        user_credential=user_oauth
    ) as client:
        # Access user's Google Drive (via their token)
        files = await client.list("/gdrive/My Documents/")

        # Read from user's drive
        data = await client.read("/gdrive/My Documents/report.pdf")

        # ACL still enforced (even with user's token)
```

#### Mount Management

```python
async def mount_example():
    async with NexusClient(api_key="...") as client:
        # Mount Google Drive
        await client.create_mount(
            mount_point="/my-gdrive",
            backend_type="gdrive",
            backend_config={
                "credentials": oauth_token,
                "root_folder_id": "abc123"
            }
        )

        # List mounts
        mounts = await client.list_mounts()
        for mount in mounts:
            print(f"{mount.mount_point}: {mount.backend_type} ({mount.status})")

        # Access mounted files
        files = await client.list("/my-gdrive/Documents/")
```

#### Job Management

```python
async def job_example():
    async with NexusClient(api_key="...") as client:
        # Create background job
        job_id = await client.create_job(
            job_type="semantic_index",
            config={
                "path_prefix": client.workspace("documents/"),
                "recursive": True
            },
            priority=80
        )

        # Poll job status
        while True:
            status = await client.get_job_status(job_id)
            print(f"State: {status['state']}, Progress: {status['progress']}/{status['total']}")

            if status["state"] in ("succeeded", "failed"):
                break

            await asyncio.sleep(2)
```

---

## Summary

### What's New in Nexus 5.0

**1. Agent Intelligence** (+ ReasoningBank)
- Self-evolving agent memory
- Memory consolidation and pruning
- Multi-agent memory sharing
- Memory effectiveness analytics

**2. Advanced Content Processing** (+ Supermemory)
- Multi-stage document pipeline
- Semantic chunking for better search
- Document type detection
- Processing state tracking

**3. Production Operations** (+ Timelinize)
- Resumable job system with checkpointing
- Auto-refreshing OAuth tokens
- Backend recognition and auto-mounting
- CPU throttling

**4. Developer Experience** (+ Supermemory)
- Model Context Protocol (MCP) server
- Claude Desktop / Cursor integration
- Direct file access from AI tools

### Expected Capabilities

| Capability | Performance | Comparison |
|------------|-------------|------------|
| **Write Speed** | 500-1000 MB/s | 10-50× vs direct |
| **Read Latency** | <10ms | 10-50× vs remote |
| **Memory Search** | <100ms | Sub-second agent recall |
| **Storage Savings** | 30-50% | Via CAS dedup |
| **Job Resume** | 100% | Survives restarts |
| **Memory Evolution** | Automatic | Self-improving |
| **Tool Integration** | Native | Claude/Cursor MCP |

### Implementation Status

✅ **Complete Design** - All components specified
✅ **Database Schema** - Full schema with indexes
✅ **API Specification** - Complete REST + MCP APIs
✅ **Interface Contracts** - All abstract interfaces defined
✅ **Integration Patterns** - Clear integration points

**Ready to implement!** 🚀

---

**End of Nexus Comprehensive Architecture v5.0**

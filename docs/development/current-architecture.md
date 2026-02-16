# Nexus Current Architecture (Phase 1 Documentation)

**Date:** 2026-01-02
**Status:** As-Is Documentation (Pre-Refactoring)
**Related:** Issue #987 (Phase 1), Issue #986 (Architecture Analysis)

---

## Executive Summary

This document describes the **current** Nexus architecture as it exists today, including known issues and technical debt. This is not aspirational documentation - it reflects reality for accurate refactoring planning.

**Key Statistics:**
- **254 Python source files**
- **189 TYPE_CHECKING guards** (circular dependency indicators)
- **531 type: ignore comments** (type safety issues)
- **Largest file:** 6,167 lines (nexus_fs.py)
- **Test count:** 4,118 tests

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Nexus Platform                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │   Embedded   │  │  Monolithic  │  │ Distributed  │     │
│  │     Mode     │  │     Mode     │  │     Mode     │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
           │                    │                    │
           ▼                    ▼                    ▼
    ┌──────────┐         ┌──────────┐        ┌──────────┐
    │  Direct  │         │  FastAPI │        │  Remote  │
    │   API    │         │  Server  │        │  Client  │
    └──────────┘         └──────────┘        └──────────┘
           │                    │                    │
           └────────────────────┴────────────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │   NexusFS     │
                      │  (God Object) │
                      │  6,167 lines  │
                      └───────────────┘
```

---

## Core Components

### 1. NexusFS (Core God Object)

**File:** `src/nexus/core/nexus_fs.py` (6,167 lines)

**Problem:** Massive monolith using 9 mixins totaling 12,539 lines of code.

**Structure:**
```python
class NexusFS(  # type: ignore[misc]  # Even mypy can't handle this
    NexusFSCoreMixin,         # 2,807 lines - CRUD operations
    NexusFSSearchMixin,       # 2,175 lines - Semantic search
    NexusFSReBACMixin,        # 2,554 lines - Permissions
    NexusFSVersionsMixin,     # 300 lines - Version control
    NexusFSMountsMixin,       # 2,048 lines - Mount management
    NexusFSOAuthMixin,        # 1,116 lines - OAuth integration
    NexusFSSkillsMixin,       # 874 lines - Skills system
    NexusFSMCPMixin,          # 379 lines - MCP integration
    NexusFSLLMMixin,          # 286 lines - LLM interactions
    NexusFilesystem           # Abstract base
):
```

**Responsibilities (Too Many):**
- File CRUD operations
- Directory management
- Permission checking (ReBAC)
- Semantic search
- Version control
- Mount management
- OAuth token management
- Skills lifecycle
- MCP server integration
- LLM interactions
- Cache management
- Backend delegation
- Metadata management

**Issues:**
- ❌ Violates Single Responsibility Principle
- ❌ Impossible to test in isolation
- ❌ Tight coupling between unrelated features
- ❌ Diamond inheritance problems
- ❌ Method resolution order complexity

**Phase 2 Goal:** Split into 8+ independent services using composition.

---

### 2. Permission System (Multiple Implementations)

**Three ReBAC Managers:**

| File | Size | Status | Issue |
|------|------|--------|-------|
| `rebac_manager.py` | 180KB (4,400 lines) | Production | Original |
| `rebac_manager_enhanced.py` | 183KB (4,500 lines) | Beta | Enhanced version |
| `rebac_manager_tenant_aware.py` | ~100KB | Experimental | Tenant isolation |

**Problem:** No clear migration path, overlapping features, massive duplication.

**Phase 2 Goal:** Consolidate to single implementation.

---

### 3. Storage Layer

```
Storage Layer
├── Backends
│   ├── LocalBackend (filesystem)
│   ├── S3Backend (AWS S3)
│   ├── GCSBackend (Google Cloud Storage)
│   ├── GDriveBackend (Google Drive)
│   └── Custom backends via registry
├── Metadata Store
│   ├── SQLAlchemy ORM
│   ├── PostgreSQL (production)
│   └── SQLite (development)
└── Content Cache
    ├── L1: In-memory (LRU)
    ├── L2: Redis/Dragonfly
    └── Tiger Cache (custom)
```

**Files:**
- `src/nexus/storage/sqlalchemy_metadata_store.py` (114KB, 2,800 lines) ⚠️
- `src/nexus/storage/models.py` (128KB, 3,200 lines) ⚠️
- `src/nexus/backends/local.py`
- `src/nexus/backends/s3.py`
- `src/nexus/backends/gcs.py`

**Issues:**
- Multiple cache layers with inconsistent interfaces
- Complex invalidation logic
- N+1 query patterns in metadata operations

---

### 4. Search System

**Components:**
- Semantic search (embeddings)
- Hybrid search (semantic + keyword)
- BM25S for ranked text search (500x faster)
- Result reranking

**Files:**
- `src/nexus/core/nexus_fs_search.py` (2,175 lines) ⚠️
- `src/nexus/search/` (multiple files)

**Integration:**
- PostgreSQL pgvector for embeddings
- SQLite-vec for lightweight embedding search
- BM25S for full-text search

**Phase 2 Goal:** Extract to SearchService.

---

### 5. Authentication & Authorization

**OAuth Providers:**
- Google
- Microsoft
- X (Twitter)
- Generic OAuth

**Files (15+ in `src/nexus/server/auth/`):**
- `oauth_google.py`
- `oauth_microsoft.py`
- `oauth_x.py`
- `token_manager.py`
- `token_encryptor.py`
- `pending_oauth_states.py`
- ... and 9 more

**Problem:** Large attack surface, scattered logic.

**Phase 4 Goal:** Consolidate using Authlib.

---

### 6. Remote Client

**Files:**
- `src/nexus/remote/client.py` (199KB, ~5,000 lines) ⚠️
- `src/nexus/remote/async_client.py` (105KB, ~2,500 lines) ⚠️

**Problem:** Massive files with duplicated logic.

**Phase 4 Goal:** Split into modular client components.

---

## Circular Dependencies

**Found:** 189 TYPE_CHECKING guards across codebase

**Common Patterns:**

### Pattern 1: Type Hint Circularity
```python
# File A
if TYPE_CHECKING:
    from module_b import ClassB

def function() -> 'ClassB':
    pass

# File B
if TYPE_CHECKING:
    from module_a import ClassA

def other_function() -> 'ClassA':
    pass
```

### Pattern 2: Service Cross-References
```python
# search_service.py
if TYPE_CHECKING:
    from nexus.core.permissions import PermissionService

# permission_service.py
if TYPE_CHECKING:
    from nexus.core.search import SearchService
```

**Phase 4 Goal:** Eliminate circular dependencies using:
- Protocol types for interfaces
- Dependency injection
- Proper layering (core → services → API)

---

## Module Structure

```
src/nexus/
├── __init__.py
├── config.py                    # Configuration management
├── sync.py                      # Sync operations
│
├── core/                        # Core filesystem (⚠️ God Object here)
│   ├── nexus_fs.py             # Main class (6,167 lines) 🚨
│   ├── nexus_fs_core.py        # CRUD mixin (2,807 lines) ⚠️
│   ├── nexus_fs_search.py      # Search mixin (2,175 lines) ⚠️
│   ├── nexus_fs_rebac.py       # Permissions mixin (2,554 lines) ⚠️
│   ├── nexus_fs_mounts.py      # Mounts mixin (2,048 lines) ⚠️
│   ├── nexus_fs_oauth.py       # OAuth mixin (1,116 lines) ⚠️
│   ├── nexus_fs_skills.py      # Skills mixin (874 lines) ⚠️
│   ├── rebac_manager.py        # Permissions (4,400 lines) ⚠️
│   ├── rebac_manager_enhanced.py # Permissions v2 (4,500 lines) ⚠️
│   ├── metadata.py
│   ├── sessions.py
│   └── cache/                  # Caching subsystem
│
├── storage/                     # Storage backends & metadata
│   ├── metadata_store.py       # Metadata DB (2,800 lines) ⚠️
│   ├── models.py               # SQLAlchemy models (3,200 lines) ⚠️
│   ├── content_cache.py
│   └── embedding_store.py
│
├── backends/                    # Storage backend implementations
│   ├── local.py                # Local filesystem
│   ├── s3.py                   # AWS S3
│   ├── gcs.py                  # Google Cloud Storage
│   └── registry.py             # Backend registration
│
├── server/                      # FastAPI server
│   ├── fastapi_server.py
│   ├── rpc_server.py
│   └── auth/                   # Authentication (15+ files) ⚠️
│
├── remote/                      # Remote client
│   ├── client.py               # Sync client (5,000 lines) ⚠️
│   └── async_client.py         # Async client (2,500 lines) ⚠️
│
├── llm/                         # LLM integration
│   ├── provider.py
│   ├── context_builder.py
│   └── citation.py
│
├── parsers/                     # Document parsing
│   ├── markitdown_parser.py
│   └── providers/
│
├── tools/                       # Tool integrations
│   └── langgraph/              # LangGraph integration
│
├── skills/                      # Skills system
│   ├── manager.py
│   └── registry.py
│
├── mcp/                         # Model Context Protocol
│   └── server.py
│
└── cli/                         # Command-line interface
    └── main.py
```

---

## Data Flow

### Read Operation Flow
```
┌─────────┐
│ Client  │
└────┬────┘
     │ 1. read(path, context)
     ▼
┌─────────────┐
│  NexusFS    │
└────┬────────┘
     │ 2. Check permissions
     ▼
┌──────────────┐
│ PermissionSvc│ (embedded in NexusFS today)
└────┬─────────┘
     │ 3. Check ReBACManager
     ▼
┌──────────────┐
│ ReBACManager │
└────┬─────────┘
     │ 4. Query permissions
     ▼
┌──────────────┐
│ Metadata DB  │
└────┬─────────┘
     │ 5. Get file metadata
     ▼
┌──────────────┐
│ NexusFS      │
└────┬─────────┘
     │ 6. Check content cache
     ▼
┌──────────────┐
│ContentCache  │
└────┬─────────┘
     │ 7. Cache miss - read from backend
     ▼
┌──────────────┐
│ Backend      │ (Local, S3, GCS, etc.)
└────┬─────────┘
     │ 8. Return content
     ▼
┌─────────┐
│ Client  │
└─────────┘
```

### Write Operation Flow
```
Client → NexusFS → Permissions → Backend → MetadataStore → Cache Invalidation
```

---

## Deployment Modes

### 1. Embedded Mode
```python
from nexus import NexusFS

# Direct API - no server
fs = NexusFS(backend="local", db_path="nexus.db")
fs.write("/data/file.txt", b"content")
```

### 2. Monolithic Server Mode
```bash
nexus server --port 8080
```
- Single FastAPI server
- All features in one process
- Shared database connection

### 3. Distributed Mode
```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ Nexus Server │────▶│ PostgreSQL   │◀────│ Redis/       │
│  (FastAPI)   │     │   Database   │     │ Dragonfly    │
└──────────────┘     └──────────────┘     └──────────────┘
       │
       ▼
┌──────────────┐
│ MCP Server   │
│ (Port 3000)  │
└──────────────┘
       │
       ▼
┌──────────────┐
│ LangGraph    │
│    Agent     │
└──────────────┘
```

---

## Known Issues & Technical Debt

### Critical (Phase 1-2)
- ❌ **God Object:** NexusFS 6,167 lines with 9 mixins
- ❌ **Test Infrastructure:** Was broken (fixed in Phase 1)
- ❌ **Multiple ReBAC:** 3 implementations, no clear choice
- ❌ **Large Files:** 7 files over 2,000 lines each

### High Priority (Phase 3-4)
- ⚠️  **Type Safety:** 531 type: ignore suppressions
- ⚠️  **Circular Deps:** 189 TYPE_CHECKING guards
- ⚠️  **Code Duplication:** Sync/async client duplication
- ⚠️  **API Inconsistency:** Inconsistent parameter patterns

### Medium Priority (Phase 4-5)
- 📋 **N+1 Queries:** Metadata operations not batched
- 📋 **Cache Complexity:** Multiple layers, complex invalidation
- 📋 **Auth Sprawl:** 15+ authentication files
- 📋 **Deprecated Code:** Old parameters still present

---

## Performance Characteristics

### Current Performance
- **File Read (cached):** < 1ms
- **File Read (uncached):** 10-50ms
- **Permission Check:** 1-5ms
- **Semantic Search:** 50-200ms
- **Batch Operations:** Varies (N+1 issues)

### Bottlenecks
1. N+1 queries in batch metadata operations
2. Permission checks not batched
3. Cache invalidation overhead
4. Large file loading for parsing

---

## Security Model

### Authentication
- OAuth 2.0 (Google, Microsoft, X)
- API Key authentication
- JWT tokens

### Authorization
- Relationship-Based Access Control (ReBAC)
- Hierarchical permissions
- Tenant isolation
- Admin bypass flag (⚠️ security concern)

### Encryption
- Fernet encryption for OAuth tokens
- HTTPS for all network communication
- At-rest encryption via backend

---

## Testing

### Test Structure
```
tests/
├── unit/                       # Unit tests (fast)
│   ├── core/
│   ├── storage/
│   ├── backends/
│   └── server/
├── integration/                # Integration tests
│   ├── test_auth_postgres.py
│   └── test_skills_lifecycle.py
└── benchmarks/                 # Performance tests
```

### Test Statistics
- **Total Tests:** 4,118
- **Collection Errors:** 0 (fixed Phase 1)
- **Test Execution:** Parallel with xdist
- **Coverage:** TBD (need to run coverage report)

---

## Dependencies

### Core Dependencies
- **FastAPI** - Web framework
- **SQLAlchemy** - ORM
- **PostgreSQL** - Production database
- **Redis/Dragonfly** - Caching
- **LiteLLM** - Multi-provider LLM
- **pgvector** - Vector search

### Cloud Providers
- **boto3** - AWS S3
- **google-cloud-storage** - GCS
- **google-api-python-client** - Google Drive

### Processing
- **markitdown** - Document parsing
- **BM25S** - Full-text search
- **tiktoken** - Token counting

---

## Refactoring Roadmap

### Phase 1: Stabilization ✅ (In Progress)
- ✅ Fix test infrastructure (79 errors → 0)
- ✅ Establish code quality standards
- 🔄 Document architecture (this document)
- ⏳ Audit deprecated features

### Phase 2: Core Refactoring (Weeks 5-12)
- Extract SearchService from NexusFS
- Extract PermissionService from NexusFS
- Consolidate ReBAC managers
- Extract remaining 6 services
- Slim NexusFS to <500 lines

### Phase 3: API Cleanup (Weeks 13-16)
- Fix 531 type: ignore comments
- Standardize API patterns
- Replace Any types with Protocols
- Configuration object pattern

### Phase 4: Optimization (Weeks 17-20)
- Consolidate authentication
- Unified caching layer
- Fix N+1 queries
- Remove circular dependencies

### Phase 5: Security & Polish (Weeks 21-24)
- Security audit
- Input validation
- Remove deprecated code
- Complete documentation

---

## Comparison: Current vs. Target Architecture

### Current (Before Refactoring)
```
NexusFS (Monolith)
├── 9 Mixins (12,539 lines)
├── Tight coupling
├── Hard to test
└── Inheritance hell
```

### Target (After Phase 2)
```
NexusFS (Orchestrator, <500 lines)
├── SearchService (composition)
├── PermissionService (composition)
├── MountService (composition)
├── VersionService (composition)
├── OAuthService (composition)
├── SkillService (composition)
├── MCPService (composition)
└── LLMService (composition)
```

---

## References

- **Issue #986:** Original architecture analysis
- **Issue #987:** Phase 1 - Stabilization & Foundation
- **Issue #988:** Phase 2 - Core Refactoring
- **PHASE_1_PROGRESS.md:** Current progress tracking
- **CONTRIBUTING.md:** Code quality standards

---

**Document Status:** Living document, updated as refactoring progresses
**Last Updated:** 2026-01-02
**Next Review:** After Phase 2 completion

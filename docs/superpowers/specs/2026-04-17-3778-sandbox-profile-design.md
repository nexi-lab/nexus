# Issue #3778 — Lightweight `SANDBOX` profile for agent sandboxes

**Issue**: [nexi-lab/nexus#3778](https://github.com/nexi-lab/nexus/issues/3778)
**Epic**: [#3777 — Nexus as Context Layer for Secure Agent Runtimes](https://github.com/nexi-lab/nexus/issues/3777)
**Phase**: 1 — Foundation (no dependencies)

## Problem

The full Nexus server requires PostgreSQL, Dragonfly, and optionally Zoekt — too heavy to run inside every agent sandbox. For the "thin client" model (one Nexus per sandbox connecting to peer Nexus instances or a hub), we need a profile that runs on SQLite, in-process LRU cache, and BM25S with zero external services.

## Non-goals

- Replacing the full server profile for production hubs.
- Building a new packaging boundary (no new top-level package; `nexus-fs` slim wheel deliberately excludes bricks/server/cli and is the wrong layer for this).
- Enforcement: profile sets defaults, not hard guards. Users can still point SANDBOX at PG if they know what they're doing.
- Offline semantic search (no local vector backend). Semantic is federated; when all peers are unreachable the response is flagged `semantic_degraded=true` and BM25S results are returned.
- Windows support (CI will cover Linux + macOS only).
- Cross-sandbox orchestration (agentenv's concern).

## Decisions (from brainstorming)

| # | Question | Decision |
|---|---|---|
| Q1 | Profile shape | New tier `DeploymentProfile.SANDBOX` (not alias, not override of LITE) |
| Q2 | Hub-connected vs standalone | Hybrid: local BM25S + federated semantic with graceful degradation |
| Q3 | Default brick set | `LITE` base + `SEARCH, MCP, FEDERATION, PARSERS` |
| Q4 | Storage backend | Profile-gated defaults (SQLite + in-mem LRU + local disk); user config wins |
| Q5 | HTTP surface | MCP + `/health` + `/api/v2/features` only |
| Q6 | Docker packaging | One Dockerfile, two tags via `--build-arg NEXUS_PROFILE_EXTRAS={all,sandbox}` |
| Q7 | Pip extra | New minimal `sandbox` extra: `bm25s, cachetools, pdf-inspector, tokenizers` |
| Q8 | Federation failure | Fail-soft with `semantic_degraded=true` flag on response |

## Architecture

One Nexus process per agent sandbox. Zero external services required to boot.

```
┌───────────────────────────────────────────────┐
│  Agent sandbox                                │
│  ┌─────────────────────────────────────────┐  │
│  │  nexus (NEXUS_PROFILE=sandbox)          │  │
│  │                                         │  │
│  │  MCP transport (stdio or HTTP)          │  │
│  │      │                                  │  │
│  │      ▼                                  │  │
│  │  Bricks: SEARCH, MCP, FEDERATION,       │  │
│  │          PARSERS + LITE base            │  │
│  │      │                                  │  │
│  │      ▼                                  │  │
│  │  Storage: SQLite (meta+records)         │  │
│  │           cachetools.LRU (cache)        │  │
│  │           local disk (blobs)            │  │
│  │           BM25S mmap index              │  │
│  │      │                                  │  │
│  │      └─(federated_search)───┐           │  │
│  │                             │           │  │
│  │  HTTP surface:              │           │  │
│  │    /health                  │           │  │
│  │    /api/v2/features         │           │  │
│  └─────────────────────────────┼───────────┘  │
│                                │              │
└────────────────────────────────┼──────────────┘
                                 │ gRPC/HTTP (federation)
                                 ▼
                    ┌─────────────────────┐
                    │  Peer nexus(es)     │
                    │  or hub             │
                    │  (optional)         │
                    └─────────────────────┘
```

## Components

### 1. `DeploymentProfile.SANDBOX`
**File**: `src/nexus/contracts/deployment_profile.py`

- Add `SANDBOX = "sandbox"` to the `StrEnum`.
- Add `_SANDBOX_BRICKS = _LITE_BRICKS | frozenset({SEARCH, MCP, FEDERATION, PARSERS})`.
- Register in `_PROFILE_BRICKS` dict.
- Update module docstring hierarchy: `sandbox` sits as a distinct tier — it is a superset of `lite` but a proper subset of `full`.
- Add to `lib/performance_tuning.py`: `SANDBOX` tuning entry with `thread_pool_size=4`, `default_workers=2`, `db_pool_size=2`, `asyncpg_max_size=0`, `search_max_concurrency=2`.

Explicit off-by-default bricks (not in `_SANDBOX_BRICKS`): `LLM`, `PAY`, `SANDBOX` (brick), `WORKFLOWS`, `DISCOVERY`, `MEMORY`, `SKILLS`, `ACCESS_MANIFEST`, `CATALOG`, `DELEGATION`, `IDENTITY`, `SHARE_LINK`, `VERSIONING`, `WORKSPACE`, `PORTABILITY`, `SNAPSHOT`, `TASK_MANAGER`, `ACP`, `OBSERVABILITY`, `UPLOADS`, `RESILIENCY`.

### 2. Config resolver
**File**: `src/nexus/config.py`

- Allow `"sandbox"` in `NexusConfig.profile` validator.
- New helper `_apply_sandbox_defaults(cfg: NexusConfig) -> NexusConfig` runs after env merge and before return from `_load_from_environment` / `_load_from_dict`:
  - `backend` → `"local"` if unset
  - `data_dir` → `~/.nexus/sandbox/` if unset
  - `db_path` → `<data_dir>/nexus.db` if unset
  - `metastore_path` / `record_store_path` → same SQLite file if unset
  - `cache_size_mb` → `64` if unset
  - `enable_vector_search` → `False` if unset
- Explicit user values always win — no forced override.

### 3. Factory boot path
**Files**: `src/nexus/factory/orchestrator.py`, `src/nexus/factory/_wired.py`, `src/nexus/factory/_bricks.py`

- `enabled_bricks` resolution reuses existing path — no change needed once `DeploymentProfile.SANDBOX.default_bricks()` returns the right set.
- Cache store selection: `_BootContext` gains `cache_store_kind = "inmem" | "dragonfly" | ...`. When profile is `sandbox` (or cache URL unset), pick `InMemoryLRUCache`.
- New tiny adapter: `src/nexus/storage/cache_in_mem.py` wrapping `cachetools.LRUCache` behind the existing `CacheStore` protocol.

### 4. HTTP surface allowlist
**Files**: `src/nexus/server/fastapi_server.py`, `src/nexus/server/lifespan/*`

- Add `SANDBOX_HTTP_ALLOWLIST = frozenset({"/health", "/api/v2/features"})`.
- Router-mount loop: when profile is `sandbox`, skip any router whose prefix isn't in the allowlist.
- MCP transport boot unchanged (it's a separate brick).

### 5. Federated semantic with degraded flag
**Files**: `src/nexus/bricks/search/federated_search.py`, `src/nexus/bricks/search/search_service.py`

- Semantic path: if profile is `sandbox` and no local vector backend, delegate to federation.
- Wrap federation call: on `FederationUnreachableError` or all-peers-fail, fall back to BM25S local and set `semantic_degraded=True` on the response.
- WARNING logged once per session via rate-limited logger (reuse existing pattern if one exists; otherwise a module-level `_warned_once: bool`).
- Partial peer failure (some OK) → use RRF from reachable peers, no degraded flag.
- Response schema: add optional `semantic_degraded: bool` field to:
  - `SearchResult` / the bricks-level result type
  - HTTP response model for `/api/v2/search/*`
  - MCP tool output schema for `search` tool

### 6. Pip extra
**File**: `pyproject.toml`

```toml
sandbox = [
    "bm25s>=0.2",
    "cachetools>=5.0",
    "pdf-inspector>=0.1",   # from #3757
    "tokenizers>=0.15",
]
```

`all` extra remains the superset. Explicitly NOT pulled in by `sandbox`: `asyncpg`, `psycopg`, `redis`, `txtai`, `sentence-transformers`, `markitdown`, `fastembed`.

### 7. Docker variant
**File**: `Dockerfile`

- Add `ARG NEXUS_PROFILE_EXTRAS=all`.
- Change pip install line: `RUN pip install ".[${NEXUS_PROFILE_EXTRAS}]"`.
- CI builds two tags:
  - `nexus:latest` → `--build-arg NEXUS_PROFILE_EXTRAS=all`
  - `nexus:sandbox` → `--build-arg NEXUS_PROFILE_EXTRAS=sandbox`
- Size target: `nexus:sandbox` < 250MB compressed.

### 8. CLI validation
**Files**: `src/nexus/cli/utils.py`, `src/nexus/cli/commands/*`

- Add `"sandbox"` to any profile-validation allowlist.
- No new CLI flags.

## Data flow

### Boot flow

```
NEXUS_PROFILE=sandbox nexus serve
  → _load_from_environment()            # config.py
  → _apply_sandbox_defaults(cfg)        # new
  → resolve_enabled_bricks(SANDBOX, overrides)   # contracts
  → SQLite metastore/record_store       # existing factory
  → InMemoryLRUCache                    # new selection
  → _boot_pre_kernel_services + _boot_independent_bricks    # orchestrator
  → FastAPI lifespan w/ router allowlist
  → MCP transport ready
```

Invariant: boot completes in <5s on warm disk with no network I/O on the critical path.

### Search flow (semantic)

```
MCP tool call: search(mode="semantic")
  → SearchService.search()
  → profile==SANDBOX && no local vector backend
  → FederatedSearch.query(peers)
      ├─ peers reachable → RRF → (semantic_degraded=False)
      └─ all fail → BM25S local → (semantic_degraded=True)
                              + WARN once/session
```

### Search flow (keyword)

```
MCP tool call: search(mode="keyword")
  → SearchService.search() → BM25S local → (semantic_degraded=False)
```

## Error handling

### Boot failures
- **Missing `sandbox` extras** (`bm25s` / `cachetools` import fails): `BootError("Profile 'sandbox' requires 'nexus-ai-fs[sandbox]' extras. Install with: pip install 'nexus-ai-fs[sandbox]'")`.
- **User supplies PG/Dragonfly URL with `NEXUS_PROFILE=sandbox`**: log WARNING once, honor the URL. If it fails, surface normal connection error (don't mask).
- **`~/.nexus/sandbox/` unwritable**: `BootError` with path in message.
- **SQLite corruption**: `BootError`; user fixes path. No auto-recovery.

### Federation runtime failures
- **All peers unreachable** / `FederationUnreachableError`: BM25S fallback + `semantic_degraded=True` + WARN once/session.
- **Partial peer failure**: RRF from available peers; no degraded flag.
- **Zero peers configured + semantic query**: behaves like "all unreachable" (BM25S + degraded flag).

### HTTP surface
- Disabled router → standard FastAPI 404.
- `/metrics` with OBSERVABILITY off → 404.

### Cache
- `InMemoryLRUCache` OOM → cachetools LRU eviction; bounded by `cache_size_mb=64` default. No error surface.

### Deliberately unhandled
- PG/Dragonfly reachable but `SANDBOX` selected: no check, no block (Q4=A).
- Peer bandwidth exhaustion: out of scope; rate limiting is hub's responsibility.

## Testing

### Unit
- `tests/unit/core/test_sandbox_profile.py`:
  - Enum membership; `_SANDBOX_BRICKS` correct; superset of `LITE`; subset of `FULL`.
  - `resolve_enabled_bricks(SANDBOX)` matches expected set.
  - Off-by-default set excludes LLM/PAY/SANDBOX-brick/OBSERVABILITY/etc.
- Extend `tests/unit/core/test_deployment_profile.py::test_valid_profiles` to include `"sandbox"`.
- `tests/unit/test_config_sandbox.py`:
  - `_apply_sandbox_defaults` sets SQLite paths + local backend when unset.
  - Explicit user values survive.
  - `cache_size_mb=64` default only when profile=sandbox and unset.

### Integration
- `tests/integration/test_sandbox_boot.py`:
  - Boots with no PG/Dragonfly/Zoekt running.
  - `/health` → 200.
  - `/api/v2/features` → 200, `profile="sandbox"`, correct `enabled_bricks`.
  - Disabled router (e.g. `/api/v2/skills/*`) → 404.
  - Boot wall-clock asserted `< 5s`.

### Memory benchmark (gated)
- `tests/integration/test_sandbox_memory.py` behind `--sandbox-memory` pytest marker:
  - Boot + index 100 small files + serve 10 MCP calls.
  - RSS via `psutil`, asserted `< 300MB`.
  - Skip by default on CI; reproducible locally.

### Federated degraded flag
- `tests/unit/bricks/search/test_federated_degraded.py`:
  - Federation raises `FederationUnreachableError` → `semantic_degraded=True` + BM25S results.
  - Partial success → `semantic_degraded=False`.
  - WARN logged exactly once across many queries in the same session.

### MCP contract
- `tests/e2e/self_contained/test_sandbox_mcp.py`:
  - Spawn SANDBOX nexus; MCP stdio client searches; `semantic_degraded` field present in response schema.

### Docker smoke (CI)
- Build `nexus:sandbox` with `--build-arg NEXUS_PROFILE_EXTRAS=sandbox`.
- Assert image size `< 250MB` via `docker image inspect`.
- Run container with `NEXUS_PROFILE=sandbox`, curl `/health` from outside → 200.
- Assert no connection attempts to `localhost:5432` / `localhost:6379` (block via firewall rule in test, assert boot still succeeds).

### Out of scope
- Windows platform.
- Federation performance benchmarks.
- Multi-sandbox stress test (agentenv's concern).

## Acceptance criteria (from issue)

- [x] `NEXUS_PROFILE=sandbox nexus serve` boots with zero external services — covered by integration test.
- [x] Search works (BM25S keyword + federated semantic when peers configured) — covered by federated + MCP tests.
- [x] MCP server responds to all standard tools — covered by MCP e2e test.
- [x] Memory usage < 300MB idle — covered by memory benchmark (gated).
- [x] Boot time < 5 seconds — covered by integration test assertion.

## Rollout

1. Land profile + config defaults + cache adapter (PR 1).
2. Land HTTP allowlist + router gating (PR 2).
3. Land federated degraded flag + schema additions (PR 3).
4. Land Docker build-arg + CI matrix (PR 4).
5. Docs: `docs/deployment/sandbox-profile.md` explaining when to use SANDBOX vs FULL vs LITE.

PR sequencing keeps each change reviewable in isolation; all four can be in flight as separate branches off `develop`.

## Open questions

None. All clarifications resolved during brainstorming (Q1–Q8).

# Nexus Idle RSS Tuning — Design (Issue #3997)

**Date:** 2026-05-02
**Issue:** [#3997 — perf: nexusd idle RSS ~1.5 GB on demo profile](https://github.com/nexi-lab/nexus/issues/3997)
**Status:** Approved design, pending implementation plan

## Problem

`nexusd --config configs/config.demo.yaml` idle RSS = 1.5 GB, VmData = 14 GB, 37 threads. Roughly 4× the sandbox profile target (<400 MB). Single-process Python 3.14 + uvicorn + asyncio in Docker.

## Goal

Demo profile cold-start idle RSS ≤ 450 MB. Idle threads ≤ 20. VmData ≤ 4 GB. Search remains enabled by default (search:true in demo config), but heavy ML models load only when explicitly opted in.

## Root Causes (verified, with citations)

1. **Eager local embedding model load** — `Embeddings(path="sentence-transformers/all-MiniLM-L6-v2")` at `src/nexus/bricks/search/txtai_backend.py:465`, awaited at `src/nexus/server/lifespan/search.py:176`. ~900 MB.
2. **Eager cross-encoder reranker** — `Dockerfile:328` hardcodes `NEXUS_TXTAI_RERANKER`. ~300 MB.
3. **Oversized `_FULL_TUNING` pools** — `src/nexus/lib/performance_tuning.py:572-625`: `thread_pool_size=200`, `db_pool_size=20+30 overflow`, `httpx_max_connections=100`, `connector_max_workers=20`.
4. **No allocator tuning** — glibc default `M_ARENA_MAX = 8 × cpu_count` causes 200-400 MB RSS bloat. `Dockerfile:200-203` sets `LD_PRELOAD`/`GLIBC_TUNABLES` but not arenas.
5. **BLAS thread inflation** — numpy/torch/sentence-transformers spawn `cpu_count` BLAS threads at import; each = 8 MB pthread stack + glibc arena. Likely accounts for most of the 37 idle threads.

## Design

### Six change surfaces

```
┌─ Dockerfile + docker-entrypoint.sh ────────────────────────┐
│ • Add libjemalloc2 apt package                             │
│ • LD_PRELOAD: prepend libjemalloc.so.2                     │
│ • ENV MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072     │
│ • ENV OMP/OPENBLAS/MKL/NUMEXPR/VECLIB_MAXIMUM_THREADS=1    │
│ • DROP NEXUS_TXTAI_RERANKER hardcode                       │
│ • entrypoint: strip jemalloc from LD_PRELOAD if missing    │
└────────────────────────────────────────────────────────────┘
┌─ src/nexus/lib/performance_tuning.py ──────────────────────┐
│ • _FULL_TUNING: thread_pool 200→40, db 20+30→5+5,          │
│   httpx 100→20, connector 20→6                             │
│ • _CLOUD_TUNING: untouched (already sized for multi-tenant)│
└────────────────────────────────────────────────────────────┘
┌─ src/nexus/server/lifespan/__init__.py ────────────────────┐
│ • threading.stack_size(1<<20) before any thread spawn      │
│ • gc.set_threshold(50_000, 10, 10) at boot                 │
│ • gc.freeze() after services start                         │
│ • Add idle trimmer bg_task (60s gc.collect+malloc_trim)    │
└────────────────────────────────────────────────────────────┘
┌─ src/nexus/server/lifespan/search.py ──────────────────────┐
│ • _resolve_txtai_runtime_config: three-way auto            │
│   (no key→BM25 None, key→openai API, explicit→local)       │
└────────────────────────────────────────────────────────────┘
┌─ src/nexus/bricks/search/txtai_backend.py ─────────────────┐
│ • _startup_impl: branch on model is None → BM25 fast-path  │
│   (skip Embeddings(config) entirely, use existing BM25     │
│   fallback config from line 472-480 as primary)            │
└────────────────────────────────────────────────────────────┘
┌─ tests/integration/test_demo_memory.py ────────────────────┐
│ • pytest marker @pytest.mark.demo_memory                   │
│ • docker compose up demo, sleep 30, parse /proc/$pid/status│
│ • assert VmRSS < 450 MB, Threads < 20, VmData < 4 GB       │
└────────────────────────────────────────────────────────────┘
```

### Pool size changes (`_FULL_TUNING` only; `_CLOUD_TUNING` untouched)

| Field | Now | New | Source |
|---|---|---|---|
| `thread_pool_size` | 200 | **40** | Starlette/anyio upstream default ([FastAPI #8587](https://github.com/fastapi/fastapi/discussions/8587), [Kludex/fastapi-tips](https://github.com/Kludex/fastapi-tips)) |
| `httpx_max_connections` | 100 | **20** | [httpx Resource Limits](https://www.python-httpx.org/advanced/resource-limits/) |
| `remote_pool_maxsize` | 20 | **10** | Per-host pool, HTTP/2 collapses anyway |
| `connector_max_workers` | 20 | **6** | Blob ops I/O-bound; 6 saturates network |
| `db_pool_size` | 20 | **5** | [Cloud SQL sample](https://cloud.google.com/sql/docs/postgres/samples/cloud-sql-postgres-sqlalchemy-limit) |
| `db_max_overflow` | 30 | **5** | Single-tenant burst; PG idle conn ≈ 10 MB ([AWS](https://aws.amazon.com/blogs/database/resources-consumed-by-idle-postgresql-connections/)) |

### Dockerfile additions (`Dockerfile:202-203` area)

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends libjemalloc2 \
    && rm -rf /var/lib/apt/lists/*

ENV LD_PRELOAD="/usr/lib/x86_64-linux-gnu/libjemalloc.so.2:/usr/lib/libgomp.so.1"
ENV MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1
```

`Dockerfile:328` — DROP the line:
```
NEXUS_TXTAI_RERANKER=cross-encoder/ms-marco-MiniLM-L-2-v2 \
```

LD_PRELOAD ordering: jemalloc **first** (intercepts allocator calls before any other lib). Existing `libgomp.so.1` chained second. Entrypoint conditionally appends `libc10.so` for torch — that chain remains correct.

Sources: [Battle of the Mallocators 2025](http://smalldatum.blogspot.com/2025/04/battle-of-mallocators.html), [Heroku — Tuning glibc Memory Behavior](https://devcenter.heroku.com/articles/tuning-glibc-memory-behavior), [Software at Scale — malloc_trim](https://www.softwareatscale.dev/p/run-python-servers-more-efficiently), [numpy #17856 — OpenBLAS at import](https://github.com/numpy/numpy/issues/17856).

### Search runtime resolver — three-way auto

`src/nexus/server/lifespan/search.py:27-49`:

```python
def _resolve_txtai_runtime_config() -> tuple[str | None, dict[str, str] | None]:
    """Resolve embedding model + vectors config from env.

    Returns (None, None) when no model should be loaded — txtai backend then
    runs in BM25-only mode (keyword search over content + objects).
    """
    explicit_model = os.environ.get("NEXUS_TXTAI_MODEL", "").strip()
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    use_api_explicit = _env_truthy("NEXUS_TXTAI_USE_API_EMBEDDINGS")

    # Explicit local model wins (opt-in to ~900 MB)
    if explicit_model and not explicit_model.startswith("openai/"):
        return explicit_model, None

    # OpenAI key present → API embeddings, ~0 RAM
    if openai_api_key and (use_api_explicit or not explicit_model):
        model = explicit_model or "openai/text-embedding-3-small"
        vectors: dict[str, str] = {"api_key": openai_api_key}
        if openai_base_url:
            vectors["api_base"] = openai_base_url
        return model, vectors

    # No key, no explicit local model → BM25 keyword-only
    return None, None
```

Add boot-log line:
```python
mode = "bm25-only" if model is None else ("openai-api" if model.startswith("openai/") else "local")
logger.info("Search backend mode: %s (model=%s)", mode, model)
```

### Backend BM25 fast-path

`src/nexus/bricks/search/txtai_backend.py:_startup_impl` — early branch when `self._model is None`:

```python
if self._model is None:
    bm25_config = {
        "keyword": True,
        "content": content_store,
        "objects": True,
    }
    self._embeddings = Embeddings(bm25_config)
    self._hybrid = False
    logger.info("txtai backend started in BM25-only mode (no embedding model)")
    self._started = True
    self._configure_litellm()
    return
```

Reuses the existing fallback config block (lines 472-480) but takes it intentionally instead of via exception.

### Lifespan boot tweaks

`src/nexus/server/lifespan/__init__.py` — add at very top of `lifespan()` async ctx, before any other work:

```python
import threading, gc, ctypes
threading.stack_size(1 << 20)
gc.set_threshold(50_000, 10, 10)
```

After all startup phases complete, before `yield`:
```python
gc.freeze()
bg_tasks.append(asyncio.create_task(_idle_trimmer(), name="idle_trimmer"))
yield
```

`_idle_trimmer` helper:
```python
async def _idle_trimmer() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.malloc_trim  # probe
    except (OSError, AttributeError):
        logger.info("malloc_trim unavailable, idle trimmer disabled")
        return
    while True:
        await asyncio.sleep(60)
        try:
            gc.collect()
            libc.malloc_trim(0)
        except Exception:
            logger.exception("idle_trimmer iteration failed")
```

### Entrypoint safety net

`docker-entrypoint.sh` — strip jemalloc from LD_PRELOAD if missing at runtime (defensive, in case base image diverges):

```sh
if [ ! -f /usr/lib/x86_64-linux-gnu/libjemalloc.so.2 ]; then
    echo "WARN: libjemalloc.so.2 not found, falling back to glibc+arena cap" >&2
    export LD_PRELOAD="${LD_PRELOAD#*libjemalloc.so.2:}"
fi
```

## Data Flow (boot path)

```
docker compose up
   ↓
docker-entrypoint.sh
   ├─ LD_PRELOAD = libjemalloc.so.2 : libgomp.so.1 [: libc10.so if torch]
   ├─ MALLOC_ARENA_MAX=2 (active for all subsequent allocs)
   ├─ OMP/OPENBLAS/MKL=1 (locks BLAS thread spawn at first numpy import)
   └─ exec nexusd
        ↓
   uvicorn boot
        ↓
   FastAPI lifespan startup
        ├─ threading.stack_size(1<<20)        ← FIRST, before any thread
        ├─ gc.set_threshold(50_000, 10, 10)
        ├─ nx.bootstrap (NexusFS)
        ├─ startup_observability
        ├─ limiter.total_tokens = 40           ← was 200
        ├─ startup_search
        │     ├─ _resolve_txtai_runtime_config → (model, vectors)
        │     │     ├─ OPENAI_API_KEY set?  → ("openai/text-embedding-3-small", {api_key})
        │     │     ├─ NEXUS_TXTAI_MODEL=local?  → (model, None)  [opt-in heavy path]
        │     │     └─ neither               → (None, None)        [BM25 default]
        │     └─ SearchDaemon.startup
        │           └─ txtai_backend._startup_impl
        │                 ├─ model is None → BM25 fast-path, no model load
        │                 ├─ model startswith "openai/" → API embeddings, ~0 RAM
        │                 └─ model startswith "sentence-transformers/" → local load
        │                 └─ NEXUS_TXTAI_RERANKER set? → load reranker (opt-in only)
        ├─ startup_services
        ├─ ... other startups
        ├─ start idle_trimmer bg_task (60s loop)   ← new
        ├─ gc.freeze()                              ← AFTER all warmup imports
        └─ yield → serving
```

## Behavioral envelopes

| Setup | Search mode | Idle RSS (target) |
|---|---|---|
| No `OPENAI_API_KEY`, no model env | BM25 keyword (full-text + title) | **~350 MB** |
| `OPENAI_API_KEY` set | Hybrid BM25 + remote API embeddings | ~400 MB |
| `NEXUS_TXTAI_MODEL=sentence-transformers/...` | Hybrid local (opt-in) | ~1.3 GB |
| `+ NEXUS_TXTAI_RERANKER=...` | + cross-encoder reranker | +300 MB |

Demo `configs/config.demo.yaml:130` keeps `search: true`. No config change needed.

## Error handling

| Scenario | Mitigation |
|---|---|
| **jemalloc preload missing at runtime** | `docker-entrypoint.sh` checks file exists; strips from LD_PRELOAD with WARN log if missing. App still gets `MALLOC_ARENA_MAX=2`. |
| **BLAS=1 regresses unforeseen workload** | Document in CHANGELOG + sandbox-profile.md. Users override via `docker run -e OMP_NUM_THREADS=N` (last write wins). |
| **Operator forgot OPENAI_API_KEY, expected embeddings** | INFO log at boot: `"Search backend mode: %s (model=%s)"`. Visible in `docker logs`. |
| **txtai BM25-only path raises** | Existing try/except at `txtai_backend.py:482-487` catches → degraded mode (`_embeddings = None`). App stays up. |
| **Idle trimmer fails (Alpine/musl, missing libc)** | Probe `libc.malloc_trim` at task start; if unavailable, log once and exit task. App unaffected. |
| **`gc.freeze()` interaction** | Call only at end of startup, before `yield`. Documented constraint: no module-level global rebinds at runtime (hot reload, monkeypatching). |
| **Threads spawned before stack_size cap** | `threading.stack_size()` is the first lifespan line. C extensions that pre-spawn at import are accepted loss; anyio worker pool dominates. |
| **Demo regression test flakes** | Unique compose project name, random ports, poll `/healthz/ready` (60s timeout) + 15s extra for lazy init, `try/finally` teardown. |

## Testing strategy

### Layer 1: Unit tests (fast, no I/O)

- `test_performance_tuning.py` — assert `_FULL_TUNING` field values + JSON snapshot fixture.
- `test_search_runtime_config.py` (new) — parametrized matrix over env permutations, assert tuple return.
- `test_txtai_backend_bm25_only.py` (new) — pass `model=None`, assert BM25 config used, no model download.
- `test_lifespan_boot_tweaks.py` (new) — assert `threading.stack_size()`, `gc.get_threshold()`, `idle_trimmer` task registered.

### Layer 2: Integration test (slow, Docker)

`tests/integration/test_demo_memory.py` — `@pytest.mark.demo_memory`:
- Boot demo via `docker compose -p nexus-demo-test-{uuid} up -d`
- Poll `/healthz/ready` (60s timeout) + 15s lazy-init buffer
- Read `/proc/$pid/status` from inside container
- Assert `VmRSS_kB < 450 * 1024`, `Threads < 20`, `VmData_kB < 4 * 1024 * 1024`
- Log full status + first 20 lines of maps on failure
- `try/finally` `docker compose down -v` teardown

### Layer 3: CI wiring

- Unit tests: existing PR CI suite (~<1s added).
- Integration: new `.github/workflows/demo-memory.yml`, runs on `develop` push + nightly cron + manual dispatch. Not blocking PR by default.

### Manual verification (per PR)

```sh
docker build -t nexus:test .
docker run --rm nexus:test ls -la /usr/lib/x86_64-linux-gnu/libjemalloc.so.2
docker compose -p nexus-demo up -d
sleep 30
docker exec nexus-demo-nexus-1 sh -c \
    'pid=$(pgrep -f nexusd | head -1); cat /proc/$pid/status | grep -E "^(VmRSS|VmData|Threads)"'
docker exec nexus-demo-nexus-1 sh -c \
    'pid=$(pgrep -f nexusd | head -1); cat /proc/$pid/maps | grep -i jemalloc | head -3'
docker exec nexus-demo-nexus-1 env | grep -E "OMP|OPENBLAS|MKL"
docker logs nexus-demo-nexus-1 2>&1 | grep -i "search backend mode"
```

## Acceptance criteria (from issue #3997)

- Demo profile cold-start idle RSS ≤ 450 MB
- Idle thread count ≤ 20
- VmData ≤ 4 GB virtual
- First `/api/v2/search/query` may pay one-time 2-5 s model-load latency *only when local model opted in* — BM25 default has no model-load latency.
- New regression test (`pytest -m demo_memory`) measuring RSS 30 s after boot, asserting `rss_mb < 450`

## Out of scope

- PGO Python build (issue's Option E) — separate follow-up if RSS still misses target after Tier 1 changes.
- `gc.set_threshold` is in scope (latency win bundled with memory work). If it causes regressions, revert independently.
- Removing the legacy `nexus.bricks.search.embeddings` module — already deleted per code comment at search.py:171.
- New profile (`demo-lite`) — issue's Option D suggested this; we tune `_FULL_TUNING` directly instead, so demo + production-full both benefit. `_CLOUD_TUNING` untouched.

## References

- [Issue #3997](https://github.com/nexi-lab/nexus/issues/3997)
- [glibc mallopt(3)](https://man7.org/linux/man-pages/man3/mallopt.3.html), [malloc_trim(3)](https://man7.org/linux/man-pages/man3/malloc_trim.3.html)
- [AnyIO threads](https://anyio.readthedocs.io/en/stable/threads.html)
- [SQLAlchemy 2.0 pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [PostgreSQL Resource Consumption](https://www.postgresql.org/docs/current/runtime-config-resource.html)
- [PyTorch performance tuning](https://docs.pytorch.org/tutorials/recipes/recipes/tuning_guide.html)
- [scikit-learn parallelism](https://scikit-learn.org/1.1/computing/parallelism.html)
- [Uvicorn Settings](https://uvicorn.dev/settings/)

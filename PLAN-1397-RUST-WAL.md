# Implementation Plan: #1397 Rust-Accelerated Event Log WAL (bincode)

## Decisions Summary

| # | Decision | Choice |
|---|----------|--------|
| 1 | WAL vs Redis | Complementary — WAL for durability, Redis stays for pub/sub |
| 2 | Crate organization | New `nexus_wal` crate, re-exported through `nexus_fast` |
| 3 | FFI boundary | Rust owns serialization (bincode). Python passes dicts, Rust extracts + serializes |
| 4 | Abstraction layers | Two-layer: Rust WAL core (sync) + Python EventLogProtocol wrapper (async) |
| 5 | Event DRY | Unify WriteEvent into FileEvent. WAL Rust struct maps 1:1 to FileEvent |
| 6 | Protocol | Define full EventLogProtocol before implementation |
| 7 | Python fallback | ABC + conditional import. Fallback uses SQLite in WAL mode |
| 8 | Schema versioning | 8-byte version header per segment file (4-byte magic + 4-byte version) |
| 9 | Crash testing | Rust fault injection + targeted Python recovery tests |
| 10 | Backend parity | Shared parametrized test suite (both backends) |
| 11 | Benchmarks | Rust criterion + Python pytest-benchmark in CI |
| 12 | Edge cases | All 6 edge cases covered at launch |
| 13 | fsync strategy | Group commit with configurable durability (batch/every/none) |
| 14 | FFI overhead | Batch API primary (<1μs/event amortized), single append <5μs |
| 15 | Segment sizing | 16MB size-based segments, configurable |
| 16 | Integrity | CRC32 per record via crc32fast (SIMD-accelerated) |

## Architecture Diagram

```
                    Python Layer (async)
┌──────────────────────────────────────────────────┐
│  EventLogProtocol (core/protocols/event_log.py)  │
│    append() → EventId                            │
│    read_from() → list[Event]                     │
│    subscribe() → AsyncIterator[Event]            │
│    truncate() / sync() / health_check()          │
└────────────────────┬─────────────────────────────┘
                     │ implements
        ┌────────────┴────────────┐
        │                         │
┌───────┴────────┐    ┌──────────┴──────────┐
│  WALEventLog   │    │  SQLiteEventLog     │
│  (Rust-backed) │    │  (Python fallback)  │
│                │    │                     │
│  Wraps PyO3    │    │  SQLite WAL mode    │
│  RustWAL       │    │  Pure Python        │
└───────┬────────┘    └─────────────────────┘
        │ PyO3 FFI
┌───────┴────────────────────────────────────────┐
│  RustEventLogWAL (rust/nexus_wal)              │
│                                                │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  │
│  │ Segment  │  │ Group     │  │ Recovery   │  │
│  │ Manager  │  │ Commit    │  │ Engine     │  │
│  │          │  │ (fsync)   │  │            │  │
│  └──────────┘  └───────────┘  └────────────┘  │
│                                                │
│  Record format: [len:u32][crc32:u32][bincode]  │
│  Segment header: [magic:4B][version:4B]        │
└────────────────────────────────────────────────┘
        │ complement (not replace)
┌───────┴──────────────────────┐
│  RedisEventBus (existing)    │
│  Distributed pub/sub fanout  │
└──────────────────────────────┘
```

## Record Format

```
Segment File Layout:
┌──────────────────────────────────────────┐
│ Header (8 bytes)                         │
│  [0x4E 0x58 0x57 0x4C] = "NXWL" magic   │
│  [0x00 0x00 0x00 0x01] = version 1       │
├──────────────────────────────────────────┤
│ Record 0                                 │
│  [sequence_number: u64]  (8 bytes)       │
│  [payload_len: u32]      (4 bytes)       │
│  [payload: bincode bytes] (variable)     │
│  [crc32: u32]            (4 bytes)       │
├──────────────────────────────────────────┤
│ Record 1                                 │
│  ...                                     │
└──────────────────────────────────────────┘
```

CRC32 covers: sequence_number + payload_len + payload (not the CRC itself).

## Bincode Event Schema (Rust)

```rust
#[derive(Serialize, Deserialize)]
struct WalEvent {
    event_type: u8,        // FileEventType enum as u8
    path: String,
    zone_id: Option<String>,
    timestamp_ns: u64,     // nanosecond precision, not ISO string
    event_id: [u8; 16],    // UUID as bytes (no string overhead)
    old_path: Option<String>,
    size: Option<u64>,
    etag: Option<String>,
    agent_id: Option<String>,
    revision: Option<u64>,
}
```

Maps 1:1 to Python `FileEvent`. Compact representation:
- `event_type` as u8 (not string) — 1 byte vs ~12 bytes
- `timestamp_ns` as u64 (not ISO string) — 8 bytes vs ~25 bytes
- `event_id` as raw UUID bytes — 16 bytes vs 36 bytes
- Estimated bincode size: ~80-150 bytes/event (vs ~300-500 bytes JSON)

---

## Implementation Phases

### Phase 1: Protocol Foundation
**Files:**
- `src/nexus/core/protocols/event_log.py` (NEW) — EventLogProtocol definition
- `src/nexus/core/protocols/__init__.py` (NEW or EDIT) — exports

**Work:**
1. Define `EventLogProtocol` with:
   - `async def append(self, event: FileEvent) -> int` (returns sequence number)
   - `async def append_batch(self, events: list[FileEvent]) -> list[int]`
   - `async def read_from(self, seq: int, limit: int = 1000) -> list[FileEvent]`
   - `async def subscribe(self, pattern: str) -> AsyncIterator[FileEvent]`
   - `async def truncate(self, before_seq: int) -> int` (returns records truncated)
   - `async def sync(self) -> None` (force fsync)
   - `async def health_check(self) -> bool`
   - `def current_sequence(self) -> int` (latest sequence number)

2. Define `EventLogConfig` dataclass:
   - `wal_dir: Path` — directory for WAL segment files
   - `segment_size_mb: int = 16`
   - `sync_mode: Literal["batch", "every", "none"] = "batch"`
   - `batch_sync_interval_ms: int = 10` — group commit interval
   - `max_batch_size: int = 1000` — force sync after N pending

**Estimated LOC:** ~60

### Phase 2: Rust WAL Core (`rust/nexus_wal/`)
**Files:**
- `rust/nexus_wal/Cargo.toml` (NEW)
- `rust/nexus_wal/src/lib.rs` (NEW) — module root
- `rust/nexus_wal/src/event.rs` (NEW) — WalEvent struct + bincode
- `rust/nexus_wal/src/segment.rs` (NEW) — segment file management
- `rust/nexus_wal/src/wal.rs` (NEW) — WAL core (append, read, truncate, sync)
- `rust/nexus_wal/src/group_commit.rs` (NEW) — batched fsync
- `rust/nexus_wal/src/recovery.rs` (NEW) — crash recovery + CRC validation
- `rust/nexus_wal/src/pyo3_bindings.rs` (NEW) — PyO3 wrapper

**Dependencies (Cargo.toml):**
```toml
[dependencies]
serde = { version = "1.0", features = ["derive"] }
bincode = "1.3"
crc32fast = "1.4"
pyo3 = { version = "0.27", features = ["extension-module"] }
parking_lot = "0.12"   # faster Mutex than std
uuid = { version = "1.0", features = ["v4"] }
```

**Work:**
1. `event.rs`: `WalEvent` struct with `Serialize`/`Deserialize`. Conversion functions:
   - `from_py_dict(py: Python, dict: &PyDict) -> PyResult<WalEvent>`
   - `to_py_dict(py: Python, event: &WalEvent) -> PyResult<PyObject>`

2. `segment.rs`: Segment file management
   - `SegmentWriter::new(dir, segment_id)` — creates file with header
   - `SegmentWriter::append(seq, payload) -> Result<()>` — writes record + CRC32
   - `SegmentReader::open(path) -> Result<Self>` — validates header
   - `SegmentReader::iter() -> impl Iterator<Item = Result<(u64, Vec<u8>)>>`
   - `SegmentReader::read_from(seq) -> Result<Vec<(u64, Vec<u8>)>>`
   - Rotation: new segment when current exceeds `segment_size_bytes`
   - Naming: `wal-{first_sequence_number}.seg`

3. `wal.rs`: Core WAL engine
   - `WalEngine::open(config) -> Result<Self>` — open or recover
   - `WalEngine::append(event: &[u8]) -> Result<u64>` — returns sequence number
   - `WalEngine::append_batch(events: &[&[u8]]) -> Result<Vec<u64>>`
   - `WalEngine::read_from(seq: u64, limit: usize) -> Result<Vec<(u64, Vec<u8>)>>`
   - `WalEngine::truncate(before_seq: u64) -> Result<u64>` — delete old segments
   - `WalEngine::sync() -> Result<()>` — force fsync active segment
   - `WalEngine::current_sequence() -> u64`
   - Thread-safe via `parking_lot::Mutex` on active segment

4. `group_commit.rs`: Batched fsync thread
   - Background thread wakes every `batch_sync_interval_ms`
   - Collects pending writers, issues single `fsync()`, notifies all
   - Uses `parking_lot::Condvar` for efficient signaling

5. `recovery.rs`: Crash recovery
   - Scan all segment files in order
   - For each segment: validate header, iterate records
   - For each record: validate CRC32
   - On first bad CRC: truncate segment at that point
   - Set `current_sequence` to last valid record's sequence + 1
   - Log recovery statistics (valid records, truncated bytes)

6. `pyo3_bindings.rs`: PyO3 module
   - `#[pyclass] struct RustEventLogWAL` wrapping `WalEngine`
   - `#[pymethods]`:
     - `fn new(wal_dir: &str, config: &PyDict) -> PyResult<Self>`
     - `fn append(&self, py: Python, event: &PyDict) -> PyResult<u64>`
     - `fn append_batch(&self, py: Python, events: &PyList) -> PyResult<Vec<u64>>`
     - `fn read_from(&self, py: Python, seq: u64, limit: usize) -> PyResult<Vec<PyObject>>`
     - `fn truncate(&self, before_seq: u64) -> PyResult<u64>`
     - `fn sync(&self) -> PyResult<()>`
     - `fn current_sequence(&self) -> u64`
     - `fn health_check(&self) -> bool`

**Estimated LOC (Rust):** ~800-1000

### Phase 3: Re-export Through nexus_fast
**Files:**
- `Cargo.toml` (EDIT) — add `nexus_wal` as workspace member and dependency
- `src/rust/lib.rs` (EDIT) — re-export `nexus_wal` PyO3 classes
- OR: separate .so if re-export is too complex (backup plan)

**Work:**
1. Add `nexus_wal` as workspace member in root `Cargo.toml`
2. Add `nexus_wal` as dependency of the root crate
3. Re-export `RustEventLogWAL` in the `_nexus_fast` module
4. Verify `import nexus._nexus_fast; nexus._nexus_fast.RustEventLogWAL` works

**Estimated LOC change:** ~20

### Phase 4: Python WAL Wrapper + SQLite Fallback
**Files:**
- `src/nexus/core/event_log_wal.py` (NEW) — WALEventLog (Rust-backed)
- `src/nexus/core/event_log_sqlite.py` (NEW) — SQLiteEventLog (fallback)
- `src/nexus/core/event_log_factory.py` (NEW) — factory + conditional import

**Work:**
1. `event_log_wal.py`: `WALEventLog` class
   - Implements `EventLogProtocol`
   - Wraps `RustEventLogWAL` PyO3 object
   - `append()`: converts `FileEvent` → dict → calls Rust → returns seq
   - `append_batch()`: converts list[FileEvent] → list[dict] → calls Rust
   - `read_from()`: calls Rust → converts list[dict] → list[FileEvent]
   - `subscribe()`: polls `read_from()` with cursor tracking (async generator)
   - `truncate()`, `sync()`, `health_check()`: thin wrappers

2. `event_log_sqlite.py`: `SQLiteEventLog` class
   - Implements `EventLogProtocol`
   - Uses `sqlite3` stdlib with WAL mode (`PRAGMA journal_mode=WAL`)
   - Schema: `CREATE TABLE wal_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_json TEXT, created_at REAL)`
   - `append()`: `INSERT INTO wal_events` + return `lastrowid`
   - `read_from()`: `SELECT WHERE seq >= ? LIMIT ?`
   - `truncate()`: `DELETE WHERE seq < ?`
   - `sync()`: `PRAGMA wal_checkpoint(FULL)`
   - `subscribe()`: polls with `asyncio.sleep()` between checks

3. `event_log_factory.py`: Factory
   ```python
   def create_event_log(config: EventLogConfig) -> EventLogProtocol:
       try:
           from nexus._nexus_fast import RustEventLogWAL
           return WALEventLog(RustEventLogWAL(str(config.wal_dir), ...))
       except ImportError:
           return SQLiteEventLog(config.wal_dir / "event_log.db")
   ```

**Estimated LOC (Python):** ~250-350

### Phase 5: Integration with Existing Event Bus
**Files:**
- `src/nexus/core/event_bus.py` (EDIT) — add WAL integration
- `src/nexus/server/fastapi_server.py` (EDIT) — wire WAL in startup

**Work:**
1. In `RedisEventBus.publish()`: also call `event_log.append(event)` (WAL first, then Redis pub/sub)
2. In `RedisEventBus.startup_sync()`: use WAL `read_from()` instead of PG query for missed events
3. Factory wiring: create `event_log` in server startup, pass to `RedisEventBus`
4. The WAL becomes the SSOT for recent events; PG `operation_log` remains the long-term archive

**Estimated LOC change:** ~30-50

### Phase 6: WriteEvent Unification (5C)
**Files:**
- `src/nexus/storage/write_buffer.py` (EDIT) — use FileEvent instead of WriteEvent
- `src/nexus/core/event_bus.py` (EDIT) — add any missing fields to FileEvent

**Work:**
1. Add missing fields to `FileEvent`: `metadata`, `is_new`, `snapshot_hash`, `metadata_snapshot` (from WriteEvent)
   - OR: create a `WriteContext` that pairs `FileEvent` with write-specific metadata
2. Update `WriteBuffer` to accept `FileEvent` instead of `WriteEvent`
3. Remove `WriteEvent` and `EventType` from `write_buffer.py`
4. Update tests

**Note:** This can be deferred to a follow-up PR if scope is too large. The WAL works without this unification.

**Estimated LOC change:** ~100 (mostly deletions + test updates)

### Phase 7: Tests
**Files:**
- `rust/nexus_wal/src/` — Rust `#[cfg(test)]` modules in each file
- `tests/unit/core/test_event_log_wal.py` (NEW) — parametrized WAL tests
- `tests/unit/core/test_event_log_protocol.py` (NEW) — Protocol conformance
- `tests/integration/test_event_log_crash_recovery.py` (NEW) — Python-side crash tests
- `tests/benchmarks/bench_event_log_wal.py` (NEW) — Python benchmarks
- `rust/nexus_wal/benches/wal_bench.rs` (NEW) — Rust criterion benchmarks

**Rust Tests (in each module's `#[cfg(test)]`):**
1. `event.rs` tests: bincode round-trip, schema version handling
2. `segment.rs` tests: write/read, rotation, header validation
3. `wal.rs` tests: append, read_from, truncate, concurrent access
4. `recovery.rs` tests: **fault injection** — truncated file, corrupted CRC, missing segment, partial write
5. `group_commit.rs` tests: batched fsync, multiple writers

**Python Tests (`test_event_log_wal.py`):**
```python
@pytest.fixture(params=["rust", "sqlite"])
def event_log(request, tmp_path):
    if request.param == "rust":
        try:
            return WALEventLog(RustEventLogWAL(str(tmp_path), {}))
        except ImportError:
            pytest.skip("Rust extension not available")
    return SQLiteEventLog(tmp_path / "test.db")
```

Test cases (run against both backends):
1. Append single event → verify sequence number
2. Append batch → verify all sequence numbers sequential
3. Read from specific sequence → correct events returned
4. Truncate → old events gone, new events intact
5. Sync → no error (verify fsync called on Rust side via mock)
6. Health check → True when operational
7. **Edge cases:**
   - Segment rotation under load (append >16MB of events)
   - Concurrent appends from multiple threads
   - Truncate while readers active
   - Empty WAL directory (first start)
   - Disk full simulation (via mock on Rust side)
   - Very large event (>1MB metadata field)

**Python Crash Recovery Tests (`test_event_log_crash_recovery.py`):**
1. Write N events → delete lock file → reopen → verify N events readable
2. Write events → truncate segment file mid-record → reopen → verify recovery

**Benchmarks:**
- Rust: `criterion` bench for `append` and `append_batch` (1K, 10K, 100K events)
- Python: `pytest-benchmark` for round-trip through PyO3 (single and batch)
- Target assertions: batch <1μs/event, single <5μs/event

**Estimated LOC (tests):** ~400-500 Rust, ~300-400 Python

---

## File Summary

| File | Action | Phase |
|------|--------|-------|
| `src/nexus/core/protocols/event_log.py` | NEW | 1 |
| `rust/nexus_wal/Cargo.toml` | NEW | 2 |
| `rust/nexus_wal/src/lib.rs` | NEW | 2 |
| `rust/nexus_wal/src/event.rs` | NEW | 2 |
| `rust/nexus_wal/src/segment.rs` | NEW | 2 |
| `rust/nexus_wal/src/wal.rs` | NEW | 2 |
| `rust/nexus_wal/src/group_commit.rs` | NEW | 2 |
| `rust/nexus_wal/src/recovery.rs` | NEW | 2 |
| `rust/nexus_wal/src/pyo3_bindings.rs` | NEW | 2 |
| `Cargo.toml` | EDIT | 3 |
| `src/rust/lib.rs` | EDIT | 3 |
| `src/nexus/core/event_log_wal.py` | NEW | 4 |
| `src/nexus/core/event_log_sqlite.py` | NEW | 4 |
| `src/nexus/core/event_log_factory.py` | NEW | 4 |
| `src/nexus/core/event_bus.py` | EDIT | 5 |
| `src/nexus/server/fastapi_server.py` | EDIT | 5 |
| `src/nexus/storage/write_buffer.py` | EDIT | 6 |
| `tests/unit/core/test_event_log_wal.py` | NEW | 7 |
| `tests/unit/core/test_event_log_protocol.py` | NEW | 7 |
| `tests/integration/test_event_log_crash_recovery.py` | NEW | 7 |
| `tests/benchmarks/bench_event_log_wal.py` | NEW | 7 |
| `rust/nexus_wal/benches/wal_bench.rs` | NEW | 7 |

**Total estimated new LOC:** ~1,500-2,000 Rust + ~700-900 Python
**Total estimated edit LOC:** ~100-150 across existing files

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Re-exporting nexus_wal through nexus_fast may be complex | Backup: ship as separate `_nexus_wal.so` |
| Group commit thread interaction with Python GIL | Release GIL during fsync via `py.allow_threads()` |
| SQLite fallback perf much worse than Rust | Document: fallback is for correctness, not performance |
| Phase 6 (WriteEvent unification) scope creep | Can be deferred to follow-up PR |
| bincode schema changes break existing WAL files | Version header (Decision 8) handles this |

## References

- [OkayWAL: Write-Ahead Log in Rust](https://github.com/khonsulabs/okaywal)
- [Design and Reliability of a User Space WAL in Rust (arXiv 2507.13062)](https://arxiv.org/html/2507.13062)
- [walcraft: WAL with bincode](https://github.com/RustyFarmer101/walcraft)
- NEXUS-LEGO-ARCHITECTURE.md Sections 2.2, 6.3, 6.5

# nexus-prefetch Crate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new Rust crate `rust/nexus-prefetch/` that replaces the Python `ReadaheadManager` with a Mountpoint-S3-style adaptive prefetcher featuring pluggable detectors (Sequential, Stride, MajorityTrend), per-file-handle state with doubling/capped windows, bounded worker pool with backpressure, and reset-on-reorder semantics.

**Architecture:** The crate is layered: (1) detector trait + impls (pure, no I/O), (2) per-fh `Session` state (window + counters + pending-set), (3) `PrefetchEngine` that composes detector + session + worker pool with bounded `tokio::sync::mpsc` queue, calling an injected `RangeReader` trait so the engine is backend-agnostic. The engine is exposed to Python via pyo3 bindings on `nexus-cdylib` so `ReadaheadManager.on_open/on_read/on_release` can route to the Rust engine. Kernel integration emits hints from the DT_REG `sys_read` path after each read. Detector decoupling lets us A/B detectors without changing wiring.

**Tech Stack:** Rust 2021, tokio (rt-multi-thread, sync, macros, time), parking_lot, dashmap, thiserror, tracing, pyo3 (for Python bridge), criterion (benches). Python: existing `ReadaheadManager` becomes a thin shim around the Rust engine.

**Scope note:** This plan covers one feature spanning new-crate + trait extension + Python bridge + kernel wiring. It is intentionally staged into 5 independently-testable phases. Each phase ends with passing tests and a clean commit. If reviewer prefers separate plans per subsystem, split at the phase boundaries below — but the crate itself (Phases 1–2) is self-contained and shippable alone.

---

## File Structure

**New files (all in `rust/nexus-prefetch/`):**

- `Cargo.toml` — package manifest mirroring `rust/contracts/Cargo.toml` shape; depends on `tokio`, `dashmap`, `parking_lot`, `thiserror`, `tracing`, `bytes`, optional `pyo3`. **One responsibility:** dependency declaration.
- `src/lib.rs` — module roots + public re-exports (`Detector`, `PrefetchEngine`, `Session`, `RangeReader`, `EngineConfig`, `PrefetchError`). **One responsibility:** crate surface.
- `src/error.rs` — `PrefetchError` enum (`Backend`, `QueueFull`, `Shutdown`, `OutOfRange`). **One responsibility:** error type.
- `src/config.rs` — `EngineConfig` struct (block_size, initial_window, max_window, max_workers, queue_capacity, max_blocks_per_trigger, sequential_tolerance, min_sequential_count). **One responsibility:** tunables, with `Default`.
- `src/pattern.rs` — `AccessPattern` enum (`Sequential`, `Stride { stride: i64 }`, `Trend { offsets: Vec<i64> }`, `Random`). **One responsibility:** detector output ADT.
- `src/detector.rs` — `trait Detector { fn observe(&mut self, offset: u64, size: u32) -> AccessPattern; fn reset(&mut self); }`. **One responsibility:** detector contract.
- `src/detector/sequential.rs` — `SequentialDetector` impl. Tracks `last_offset+last_size`, `sequential_count`, `tolerance`. **One responsibility:** sequential pattern detection.
- `src/detector/stride.rs` — `StrideDetector` impl. Tracks last two offsets, computes stride, requires 3 confirmations. **One responsibility:** fixed-stride pattern detection.
- `src/detector/trend.rs` — `MajorityTrendDetector` impl (Leap ATC'20 — sliding window of last N deltas, returns majority delta if >50%). **One responsibility:** majority-trend detection.
- `src/session.rs` — `Session` struct (per-fh state: `last_offset`, `last_size`, `window`, `prefetch_pending: BTreeSet<u64>`, `prefetch_buffer: BTreeMap<u64, Bytes>`, detector box). **One responsibility:** per-handle accounting + buffer pool slot.
- `src/range_reader.rs` — `trait RangeReader: Send + Sync { fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError>; }`. **One responsibility:** abstraction over backend range reads.
- `src/engine.rs` — `PrefetchEngine` (owns sessions DashMap, worker pool, bounded mpsc, RangeReader Arc). Methods: `on_open`, `on_read`, `on_release`, `shutdown`. **One responsibility:** orchestration.
- `src/worker.rs` — worker-loop function consuming the mpsc, calling `RangeReader::read`, depositing bytes into the session's buffer. **One responsibility:** I/O execution.
- `src/metrics.rs` — atomic counters (hits, misses, prefetched_bytes, dropped_due_to_backpressure). **One responsibility:** observability.
- `src/pyo3_bindings.rs` (feature `python`) — `#[pyclass] PyPrefetchEngine` wrapping `PrefetchEngine`. **One responsibility:** Python bridge.
- `tests/integration.rs` — end-to-end: synthetic RangeReader + sequential workload assertion. **One responsibility:** integration test surface.
- `benches/throughput.rs` — criterion benchmark for sequential/stride workloads. **One responsibility:** perf gate.

**Modified files (top-level):**

- `Cargo.toml` — add `"rust/nexus-prefetch"` to `members`; add `nexus-prefetch = { path = "rust/nexus-prefetch" }` to `[workspace.dependencies]`.
- `rust/kernel/src/abc/object_store.rs` — extend `ObjectStore` trait with default-impl `read_range(content_id, offset, size, ctx)` method (Phase 2).
- `rust/backends/src/storage/cas_local.rs` — override `read_range` (Phase 2).
- `rust/backends/src/storage/path_local.rs` — override `read_range` (Phase 2).
- `rust/backends/src/storage/remote.rs` — override `read_range` if remote RPC supports it (Phase 2).
- `rust/nexus-cdylib/Cargo.toml` — add `nexus-prefetch = { workspace = true, features = ["python"] }` (Phase 3).
- `rust/nexus-cdylib/src/lib.rs` — register `PyPrefetchEngine` (Phase 3).
- `src/nexus/fuse/readahead.py` — `ReadaheadManager` becomes thin shim over `PyPrefetchEngine`. Keep public API (`on_open`, `on_read`, `on_release`, `from_dict` config) (Phase 3).
- `rust/kernel/src/kernel/io.rs:264–275` — emit prefetch hint on DT_REG successful read via injected `Arc<dyn PrefetchHintSink>` held by `Kernel` (Phase 4).
- `rust/kernel/src/kernel/mod.rs` — add `prefetch_hint_sink` field + setter (Phase 4).

---

## Phase 1: Standalone crate — detectors + session + engine (no Python, no kernel)

### Task 1: Create crate skeleton

**Files:**
- Create: `rust/nexus-prefetch/Cargo.toml`
- Create: `rust/nexus-prefetch/src/lib.rs`
- Modify: `Cargo.toml` (workspace root)

- [ ] **Step 1: Add `rust/nexus-prefetch/Cargo.toml`**

```toml
[package]
name = "nexus-prefetch"
version = "0.1.0"
edition = "2021"
description = "Adaptive prefetcher — per-fh window + pluggable detectors (Sequential / Stride / MajorityTrend). Replaces Python ReadaheadManager. See issue #4057."

[lib]
name = "nexus_prefetch"
crate-type = ["rlib"]

[features]
default = []
python = ["pyo3"]

[dependencies]
bytes = "1"
dashmap = "6.1"
parking_lot = "0.12"
thiserror = "2.0"
tracing = "0.1"
tokio = { version = "1", features = ["rt", "rt-multi-thread", "sync", "time", "macros"] }
pyo3 = { version = "0.22", features = ["extension-module"], optional = true }

[dev-dependencies]
tokio = { version = "1", features = ["rt-multi-thread", "macros", "test-util"] }
criterion = "0.5"

[[bench]]
name = "throughput"
harness = false
```

- [ ] **Step 2: Add `rust/nexus-prefetch/src/lib.rs`**

```rust
//! nexus-prefetch — adaptive read-ahead engine.
//!
//! See `docs/superpowers/plans/2026-05-11-issue-4057-nexus-prefetch.md`
//! for architecture. Public surface is `PrefetchEngine` + `RangeReader`.

pub mod config;
pub mod detector;
pub mod engine;
pub mod error;
pub mod metrics;
pub mod pattern;
pub mod range_reader;
pub mod session;
pub mod worker;

#[cfg(feature = "python")]
pub mod pyo3_bindings;

pub use config::EngineConfig;
pub use detector::Detector;
pub use engine::PrefetchEngine;
pub use error::PrefetchError;
pub use pattern::AccessPattern;
pub use range_reader::RangeReader;
pub use session::Session;
```

- [ ] **Step 3: Add crate to workspace**

In `Cargo.toml` at workspace root, modify the `members` array (currently ending with `"rust/nexus-cdylib"`) to include `"rust/nexus-prefetch"`:

```toml
members = [
    "rust/contracts",
    "rust/lib",
    "rust/transport",
    "rust/kernel",
    "rust/backends",
    "rust/services",
    "rust/raft",
    "rust/profiles/cluster",
    "rust/nexus-cdylib",
    "rust/nexus-prefetch",
]
```

And add to `[workspace.dependencies]` (right after the existing `raft = ...` entry):

```toml
nexus-prefetch = { path = "rust/nexus-prefetch" }
```

- [ ] **Step 4: Create empty module stubs so `cargo check` passes**

Create each of these files containing only a single-line comment so the `pub mod` lines in `lib.rs` resolve:

```bash
for f in config detector engine error metrics pattern range_reader session worker; do
  echo "//! Stub for nexus-prefetch::$f — filled in subsequent tasks." > rust/nexus-prefetch/src/$f.rs
done
```

- [ ] **Step 5: Verify the crate builds**

Run: `cargo check -p nexus-prefetch`
Expected: clean compile (the stubs are empty modules, allowed).

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml rust/nexus-prefetch/Cargo.toml rust/nexus-prefetch/src/
git commit -m "feat(#4057): scaffold nexus-prefetch crate"
```

---

### Task 2: `PrefetchError` enum

**Files:**
- Modify: `rust/nexus-prefetch/src/error.rs`
- Test: inline `#[cfg(test)] mod tests` in `error.rs`

- [ ] **Step 1: Write the failing test**

Replace the stub at `rust/nexus-prefetch/src/error.rs`:

```rust
//! Error type for the prefetch engine.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum PrefetchError {
    #[error("backend read failed: {0}")]
    Backend(String),
    #[error("prefetch queue full — dropping hint")]
    QueueFull,
    #[error("engine shutting down")]
    Shutdown,
    #[error("offset {offset} + size {size} exceeds file bounds {file_size}")]
    OutOfRange { offset: u64, size: u32, file_size: u64 },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_includes_offset_for_out_of_range() {
        let e = PrefetchError::OutOfRange { offset: 1024, size: 512, file_size: 1000 };
        let s = format!("{e}");
        assert!(s.contains("1024"));
        assert!(s.contains("512"));
        assert!(s.contains("1000"));
    }

    #[test]
    fn backend_error_preserves_message() {
        let e = PrefetchError::Backend("net timeout".into());
        assert!(format!("{e}").contains("net timeout"));
    }
}
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cargo test -p nexus-prefetch --lib error`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/error.rs
git commit -m "feat(#4057): add PrefetchError enum"
```

---

### Task 3: `AccessPattern` ADT

**Files:**
- Modify: `rust/nexus-prefetch/src/pattern.rs`

- [ ] **Step 1: Write the file with tests**

```rust
//! Detector output — what kind of access pattern the latest observation
//! suggests.  Drives the prefetch decision in the engine.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AccessPattern {
    /// No prefetch should be issued yet.
    Cold,
    /// Sequential forward — prefetcher should issue the next N blocks.
    Sequential,
    /// Fixed stride detected — prefetcher should issue blocks at
    /// `offset + k*stride` for k=1..max_blocks.
    Stride { stride: i64 },
    /// Leap-style majority trend — issue prefetches along the dominant
    /// delta direction.
    Trend { delta: i64 },
    /// Random — prefetcher must reset window and drop pending.
    Random,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns_compare_by_value() {
        assert_eq!(AccessPattern::Sequential, AccessPattern::Sequential);
        assert_ne!(
            AccessPattern::Stride { stride: 4096 },
            AccessPattern::Stride { stride: 8192 }
        );
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib pattern`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/pattern.rs
git commit -m "feat(#4057): add AccessPattern ADT"
```

---

### Task 4: `EngineConfig` with defaults

**Files:**
- Modify: `rust/nexus-prefetch/src/config.rs`

- [ ] **Step 1: Write the file with tests**

```rust
//! Engine tunables.  Defaults track the Python ReadaheadConfig values
//! at `src/nexus/fuse/readahead.py:66–74` so behavior is unchanged
//! when the engine swaps in transparently.

#[derive(Debug, Clone)]
pub struct EngineConfig {
    /// Per-block size for prefetch range GETs.
    pub block_size: u32,
    /// Initial readahead window in bytes.
    pub initial_window: u64,
    /// Max readahead window in bytes.  Capped at 2 GiB per acceptance.
    pub max_window: u64,
    /// Worker pool size (concurrent in-flight range GETs).
    pub max_workers: usize,
    /// Bounded mpsc capacity — once full, new hints are dropped
    /// (backpressure).
    pub queue_capacity: usize,
    /// Max blocks issued per prefetch trigger.
    pub max_blocks_per_trigger: u32,
    /// Bytes of slack for sequential detection.
    pub sequential_tolerance: u64,
    /// Confirmations before a pattern triggers prefetch.
    pub min_sequential_count: u32,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            block_size: 4 * 1024 * 1024,
            initial_window: 512 * 1024,
            max_window: 64 * 1024 * 1024,
            max_workers: 8,
            queue_capacity: 1024,
            max_blocks_per_trigger: 8,
            sequential_tolerance: 64 * 1024,
            min_sequential_count: 2,
        }
    }
}

const MAX_ALLOWED_WINDOW: u64 = 2 * 1024 * 1024 * 1024; // 2 GiB

impl EngineConfig {
    /// Clamp `max_window` to the 2 GiB ceiling required by the spec.
    pub fn clamp(mut self) -> Self {
        if self.max_window > MAX_ALLOWED_WINDOW {
            self.max_window = MAX_ALLOWED_WINDOW;
        }
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python_constants() {
        let cfg = EngineConfig::default();
        assert_eq!(cfg.block_size, 4 * 1024 * 1024);
        assert_eq!(cfg.initial_window, 512 * 1024);
        assert_eq!(cfg.max_window, 64 * 1024 * 1024);
        assert_eq!(cfg.max_workers, 8);
        assert_eq!(cfg.max_blocks_per_trigger, 8);
        assert_eq!(cfg.sequential_tolerance, 64 * 1024);
        assert_eq!(cfg.min_sequential_count, 2);
    }

    #[test]
    fn clamp_caps_max_window_at_2gib() {
        let cfg = EngineConfig {
            max_window: 4 * 1024 * 1024 * 1024,
            ..Default::default()
        }
        .clamp();
        assert_eq!(cfg.max_window, 2 * 1024 * 1024 * 1024);
    }

    #[test]
    fn clamp_leaves_window_below_ceiling_unchanged() {
        let cfg = EngineConfig::default().clamp();
        assert_eq!(cfg.max_window, 64 * 1024 * 1024);
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib config`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/config.rs
git commit -m "feat(#4057): add EngineConfig with 2 GiB clamp"
```

---

### Task 5: `Detector` trait

**Files:**
- Modify: `rust/nexus-prefetch/src/detector.rs`
- Create: `rust/nexus-prefetch/src/detector/mod.rs` (turn `detector.rs` into a directory module)

- [ ] **Step 1: Delete the file stub and create a module directory**

```bash
rm rust/nexus-prefetch/src/detector.rs
mkdir -p rust/nexus-prefetch/src/detector
```

- [ ] **Step 2: Write `rust/nexus-prefetch/src/detector/mod.rs`**

```rust
//! Pluggable pattern detector trait.  Implementations:
//! - `SequentialDetector` (forward-progressing reads)
//! - `StrideDetector` (fixed-delta strided access)
//! - `MajorityTrendDetector` (Leap ATC'20)

use crate::pattern::AccessPattern;

pub mod sequential;
pub mod stride;
pub mod trend;

pub use sequential::SequentialDetector;
pub use stride::StrideDetector;
pub use trend::MajorityTrendDetector;

/// Detector contract — observe (offset, size) tuples in arrival order
/// and emit an `AccessPattern` recommendation.  Detectors are
/// per-file-handle, not shared.
pub trait Detector: Send + 'static {
    /// Feed one observation; receive a pattern hint.
    fn observe(&mut self, offset: u64, size: u32) -> AccessPattern;

    /// Reset internal state (e.g. on file close, or on engine wishing
    /// to restart pattern hunting).
    fn reset(&mut self);
}
```

- [ ] **Step 3: Update `lib.rs` re-exports** (no change needed — `pub mod detector;` already covers it)

- [ ] **Step 4: Create stubs for the three detector files so `cargo check` works**

```bash
for f in sequential stride trend; do
  echo "//! Stub for $f detector." > rust/nexus-prefetch/src/detector/$f.rs
done
```

Add minimal placeholder to each — `sequential.rs`:

```rust
//! Stub for sequential detector.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct SequentialDetector;

impl Detector for SequentialDetector {
    fn observe(&mut self, _offset: u64, _size: u32) -> AccessPattern { AccessPattern::Cold }
    fn reset(&mut self) {}
}
```

Repeat verbatim (with renamed struct `StrideDetector`, `MajorityTrendDetector`) for `stride.rs` and `trend.rs`. These are placeholders only to make compilation succeed; real impls land in Tasks 6–8.

- [ ] **Step 5: Verify compilation**

Run: `cargo check -p nexus-prefetch`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add rust/nexus-prefetch/src/
git commit -m "feat(#4057): add Detector trait + impl stubs"
```

---

### Task 6: `SequentialDetector` implementation

**Files:**
- Modify: `rust/nexus-prefetch/src/detector/sequential.rs`

- [ ] **Step 1: Write the failing test (replace file contents)**

```rust
//! Forward-sequential detector — mirrors the logic at
//! `src/nexus/fuse/readahead.py:194–235`.
//!
//! State: last (offset, size), sequential_count.  A read is sequential
//! if its offset lies within `tolerance` of `last_offset + last_size`
//! AND does not move backward.  After `min_sequential_count`
//! consecutive sequential observations we emit `Sequential`; otherwise
//! `Cold` (warming up) or `Random` (broken stream).

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct SequentialDetector {
    last_offset: u64,
    last_size: u32,
    sequential_count: u32,
    tolerance: u64,
    min_sequential_count: u32,
    initialised: bool,
}

impl SequentialDetector {
    pub fn new(tolerance: u64, min_sequential_count: u32) -> Self {
        Self {
            last_offset: 0,
            last_size: 0,
            sequential_count: 0,
            tolerance,
            min_sequential_count,
            initialised: false,
        }
    }
}

impl Detector for SequentialDetector {
    fn observe(&mut self, offset: u64, size: u32) -> AccessPattern {
        if !self.initialised {
            self.last_offset = offset;
            self.last_size = size;
            self.initialised = true;
            return AccessPattern::Cold;
        }

        let expected = self.last_offset.saturating_add(self.last_size as u64);
        let signed_diff = offset as i64 - expected as i64;
        let forward = offset >= self.last_offset;
        let within_tol = signed_diff.unsigned_abs() <= self.tolerance;

        let pattern = if forward && within_tol {
            self.sequential_count += 1;
            if self.sequential_count >= self.min_sequential_count {
                AccessPattern::Sequential
            } else {
                AccessPattern::Cold
            }
        } else {
            self.sequential_count = 0;
            AccessPattern::Random
        };

        self.last_offset = offset;
        self.last_size = size;
        pattern
    }

    fn reset(&mut self) {
        self.last_offset = 0;
        self.last_size = 0;
        self.sequential_count = 0;
        self.initialised = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det() -> SequentialDetector {
        SequentialDetector::new(64 * 1024, 2)
    }

    #[test]
    fn first_observation_is_cold() {
        let mut d = det();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
    }

    #[test]
    fn two_consecutive_sequential_reads_trigger_sequential() {
        let mut d = det();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
        assert_eq!(d.observe(4096, 4096), AccessPattern::Cold); // count=1, below threshold
        assert_eq!(d.observe(8192, 4096), AccessPattern::Sequential); // count=2
    }

    #[test]
    fn within_tolerance_still_sequential() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096); // count=1
        // Next read at 8192 + 32 KiB skip (< 64 KiB tolerance) — still seq.
        assert_eq!(d.observe(8192 + 32 * 1024, 4096), AccessPattern::Sequential);
    }

    #[test]
    fn backward_jump_is_random_and_resets() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.observe(8192, 4096);
        // Backward jump
        assert_eq!(d.observe(0, 4096), AccessPattern::Random);
        // Counter resets — next sequential pair must re-prime
        assert_eq!(d.observe(4096, 4096), AccessPattern::Cold);
        assert_eq!(d.observe(8192, 4096), AccessPattern::Sequential);
    }

    #[test]
    fn large_forward_gap_beyond_tolerance_is_random() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        // Jump 10 MiB ahead — far beyond 64 KiB tolerance.
        assert_eq!(d.observe(10 * 1024 * 1024, 4096), AccessPattern::Random);
    }

    #[test]
    fn reset_clears_state() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.reset();
        assert_eq!(d.observe(1024, 4096), AccessPattern::Cold);
    }
}
```

- [ ] **Step 2: Run tests, expect failure first**

Run: `cargo test -p nexus-prefetch --lib detector::sequential`
Expected: PASS after Step 1 (we wrote the impl alongside the tests; if any test fails, fix per the assertion message before continuing).

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/detector/sequential.rs
git commit -m "feat(#4057): SequentialDetector with reset-on-reorder"
```

---

### Task 7: `StrideDetector` implementation

**Files:**
- Modify: `rust/nexus-prefetch/src/detector/stride.rs`

- [ ] **Step 1: Write the implementation + tests (full replace)**

```rust
//! Fixed-stride detector.  Tracks the last two offsets; computes the
//! signed delta; if the next observation matches that delta (within
//! 1 block), emits `Stride { stride }`.  Three confirmations required
//! before `Stride` fires — single matches are still `Cold`.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

const MIN_CONFIRMATIONS: u32 = 3;

pub struct StrideDetector {
    history: [Option<u64>; 3], // ring buffer of last 3 offsets
    confirmations: u32,
    last_stride: Option<i64>,
}

impl Default for StrideDetector {
    fn default() -> Self { Self::new() }
}

impl StrideDetector {
    pub fn new() -> Self {
        Self { history: [None, None, None], confirmations: 0, last_stride: None }
    }

    fn push(&mut self, offset: u64) {
        self.history.rotate_left(1);
        self.history[2] = Some(offset);
    }
}

impl Detector for StrideDetector {
    fn observe(&mut self, offset: u64, _size: u32) -> AccessPattern {
        self.push(offset);

        let (Some(a), Some(b), Some(c)) = (self.history[0], self.history[1], self.history[2])
        else {
            return AccessPattern::Cold;
        };

        let s1 = b as i64 - a as i64;
        let s2 = c as i64 - b as i64;
        if s1 == 0 || s2 == 0 {
            self.confirmations = 0;
            self.last_stride = None;
            return AccessPattern::Cold;
        }

        if s1 == s2 {
            if self.last_stride == Some(s1) {
                self.confirmations += 1;
            } else {
                self.last_stride = Some(s1);
                self.confirmations = 1;
            }
            if self.confirmations >= MIN_CONFIRMATIONS - 1 {
                return AccessPattern::Stride { stride: s1 };
            }
            return AccessPattern::Cold;
        }

        self.confirmations = 0;
        self.last_stride = None;
        AccessPattern::Random
    }

    fn reset(&mut self) {
        self.history = [None, None, None];
        self.confirmations = 0;
        self.last_stride = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_stride_detected_after_three_confirms() {
        let mut d = StrideDetector::new();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold); // history: [_, _, 0]
        assert_eq!(d.observe(8192, 4096), AccessPattern::Cold); // [_, 0, 8192]
        // Third observation: triple (0, 8192, 16384) → s1=s2=8192, confirmations=1
        assert_eq!(d.observe(16384, 4096), AccessPattern::Cold);
        // Fourth: triple (8192, 16384, 24576) → s1=s2=8192, confirmations=2 → Stride
        assert_eq!(d.observe(24576, 4096), AccessPattern::Stride { stride: 8192 });
    }

    #[test]
    fn changing_stride_resets_to_random() {
        let mut d = StrideDetector::new();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.observe(8192, 4096); // s1=s2=4096, conf=1, Cold
        // Now break: jump by 16 KiB.
        assert_eq!(d.observe(8192 + 16 * 1024, 4096), AccessPattern::Random);
    }

    #[test]
    fn negative_stride_detected() {
        let mut d = StrideDetector::new();
        d.observe(100_000, 4096);
        d.observe(90_000, 4096);
        d.observe(80_000, 4096); // s1=s2=-10000, conf=1
        assert_eq!(d.observe(70_000, 4096), AccessPattern::Stride { stride: -10_000 });
    }

    #[test]
    fn zero_stride_is_not_a_pattern() {
        let mut d = StrideDetector::new();
        d.observe(1000, 4096);
        d.observe(1000, 4096);
        // s1=0 — should never emit Stride { stride: 0 }
        let p = d.observe(1000, 4096);
        assert!(matches!(p, AccessPattern::Cold));
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib detector::stride`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/detector/stride.rs
git commit -m "feat(#4057): StrideDetector with 3-observation confirm"
```

---

### Task 8: `MajorityTrendDetector` (Leap ATC'20)

**Files:**
- Modify: `rust/nexus-prefetch/src/detector/trend.rs`

- [ ] **Step 1: Write the implementation + tests**

```rust
//! Leap-style majority-trend detector (ATC'20 §3).  Keeps a sliding
//! window of the last N signed deltas; if more than half are equal to
//! some value `d`, emits `Trend { delta: d }`.  Tolerates jitter that
//! a strict stride detector would reject.

use std::collections::HashMap;
use crate::detector::Detector;
use crate::pattern::AccessPattern;

const WINDOW: usize = 8;

pub struct MajorityTrendDetector {
    last_offset: Option<u64>,
    deltas: std::collections::VecDeque<i64>,
}

impl Default for MajorityTrendDetector {
    fn default() -> Self { Self::new() }
}

impl MajorityTrendDetector {
    pub fn new() -> Self {
        Self { last_offset: None, deltas: std::collections::VecDeque::with_capacity(WINDOW) }
    }
}

impl Detector for MajorityTrendDetector {
    fn observe(&mut self, offset: u64, _size: u32) -> AccessPattern {
        let Some(prev) = self.last_offset else {
            self.last_offset = Some(offset);
            return AccessPattern::Cold;
        };
        let delta = offset as i64 - prev as i64;
        self.last_offset = Some(offset);

        if self.deltas.len() == WINDOW {
            self.deltas.pop_front();
        }
        self.deltas.push_back(delta);

        if self.deltas.len() < WINDOW / 2 {
            return AccessPattern::Cold;
        }

        let mut counts: HashMap<i64, usize> = HashMap::new();
        for d in &self.deltas {
            *counts.entry(*d).or_insert(0) += 1;
        }
        let (best_delta, best_count) = counts
            .iter()
            .max_by_key(|(_, c)| **c)
            .map(|(d, c)| (*d, *c))
            .unwrap();

        if best_count * 2 > self.deltas.len() {
            AccessPattern::Trend { delta: best_delta }
        } else {
            AccessPattern::Random
        }
    }

    fn reset(&mut self) {
        self.last_offset = None;
        self.deltas.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_observation_is_cold() {
        let mut d = MajorityTrendDetector::new();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
    }

    #[test]
    fn dominant_delta_wins_with_jitter() {
        let mut d = MajorityTrendDetector::new();
        // Offsets: 0, 4096, 8192, 12288, 16384, 99999 (noise), 24576, 28672
        // Deltas: +4096 ×5, +83615, +4097 — majority is +4096.
        for off in [0u64, 4096, 8192, 12288, 16384, 99999, 24576, 28672, 32768] {
            let _ = d.observe(off, 4096);
        }
        // After WINDOW=8 deltas accumulated, +4096 occurs 5×, majority.
        assert!(matches!(
            d.observe(36864, 4096),
            AccessPattern::Trend { delta: 4096 }
        ));
    }

    #[test]
    fn random_offsets_yield_random_pattern() {
        let mut d = MajorityTrendDetector::new();
        // Random offsets — no delta repeats.
        for off in [0u64, 100, 1_000_000, 42, 999_999, 17, 314_159, 27_182, 99_991] {
            let _ = d.observe(off, 4096);
        }
        // Tenth observation: no delta has majority.
        assert_eq!(
            d.observe(424_242, 4096),
            AccessPattern::Random
        );
    }

    #[test]
    fn reset_clears_state() {
        let mut d = MajorityTrendDetector::new();
        for off in (0..10).map(|i| i * 4096) {
            d.observe(off, 4096);
        }
        d.reset();
        assert_eq!(d.observe(123, 4096), AccessPattern::Cold);
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib detector::trend`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/detector/trend.rs
git commit -m "feat(#4057): MajorityTrendDetector (Leap ATC'20)"
```

---

### Task 9: `Session` — per-fh state with doubling window

**Files:**
- Modify: `rust/nexus-prefetch/src/session.rs`

- [ ] **Step 1: Write file with tests**

```rust
//! Per-file-handle state — the unit of book-keeping for one open fh.
//! Owns its own detector (boxed), the readahead window (doubles on
//! every confirmed sequential hit, capped at `max_window`), the set
//! of pending block offsets (to dedupe in-flight prefetches), and the
//! deposited buffer (block offset → bytes).

use std::collections::{BTreeMap, BTreeSet};
use bytes::Bytes;
use crate::detector::Detector;
use crate::config::EngineConfig;

pub struct Session {
    pub path: String,
    pub fh: u64,
    pub file_size: Option<u64>,
    pub window: u64,
    pub max_window: u64,
    pub detector: Box<dyn Detector>,
    pub pending: BTreeSet<u64>,
    pub buffer: BTreeMap<u64, Bytes>,
    pub hits: u64,
    pub misses: u64,
}

impl Session {
    pub fn new(
        path: String,
        fh: u64,
        file_size: Option<u64>,
        detector: Box<dyn Detector>,
        cfg: &EngineConfig,
    ) -> Self {
        Self {
            path,
            fh,
            file_size,
            window: cfg.initial_window,
            max_window: cfg.max_window,
            detector,
            pending: BTreeSet::new(),
            buffer: BTreeMap::new(),
            hits: 0,
            misses: 0,
        }
    }

    /// Double the window, capped at `max_window`.  Called after a
    /// confirmed sequential observation.
    pub fn grow_window(&mut self) {
        let doubled = self.window.saturating_mul(2);
        self.window = doubled.min(self.max_window);
    }

    /// Reset window to initial — invoked on `AccessPattern::Random`
    /// from the detector.  Also clears pending so we stop awaiting
    /// blocks the stream no longer cares about.
    pub fn shrink_and_clear(&mut self, initial_window: u64) {
        self.window = initial_window;
        self.pending.clear();
    }

    /// Mark a block offset as pending prefetch.  Returns `false` if
    /// already in flight or already buffered (caller should skip).
    pub fn mark_pending(&mut self, block_offset: u64) -> bool {
        if self.pending.contains(&block_offset) || self.buffer.contains_key(&block_offset) {
            return false;
        }
        self.pending.insert(block_offset);
        true
    }

    /// Deposit a completed block; remove from pending.
    pub fn deposit(&mut self, block_offset: u64, bytes: Bytes) {
        self.pending.remove(&block_offset);
        self.buffer.insert(block_offset, bytes);
    }

    /// Take a buffered range if it fully covers `[offset, offset+size)`.
    /// Returns `None` on miss.  Removes consumed blocks from the buffer.
    pub fn take_range(&mut self, offset: u64, size: u32, block_size: u32) -> Option<Bytes> {
        let end = offset + size as u64;
        let first_block = (offset / block_size as u64) * block_size as u64;
        let mut out = Vec::with_capacity(size as usize);
        let mut cursor = first_block;
        while cursor < end {
            let block = self.buffer.get(&cursor)?;
            let block_end = cursor + block.len() as u64;
            let take_from = offset.max(cursor) - cursor;
            let take_to = (end.min(block_end)) - cursor;
            out.extend_from_slice(&block[take_from as usize..take_to as usize]);
            cursor = block_end;
        }
        // Drop consumed blocks so the buffer doesn't grow unbounded.
        let consumed: Vec<u64> = self
            .buffer
            .range(first_block..end)
            .map(|(k, _)| *k)
            .collect();
        for k in consumed {
            self.buffer.remove(&k);
        }
        self.hits += 1;
        Some(Bytes::from(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detector::SequentialDetector;

    fn sess(cfg: &EngineConfig) -> Session {
        Session::new(
            "/x".into(),
            1,
            Some(1024 * 1024),
            Box::new(SequentialDetector::new(cfg.sequential_tolerance, cfg.min_sequential_count)),
            cfg,
        )
    }

    #[test]
    fn window_doubles_capped_at_max() {
        let cfg = EngineConfig { initial_window: 1024, max_window: 4096, ..Default::default() };
        let mut s = sess(&cfg);
        assert_eq!(s.window, 1024);
        s.grow_window();
        assert_eq!(s.window, 2048);
        s.grow_window();
        assert_eq!(s.window, 4096);
        s.grow_window();
        assert_eq!(s.window, 4096); // capped
    }

    #[test]
    fn shrink_resets_window_and_pending() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        s.window = 1024 * 1024;
        s.mark_pending(0);
        s.mark_pending(4096);
        s.shrink_and_clear(cfg.initial_window);
        assert_eq!(s.window, cfg.initial_window);
        assert!(s.pending.is_empty());
    }

    #[test]
    fn mark_pending_dedupes() {
        let cfg = EngineConfig::default();
        let mut s = sess(&cfg);
        assert!(s.mark_pending(0));
        assert!(!s.mark_pending(0));
    }

    #[test]
    fn take_range_hits_when_blocks_present() {
        let cfg = EngineConfig { block_size: 16, ..Default::default() };
        let mut s = sess(&cfg);
        s.deposit(0, Bytes::from(vec![1u8; 16]));
        s.deposit(16, Bytes::from(vec![2u8; 16]));
        let out = s.take_range(8, 16, 16).expect("hit");
        assert_eq!(out.len(), 16);
        assert_eq!(out[0], 1);
        assert_eq!(out[7], 1);
        assert_eq!(out[8], 2);
    }

    #[test]
    fn take_range_misses_when_block_absent() {
        let cfg = EngineConfig { block_size: 16, ..Default::default() };
        let mut s = sess(&cfg);
        assert!(s.take_range(0, 16, 16).is_none());
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib session`
Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/session.rs
git commit -m "feat(#4057): Session with doubling window + buffer"
```

---

### Task 10: `RangeReader` trait + mock

**Files:**
- Modify: `rust/nexus-prefetch/src/range_reader.rs`

- [ ] **Step 1: Write file with tests**

```rust
//! Abstraction over backend range reads.  The engine calls this for
//! every prefetch block.  Real impls wrap `ObjectStore::read_range`
//! (Rust kernel) or a Python callable (pyo3 bridge); tests use the
//! `MockRangeReader` below.

use std::sync::Arc;
use bytes::Bytes;
use crate::error::PrefetchError;

pub trait RangeReader: Send + Sync + 'static {
    fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError>;
}

/// Boilerplate-free `Arc` alias for the engine's stored reader.
pub type SharedRangeReader = Arc<dyn RangeReader>;

#[cfg(test)]
pub mod mock {
    use super::*;
    use parking_lot::Mutex;

    pub struct MockRangeReader {
        pub data: Bytes,
        pub call_log: Mutex<Vec<(String, u64, u32)>>,
    }

    impl MockRangeReader {
        pub fn new(data: Bytes) -> Self {
            Self { data, call_log: Mutex::new(Vec::new()) }
        }
    }

    impl RangeReader for MockRangeReader {
        fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
            self.call_log.lock().push((key.to_string(), offset, size));
            let end = (offset + size as u64).min(self.data.len() as u64) as usize;
            let start = offset as usize;
            if start >= self.data.len() {
                return Err(PrefetchError::OutOfRange {
                    offset,
                    size,
                    file_size: self.data.len() as u64,
                });
            }
            Ok(self.data.slice(start..end))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mock::MockRangeReader;

    #[test]
    fn mock_returns_slice() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8, 2, 3, 4, 5, 6, 7, 8]));
        let out = r.read("x", 2, 4).unwrap();
        assert_eq!(&out[..], &[3, 4, 5, 6]);
    }

    #[test]
    fn mock_out_of_range_errors() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8; 4]));
        assert!(matches!(
            r.read("x", 10, 4),
            Err(PrefetchError::OutOfRange { .. })
        ));
    }

    #[test]
    fn mock_logs_calls() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8; 16]));
        let _ = r.read("a", 0, 4);
        let _ = r.read("b", 4, 4);
        let log = r.call_log.lock();
        assert_eq!(log.len(), 2);
        assert_eq!(log[0], ("a".to_string(), 0, 4));
        assert_eq!(log[1], ("b".to_string(), 4, 4));
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib range_reader`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/range_reader.rs
git commit -m "feat(#4057): RangeReader trait + test mock"
```

---

### Task 11: `metrics::EngineMetrics`

**Files:**
- Modify: `rust/nexus-prefetch/src/metrics.rs`

- [ ] **Step 1: Write file with tests**

```rust
//! Process-wide engine counters.  Cheap atomic stores; exported via
//! `PrefetchEngine::metrics()` for observability and `pyo3` bridge.

use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Default)]
pub struct EngineMetrics {
    pub hits: AtomicU64,
    pub misses: AtomicU64,
    pub prefetched_bytes: AtomicU64,
    pub dropped_backpressure: AtomicU64,
    pub resets: AtomicU64,
}

impl EngineMetrics {
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            hits: self.hits.load(Ordering::Relaxed),
            misses: self.misses.load(Ordering::Relaxed),
            prefetched_bytes: self.prefetched_bytes.load(Ordering::Relaxed),
            dropped_backpressure: self.dropped_backpressure.load(Ordering::Relaxed),
            resets: self.resets.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MetricsSnapshot {
    pub hits: u64,
    pub misses: u64,
    pub prefetched_bytes: u64,
    pub dropped_backpressure: u64,
    pub resets: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_reflects_current_values() {
        let m = EngineMetrics::default();
        m.hits.fetch_add(7, Ordering::Relaxed);
        m.dropped_backpressure.fetch_add(2, Ordering::Relaxed);
        let s = m.snapshot();
        assert_eq!(s.hits, 7);
        assert_eq!(s.dropped_backpressure, 2);
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib metrics`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/metrics.rs
git commit -m "feat(#4057): EngineMetrics counters"
```

---

### Task 12: Worker loop

**Files:**
- Modify: `rust/nexus-prefetch/src/worker.rs`

- [ ] **Step 1: Write file (no unit tests — exercised via integration test in Task 14)**

```rust
//! Worker task — consumes prefetch jobs off a bounded mpsc, calls the
//! injected `RangeReader`, deposits bytes into the requesting session.
//!
//! Spawned `max_workers` times by the engine.  Loops until the
//! channel is closed by `PrefetchEngine::shutdown`.

use std::sync::Arc;
use tokio::sync::mpsc;
use dashmap::DashMap;
use parking_lot::Mutex;
use tracing::{debug, warn};
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use crate::metrics::EngineMetrics;

pub struct PrefetchJob {
    pub fh: u64,
    pub key: String,
    pub block_offset: u64,
    pub block_size: u32,
}

pub async fn run_worker(
    mut rx: mpsc::Receiver<PrefetchJob>,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    reader: SharedRangeReader,
    metrics: Arc<EngineMetrics>,
) {
    while let Some(job) = rx.recv().await {
        // Block-on the sync reader.  In future revisions we may switch
        // to an async-native trait, but the current `ObjectStore::read`
        // is sync (`rust/kernel/src/abc/object_store.rs:86`) so any
        // wrapper hops through `spawn_blocking` anyway.
        let reader_clone = reader.clone();
        let result = tokio::task::spawn_blocking(move || {
            reader_clone.read(&job.key, job.block_offset, job.block_size)
        })
        .await;

        match result {
            Ok(Ok(bytes)) => {
                let size = bytes.len() as u64;
                if let Some(slot) = sessions.get(&job.fh) {
                    let mut s = slot.lock();
                    s.deposit(job.block_offset, bytes);
                    metrics
                        .prefetched_bytes
                        .fetch_add(size, std::sync::atomic::Ordering::Relaxed);
                } else {
                    debug!(
                        fh = job.fh,
                        offset = job.block_offset,
                        "prefetch landed after release; dropping"
                    );
                }
            }
            Ok(Err(e)) => {
                warn!(error = %e, fh = job.fh, offset = job.block_offset, "prefetch read failed");
                if let Some(slot) = sessions.get(&job.fh) {
                    slot.lock().pending.remove(&job.block_offset);
                }
            }
            Err(join_err) => {
                warn!(error = %join_err, "prefetch worker join failed");
            }
        }
    }
}
```

- [ ] **Step 2: Verify compilation**

Run: `cargo check -p nexus-prefetch`
Expected: clean (downstream `engine.rs` not yet built; this file compiles via `pub mod worker;` in `lib.rs`).

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/worker.rs
git commit -m "feat(#4057): prefetch worker loop"
```

---

### Task 13: `PrefetchEngine`

**Files:**
- Modify: `rust/nexus-prefetch/src/engine.rs`

- [ ] **Step 1: Write the engine + unit tests**

```rust
//! Top-level prefetch orchestrator.  Owns session map, worker pool,
//! bounded mpsc.  Public methods mirror the Python `ReadaheadManager`
//! surface — `on_open`, `on_read`, `on_release`, `shutdown` — so the
//! Python shim swaps with zero call-site changes.

use std::sync::Arc;
use bytes::Bytes;
use dashmap::DashMap;
use parking_lot::Mutex;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::debug;
use std::sync::atomic::Ordering;

use crate::config::EngineConfig;
use crate::detector::{Detector, SequentialDetector};
use crate::metrics::{EngineMetrics, MetricsSnapshot};
use crate::pattern::AccessPattern;
use crate::range_reader::SharedRangeReader;
use crate::session::Session;
use crate::worker::{run_worker, PrefetchJob};

pub struct PrefetchEngine {
    cfg: EngineConfig,
    sessions: Arc<DashMap<u64, Arc<Mutex<Session>>>>,
    tx: mpsc::Sender<PrefetchJob>,
    metrics: Arc<EngineMetrics>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    runtime: Option<tokio::runtime::Runtime>,
    detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync>,
}

impl PrefetchEngine {
    /// Build with default Sequential detector and an owned tokio runtime.
    /// Caller can pass `None` for `runtime` if they want the engine to
    /// piggyback on an already-running runtime (it will then panic if
    /// `spawn` is called outside a runtime context).
    pub fn new(
        cfg: EngineConfig,
        reader: SharedRangeReader,
        runtime: Option<tokio::runtime::Runtime>,
    ) -> Self {
        let cfg = cfg.clamp();
        let metrics = Arc::new(EngineMetrics::default());
        let sessions = Arc::new(DashMap::new());
        let (tx, rx) = mpsc::channel(cfg.queue_capacity);
        let detector_factory: Box<dyn Fn() -> Box<dyn Detector> + Send + Sync> = {
            let tol = cfg.sequential_tolerance;
            let min = cfg.min_sequential_count;
            Box::new(move || Box::new(SequentialDetector::new(tol, min)))
        };

        let mut workers = Vec::with_capacity(cfg.max_workers);
        let rx = Arc::new(parking_lot::Mutex::new(Some(rx)));

        // Each worker owns the receiver via a Mutex-around-Option dance.
        // Only the first one to lock + take gets it; the others exit.
        // Cleaner pattern: split work into one consumer and use a
        // fan-out via task::spawn — but a single multi-consumer mpsc
        // doesn't exist in std-tokio, so we wrap the receiver and use
        // exactly one consumer that fans out per-job onto blocking-pool.
        let single_rx = rx.lock().take().expect("rx not yet taken");
        let h = match runtime.as_ref() {
            Some(rt) => rt.spawn(run_worker(
                single_rx,
                sessions.clone(),
                reader.clone(),
                metrics.clone(),
            )),
            None => tokio::spawn(run_worker(
                single_rx,
                sessions.clone(),
                reader.clone(),
                metrics.clone(),
            )),
        };
        workers.push(h);

        Self {
            cfg,
            sessions,
            tx,
            metrics,
            workers: Mutex::new(workers),
            runtime,
            detector_factory,
        }
    }

    pub fn on_open(&self, fh: u64, path: &str, file_size: Option<u64>) {
        let det = (self.detector_factory)();
        let sess = Session::new(path.to_string(), fh, file_size, det, &self.cfg);
        self.sessions.insert(fh, Arc::new(Mutex::new(sess)));
    }

    /// Return prefetched bytes covering `[offset, offset+size)` if
    /// the engine already has them; otherwise `None` (caller falls
    /// back to backend read).  Always feeds the observation into the
    /// detector and may enqueue further prefetch jobs.
    pub fn on_read(&self, fh: u64, offset: u64, size: u32) -> Option<Bytes> {
        let slot = self.sessions.get(&fh)?;
        let mut s = slot.lock();

        // Drive detector first so window decisions reflect current obs.
        let pattern = s.detector.observe(offset, size);
        match pattern {
            AccessPattern::Sequential | AccessPattern::Stride { .. } | AccessPattern::Trend { .. } => {
                s.grow_window();
                self.enqueue_prefetch(&mut s, offset, pattern);
            }
            AccessPattern::Random => {
                s.shrink_and_clear(self.cfg.initial_window);
                self.metrics.resets.fetch_add(1, Ordering::Relaxed);
            }
            AccessPattern::Cold => {}
        }

        let hit = s.take_range(offset, size, self.cfg.block_size);
        if hit.is_some() {
            self.metrics.hits.fetch_add(1, Ordering::Relaxed);
        } else {
            self.metrics.misses.fetch_add(1, Ordering::Relaxed);
            s.misses += 1;
        }
        hit
    }

    pub fn on_release(&self, fh: u64) {
        self.sessions.remove(&fh);
    }

    pub fn metrics(&self) -> MetricsSnapshot {
        self.metrics.snapshot()
    }

    pub fn shutdown(self) {
        drop(self.tx);
        let mut workers = self.workers.lock();
        for h in workers.drain(..) {
            h.abort();
        }
        // The owned runtime, if any, is dropped here — workers torn down.
    }

    fn enqueue_prefetch(&self, s: &mut Session, current_offset: u64, pattern: AccessPattern) {
        let block_size = self.cfg.block_size as u64;
        let first_block_offset = match pattern {
            AccessPattern::Sequential => ((current_offset / block_size) + 1) * block_size,
            AccessPattern::Stride { stride } if stride > 0 => {
                current_offset + stride as u64
            }
            AccessPattern::Stride { stride } if stride < 0 => {
                current_offset.saturating_sub((-stride) as u64)
            }
            AccessPattern::Trend { delta } if delta > 0 => current_offset + delta as u64,
            AccessPattern::Trend { delta } if delta < 0 => {
                current_offset.saturating_sub((-delta) as u64)
            }
            _ => return,
        };

        let max_offset = first_block_offset + s.window;
        let mut cur = first_block_offset;
        let mut issued = 0u32;
        while cur < max_offset && issued < self.cfg.max_blocks_per_trigger {
            if let Some(fs) = s.file_size {
                if cur >= fs {
                    break;
                }
            }
            if s.mark_pending(cur) {
                let job = PrefetchJob {
                    fh: s.fh,
                    key: s.path.clone(),
                    block_offset: cur,
                    block_size: self.cfg.block_size,
                };
                if let Err(_e) = self.tx.try_send(job) {
                    self.metrics
                        .dropped_backpressure
                        .fetch_add(1, Ordering::Relaxed);
                    s.pending.remove(&cur);
                    debug!(fh = s.fh, offset = cur, "queue full — dropping prefetch");
                    break;
                }
            }
            cur += block_size;
            issued += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::range_reader::mock::MockRangeReader;
    use std::time::Duration;

    fn build_engine(data: Vec<u8>, cfg: EngineConfig) -> PrefetchEngine {
        let reader: SharedRangeReader = Arc::new(MockRangeReader::new(Bytes::from(data)));
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        PrefetchEngine::new(cfg, reader, Some(rt))
    }

    #[test]
    fn miss_on_first_read_no_session() {
        let cfg = EngineConfig::default();
        let e = build_engine(vec![1u8; 1024], cfg);
        assert!(e.on_read(99, 0, 16).is_none());
    }

    #[test]
    fn sequential_workload_eventually_hits() {
        let cfg = EngineConfig {
            block_size: 16,
            initial_window: 64,
            max_window: 512,
            queue_capacity: 64,
            max_blocks_per_trigger: 4,
            sequential_tolerance: 0,
            min_sequential_count: 2,
            ..Default::default()
        };
        let e = build_engine(vec![7u8; 4096], cfg);
        e.on_open(1, "/x", Some(4096));

        // Warm-up reads (no prefetch yet).
        let _ = e.on_read(1, 0, 16);
        let _ = e.on_read(1, 16, 16);
        // After this read, sequential is confirmed and prefetch issues.
        let _ = e.on_read(1, 32, 16);

        // Give the worker time to deposit.
        std::thread::sleep(Duration::from_millis(200));

        // Next read should be a hit (block 48 was prefetched).
        let got = e.on_read(1, 48, 16);
        assert!(got.is_some(), "expected prefetched hit at offset 48");
        assert_eq!(&got.unwrap()[..], &[7u8; 16]);
    }

    #[test]
    fn release_removes_session() {
        let cfg = EngineConfig::default();
        let e = build_engine(vec![1u8; 1024], cfg);
        e.on_open(1, "/x", Some(1024));
        assert!(e.sessions.contains_key(&1));
        e.on_release(1);
        assert!(!e.sessions.contains_key(&1));
    }

    #[test]
    fn backpressure_drop_increments_metric() {
        // Tiny queue so the second job is rejected.
        let cfg = EngineConfig {
            block_size: 16,
            initial_window: 1024,
            max_window: 8192,
            queue_capacity: 1,
            max_blocks_per_trigger: 32,
            sequential_tolerance: 0,
            min_sequential_count: 2,
            max_workers: 1,
            ..Default::default()
        };
        let reader_data = vec![0u8; 1 << 20];
        // Throttle the reader so jobs back up.
        struct SlowReader(Bytes);
        impl crate::range_reader::RangeReader for SlowReader {
            fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, crate::error::PrefetchError> {
                std::thread::sleep(Duration::from_millis(50));
                let end = (off + sz as u64) as usize;
                Ok(self.0.slice(off as usize..end.min(self.0.len())))
            }
        }
        let reader: SharedRangeReader = Arc::new(SlowReader(Bytes::from(reader_data)));
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let e = PrefetchEngine::new(cfg, reader, Some(rt));
        e.on_open(1, "/x", Some(1 << 20));
        let _ = e.on_read(1, 0, 16);
        let _ = e.on_read(1, 16, 16);
        let _ = e.on_read(1, 32, 16); // triggers a big issue
        // Sleep less than the reader latency so queue stays full.
        std::thread::sleep(Duration::from_millis(10));
        let snap = e.metrics();
        assert!(snap.dropped_backpressure > 0, "expected drop counter > 0, got {snap:?}");
    }
}
```

- [ ] **Step 2: Run tests**

Run: `cargo test -p nexus-prefetch --lib engine -- --test-threads=1`
Expected: 4 passed.  (Single-threaded run avoids flakiness in the timing-sensitive backpressure test.)

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/engine.rs
git commit -m "feat(#4057): PrefetchEngine orchestrator"
```

---

### Task 14: Integration test — end-to-end sequential workload

**Files:**
- Create: `rust/nexus-prefetch/tests/integration.rs`

- [ ] **Step 1: Write the test file**

```rust
//! End-to-end: synthetic file → sequential read pattern → confirm
//! hit-ratio rises above 50% after warm-up window.

use std::sync::Arc;
use std::time::Duration;
use bytes::Bytes;
use nexus_prefetch::{EngineConfig, PrefetchEngine, RangeReader};
use nexus_prefetch::range_reader::SharedRangeReader;

struct VecReader(Bytes);

impl RangeReader for VecReader {
    fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, nexus_prefetch::PrefetchError> {
        let start = off as usize;
        let end = (off + sz as u64) as usize;
        if start >= self.0.len() {
            return Err(nexus_prefetch::PrefetchError::OutOfRange {
                offset: off,
                size: sz,
                file_size: self.0.len() as u64,
            });
        }
        Ok(self.0.slice(start..end.min(self.0.len())))
    }
}

#[test]
fn sequential_workload_majority_hits() {
    let file = vec![42u8; 1 << 20]; // 1 MiB
    let reader: SharedRangeReader = Arc::new(VecReader(Bytes::from(file)));
    let cfg = EngineConfig {
        block_size: 4096,
        initial_window: 16 * 1024,
        max_window: 256 * 1024,
        queue_capacity: 64,
        max_blocks_per_trigger: 8,
        sequential_tolerance: 0,
        min_sequential_count: 2,
        max_workers: 4,
        ..Default::default()
    };
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let engine = PrefetchEngine::new(cfg, reader, Some(rt));
    engine.on_open(1, "/big", Some(1 << 20));

    // 64 sequential 4-KiB reads.
    let mut hits = 0u32;
    let mut misses = 0u32;
    for i in 0..64u64 {
        let off = i * 4096;
        // Give workers a moment to settle after issuance.
        if i > 0 && i % 4 == 0 {
            std::thread::sleep(Duration::from_millis(20));
        }
        if engine.on_read(1, off, 4096).is_some() {
            hits += 1;
        } else {
            misses += 1;
        }
    }
    let ratio = hits as f64 / (hits + misses) as f64;
    assert!(
        ratio > 0.5,
        "expected majority hits after warmup, got hits={hits} misses={misses} ratio={ratio}"
    );
}
```

- [ ] **Step 2: Run the integration test**

Run: `cargo test -p nexus-prefetch --test integration`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/tests/integration.rs
git commit -m "test(#4057): integration test for sequential hit ratio"
```

---

### Task 15: Criterion benchmark

**Files:**
- Create: `rust/nexus-prefetch/benches/throughput.rs`

- [ ] **Step 1: Write the benchmark**

```rust
//! Throughput bench — feeds N sequential + N strided reads into the
//! engine to measure overall wall time.  Used as the acceptance gate
//! (sequential ≥1.5×, stride ≥1.3× vs no-prefetch baseline).

use std::sync::Arc;
use bytes::Bytes;
use criterion::{black_box, criterion_group, criterion_main, Criterion};
use nexus_prefetch::{EngineConfig, PrefetchEngine};
use nexus_prefetch::range_reader::{RangeReader, SharedRangeReader};

struct LatencyReader(Bytes, std::time::Duration);
impl RangeReader for LatencyReader {
    fn read(&self, _: &str, off: u64, sz: u32) -> Result<Bytes, nexus_prefetch::PrefetchError> {
        std::thread::sleep(self.1);
        let start = off as usize;
        let end = (off + sz as u64) as usize;
        Ok(self.0.slice(start..end.min(self.0.len())))
    }
}

fn bench_sequential(c: &mut Criterion) {
    let file = Bytes::from(vec![1u8; 64 * 1024 * 1024]);
    let cfg = EngineConfig {
        block_size: 64 * 1024,
        initial_window: 256 * 1024,
        max_window: 4 * 1024 * 1024,
        queue_capacity: 256,
        max_blocks_per_trigger: 8,
        max_workers: 8,
        ..Default::default()
    };

    c.bench_function("sequential_1mb_with_5ms_backend", |b| {
        b.iter(|| {
            let reader: SharedRangeReader =
                Arc::new(LatencyReader(file.clone(), std::time::Duration::from_millis(5)));
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(4)
                .enable_all()
                .build()
                .unwrap();
            let engine = PrefetchEngine::new(cfg.clone(), reader, Some(rt));
            engine.on_open(1, "/big", Some(file.len() as u64));
            for i in 0..16u64 {
                let off = i * 64 * 1024;
                let _ = black_box(engine.on_read(1, off, 64 * 1024));
            }
        })
    });
}

criterion_group!(benches, bench_sequential);
criterion_main!(benches);
```

- [ ] **Step 2: Verify the bench compiles + runs**

Run: `cargo bench -p nexus-prefetch --no-run`
Expected: clean compilation.  (Don't run `cargo bench` in CI — too slow.  It's a local acceptance tool.)

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/benches/throughput.rs
git commit -m "bench(#4057): criterion throughput bench"
```

---

### Phase 1 Gate

Before continuing to Phase 2, verify the whole crate is healthy:

- [ ] Run: `cargo test -p nexus-prefetch -- --test-threads=1`
  Expected: all tests pass (target ≥ 24).
- [ ] Run: `cargo clippy -p nexus-prefetch --all-targets -- -D warnings`
  Expected: no warnings.
- [ ] Run: `cargo fmt -p nexus-prefetch -- --check`
  Expected: clean.

**Phase 1 is now a shippable, self-contained Rust crate.**  Phases 2–5 wire it into the rest of the system.

---

## Phase 2: Extend `ObjectStore::read_range`

The Rust `ObjectStore` trait at `rust/kernel/src/abc/object_store.rs:86` exposes `read_content(content_id, ctx) -> Vec<u8>` — full-content only, no range.  We need a range-capable method so the prefetcher can issue partial GETs without buffering whole files.

### Task 16: Add `read_range` to `ObjectStore` trait

**Files:**
- Modify: `rust/kernel/src/abc/object_store.rs`

- [ ] **Step 1: Add the method with a default implementation**

Open `rust/kernel/src/abc/object_store.rs`.  Locate the `ObjectStore` trait (around line 86).  Add the following method **inside** the trait block, immediately after `read_content`:

```rust
    /// Read a contiguous byte range from a content object.
    ///
    /// Default impl reads the full content and slices.  Backends that
    /// can do native range GETs (S3, HTTP, local files) should override
    /// for perf — falling through to the default doubles read amplification
    /// on tiny prefetch blocks.
    fn read_range(
        &self,
        content_id: &str,
        offset: u64,
        size: u32,
        ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let whole = self.read_content(content_id, ctx)?;
        let start = offset as usize;
        let end = ((offset + size as u64) as usize).min(whole.len());
        if start >= whole.len() {
            return Ok(Vec::new());
        }
        Ok(whole[start..end].to_vec())
    }
```

- [ ] **Step 2: Add a unit test next to the trait (use a fake impl)**

Append to the same file:

```rust
#[cfg(test)]
mod read_range_default_tests {
    use super::*;
    use crate::kernel::OperationContext;

    struct FakeStore { data: Vec<u8> }
    impl ObjectStore for FakeStore {
        fn name(&self) -> &str { "fake" }
        fn read_content(&self, _: &str, _: &OperationContext) -> Result<Vec<u8>, StorageError> {
            Ok(self.data.clone())
        }
        fn get_content_size(&self, _: &str) -> Result<u64, StorageError> {
            Ok(self.data.len() as u64)
        }
        // implement remaining required methods returning trivial values...
    }

    #[test]
    fn default_read_range_slices_full_content() {
        let s = FakeStore { data: (0u8..16).collect() };
        let ctx = OperationContext::test();
        let r = s.read_range("x", 4, 8, &ctx).unwrap();
        assert_eq!(r, (4u8..12).collect::<Vec<_>>());
    }

    #[test]
    fn read_range_past_end_returns_empty() {
        let s = FakeStore { data: vec![1u8; 4] };
        let ctx = OperationContext::test();
        let r = s.read_range("x", 100, 8, &ctx).unwrap();
        assert!(r.is_empty());
    }
}
```

> ⚠️ **Cross-check**: the `// implement remaining required methods` comment in `FakeStore` is a placeholder — flesh out only the methods the `ObjectStore` trait actually requires (read the trait between line 86 and its closing `}` to enumerate them; add minimal `Ok(Default::default())` impls or `unimplemented!()` for any non-test-path methods).

- [ ] **Step 3: Run tests**

Run: `cargo test -p kernel --lib abc::object_store::read_range_default_tests`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add rust/kernel/src/abc/object_store.rs
git commit -m "feat(#4057): ObjectStore::read_range with default slice impl"
```

---

### Task 17: Override `read_range` in `cas_local` backend

**Files:**
- Modify: `rust/backends/src/storage/cas_local.rs`

- [ ] **Step 1: Locate the `impl ObjectStore for CasLocalBackend` block** in `cas_local.rs`.

- [ ] **Step 2: Add a `read_range` override that uses `std::fs::File::seek + read_exact`**

```rust
    fn read_range(
        &self,
        content_id: &str,
        offset: u64,
        size: u32,
        _ctx: &crate::kernel::OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        use std::io::{Read, Seek, SeekFrom};
        let path = self.path_for_content(content_id);
        let mut f = std::fs::File::open(&path)
            .map_err(|e| StorageError::NotFound(format!("{}: {}", path.display(), e)))?;
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| StorageError::IOError(e.to_string()))?;
        let mut buf = vec![0u8; size as usize];
        let n = f
            .read(&mut buf)
            .map_err(|e| StorageError::IOError(e.to_string()))?;
        buf.truncate(n);
        Ok(buf)
    }
```

> The exact method name `path_for_content` may differ in this codebase — open `cas_local.rs` and use whatever helper resolves a content_id to a filesystem path.  If no such helper exists, inline the path computation using `self.root_dir.join(...)` per surrounding conventions.

- [ ] **Step 3: Add an integration test**

Append (or extend, if test module exists) at the bottom of `cas_local.rs`:

```rust
#[cfg(test)]
mod read_range_tests {
    use super::*;
    // Test mirroring existing CasLocalBackend tests in this file.

    #[test]
    fn read_range_returns_offset_slice() {
        let tmp = tempfile::tempdir().unwrap();
        let backend = CasLocalBackend::new(tmp.path().to_path_buf());
        let ctx = crate::kernel::OperationContext::test();
        let content_id = backend.write_content(b"hello world!", "manual-id", &ctx, 0).unwrap();
        let got = backend.read_range(&content_id, 6, 5, &ctx).unwrap();
        assert_eq!(&got, b"world");
    }
}
```

> If `write_content`'s signature in this codebase differs (e.g. CAS hash auto-derived, ignoring the manual id), adapt accordingly by reading the existing test patterns in the same file.

- [ ] **Step 4: Run tests**

Run: `cargo test -p backends --lib storage::cas_local::read_range_tests`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add rust/backends/src/storage/cas_local.rs
git commit -m "feat(#4057): CasLocalBackend::read_range native impl"
```

---

### Task 18: Override `read_range` in `path_local` backend

**Files:**
- Modify: `rust/backends/src/storage/path_local.rs`

- [ ] **Step 1: Add the override mirroring Task 17**

Identical pattern — `File::open(self.resolve(content_id))` then seek + read — adapted to whatever path-resolution helper `path_local.rs` already uses.

- [ ] **Step 2: Add a test mirroring Task 17 Step 3.**

- [ ] **Step 3: Run tests**

Run: `cargo test -p backends --lib storage::path_local::read_range_tests`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add rust/backends/src/storage/path_local.rs
git commit -m "feat(#4057): PathLocalBackend::read_range native impl"
```

---

### Task 19: `remote.rs` — RPC range read

**Files:**
- Modify: `rust/backends/src/storage/remote.rs`

- [ ] **Step 1: Inspect the remote backend's proto definition** to see whether `ReadRange` is already on the wire.  Run `grep -rn "ReadRange\|read_range" /Users/tafeng/nexus/rust/transport/src/ /Users/tafeng/nexus/protos/ 2>/dev/null` and locate the RPC.

- [ ] **Step 2a: If a `ReadRange` RPC exists already**, wire it: open `remote.rs`, find `impl ObjectStore for RemoteBackend`, add a `read_range` that calls the existing RPC via the existing tonic channel.  Mirror the call shape of `read_content`.

- [ ] **Step 2b: If no `ReadRange` RPC exists**, fall through to the default — no override needed.  Note this in the commit message; a follow-up issue should add the wire-level range RPC.

- [ ] **Step 3: Add test** (only if 2a): a stubbed RPC test mirroring existing `RemoteBackend` tests in the same file.

- [ ] **Step 4: Run tests**

Run: `cargo test -p backends --lib storage::remote`
Expected: all existing tests still pass + new test passes (if 2a).

- [ ] **Step 5: Commit**

```bash
git add rust/backends/src/storage/remote.rs
git commit -m "feat(#4057): RemoteBackend::read_range (or note absence)"
```

---

### Task 20: `KernelRangeReader` — adapter that implements `nexus_prefetch::RangeReader`

**Files:**
- Create: `rust/kernel/src/prefetch_adapter.rs`
- Modify: `rust/kernel/src/lib.rs` (to register the new module)
- Modify: `rust/kernel/Cargo.toml` (add `nexus-prefetch` dep)

- [ ] **Step 1: Add the workspace dep**

In `rust/kernel/Cargo.toml`, add to `[dependencies]`:

```toml
nexus-prefetch = { workspace = true }
```

- [ ] **Step 2: Create the adapter**

`rust/kernel/src/prefetch_adapter.rs`:

```rust
//! Adapter that lets `nexus-prefetch` issue range GETs through the
//! kernel's `ObjectStore` pillar.  Wraps a `(Backend, OperationContext)`
//! pair and implements `RangeReader`.

use std::sync::Arc;
use bytes::Bytes;
use nexus_prefetch::{PrefetchError, RangeReader};
use crate::abc::object_store::ObjectStore;
use crate::kernel::OperationContext;

pub struct KernelRangeReader {
    backend: Arc<dyn ObjectStore>,
    ctx: OperationContext,
}

impl KernelRangeReader {
    pub fn new(backend: Arc<dyn ObjectStore>, ctx: OperationContext) -> Self {
        Self { backend, ctx }
    }
}

impl RangeReader for KernelRangeReader {
    fn read(&self, content_id: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
        self.backend
            .read_range(content_id, offset, size, &self.ctx)
            .map(Bytes::from)
            .map_err(|e| PrefetchError::Backend(format!("{e:?}")))
    }
}
```

- [ ] **Step 3: Register the module**

In `rust/kernel/src/lib.rs`, add at module scope:

```rust
pub mod prefetch_adapter;
```

- [ ] **Step 4: Write a unit test**

At the bottom of `rust/kernel/src/prefetch_adapter.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::abc::object_store::StorageError;

    struct ConstBackend(Vec<u8>);
    impl ObjectStore for ConstBackend {
        fn name(&self) -> &str { "const" }
        fn read_content(&self, _: &str, _: &OperationContext) -> Result<Vec<u8>, StorageError> {
            Ok(self.0.clone())
        }
        fn get_content_size(&self, _: &str) -> Result<u64, StorageError> { Ok(self.0.len() as u64) }
        // fill in trait method stubs as in Task 16
    }

    #[test]
    fn adapter_returns_slice() {
        let backend: Arc<dyn ObjectStore> = Arc::new(ConstBackend((0u8..16).collect()));
        let ctx = OperationContext::test();
        let r = KernelRangeReader::new(backend, ctx);
        let out = <KernelRangeReader as RangeReader>::read(&r, "x", 4, 8).unwrap();
        assert_eq!(&out[..], &(4u8..12).collect::<Vec<_>>()[..]);
    }
}
```

- [ ] **Step 5: Run tests**

Run: `cargo test -p kernel --lib prefetch_adapter`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/prefetch_adapter.rs rust/kernel/src/lib.rs rust/kernel/Cargo.toml
git commit -m "feat(#4057): KernelRangeReader adapter"
```

---

## Phase 3: pyo3 bridge + drop-in Python shim

### Task 21: `pyo3_bindings.rs` — `PyPrefetchEngine`

**Files:**
- Modify: `rust/nexus-prefetch/src/pyo3_bindings.rs`

- [ ] **Step 1: Replace stub with real bindings**

```rust
//! pyo3 bridge exposing `PyPrefetchEngine` to Python.  Compiled only
//! under the `python` feature.  The engine takes a Python callable
//! `(key: str, offset: int, size: int) -> bytes` as its RangeReader,
//! so the Python `ReadaheadManager` shim can forward to
//! `read_range_from_backend` without round-tripping through Rust
//! backend dispatch.

#![cfg(feature = "python")]

use std::sync::Arc;
use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use crate::{EngineConfig, PrefetchEngine, PrefetchError, RangeReader};

struct PyCallableReader {
    callable: PyObject,
}

impl RangeReader for PyCallableReader {
    fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
        Python::with_gil(|py| -> Result<Bytes, PrefetchError> {
            let res = self
                .callable
                .call1(py, (key, offset, size))
                .map_err(|e| PrefetchError::Backend(format!("python read failed: {e}")))?;
            let bytes = res
                .extract::<&PyBytes>(py)
                .map_err(|e| PrefetchError::Backend(format!("expected bytes from py read: {e}")))?;
            Ok(Bytes::copy_from_slice(bytes.as_bytes()))
        })
    }
}

#[pyclass(name = "PrefetchEngine", module = "nexus_runtime")]
pub struct PyPrefetchEngine {
    inner: Option<PrefetchEngine>,
}

#[pymethods]
impl PyPrefetchEngine {
    #[new]
    #[pyo3(signature = (read_callable, block_size, initial_window, max_window, max_workers, queue_capacity, max_blocks_per_trigger, sequential_tolerance, min_sequential_count))]
    fn new(
        read_callable: PyObject,
        block_size: u32,
        initial_window: u64,
        max_window: u64,
        max_workers: usize,
        queue_capacity: usize,
        max_blocks_per_trigger: u32,
        sequential_tolerance: u64,
        min_sequential_count: u32,
    ) -> PyResult<Self> {
        let cfg = EngineConfig {
            block_size,
            initial_window,
            max_window,
            max_workers,
            queue_capacity,
            max_blocks_per_trigger,
            sequential_tolerance,
            min_sequential_count,
        };
        let reader: Arc<dyn RangeReader> = Arc::new(PyCallableReader { callable: read_callable });
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(cfg.max_workers)
            .enable_all()
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rt build: {e}")))?;
        let engine = PrefetchEngine::new(cfg, reader, Some(rt));
        Ok(Self { inner: Some(engine) })
    }

    fn on_open(&self, fh: u64, path: &str, file_size: Option<u64>) {
        if let Some(e) = self.inner.as_ref() {
            e.on_open(fh, path, file_size);
        }
    }

    fn on_read<'py>(
        &self,
        py: Python<'py>,
        fh: u64,
        offset: u64,
        size: u32,
    ) -> Option<&'py PyBytes> {
        let e = self.inner.as_ref()?;
        let b = e.on_read(fh, offset, size)?;
        Some(PyBytes::new(py, &b))
    }

    fn on_release(&self, fh: u64) {
        if let Some(e) = self.inner.as_ref() {
            e.on_release(fh);
        }
    }

    fn metrics(&self) -> PyResult<(u64, u64, u64, u64, u64)> {
        let e = self
            .inner
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("engine shut down"))?;
        let s = e.metrics();
        Ok((s.hits, s.misses, s.prefetched_bytes, s.dropped_backpressure, s.resets))
    }

    fn shutdown(&mut self) {
        if let Some(e) = self.inner.take() {
            e.shutdown();
        }
    }
}
```

- [ ] **Step 2: Verify the bindings compile**

Run: `cargo check -p nexus-prefetch --features python`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add rust/nexus-prefetch/src/pyo3_bindings.rs
git commit -m "feat(#4057): pyo3 bindings for PrefetchEngine"
```

---

### Task 22: Register `PyPrefetchEngine` in `nexus-cdylib`

**Files:**
- Modify: `rust/nexus-cdylib/Cargo.toml`
- Modify: `rust/nexus-cdylib/src/lib.rs`

- [ ] **Step 1: Add dep**

In `rust/nexus-cdylib/Cargo.toml`, under `[dependencies]`:

```toml
nexus-prefetch = { workspace = true, features = ["python"] }
```

- [ ] **Step 2: Register class** — read `rust/nexus-cdylib/src/lib.rs` to find the `#[pymodule]` function (search for `fn nexus_runtime(`).  Add inside the function body:

```rust
    m.add_class::<nexus_prefetch::pyo3_bindings::PyPrefetchEngine>()?;
```

- [ ] **Step 3: Verify the wheel compiles**

Run: `cargo check -p nexus-cdylib`
Expected: clean.

- [ ] **Step 4: Build the wheel and confirm the class is exposed**

Run: `cd /Users/tafeng/nexus/.claude/worktrees/wiggly-cuddling-raven && maturin develop --release -m rust/nexus-cdylib/Cargo.toml`
Expected: build succeeds.

Then: `python -c "from nexus_runtime import PrefetchEngine; print(PrefetchEngine)"`
Expected: prints `<class 'builtins.PrefetchEngine'>` (or similar).

- [ ] **Step 5: Commit**

```bash
git add rust/nexus-cdylib/Cargo.toml rust/nexus-cdylib/src/lib.rs
git commit -m "feat(#4057): expose PrefetchEngine to Python via cdylib"
```

---

### Task 23: Python shim — route `ReadaheadManager` to `PrefetchEngine`

**Files:**
- Modify: `src/nexus/fuse/readahead.py`
- Test: `tests/unit/fuse/test_readahead_shim.py` (create)

- [ ] **Step 1: Write a failing test**

Create `tests/unit/fuse/test_readahead_shim.py`:

```python
"""Verifies ReadaheadManager routes to the Rust PrefetchEngine when enabled."""

import pytest

from nexus.fuse.readahead import ReadaheadConfig, ReadaheadManager


@pytest.fixture
def synthetic_file_bytes():
    return b"\x42" * (256 * 1024)


def test_rust_engine_serves_sequential_hits(synthetic_file_bytes):
    config = ReadaheadConfig(
        enabled=True,
        block_size=4096,
        prefetch_workers=2,
        min_sequential_count=2,
        initial_window=16 * 1024,
        max_window=128 * 1024,
        sequential_tolerance=0,
        max_blocks_per_trigger=4,
    )

    def read_func(path, offset, size):
        return synthetic_file_bytes[offset : offset + size]

    rm = ReadaheadManager(config=config, read_func=read_func, use_rust_engine=True)
    fh = 1
    rm.on_open(fh, "/synthetic", file_size=len(synthetic_file_bytes))

    # Two warm-up reads — no hits expected.
    assert rm.on_read(fh, "/synthetic", 0, 4096) is None
    assert rm.on_read(fh, "/synthetic", 4096, 4096) is None
    # Third read triggers prefetch; give it a moment to land.
    assert rm.on_read(fh, "/synthetic", 8192, 4096) is None
    import time
    time.sleep(0.2)
    # Subsequent read should be a hit.
    got = rm.on_read(fh, "/synthetic", 12288, 4096)
    assert got is not None
    assert got == synthetic_file_bytes[12288:16384]
    rm.on_release(fh)
```

- [ ] **Step 2: Run, expect failure** (Rust-routing flag doesn't exist yet)

Run: `pytest tests/unit/fuse/test_readahead_shim.py -x`
Expected: FAIL — `ReadaheadManager` rejects `use_rust_engine` kwarg.

- [ ] **Step 3: Modify `ReadaheadManager`**

In `src/nexus/fuse/readahead.py`, add a new branch in `__init__` (after the existing `self._sessions = {}` initialisation at ~line 583):

```python
    def __init__(
        self,
        config: "ReadaheadConfig",
        read_func: Callable[[str, int, int], bytes],
        local_disk_cache: "LocalDiskCache | None" = None,
        content_hash_func: Callable[[str], str | None] | None = None,
        zone_id: str | None = None,
        use_rust_engine: bool = False,
    ):
        # ... existing init body ...
        self._rust_engine = None
        if use_rust_engine:
            try:
                from nexus_runtime import PrefetchEngine as _RustEngine

                self._rust_engine = _RustEngine(
                    read_func,
                    config.block_size,
                    config.initial_window,
                    config.max_window,
                    config.prefetch_workers,
                    1024,  # queue_capacity
                    config.max_blocks_per_trigger,
                    config.sequential_tolerance,
                    config.min_sequential_count,
                )
            except ImportError:
                logger.warning("[READAHEAD] nexus_runtime.PrefetchEngine unavailable, falling back to Python")
                self._rust_engine = None
```

Then in `on_open`, `on_read`, `on_release`, add an early-return shortcut at the top of each method:

```python
    def on_open(self, fh: int, path: str, file_size: int | None = None) -> None:
        if self._rust_engine is not None:
            self._rust_engine.on_open(fh, path, file_size)
            return
        # ... existing body ...

    def on_read(self, fh: int, path: str, offset: int, size: int) -> bytes | None:
        if self._rust_engine is not None:
            return self._rust_engine.on_read(fh, offset, size)
        # ... existing body ...

    def on_release(self, fh: int) -> None:
        if self._rust_engine is not None:
            self._rust_engine.on_release(fh)
            return
        # ... existing body ...
```

> **Note:** the Rust engine ignores `path` on `on_read` because the session stores it; the Python signature keeps `path` for API compatibility.

- [ ] **Step 4: Run, expect pass**

Run: `pytest tests/unit/fuse/test_readahead_shim.py -x`
Expected: PASS.

- [ ] **Step 5: Run the broader readahead suite to confirm no regression**

Run: `pytest tests/unit/fuse/ -k readahead -x`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/fuse/readahead.py tests/unit/fuse/test_readahead_shim.py
git commit -m "feat(#4057): ReadaheadManager shim routes to Rust PrefetchEngine"
```

---

### Task 24: Flip the call site to enable the Rust engine

**Files:**
- Modify: `src/nexus/fuse/operations.py`

- [ ] **Step 1: Find the ReadaheadManager construction** at ~line 205–212:

```python
readahead = ReadaheadManager(
    config=readahead_config,
    read_func=lambda path, offset, size: read_range_from_backend(
        self._ctx, path, offset, size
    ),
    local_disk_cache=local_disk_cache,
    …
)
```

- [ ] **Step 2: Add the toggle, defaulted on**

```python
readahead = ReadaheadManager(
    config=readahead_config,
    read_func=lambda path, offset, size: read_range_from_backend(
        self._ctx, path, offset, size
    ),
    local_disk_cache=local_disk_cache,
    zone_id=zone_id,
    use_rust_engine=os.environ.get("NEXUS_PREFETCH_RUST", "1") != "0",
)
```

`use_rust_engine=True` by default; `NEXUS_PREFETCH_RUST=0` flips back to Python.

- [ ] **Step 3: Run the FUSE op tests**

Run: `pytest tests/unit/fuse/ops/ -x`
Expected: all green.

- [ ] **Step 4: Manual smoke test (if a local FUSE mount is available)**

Run: `nexus up` (per `Skill: nexus-stack`); mount; do `cat /mnt/largefile | wc -c` for a 64 MiB file twice; check `dmesg` / logs for `READAHEAD HIT` lines.  This validates the full path.  Skip if no FUSE env handy.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/fuse/operations.py
git commit -m "feat(#4057): enable Rust prefetcher by default via env toggle"
```

---

## Phase 4: Kernel-side hint emission

For backend reads that go through Rust `sys_read` without crossing the FUSE Python boundary (e.g. peer-blob fetches, future Rust-native readers), the kernel itself should emit prefetch hints so the engine is fed even when Python is bypassed.

### Task 25: `PrefetchHintSink` trait

**Files:**
- Create: `rust/kernel/src/prefetch_hint.rs`
- Modify: `rust/kernel/src/lib.rs`

- [ ] **Step 1: Define the sink trait**

```rust
//! Kernel-side prefetch hint emission.  The kernel doesn't own the
//! engine — the cdylib does — so this trait is the cut.  A concrete
//! sink is installed from Python after the engine is constructed.

pub trait PrefetchHintSink: Send + Sync {
    /// Notify that a DT_REG read just completed.  The sink is expected
    /// to be cheap (≤ 100 ns); never block.
    fn on_read(&self, path: &str, offset: u64, size: u32);
}

/// No-op sink — used when prefetch is disabled or not yet installed.
pub struct NullSink;
impl PrefetchHintSink for NullSink {
    fn on_read(&self, _: &str, _: u64, _: u32) {}
}
```

Register in `lib.rs`:

```rust
pub mod prefetch_hint;
```

- [ ] **Step 2: Add a field to `Kernel`**

Find `struct Kernel` (search `rust/kernel/src/kernel/mod.rs` for `pub struct Kernel`).  Add:

```rust
    pub(crate) prefetch_sink: parking_lot::RwLock<std::sync::Arc<dyn crate::prefetch_hint::PrefetchHintSink>>,
```

In the constructor, initialise to `NullSink`:

```rust
    prefetch_sink: parking_lot::RwLock::new(std::sync::Arc::new(crate::prefetch_hint::NullSink)),
```

Add a setter:

```rust
impl Kernel {
    pub fn set_prefetch_sink(&self, sink: std::sync::Arc<dyn crate::prefetch_hint::PrefetchHintSink>) {
        *self.prefetch_sink.write() = sink;
    }
}
```

- [ ] **Step 3: Emit hint from `sys_read` DT_REG path**

In `rust/kernel/src/kernel/io.rs`, at line ~265 (just before `Ok(SysReadResult { ...data: Some(data)... entry_type: DT_REG ... })`), insert:

```rust
        // §4057: emit prefetch hint after successful DT_REG read.
        if let Some(data_ref) = content.as_ref() {
            let sink = self.prefetch_sink.read().clone();
            sink.on_read(path, offset, data_ref.len() as u32);
        }
```

- [ ] **Step 4: Add a unit test**

`rust/kernel/src/prefetch_hint.rs` append:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use parking_lot::Mutex;

    struct RecordingSink(Mutex<Vec<(String, u64, u32)>>);
    impl PrefetchHintSink for RecordingSink {
        fn on_read(&self, path: &str, off: u64, sz: u32) {
            self.0.lock().push((path.to_string(), off, sz));
        }
    }

    #[test]
    fn null_sink_is_a_noop() {
        let s = NullSink;
        s.on_read("/x", 0, 4); // no panic, no crash
    }

    #[test]
    fn recording_sink_captures_call() {
        let s = Arc::new(RecordingSink(Mutex::new(vec![])));
        (s.as_ref() as &dyn PrefetchHintSink).on_read("/p", 8, 16);
        let log = s.0.lock();
        assert_eq!(log.len(), 1);
        assert_eq!(log[0], ("/p".to_string(), 8, 16));
    }
}
```

- [ ] **Step 5: Run tests**

Run: `cargo test -p kernel --lib prefetch_hint`
Expected: 2 passed.

Run: `cargo test -p kernel` (full kernel suite — verify the sys_read patch didn't break anything)
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add rust/kernel/src/prefetch_hint.rs rust/kernel/src/lib.rs rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs
git commit -m "feat(#4057): Kernel emits prefetch hints from sys_read"
```

---

### Task 26: Bridge — Rust engine implements `PrefetchHintSink`

**Files:**
- Modify: `rust/nexus-prefetch/src/engine.rs`
- Modify: `rust/nexus-prefetch/src/pyo3_bindings.rs`

- [ ] **Step 1: Implement `PrefetchHintSink` for an Arc-wrapped engine**

At the bottom of `rust/nexus-prefetch/src/engine.rs`, add (gated on a feature to avoid the cyclic dep):

```rust
// Cross-crate trait impl lives in the kernel crate (KernelHintSink) since
// `nexus-prefetch` doesn't depend on `kernel`.  See `rust/kernel/src/prefetch_hint.rs`
// for the impl that wires an Arc<PrefetchEngine> into the sink trait.
```

- [ ] **Step 2: Add the impl in `rust/kernel/src/prefetch_hint.rs`**

Append:

```rust
/// Adapter — wraps a `nexus_prefetch::PrefetchEngine` into a sink.
/// The engine internally tracks per-fh state by file handle, but the
/// kernel only knows the *path* at the sys_read level.  We therefore
/// key the engine sessions by a path-hash (u64) and hand that out
/// as the synthetic fh.  This is a one-way fire-and-forget channel —
/// the kernel can emit hints but does not consume them.
pub struct KernelEngineSink {
    engine: std::sync::Arc<nexus_prefetch::PrefetchEngine>,
    path_to_fh: dashmap::DashMap<String, u64>,
    next_fh: std::sync::atomic::AtomicU64,
}

impl KernelEngineSink {
    pub fn new(engine: std::sync::Arc<nexus_prefetch::PrefetchEngine>) -> Self {
        Self {
            engine,
            path_to_fh: dashmap::DashMap::new(),
            next_fh: std::sync::atomic::AtomicU64::new(1),
        }
    }

    fn fh_for(&self, path: &str) -> u64 {
        if let Some(v) = self.path_to_fh.get(path) {
            return *v;
        }
        let fh = self.next_fh.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        self.path_to_fh.insert(path.to_string(), fh);
        self.engine.on_open(fh, path, None);
        fh
    }
}

impl PrefetchHintSink for KernelEngineSink {
    fn on_read(&self, path: &str, offset: u64, size: u32) {
        let fh = self.fh_for(path);
        let _ = self.engine.on_read(fh, offset, size);
    }
}
```

- [ ] **Step 3: Test**

Append at the bottom of `rust/kernel/src/prefetch_hint.rs`:

```rust
#[cfg(test)]
mod engine_sink_tests {
    use super::*;
    use nexus_prefetch::{EngineConfig, PrefetchEngine, RangeReader, PrefetchError};
    use bytes::Bytes;
    use std::sync::Arc;

    struct Noop;
    impl RangeReader for Noop {
        fn read(&self, _: &str, _: u64, _: u32) -> Result<Bytes, PrefetchError> {
            Ok(Bytes::from_static(b""))
        }
    }

    #[test]
    fn kernel_sink_opens_session_lazily_on_first_hint() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let engine = Arc::new(PrefetchEngine::new(
            EngineConfig::default(),
            Arc::new(Noop) as Arc<dyn RangeReader>,
            Some(rt),
        ));
        let sink = KernelEngineSink::new(engine.clone());
        sink.on_read("/x", 0, 4096);
        sink.on_read("/x", 4096, 4096);
        // No assertion on outcome — metrics race; the assertion is that
        // it didn't panic and lazily opened a session.
        assert!(sink.path_to_fh.contains_key("/x"));
    }
}
```

- [ ] **Step 4: Run tests**

Run: `cargo test -p kernel --lib prefetch_hint`
Expected: 3 passed (including the new one).

- [ ] **Step 5: Commit**

```bash
git add rust/kernel/src/prefetch_hint.rs
git commit -m "feat(#4057): KernelEngineSink bridges hint trait to PrefetchEngine"
```

---

## Phase 5: Acceptance benchmarks + docs

### Task 27: Run the benchmark for the acceptance gate

**Files:** none modified.

- [ ] **Step 1: Run the baseline (Python ReadaheadManager)**

Run: `NEXUS_PREFETCH_RUST=0 pytest tests/perf/test_readahead_throughput.py -k sequential -v --tb=short` (or whichever existing perf harness lives in `tests/perf/`).

> If no perf harness exists for readahead, fall back to a micro-bench: `cargo bench -p nexus-prefetch` and compare runtimes for `sequential_1mb_with_5ms_backend` between (a) the engine running and (b) a one-shot variant of the bench that disables prefetch by setting `cfg.max_blocks_per_trigger = 0`.

Record the ms/op number for the sequential bench.

- [ ] **Step 2: Run with Rust prefetch**

Run: `cargo bench -p nexus-prefetch`

Record the ms/op number.

- [ ] **Step 3: Verify acceptance**

Check that:
- Sequential bench ≥ 1.5× faster.
- Stride bench (add a second criterion `bench_stride` mirroring `bench_sequential` with strided offsets) ≥ 1.3× faster.
- Run an additional bench with random 4-KiB reads on a small file (≤ 1 MiB) and confirm no regression vs baseline.

If any gate fails, stop and investigate — file an issue note before merging.

- [ ] **Step 4: Commit the bench updates if any were added**

```bash
git add rust/nexus-prefetch/benches/
git commit -m "bench(#4057): add stride + random-small-file bench cases"
```

---

### Task 28: Update top-level docs / changelog

**Files:**
- Modify: `KERNEL-ARCHITECTURE.md` (if it documents the read path; otherwise skip)
- Modify: `CHANGELOG.md` if present.

- [ ] **Step 1: Read** the relevant section of `KERNEL-ARCHITECTURE.md` (search for `ReadaheadManager`, `prefetch`, `sys_read`).

- [ ] **Step 2: Add a paragraph** describing the new `nexus-prefetch` crate, the detector trait, and the `NEXUS_PREFETCH_RUST` env toggle.  Reference issue #4057.

- [ ] **Step 3: Commit**

```bash
git add KERNEL-ARCHITECTURE.md CHANGELOG.md
git commit -m "docs(#4057): document Rust prefetcher in kernel architecture"
```

---

### Task 29: Final acceptance verification

- [ ] Run: `cargo test --workspace -- --test-threads=1`
  Expected: full Rust workspace green.
- [ ] Run: `pytest tests/unit/fuse/ -x`
  Expected: all FUSE unit tests pass.
- [ ] Run: `cargo clippy --workspace --all-targets -- -D warnings`
  Expected: no warnings.

If everything is green, open the PR per `superpowers:finishing-a-development-branch`.

---

## Acceptance Criteria Mapping

| Acceptance criterion (from issue #4057) | Implemented in |
|---|---|
| New crate with detector trait + 3 impls (Sequential / Stride / MajorityTrend) | Tasks 1, 5–8 |
| Per-fh state, doubling window, capped at 2 GiB | Tasks 4 (clamp), 9 (grow_window) |
| Backpressure on cache pressure | Tasks 4 (queue_capacity), 13 (try_send + dropped_backpressure metric) |
| Reset on out-of-order | Tasks 6 (SequentialDetector backward-jump returns Random), 9 (shrink_and_clear), 13 (Random→shrink) |
| Wired into FUSE read path | Tasks 21–24 (pyo3 + ReadaheadManager shim + operations.py toggle) |
| Benchmark: sequential ≥ 1.5×, stride ≥ 1.3× | Tasks 15, 27 |
| No regression on small-file random reads | Task 27 third bench case |

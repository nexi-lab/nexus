# RUST IMPLEMENTATION CROSS-REFERENCE

## Addendum to Nexus Optimization Master Plan

**Date:** December 26, 2025
**Purpose:** Cross-reference proposed optimizations with existing Rust implementation

---

## RUST IMPLEMENTATION STATUS

### Already Implemented in Rust ✅

| Optimization | File | Lines | Performance |
|--------------|------|-------|-------------|
| **String Interning** | `rust/nexus_fast/src/lib.rs` | 25-48, 134-167 | 4x memory reduction per symbol |
| **Graph Caching (Issue #862)** | `rust/nexus_fast/src/lib.rs` | 30-36, 764-777 | 90%+ reduction in graph rebuild |
| **LRU Namespace Config (Issue #861)** | `rust/nexus_fast/src/lib.rs` | 38-48, 871-883 | Avoids repeated JSON parsing |
| **Memoization Cache** | `rust/nexus_fast/src/lib.rs` | 106-108, 162-164 | Prevents recomputation within request |
| **Lock-Free Concurrent Cache** | `rust/nexus_fast/src/lib.rs` | 163-164 | DashMap for parallel execution |
| **Parallel Permission Checks** | `rust/nexus_fast/src/lib.rs` | 51-52, 896-945 | Rayon parallelization (>50 items) |
| **AHashMap** | `rust/nexus_fast/src/lib.rs` | 3, 106 | 2-3x faster than std HashMap |
| **SIMD Grep (Issue #863)** | `rust/nexus_fast/src/lib.rs` | 7, memchr | 4-10x faster literal search |
| **SIMD UTF-8 (Issue #864)** | `rust/nexus_fast/src/lib.rs` | 14 | ~8x faster validation |
| **Memory-Mapped File I/O** | `rust/nexus_fast/src/lib.rs` | 8, 2068-2070 | Zero-copy reads |
| **BLAKE3 Hashing** | `src/rust/lib.rs` | 558-606 | ~10x faster than SHA-256 |

### Performance Achieved

```
Single permission check:  ~50x speedup (6µs vs 300µs Python)
Bulk 10-100 checks:       ~70-80x speedup
Bulk 1000+ checks:        ~85x speedup
Grep with literal:        ~50-100x speedup
Glob matching:            ~10-20x speedup
```

---

## GAP ANALYSIS: Proposed vs. Implemented

### Phase 1 Quick Wins

| Proposed Optimization | Rust Status | Python Status | Action Needed |
|-----------------------|-------------|---------------|---------------|
| **1.1 Timestamp Quantization** | ❌ Not in Rust | ❌ Not implemented | Implement in Python cache layer |
| **1.2 Request Deduplication** | ❌ Not in Rust | ❌ Not implemented | Add to Python, consider Rust DashMap |
| **1.3 Tiger Cache Single Ops** | N/A (Python bitmap) | ⚠️ Partial | Extend Python Tiger Cache |
| **1.4 Goroutine/Task Audit** | ✅ Rust is safe | ⚠️ Need audit | Audit Python async code |

### Phase 2 Core Optimizations

| Proposed Optimization | Rust Status | Python Status | Action Needed |
|-----------------------|-------------|---------------|---------------|
| **2.1 Subproblem Caching** | ❌ Caches final only | ❌ Not implemented | Add to both layers |
| **2.2 Leopard Index** | ❌ Not implemented | ⚠️ Partial (async_rebac) | Full implementation needed |
| **2.3 BulkCheckPermission** | ✅ `compute_permissions_bulk` | ✅ Available | Already done! |
| **2.4 Memory-Efficient Metadata** | ✅ String interning helps | ❌ Python objects | Add compact structs |
| **2.5 Watch API** | N/A (DB events) | ❌ Not implemented | Python implementation |

### Phase 3 Advanced

| Proposed Optimization | Rust Status | Python Status | Action Needed |
|-----------------------|-------------|---------------|---------------|
| **3.1 Volume-Based Storage** | ❌ Not implemented | ❌ Not implemented | Major new feature |
| **3.2 Tiered Storage + EC** | ❌ Not implemented | ❌ Not implemented | Major new feature |
| **3.3 Cross-Tenant Optimization** | ❌ Not implemented | ❌ Not implemented | New feature |

---

## RUST-SPECIFIC OPTIMIZATION OPPORTUNITIES

### 1. Expose Bloom Filter to Python

**Current State:** Bloom filter imported but NOT exposed as pyfunction

```rust
// rust/nexus_fast/src/lib.rs line 4
use bloomfilter::Bloom;

// BUT no #[pyfunction] exports it!
```

**Proposed Addition:**
```rust
#[pyfunction]
fn bloom_create(capacity: usize, fp_rate: f64) -> BloomWrapper {
    BloomWrapper(Bloom::new_for_fp_rate(capacity, fp_rate))
}

#[pyfunction]
fn bloom_check(bloom: &BloomWrapper, key: &str) -> bool {
    bloom.0.check(&key)
}

#[pyfunction]
fn bloom_add(bloom: &mut BloomWrapper, key: &str) {
    bloom.0.set(&key)
}
```

**Impact:** Fast cache miss detection, ~10% backend load reduction

---

### 2. Add Subproblem Caching to Rust

**Current State:** Only caches final permission results

```rust
// rust/nexus_fast/src/lib.rs line 106-108
type MemoCache = AHashMap<(String, String, String, String, String), bool>;
// Key: (subject_type, subject_id, permission, object_type, object_id) → bool
```

**Proposed Enhancement:**
```rust
// Separate caches for subproblems
struct SubproblemCache {
    // User → Groups (transitive) - very stable, long TTL
    membership: AHashMap<(Sym, Sym), Vec<(Sym, Sym)>>,

    // Resource → Ancestors - very stable
    hierarchy: AHashMap<(Sym, Sym), Vec<(Sym, Sym)>>,

    // (Group, Permission, Resource) → bool - can change
    grants: AHashMap<(Sym, Sym, Sym, Sym, Sym), bool>,
}
```

**Impact:** 60%+ cache reuse across different permission checks

---

### 3. Add Leopard Index to Rust

**Current State:** Uses BFS traversal for group membership

**Proposed Addition:**
```rust
/// Transitive closure index for O(1) membership lookups
struct LeopardIndex {
    // subject → all groups transitively
    closure: DashMap<(Sym, Sym), AHashSet<(Sym, Sym)>>,
    // group → all members (for invalidation)
    reverse: DashMap<(Sym, Sym), AHashSet<(Sym, Sym)>>,
}

impl LeopardIndex {
    fn is_member(&self, subject: (Sym, Sym), group: (Sym, Sym)) -> bool {
        self.closure
            .get(&subject)
            .map(|groups| groups.contains(&group))
            .unwrap_or(false)
    }
}
```

**Impact:** 50-500x faster group membership checks

---

### 4. Request Deduplication with DashMap

**Current State:** No deduplication, concurrent checks compute separately

**Proposed Addition:**
```rust
use std::sync::Arc;
use tokio::sync::broadcast;

/// In-flight request tracker for deduplication
struct InFlightTracker {
    pending: DashMap<InternedMemoKey, Arc<broadcast::Sender<bool>>>,
}

impl InFlightTracker {
    fn get_or_compute<F>(&self, key: InternedMemoKey, compute: F) -> bool
    where
        F: FnOnce() -> bool,
    {
        // Check if already computing
        if let Some(sender) = self.pending.get(&key) {
            // Subscribe and wait
            let mut rx = sender.subscribe();
            return rx.blocking_recv().unwrap_or(false);
        }

        // We're first - compute and broadcast
        let (tx, _) = broadcast::channel(1);
        self.pending.insert(key, Arc::new(tx.clone()));

        let result = compute();
        let _ = tx.send(result);
        self.pending.remove(&key);

        result
    }
}
```

**Impact:** 10-50x fewer computations under concurrent load

---

## REVISED IMPLEMENTATION PRIORITY

Based on Rust cross-reference, here's the updated priority:

### Immediate (Rust Already Helps)

| Task | Status | Next Step |
|------|--------|-----------|
| BulkCheckPermission | ✅ Done in Rust | Use more extensively in Python |
| String interning | ✅ Done in Rust | Already active |
| Graph caching | ✅ Done in Rust | Already active |
| SIMD operations | ✅ Done in Rust | Already active |

### Phase 1 (Python Layer Focus)

| Task | Priority | Rust Needed? |
|------|----------|--------------|
| **Timestamp Quantization** | P0 | No - Python cache keys |
| **Request Deduplication** | P0 | Optional (DashMap available) |
| **Tiger Cache Single Ops** | P0 | No - Python bitmap |
| **Expose Bloom Filter** | P1 | Yes - simple addition |

### Phase 2 (Rust Enhancement)

| Task | Priority | Effort |
|------|----------|--------|
| **Subproblem Caching** | P0 | Medium - extend memoization |
| **Leopard Index** | P1 | High - new data structure |
| **Watch API** | P1 | No Rust - Python/DB events |

---

## SPECIFIC CODE CHANGES NEEDED

### 1. Expose Bloom Filter (Quick Win)

**File:** `rust/nexus_fast/src/lib.rs`

Add near line 2000 (with other pyfunctions):
```rust
#[pyclass]
struct BloomWrapper(Bloom<String>);

#[pymethods]
impl BloomWrapper {
    #[new]
    fn new(capacity: usize, fp_rate: f64) -> Self {
        BloomWrapper(Bloom::new_for_fp_rate(capacity, fp_rate))
    }

    fn check(&self, key: &str) -> bool {
        self.0.check(&key.to_string())
    }

    fn add(&mut self, key: &str) {
        self.0.set(&key.to_string())
    }
}

// Add to #[pymodule]
m.add_class::<BloomWrapper>()?;
```

### 2. Extend Memoization for Subproblems

**File:** `rust/nexus_fast/src/lib.rs`

Modify struct around line 106:
```rust
struct MultiLevelCache {
    // Final results (current)
    permissions: InternedMemoCache,

    // NEW: Subproblem caches
    memberships: AHashMap<(Sym, Sym, Sym), Vec<(Sym, Sym)>>,  // (tenant, user_type, user_id) → groups
    hierarchies: AHashMap<(Sym, Sym, Sym), Vec<(Sym, Sym)>>,  // (tenant, res_type, res_id) → ancestors
}
```

### 3. Add Parallel Threshold Configuration

**Current:** Hardcoded thresholds

```rust
// line 51-52
const GLOB_PARALLEL_THRESHOLD: usize = 500;
const PERMISSION_PARALLEL_THRESHOLD: usize = 50;
```

**Proposed:** Make configurable via Python

```rust
#[pyfunction]
fn set_parallel_threshold(operation: &str, threshold: usize) -> PyResult<()> {
    match operation {
        "glob" => GLOB_THRESHOLD.store(threshold, Ordering::SeqCst),
        "permission" => PERM_THRESHOLD.store(threshold, Ordering::SeqCst),
        _ => return Err(PyValueError::new_err("Unknown operation")),
    }
    Ok(())
}
```

---

## WHAT RUST ALREADY DOES WELL

### 1. Permission Bulk Checks (85x speedup)

```rust
// Line 756-945: compute_permissions_bulk
// - String interning for O(1) equality
// - Graph caching across calls (Issue #862)
// - Parallel execution with rayon (>50 items)
// - Lock-free DashMap for concurrent memoization
```

### 2. Memory Efficiency

```rust
// String interning: 4 bytes per symbol vs variable-length strings
type Sym = DefaultSymbol;  // Line 28

// Interned entities: 8 bytes total (2 × 4-byte symbols)
struct InternedEntity {
    entity_type: Sym,  // 4 bytes
    entity_id: Sym,    // 4 bytes
}
```

### 3. Zero-Copy Operations

```rust
// Memory-mapped file reads (Line 2068-2070)
let mmap = unsafe { Mmap::map(&file)? };

// SIMD UTF-8 validation (Line 14)
use simdutf8::basic::from_utf8 as simd_from_utf8;
```

---

## SUMMARY: Rust vs Python Responsibility

| Layer | Rust Responsibility | Python Responsibility |
|-------|---------------------|----------------------|
| **Caching** | Memoization within request | L1/L2 cache management, TTL, invalidation |
| **Permission Checks** | Graph traversal, bulk checks | Tiger Cache bitmaps, Watch API |
| **Hashing** | BLAKE3, content hashing | CAS deduplication logic |
| **Search** | Grep (SIMD), glob matching | Index management, result pagination |
| **Quantization** | N/A | Timestamp quantization in cache keys |
| **Deduplication** | Could add DashMap tracker | In-flight request tracking |

---

## NEXT STEPS

1. **Immediate:** Use existing Rust bulk checks more extensively
2. **Week 1:** Implement timestamp quantization in Python
3. **Week 1:** Expose Bloom filter from Rust
4. **Week 2:** Add request deduplication (Python first, Rust optional)
5. **Week 3-4:** Add subproblem caching to Rust
6. **Week 5-6:** Implement Leopard index in Rust

//! Observability syscalls — observer registry, file watch registry,
//! `sys_watch`, and the shared `dispatch_mutation` helper.
//!
//! Phase G of Phase 3 restructure plan extracted these methods from
//! the monolithic `kernel.rs`.  The split is a file-organization
//! change — every method stays a member of [`Kernel`] via the
//! submodule's `impl Kernel { ... }` block.

use std::sync::Arc;

use crate::dispatch::{FileEvent, FileEventType, MutationObserver};

use super::{Kernel, OperationContext};

impl Kernel {
    // ── Observer registry (§10 Phase 10 / §11 Phase 2) ────────────────
    //
    // These methods are pre-built infrastructure for §11 Phases 3/5/6:
    //   - Phase 3: replaces the inline loop with a `ThreadPool::execute()`
    //     submission so observers run off the syscall hot path.
    //   - Phase 5: kernel sys_* methods call `dispatch_observers` after
    //     each successful mutation.
    //   - Phase 6: PyKernel.register_observer rewires from the legacy
    //     `Py<PyAny>`-based ObserverRegistry to this Rust-typed registry,
    //     and the legacy registry is deleted.
    //
    // No production caller exists yet, hence #[allow(dead_code)] — the
    // attribute is removed when Phase 5 wires the first call site.

    ///
    /// `OBSERVE_INLINE` was deleted in §11 Phase 2 — all OBSERVE callbacks
    /// run on `observer_pool` (the kernel's background ThreadPool). There
    /// is no other mode. Observers needing synchronous-blocking semantics
    /// must be moved to INTERCEPT POST.
    #[allow(dead_code)]
    pub fn register_observer(
        &self,
        observer: Arc<dyn MutationObserver>,
        name: String,
        event_mask: u32,
    ) {
        self.observers.lock().register(observer, name, event_mask);
    }

    /// Unregister observer by name. Returns true if removed.
    #[allow(dead_code)]
    pub fn unregister_observer(&self, name: &str) -> bool {
        self.observers.lock().unregister(name)
    }

    /// OBSERVE-phase dispatch — call all matching observers inline.
    ///
    /// Fire-and-forget by contract. Observers are pure Rust (~0.5μs each:
    /// FileWatchRegistry Condvar notify + StreamEventObserver stream_write_nowait).
    /// Inline dispatch avoids ThreadPool + fork() incompatibility in xdist CI.
    ///
    /// Snapshot-then-drop-lock pattern: collect Arc clones under the registry
    /// lock, release lock, then call each observer. Prevents deadlocks if an
    /// observer re-enters the kernel.
    ///
    /// Called by every successful Tier 1 mutation syscall via dispatch_mutation.
    pub fn dispatch_observers(&self, event: &FileEvent) {
        let observers = self.observers.lock().matching(event.event_type as u32);
        for obs in observers {
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                obs.on_mutation(event);
            }));
        }
    }

    /// No-op — observers dispatch inline (no background pool).
    /// Kept for API compat with tests that call flush_observers().
    pub fn flush_observers(&self) {}

    /// Total registered Rust-native observers.
    pub fn observer_count(&self) -> usize {
        self.observers.lock().count()
    }

    /// Dispatch a manually constructed FileEvent (for DLC mount/unmount, Python fallback).
    pub fn dispatch_event(&self, event_type: FileEventType, path: &str) {
        let event = FileEvent::new(event_type, path);
        self.dispatch_observers(&event);
    }

    /// Helper: build a `FileEvent` pre-populated with the syscall's
    /// `OperationContext` identity fields (zone_id, user_id, agent_id),
    /// then apply caller-provided extras and dispatch.
    ///
    /// Used by sys_* methods to keep the per-syscall dispatch site to
    /// 3-4 lines instead of a 15-field struct literal. Fast path: when
    /// no observers are registered, `dispatch_observers` is an early
    /// return after a single Mutex acquire — the FileEvent construction
    /// is essentially free against any observer-bearing workload, so
    /// there's no point in gating it behind a count check.
    #[inline]
    pub(super) fn dispatch_mutation(
        &self,
        event_type: FileEventType,
        path: &str,
        ctx: &OperationContext,
        extra: impl FnOnce(&mut FileEvent),
    ) {
        let mut event = FileEvent::new(event_type, path);
        event.zone_id = Some(ctx.zone_id.clone());
        if !ctx.user_id.is_empty() {
            event.user_id = Some(ctx.user_id.clone());
        }
        event.agent_id = ctx.agent_id.clone();
        extra(&mut event);
        self.dispatch_observers(&event);
    }

    // ── File watch registry (§10 A3) ──────────────────────────────────

    /// Register a file watch pattern. Returns watch ID.
    pub fn register_watch(&self, pattern: &str) -> u64 {
        self.file_watches.register(pattern)
    }

    /// Unregister a file watch by ID.
    pub fn unregister_watch(&self, watch_id: u64) -> bool {
        self.file_watches.unregister(watch_id)
    }

    /// Match a path against all registered watch patterns.
    /// Returns list of matching watch IDs.
    pub fn match_watches(&self, path: &str) -> Vec<u64> {
        self.file_watches.match_path(path)
    }

    /// sys_watch — block until a file event matching the pattern arrives, or timeout.
    /// Tier 1 syscall (inotify equivalent). Returns matching FileEvent or None on timeout.
    pub fn sys_watch(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent> {
        self.file_watches.wait_for_event(pattern, timeout_ms)
    }
}

//! FileWatchRegistry — Rust-native file watch pattern matching (§10 A3).
//!
//! Kernel primitive for inotify-like file change notification. Stores
//! watch patterns (glob-style) with unique IDs.
//!
//! In-tree Rust callers (sudocode `spawn_task`, the matrix-adapter
//! `/sync` long-poll fallback, managed-agent watch use cases) reach
//! the registry through the `sys_watch(pattern, timeout)` syscall:
//! [`Self::wait_for_event`] registers a temporary watch with a
//! `WatchNotify` backing, parks the caller on a condition variable
//! until a matching event arrives or the timeout fires, and
//! unregisters the temp watch on return. [`Self::notify_match`]
//! (called from `dispatch_observers` on every mutation) wakes
//! blocked waiters whose pattern matches.

use globset::{Glob, GlobMatcher};
use parking_lot::{Condvar, Mutex, RwLock};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::dispatch::FileEvent;

/// Per-blocking-watch notification channel. The condition-variable
/// signals event arrival; the inbox buffers events that fire before
/// the waiter has actually parked on the condvar, so a notify racing
/// in just after registration isn't dropped on the floor.
///
/// Lifetime is scoped to a single `wait_for_event` call — on return,
/// the temp watch is unregistered and any unconsumed events are
/// dropped along with the [`WatchNotify`].
struct WatchNotify {
    inbox: Mutex<Vec<FileEvent>>,
    condvar: Condvar,
}

/// A registered file watch entry.
struct WatchEntry {
    id: u64,
    #[allow(dead_code)]
    pattern: String,
    matcher: GlobMatcher,
    /// Notification channel populated by [`FileWatchRegistry::wait_for_event`]
    /// at temp-watch registration time. The `dispatch_observers` push
    /// path uses this to wake the parked waiter.
    notify: Arc<WatchNotify>,
}

/// Kernel file watch registry — pattern matching without GIL.
pub(crate) struct FileWatchRegistry {
    watches: RwLock<Vec<WatchEntry>>,
    next_id: AtomicU64,
}

impl FileWatchRegistry {
    pub(crate) fn new() -> Self {
        Self {
            watches: RwLock::new(Vec::new()),
            next_id: AtomicU64::new(1),
        }
    }

    fn register_with_notify(&self, pattern: &str, notify: Arc<WatchNotify>) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        // Build glob matcher — fallback to literal match if glob parse fails
        let matcher = Glob::new(pattern)
            .unwrap_or_else(|_| Glob::new(&globset::escape(pattern)).unwrap())
            .compile_matcher();
        self.watches.write().push(WatchEntry {
            id,
            pattern: pattern.to_string(),
            matcher,
            notify,
        });
        id
    }

    /// Unregister a watch by ID. Returns true if found.
    pub(crate) fn unregister(&self, watch_id: u64) -> bool {
        let mut watches = self.watches.write();
        if let Some(pos) = watches.iter().position(|w| w.id == watch_id) {
            watches.swap_remove(pos);
            true
        } else {
            false
        }
    }

    /// Number of registered watches.
    #[allow(dead_code)]
    pub(crate) fn len(&self) -> usize {
        self.watches.read().len()
    }

    /// Notify every blocking `wait_for_event` waiter whose pattern
    /// matches `event.path`. Called by the kernel's
    /// `dispatch_observers` (observability.rs) on every successful
    /// mutation, so any thread parked in [`Self::wait_for_event`]
    /// wakes within a single mutation latency.
    ///
    /// Snapshot-then-drop-lock pattern — collect `Arc<WatchNotify>`
    /// clones under the registry RwLock read guard, drop the guard,
    /// then push events. Holding the registry guard across
    /// `Mutex::lock` on `WatchNotify::inbox` would deadlock with a
    /// concurrent `register_with_notify` writer.
    pub(crate) fn notify_match(&self, event: &FileEvent) {
        let path = event.path();
        let notifies: Vec<Arc<WatchNotify>> = {
            let guard = self.watches.read();
            guard
                .iter()
                .filter(|w| w.matcher.is_match(path))
                .map(|w| Arc::clone(&w.notify))
                .collect()
        };
        for notify in notifies {
            notify.inbox.lock().push(event.clone());
            notify.condvar.notify_one();
        }
    }

    /// Block until a file event matching `pattern` fires, or
    /// `timeout_ms` elapses.
    ///
    /// `timeout_ms == 0` is a non-blocking try — returns immediately
    /// with whatever's already in the freshly-armed inbox (always
    /// `None` because the temporary watch was just registered).
    /// Callers that want unbounded blocking pass a large timeout
    /// (e.g. `u64::MAX / 2`); there's no separate sentinel for
    /// "wait forever" because every real caller has some upper
    /// bound (matrix `/sync` 30s default, sudocode `spawn_task`'s
    /// poll interval, …).
    ///
    /// Cleanup is unconditional — the temporary watch is
    /// unregistered on every return path (event arrival, timeout,
    /// or panic via the implicit drop) so leaked watch entries
    /// can't accumulate.
    pub(crate) fn wait_for_event(&self, pattern: &str, timeout_ms: u64) -> Option<FileEvent> {
        let notify = Arc::new(WatchNotify {
            inbox: Mutex::new(Vec::new()),
            condvar: Condvar::new(),
        });
        let watch_id = self.register_with_notify(pattern, Arc::clone(&notify));

        // RAII guard so the temporary watch is unregistered even
        // on the panic path. Functionally equivalent to a defer
        // block in Go.
        struct UnregisterOnDrop<'a> {
            registry: &'a FileWatchRegistry,
            id: u64,
        }
        impl Drop for UnregisterOnDrop<'_> {
            fn drop(&mut self) {
                self.registry.unregister(self.id);
            }
        }
        let _guard = UnregisterOnDrop {
            registry: self,
            id: watch_id,
        };

        let timeout = Duration::from_millis(timeout_ms);
        let deadline = Instant::now().checked_add(timeout);

        let mut inbox = notify.inbox.lock();
        // Spurious-wake-tolerant loop: parking_lot's `wait_for`
        // can return early without a notify, so we re-check the
        // inbox + remaining-timeout each iteration. Returns the
        // first event (FIFO); additional events that fired while
        // we were parked are dropped when the `UnregisterOnDrop`
        // guard tears down the temp watch on return. Callers that
        // need to consume successive events re-arm with another
        // `sys_watch` call — each call is its own independent
        // one-shot window with a fresh inbox.
        loop {
            if let Some(first) = inbox.first() {
                let event = first.clone();
                inbox.remove(0);
                return Some(event);
            }
            let remaining = match deadline {
                Some(d) => d.checked_duration_since(Instant::now())?,
                None => timeout, // overflow-saturating; treat as full timeout
            };
            if remaining.is_zero() {
                return None;
            }
            let wake = notify.condvar.wait_for(&mut inbox, remaining);
            if wake.timed_out() && inbox.is_empty() {
                return None;
            }
        }
    }
}

/// RemoteWatchProtocol — kernel-agnostic interface for distributed watch.
///
/// Implementations deferred to another AI doing DT_STREAM migration.
/// Defined here so kernel can hold `Option<Box<dyn RemoteWatchProtocol>>`.
#[allow(dead_code)]
pub(crate) trait RemoteWatchProtocol: Send + Sync {
    /// Subscribe to remote watch events for a path pattern.
    fn subscribe(&self, pattern: &str, zone_id: &str) -> Result<u64, String>;
    /// Unsubscribe from remote watch.
    fn unsubscribe(&self, subscription_id: u64) -> Result<(), String>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dispatch::FileEventType;
    use std::thread;

    #[test]
    fn wait_for_event_returns_on_matching_notify() {
        let registry = Arc::new(FileWatchRegistry::new());
        let notifier = Arc::clone(&registry);
        let waker = thread::spawn(move || {
            // Give the waiter time to park on the condvar before
            // notifying — without this sleep the notify may fire
            // before `wait_for_event` registers its temporary
            // watch, leaving the waiter to time out.
            thread::sleep(Duration::from_millis(20));
            let event = FileEvent::new(FileEventType::FileWrite, "/proc/p1/chat-with-me");
            notifier.notify_match(&event);
        });

        let event = registry
            .wait_for_event("/proc/p1/chat-with-me", 1_000)
            .expect("notify should have woken the waiter");
        assert_eq!(event.path(), "/proc/p1/chat-with-me");
        waker.join().unwrap();

        // Watch entry was unregistered on return — registry is empty.
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn wait_for_event_returns_none_on_timeout() {
        let registry = FileWatchRegistry::new();
        let result = registry.wait_for_event("/proc/p1/chat-with-me", 50);
        assert!(result.is_none());
        // Cleanup: temporary watch dropped on return.
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn wait_for_event_zero_timeout_is_non_blocking() {
        let registry = FileWatchRegistry::new();
        let started = Instant::now();
        let result = registry.wait_for_event("/proc/p1/chat-with-me", 0);
        // Should return immediately (well under the 50ms a real
        // condvar wait would take). Allow generous slack for slow
        // CI runners.
        assert!(started.elapsed() < Duration::from_millis(20));
        assert!(result.is_none());
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn wait_for_event_filters_by_pattern() {
        let registry = Arc::new(FileWatchRegistry::new());
        let notifier = Arc::clone(&registry);
        let waker = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            // Wrong path — must not wake the waiter.
            let other = FileEvent::new(FileEventType::FileWrite, "/other/path");
            notifier.notify_match(&other);
            // Right path — must wake.
            thread::sleep(Duration::from_millis(20));
            let target = FileEvent::new(FileEventType::FileWrite, "/proc/p1/chat-with-me");
            notifier.notify_match(&target);
        });

        let event = registry
            .wait_for_event("/proc/p1/chat-with-me", 500)
            .expect("only the matching path should wake the waiter");
        assert_eq!(event.path(), "/proc/p1/chat-with-me");
        waker.join().unwrap();
    }
}

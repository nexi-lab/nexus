//! In-flight operation counter with RAII drop-decrement.
//!
//! [`OpCounter`] tracks how many long-running background operations
//! are currently in progress. Callers acquire an [`OpGuard`] via
//! [`OpCounter::enter`]; the guard's `Drop` impl decrements the
//! counter, so an observer reads a truthful "N operations in flight"
//! at any instant.
//!
//! **Guard placement invariant.** The guard MUST be moved INTO the
//! `spawn_blocking` closure that runs the actual work — never left
//! owned by the outer async future. A `tonic` RPC future can be
//! dropped by client cancellation while the `spawn_blocking` task it
//! launched keeps running; a guard on the future runs its `Drop` at
//! cancellation and zeroes the counter mid-work, during exactly the
//! partial-build window the counter exists to expose. The contract
//! test `spawn_blocking_holds_count_past_future_drop` locks in this
//! invariant.
//!
//! # Example
//!
//! ```
//! use services::ops::OpCounter;
//!
//! # async fn build_index() {}
//! # let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
//! # rt.block_on(async {
//! let ops = OpCounter::new();
//! let guard = ops.enter();
//! tokio::task::spawn_blocking(move || {
//!     let _guard = guard; // decrement fires when the WORK ends, not the RPC future
//!     // ... long-running work ...
//! })
//! .await
//! .unwrap();
//! assert_eq!(ops.count(), 0);
//! # });
//! ```

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

/// Counter of currently in-flight operations.
///
/// Cheap to `clone` — the counter itself is `Arc<AtomicU32>` inside,
/// so every clone points at the same underlying count.
#[derive(Clone, Default)]
pub struct OpCounter(Arc<AtomicU32>);

impl OpCounter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Snapshot of the current in-flight count.
    pub fn count(&self) -> u32 {
        self.0.load(Ordering::SeqCst)
    }

    /// Increment the counter and return a guard that decrements on drop.
    ///
    /// See the module docstring for the guard placement invariant.
    pub fn enter(&self) -> OpGuard {
        OpGuard::enter(&self.0)
    }
}

/// RAII handle: increments the counter on [`OpCounter::enter`],
/// decrements it when dropped.
///
/// Move this INTO the `spawn_blocking` closure that runs the work.
pub struct OpGuard(Arc<AtomicU32>);

impl OpGuard {
    fn enter(counter: &Arc<AtomicU32>) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        Self(Arc::clone(counter))
    }
}

impl Drop for OpGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enter_increments_drop_decrements() {
        let ops = OpCounter::new();
        assert_eq!(ops.count(), 0);
        {
            let _g = ops.enter();
            assert_eq!(ops.count(), 1);
        }
        assert_eq!(ops.count(), 0);
    }

    #[test]
    fn count_is_max_of_concurrent_guards() {
        let ops = OpCounter::new();
        let g1 = ops.enter();
        let g2 = ops.enter();
        let g3 = ops.enter();
        assert_eq!(ops.count(), 3);
        drop(g2);
        assert_eq!(ops.count(), 2);
        drop(g1);
        drop(g3);
        assert_eq!(ops.count(), 0);
    }

    #[test]
    fn clone_shares_counter() {
        let a = OpCounter::new();
        let b = a.clone();
        let _g = a.enter();
        assert_eq!(b.count(), 1);
    }

    // Contract test for the module invariant: dropping the RPC future
    // that spawn_blocking'd the work MUST NOT zero the counter while
    // the work is still running. The guard has to live inside the
    // blocking closure.
    #[test]
    fn spawn_blocking_holds_count_past_future_drop() {
        use std::sync::mpsc::channel;
        use std::time::Duration;

        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();

        let ops = OpCounter::new();
        let (release_tx, release_rx) = channel::<()>();
        let (started_tx, started_rx) = channel::<()>();

        // Enter the counter, hand the guard to spawn_blocking, then
        // DROP the outer future by dropping the JoinHandle.
        {
            let ops_clone = ops.clone();
            let handle = rt.spawn(async move {
                let guard = ops_clone.enter();
                let started_tx = started_tx.clone();
                let _blocking = tokio::task::spawn_blocking(move || {
                    let _guard = guard; // MOVED into the blocking closure
                    started_tx.send(()).unwrap();
                    // Simulate long-running work: block until released.
                    release_rx.recv().unwrap();
                });
                // Drop `_blocking` (and this future) here so cancellation
                // of the RPC future is simulated.
            });
            drop(handle);
        }

        // Wait for the blocking task to have entered the counter.
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();

        // At this point the outer future is gone but the blocking task
        // is still running with the guard.
        assert_eq!(
            ops.count(),
            1,
            "counter must reflect the still-running blocking task"
        );

        // Release the blocking task and let it drain.
        release_tx.send(()).unwrap();
        for _ in 0..200 {
            if ops.count() == 0 {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(ops.count(), 0, "counter should decrement after work ends");
    }
}

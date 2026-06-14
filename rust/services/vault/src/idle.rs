//! Idle tracking for `nexusd-vault`: shut down when no RPC traffic for N seconds.
//!
//! Why: when the vault data dir lives on file-level cloud-synced storage
//! (Dropbox / OneDrive / iCloud / Syncthing / NFS), the OS holds exclusive
//! file locks on `vault-meta.redb` while the daemon runs. A long-running
//! daemon starves the sync client and the cloud copy lags arbitrarily far
//! behind local. With idle-shutdown the daemon releases its locks when
//! there is no active traffic, so the sync client can publish a clean
//! state and other machines pick it up.
//!
//! Forward note: this module is an interim story. Once nexus-vfs
//! federation is generally available, vault data should ride federation
//! replication directly — file-level cloud sync becomes unnecessary.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Shared last-RPC timestamp (Unix seconds). Cheap to clone.
#[derive(Clone)]
pub struct IdleTracker {
    last_rpc_at: Arc<AtomicI64>,
}

impl IdleTracker {
    pub fn new() -> Self {
        Self {
            last_rpc_at: Arc::new(AtomicI64::new(now_secs())),
        }
    }

    /// Mark "now" as the most recent RPC time. Called from a gRPC interceptor
    /// on every inbound request.
    pub fn bump(&self) {
        self.last_rpc_at.store(now_secs(), Ordering::Relaxed);
    }

    /// Seconds since the last bump (clamped to >= 0 against clock skew).
    pub fn idle_seconds(&self) -> i64 {
        (now_secs() - self.last_rpc_at.load(Ordering::Relaxed)).max(0)
    }
}

impl Default for IdleTracker {
    fn default() -> Self {
        Self::new()
    }
}

fn now_secs() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[derive(Debug, PartialEq, Eq)]
pub enum ShutdownReason {
    /// External signal (SIGINT / Ctrl-C).
    Signal,
    /// No RPC traffic for `idle_for_secs` seconds.
    Idle { idle_for_secs: i64 },
}

/// Wait for either a shutdown signal or the idle threshold to be exceeded.
///
/// `idle_shutdown_seconds == 0` disables the idle path — the returned
/// future resolves only when `signal` resolves.
///
/// Caveat: if a single RPC takes longer than `idle_shutdown_seconds`, the
/// idle path can fire mid-request. tonic's `serve_with_shutdown` drains
/// in-flight requests gracefully, so the request still completes; only
/// new connections are refused. Callers should set the threshold above
/// the longest expected RPC duration.
pub async fn wait_for_shutdown<F>(
    tracker: IdleTracker,
    signal: F,
    idle_shutdown_seconds: u64,
) -> ShutdownReason
where
    F: std::future::Future<Output = ()>,
{
    if idle_shutdown_seconds == 0 {
        signal.await;
        return ShutdownReason::Signal;
    }
    let threshold = idle_shutdown_seconds as i64;
    // 250ms gives < 1s worst-case detection latency without burning CPU.
    let poll = Duration::from_millis(250);
    tokio::pin!(signal);
    loop {
        tokio::select! {
            _ = &mut signal => return ShutdownReason::Signal,
            _ = tokio::time::sleep(poll) => {
                let idle = tracker.idle_seconds();
                if idle >= threshold {
                    return ShutdownReason::Idle { idle_for_secs: idle };
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_tracker_is_not_idle() {
        let t = IdleTracker::new();
        assert!(t.idle_seconds() < 2, "fresh tracker should show ≈0s idle");
    }

    #[test]
    fn bump_resets_idle() {
        let t = IdleTracker::new();
        // Even without sleep, bump should leave idle ≈ 0.
        t.bump();
        assert!(t.idle_seconds() < 2);
    }

    #[tokio::test]
    async fn signal_wins_over_idle() {
        // Threshold of 60s — would never fire during this test if the
        // signal didn't take precedence.
        let t = IdleTracker::new();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let signal = async move {
            let _ = rx.await;
        };
        let task = tokio::spawn(wait_for_shutdown(t, signal, 60));
        tokio::time::sleep(Duration::from_millis(50)).await;
        tx.send(()).unwrap();
        let reason = task.await.unwrap();
        assert_eq!(reason, ShutdownReason::Signal);
    }

    #[tokio::test]
    async fn disabled_only_returns_on_signal() {
        let t = IdleTracker::new();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let signal = async move {
            let _ = rx.await;
        };
        let task = tokio::spawn(wait_for_shutdown(t, signal, 0));
        // Hold for a bit; with idle path disabled, task must stay alive.
        tokio::time::sleep(Duration::from_millis(500)).await;
        assert!(!task.is_finished(), "disabled idle path must not time out");
        tx.send(()).unwrap();
        assert_eq!(task.await.unwrap(), ShutdownReason::Signal);
    }

    #[tokio::test]
    async fn idle_fires_after_threshold() {
        let t = IdleTracker::new();
        // Signal future that never resolves — only the idle path can win.
        let signal = std::future::pending::<()>();
        let task = tokio::spawn(wait_for_shutdown(t, signal, 1));
        // Threshold is 1s; allow up to 3s wall clock for poll latency + CI noise.
        let reason = tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .expect("idle path must fire within 3s")
            .unwrap();
        match reason {
            ShutdownReason::Idle { idle_for_secs } => assert!(idle_for_secs >= 1),
            other => panic!("expected Idle, got {other:?}"),
        }
    }
}

//! Regression: `ZoneManager` public sync API must work when called from
//! a `#[tokio::main]` async context (the production `nexusd-cluster`
//! daemon's call shape).
//!
//! Background: `ZoneManager::new` and several other sync methods bridge
//! to async raft IPC via the inner runtime's `block_on`. A nested
//! `Handle::block_on` call from a thread already registered as a tokio
//! worker panics — tokio refuses to park a worker because the awaited
//! future could deadlock against tasks needing the same worker pool.
//!
//! `nexusd-cluster::run_daemon` exposes exactly that shape:
//! `#[tokio::main(flavor = "multi_thread")]` makes the entry thread a
//! worker, then `open_zone_manager` calls `ZoneManager::new` (sync) on
//! that worker. Without the `bridge_block_on` wrapper inside
//! `ZoneManager`, this panics with "Cannot start a runtime from within
//! a runtime."
//!
//! The Federation E2E suite does NOT cover this path — it goes through
//! PyO3, where the Python main thread is a regular sync thread (no
//! outer tokio runtime) and `block_on` is straightforwardly safe.
//!
//! This test exercises every sync API that bridges to async work, all
//! from inside a `#[tokio::test(flavor = "multi_thread")]` context to
//! mirror the production daemon's tokio runtime shape.

#![cfg(all(feature = "grpc", has_protos))]

use nexus_raft::ZoneManager;
use std::time::Duration;
use tempfile::TempDir;

/// Pick a random unused TCP port for the raft gRPC server. The test
/// doesn't actually connect to it — we only need a unique bind address
/// so concurrent `cargo test` invocations don't collide.
fn ephemeral_bind_addr() -> String {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind 0");
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    format!("127.0.0.1:{}", port)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn zone_manager_constructs_under_tokio_main() {
    // Phase 1 of the regression: ZoneManager::new internally calls
    // ZoneRaftRegistry::open_existing_zones_from_disk, which used to
    // be `async fn` and was driven via `inner.handle().block_on(...)`.
    // On a tokio worker (this test thread) that block_on panicked.
    // Fixed by making the registry layer fully sync (no campaign, no
    // .await needed in setup_zone) — block_on disappears from the
    // construction path entirely.
    let tmp = TempDir::new().unwrap();
    let bind = ephemeral_bind_addr();
    let zm = ZoneManager::new(
        "regression-host",
        tmp.path().to_str().unwrap(),
        vec![],
        &bind,
        None,
    )
    .expect("ZoneManager::new must not panic on tokio worker");
    zm.shutdown();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn zone_manager_create_and_apply_topology_under_tokio_main() {
    // Phase 2 of the regression: even after `new` works, the daemon
    // continues with `create_zone` and a `tokio::spawn`-driven
    // `apply_topology` loop. Both bridge to async raft IPC (channel
    // send + apply ack from the driver task) via `bridge_block_on` —
    // verifies that the helper takes the `block_in_place` branch when
    // the calling thread is a worker.
    let tmp = TempDir::new().unwrap();
    let bind = ephemeral_bind_addr();
    let zm = ZoneManager::new(
        "regression-host",
        tmp.path().to_str().unwrap(),
        vec![],
        &bind,
        None,
    )
    .expect("ZoneManager::new");

    // Single-voter zone — raft-rs election timer (default ~150-300ms)
    // will self-elect once `tick()` runs in the spawned transport_loop.
    zm.create_zone("root", vec![])
        .expect("create_zone must not panic on tokio worker");

    // Wait briefly for the election timer to fire so subsequent
    // raft-IPC ops (apply_topology drives propose) can land. raft-rs's
    // default election_tick is 10 ticks at 10ms = 100ms; give a small
    // margin for the driver to settle.
    tokio::time::sleep(Duration::from_millis(500)).await;

    // apply_topology drives ensure_root_entry → propose root "/" entry
    // through the raft pipeline. With no pending mounts, returns true
    // once the root entry lands (or false if the leader hasn't elected
    // yet, in which case we just verify it didn't panic).
    let _ = zm
        .apply_topology("root")
        .expect("apply_topology must not panic on tokio worker");

    zm.shutdown();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn zone_manager_remove_zone_under_tokio_main() {
    // Phase 3: remove_zone bridges to the only inherently-async
    // registry op (awaits transport_handle JoinHandle for graceful
    // shutdown). On a tokio worker, naked block_on would panic;
    // bridge_block_on uses block_in_place to release the worker first.
    let tmp = TempDir::new().unwrap();
    let bind = ephemeral_bind_addr();
    let zm = ZoneManager::new(
        "regression-host",
        tmp.path().to_str().unwrap(),
        vec![],
        &bind,
        None,
    )
    .expect("ZoneManager::new");

    zm.create_zone("disposable", vec![]).expect("create_zone");

    zm.remove_zone("disposable", true)
        .expect("remove_zone must not panic on tokio worker");

    zm.shutdown();
}

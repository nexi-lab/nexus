//! Bootstrap branch-3 contract pin: empty storage + `NEXUS_BOOTSTRAP_NEW`
//! unset must block on JoinZone RPC indefinitely against unreachable
//! peers, then succeed once a founder appears.
//!
//! This exercises the wire-level retry loop in `bootstrap_or_join_root`
//! without going through `init_from_env` (which requires a `Kernel`).
//! The retry loop is private; we exercise the same observable behavior
//! via `call_join_zone_rpc` against an unreachable address.

#![cfg(all(feature = "grpc", has_protos))]

use std::time::Duration;

use nexus_raft::transport::call_join_zone_rpc;
use nexus_raft::ZoneManager;
use tempfile::TempDir;

fn mint_random_id() -> u64 {
    let id = rand::random::<u64>();
    if id == 0 {
        1
    } else {
        id
    }
}

/// Bind a TCP port, drop the listener, return the address — used to
/// pre-allocate ports for nodes that will bind themselves later.
fn alloc_port() -> String {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind 0");
    let addr = listener.local_addr().expect("local_addr");
    drop(listener);
    format!("{}", addr)
}

fn make_node(node_id: u64, dir: &std::path::Path) -> (std::sync::Arc<ZoneManager>, String) {
    let bind_str = alloc_port();
    let zm = ZoneManager::with_node_id(
        "test-host",
        node_id,
        dir.to_str().expect("utf-8"),
        vec![],
        &bind_str,
        None,
    )
    .expect("ZoneManager");
    (zm, bind_str)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_join_zone_rpc_against_unreachable_peer_returns_connection_error() {
    // Node B's JoinZone target is an unbound address (we allocated then
    // dropped the listener — no server is listening).  call_join_zone_rpc
    // returns Err(Connection(...)) at TCP level, not a server-side
    // error.  This pins the "branch 3 retry loop sees unreachable peer
    // → continue to next" classification used by bootstrap_or_join_root.
    let unreachable = alloc_port();
    let endpoint = format!("http://{unreachable}");
    let result = call_join_zone_rpc(
        &endpoint,
        "root",
        mint_random_id(),
        &endpoint, // self_address — informational
        false,
        2, // short timeout — no point waiting on a closed port
    )
    .await;

    assert!(
        result.is_err(),
        "JoinZone against unreachable peer must Err, got {result:?}",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_late_founder_unblocks_waiting_joiner() {
    // Branch 3 contract: B has empty storage and no BOOTSTRAP_NEW flag.
    // It would loop calling JoinZone against A's address indefinitely.
    // We simulate the retry loop by polling JoinZone with retries until
    // A becomes reachable (founder boot ~0–500ms later in this test).
    //
    // The pin is "B's first attempt fails (A absent), a later attempt
    // succeeds (A founded)".  This is exactly the asymmetric-startup
    // scenario from the plan's Win↔Mac flow, just compressed into one
    // process.
    let dir_a = TempDir::new().expect("dir-a");
    let dir_b = TempDir::new().expect("dir-b");
    let id_a = mint_random_id();
    let id_b = mint_random_id();

    // Pre-allocate A's port so B can dial a stable address.
    let bind_a = alloc_port();
    let endpoint_a = format!("http://{bind_a}");

    // First attempt: A doesn't exist yet — must Err.
    let early = call_join_zone_rpc(&endpoint_a, "root", id_b, &endpoint_a, false, 1).await;
    assert!(
        early.is_err(),
        "early JoinZone before founder boot must Err, got {early:?}",
    );

    // Spawn A's founder bootstrap on a delay.  Use the pre-allocated
    // port; ZoneManager will bind to it (alloc_port dropped its listener,
    // so the port is free again).
    let dir_a_path = dir_a.path().to_path_buf();
    let bind_a_clone = bind_a.clone();
    let founder_handle = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(300)).await;
        let zm = ZoneManager::with_node_id(
            "test-host",
            id_a,
            dir_a_path.to_str().expect("utf-8"),
            vec![],
            &bind_a_clone,
            None,
        )
        .expect("ZoneManager A");
        let zone = zm
            .create_zone("root", vec![format!("{id_a}@{bind_a_clone}")])
            .expect("create root on A");
        // Self-elect as 1-voter leader.
        for _ in 0..100 {
            if zone.is_leader() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(zone.is_leader(), "founder must self-elect");
        zm
    });

    // Now bring B up and have it JoinZone with retry loop.
    let (zm_b, bind_b) = make_node(id_b, dir_b.path());
    let endpoint_b = format!("http://{bind_b}");

    // Local register first (skip_bootstrap=true) so the leader can
    // append-replicate to B once AddNode commits.
    zm_b.join_zone("root", vec![format!("{id_a}@{bind_a}")], false)
        .expect("local join_zone on B");

    // Retry JoinZone RPC against A every 100ms for up to 5s.
    let mut joined = false;
    for _ in 0..50 {
        match call_join_zone_rpc(&endpoint_a, "root", id_b, &endpoint_b, false, 5).await {
            Ok(result) if result.success => {
                joined = true;
                break;
            }
            _ => tokio::time::sleep(Duration::from_millis(100)).await,
        }
    }
    assert!(
        joined,
        "JoinZone must eventually succeed once founder boots"
    );

    let _zm_a = founder_handle.await.expect("founder");
}

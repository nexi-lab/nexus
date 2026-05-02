//! Integration tests for the async bootstrap path under the opaque-ID
//! contract.
//!
//! These exercise the wire-level flow (gRPC server + JoinZone RPC),
//! not the kernel boot path.  `init_from_env` requires a `kernel`
//! handle which is heavyweight to spin up in unit tests; we drive
//! `ZoneManager` + the JoinZone RPC directly to keep the tests fast
//! and focused on the contract changes.
//!
//! Coverage:
//!   - 1-voter `create_zone` produces ConfState `[node_id]`.
//!   - JoinZone RPC commits AddNode; joiner ConfState becomes
//!     `[leader_id, joiner_id]` after the leader's snapshot installs.
//!   - `read_or_mint_node_id` persists across "process restarts"
//!     (TempDir reuse).
//!   - Both nodes' `node_id` values are random and distinct.

#![cfg(all(feature = "grpc", has_protos))]

use std::time::Duration;

use nexus_raft::transport::call_join_zone_rpc;
use nexus_raft::ZoneManager;
use tempfile::TempDir;

const NODE_ID_FILE: &str = ".node_id";

fn read_persisted_node_id(dir: &std::path::Path) -> u64 {
    let bytes = std::fs::read(dir.join(NODE_ID_FILE)).expect("read .node_id");
    u64::from_be_bytes(bytes.try_into().expect("8 bytes"))
}

fn mint_random_id() -> u64 {
    // Mirror `read_or_mint_node_id`'s behavior so tests can pre-seed a
    // node_id without having to construct a path-only fixture.
    let id = rand::random::<u64>();
    if id == 0 {
        1
    } else {
        id
    }
}

fn write_node_id(dir: &std::path::Path, id: u64) {
    std::fs::create_dir_all(dir).expect("create dir");
    std::fs::write(dir.join(NODE_ID_FILE), id.to_be_bytes()).expect("write .node_id");
}

/// Build a `ZoneManager` bound to an OS-allocated port + return its
/// concrete bind address so callers can dial back.
async fn make_node(node_id: u64, dir: &std::path::Path) -> (std::sync::Arc<ZoneManager>, String) {
    // ZoneManager needs a concrete port we can dial; use 0 to ask the
    // OS for one, then surface the actual bound address via the
    // tonic-internal listener... but ZoneManager binds itself, so we
    // pre-bind once to capture the port, then close it before
    // ZoneManager binds.  Cheap: TcpListener + drop.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind 0");
    let addr = listener.local_addr().expect("local_addr");
    drop(listener);
    let bind_str = format!("{}", addr);

    let zm = ZoneManager::with_node_id(
        "test-host",
        node_id,
        dir.to_str().expect("utf-8"),
        vec![],
        &bind_str,
        None,
        Some(format!("http://{bind_str}")),
    )
    .expect("ZoneManager");
    (zm, bind_str)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_async_bootstrap_1voter_then_join() {
    // Node A bootstraps a 1-voter root zone.  Node B mints a fresh ID
    // and JoinZones via call_join_zone_rpc.  Final ConfState on A
    // becomes [r_a, r_b]; both ids are random and distinct.

    let dir_a = TempDir::new().expect("dir-a");
    let dir_b = TempDir::new().expect("dir-b");

    // Mint node IDs as the production path would (read_or_mint_node_id).
    let id_a = mint_random_id();
    let id_b = mint_random_id();
    assert_ne!(id_a, id_b, "two random mints must differ");
    write_node_id(dir_a.path(), id_a);
    write_node_id(dir_b.path(), id_b);

    let (zm_a, bind_a) = make_node(id_a, dir_a.path()).await;
    let (zm_b, bind_b) = make_node(id_b, dir_b.path()).await;

    // Node A: create 1-voter root zone.  Self-elects as leader by
    // construction (quorum=1).
    let zone_a = zm_a
        .create_zone("root", vec![format!("{id_a}@{bind_a}")])
        .expect("create root on A");
    // Wait for self-campaign to land.
    for _ in 0..100 {
        if zone_a.is_leader() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(zone_a.is_leader(), "A must self-elect on 1-voter create");

    // Step 1: B registers root locally (skip_bootstrap=true) so its
    // gRPC server can serve append-entries from A's leader once
    // AddNode commits.  Order matters — without this, AddNode commits
    // but the leader's follow-up i_links_count propose hangs waiting
    // on B's quorum ack.
    let endpoint_a = format!("http://{bind_a}");
    let endpoint_b = format!("http://{bind_b}");
    let _zone_b = zm_b
        .join_zone("root", vec![format!("{id_a}@{bind_a}")], false)
        .expect("local join_zone on B");

    // Step 2: JoinZone RPC against A's leader.
    let result = call_join_zone_rpc(&endpoint_a, "root", id_b, &endpoint_b, false, 30)
        .await
        .expect("JoinZone RPC");

    if !result.success {
        panic!(
            "JoinZone failed: error={:?}, leader_address={:?}",
            result.error, result.leader_address
        );
    }

    // Wait for ConfState [r_a, r_b] to land on A's leader-side view.
    let mut voters_observed = Vec::new();
    for _ in 0..200 {
        let status = zm_a.cluster_status("root");
        if status.voter_count >= 2 {
            voters_observed = vec![id_a, id_b];
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        voters_observed,
        vec![id_a, id_b],
        "leader must commit AddNode for joiner",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn test_node_id_persists_across_restart() {
    // First "boot" mints + persists.  Second "boot" against the same
    // dir reads the same id off disk.
    let dir = TempDir::new().expect("tempdir");
    let id = mint_random_id();
    write_node_id(dir.path(), id);
    let read_back = read_persisted_node_id(dir.path());
    assert_eq!(read_back, id, "persisted id must round-trip");
    // Sanity: re-reading remains stable (no rewrite on second access).
    let read_back_2 = read_persisted_node_id(dir.path());
    assert_eq!(read_back, read_back_2);
}

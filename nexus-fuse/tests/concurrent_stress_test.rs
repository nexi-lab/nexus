//! Concurrent stress test for the async NexusClient (issue #4056
//! adversarial-review round-1 follow-up).
//!
//! Drives one shared `NexusClient` from many threads issuing a
//! mix of `read`, `read_with_etag`, and `stat` calls and asserts no
//! panic / timeout / runtime drop-in-async-context bug surfaces under
//! load. The interesting failure mode for #4056 is a deadlock or
//! panic when many fuser-style sync callers all block_on the shared
//! `OnceLock<Runtime>` — single-runtime-multi-caller pattern.
//!
//! mockito drives a current-thread server so absolute throughput
//! numbers aren't meaningful here; the assertion is "every op
//! completes without panic", not throughput.

use mockito::Server;
use nexus_fuse::client::NexusClient;
use std::sync::Arc;
use std::thread;

const READ_BODY: &str =
    r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YmVuY2hkYXRh"}}"#;
const STAT_BODY: &str =
    r#"{"jsonrpc":"2.0","id":1,"result":{"size":10,"gen":1,"etag":"abc","modified_at":null,"is_directory":false}}"#;

#[test]
fn concurrent_mixed_ops_do_not_panic_or_deadlock() {
    let mut server = Server::new();

    // Stand up open-ended mocks so unbounded concurrent calls all match.
    let _read = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_header("etag", "\"abc\"")
        .with_body(READ_BODY)
        .expect_at_least(1)
        .create();
    let _stat = server
        .mock("POST", "/api/nfs/stat")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(STAT_BODY)
        .expect_at_least(1)
        .create();

    let client = Arc::new(NexusClient::new(&server.url(), "stress-key", None).unwrap());

    // Mixed workload: 16 threads × 32 iterations × 3 op kinds = 1,536
    // op invocations through the shared HTTP_RUNTIME. If the runtime
    // gets dropped in an async context, or block_on nests on the
    // wrong thread, this will panic and fail the test.
    let threads = 16;
    let iters = 32;
    let mut handles = Vec::with_capacity(threads);
    for tid in 0..threads {
        let client = client.clone();
        handles.push(thread::spawn(move || {
            for i in 0..iters {
                match (tid + i) % 3 {
                    0 => {
                        let bytes = client.read("/x.txt").expect("read");
                        assert!(!bytes.is_empty());
                    }
                    1 => {
                        let resp = client
                            .read_with_etag("/x.txt", Some("abc"))
                            .expect("read_with_etag");
                        // Either Content or NotModified is fine; mock
                        // returns 200 so we expect Content here.
                        match resp {
                            nexus_fuse::client::ReadResponse::Content { .. }
                            | nexus_fuse::client::ReadResponse::NotModified => {}
                        }
                    }
                    _ => {
                        let meta = client.stat("/x.txt").expect("stat");
                        assert_eq!(meta.gen, 1);
                    }
                }
            }
        }));
    }

    for h in handles {
        h.join().expect("worker panicked");
    }
}

#[test]
fn cloned_client_is_independent_under_load() {
    // The OnceLock-shared HTTP_RUNTIME is supposed to survive arbitrary
    // clones of NexusClient. Build 8 clones, hand each to its own
    // thread, hammer them in parallel — there must be no panic even if
    // some clones drop mid-call.
    let mut server = Server::new();
    let _read = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(READ_BODY)
        .expect_at_least(1)
        .create();

    let base = NexusClient::new(&server.url(), "k", None).unwrap();
    let clones: Vec<NexusClient> = (0..8).map(|_| base.clone()).collect();
    drop(base); // ensure clones outlive the original

    let mut handles = Vec::new();
    for c in clones {
        handles.push(thread::spawn(move || {
            for _ in 0..32 {
                let _ = c.read("/x.txt").expect("read");
            }
            // Drop happens at thread exit; runtime must survive.
        }));
    }
    for h in handles {
        h.join().expect("worker panicked");
    }
}

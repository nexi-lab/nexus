//! Concurrent-read throughput benchmark for issue #4056.
//!
//! Measures the post-#4056 `NexusClient` (pooled async hyper + shared
//! multi-thread tokio runtime via `OnceLock`) under varying caller
//! concurrency against a local multi-thread HTTP/1.1 responder.
//!
//! **About the pre-PR baseline**
//!
//! Earlier rounds of adversarial review (R1, R2) replaced an inflated
//! "fresh client per call" baseline with a "shared async client +
//! shared current-thread runtime" emulation of `reqwest::blocking`.
//! Round 7 flagged that even that emulation isn't faithful: reqwest
//! 0.13's `blocking::Client` actually owns a dedicated reqwest-
//! internal sync-runtime thread, dispatches requests over an mpsc
//! channel, and `tokio::spawn`s each request there. A truly faithful
//! baseline would have to compile against the `blocking` feature,
//! which this crate dropped as part of #4056.
//!
//! Rather than ship numbers built on an emulation the reviewer
//! correctly rejected as not-quite-pre-PR, this bench reports only
//! the post-#4056 throughput at varying concurrency. The "≥2× vs
//! pre-PR" acceptance row in `PERFORMANCE_RESULTS.md` is honestly
//! marked "not met" — running the real `reqwest::blocking` baseline
//! requires a separate checkout/branch with the blocking feature.
//!
//! Why a raw multi-thread server instead of mockito: mockito's server
//! runs on a `current_thread` tokio runtime and accepts one request
//! at a time, collapsing any client-side concurrency win. Hand-rolling
//! an HTTP/1.1 responder on a multi-thread tokio runtime keeps the
//! server out of the way so the benchmark really measures the client.
//!
//! Run with: cargo bench --bench concurrent_read

use nexus_fuse::client::NexusClient;
use std::hint::black_box;
use std::net::SocketAddr;
use std::sync::Arc;
use std::thread;
use std::time::Instant;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::runtime::Runtime;

const READ_PATH: &str = "/bench-read.txt";
const RESPONSE_BODY: &str =
    r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YmVuY2hkYXRh"}}"#;

struct BenchServer {
    addr: SocketAddr,
    _runtime: Runtime,
}

impl BenchServer {
    fn url(&self) -> String {
        format!("http://{}", self.addr)
    }
}

fn spawn_bench_server() -> BenchServer {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .enable_all()
        .thread_name("bench-server")
        .build()
        .expect("build bench server runtime");

    let (tx, rx) = std::sync::mpsc::channel();

    runtime.spawn(async move {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind bench server");
        let addr = listener.local_addr().expect("local_addr");
        tx.send(addr).expect("send addr");
        loop {
            let (mut socket, _) = match listener.accept().await {
                Ok(pair) => pair,
                Err(_) => continue,
            };
            tokio::spawn(async move {
                let mut buf = [0u8; 4096];
                loop {
                    let n = match socket.read(&mut buf).await {
                        Ok(0) => break,
                        Ok(n) => n,
                        Err(_) => break,
                    };
                    let _ = n;
                    let response = format!(
                        "HTTP/1.1 200 OK\r\n\
                         Content-Type: application/json\r\n\
                         Content-Length: {}\r\n\
                         Connection: keep-alive\r\n\
                         ETag: \"bench\"\r\n\
                         \r\n{}",
                        RESPONSE_BODY.len(),
                        RESPONSE_BODY
                    );
                    if socket.write_all(response.as_bytes()).await.is_err() {
                        break;
                    }
                }
            });
        }
    });

    let addr = rx.recv().expect("bench server bind");
    BenchServer {
        addr,
        _runtime: runtime,
    }
}

/// Drive the shared, post-#4056 `NexusClient` from `threads` parallel
/// reader threads, each issuing `ops_per_thread` `client.read` calls.
/// Every reader rides the same hyper connection pool / HTTP keep-alive.
fn run_pooled(client: &Arc<NexusClient>, threads: usize, ops_per_thread: usize) {
    let mut handles = Vec::with_capacity(threads);
    for _ in 0..threads {
        let client = client.clone();
        handles.push(thread::spawn(move || {
            for _ in 0..ops_per_thread {
                let bytes = client.read(READ_PATH).expect("read failed");
                black_box(bytes);
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
}

fn measure<F: FnOnce()>(f: F, total_ops: usize) -> f64 {
    let start = Instant::now();
    f();
    let elapsed = start.elapsed().as_secs_f64();
    total_ops as f64 / elapsed
}

fn main() {
    let server = spawn_bench_server();
    let url = server.url();
    let api_key = "bench-key";
    let pooled_client = Arc::new(NexusClient::new(&url, api_key, None).expect("client build"));

    // Warm pooled connections so the first measured iteration isn't
    // paying TCP-handshake amortization.
    for _ in 0..8 {
        let _ = pooled_client.read(READ_PATH);
    }

    println!("{:<10} {:<14}", "threads", "pooled ops/s");
    println!("{:-<26}", "");

    for &threads in &[1usize, 4, 8, 16, 32] {
        let ops_per_thread = 256;
        let thrpt = measure(
            || run_pooled(&pooled_client, threads, ops_per_thread),
            threads * ops_per_thread,
        );
        println!("{:<10} {:<14.0}", threads, thrpt);
    }
}

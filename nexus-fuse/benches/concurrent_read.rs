//! Concurrent-read throughput benchmark for issue #4056.
//!
//! Issue acceptance: pooled async client (post-#4056) achieves ≥2×
//! the concurrent-read throughput of an "equivalent of pre-#4056" path.
//! This bench is a one-shot driver (not criterion) — it runs each
//! scenario once, prints ops/sec, and computes a speedup ratio.
//!
//! **Baselines**
//!
//! - `pooled` (post-#4056): one shared `NexusClient` whose internal
//!   `reqwest::Client` rides the process-wide multi-thread tokio
//!   runtime (`HTTP_RUNTIME` `OnceLock`). All reader threads share one
//!   connection pool with HTTP keep-alive.
//!
//! - `pre_pr_blocking` (faithful pre-#4056 emulation): one shared
//!   async `reqwest::Client` driven by a single shared **current-thread**
//!   tokio runtime, with every read going through `runtime.block_on`.
//!   This matches what `reqwest::blocking::Client` did internally
//!   pre-#4056 — one client, one current-thread runtime that every
//!   blocking call multiplexed through. Reviewer-requested replacement
//!   for the prior `unpooled` (fresh-client-per-call) baseline, which
//!   exaggerated the speedup by manufacturing a worst-case lifecycle
//!   the production code never had (round-1 adversarial review).
//!
//! - `unpooled` (worst case, for context only): fresh `NexusClient`
//!   per call. Kept because it bounds the cost of the
//!   no-pool-reuse-at-all regression risk; *not* a faithful pre-PR
//!   replica. Run with limited ops/thread because each iteration
//!   leaves a socket in TIME_WAIT.
//!
//! Why a raw multi-thread server instead of mockito: mockito's server
//! runs on a `current_thread` tokio runtime and accepts one request
//! at a time, which collapses any pool-vs-no-pool signal. Hand-rolling
//! an HTTP/1.1 responder on a multi-thread tokio runtime keeps the
//! server out of the way so the benchmark really measures the client.
//!
//! Run with: cargo bench --bench concurrent_read

use nexus_fuse::client::NexusClient;
use std::hint::black_box;
use std::net::SocketAddr;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
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

/// Build a JSON-RPC `read` request body matching what `NexusClient`
/// sends — same path, same JSON-RPC envelope. Lets the baselines hit
/// the same server endpoint as the pooled path without depending on
/// NexusClient internals.
fn build_read_body() -> String {
    format!(
        r#"{{"jsonrpc":"2.0","id":1,"method":"read","params":{{"path":"{}"}}}}"#,
        READ_PATH
    )
}

/// Pooled path (post-#4056): one shared NexusClient. All reader
/// threads reuse the same hyper connection pool with HTTP keep-alive.
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

/// Faithful pre-#4056 baseline: shared async `reqwest::Client` driven
/// by a single shared **current-thread** tokio runtime. Mirrors what
/// `reqwest::blocking::Client` did internally — one client, one
/// current-thread runtime, every call multiplexed through it. The
/// current-thread runtime serializes polling, so concurrent callers
/// queue at the runtime rather than at TCP setup; that's the actual
/// pre-PR bottleneck the migration was meant to lift.
fn run_pre_pr_blocking(
    client: Arc<reqwest::Client>,
    runtime: Arc<Runtime>,
    url: &str,
    threads: usize,
    ops_per_thread: usize,
) {
    let body = build_read_body();
    let url = format!("{}/api/nfs/read", url);
    let mut handles = Vec::with_capacity(threads);
    for _ in 0..threads {
        let client = client.clone();
        let runtime = runtime.clone();
        let url = url.clone();
        let body = body.clone();
        handles.push(thread::spawn(move || {
            for _ in 0..ops_per_thread {
                let bytes = runtime.block_on(async {
                    let resp = client
                        .post(&url)
                        .header("content-type", "application/json")
                        .body(body.clone())
                        .send()
                        .await
                        .expect("send");
                    resp.bytes().await.expect("read body")
                });
                black_box(bytes);
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
}

/// Unpooled baseline (worst case): fresh `NexusClient` per call.
/// Bounds the no-pool-reuse-at-all regression risk; kept for context
/// but not a faithful pre-PR replica.
fn run_unpooled(url: &str, api_key: &str, threads: usize, ops_per_thread: usize) {
    let mut handles = Vec::with_capacity(threads);
    for _ in 0..threads {
        let url = url.to_string();
        let api_key = api_key.to_string();
        handles.push(thread::spawn(move || {
            for _ in 0..ops_per_thread {
                let client = NexusClient::new(&url, &api_key, None).expect("client build");
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

fn build_pre_pr_runtime() -> Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .thread_name("pre-pr-bench")
        .build()
        .expect("build current-thread runtime")
}

fn build_pre_pr_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .connect_timeout(Duration::from_secs(5))
        .pool_max_idle_per_host(64)
        .pool_idle_timeout(Some(Duration::from_secs(60)))
        .tcp_keepalive(Some(Duration::from_secs(30)))
        .no_proxy()
        .build()
        .expect("build pre-pr client")
}

fn main() {
    let server = spawn_bench_server();
    let url = server.url();
    let api_key = "bench-key";
    let pooled_client = Arc::new(NexusClient::new(&url, api_key, None).expect("client build"));

    // Warm pooled.
    for _ in 0..8 {
        let _ = pooled_client.read(READ_PATH);
    }

    // Faithful pre-PR baseline: one shared async client + one shared
    // current-thread runtime that every "blocking" call multiplexes
    // through (matches reqwest::blocking::Client semantics). The
    // client must be built *inside* the runtime — reqwest registers
    // its connector with the current reactor at construction.
    let pre_pr_runtime = Arc::new(build_pre_pr_runtime());
    let pre_pr_client = Arc::new(pre_pr_runtime.block_on(async { build_pre_pr_client() }));
    // Warm pre-PR baseline. The request future has to be built
    // inside `block_on` because reqwest's send() registers a
    // tokio::time::Sleep for the request timeout, and Sleep::new
    // requires runtime context (not just future-polling context).
    let warm_body = build_read_body();
    let warm_url = format!("{}/api/nfs/read", url);
    for _ in 0..8 {
        let client = pre_pr_client.clone();
        let url_inner = warm_url.clone();
        let body_inner = warm_body.clone();
        let _ = pre_pr_runtime.block_on(async move {
            client
                .post(&url_inner)
                .header("content-type", "application/json")
                .body(body_inner)
                .send()
                .await
        });
    }

    println!(
        "{:<10} {:<14} {:<18} {:<14} {:<10} {:<10}",
        "threads",
        "pooled ops/s",
        "pre_pr_blocking",
        "unpooled ops/s",
        "vs pre-PR",
        "vs unpool",
    );
    println!("{:-<78}", "");

    for &threads in &[1usize, 4, 8, 16] {
        let pooled_ops = 256;
        let pre_pr_ops = 256;
        let unpooled_ops = 8; // bounded to keep total connections under ephemeral-port pressure

        let pooled_thrpt = measure(
            || run_pooled(&pooled_client, threads, pooled_ops),
            threads * pooled_ops,
        );
        let pre_pr_thrpt = measure(
            || {
                run_pre_pr_blocking(
                    pre_pr_client.clone(),
                    pre_pr_runtime.clone(),
                    &url,
                    threads,
                    pre_pr_ops,
                )
            },
            threads * pre_pr_ops,
        );
        let unpooled_thrpt = measure(
            || run_unpooled(&url, api_key, threads, unpooled_ops),
            threads * unpooled_ops,
        );
        let vs_pre_pr = pooled_thrpt / pre_pr_thrpt;
        let vs_unpool = pooled_thrpt / unpooled_thrpt;

        println!(
            "{:<10} {:<14.0} {:<18.0} {:<14.0} {:<10.2} {:<10.2}",
            threads, pooled_thrpt, pre_pr_thrpt, unpooled_thrpt, vs_pre_pr, vs_unpool,
        );
    }
}

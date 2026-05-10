//! Concurrent-read throughput benchmark for issue #4056.
//!
//! Issue acceptance: pooled async client (post-#4056) achieves ≥2×
//! the concurrent-read throughput of an unpooled baseline. This is a
//! one-shot benchmark (not criterion) — it runs each scenario once,
//! prints ops/sec, and computes the speedup ratio. Criterion's many-
//! sample sweep was incompatible with the unpooled path: each call
//! opens a fresh TCP socket and macOS's ephemeral-port range (~16k
//! with a 60s TIME_WAIT) is exhausted within a single measurement
//! window. One pass with bounded workload sidesteps that and is still
//! enough to confirm the order-of-magnitude.
//!
//! Why a raw multi-thread server instead of mockito: mockito's server
//! runs on a `current_thread` tokio runtime and accepts one request
//! at a time, which silently caps concurrency at one regardless of
//! caller threads. That collapses any pool-vs-no-pool signal. Hand-
//! rolling an HTTP/1.1 responder on a multi-thread tokio runtime keeps
//! the server out of the way so the benchmark really measures the
//! client.
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

/// Pooled path: one shared NexusClient. All reader threads reuse the
/// same hyper connection pool with HTTP keep-alive.
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

/// Unpooled baseline: every read constructs a fresh NexusClient — and
/// therefore a fresh hyper connection pool that must dial from scratch.
/// Emulates the worst-case behavior the issue describes.
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

fn main() {
    let server = spawn_bench_server();
    let url = server.url();
    let api_key = "bench-key";
    let pooled_client = Arc::new(NexusClient::new(&url, api_key, None).expect("client build"));

    // Warm both paths.
    for _ in 0..8 {
        let _ = pooled_client.read(READ_PATH);
    }

    println!(
        "{:<10} {:<14} {:<14} {:<8}",
        "threads", "pooled (ops/s)", "unpooled (ops/s)", "speedup"
    );
    println!("{:-<46}", "");

    for &threads in &[1usize, 4, 8, 16] {
        let pooled_ops = 256;
        let unpooled_ops = 8; // bounded to keep total connections under ephemeral-port pressure

        let pooled_thrpt = measure(
            || run_pooled(&pooled_client, threads, pooled_ops),
            threads * pooled_ops,
        );
        let unpooled_thrpt = measure(
            || run_unpooled(&url, api_key, threads, unpooled_ops),
            threads * unpooled_ops,
        );
        let speedup = pooled_thrpt / unpooled_thrpt;

        println!(
            "{:<10} {:<14.0} {:<14.0} {:.2}x",
            threads, pooled_thrpt, unpooled_thrpt, speedup
        );
    }
}

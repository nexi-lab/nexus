use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use nexus_fuse::metrics;

static TEST_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

fn test_guard() -> std::sync::MutexGuard<'static, ()> {
    TEST_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[test]
fn render_includes_recorded_counter_gauge_and_histogram() {
    let _guard = test_guard();
    metrics::reset_for_tests();

    metrics::record_cache_request("sqlite", "hit");
    metrics::set_cache_bytes_in_use("sqlite", 4096);
    metrics::record_read("cache", 128, Duration::from_millis(2));
    metrics::record_write_backend_rpc();

    let body = metrics::render();

    assert!(body.contains("nexus_cache_requests_total{tier=\"sqlite\",result=\"hit\"} 1"));
    assert!(body.contains("nexus_cache_bytes_in_use{tier=\"sqlite\"} 4096"));
    assert!(body.contains("nexus_read_bytes_total{tier=\"cache\"} 128"));
    assert!(body.contains("nexus_read_latency_seconds_count{tier=\"cache\"} 1"));
    assert!(body.contains("nexus_write_backend_rpc_total 1"));
}

#[test]
fn render_exposes_full_issue_metric_catalog_after_reset() {
    let _guard = test_guard();
    metrics::reset_for_tests();

    let body = metrics::render();

    for metric in [
        "nexus_cache_requests_total",
        "nexus_cache_hit_ratio",
        "nexus_cache_evictions_total",
        "nexus_cache_bytes_in_use",
        "nexus_cache_admission_rejected_total",
        "nexus_cache_etag_revalidate_total",
        "nexus_prefetch_issued_bytes_total",
        "nexus_prefetch_used_bytes_total",
        "nexus_prefetch_wasted_bytes_total",
        "nexus_prefetch_window_size",
        "nexus_prefetch_pattern_detected_total",
        "nexus_read_latency_seconds",
        "nexus_read_bytes_total",
        "nexus_read_batch_size",
        "nexus_fuse_passthrough_used_total",
        "nexus_write_coalesce_flush_total",
        "nexus_write_coalesce_dirty_bytes",
        "nexus_write_backend_rpc_total",
        "nexus_generation_mismatch_total",
        "nexus_etag_check_total",
    ] {
        assert!(
            body.contains(metric),
            "missing metric catalog entry {metric}"
        );
    }
}

#[test]
fn render_uses_python_compatible_histogram_bucket_labels() {
    let _guard = test_guard();
    metrics::reset_for_tests();

    metrics::record_read("backend", 128, Duration::from_secs(12));

    let body = metrics::render();

    assert!(body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"1.0\"}"));
    assert!(body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"5.0\"}"));
    assert!(body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"10.0\"}"));
    assert!(!body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"1\"}"));
    assert!(!body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"5\"}"));
    assert!(!body.contains("nexus_read_latency_seconds_bucket{tier=\"backend\",le=\"10\"}"));
}

#[test]
fn prefetch_window_unknown_scopes_default_to_default() {
    let _guard = test_guard();
    metrics::reset_for_tests();

    metrics::set_prefetch_window_size(2048, "tenant-123", "path-/secret");

    let body = metrics::render();
    assert!(
        body.contains("nexus_prefetch_window_size{mount=\"default\",workspace=\"default\"} 2048")
    );
}

#[test]
fn unknown_labels_collapse_to_other() {
    let _guard = test_guard();
    metrics::reset_for_tests();

    metrics::record_cache_request("path-/secret", "tenant-123");

    let body = metrics::render();
    assert!(body.contains("nexus_cache_requests_total{tier=\"other\",result=\"other\"} 1"));
}

#[test]
fn metrics_server_returns_prometheus_text() {
    let _guard = test_guard();
    metrics::reset_for_tests();
    metrics::record_write_backend_rpc();

    let server = metrics::start_server("127.0.0.1:0").expect("metrics server should bind");
    let addr = server.local_addr();

    let mut stream = TcpStream::connect(addr).expect("connect metrics server");
    stream
        .write_all(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");

    let mut body = String::new();
    stream.read_to_string(&mut body).expect("read response");
    assert!(body.contains("HTTP/1.1 200 OK"));
    assert!(body.contains("nexus_write_backend_rpc_total 1"));
}

#[test]
fn idle_client_does_not_block_later_scrape() {
    let _guard = test_guard();
    metrics::reset_for_tests();
    metrics::record_write_backend_rpc();

    let server = metrics::start_server("127.0.0.1:0").expect("metrics server should bind");
    let addr = server.local_addr();

    let _idle = TcpStream::connect(addr).expect("connect idle client");

    let mut stream = TcpStream::connect(addr).expect("connect scraping client");
    stream
        .set_read_timeout(Some(Duration::from_millis(500)))
        .expect("set scrape read timeout");
    stream
        .write_all(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");

    let mut body = String::new();
    stream.read_to_string(&mut body).expect("read response");
    assert!(body.contains("HTTP/1.1 200 OK"));
    assert!(body.contains("nexus_write_backend_rpc_total 1"));
}

#[test]
fn dropping_server_releases_bound_address() {
    let _guard = test_guard();

    let server = metrics::start_server("127.0.0.1:0").expect("metrics server should bind");
    let addr = server.local_addr();
    drop(server);

    let rebound = metrics::start_server(&addr.to_string()).expect("metrics server should rebind");
    assert_eq!(rebound.local_addr(), addr);
}

#[test]
fn server_rejects_non_metrics_paths() {
    let _guard = test_guard();

    let server = metrics::start_server("127.0.0.1:0").expect("metrics server should bind");
    let addr = server.local_addr();

    let mut stream = TcpStream::connect(addr).expect("connect metrics server");
    stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .expect("write request");

    let mut body = String::new();
    stream.read_to_string(&mut body).expect("read response");
    assert!(!body.contains("HTTP/1.1 200 OK"));
}

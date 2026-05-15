//! Prometheus text metrics for the standalone nexus-fuse binary (#4062).

use std::collections::HashMap;
use std::fmt::Display;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, LazyLock, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const READ_LATENCY_BUCKETS: &[f64] = &[
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];
const READ_BATCH_SIZE_BUCKETS: &[f64] =
    &[1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0];

#[derive(Clone, Debug)]
struct HistogramState {
    buckets: &'static [f64],
    bucket_counts: Vec<u64>,
    count: u64,
    sum: f64,
}

impl HistogramState {
    fn new(buckets: &'static [f64]) -> Self {
        Self {
            buckets,
            bucket_counts: vec![0; buckets.len()],
            count: 0,
            sum: 0.0,
        }
    }

    fn observe(&mut self, value: f64) {
        let safe = if value.is_finite() && value > 0.0 {
            value
        } else {
            0.0
        };
        self.count += 1;
        self.sum += safe;
        for (idx, bucket) in self.buckets.iter().enumerate() {
            if safe <= *bucket {
                self.bucket_counts[idx] += 1;
            }
        }
    }
}

struct MetricsState {
    cache_requests: HashMap<(String, String), u64>,
    cache_hit_ratio: HashMap<String, f64>,
    cache_evictions: HashMap<(String, String), u64>,
    cache_etag_revalidate: HashMap<String, u64>,
    etag_checks: HashMap<String, u64>,
    cache_bytes_in_use: HashMap<String, u64>,
    cache_admission_rejected_total: u64,
    prefetch_issued_bytes_total: u64,
    prefetch_used_bytes_total: u64,
    prefetch_wasted_bytes_total: u64,
    prefetch_window_size: HashMap<(String, String), u64>,
    prefetch_pattern_detected: HashMap<String, u64>,
    read_bytes: HashMap<String, u64>,
    read_latency: HashMap<String, HistogramState>,
    read_batch_size: HistogramState,
    fuse_passthrough_used_total: u64,
    write_coalesce_flush: HashMap<String, u64>,
    write_coalesce_dirty_bytes: u64,
    write_backend_rpc_total: u64,
    generation_mismatch_total: u64,
    hydration_files: HashMap<String, u64>,
    hydration_bytes: HashMap<String, u64>,
    hydration_duration_ms_total: u64,
}

impl Default for MetricsState {
    fn default() -> Self {
        Self {
            cache_requests: HashMap::new(),
            cache_hit_ratio: HashMap::new(),
            cache_evictions: HashMap::new(),
            cache_etag_revalidate: HashMap::new(),
            etag_checks: HashMap::new(),
            cache_bytes_in_use: HashMap::new(),
            cache_admission_rejected_total: 0,
            prefetch_issued_bytes_total: 0,
            prefetch_used_bytes_total: 0,
            prefetch_wasted_bytes_total: 0,
            prefetch_window_size: HashMap::new(),
            prefetch_pattern_detected: HashMap::new(),
            read_bytes: HashMap::new(),
            read_latency: HashMap::new(),
            read_batch_size: HistogramState::new(READ_BATCH_SIZE_BUCKETS),
            fuse_passthrough_used_total: 0,
            write_coalesce_flush: HashMap::new(),
            write_coalesce_dirty_bytes: 0,
            write_backend_rpc_total: 0,
            generation_mismatch_total: 0,
            hydration_files: HashMap::new(),
            hydration_bytes: HashMap::new(),
            hydration_duration_ms_total: 0,
        }
    }
}

static METRICS: LazyLock<Mutex<MetricsState>> =
    LazyLock::new(|| Mutex::new(MetricsState::default()));

#[cfg(test)]
static TEST_METRICS_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

#[cfg(test)]
pub(crate) fn test_guard() -> std::sync::MutexGuard<'static, ()> {
    TEST_METRICS_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub struct MetricsServer {
    local_addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl MetricsServer {
    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }
}

impl Drop for MetricsServer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.local_addr);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn bounded(value: &str, allowed: &[&str]) -> String {
    let normalized = value.trim().to_ascii_lowercase().replace('-', "_");
    if allowed.iter().any(|item| *item == normalized) {
        normalized
    } else {
        "other".to_string()
    }
}

fn cache_tier(value: &str) -> String {
    bounded(value, &["sqlite", "dram", "nvme", "l1", "l2", "other"])
}

fn cache_result(value: &str) -> String {
    bounded(value, &["hit", "miss", "stale", "other"])
}

fn cache_eviction_reason(value: &str) -> String {
    bounded(value, &["capacity", "ttl", "manual", "other"])
}

fn hydration_file_result(value: &str) -> String {
    bounded(
        value,
        &[
            "admitted",
            "skipped_warm",
            "skipped_size",
            "skipped_budget",
            "failed",
            "other",
        ],
    )
}

fn hydration_bytes_result(value: &str) -> String {
    bounded(value, &["admitted", "skipped", "other"])
}

fn etag_result(value: &str) -> String {
    bounded(
        value,
        &[
            "304",
            "updated",
            "error",
            "fallback",
            "unexpected_304",
            "other",
        ],
    )
}

fn prefetch_pattern(value: &str) -> String {
    bounded(
        value,
        &["sequential", "stride", "random", "majority_trend", "other"],
    )
}

fn read_tier(value: &str) -> String {
    bounded(
        value,
        &[
            "backend",
            "virtual",
            "error",
            "batch",
            "cache",
            "sqlite",
            "dram",
            "nvme",
            "passthrough",
            "other",
        ],
    )
}

fn write_flush_trigger(value: &str) -> String {
    bounded(
        value,
        &["time", "bytes", "close", "sync", "snapshot", "other"],
    )
}

fn scope(value: &str) -> String {
    let normalized = value.trim().to_ascii_lowercase().replace('-', "_");
    if ["default", "root", "local", "server", "fuse"].contains(&normalized.as_str()) {
        normalized
    } else {
        "default".to_string()
    }
}

pub fn reset_for_tests() {
    *METRICS.lock().unwrap() = MetricsState::default();
}

pub fn record_cache_request(tier: &str, result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .cache_requests
        .entry((cache_tier(tier), cache_result(result)))
        .or_insert(0) += 1;
}

pub fn set_cache_hit_ratio(tier: &str, ratio: f64) {
    let mut metrics = METRICS.lock().unwrap();
    let bounded_ratio = if ratio.is_finite() {
        ratio.clamp(0.0, 1.0)
    } else {
        0.0
    };
    metrics
        .cache_hit_ratio
        .insert(cache_tier(tier), bounded_ratio);
}

pub fn record_cache_eviction(tier: &str, reason: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .cache_evictions
        .entry((cache_tier(tier), cache_eviction_reason(reason)))
        .or_insert(0) += 1;
}

pub fn record_cache_etag_revalidate(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .cache_etag_revalidate
        .entry(etag_result(result))
        .or_insert(0) += 1;
}

pub fn record_cache_admission_rejected() {
    let mut metrics = METRICS.lock().unwrap();
    metrics.cache_admission_rejected_total += 1;
}

pub fn record_etag_check(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics.etag_checks.entry(etag_result(result)).or_insert(0) += 1;
}

pub fn record_prefetch_issued(bytes: usize) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.prefetch_issued_bytes_total += bytes as u64;
}

pub fn record_prefetch_used(bytes: usize) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.prefetch_used_bytes_total += bytes as u64;
}

pub fn record_prefetch_wasted(bytes: usize) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.prefetch_wasted_bytes_total += bytes as u64;
}

pub fn set_prefetch_window_size(window_size: u64, mount: &str, workspace: &str) {
    let mut metrics = METRICS.lock().unwrap();
    metrics
        .prefetch_window_size
        .insert((scope(mount), scope(workspace)), window_size);
}

pub fn record_prefetch_pattern(pattern: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .prefetch_pattern_detected
        .entry(prefetch_pattern(pattern))
        .or_insert(0) += 1;
}

pub fn set_cache_bytes_in_use(tier: &str, bytes: u64) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.cache_bytes_in_use.insert(cache_tier(tier), bytes);
}

pub fn record_read(tier: &str, bytes: usize, latency: Duration) {
    let safe_tier = read_tier(tier);
    let mut metrics = METRICS.lock().unwrap();
    *metrics.read_bytes.entry(safe_tier.clone()).or_insert(0) += bytes as u64;
    metrics
        .read_latency
        .entry(safe_tier)
        .or_insert_with(|| HistogramState::new(READ_LATENCY_BUCKETS))
        .observe(latency.as_secs_f64());
}

pub fn record_read_batch_size(count: usize) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.read_batch_size.observe(count as f64);
}

pub fn record_fuse_passthrough_used() {
    let mut metrics = METRICS.lock().unwrap();
    metrics.fuse_passthrough_used_total += 1;
}

pub fn record_write_coalesce_flush(trigger: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .write_coalesce_flush
        .entry(write_flush_trigger(trigger))
        .or_insert(0) += 1;
}

pub fn set_write_coalesce_dirty_bytes(bytes: u64) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.write_coalesce_dirty_bytes = bytes;
}

pub fn record_write_backend_rpc() {
    let mut metrics = METRICS.lock().unwrap();
    metrics.write_backend_rpc_total += 1;
}

pub fn record_generation_mismatch() {
    let mut metrics = METRICS.lock().unwrap();
    metrics.generation_mismatch_total += 1;
}

pub fn record_hydration_file(result: &str) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .hydration_files
        .entry(hydration_file_result(result))
        .or_insert(0) += 1;
}

pub fn record_hydration_bytes(result: &str, n: u64) {
    let mut metrics = METRICS.lock().unwrap();
    *metrics
        .hydration_bytes
        .entry(hydration_bytes_result(result))
        .or_insert(0) += n;
}

pub fn observe_hydration_duration_ms(ms: u64) {
    let mut metrics = METRICS.lock().unwrap();
    metrics.hydration_duration_ms_total = metrics.hydration_duration_ms_total.saturating_add(ms);
}

fn write_sample_line(out: &mut String, name: &str, labels: &str, value: impl Display) {
    if labels.is_empty() {
        out.push_str(&format!("{name} {value}\n"));
    } else {
        out.push_str(&format!("{name}{{{labels}}} {value}\n"));
    }
}

fn write_counter_line(out: &mut String, name: &str, labels: &str, value: u64) {
    write_sample_line(out, name, labels, value);
}

fn bucket_label(bucket: f64) -> String {
    if bucket.fract() == 0.0 {
        format!("{bucket:.1}")
    } else {
        bucket.to_string()
    }
}

fn write_histogram(out: &mut String, name: &str, labels: &str, histogram: &HistogramState) {
    for (idx, bucket) in histogram.buckets.iter().enumerate() {
        let bucket_labels = if labels.is_empty() {
            format!("le=\"{}\"", bucket_label(*bucket))
        } else {
            format!("{labels},le=\"{}\"", bucket_label(*bucket))
        };
        write_counter_line(
            out,
            &format!("{name}_bucket"),
            &bucket_labels,
            histogram.bucket_counts[idx],
        );
    }

    let inf_labels = if labels.is_empty() {
        "le=\"+Inf\"".to_string()
    } else {
        format!("{labels},le=\"+Inf\"")
    };
    write_counter_line(out, &format!("{name}_bucket"), &inf_labels, histogram.count);
    write_sample_line(out, &format!("{name}_sum"), labels, histogram.sum);
    write_counter_line(out, &format!("{name}_count"), labels, histogram.count);
}

pub fn render() -> String {
    let metrics = METRICS.lock().unwrap();
    let mut out = String::new();

    out.push_str("# TYPE nexus_cache_requests_total counter\n");
    let mut cache_requests: Vec<_> = metrics.cache_requests.iter().collect();
    cache_requests.sort_by(|left, right| left.0.cmp(right.0));
    for ((tier, result), value) in cache_requests {
        write_counter_line(
            &mut out,
            "nexus_cache_requests_total",
            &format!("tier=\"{tier}\",result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_cache_hit_ratio gauge\n");
    let mut cache_hit_ratio: Vec<_> = metrics.cache_hit_ratio.iter().collect();
    cache_hit_ratio.sort_by(|left, right| left.0.cmp(right.0));
    for (tier, value) in cache_hit_ratio {
        write_sample_line(
            &mut out,
            "nexus_cache_hit_ratio",
            &format!("tier=\"{tier}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_cache_evictions_total counter\n");
    let mut cache_evictions: Vec<_> = metrics.cache_evictions.iter().collect();
    cache_evictions.sort_by(|left, right| left.0.cmp(right.0));
    for ((tier, reason), value) in cache_evictions {
        write_counter_line(
            &mut out,
            "nexus_cache_evictions_total",
            &format!("tier=\"{tier}\",reason=\"{reason}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_cache_bytes_in_use gauge\n");
    let mut cache_bytes_in_use: Vec<_> = metrics.cache_bytes_in_use.iter().collect();
    cache_bytes_in_use.sort_by(|left, right| left.0.cmp(right.0));
    for (tier, value) in cache_bytes_in_use {
        out.push_str(&format!(
            "nexus_cache_bytes_in_use{{tier=\"{tier}\"}} {value}\n"
        ));
    }

    out.push_str("# TYPE nexus_cache_admission_rejected_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_cache_admission_rejected_total",
        "",
        metrics.cache_admission_rejected_total,
    );

    out.push_str("# TYPE nexus_cache_etag_revalidate_total counter\n");
    let mut cache_etag_revalidate: Vec<_> = metrics.cache_etag_revalidate.iter().collect();
    cache_etag_revalidate.sort_by(|left, right| left.0.cmp(right.0));
    for (result, value) in cache_etag_revalidate {
        write_counter_line(
            &mut out,
            "nexus_cache_etag_revalidate_total",
            &format!("result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_prefetch_issued_bytes_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_prefetch_issued_bytes_total",
        "",
        metrics.prefetch_issued_bytes_total,
    );

    out.push_str("# TYPE nexus_prefetch_used_bytes_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_prefetch_used_bytes_total",
        "",
        metrics.prefetch_used_bytes_total,
    );

    out.push_str("# TYPE nexus_prefetch_wasted_bytes_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_prefetch_wasted_bytes_total",
        "",
        metrics.prefetch_wasted_bytes_total,
    );

    out.push_str("# TYPE nexus_prefetch_window_size gauge\n");
    let mut prefetch_window_size: Vec<_> = metrics.prefetch_window_size.iter().collect();
    prefetch_window_size.sort_by(|left, right| left.0.cmp(right.0));
    for ((mount, workspace), value) in prefetch_window_size {
        write_counter_line(
            &mut out,
            "nexus_prefetch_window_size",
            &format!("mount=\"{mount}\",workspace=\"{workspace}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_prefetch_pattern_detected_total counter\n");
    let mut prefetch_pattern_detected: Vec<_> = metrics.prefetch_pattern_detected.iter().collect();
    prefetch_pattern_detected.sort_by(|left, right| left.0.cmp(right.0));
    for (pattern, value) in prefetch_pattern_detected {
        write_counter_line(
            &mut out,
            "nexus_prefetch_pattern_detected_total",
            &format!("pattern=\"{pattern}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_etag_check_total counter\n");
    let mut etag_checks: Vec<_> = metrics.etag_checks.iter().collect();
    etag_checks.sort_by(|left, right| left.0.cmp(right.0));
    for (result, value) in etag_checks {
        write_counter_line(
            &mut out,
            "nexus_etag_check_total",
            &format!("result=\"{result}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_read_bytes_total counter\n");
    let mut read_bytes: Vec<_> = metrics.read_bytes.iter().collect();
    read_bytes.sort_by(|left, right| left.0.cmp(right.0));
    for (tier, value) in read_bytes {
        write_counter_line(
            &mut out,
            "nexus_read_bytes_total",
            &format!("tier=\"{tier}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_read_latency_seconds histogram\n");
    let mut read_latency: Vec<_> = metrics.read_latency.iter().collect();
    read_latency.sort_by(|left, right| left.0.cmp(right.0));
    for (tier, histogram) in read_latency {
        write_histogram(
            &mut out,
            "nexus_read_latency_seconds",
            &format!("tier=\"{tier}\""),
            histogram,
        );
    }

    out.push_str("# TYPE nexus_read_batch_size histogram\n");
    write_histogram(
        &mut out,
        "nexus_read_batch_size",
        "",
        &metrics.read_batch_size,
    );

    out.push_str("# TYPE nexus_fuse_passthrough_used_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_fuse_passthrough_used_total",
        "",
        metrics.fuse_passthrough_used_total,
    );

    out.push_str("# TYPE nexus_write_coalesce_flush_total counter\n");
    let mut write_coalesce_flush: Vec<_> = metrics.write_coalesce_flush.iter().collect();
    write_coalesce_flush.sort_by(|left, right| left.0.cmp(right.0));
    for (trigger, value) in write_coalesce_flush {
        write_counter_line(
            &mut out,
            "nexus_write_coalesce_flush_total",
            &format!("trigger=\"{trigger}\""),
            *value,
        );
    }

    out.push_str("# TYPE nexus_write_coalesce_dirty_bytes gauge\n");
    write_counter_line(
        &mut out,
        "nexus_write_coalesce_dirty_bytes",
        "",
        metrics.write_coalesce_dirty_bytes,
    );

    out.push_str("# TYPE nexus_write_backend_rpc_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_write_backend_rpc_total",
        "",
        metrics.write_backend_rpc_total,
    );

    out.push_str("# TYPE nexus_generation_mismatch_total counter\n");
    write_counter_line(
        &mut out,
        "nexus_generation_mismatch_total",
        "",
        metrics.generation_mismatch_total,
    );

    // Hydration files counter
    let mut entries: Vec<(&String, &u64)> = metrics.hydration_files.iter().collect();
    entries.sort();
    out.push_str("# HELP nexus_hydration_files_total Files processed during eager hydration.\n");
    out.push_str("# TYPE nexus_hydration_files_total counter\n");
    for (result, count) in entries {
        out.push_str(&format!(
            "nexus_hydration_files_total{{result=\"{}\"}} {}\n",
            result, count
        ));
    }

    // Hydration bytes counter
    let mut entries: Vec<(&String, &u64)> = metrics.hydration_bytes.iter().collect();
    entries.sort();
    out.push_str("# HELP nexus_hydration_bytes_total Bytes processed during eager hydration.\n");
    out.push_str("# TYPE nexus_hydration_bytes_total counter\n");
    for (result, bytes) in entries {
        out.push_str(&format!(
            "nexus_hydration_bytes_total{{result=\"{}\"}} {}\n",
            result, bytes
        ));
    }

    // Duration counter
    out.push_str(
        "# HELP nexus_hydration_duration_ms_total Cumulative hydration wall time in ms.\n",
    );
    out.push_str("# TYPE nexus_hydration_duration_ms_total counter\n");
    out.push_str(&format!(
        "nexus_hydration_duration_ms_total {}\n",
        metrics.hydration_duration_ms_total
    ));

    out
}

fn response(status: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status}\r\ncontent-type: text/plain; version=0.0.4; charset=utf-8\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
        body.len(),
        body
    )
}

fn request_is_metrics_scrape(request: &str) -> bool {
    request.lines().next().and_then(|line| {
        let mut parts = line.split_whitespace();
        Some((parts.next()?, parts.next()?))
    }) == Some(("GET", "/metrics"))
}

fn handle_client(mut stream: TcpStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(250)));
    let mut buffer = [0_u8; 1024];
    let bytes_read = match stream.read(&mut buffer) {
        Ok(0) => return,
        Ok(bytes_read) => bytes_read,
        Err(error)
            if matches!(
                error.kind(),
                ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
            ) =>
        {
            return;
        }
        Err(_) => return,
    };

    let request = String::from_utf8_lossy(&buffer[..bytes_read]);
    let response = if request_is_metrics_scrape(&request) {
        response("200 OK", &render())
    } else {
        response("404 Not Found", "not found\n")
    };
    let _ = stream.write_all(response.as_bytes());
}

pub fn start_server(addr: &str) -> std::io::Result<MetricsServer> {
    let listener = TcpListener::bind(addr)?;
    let local_addr = listener.local_addr()?;
    listener.set_nonblocking(true)?;
    let shutdown = Arc::new(AtomicBool::new(false));
    let thread_shutdown = Arc::clone(&shutdown);
    let thread = thread::spawn(move || {
        while !thread_shutdown.load(Ordering::Relaxed) {
            match listener.accept() {
                Ok((stream, _)) => {
                    thread::spawn(move || handle_client(stream));
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => {
                    thread::sleep(Duration::from_millis(10));
                }
            }
        }
    });
    Ok(MetricsServer {
        local_addr,
        shutdown,
        thread: Some(thread),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_record_hydration_file_increments_per_result() {
        let _guard = test_guard();
        reset_for_tests();
        record_hydration_file("admitted");
        record_hydration_file("admitted");
        record_hydration_file("skipped_warm");
        record_hydration_file("failed");
        let rendered = render();
        assert!(rendered.contains(r#"nexus_hydration_files_total{result="admitted"} 2"#));
        assert!(rendered.contains(r#"nexus_hydration_files_total{result="skipped_warm"} 1"#));
        assert!(rendered.contains(r#"nexus_hydration_files_total{result="failed"} 1"#));
    }

    #[test]
    fn test_record_hydration_bytes_accumulates() {
        let _guard = test_guard();
        reset_for_tests();
        record_hydration_bytes("admitted", 1024);
        record_hydration_bytes("admitted", 2048);
        record_hydration_bytes("skipped", 512);
        let rendered = render();
        assert!(rendered.contains(r#"nexus_hydration_bytes_total{result="admitted"} 3072"#));
        assert!(rendered.contains(r#"nexus_hydration_bytes_total{result="skipped"} 512"#));
    }

    #[test]
    fn test_observe_hydration_duration_accumulates() {
        let _guard = test_guard();
        reset_for_tests();
        observe_hydration_duration_ms(120);
        observe_hydration_duration_ms(80);
        let rendered = render();
        assert!(rendered.contains("nexus_hydration_duration_ms_total 200"));
    }

    #[test]
    fn test_record_hydration_file_unknown_result_buckets_to_other() {
        let _guard = test_guard();
        reset_for_tests();
        record_hydration_file("not_a_real_result");
        let rendered = render();
        assert!(rendered.contains(r#"nexus_hydration_files_total{result="other"} 1"#));
    }
}

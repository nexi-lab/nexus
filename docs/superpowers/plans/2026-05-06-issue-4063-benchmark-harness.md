# Benchmark Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `nexus-bench` Rust trace-replay harness for issue #4063 with committed workloads, JSON/Markdown reports, result diffing, and lightweight CI gating.

**Architecture:** Keep `nexus-bench/` outside the root Cargo workspace, mirroring the standalone `nexus-fuse` boundary. The crate has typed trace loading/validation, target adapters behind a `BenchTarget` trait, a runner that records per-operation metrics, report/diff modules for stable artifacts, and a CLI that wires replay, validate, and diff commands.

**Tech Stack:** Rust 2021, clap, serde, serde_json, thiserror, anyhow, reqwest blocking client, chrono, tempfile, GitHub Actions.

---

## File Structure

- Create `nexus-bench/Cargo.toml`: standalone binary/library crate dependencies.
- Create `nexus-bench/src/lib.rs`: module exports for tests and CLI.
- Create `nexus-bench/src/error.rs`: shared `BenchError` and `BenchResult`.
- Create `nexus-bench/src/trace.rs`: `TraceOp`, operation kinds, load/validate helpers.
- Create `nexus-bench/src/metrics.rs`: operation metrics, result schema, percentile/throughput aggregation.
- Create `nexus-bench/src/report.rs`: JSON writing, Markdown summary rendering, diff rendering.
- Create `nexus-bench/src/threshold.rs`: threshold JSON schema and diff pass/fail evaluation.
- Create `nexus-bench/src/runner.rs`: sequential and adjacent-group concurrent replay.
- Create `nexus-bench/src/target/mod.rs`: `BenchTarget` trait and target module exports.
- Create `nexus-bench/src/target/noop.rs`: deterministic target for tests and CI smoke.
- Create `nexus-bench/src/target/mount.rs`: mounted-filesystem target.
- Create `nexus-bench/src/target/http.rs`: Nexus JSON-RPC HTTP target.
- Create `nexus-bench/src/cli.rs`: clap command definitions and command dispatch.
- Create `nexus-bench/src/main.rs`: binary entrypoint.
- Create `nexus-bench/traces/*.json`: the eight standard workload traces.
- Create `nexus-bench/thresholds/default.json`: regression thresholds.
- Create `nexus-bench/baselines/develop-5698d0026-noop.json`: deterministic smoke baseline.
- Create `nexus-bench/README.md`: local runbook and workload authoring guide.
- Create `.github/workflows/nexus-bench.yml`: lightweight CI validation and diff gate.

## Task 1: Scaffold Crate And Trace Schema

**Files:**
- Create: `nexus-bench/Cargo.toml`
- Create: `nexus-bench/src/lib.rs`
- Create: `nexus-bench/src/error.rs`
- Create: `nexus-bench/src/trace.rs`

- [ ] **Step 1: Create the crate manifest and module shell**

Create `nexus-bench/Cargo.toml`:

```toml
[package]
name = "nexus-bench"
version = "0.1.0"
edition = "2021"
description = "Trace replay benchmark harness for Nexus filesystem workloads"
license = "Apache-2.0"
repository = "https://github.com/nexi-lab/nexus"

[dependencies]
anyhow = "1"
base64 = "0.22"
chrono = { version = "0.4", default-features = false, features = ["clock", "serde"] }
clap = { version = "4", features = ["derive"] }
reqwest = { version = "0.11", default-features = false, features = ["blocking", "json", "rustls-tls"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
thiserror = "1"

[dev-dependencies]
tempfile = "3"
```

Create `nexus-bench/src/lib.rs`:

```rust
pub mod error;
pub mod trace;
```

Create `nexus-bench/src/error.rs`:

```rust
use std::path::PathBuf;

pub type BenchResult<T> = Result<T, BenchError>;

#[derive(Debug, thiserror::Error)]
pub enum BenchError {
    #[error("trace validation failed at operation {index}: {message}")]
    TraceValidation { index: usize, message: String },

    #[error("failed to read {path}: {source}")]
    ReadFile {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("failed to parse json from {path}: {source}")]
    ParseJson {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },

    #[error("io error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("target operation failed: {0}")]
    Target(String),

    #[error("http request failed: {0}")]
    Http(String),

    #[error("diff threshold failed: {0}")]
    Threshold(String),
}
```

- [ ] **Step 2: Write failing trace schema tests**

Create `nexus-bench/src/trace.rs` with tests first:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn op(kind: OpKind, path: &str) -> TraceOp {
        TraceOp {
            timestamp_ns: 0,
            op: kind,
            path: path.to_string(),
            to_path: None,
            offset: None,
            length: None,
            payload_seed: None,
            parallel_group: None,
        }
    }

    #[test]
    fn validation_rejects_relative_path() {
        let trace = vec![op(OpKind::Getattr, "relative.txt")];
        let err = validate_trace(&trace).expect_err("relative paths must fail validation");
        assert!(err.to_string().contains("path must be absolute"));
    }

    #[test]
    fn validation_rejects_non_monotonic_timestamps() {
        let mut first = op(OpKind::Getattr, "/a");
        first.timestamp_ns = 10;
        let mut second = op(OpKind::Lookup, "/b");
        second.timestamp_ns = 9;
        let err = validate_trace(&[first, second]).expect_err("timestamps must be monotonic");
        assert!(err.to_string().contains("timestamp_ns must be monotonic"));
    }

    #[test]
    fn validation_requires_read_range() {
        let err = validate_trace(&[op(OpKind::Read, "/file")]).expect_err("read without range must fail");
        assert!(err.to_string().contains("read requires offset and length"));
    }

    #[test]
    fn validation_requires_write_seed() {
        let mut write = op(OpKind::Write, "/file");
        write.offset = Some(0);
        write.length = Some(32);
        let err = validate_trace(&[write]).expect_err("write without seed must fail");
        assert!(err.to_string().contains("write requires offset, length, and payload_seed"));
    }

    #[test]
    fn validation_accepts_complete_trace() {
        let mut read = op(OpKind::Read, "/file");
        read.offset = Some(0);
        read.length = Some(64);
        let mut write = op(OpKind::Write, "/file");
        write.timestamp_ns = 1;
        write.offset = Some(64);
        write.length = Some(32);
        write.payload_seed = Some(7);
        validate_trace(&[read, write]).expect("complete trace should validate");
    }

    #[test]
    fn json_uses_lowercase_operation_names() {
        let raw = r#"[{"timestamp_ns":0,"op":"readdir","path":"/workspace"}]"#;
        let trace: Vec<TraceOp> = serde_json::from_str(raw).expect("trace json should parse");
        assert_eq!(trace[0].op, OpKind::Readdir);
    }
}
```

- [ ] **Step 3: Run the trace tests and verify they fail**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml trace::
```

Expected: compilation fails because `TraceOp`, `OpKind`, and `validate_trace` are not implemented.

- [ ] **Step 4: Implement the trace schema and validation**

Replace `nexus-bench/src/trace.rs` above the test module with:

```rust
use std::{fs, path::Path};

use serde::{Deserialize, Serialize};

use crate::error::{BenchError, BenchResult};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpKind {
    Read,
    Write,
    Getattr,
    Lookup,
    Readdir,
    Delete,
    Rename,
    Mkdir,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceOp {
    pub timestamp_ns: u64,
    pub op: OpKind,
    pub path: String,
    #[serde(default)]
    pub to_path: Option<String>,
    #[serde(default)]
    pub offset: Option<u64>,
    #[serde(default)]
    pub length: Option<u64>,
    #[serde(default)]
    pub payload_seed: Option<u64>,
    #[serde(default)]
    pub parallel_group: Option<String>,
}

impl TraceOp {
    pub fn logical_read_len(&self) -> u64 {
        if self.op == OpKind::Read {
            self.length.unwrap_or(0)
        } else {
            0
        }
    }

    pub fn logical_write_len(&self) -> u64 {
        if self.op == OpKind::Write {
            self.length.unwrap_or(0)
        } else {
            0
        }
    }
}

pub fn load_trace(path: &Path) -> BenchResult<Vec<TraceOp>> {
    let bytes = fs::read(path).map_err(|source| BenchError::ReadFile {
        path: path.to_path_buf(),
        source,
    })?;
    let trace: Vec<TraceOp> = serde_json::from_slice(&bytes).map_err(|source| BenchError::ParseJson {
        path: path.to_path_buf(),
        source,
    })?;
    validate_trace(&trace)?;
    Ok(trace)
}

pub fn validate_trace(trace: &[TraceOp]) -> BenchResult<()> {
    let mut last_ts = 0;
    for (index, op) in trace.iter().enumerate() {
        if !op.path.starts_with('/') {
            return trace_error(index, "path must be absolute");
        }
        if op.path.contains("//") || op.path.contains("/../") || op.path.ends_with("/..") {
            return trace_error(index, "path must be normalized");
        }
        if index > 0 && op.timestamp_ns < last_ts {
            return trace_error(index, "timestamp_ns must be monotonic");
        }
        last_ts = op.timestamp_ns;

        match op.op {
            OpKind::Read => {
                if op.offset.is_none() || op.length.is_none() {
                    return trace_error(index, "read requires offset and length");
                }
            }
            OpKind::Write => {
                if op.offset.is_none() || op.length.is_none() || op.payload_seed.is_none() {
                    return trace_error(index, "write requires offset, length, and payload_seed");
                }
            }
            OpKind::Rename => {
                let Some(to_path) = &op.to_path else {
                    return trace_error(index, "rename requires to_path");
                };
                if !to_path.starts_with('/') {
                    return trace_error(index, "rename to_path must be absolute");
                }
            }
            OpKind::Getattr | OpKind::Lookup | OpKind::Readdir | OpKind::Delete | OpKind::Mkdir => {}
        }
    }
    Ok(())
}

fn trace_error(index: usize, message: &str) -> BenchResult<()> {
    Err(BenchError::TraceValidation {
        index,
        message: message.to_string(),
    })
}
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml trace::
```

Expected: all trace tests pass.

Commit:

```bash
git add nexus-bench/Cargo.toml nexus-bench/src/lib.rs nexus-bench/src/error.rs nexus-bench/src/trace.rs
git commit -m "feat: add nexus bench trace schema"
```

## Task 2: Metrics, Results, And Markdown Reports

**Files:**
- Modify: `nexus-bench/src/lib.rs`
- Create: `nexus-bench/src/metrics.rs`
- Create: `nexus-bench/src/report.rs`

- [ ] **Step 1: Export metrics and report modules**

Modify `nexus-bench/src/lib.rs`:

```rust
pub mod error;
pub mod metrics;
pub mod report;
pub mod trace;
```

- [ ] **Step 2: Write failing metrics/report tests**

Create `nexus-bench/src/metrics.rs` with:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::OpKind;
    use std::time::Duration;

    #[test]
    fn aggregate_records_counts_bytes_and_percentiles() {
        let samples = vec![
            OperationSample::success(OpKind::Read, Duration::from_micros(100), OperationMetrics {
                logical_bytes_read: 100,
                logical_bytes_written: 0,
                rpc_count: 1,
                egress_bytes: 120,
                cache_hit: Some(true),
            }),
            OperationSample::success(OpKind::Read, Duration::from_micros(300), OperationMetrics {
                logical_bytes_read: 300,
                logical_bytes_written: 0,
                rpc_count: 1,
                egress_bytes: 360,
                cache_hit: Some(false),
            }),
        ];

        let result = BenchResultFile::from_samples(
            "agent-cold-start",
            "noop",
            "abc123",
            chrono::DateTime::parse_from_rfc3339("2026-05-06T00:00:00Z").unwrap().to_utc(),
            Duration::from_millis(10),
            &samples,
        );

        assert_eq!(result.operations.total, 2);
        assert_eq!(result.operations.succeeded, 2);
        assert_eq!(result.operations.by_kind.get("read"), Some(&2));
        assert_eq!(result.logical_bytes_read, 400);
        assert_eq!(result.bytes_egress, 480);
        assert_eq!(result.cache_hit_rate, Some(0.5));
        assert!(result.latency_ms.p50 >= 0.1);
        assert!(result.throughput.ops_per_sec > 0.0);
    }
}
```

Create `nexus-bench/src/report.rs` with:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics::{BenchResultFile, LatencySummary, OperationCounts, ThroughputSummary};
    use std::collections::BTreeMap;

    #[test]
    fn markdown_summary_contains_required_metrics() {
        let result = BenchResultFile {
            schema_version: 1,
            workload: "agent-cold-start".to_string(),
            target: "noop".to_string(),
            git_sha: "abc123".to_string(),
            started_at: chrono::DateTime::parse_from_rfc3339("2026-05-06T00:00:00Z").unwrap().to_utc(),
            duration_ms: 10.0,
            operations: OperationCounts {
                total: 2,
                succeeded: 2,
                failed: 0,
                by_kind: BTreeMap::from([("read".to_string(), 2)]),
            },
            throughput: ThroughputSummary {
                ops_per_sec: 200.0,
                read_bytes_per_sec: 40000.0,
                write_bytes_per_sec: 0.0,
            },
            latency_ms: LatencySummary {
                min: 0.1,
                p50: 0.2,
                p90: 0.3,
                p95: 0.3,
                p99: 0.3,
                max: 0.3,
                mean: 0.2,
            },
            rpc_count: 2,
            bytes_egress: 400,
            logical_bytes_read: 400,
            logical_bytes_written: 0,
            cache_hit_rate: None,
            errors: vec![],
        };

        let md = render_summary_markdown(&result);
        assert!(md.contains("# Nexus Bench Result: agent-cold-start"));
        assert!(md.contains("| ops/sec | 200.00 |"));
        assert!(md.contains("| cache hit rate | n/a |"));
    }
}
```

- [ ] **Step 3: Run the tests and verify they fail**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml metrics:: report::
```

Expected: compilation fails because metrics and report types/functions are not implemented.

- [ ] **Step 4: Implement metrics aggregation**

Replace `nexus-bench/src/metrics.rs` above the tests with:

```rust
use std::{collections::BTreeMap, time::Duration};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::trace::OpKind;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OperationMetrics {
    pub logical_bytes_read: u64,
    pub logical_bytes_written: u64,
    pub rpc_count: u64,
    pub egress_bytes: u64,
    pub cache_hit: Option<bool>,
}

#[derive(Debug, Clone)]
pub struct OperationSample {
    pub op: OpKind,
    pub latency: Duration,
    pub metrics: OperationMetrics,
    pub error: Option<String>,
}

impl OperationSample {
    pub fn success(op: OpKind, latency: Duration, metrics: OperationMetrics) -> Self {
        Self {
            op,
            latency,
            metrics,
            error: None,
        }
    }

    pub fn failure(op: OpKind, latency: Duration, error: String) -> Self {
        Self {
            op,
            latency,
            metrics: OperationMetrics::default(),
            error: Some(error),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BenchResultFile {
    pub schema_version: u32,
    pub workload: String,
    pub target: String,
    pub git_sha: String,
    pub started_at: DateTime<Utc>,
    pub duration_ms: f64,
    pub operations: OperationCounts,
    pub throughput: ThroughputSummary,
    pub latency_ms: LatencySummary,
    pub rpc_count: u64,
    pub bytes_egress: u64,
    pub logical_bytes_read: u64,
    pub logical_bytes_written: u64,
    pub cache_hit_rate: Option<f64>,
    pub errors: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OperationCounts {
    pub total: u64,
    pub succeeded: u64,
    pub failed: u64,
    pub by_kind: BTreeMap<String, u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ThroughputSummary {
    pub ops_per_sec: f64,
    pub read_bytes_per_sec: f64,
    pub write_bytes_per_sec: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LatencySummary {
    pub min: f64,
    pub p50: f64,
    pub p90: f64,
    pub p95: f64,
    pub p99: f64,
    pub max: f64,
    pub mean: f64,
}

impl BenchResultFile {
    pub fn from_samples(
        workload: &str,
        target: &str,
        git_sha: &str,
        started_at: DateTime<Utc>,
        duration: Duration,
        samples: &[OperationSample],
    ) -> Self {
        let total = samples.len() as u64;
        let failed = samples.iter().filter(|sample| sample.error.is_some()).count() as u64;
        let succeeded = total - failed;
        let mut by_kind = BTreeMap::new();
        let mut latencies: Vec<f64> = samples
            .iter()
            .map(|sample| sample.latency.as_secs_f64() * 1000.0)
            .collect();
        latencies.sort_by(|a, b| a.total_cmp(b));

        let duration_secs = duration.as_secs_f64().max(0.000_001);
        let logical_bytes_read: u64 = samples.iter().map(|sample| sample.metrics.logical_bytes_read).sum();
        let logical_bytes_written: u64 = samples.iter().map(|sample| sample.metrics.logical_bytes_written).sum();
        let rpc_count: u64 = samples.iter().map(|sample| sample.metrics.rpc_count).sum();
        let bytes_egress: u64 = samples.iter().map(|sample| sample.metrics.egress_bytes).sum();
        let hits: Vec<bool> = samples.iter().filter_map(|sample| sample.metrics.cache_hit).collect();

        for sample in samples {
            *by_kind.entry(format!("{:?}", sample.op).to_lowercase()).or_insert(0) += 1;
        }

        Self {
            schema_version: 1,
            workload: workload.to_string(),
            target: target.to_string(),
            git_sha: git_sha.to_string(),
            started_at,
            duration_ms: duration.as_secs_f64() * 1000.0,
            operations: OperationCounts {
                total,
                succeeded,
                failed,
                by_kind,
            },
            throughput: ThroughputSummary {
                ops_per_sec: total as f64 / duration_secs,
                read_bytes_per_sec: logical_bytes_read as f64 / duration_secs,
                write_bytes_per_sec: logical_bytes_written as f64 / duration_secs,
            },
            latency_ms: LatencySummary::from_sorted_ms(&latencies),
            rpc_count,
            bytes_egress,
            logical_bytes_read,
            logical_bytes_written,
            cache_hit_rate: if hits.is_empty() {
                None
            } else {
                Some(hits.iter().filter(|hit| **hit).count() as f64 / hits.len() as f64)
            },
            errors: samples.iter().filter_map(|sample| sample.error.clone()).collect(),
        }
    }
}

impl LatencySummary {
    fn from_sorted_ms(values: &[f64]) -> Self {
        if values.is_empty() {
            return Self {
                min: 0.0,
                p50: 0.0,
                p90: 0.0,
                p95: 0.0,
                p99: 0.0,
                max: 0.0,
                mean: 0.0,
            };
        }
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        Self {
            min: values[0],
            p50: percentile(values, 0.50),
            p90: percentile(values, 0.90),
            p95: percentile(values, 0.95),
            p99: percentile(values, 0.99),
            max: values[values.len() - 1],
            mean,
        }
    }
}

fn percentile(values: &[f64], quantile: f64) -> f64 {
    let idx = ((values.len() - 1) as f64 * quantile).round() as usize;
    values[idx.min(values.len() - 1)]
}
```

- [ ] **Step 5: Implement Markdown and JSON report helpers**

Replace `nexus-bench/src/report.rs` above the tests with:

```rust
use std::{fs, path::Path};

use crate::{
    error::{BenchError, BenchResult},
    metrics::BenchResultFile,
};

pub fn write_result_json(path: &Path, result: &BenchResultFile) -> BenchResult<()> {
    let json = serde_json::to_vec_pretty(result).map_err(|source| BenchError::ParseJson {
        path: path.to_path_buf(),
        source,
    })?;
    fs::write(path, json).map_err(|source| BenchError::Io {
        path: path.to_path_buf(),
        source,
    })
}

pub fn write_markdown(path: &Path, markdown: &str) -> BenchResult<()> {
    fs::write(path, markdown).map_err(|source| BenchError::Io {
        path: path.to_path_buf(),
        source,
    })
}

pub fn render_summary_markdown(result: &BenchResultFile) -> String {
    let cache = result
        .cache_hit_rate
        .map(|rate| format!("{:.2}%", rate * 100.0))
        .unwrap_or_else(|| "n/a".to_string());
    format!(
        "# Nexus Bench Result: {}\n\n\
| Metric | Value |\n\
|---|---:|\n\
| target | {} |\n\
| operations | {} |\n\
| failed | {} |\n\
| ops/sec | {:.2} |\n\
| p50 latency | {:.3} ms |\n\
| p95 latency | {:.3} ms |\n\
| p99 latency | {:.3} ms |\n\
| rpc count | {} |\n\
| bytes egress | {} |\n\
| cache hit rate | {} |\n",
        result.workload,
        result.target,
        result.operations.total,
        result.operations.failed,
        result.throughput.ops_per_sec,
        result.latency_ms.p50,
        result.latency_ms.p95,
        result.latency_ms.p99,
        result.rpc_count,
        human_bytes(result.bytes_egress),
        cache
    )
}

fn human_bytes(bytes: u64) -> String {
    const MIB: f64 = 1024.0 * 1024.0;
    if bytes >= 1024 * 1024 {
        format!("{:.2} MiB", bytes as f64 / MIB)
    } else {
        format!("{} B", bytes)
    }
}
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml metrics:: report::
```

Expected: metrics and report tests pass.

Commit:

```bash
git add nexus-bench/src/lib.rs nexus-bench/src/metrics.rs nexus-bench/src/report.rs
git commit -m "feat: add nexus bench result reporting"
```

## Task 3: Noop Target And Replay Runner

**Files:**
- Modify: `nexus-bench/src/lib.rs`
- Create: `nexus-bench/src/target/mod.rs`
- Create: `nexus-bench/src/target/noop.rs`
- Create: `nexus-bench/src/runner.rs`

- [ ] **Step 1: Export target and runner modules**

Modify `nexus-bench/src/lib.rs`:

```rust
pub mod error;
pub mod metrics;
pub mod report;
pub mod runner;
pub mod target;
pub mod trace;
```

- [ ] **Step 2: Write failing noop/runner tests**

Create `nexus-bench/src/target/mod.rs`:

```rust
pub mod noop;

use crate::{
    error::BenchResult,
    metrics::OperationMetrics,
    trace::TraceOp,
};

pub trait BenchTarget: Send + Sync {
    fn name(&self) -> &'static str;
    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics>;
}
```

Create `nexus-bench/src/target/noop.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

    #[test]
    fn noop_counts_logical_bytes() {
        let target = NoopTarget;
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Read,
            path: "/file".to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(4096),
            payload_seed: None,
            parallel_group: None,
        };
        let metrics = target.execute(&op).expect("noop target should succeed");
        assert_eq!(metrics.logical_bytes_read, 4096);
        assert_eq!(metrics.logical_bytes_written, 0);
        assert_eq!(metrics.rpc_count, 1);
        assert_eq!(metrics.egress_bytes, 4096);
        assert_eq!(metrics.cache_hit, None);
    }
}
```

Create `nexus-bench/src/runner.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        target::noop::NoopTarget,
        trace::{OpKind, TraceOp},
    };

    fn read_op(path: &str, group: Option<&str>) -> TraceOp {
        TraceOp {
            timestamp_ns: 0,
            op: OpKind::Read,
            path: path.to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(10),
            payload_seed: None,
            parallel_group: group.map(str::to_string),
        }
    }

    #[test]
    fn runner_records_successful_samples() {
        let trace = vec![read_op("/a", None), read_op("/b", None)];
        let samples = run_trace(&NoopTarget, &trace, false).expect("runner should succeed");
        assert_eq!(samples.len(), 2);
        assert!(samples.iter().all(|sample| sample.error.is_none()));
        assert_eq!(samples.iter().map(|sample| sample.metrics.logical_bytes_read).sum::<u64>(), 20);
    }

    #[test]
    fn runner_handles_adjacent_parallel_groups() {
        let trace = vec![
            read_op("/a", Some("group-1")),
            read_op("/b", Some("group-1")),
            read_op("/c", None),
        ];
        let samples = run_trace(&NoopTarget, &trace, false).expect("parallel group should succeed");
        assert_eq!(samples.len(), 3);
    }
}
```

- [ ] **Step 3: Run tests and verify they fail**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml target:: runner::
```

Expected: compilation fails because `NoopTarget` and `run_trace` are not implemented.

- [ ] **Step 4: Implement the noop target**

Replace `nexus-bench/src/target/noop.rs` above the tests with:

```rust
use crate::{
    error::BenchResult,
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone, Copy, Default)]
pub struct NoopTarget;

impl BenchTarget for NoopTarget {
    fn name(&self) -> &'static str {
        "noop"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let logical_bytes_read = op.logical_read_len();
        let logical_bytes_written = op.logical_write_len();
        let egress_bytes = if op.op == OpKind::Read { logical_bytes_read } else { 0 };
        Ok(OperationMetrics {
            logical_bytes_read,
            logical_bytes_written,
            rpc_count: 1,
            egress_bytes,
            cache_hit: None,
        })
    }
}
```

- [ ] **Step 5: Implement the runner**

Replace `nexus-bench/src/runner.rs` above the tests with:

```rust
use std::{thread, time::Instant};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationSample,
    target::BenchTarget,
    trace::TraceOp,
};

pub fn run_trace<T: BenchTarget + Clone + 'static>(
    target: &T,
    trace: &[TraceOp],
    allow_errors: bool,
) -> BenchResult<Vec<OperationSample>> {
    let mut samples = Vec::with_capacity(trace.len());
    let mut index = 0;

    while index < trace.len() {
        if let Some(group) = non_empty_group(&trace[index]) {
            let start = index;
            while index < trace.len() && non_empty_group(&trace[index]).as_deref() == Some(group.as_str()) {
                index += 1;
            }
            let mut grouped = run_parallel_group(target.clone(), &trace[start..index]);
            if !allow_errors {
                if let Some(error) = grouped.iter().find_map(|sample| sample.error.clone()) {
                    return Err(BenchError::Target(error));
                }
            }
            samples.append(&mut grouped);
        } else {
            let sample = run_one(target, &trace[index]);
            if !allow_errors {
                if let Some(error) = sample.error.clone() {
                    return Err(BenchError::Target(error));
                }
            }
            samples.push(sample);
            index += 1;
        }
    }

    Ok(samples)
}

fn run_parallel_group<T: BenchTarget + Clone + 'static>(target: T, ops: &[TraceOp]) -> Vec<OperationSample> {
    let handles: Vec<_> = ops
        .iter()
        .cloned()
        .map(|op| {
            let target = target.clone();
            thread::spawn(move || run_one(&target, &op))
        })
        .collect();
    handles
        .into_iter()
        .map(|handle| handle.join().unwrap_or_else(|_| {
            OperationSample::failure(crate::trace::OpKind::Lookup, Default::default(), "parallel worker panicked".to_string())
        }))
        .collect()
}

fn run_one<T: BenchTarget>(target: &T, op: &TraceOp) -> OperationSample {
    let started = Instant::now();
    match target.execute(op) {
        Ok(metrics) => OperationSample::success(op.op, started.elapsed(), metrics),
        Err(err) => OperationSample::failure(op.op, started.elapsed(), err.to_string()),
    }
}

fn non_empty_group(op: &TraceOp) -> Option<String> {
    op.parallel_group.as_ref().filter(|group| !group.is_empty()).cloned()
}
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml target:: runner::
```

Expected: noop and runner tests pass.

Commit:

```bash
git add nexus-bench/src/lib.rs nexus-bench/src/target/mod.rs nexus-bench/src/target/noop.rs nexus-bench/src/runner.rs
git commit -m "feat: add noop trace replay runner"
```

## Task 4: Mount And HTTP Targets

**Files:**
- Modify: `nexus-bench/src/target/mod.rs`
- Create: `nexus-bench/src/target/mount.rs`
- Create: `nexus-bench/src/target/http.rs`

- [ ] **Step 1: Export target modules**

Modify `nexus-bench/src/target/mod.rs`:

```rust
pub mod http;
pub mod mount;
pub mod noop;

use crate::{
    error::BenchResult,
    metrics::OperationMetrics,
    trace::TraceOp,
};

pub trait BenchTarget: Send + Sync {
    fn name(&self) -> &'static str;
    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics>;
}
```

- [ ] **Step 2: Write failing mount target tests**

Create `nexus-bench/src/target/mount.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

    fn op(kind: OpKind, path: &str) -> TraceOp {
        TraceOp {
            timestamp_ns: 0,
            op: kind,
            path: path.to_string(),
            to_path: None,
            offset: None,
            length: None,
            payload_seed: None,
            parallel_group: None,
        }
    }

    #[test]
    fn mount_target_reads_requested_range_from_root() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("file.bin"), b"abcdef").unwrap();
        let target = MountTarget::new(temp.path().to_path_buf());
        let mut read = op(OpKind::Read, "/file.bin");
        read.offset = Some(2);
        read.length = Some(3);

        let metrics = target.execute(&read).expect("range read should succeed");
        assert_eq!(metrics.logical_bytes_read, 3);
        assert_eq!(metrics.egress_bytes, 3);
    }

    #[test]
    fn mount_target_writes_seeded_payload() {
        let temp = tempfile::tempdir().unwrap();
        let target = MountTarget::new(temp.path().to_path_buf());
        let mut write = op(OpKind::Write, "/out.bin");
        write.offset = Some(0);
        write.length = Some(4);
        write.payload_seed = Some(9);

        let metrics = target.execute(&write).expect("write should succeed");
        assert_eq!(metrics.logical_bytes_written, 4);
        assert_eq!(std::fs::read(temp.path().join("out.bin")).unwrap().len(), 4);
    }
}
```

- [ ] **Step 3: Write failing HTTP request-building tests**

Create `nexus-bench/src/target/http.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

    #[test]
    fn rpc_payload_for_write_uses_nexus_bytes_shape() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Write,
            path: "/file.bin".to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(3),
            payload_seed: Some(1),
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("write payload should build");
        assert_eq!(payload["method"], "write");
        assert_eq!(payload["params"]["path"], "/file.bin");
        assert_eq!(payload["params"]["content"]["__type__"], "bytes");
        assert!(payload["params"]["content"]["data"].as_str().unwrap().len() >= 4);
    }

    #[test]
    fn rpc_method_for_rename_matches_nexus_fuse_client() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Rename,
            path: "/old".to_string(),
            to_path: Some("/new".to_string()),
            offset: None,
            length: None,
            payload_seed: None,
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("rename payload should build");
        assert_eq!(payload["method"], "rename");
        assert_eq!(payload["params"]["old_path"], "/old");
        assert_eq!(payload["params"]["new_path"], "/new");
    }
}
```

- [ ] **Step 4: Run target tests and verify they fail**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml target::mount:: target::http::
```

Expected: compilation fails because `MountTarget`, `HttpTarget`, and `rpc_payload` are not implemented.

- [ ] **Step 5: Implement mount target**

Replace `nexus-bench/src/target/mount.rs` above the tests with:

```rust
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone)]
pub struct MountTarget {
    root: PathBuf,
}

impl MountTarget {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn resolve(&self, path: &str) -> PathBuf {
        let relative = path.trim_start_matches('/');
        self.root.join(relative)
    }
}

impl BenchTarget for MountTarget {
    fn name(&self) -> &'static str {
        "mount"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let path = self.resolve(&op.path);
        match op.op {
            OpKind::Read => read_range(&path, op),
            OpKind::Write => write_range(&path, op),
            OpKind::Getattr | OpKind::Lookup => {
                fs::metadata(&path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Readdir => {
                let count = fs::read_dir(&path).map_err(|source| BenchError::Io { path: path.clone(), source })?.count();
                Ok(metrics(0, 0, 1, count as u64))
            }
            OpKind::Delete => {
                remove_path(&path)?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Rename => {
                let to_path = self.resolve(op.to_path.as_deref().unwrap_or("/"));
                fs::rename(&path, &to_path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Mkdir => {
                fs::create_dir_all(&path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
        }
    }
}

fn read_range(path: &Path, op: &TraceOp) -> BenchResult<OperationMetrics> {
    let offset = op.offset.unwrap_or(0);
    let len = op.length.unwrap_or(0) as usize;
    let mut file = File::open(path).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    file.seek(SeekFrom::Start(offset)).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    let mut buf = vec![0; len];
    let read = file.read(&mut buf).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })? as u64;
    Ok(metrics(read, 0, 1, read))
}

fn write_range(path: &Path, op: &TraceOp) -> BenchResult<OperationMetrics> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|source| BenchError::Io { path: parent.to_path_buf(), source })?;
    }
    let offset = op.offset.unwrap_or(0);
    let len = op.length.unwrap_or(0) as usize;
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .open(path)
        .map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    file.seek(SeekFrom::Start(offset)).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    let payload = seeded_payload(op.payload_seed.unwrap_or(0), len);
    file.write_all(&payload).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    Ok(metrics(0, len as u64, 1, 0))
}

fn remove_path(path: &Path) -> BenchResult<()> {
    let metadata = fs::metadata(path).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })?;
    if metadata.is_dir() {
        fs::remove_dir_all(path).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })
    } else {
        fs::remove_file(path).map_err(|source| BenchError::Io { path: path.to_path_buf(), source })
    }
}

fn seeded_payload(seed: u64, len: usize) -> Vec<u8> {
    (0..len).map(|idx| seed.wrapping_add(idx as u64) as u8).collect()
}

fn metrics(read: u64, written: u64, rpc_count: u64, egress: u64) -> OperationMetrics {
    OperationMetrics {
        logical_bytes_read: read,
        logical_bytes_written: written,
        rpc_count,
        egress_bytes: egress,
        cache_hit: None,
    }
}
```

- [ ] **Step 6: Implement HTTP payloads and target**

Replace `nexus-bench/src/target/http.rs` above the tests with:

```rust
use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::blocking::Client;
use serde_json::{json, Value};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone)]
pub struct HttpTarget {
    client: Client,
    base_url: String,
    api_key: String,
}

impl HttpTarget {
    pub fn new(base_url: String, api_key: String) -> BenchResult<Self> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .connect_timeout(std::time::Duration::from_secs(5))
            .no_proxy()
            .build()
            .map_err(|err| BenchError::Http(err.to_string()))?;
        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key,
        })
    }
}

impl BenchTarget for HttpTarget {
    fn name(&self) -> &'static str {
        "http"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let payload = rpc_payload(op)?;
        let method = payload["method"].as_str().unwrap_or("unknown");
        let url = format!("{}/api/nfs/{}", self.base_url, method);
        let response = self
            .client
            .post(url)
            .bearer_auth(&self.api_key)
            .json(&payload)
            .send()
            .map_err(|err| BenchError::Http(err.to_string()))?;
        let status = response.status();
        let body = response.bytes().map_err(|err| BenchError::Http(err.to_string()))?;
        if !status.is_success() {
            return Err(BenchError::Http(format!("{}: {}", status, String::from_utf8_lossy(&body))));
        }
        Ok(OperationMetrics {
            logical_bytes_read: op.logical_read_len(),
            logical_bytes_written: op.logical_write_len(),
            rpc_count: 1,
            egress_bytes: if op.op == OpKind::Read { body.len() as u64 } else { 0 },
            cache_hit: None,
        })
    }
}

pub fn rpc_payload(op: &TraceOp) -> BenchResult<Value> {
    let (method, params) = match op.op {
        OpKind::Read => ("read", json!({"path": op.path})),
        OpKind::Write => {
            let payload = seeded_payload(op.payload_seed.unwrap_or(0), op.length.unwrap_or(0) as usize);
            (
                "write",
                json!({
                    "path": op.path,
                    "content": {"__type__": "bytes", "data": STANDARD.encode(payload)}
                }),
            )
        }
        OpKind::Getattr => ("stat", json!({"path": op.path})),
        OpKind::Lookup => ("exists", json!({"path": op.path})),
        OpKind::Readdir => ("list", json!({"path": op.path, "recursive": false, "details": true})),
        OpKind::Delete => ("delete", json!({"path": op.path})),
        OpKind::Rename => (
            "rename",
            json!({"old_path": op.path, "new_path": op.to_path.clone().unwrap_or_default()}),
        ),
        OpKind::Mkdir => ("mkdir", json!({"path": op.path})),
    };

    Ok(json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }))
}

fn seeded_payload(seed: u64, len: usize) -> Vec<u8> {
    (0..len).map(|idx| seed.wrapping_add(idx as u64) as u8).collect()
}
```

- [ ] **Step 7: Run tests and commit**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml target::
```

Expected: target tests pass.

Commit:

```bash
git add nexus-bench/src/target/mod.rs nexus-bench/src/target/mount.rs nexus-bench/src/target/http.rs
git commit -m "feat: add nexus bench replay targets"
```

## Task 5: Diff Thresholds And CLI Wiring

**Files:**
- Modify: `nexus-bench/src/lib.rs`
- Create: `nexus-bench/src/threshold.rs`
- Create: `nexus-bench/src/cli.rs`
- Create: `nexus-bench/src/main.rs`

- [ ] **Step 1: Export threshold and CLI modules**

Modify `nexus-bench/src/lib.rs`:

```rust
pub mod cli;
pub mod error;
pub mod metrics;
pub mod report;
pub mod runner;
pub mod target;
pub mod threshold;
pub mod trace;
```

- [ ] **Step 2: Write failing threshold tests**

Create `nexus-bench/src/threshold.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics::{BenchResultFile, LatencySummary, OperationCounts, ThroughputSummary};
    use std::collections::BTreeMap;

    fn result(p95: f64, ops_per_sec: f64) -> BenchResultFile {
        BenchResultFile {
            schema_version: 1,
            workload: "agent-cold-start".to_string(),
            target: "noop".to_string(),
            git_sha: "abc".to_string(),
            started_at: chrono::Utc::now(),
            duration_ms: 1.0,
            operations: OperationCounts {
                total: 1,
                succeeded: 1,
                failed: 0,
                by_kind: BTreeMap::new(),
            },
            throughput: ThroughputSummary {
                ops_per_sec,
                read_bytes_per_sec: 0.0,
                write_bytes_per_sec: 0.0,
            },
            latency_ms: LatencySummary {
                min: p95,
                p50: p95,
                p90: p95,
                p95,
                p99: p95,
                max: p95,
                mean: p95,
            },
            rpc_count: 1,
            bytes_egress: 1,
            logical_bytes_read: 1,
            logical_bytes_written: 0,
            cache_hit_rate: None,
            errors: vec![],
        }
    }

    #[test]
    fn lower_is_better_metric_fails_on_regression() {
        let thresholds = ThresholdSet::from_json_str(r#"{"latency_ms.p95":{"max_regression_percent":20.0}}"#).unwrap();
        let diff = evaluate(&result(10.0, 100.0), &result(13.0, 100.0), &thresholds);
        assert!(!diff.passed);
        assert_eq!(diff.rows[0].status, DiffStatus::Failed);
    }

    #[test]
    fn higher_is_better_metric_fails_on_drop() {
        let thresholds = ThresholdSet::from_json_str(
            r#"{"throughput.ops_per_sec":{"max_regression_percent":10.0,"higher_is_better":true}}"#,
        )
        .unwrap();
        let diff = evaluate(&result(10.0, 100.0), &result(10.0, 80.0), &thresholds);
        assert!(!diff.passed);
        assert_eq!(diff.rows[0].status, DiffStatus::Failed);
    }
}
```

- [ ] **Step 3: Run threshold tests and verify they fail**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml threshold::
```

Expected: compilation fails because `ThresholdSet`, `evaluate`, and diff result types are not implemented.

- [ ] **Step 4: Implement threshold diffing**

Replace `nexus-bench/src/threshold.rs` above the tests with:

```rust
use std::{collections::BTreeMap, fs, path::Path};

use serde::{Deserialize, Serialize};

use crate::{
    error::{BenchError, BenchResult},
    metrics::BenchResultFile,
};

#[derive(Debug, Clone, Deserialize)]
pub struct Threshold {
    pub max_regression_percent: f64,
    #[serde(default)]
    pub higher_is_better: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ThresholdSet(pub BTreeMap<String, Threshold>);

impl ThresholdSet {
    pub fn from_json_str(raw: &str) -> BenchResult<Self> {
        serde_json::from_str(raw).map_err(|err| BenchError::Threshold(err.to_string()))
    }

    pub fn load(path: &Path) -> BenchResult<Self> {
        let raw = fs::read_to_string(path).map_err(|source| BenchError::ReadFile {
            path: path.to_path_buf(),
            source,
        })?;
        Self::from_json_str(&raw)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DiffResult {
    pub passed: bool,
    pub rows: Vec<DiffRow>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DiffRow {
    pub metric: String,
    pub baseline: Option<f64>,
    pub candidate: Option<f64>,
    pub percent_change: Option<f64>,
    pub threshold_percent: f64,
    pub status: DiffStatus,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DiffStatus {
    Passed,
    Failed,
    Skipped,
}

pub fn evaluate(baseline: &BenchResultFile, candidate: &BenchResultFile, thresholds: &ThresholdSet) -> DiffResult {
    let mut rows = Vec::new();
    for (metric, threshold) in &thresholds.0 {
        let base = metric_value(baseline, metric);
        let cand = metric_value(candidate, metric);
        let (percent_change, status) = match (base, cand) {
            (Some(base), Some(cand)) if base != 0.0 => {
                let change = if threshold.higher_is_better {
                    ((base - cand) / base) * 100.0
                } else {
                    ((cand - base) / base) * 100.0
                };
                let status = if change > threshold.max_regression_percent {
                    DiffStatus::Failed
                } else {
                    DiffStatus::Passed
                };
                (Some(change), status)
            }
            _ => (None, DiffStatus::Skipped),
        };
        rows.push(DiffRow {
            metric: metric.clone(),
            baseline: base,
            candidate: cand,
            percent_change,
            threshold_percent: threshold.max_regression_percent,
            status,
        });
    }
    let passed = rows.iter().all(|row| row.status != DiffStatus::Failed);
    DiffResult { passed, rows }
}

fn metric_value(result: &BenchResultFile, metric: &str) -> Option<f64> {
    match metric {
        "latency_ms.p95" => Some(result.latency_ms.p95),
        "latency_ms.p99" => Some(result.latency_ms.p99),
        "throughput.ops_per_sec" => Some(result.throughput.ops_per_sec),
        "rpc_count" => Some(result.rpc_count as f64),
        "bytes_egress" => Some(result.bytes_egress as f64),
        "cache_hit_rate" => result.cache_hit_rate,
        _ => None,
    }
}
```

- [ ] **Step 5: Add diff Markdown rendering**

Append this to `nexus-bench/src/report.rs`:

```rust
use crate::threshold::{DiffResult, DiffStatus};

pub fn render_diff_markdown(diff: &DiffResult) -> String {
    let mut out = String::from("# Nexus Bench Diff\n\n| Metric | Baseline | Candidate | Change | Threshold | Status |\n|---|---:|---:|---:|---:|---|\n");
    for row in &diff.rows {
        let baseline = row.baseline.map(format_f64).unwrap_or_else(|| "n/a".to_string());
        let candidate = row.candidate.map(format_f64).unwrap_or_else(|| "n/a".to_string());
        let change = row
            .percent_change
            .map(|value| format!("{value:.2}%"))
            .unwrap_or_else(|| "n/a".to_string());
        let status = match row.status {
            DiffStatus::Passed => "passed",
            DiffStatus::Failed => "failed",
            DiffStatus::Skipped => "skipped",
        };
        out.push_str(&format!(
            "| {} | {} | {} | {} | {:.2}% | {} |\n",
            row.metric, baseline, candidate, change, row.threshold_percent, status
        ));
    }
    out
}

fn format_f64(value: f64) -> String {
    format!("{value:.3}")
}
```

If Rust reports duplicate imports, move all `use` lines to the top of `report.rs`.

- [ ] **Step 6: Implement CLI and main**

Create `nexus-bench/src/cli.rs`:

```rust
use std::{
    path::PathBuf,
    process::Command,
    time::Instant,
};

use chrono::Utc;
use clap::{Parser, Subcommand, ValueEnum};

use crate::{
    error::{BenchError, BenchResult},
    metrics::BenchResultFile,
    report::{render_diff_markdown, render_summary_markdown, write_markdown, write_result_json},
    runner::run_trace,
    target::{http::HttpTarget, mount::MountTarget, noop::NoopTarget, BenchTarget},
    threshold::{evaluate, ThresholdSet},
    trace::load_trace,
};

#[derive(Debug, Parser)]
#[command(name = "nexus-bench")]
pub struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    Validate {
        #[arg(long)]
        trace_dir: PathBuf,
    },
    Replay {
        #[arg(long)]
        trace: PathBuf,
        #[arg(long, value_enum)]
        target: TargetKind,
        #[arg(long)]
        mount_root: Option<PathBuf>,
        #[arg(long)]
        base_url: Option<String>,
        #[arg(long)]
        api_key: Option<String>,
        #[arg(long)]
        out_json: PathBuf,
        #[arg(long)]
        out_md: PathBuf,
        #[arg(long, default_value_t = false)]
        allow_errors: bool,
    },
    Diff {
        #[arg(long)]
        baseline: PathBuf,
        #[arg(long)]
        candidate: PathBuf,
        #[arg(long)]
        threshold: PathBuf,
        #[arg(long)]
        out_md: PathBuf,
    },
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum TargetKind {
    Noop,
    Mount,
    Http,
}

pub fn run() -> BenchResult<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Validate { trace_dir } => {
            for entry in std::fs::read_dir(&trace_dir).map_err(|source| BenchError::Io { path: trace_dir.clone(), source })? {
                let path = entry.map_err(|source| BenchError::Io { path: trace_dir.clone(), source })?.path();
                if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
                    load_trace(&path)?;
                }
            }
            Ok(())
        }
        Commands::Replay {
            trace,
            target,
            mount_root,
            base_url,
            api_key,
            out_json,
            out_md,
            allow_errors,
        } => {
            let trace_ops = load_trace(&trace)?;
            match target {
                TargetKind::Noop => replay_with_target(&NoopTarget, "noop", &trace, &trace_ops, &out_json, &out_md, allow_errors),
                TargetKind::Mount => {
                    let root = mount_root.ok_or_else(|| BenchError::Target("--mount-root is required for mount target".to_string()))?;
                    let target = MountTarget::new(root);
                    replay_with_target(&target, "mount", &trace, &trace_ops, &out_json, &out_md, allow_errors)
                }
                TargetKind::Http => {
                    let base_url = base_url.ok_or_else(|| BenchError::Target("--base-url is required for http target".to_string()))?;
                    let api_key = api_key.ok_or_else(|| BenchError::Target("--api-key is required for http target".to_string()))?;
                    let target = HttpTarget::new(base_url, api_key)?;
                    replay_with_target(&target, "http", &trace, &trace_ops, &out_json, &out_md, allow_errors)
                }
            }
        }
        Commands::Diff {
            baseline,
            candidate,
            threshold,
            out_md,
        } => {
            let baseline_result: BenchResultFile = serde_json::from_slice(&std::fs::read(&baseline).map_err(|source| BenchError::ReadFile {
                path: baseline.clone(),
                source,
            })?)
            .map_err(|source| BenchError::ParseJson {
                path: baseline.clone(),
                source,
            })?;
            let candidate_result: BenchResultFile = serde_json::from_slice(&std::fs::read(&candidate).map_err(|source| BenchError::ReadFile {
                path: candidate.clone(),
                source,
            })?)
            .map_err(|source| BenchError::ParseJson {
                path: candidate.clone(),
                source,
            })?;
            let thresholds = ThresholdSet::load(&threshold)?;
            let diff = evaluate(&baseline_result, &candidate_result, &thresholds);
            write_markdown(&out_md, &render_diff_markdown(&diff))?;
            if diff.passed {
                Ok(())
            } else {
                Err(BenchError::Threshold("candidate exceeded regression threshold".to_string()))
            }
        }
    }
}

fn replay_with_target<T: BenchTarget + Clone + 'static>(
    target: &T,
    target_name: &str,
    trace_path: &std::path::Path,
    trace_ops: &[crate::trace::TraceOp],
    out_json: &std::path::Path,
    out_md: &std::path::Path,
    allow_errors: bool,
) -> BenchResult<()> {
    let started_at = Utc::now();
    let started = Instant::now();
    let samples = run_trace(target, trace_ops, allow_errors)?;
    let result = BenchResultFile::from_samples(
        workload_name(trace_path),
        target_name,
        &git_sha(),
        started_at,
        started.elapsed(),
        &samples,
    );
    write_result_json(out_json, &result)?;
    write_markdown(out_md, &render_summary_markdown(&result))
}

fn workload_name(path: &std::path::Path) -> &str {
    path.file_stem().and_then(|stem| stem.to_str()).unwrap_or("unknown")
}

fn git_sha() -> String {
    Command::new("git")
        .args(["rev-parse", "--short=9", "HEAD"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|sha| sha.trim().to_string())
        .filter(|sha| !sha.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}
```

Create `nexus-bench/src/main.rs`:

```rust
fn main() {
    if let Err(err) = nexus_bench::cli::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
```

- [ ] **Step 7: Run tests and smoke CLI**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml threshold::
cargo run --manifest-path nexus-bench/Cargo.toml -- --help
```

Expected: threshold tests pass and CLI help prints `validate`, `replay`, and `diff`.

Commit:

```bash
git add nexus-bench/src/lib.rs nexus-bench/src/threshold.rs nexus-bench/src/report.rs nexus-bench/src/cli.rs nexus-bench/src/main.rs
git commit -m "feat: add nexus bench cli and diffing"
```

## Task 6: Workloads, Baseline, Docs, And CI

**Files:**
- Create: `nexus-bench/traces/agent-cold-start.json`
- Create: `nexus-bench/traces/agent-warm-trace.json`
- Create: `nexus-bench/traces/seq-large-read.json`
- Create: `nexus-bench/traces/stride-read.json`
- Create: `nexus-bench/traces/bursty-write.json`
- Create: `nexus-bench/traces/read-after-write.json`
- Create: `nexus-bench/traces/concurrent-reads.json`
- Create: `nexus-bench/traces/metadata-storm.json`
- Create: `nexus-bench/thresholds/default.json`
- Create: `nexus-bench/baselines/develop-5698d0026-noop.json`
- Create: `nexus-bench/README.md`
- Create: `.github/workflows/nexus-bench.yml`

- [ ] **Step 1: Add representative trace files**

Add compact but valid traces. Use repeated operations where the issue asks for large logical patterns; keep committed files reviewable.

For `nexus-bench/traces/agent-cold-start.json`:

```json
[
  {"timestamp_ns":0,"op":"getattr","path":"/workspace"},
  {"timestamp_ns":1000,"op":"readdir","path":"/workspace"},
  {"timestamp_ns":2000,"op":"read","path":"/workspace/README.md","offset":0,"length":32768},
  {"timestamp_ns":3000,"op":"read","path":"/workspace/pyproject.toml","offset":0,"length":16384},
  {"timestamp_ns":4000,"op":"lookup","path":"/workspace/src/nexus/__init__.py"},
  {"timestamp_ns":5000,"op":"read","path":"/workspace/src/nexus/__init__.py","offset":0,"length":8192}
]
```

For `nexus-bench/traces/agent-warm-trace.json`:

```json
[
  {"timestamp_ns":0,"op":"read","path":"/workspace/README.md","offset":0,"length":8192},
  {"timestamp_ns":1000,"op":"read","path":"/workspace/src/nexus/cli/main.py","offset":0,"length":8192},
  {"timestamp_ns":2000,"op":"getattr","path":"/workspace/src/nexus/cli/main.py"},
  {"timestamp_ns":3000,"op":"read","path":"/workspace/tests/unit/test_import.py","offset":0,"length":4096}
]
```

For `nexus-bench/traces/seq-large-read.json`:

```json
[
  {"timestamp_ns":0,"op":"read","path":"/large/one-gib.bin","offset":0,"length":1048576},
  {"timestamp_ns":1000,"op":"read","path":"/large/one-gib.bin","offset":1048576,"length":1048576},
  {"timestamp_ns":2000,"op":"read","path":"/large/one-gib.bin","offset":2097152,"length":1048576},
  {"timestamp_ns":3000,"op":"read","path":"/large/one-gib.bin","offset":3145728,"length":1048576}
]
```

For `nexus-bench/traces/stride-read.json`:

```json
[
  {"timestamp_ns":0,"op":"read","path":"/datasets/table.parquet","offset":0,"length":65536},
  {"timestamp_ns":1000,"op":"read","path":"/datasets/table.parquet","offset":1048576,"length":65536},
  {"timestamp_ns":2000,"op":"read","path":"/datasets/table.parquet","offset":2097152,"length":65536},
  {"timestamp_ns":3000,"op":"read","path":"/datasets/table.parquet","offset":3145728,"length":65536}
]
```

For `nexus-bench/traces/bursty-write.json`:

```json
[
  {"timestamp_ns":0,"op":"write","path":"/tmp/burst-000.bin","offset":0,"length":1024,"payload_seed":1,"parallel_group":"burst-1"},
  {"timestamp_ns":100000,"op":"write","path":"/tmp/burst-001.bin","offset":0,"length":1024,"payload_seed":2,"parallel_group":"burst-1"},
  {"timestamp_ns":200000,"op":"write","path":"/tmp/burst-002.bin","offset":0,"length":1024,"payload_seed":3,"parallel_group":"burst-1"},
  {"timestamp_ns":300000,"op":"write","path":"/tmp/burst-003.bin","offset":0,"length":1024,"payload_seed":4,"parallel_group":"burst-1"}
]
```

For `nexus-bench/traces/read-after-write.json`:

```json
[
  {"timestamp_ns":0,"op":"write","path":"/tmp/ryw.bin","offset":0,"length":4096,"payload_seed":11},
  {"timestamp_ns":1000,"op":"read","path":"/tmp/ryw.bin","offset":0,"length":4096},
  {"timestamp_ns":2000,"op":"write","path":"/tmp/ryw.bin","offset":4096,"length":4096,"payload_seed":12},
  {"timestamp_ns":3000,"op":"read","path":"/tmp/ryw.bin","offset":4096,"length":4096}
]
```

For `nexus-bench/traces/concurrent-reads.json`:

```json
[
  {"timestamp_ns":0,"op":"read","path":"/shared/model.bin","offset":0,"length":65536,"parallel_group":"readers-1"},
  {"timestamp_ns":0,"op":"read","path":"/shared/model.bin","offset":0,"length":65536,"parallel_group":"readers-1"},
  {"timestamp_ns":0,"op":"read","path":"/shared/model.bin","offset":0,"length":65536,"parallel_group":"readers-1"},
  {"timestamp_ns":0,"op":"read","path":"/shared/model.bin","offset":0,"length":65536,"parallel_group":"readers-1"}
]
```

For `nexus-bench/traces/metadata-storm.json`:

```json
[
  {"timestamp_ns":0,"op":"getattr","path":"/workspace/file-000.txt"},
  {"timestamp_ns":1000,"op":"lookup","path":"/workspace/file-001.txt"},
  {"timestamp_ns":2000,"op":"getattr","path":"/workspace/file-002.txt"},
  {"timestamp_ns":3000,"op":"lookup","path":"/workspace/file-003.txt"},
  {"timestamp_ns":4000,"op":"getattr","path":"/workspace/file-004.txt"},
  {"timestamp_ns":5000,"op":"lookup","path":"/workspace/file-005.txt"}
]
```

- [ ] **Step 2: Add thresholds**

Create `nexus-bench/thresholds/default.json`:

```json
{
  "latency_ms.p95": {"max_regression_percent": 20.0},
  "latency_ms.p99": {"max_regression_percent": 25.0},
  "throughput.ops_per_sec": {"max_regression_percent": 20.0, "higher_is_better": true},
  "rpc_count": {"max_regression_percent": 10.0},
  "bytes_egress": {"max_regression_percent": 10.0}
}
```

- [ ] **Step 3: Validate traces and generate baseline**

Run:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
mkdir -p /tmp/nexus-bench
cargo run --manifest-path nexus-bench/Cargo.toml -- replay --target noop --trace nexus-bench/traces/agent-cold-start.json --out-json /tmp/nexus-bench/develop-5698d0026-noop.json --out-md /tmp/nexus-bench/develop-5698d0026-noop.md
mkdir -p nexus-bench/baselines
cp /tmp/nexus-bench/develop-5698d0026-noop.json nexus-bench/baselines/develop-5698d0026-noop.json
```

Expected: validation and replay exit zero.

- [ ] **Step 4: Add README**

Create `nexus-bench/README.md`:

```markdown
# Nexus Bench

`nexus-bench` is the trace-replay benchmark harness for Nexus filesystem performance work.

## Validate traces

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
```

## Replay with the deterministic target

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target noop \
  --trace nexus-bench/traces/agent-cold-start.json \
  --out-json target/nexus-bench/agent-cold-start.json \
  --out-md target/nexus-bench/agent-cold-start.md
```

## Replay against a mounted filesystem

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target mount \
  --mount-root /tmp/nexus-mount \
  --trace nexus-bench/traces/seq-large-read.json \
  --out-json target/nexus-bench/seq-large-read.json \
  --out-md target/nexus-bench/seq-large-read.md
```

## Replay against a Nexus HTTP endpoint

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target http \
  --base-url http://localhost:2026 \
  --api-key sk-test-key-123 \
  --trace nexus-bench/traces/agent-warm-trace.json \
  --out-json target/nexus-bench/agent-warm-trace.json \
  --out-md target/nexus-bench/agent-warm-trace.md
```

## Compare results

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- diff \
  --baseline nexus-bench/baselines/develop-5698d0026-noop.json \
  --candidate target/nexus-bench/agent-cold-start.json \
  --threshold nexus-bench/thresholds/default.json \
  --out-md target/nexus-bench/diff.md
```

## Metrics

- `logical_bytes_read` and `logical_bytes_written` come from trace ranges.
- `bytes_egress` is target-observed response traffic. For the HTTP target, current Nexus reads return whole-file content, so egress can exceed logical bytes requested by a range trace.
- `rpc_count` is the number of target operations issued by the harness.
- `cache_hit_rate` is `n/a` until a target exposes reliable hit/miss data.

## Adding a workload

Add a JSON trace under `nexus-bench/traces/`. Each operation needs a monotonic `timestamp_ns`, an absolute `path`, and operation-specific fields:

- `read`: `offset`, `length`
- `write`: `offset`, `length`, `payload_seed`
- `rename`: `to_path`

Use `parallel_group` on adjacent operations that should run concurrently.
```

- [ ] **Step 5: Add CI workflow**

Create `.github/workflows/nexus-bench.yml`:

```yaml
name: Nexus Bench

on:
  pull_request:
    branches: [main, develop]
    paths:
      - "nexus-bench/**"
      - "nexus-fuse/**"
      - "rust/kernel/**"
      - "rust/backends/**"
      - "rust/transport/**"
      - ".github/workflows/nexus-bench.yml"
  push:
    branches: [main, develop]
    paths:
      - "nexus-bench/**"
      - "nexus-fuse/**"
      - "rust/kernel/**"
      - "rust/backends/**"
      - "rust/transport/**"
      - ".github/workflows/nexus-bench.yml"

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  nexus-bench:
    name: Validate trace harness
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Rust
        uses: dtolnay/rust-toolchain@stable

      - name: Test harness
        run: cargo test --manifest-path nexus-bench/Cargo.toml

      - name: Validate traces
        run: cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces

      - name: Replay smoke trace
        run: |
          mkdir -p /tmp/nexus-bench
          cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
            --target noop \
            --trace nexus-bench/traces/agent-cold-start.json \
            --out-json /tmp/nexus-bench/agent-cold-start.json \
            --out-md /tmp/nexus-bench/agent-cold-start.md

      - name: Check regression threshold plumbing
        run: |
          cargo run --manifest-path nexus-bench/Cargo.toml -- diff \
            --baseline nexus-bench/baselines/develop-5698d0026-noop.json \
            --candidate /tmp/nexus-bench/agent-cold-start.json \
            --threshold nexus-bench/thresholds/default.json \
            --out-md /tmp/nexus-bench/diff.md

      - name: Upload benchmark artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: nexus-bench-smoke
          path: /tmp/nexus-bench/*
```

- [ ] **Step 6: Run full verification**

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
mkdir -p /tmp/nexus-bench
cargo run --manifest-path nexus-bench/Cargo.toml -- replay --target noop --trace nexus-bench/traces/agent-cold-start.json --out-json /tmp/nexus-bench/agent-cold-start.json --out-md /tmp/nexus-bench/agent-cold-start.md
cargo run --manifest-path nexus-bench/Cargo.toml -- diff --baseline nexus-bench/baselines/develop-5698d0026-noop.json --candidate /tmp/nexus-bench/agent-cold-start.json --threshold nexus-bench/thresholds/default.json --out-md /tmp/nexus-bench/diff.md
```

Expected: every command exits zero. Inspect `/tmp/nexus-bench/agent-cold-start.md` and `/tmp/nexus-bench/diff.md` for readable Markdown.

- [ ] **Step 7: Commit**

Commit:

```bash
git add nexus-bench/traces nexus-bench/thresholds nexus-bench/baselines nexus-bench/README.md .github/workflows/nexus-bench.yml
git commit -m "feat: add nexus bench workloads and ci"
```

## Final Verification

Run:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
mkdir -p /tmp/nexus-bench
cargo run --manifest-path nexus-bench/Cargo.toml -- replay --target noop --trace nexus-bench/traces/agent-cold-start.json --out-json /tmp/nexus-bench/agent-cold-start.json --out-md /tmp/nexus-bench/agent-cold-start.md
cargo run --manifest-path nexus-bench/Cargo.toml -- diff --baseline nexus-bench/baselines/develop-5698d0026-noop.json --candidate /tmp/nexus-bench/agent-cold-start.json --threshold nexus-bench/thresholds/default.json --out-md /tmp/nexus-bench/diff.md
git status --short
```

Expected:

- All Rust tests pass.
- Trace validation exits zero.
- Noop replay writes JSON and Markdown.
- Diff exits zero against the committed noop baseline.
- `git status --short` only shows intentional committed changes or an empty working tree.

## Spec Coverage

- Trace-replay tool: Tasks 1, 3, 4, and 5.
- Eight workloads: Task 6.
- JSON and Markdown outputs: Tasks 2 and 5.
- Diff mode and thresholds: Task 5.
- CI integration: Task 6.
- Committed baseline: Task 6.
- Documentation/runbook: Task 6.

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
        let failed = samples
            .iter()
            .filter(|sample| sample.error.is_some())
            .count() as u64;
        let succeeded = total - failed;
        let mut by_kind = BTreeMap::new();
        let mut latencies: Vec<f64> = samples
            .iter()
            .map(|sample| sample.latency.as_secs_f64() * 1000.0)
            .collect();
        latencies.sort_by(|a, b| a.total_cmp(b));

        let duration_secs = duration.as_secs_f64().max(0.000_001);
        let logical_bytes_read: u64 = samples
            .iter()
            .map(|sample| sample.metrics.logical_bytes_read)
            .sum();
        let logical_bytes_written: u64 = samples
            .iter()
            .map(|sample| sample.metrics.logical_bytes_written)
            .sum();
        let rpc_count: u64 = samples.iter().map(|sample| sample.metrics.rpc_count).sum();
        let bytes_egress: u64 = samples
            .iter()
            .map(|sample| sample.metrics.egress_bytes)
            .sum();
        let hits: Vec<bool> = samples
            .iter()
            .filter_map(|sample| sample.metrics.cache_hit)
            .collect();

        for sample in samples {
            *by_kind
                .entry(format!("{:?}", sample.op).to_lowercase())
                .or_insert(0) += 1;
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
            errors: samples
                .iter()
                .filter_map(|sample| sample.error.clone())
                .collect(),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::OpKind;
    use std::time::Duration;

    #[test]
    fn aggregate_records_counts_bytes_and_percentiles() {
        let samples = vec![
            OperationSample::success(
                OpKind::Read,
                Duration::from_micros(100),
                OperationMetrics {
                    logical_bytes_read: 100,
                    logical_bytes_written: 0,
                    rpc_count: 1,
                    egress_bytes: 120,
                    cache_hit: Some(true),
                },
            ),
            OperationSample::success(
                OpKind::Read,
                Duration::from_micros(300),
                OperationMetrics {
                    logical_bytes_read: 300,
                    logical_bytes_written: 0,
                    rpc_count: 1,
                    egress_bytes: 360,
                    cache_hit: Some(false),
                },
            ),
        ];

        let result = BenchResultFile::from_samples(
            "agent-cold-start",
            "noop",
            "abc123",
            chrono::DateTime::parse_from_rfc3339("2026-05-06T00:00:00Z")
                .unwrap()
                .to_utc(),
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

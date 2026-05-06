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

pub fn evaluate(
    baseline: &BenchResultFile,
    candidate: &BenchResultFile,
    thresholds: &ThresholdSet,
) -> DiffResult {
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
        let thresholds =
            ThresholdSet::from_json_str(r#"{"latency_ms.p95":{"max_regression_percent":20.0}}"#)
                .unwrap();
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

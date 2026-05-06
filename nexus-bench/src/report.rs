use std::{fs, path::Path};

use crate::{
    error::{BenchError, BenchResult},
    metrics::BenchResultFile,
    threshold::{DiffResult, DiffStatus},
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

pub fn render_diff_markdown(diff: &DiffResult) -> String {
    let mut out = String::from(
        "# Nexus Bench Diff\n\n| Metric | Baseline | Candidate | Change | Threshold | Status |\n|---|---:|---:|---:|---:|---|\n",
    );
    for row in &diff.rows {
        let baseline = row
            .baseline
            .map(format_f64)
            .unwrap_or_else(|| "n/a".to_string());
        let candidate = row
            .candidate
            .map(format_f64)
            .unwrap_or_else(|| "n/a".to_string());
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
            started_at: chrono::DateTime::parse_from_rfc3339("2026-05-06T00:00:00Z")
                .unwrap()
                .to_utc(),
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

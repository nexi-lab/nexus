use std::{
    fs,
    path::{Path, PathBuf},
    time::Instant,
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::{
    error::{BenchError, BenchResult},
    metrics::BenchResultFile,
    report::{render_summary_markdown, write_markdown, write_result_json},
    runner::run_trace,
    target::BenchTarget,
    trace::load_trace,
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SuiteResultFile {
    pub schema_version: u32,
    pub target: String,
    pub git_sha: String,
    pub started_at: DateTime<Utc>,
    pub warmups: u32,
    pub iterations: u32,
    pub workloads: Vec<SuiteWorkloadResult>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SuiteWorkloadResult {
    pub workload: String,
    pub trace_path: String,
    pub summary: SuiteWorkloadSummary,
    pub runs: Vec<SuiteRunReference>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SuiteRunReference {
    pub iteration: u32,
    pub result_path: String,
    pub markdown_path: String,
    pub result: BenchResultFile,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SuiteWorkloadSummary {
    pub run_count: u32,
    pub throughput_ops_per_sec: MetricAggregate,
    pub latency_p95_ms: MetricAggregate,
    pub latency_p99_ms: MetricAggregate,
    pub rpc_count: MetricAggregate,
    pub bytes_egress: MetricAggregate,
    pub logical_bytes_read: MetricAggregate,
    pub logical_bytes_written: MetricAggregate,
    pub cache_hit_rate: Option<MetricAggregate>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MetricAggregate {
    pub min: f64,
    pub p50: f64,
    pub p95: f64,
    pub max: f64,
    pub mean: f64,
}

pub type OptionalMetricAggregate = Option<MetricAggregate>;

#[derive(Debug, Clone, Copy)]
pub struct SuiteRunOptions<'a> {
    pub target_name: &'a str,
    pub git_sha: &'a str,
    pub trace_dir: &'a Path,
    pub out_dir: &'a Path,
    pub iterations: u32,
    pub warmups: u32,
    pub allow_errors: bool,
}

impl MetricAggregate {
    pub fn from_values(mut values: Vec<f64>) -> Self {
        values.sort_by(|a, b| a.total_cmp(b));
        let mean = values.iter().sum::<f64>() / values.len() as f64;
        Self {
            min: values[0],
            p50: percentile(&values, 0.50),
            p95: percentile(&values, 0.95),
            max: values[values.len() - 1],
            mean,
        }
    }
}

impl SuiteWorkloadResult {
    pub fn from_runs(workload: String, trace_path: String, runs: Vec<BenchResultFile>) -> Self {
        let references = runs
            .into_iter()
            .enumerate()
            .map(|(idx, result)| SuiteRunReference {
                iteration: (idx + 1) as u32,
                result_path: String::new(),
                markdown_path: String::new(),
                result,
            })
            .collect();
        Self::from_run_references(workload, trace_path, references)
    }

    pub fn from_run_references(
        workload: String,
        trace_path: String,
        runs: Vec<SuiteRunReference>,
    ) -> Self {
        let summary = SuiteWorkloadSummary::from_runs(&runs);
        Self {
            workload,
            trace_path,
            summary,
            runs,
        }
    }
}

impl SuiteWorkloadSummary {
    fn from_runs(runs: &[SuiteRunReference]) -> Self {
        let results: Vec<&BenchResultFile> = runs.iter().map(|run| &run.result).collect();
        Self {
            run_count: runs.len() as u32,
            throughput_ops_per_sec: aggregate(&results, |result| result.throughput.ops_per_sec),
            latency_p95_ms: aggregate(&results, |result| result.latency_ms.p95),
            latency_p99_ms: aggregate(&results, |result| result.latency_ms.p99),
            rpc_count: aggregate(&results, |result| result.rpc_count as f64),
            bytes_egress: aggregate(&results, |result| result.bytes_egress as f64),
            logical_bytes_read: aggregate(&results, |result| result.logical_bytes_read as f64),
            logical_bytes_written: aggregate(&results, |result| {
                result.logical_bytes_written as f64
            }),
            cache_hit_rate: optional_aggregate(&results, |result| result.cache_hit_rate),
        }
    }
}

pub fn validate_suite_counts(iterations: u32, _warmups: u32) -> BenchResult<()> {
    if iterations == 0 {
        return Err(BenchError::Target(
            "--iterations must be greater than zero".to_string(),
        ));
    }
    Ok(())
}

pub fn run_suite<T: BenchTarget + Clone + 'static>(
    target: &T,
    options: SuiteRunOptions<'_>,
) -> BenchResult<SuiteResultFile> {
    validate_suite_counts(options.iterations, options.warmups)?;
    fs::create_dir_all(options.out_dir).map_err(|source| BenchError::Io {
        path: options.out_dir.to_path_buf(),
        source,
    })?;

    let started_at = Utc::now();
    let trace_paths = list_trace_paths(options.trace_dir)?;
    let mut workloads = Vec::new();

    for trace_path in trace_paths {
        let trace_ops = load_trace(&trace_path)?;
        let workload = workload_name(&trace_path).to_string();
        for _ in 0..options.warmups {
            run_trace(target, &trace_ops, options.allow_errors)?;
        }

        let workload_dir = options.out_dir.join("runs").join(&workload);
        fs::create_dir_all(&workload_dir).map_err(|source| BenchError::Io {
            path: workload_dir.clone(),
            source,
        })?;

        let mut run_refs = Vec::new();
        for iteration in 1..=options.iterations {
            let run_started_at = Utc::now();
            let started = Instant::now();
            let samples = run_trace(target, &trace_ops, options.allow_errors)?;
            let result = BenchResultFile::from_samples(
                &workload,
                options.target_name,
                options.git_sha,
                run_started_at,
                started.elapsed(),
                &samples,
            );
            let result_path = workload_dir.join(format!("run-{iteration:03}.json"));
            let markdown_path = workload_dir.join(format!("run-{iteration:03}.md"));
            write_result_json(&result_path, &result)?;
            write_markdown(&markdown_path, &render_summary_markdown(&result))?;
            run_refs.push(SuiteRunReference {
                iteration,
                result_path: display_path(options.out_dir, &result_path),
                markdown_path: display_path(options.out_dir, &markdown_path),
                result,
            });
        }

        workloads.push(SuiteWorkloadResult::from_run_references(
            workload,
            trace_path.to_string_lossy().to_string(),
            run_refs,
        ));
    }

    let suite = SuiteResultFile {
        schema_version: 1,
        target: options.target_name.to_string(),
        git_sha: options.git_sha.to_string(),
        started_at,
        warmups: options.warmups,
        iterations: options.iterations,
        workloads,
    };
    write_suite_artifacts(options.out_dir, &suite)?;
    Ok(suite)
}

pub fn write_suite_artifacts(out_dir: &Path, suite: &SuiteResultFile) -> BenchResult<()> {
    let json = serde_json::to_vec_pretty(suite).map_err(|source| BenchError::ParseJson {
        path: out_dir.join("suite.json"),
        source,
    })?;
    fs::write(out_dir.join("suite.json"), json).map_err(|source| BenchError::Io {
        path: out_dir.join("suite.json"),
        source,
    })?;
    write_markdown(&out_dir.join("suite.md"), &render_suite_markdown(suite))
}

pub fn render_suite_markdown(suite: &SuiteResultFile) -> String {
    let mut out = format!(
        "# Nexus Bench Suite: {}\n\n- git: `{}`\n- warmups: `{}`\n- iterations: `{}`\n\n| Workload | Runs | p50 ops/sec | p95 latency p95 | p95 latency p99 | p50 RPC count | p50 egress bytes | cache hit p50 |\n|---|---:|---:|---:|---:|---:|---:|---:|\n",
        suite.target, suite.git_sha, suite.warmups, suite.iterations
    );
    for workload in &suite.workloads {
        let cache = workload
            .summary
            .cache_hit_rate
            .as_ref()
            .map(|value| format!("{:.3}", value.p50))
            .unwrap_or_else(|| "n/a".to_string());
        out.push_str(&format!(
            "| {} | {} | {:.3} | {:.3} | {:.3} | {:.3} | {:.3} | {} |\n",
            workload.workload,
            workload.summary.run_count,
            workload.summary.throughput_ops_per_sec.p50,
            workload.summary.latency_p95_ms.p95,
            workload.summary.latency_p99_ms.p95,
            workload.summary.rpc_count.p50,
            workload.summary.bytes_egress.p50,
            cache
        ));
    }
    out
}

fn aggregate(
    results: &[&BenchResultFile],
    value: impl Fn(&BenchResultFile) -> f64,
) -> MetricAggregate {
    MetricAggregate::from_values(results.iter().map(|result| value(result)).collect())
}

fn optional_aggregate(
    results: &[&BenchResultFile],
    value: impl Fn(&BenchResultFile) -> Option<f64>,
) -> OptionalMetricAggregate {
    let values: Vec<f64> = results.iter().filter_map(|result| value(result)).collect();
    if values.is_empty() {
        None
    } else {
        Some(MetricAggregate::from_values(values))
    }
}

fn percentile(values: &[f64], quantile: f64) -> f64 {
    let idx = ((values.len() - 1) as f64 * quantile).round() as usize;
    values[idx.min(values.len() - 1)]
}

fn list_trace_paths(trace_dir: &Path) -> BenchResult<Vec<PathBuf>> {
    let mut paths = Vec::new();
    for entry in fs::read_dir(trace_dir).map_err(|source| BenchError::Io {
        path: trace_dir.to_path_buf(),
        source,
    })? {
        let path = entry
            .map_err(|source| BenchError::Io {
                path: trace_dir.to_path_buf(),
                source,
            })?
            .path();
        if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
            paths.push(path);
        }
    }
    paths.sort();
    if paths.is_empty() {
        return Err(BenchError::Target(format!(
            "no JSON traces found in {}",
            trace_dir.display()
        )));
    }
    Ok(paths)
}

fn workload_name(path: &Path) -> &str {
    path.file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("unknown")
}

fn display_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics::{BenchResultFile, LatencySummary, OperationCounts, ThroughputSummary};
    use std::collections::BTreeMap;

    fn result(
        workload: &str,
        ops_per_sec: f64,
        p95: f64,
        cache_hit_rate: Option<f64>,
    ) -> BenchResultFile {
        BenchResultFile {
            schema_version: 1,
            workload: workload.to_string(),
            target: "noop".to_string(),
            git_sha: "abc".to_string(),
            started_at: chrono::Utc::now(),
            duration_ms: 10.0,
            operations: OperationCounts {
                total: 2,
                succeeded: 2,
                failed: 0,
                by_kind: BTreeMap::from([("read".to_string(), 2)]),
            },
            throughput: ThroughputSummary {
                ops_per_sec,
                read_bytes_per_sec: ops_per_sec * 10.0,
                write_bytes_per_sec: 0.0,
            },
            latency_ms: LatencySummary {
                min: 1.0,
                p50: p95 / 2.0,
                p90: p95,
                p95,
                p99: p95 + 1.0,
                max: p95 + 2.0,
                mean: p95 / 2.0,
            },
            rpc_count: 2,
            bytes_egress: 100,
            logical_bytes_read: 100,
            logical_bytes_written: 0,
            cache_hit_rate,
            errors: vec![],
        }
    }

    #[test]
    fn aggregate_metric_reports_run_distribution() {
        let aggregate = MetricAggregate::from_values(vec![10.0, 30.0, 20.0]);
        assert_eq!(aggregate.min, 10.0);
        assert_eq!(aggregate.p50, 20.0);
        assert_eq!(aggregate.p95, 30.0);
        assert_eq!(aggregate.max, 30.0);
        assert_eq!(aggregate.mean, 20.0);
    }

    #[test]
    fn workload_summary_aggregates_required_metrics() {
        let runs = vec![
            result("agent-cold-start", 100.0, 10.0, Some(0.5)),
            result("agent-cold-start", 200.0, 20.0, Some(0.75)),
            result("agent-cold-start", 300.0, 30.0, Some(1.0)),
        ];

        let workload = SuiteWorkloadResult::from_runs(
            "agent-cold-start".to_string(),
            "traces/agent-cold-start.json".to_string(),
            runs,
        );

        assert_eq!(workload.runs.len(), 3);
        assert_eq!(workload.summary.throughput_ops_per_sec.p50, 200.0);
        assert_eq!(workload.summary.latency_p95_ms.p95, 30.0);
        assert_eq!(workload.summary.rpc_count.max, 2.0);
        assert_eq!(workload.summary.cache_hit_rate.as_ref().unwrap().p50, 0.75);
    }

    #[test]
    fn suite_counts_require_at_least_one_measured_iteration() {
        let err = validate_suite_counts(0, 1).expect_err("zero iterations must fail");
        assert!(err.to_string().contains("--iterations"));
    }
}

use std::{path::PathBuf, process::Command, time::Instant};

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
            for entry in std::fs::read_dir(&trace_dir).map_err(|source| BenchError::Io {
                path: trace_dir.clone(),
                source,
            })? {
                let path = entry
                    .map_err(|source| BenchError::Io {
                        path: trace_dir.clone(),
                        source,
                    })?
                    .path();
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
                TargetKind::Noop => replay_with_target(
                    &NoopTarget,
                    "noop",
                    &trace,
                    &trace_ops,
                    &out_json,
                    &out_md,
                    allow_errors,
                ),
                TargetKind::Mount => {
                    let root = mount_root.ok_or_else(|| {
                        BenchError::Target("--mount-root is required for mount target".to_string())
                    })?;
                    let target = MountTarget::new(root);
                    replay_with_target(
                        &target,
                        "mount",
                        &trace,
                        &trace_ops,
                        &out_json,
                        &out_md,
                        allow_errors,
                    )
                }
                TargetKind::Http => {
                    let base_url = base_url.ok_or_else(|| {
                        BenchError::Target("--base-url is required for http target".to_string())
                    })?;
                    let api_key = api_key.ok_or_else(|| {
                        BenchError::Target("--api-key is required for http target".to_string())
                    })?;
                    let target = HttpTarget::new(base_url, api_key)?;
                    replay_with_target(
                        &target,
                        "http",
                        &trace,
                        &trace_ops,
                        &out_json,
                        &out_md,
                        allow_errors,
                    )
                }
            }
        }
        Commands::Diff {
            baseline,
            candidate,
            threshold,
            out_md,
        } => {
            let baseline_result: BenchResultFile =
                serde_json::from_slice(&std::fs::read(&baseline).map_err(|source| {
                    BenchError::ReadFile {
                        path: baseline.clone(),
                        source,
                    }
                })?)
                .map_err(|source| BenchError::ParseJson {
                    path: baseline.clone(),
                    source,
                })?;
            let candidate_result: BenchResultFile =
                serde_json::from_slice(&std::fs::read(&candidate).map_err(|source| {
                    BenchError::ReadFile {
                        path: candidate.clone(),
                        source,
                    }
                })?)
                .map_err(|source| BenchError::ParseJson {
                    path: candidate.clone(),
                    source,
                })?;
            ensure_diff_compatible(&baseline_result, &candidate_result)?;
            let thresholds = ThresholdSet::load(&threshold)?;
            let diff = evaluate(&baseline_result, &candidate_result, &thresholds);
            write_markdown(&out_md, &render_diff_markdown(&diff))?;
            if diff.passed {
                Ok(())
            } else {
                Err(BenchError::Threshold(
                    "candidate exceeded regression threshold".to_string(),
                ))
            }
        }
    }
}

fn ensure_diff_compatible(
    baseline: &BenchResultFile,
    candidate: &BenchResultFile,
) -> BenchResult<()> {
    if baseline.schema_version != candidate.schema_version {
        return Err(BenchError::Threshold(format!(
            "baseline schema_version {} does not match candidate schema_version {}",
            baseline.schema_version, candidate.schema_version
        )));
    }
    if baseline.workload != candidate.workload {
        return Err(BenchError::Threshold(format!(
            "baseline workload {} does not match candidate workload {}",
            baseline.workload, candidate.workload
        )));
    }
    if baseline.target != candidate.target {
        return Err(BenchError::Threshold(format!(
            "baseline target {} does not match candidate target {}",
            baseline.target, candidate.target
        )));
    }
    ensure_result_succeeded("baseline", baseline)?;
    ensure_result_succeeded("candidate", candidate)?;
    Ok(())
}

fn ensure_result_succeeded(label: &str, result: &BenchResultFile) -> BenchResult<()> {
    if result.operations.failed > 0 {
        return Err(BenchError::Threshold(format!(
            "{label} result has {} failed operation(s)",
            result.operations.failed
        )));
    }
    if !result.errors.is_empty() {
        return Err(BenchError::Threshold(format!(
            "{label} result has {} recorded error(s)",
            result.errors.len()
        )));
    }
    Ok(())
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
    path.file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or("unknown")
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::metrics::{LatencySummary, OperationCounts, ThroughputSummary};
    use std::collections::BTreeMap;

    fn result(workload: &str, target: &str, schema_version: u32) -> BenchResultFile {
        BenchResultFile {
            schema_version,
            workload: workload.to_string(),
            target: target.to_string(),
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
                ops_per_sec: 1.0,
                read_bytes_per_sec: 0.0,
                write_bytes_per_sec: 0.0,
            },
            latency_ms: LatencySummary {
                min: 1.0,
                p50: 1.0,
                p90: 1.0,
                p95: 1.0,
                p99: 1.0,
                max: 1.0,
                mean: 1.0,
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
    fn diff_compatibility_accepts_same_workload_target_and_schema() {
        ensure_diff_compatible(
            &result("agent-cold-start", "noop", 1),
            &result("agent-cold-start", "noop", 1),
        )
        .expect("matching result files should compare");
    }

    #[test]
    fn diff_compatibility_rejects_schema_mismatch() {
        let err = ensure_diff_compatible(
            &result("agent-cold-start", "noop", 1),
            &result("agent-cold-start", "noop", 2),
        )
        .expect_err("schema mismatch must fail");
        assert!(err.to_string().contains("schema_version"));
    }

    #[test]
    fn diff_compatibility_rejects_workload_mismatch() {
        let err = ensure_diff_compatible(
            &result("agent-cold-start", "noop", 1),
            &result("agent-warm-trace", "noop", 1),
        )
        .expect_err("workload mismatch must fail");
        assert!(err.to_string().contains("workload"));
    }

    #[test]
    fn diff_compatibility_rejects_target_mismatch() {
        let err = ensure_diff_compatible(
            &result("agent-cold-start", "noop", 1),
            &result("agent-cold-start", "mount", 1),
        )
        .expect_err("target mismatch must fail");
        assert!(err.to_string().contains("target"));
    }

    #[test]
    fn diff_compatibility_rejects_failed_baseline_result() {
        let mut baseline = result("agent-cold-start", "noop", 1);
        baseline.operations.failed = 1;

        let err = ensure_diff_compatible(&baseline, &result("agent-cold-start", "noop", 1))
            .expect_err("failed baseline replay must not compare");
        assert!(err.to_string().contains("baseline"));
        assert!(err.to_string().contains("failed"));
    }

    #[test]
    fn diff_compatibility_rejects_failed_candidate_result() {
        let mut candidate = result("agent-cold-start", "noop", 1);
        candidate.errors.push("read failed".to_string());

        let err = ensure_diff_compatible(&result("agent-cold-start", "noop", 1), &candidate)
            .expect_err("failed candidate replay must not compare");
        assert!(err.to_string().contains("candidate"));
        assert!(err.to_string().contains("failed"));
    }
}

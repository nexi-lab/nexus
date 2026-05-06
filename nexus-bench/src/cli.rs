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

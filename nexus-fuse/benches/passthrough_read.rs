use criterion::Criterion;
use std::env;
use std::hint::black_box;
use std::process::Command;

const BENCH_FILE_ENV: &str = "NEXUS_FUSE_PASSTHROUGH_BENCH_FILE";

fn main() {
    let Some(input_path) = bench_file_from_env() else {
        eprintln!(
            "Skipping passthrough read benchmark: set {BENCH_FILE_ENV} to a large file inside a mounted Nexus FUSE passthrough filesystem.\n\
             Example: {BENCH_FILE_ENV}=/mnt/nexus/data/one-gib.bin cargo bench --bench passthrough_read -- --sample-size 10"
        );
        return;
    };

    let mut criterion = Criterion::default().configure_from_args();
    criterion.bench_function("issue_4060_passthrough_dd_read", |b| {
        b.iter(|| {
            let status = Command::new("dd")
                .arg(format!("if={input_path}"))
                .arg("of=/dev/null")
                .arg("bs=8M")
                .arg("status=none")
                .status()
                .expect("run dd passthrough read benchmark");
            assert!(status.success(), "dd passthrough read benchmark failed");
            black_box(status);
        })
    });
    criterion.final_summary();
}

fn bench_file_from_env() -> Option<String> {
    env::var(BENCH_FILE_ENV)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

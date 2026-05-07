use nexus_bench::trace::{load_trace, OpKind};

fn trace_path(name: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("traces")
        .join(name)
}

#[test]
fn seq_large_read_spans_one_gibibyte() {
    let trace = load_trace(&trace_path("seq-large-read.json")).expect("trace should load");
    let total_read: u64 = trace
        .iter()
        .filter(|op| op.op == OpKind::Read)
        .map(|op| op.length.unwrap_or(0))
        .sum();

    assert!(
        total_read >= 1024 * 1024 * 1024,
        "seq-large-read should cover at least 1 GiB, got {total_read} bytes"
    );
}

#[test]
fn bursty_write_contains_one_hundred_writes() {
    let trace = load_trace(&trace_path("bursty-write.json")).expect("trace should load");
    let writes = trace.iter().filter(|op| op.op == OpKind::Write).count();

    assert!(
        writes >= 100,
        "bursty-write should contain at least 100 writes, got {writes}"
    );
}

#[test]
fn metadata_storm_touches_one_hundred_files() {
    let trace = load_trace(&trace_path("metadata-storm.json")).expect("trace should load");
    let metadata_ops = trace
        .iter()
        .filter(|op| matches!(op.op, OpKind::Getattr | OpKind::Lookup))
        .count();

    assert!(
        metadata_ops >= 100,
        "metadata-storm should contain at least 100 metadata ops, got {metadata_ops}"
    );
}

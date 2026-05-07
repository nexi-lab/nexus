# Issue #4063 - Benchmark Harness Design

**Date**: 2026-05-06
**Issue**: [#4063](https://github.com/nexi-lab/nexus/issues/4063) - [P0] Benchmark harness and regression suite

## Context

The S3 Files roadmap issues need comparable performance claims such as hit-rate improvements, sequential-read speedups, write coalescing wins, and metadata-storm regressions. The repo currently has two benchmark surfaces:

- Python `pytest-benchmark` tests under `tests/benchmarks/`, collected by `.github/workflows/benchmark.yml`.
- A standalone `nexus-fuse` Criterion benchmark suite under `nexus-fuse/benches/`.

Those are useful but do not provide a shared trace format, committed workloads, replayable result files, or a diff tool that reviewers can use across PRs. Issue #4063 needs that shared harness before the cache, prefetch, coalescing, and async-client changes land.

## Decision

Add a standalone Rust crate at `nexus-bench/` that implements deterministic trace replay, structured result capture, Markdown summaries, and result diffing.

The crate stays outside the root Cargo workspace, like `nexus-fuse`, because the root workspace is organized around `rust/*` crates and `nexus-fuse` is explicitly excluded for dependency-boundary reasons. CI runs the harness with:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml
cargo run --manifest-path nexus-bench/Cargo.toml -- replay --target noop --trace nexus-bench/traces/agent-cold-start.json --out-json /tmp/nexus-bench.json --out-md /tmp/nexus-bench.md
```

The first implementation supports three targets:

- `noop`: deterministic in-process target for unit tests, trace validation, and CI smoke replay.
- `mount`: filesystem target that replays reads, writes, metadata calls, and directory scans against an already-mounted path.
- `http`: Nexus JSON-RPC target matching the existing `nexus-fuse` HTTP client behavior.

The first PR does not orchestrate a Nexus server or mount FUSE inside CI. The harness accepts existing endpoints and mount points so developers and later CI jobs can run end-to-end benchmarks without changing the trace/result contracts.

## Non-goals

1. A new production metrics pipeline.
2. A gRPC client implementation in the first PR. The target abstraction leaves room for it, but the current local client surface is HTTP JSON-RPC and mounted filesystem paths.
3. Automatic FUSE mounting in GitHub Actions. FUSE setup is environment-sensitive and should be introduced after the harness can already validate traces and result diffs.
4. Replacing existing `pytest-benchmark` tests.
5. Statistical benchmarking with Criterion inside the trace harness. Trace replay records operation latencies and aggregate metrics for workload comparison.

## CLI

The binary is named `nexus-bench`.

Replay a trace:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --trace nexus-bench/traces/agent-cold-start.json \
  --target noop \
  --out-json target/nexus-bench/agent-cold-start.json \
  --out-md target/nexus-bench/agent-cold-start.md
```

Replay against a mounted path:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --trace nexus-bench/traces/seq-large-read.json \
  --target mount \
  --mount-root /tmp/nexus-mount \
  --out-json target/nexus-bench/seq-large-read.json \
  --out-md target/nexus-bench/seq-large-read.md
```

Replay against a Nexus HTTP endpoint:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --trace nexus-bench/traces/agent-warm-trace.json \
  --target http \
  --base-url http://localhost:2026 \
  --api-key sk-test-key-123 \
  --out-json target/nexus-bench/agent-warm-trace.json \
  --out-md target/nexus-bench/agent-warm-trace.md
```

Compare two result files:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- diff \
  --baseline nexus-bench/baselines/develop-5698d0026-noop.json \
  --candidate target/nexus-bench/agent-cold-start.json \
  --threshold nexus-bench/thresholds/default.json \
  --out-md target/nexus-bench/diff.md
```

Validate committed traces:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
```

## Trace Format

Traces are committed as JSON arrays. Each entry is one replayable operation:

```json
[
  {
    "timestamp_ns": 0,
    "op": "read",
    "path": "/workspace/README.md",
    "offset": 0,
    "length": 32768
  },
  {
    "timestamp_ns": 500000,
    "op": "getattr",
    "path": "/workspace/src/main.py"
  },
  {
    "timestamp_ns": 900000,
    "op": "write",
    "path": "/workspace/out.log",
    "offset": 0,
    "length": 1024,
    "payload_seed": 17,
    "parallel_group": "burst-1"
  }
]
```

Supported operations:

- `read`
- `write`
- `getattr`
- `lookup`
- `readdir`
- `delete`
- `rename`
- `mkdir`

Rules:

- `timestamp_ns` is monotonic within a trace.
- `path` is absolute and normalized.
- `read` requires `offset` and `length`.
- `write` requires `offset`, `length`, and `payload_seed`.
- `rename` requires `path` and `to_path`.
- `readdir` uses `path` as the directory.
- `lookup` and `getattr` use `path` as the metadata path.
- `parallel_group` is optional. Adjacent entries with the same non-empty group are dispatched concurrently after all earlier ungrouped or grouped entries complete.
- Replay preserves trace order except for explicitly grouped operations. The runner records scheduled timestamp gaps but does not sleep by default; a `--respect-timestamps` option can enable wall-clock pacing for local experiments.

JSON is the canonical committed format because it is directly typed in Rust and easy to review. CSV import can be added later through a separate converter without changing the canonical schema.

## Standard Workloads

Commit the eight issue workloads under `nexus-bench/traces/`:

| Workload | Trace File | Pattern |
|---|---|---|
| `agent-cold-start` | `agent-cold-start.json` | Many reads below 128 KiB and metadata calls during workspace attach |
| `agent-warm-trace` | `agent-warm-trace.json` | Mixed reads of recently touched files |
| `seq-large-read` | `seq-large-read.json` | Sequential reads over a synthetic 1 GiB logical file |
| `stride-read` | `stride-read.json` | Fixed-stride reads that mimic columnar data access |
| `bursty-write` | `bursty-write.json` | 1000 small writes in a 100 ms logical window |
| `read-after-write` | `read-after-write.json` | Write then read the same ranges |
| `concurrent-reads` | `concurrent-reads.json` | Parallel-reader groups against the same file |
| `metadata-storm` | `metadata-storm.json` | 10k `getattr`/`lookup` style operations |

The traces are intentionally synthetic and small enough to review. Large logical files are represented by repeated range operations rather than by committed payloads.

## Replay Engine

The crate is split into focused modules:

- `trace`: schema types, validation, and file loading.
- `target`: `BenchTarget` trait plus `noop`, `mount`, and `http` implementations.
- `runner`: ordered replay, per-operation timing, parallel groups, and error aggregation.
- `metrics`: latency percentiles, throughput, operation counts, byte counts, and nullable target-specific counters.
- `report`: JSON result writing, Markdown summary rendering, and result diffing.
- `threshold`: regression-threshold parsing and pass/fail evaluation.
- `cli`: command-line parsing and command dispatch.

The central target interface is:

```rust
pub trait BenchTarget: Send + Sync {
    fn execute(&self, op: &TraceOp) -> Result<OperationMetrics, BenchError>;
}
```

`OperationMetrics` includes:

- `logical_bytes_read`
- `logical_bytes_written`
- `rpc_count`
- `egress_bytes`
- `cache_hit`

`cache_hit` is `Option<bool>`. `noop` and `mount` return `None`; `http` returns `None` until the server exposes a reliable hit/miss signal. The result JSON still includes `cache_hit_rate: null` so the contract is explicit.

The `http` target uses the current JSON-RPC endpoints exposed by `/api/nfs/*`. Current reads return whole-file content, not byte ranges, so `logical_bytes_read` records the requested trace range while `egress_bytes` records the bytes actually returned by the server. That distinction is deliberate because range-read and prefetch work needs to show whether network egress changes.

## Result JSON

Replay produces a stable JSON shape:

```json
{
  "schema_version": 1,
  "workload": "agent-cold-start",
  "target": "noop",
  "git_sha": "5698d0026",
  "started_at": "2026-05-06T00:00:00Z",
  "duration_ms": 12.4,
  "operations": {
    "total": 128,
    "succeeded": 128,
    "failed": 0,
    "by_kind": {"read": 96, "getattr": 32}
  },
  "throughput": {
    "ops_per_sec": 10322.5,
    "read_bytes_per_sec": 33873920.0,
    "write_bytes_per_sec": 0.0
  },
  "latency_ms": {
    "min": 0.001,
    "p50": 0.004,
    "p90": 0.009,
    "p95": 0.012,
    "p99": 0.02,
    "max": 0.04,
    "mean": 0.006
  },
  "rpc_count": 128,
  "bytes_egress": 3145728,
  "logical_bytes_read": 3145728,
  "logical_bytes_written": 0,
  "cache_hit_rate": null,
  "errors": []
}
```

Failed operations are recorded in `errors` and increment `operations.failed`. By default replay exits non-zero if any operation fails. `--allow-errors` records failures and exits zero for exploratory runs.

## Markdown Reports

Replay writes a Markdown summary table:

```markdown
# Nexus Bench Result: agent-cold-start

| Metric | Value |
|---|---:|
| target | noop |
| operations | 128 |
| failed | 0 |
| ops/sec | 10322.50 |
| p50 latency | 0.004 ms |
| p95 latency | 0.012 ms |
| p99 latency | 0.020 ms |
| rpc count | 128 |
| bytes egress | 3.00 MiB |
| cache hit rate | n/a |
```

Diff mode writes one row per metric with baseline, candidate, percent change, threshold, and status.

## Regression Thresholds

Thresholds live in `nexus-bench/thresholds/default.json`:

```json
{
  "latency_ms.p95": {"max_regression_percent": 20.0},
  "latency_ms.p99": {"max_regression_percent": 25.0},
  "throughput.ops_per_sec": {"max_regression_percent": 20.0, "higher_is_better": true},
  "rpc_count": {"max_regression_percent": 10.0},
  "bytes_egress": {"max_regression_percent": 10.0}
}
```

The diff command exits non-zero when a candidate exceeds any threshold. Metrics absent or `null` in either file are marked `skipped` and do not fail the run.

## Baseline

Commit one smoke baseline generated from the deterministic `noop` target:

```text
nexus-bench/baselines/develop-5698d0026-noop.json
```

This baseline proves that diffing, thresholds, and report generation work in CI. Real `mount` and `http` baselines are generated locally after the harness lands and can be committed by follow-up PRs with environment details in the result metadata.

## CI

Add a lightweight workflow `.github/workflows/nexus-bench.yml` triggered by:

- `nexus-bench/**`
- `nexus-fuse/**`
- `rust/kernel/**`
- `rust/backends/**`
- `rust/transport/**`
- `.github/workflows/nexus-bench.yml`

The workflow runs:

```bash
cargo test --manifest-path nexus-bench/Cargo.toml
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
cargo run --manifest-path nexus-bench/Cargo.toml -- replay --target noop --trace nexus-bench/traces/agent-cold-start.json --out-json /tmp/agent-cold-start.json --out-md /tmp/agent-cold-start.md
cargo run --manifest-path nexus-bench/Cargo.toml -- diff --baseline nexus-bench/baselines/develop-5698d0026-noop.json --candidate /tmp/agent-cold-start.json --threshold nexus-bench/thresholds/default.json --out-md /tmp/nexus-bench-diff.md
```

The workflow uploads JSON and Markdown artifacts. PR comments can be added later after the harness is producing stable real-target baselines.

## Documentation

Add `nexus-bench/README.md` with:

- how to validate traces
- how to replay with `noop`, `mount`, and `http`
- how to compare result files
- how to interpret metrics
- how to add a workload trace
- how to generate local baselines

The README explicitly states that `cache_hit_rate` is `n/a` until a target exposes hit/miss data.

## Acceptance Criteria Mapping

- Trace-replay tool runs against local Nexus instance: `http` target supports `--base-url` and `--api-key`; `mount` target supports an already-mounted local path.
- All 8 standard workloads implemented: committed trace files under `nexus-bench/traces/`.
- Results captured as JSON and Markdown: `replay --out-json --out-md`.
- Diff mode for comparing two runs: `diff --baseline --candidate --threshold --out-md`.
- CI integration with regression gates on hot-path crates: `.github/workflows/nexus-bench.yml` validates traces, runs deterministic replay, and fails on threshold breaches.
- Baseline measured on current `develop` and committed: deterministic `noop` baseline for commit `5698d0026`.
- Documentation: `nexus-bench/README.md` runbook for running locally and adding workloads.

## Risks

- Real FUSE benchmarks can be noisy in CI. The first CI gate uses deterministic `noop` replay and leaves real mount benchmarks to local or future dedicated runners.
- Cache hit rate is not currently observable from the supported targets. The result schema includes the field as nullable so later cache instrumentation does not require schema churn.
- Synthetic traces may miss production access patterns. The schema is designed so captured traces can be committed later without changing the replay engine.

## Testing

Unit tests cover:

- trace validation rejects malformed operations
- JSON traces load into typed operations
- `noop` replay records expected byte and operation totals
- latency percentiles are stable for small samples
- Markdown report rendering includes required metrics
- diff mode handles higher-is-better and lower-is-better metrics
- threshold failures return a failing status
- nullable metrics are skipped instead of failing

CI runs unit tests plus an end-to-end `noop` replay and diff against the committed baseline.

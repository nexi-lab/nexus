# Issue #4063 Upgrade - Epic #4061 Measurement Design

**Date**: 2026-05-06
**Branch**: `codex/issue-4063-bench-harness`
**PR**: [#4067](https://github.com/nexi-lab/nexus/pull/4067)

## Goal

Upgrade `nexus-bench` from a harness and CI smoke check into the measurement foundation for epic #4061. The branch must make it hard to confuse smoke plumbing with real performance evidence and must give each #4061 performance PR a repeatable way to generate comparable real-target artifacts.

## Non-Goals

- Do not claim real Nexus/FUSE/S3 performance without a configured real target.
- Do not make default CI mount FUSE or start a Nexus server. Those environments are runner-specific.
- Do not implement #4062 metrics in this PR. The harness should leave slots for cache, prefetch, read-batch, write-coalescing, and passthrough metrics once #4062 exposes them.

## Design

Add a `suite` command to `nexus-bench`:

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- suite \
  --target http \
  --base-url http://localhost:2026 \
  --api-key "$NEXUS_API_KEY" \
  --trace-dir nexus-bench/traces \
  --out-dir target/nexus-bench/real-http \
  --warmups 1 \
  --iterations 5
```

The suite command runs every committed workload trace against `noop`, `mount`, or `http`. It executes configurable warmups, then records repeated measured runs. It writes:

- per-run JSON and Markdown files under `runs/<workload>/run-NNN.*`
- `suite.json`, a machine-readable aggregate
- `suite.md`, a reviewer-friendly aggregate table

Suite summaries aggregate run-level metrics across iterations. The first supported metrics are:

- `throughput.ops_per_sec`
- `latency_ms.p95`
- `latency_ms.p99`
- `rpc_count`
- `bytes_egress`
- `logical_bytes_read`
- `logical_bytes_written`
- `cache_hit_rate` when present

## Epic Matrix

Add a machine-readable and human-readable #4061 matrix that maps each performance issue to required workloads and primary metrics:

- #4053 foyer cache: `agent-cold-start`, `agent-warm-trace`; cache hit rate and p99 latency.
- #4055 eager hydrate: `agent-cold-start`; p50/p95 latency.
- #4056 async HTTP: `concurrent-reads`, `agent-warm-trace`; throughput and p99 latency.
- #4057 prefetch: `seq-large-read`, `stride-read`; throughput, egress, and prefetch metrics from #4062.
- #4058 read batch: `metadata-storm`, `agent-cold-start`; ops/sec, p95 latency, read batch size from #4062.
- #4059 write coalescing: `bursty-write`, `read-after-write`; RPC count and write flush metrics from #4062.
- #4060 passthrough: `seq-large-read`; throughput and passthrough count from #4062.

## CI Strategy

Keep `.github/workflows/nexus-bench.yml` as the always-on smoke gate: unit tests, trace validation, deterministic noop replay, and deterministic threshold diff.

Add `.github/workflows/nexus-bench-real.yml` as a manual `workflow_dispatch` workflow for real targets. It accepts:

- `target`: `noop`, `mount`, or `http`
- `runner_label`: runner label, defaulting to `self-hosted`
- `mount_root`
- `base_url`
- `iterations`
- `warmups`

For HTTP, the workflow reads `NEXUS_BENCH_API_KEY` from repository secrets. For mount, the runner must provide an already-mounted filesystem and seeded files. The workflow uploads the complete suite artifact directory.

## Acceptance Criteria

- `nexus-bench suite --target noop` runs all eight workloads and writes per-run and aggregate artifacts.
- `nexus-bench suite --target mount/http` has the same CLI shape and fails early when required target parameters are missing.
- The suite artifact schema is committed through unit tests.
- The README and #4061 runbook explicitly state that noop is smoke-only and real perf claims require `mount` or `http` suite artifacts.
- The manual workflow can be parsed as valid YAML and points at the suite command.
- Existing smoke CI behavior remains intact.

# Epic #4061 Measurement Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `nexus-bench` so #4061 performance PRs can produce repeatable real-target benchmark artifacts.

**Architecture:** Add a suite layer above existing trace replay. Keep one-shot replay and smoke CI unchanged, then add repeated multi-workload suite execution, aggregate suite reports, an epic metric matrix, a real-target runbook, and a manual workflow for provisioned runners.

**Tech Stack:** Rust 2021, clap, serde JSON, GitHub Actions.

---

## Task 1: Suite Result Model And Aggregation

**Files:**
- Create: `nexus-bench/src/suite.rs`
- Modify: `nexus-bench/src/lib.rs`

- [ ] Add serializable suite result types: `SuiteResultFile`, `SuiteWorkloadResult`, `SuiteRunReference`, `MetricAggregate`, and `OptionalMetricAggregate`.
- [ ] Add tests that build two synthetic `BenchResultFile` values and verify aggregate p50/p95/max values.
- [ ] Export `pub mod suite;`.

## Task 2: Suite Runner And CLI

**Files:**
- Modify: `nexus-bench/src/suite.rs`
- Modify: `nexus-bench/src/cli.rs`

- [ ] Add `suite` CLI command with `--trace-dir`, `--target`, `--mount-root`, `--base-url`, `--api-key`, `--out-dir`, `--iterations`, `--warmups`, and `--allow-errors`.
- [ ] Reuse existing target implementations and `run_trace`.
- [ ] Write per-run artifacts to `runs/<workload>/run-NNN.json` and `run-NNN.md`.
- [ ] Write `suite.json` and `suite.md`.
- [ ] Add tests for iteration validation and target parameter errors.

## Task 3: Epic Matrix And Runbook

**Files:**
- Create: `nexus-bench/matrices/epic-4061.json`
- Create: `nexus-bench/EPIC-4061.md`
- Modify: `nexus-bench/README.md`

- [ ] Map #4053-#4060 to required workloads and metrics.
- [ ] Document that noop is smoke-only.
- [ ] Document real baseline commands for mount and HTTP.
- [ ] Document artifact expectations for performance claims.

## Task 4: Manual Real-Target Workflow

**Files:**
- Create: `.github/workflows/nexus-bench-real.yml`

- [ ] Add `workflow_dispatch` inputs for target, runner label, mount root, base URL, iterations, and warmups.
- [ ] Validate traces and run the suite command.
- [ ] Use `NEXUS_BENCH_API_KEY` secret for HTTP target.
- [ ] Upload the full artifact directory.

## Task 5: Verification And PR Update

**Commands:**
- `cargo test --locked --manifest-path nexus-bench/Cargo.toml`
- `cargo clippy --manifest-path nexus-bench/Cargo.toml --all-targets -- -D warnings`
- `cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces`
- `cargo run --manifest-path nexus-bench/Cargo.toml -- suite --target noop --trace-dir nexus-bench/traces --out-dir /tmp/nexus-bench-suite --warmups 1 --iterations 2`
- `jq -e . /tmp/nexus-bench-suite/suite.json nexus-bench/matrices/epic-4061.json`
- `ruby -e 'require "psych"; Psych.parse_file(".github/workflows/nexus-bench-real.yml")'`
- `git diff --check`

Commit the upgrade and push the existing PR branch.

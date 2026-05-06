# Nexus Bench

`nexus-bench` is the trace-replay benchmark harness for Nexus filesystem performance work.

## Validate traces

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- validate --trace-dir nexus-bench/traces
```

## Replay with the deterministic target

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target noop \
  --trace nexus-bench/traces/agent-cold-start.json \
  --out-json target/nexus-bench/agent-cold-start.json \
  --out-md target/nexus-bench/agent-cold-start.md
```

## Replay against a mounted filesystem

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target mount \
  --mount-root /tmp/nexus-mount \
  --trace nexus-bench/traces/seq-large-read.json \
  --out-json target/nexus-bench/seq-large-read.json \
  --out-md target/nexus-bench/seq-large-read.md
```

## Replay against a Nexus HTTP endpoint

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- replay \
  --target http \
  --base-url http://localhost:2026 \
  --api-key sk-test-key-123 \
  --trace nexus-bench/traces/agent-warm-trace.json \
  --out-json target/nexus-bench/agent-warm-trace.json \
  --out-md target/nexus-bench/agent-warm-trace.md
```

## Compare results

```bash
cargo run --manifest-path nexus-bench/Cargo.toml -- diff \
  --baseline nexus-bench/baselines/develop-5698d0026-noop.json \
  --candidate target/nexus-bench/agent-cold-start.json \
  --threshold nexus-bench/thresholds/default.json \
  --out-md target/nexus-bench/diff.md
```

## Metrics

- `logical_bytes_read` and `logical_bytes_written` come from trace ranges.
- `bytes_egress` is target-observed response traffic. For the HTTP target, current Nexus reads return whole-file content, so egress can exceed logical bytes requested by a range trace.
- `rpc_count` is the number of target operations issued by the harness.
- `cache_hit_rate` is `n/a` until a target exposes reliable hit/miss data.

## Adding a workload

Add a JSON trace under `nexus-bench/traces/`. Each operation needs a monotonic `timestamp_ns`, an absolute `path`, and operation-specific fields:

- `read`: `offset`, `length`
- `write`: `offset`, `length`, `payload_seed`
- `rename`: `to_path`

Use `parallel_group` on adjacent operations that should run concurrently.

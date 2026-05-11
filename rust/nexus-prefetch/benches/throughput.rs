//! Stub throughput bench — real benches land in Task 27 (Phase 5).
//!
//! `harness = false` in Cargo.toml means we own `main`. Keeping it empty
//! lets `cargo check -p nexus-prefetch` resolve the `[[bench]]` entry
//! without pulling criterion into the default build graph.

fn main() {}

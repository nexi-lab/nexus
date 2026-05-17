# justfile — Nexus repo-root task runner
# Install just: https://github.com/casey/just
# Usage: just setup        # build nexus-cluster binary
#        just doctor       # verify env is healthy
#        just build-kernel # rebuild kernel binary

# Build the nexus-cluster binary.
# The kernel now runs as a separate process; Python communicates via gRPC.
# Run after: git clone, git pull, or switching branches with Rust changes.
setup:
    @echo "Building nexus-cluster binary..."
    cargo build --release -p nexus-cluster
    @echo "Done. Binary at target/release/nexus-cluster."

# Verify the environment is healthy.
doctor:
    @echo "Checking nexus-cluster binary..."
    cargo build -p nexus-cluster 2>/dev/null && echo "OK — nexus-cluster builds clean" || echo "FAIL — cargo build failed"

# Rebuild only kernel crate (fastest for Kernel-only changes).
build-kernel:
    cargo build --release -p kernel

# Run the gbrain-evals benchmark gate (Issue #3699 pre-merge check).
#
# Pre-requisites:
#   - GBRAIN_EVALS_DIR  must point to a checkout of https://github.com/garrytan/gbrain-evals
#                       (containing corpus.jsonl and queries.jsonl)
#   - NEXUS_DATABASE_URL must point at a fresh Postgres instance with
#                       pg_textsearch (BM25) and pgvector installed.
#
# Pass/fail gate: recall@5 >= 0.9389, NDCG@5 >= 0.8928 (1 pp slack on the
# issue-3699 baseline of recall@5=0.9489, NDCG@5=0.9028).
#
# To smoke-test with the tiny fixture:
#   GBRAIN_EVALS_DIR=tests/benchmarks/_tiny_fixture \
#   NEXUS_DATABASE_URL=postgresql+asyncpg://localhost/nexus_bench \
#   just bench-search
bench-search:
    @test -n "${GBRAIN_EVALS_DIR}" || (echo "ERROR: set GBRAIN_EVALS_DIR to a gbrain-evals checkout" && exit 1)
    @test -n "${NEXUS_DATABASE_URL}" || (echo "ERROR: set NEXUS_DATABASE_URL to a Postgres URL" && exit 1)
    uv run python tests/benchmarks/gbrain_eval.py

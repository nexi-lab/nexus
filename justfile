# justfile — Nexus repo-root task runner
# Install just: https://github.com/casey/just
# Usage: just setup        # install nexus-cluster binary
#        just doctor       # verify env is healthy

# Install the nexus-cluster binary from nexus-vfs.
# Kernel-tier Rust (including nexus-cluster) lives in
# https://github.com/nexi-lab/nexus-vfs after #4259; this repo
# consumes the binary via `cargo install` rather than building it
# locally. Run after: git clone, git pull, or any nexus-vfs update.
setup:
    @echo "Installing nexus-cluster from nexus-vfs..."
    cargo install --git https://github.com/nexi-lab/nexus-vfs --bin nexusd-cluster nexus-cluster
    @echo "Done. Binary at ~/.cargo/bin/nexusd-cluster."

# Verify the environment is healthy.
doctor:
    @echo "Checking nexusd-cluster on PATH..."
    @command -v nexusd-cluster >/dev/null && echo "OK — nexusd-cluster on PATH" || echo "FAIL — nexusd-cluster missing; run \`just setup\`"

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

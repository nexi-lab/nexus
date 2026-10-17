#!/usr/bin/env bash
set -euo pipefail

# Share the exact daemon/plugin build and protocol check across consumer CI.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${CARGO_TARGET_DIR:?Set the native fixture target directory}"
: "${RUNNER_TEMP:?This helper runs in GitHub Actions}"
: "${GITHUB_ENV:?GitHub Actions environment file is required}"
: "${GITHUB_PATH:?GitHub Actions path file is required}"

REV=$(python3 scripts/nexus_vfs_revision.py)
fixture_source=$(mktemp -d "$RUNNER_TEMP/nexus-vfs-search-fixture.XXXXXX")
git init "$fixture_source"
git -C "$fixture_source" remote add origin https://github.com/nexi-lab/nexus-vfs.git
git -C "$fixture_source" fetch --depth=1 origin "$REV"
git -C "$fixture_source" checkout --detach FETCH_HEAD
uv run --no-project --with grpcio-tools==1.80.0 python \
  scripts/gen_search_proto.py --vfs-checkout "$fixture_source" --check
cargo build --locked --release --manifest-path "$fixture_source/Cargo.toml" \
  -p nexus-cluster -p nexus-search-plugin --features nexus-cluster/http-api
test -x "$CARGO_TARGET_DIR/release/nexusd-cluster"
test -f "$CARGO_TARGET_DIR/release/libnexus_search_plugin.so"
echo "NEXUS_KERNEL_BINARY=$CARGO_TARGET_DIR/release/nexusd-cluster" >> "$GITHUB_ENV"
echo "NEXUS_SEARCH_PLUGIN_LIBRARY=$CARGO_TARGET_DIR/release/libnexus_search_plugin.so" >> "$GITHUB_ENV"
echo "$CARGO_TARGET_DIR/release" >> "$GITHUB_PATH"

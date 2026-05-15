# Build Performance Guide

Tips for faster local builds, especially on macOS.

## Rust Extension Builds

### Use dev builds for local iteration

The project has a tuned `[profile.dev]` in `Cargo.toml` with `opt-level = 1` —
fast enough for testing, much faster to compile than release builds:

```bash
cd rust/nexus_runtime && maturin develop        # ~30s (dev profile)
cd rust/nexus_runtime && maturin develop --release  # ~5min (release, LTO)
```

Use `--release` only when you need production-grade performance (benchmarking, profiling).

### Dependencies are compiled once

The dev profile uses `opt-level = 2` for third-party dependencies via
`[profile.dev.package."*"]`. These are compiled once and cached in `target/`,
so subsequent builds after code changes are fast.

## macOS-Specific Optimizations

### XProtect / Gatekeeper scanning

macOS scans newly compiled binaries, which adds latency to each build.
Enable Developer Mode to reduce this:

```bash
# Allow developer tools to run without Gatekeeper checks
sudo spctl developer-mode enable-terminal
```

You may need to restart your terminal after this.

### Spotlight indexing

Spotlight indexes build artifacts in `target/` and `.venv/`, causing I/O
contention during builds. Exclude these directories:

**System Preferences > Siri & Spotlight > Spotlight Privacy** — add:
- Your project's `target/` directory
- Your project's `.venv/` directory

Or via command line:

```bash
# Add exclusions (requires Full Disk Access for Terminal)
mdutil -i off target/
mdutil -i off .venv/
```

### Docker: OrbStack vs Docker Desktop

[OrbStack](https://orbstack.dev/) runs Docker containers with significantly
lower overhead than Docker Desktop on macOS. It uses a lightweight Linux VM
with near-native file system performance.

If Docker builds are slow, switching to OrbStack is the single highest-impact
change on macOS.

## Optional: sccache

[sccache](https://github.com/mozilla/sccache) caches Rust compilation artifacts
across projects and machines. It's especially useful if you work on multiple
branches or frequently `cargo clean`.

```bash
brew install sccache

# Add to your shell profile:
export RUSTC_WRAPPER=sccache
```

sccache wraps `rustc` and caches compilation results. Cache hits skip compilation
entirely. Works with local disk or remote storage (S3, GCS, Redis).

## Optional: Faster linker (lld)

On macOS, the default linker is slower than LLVM's `lld`. If you have LLVM
installed, you can configure cargo to use it:

```bash
brew install llvm

# Create or edit .cargo/config.toml in your project root:
cat >> .cargo/config.toml << 'EOF'
[target.aarch64-apple-darwin]
rustflags = ["-C", "link-arg=-fuse-ld=lld"]

[target.x86_64-apple-darwin]
rustflags = ["-C", "link-arg=-fuse-ld=lld"]
EOF
```

This can reduce link times by 2-3x. Note: this configuration is personal
and should not be committed to the repository (add `.cargo/config.toml` to
`.gitignore` if needed).

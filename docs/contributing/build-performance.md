# Build Performance Guide

Tips for faster local builds, with special attention to multi-worktree
development setups.

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

## Recommended: sccache (multi-worktree dev)

If you run **multiple git worktrees in parallel** (e.g., several AI agents
on separate branches), each worktree's `target/` accumulates ~10 GB of Rust
build artifacts independently — 8 worktrees can sprawl to 40+ GB of largely
duplicate compiled crates.

[sccache](https://github.com/mozilla/sccache) wraps `rustc` and caches
compilation results by input hash, so the same crate compiled in any worktree
hits the shared cache instead of recompiling. Each worktree still keeps its
own `target/` for link artifacts (no contention with parallel builds), but
the heavy `rustc` work is deduplicated globally.

This is the single highest-leverage change for multi-worktree setups.

### Install

| Platform | Command |
|----------|---------|
| macOS    | `brew install sccache` |
| Linux (apt)   | `sudo apt install sccache` |
| Windows (winget) | `winget install Mozilla.sccache` |
| Any platform (universal fallback, ~3 min) | `cargo install sccache --locked` |

Verify the install:

```bash
sccache --version
```

### Enable for cargo

Add this export to your shell profile (`~/.zshrc`, `~/.bashrc`, or
PowerShell `$PROFILE`) — **once per machine, applies to all worktrees**:

```bash
# bash / zsh
export RUSTC_WRAPPER=sccache
```

```powershell
# PowerShell
$env:RUSTC_WRAPPER = "sccache"
# To persist, add the above line to $PROFILE
```

Open a new shell after editing the profile. From this point every
`cargo build` in any worktree consults the shared sccache.

### Verify it's working

After a build, inspect the cache stats:

```bash
sccache --show-stats
```

You should see `Cache hits` climb when you rebuild the same crate across
worktrees. A fresh first build populates the cache; the second worktree's
first build of the same crate is the one you'll feel — typically **40-70%
faster** for cold builds of large dependencies.

### Cache configuration (optional)

Default cache location and size (10 GB) are sensible for most setups. To
customize, set environment variables before any `cargo` invocation — see
[sccache docs](https://github.com/mozilla/sccache/blob/main/docs/Configuration.md).

Common knob: increase the cache size if you work on many feature branches.

```bash
export SCCACHE_CACHE_SIZE="40G"
```

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

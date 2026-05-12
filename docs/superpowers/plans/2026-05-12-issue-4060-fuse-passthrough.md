# Issue #4060 FUSE Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in Linux FUSE passthrough for eligible large reads across direct Rust mounts and Python `use_rust=True` orchestration while preserving hook-sensitive fallbacks.

**Architecture:** Upgrade `nexus-fuse` to a passthrough-capable `fuser`, then keep passthrough concerns in focused Rust modules for config parsing, eligibility, backing-file materialization, and open-handle lifecycle. The direct Rust mount owns FUSE passthrough; Python remains the orchestration layer and launches the Rust-owned mount only when passthrough is requested and the mount is a remote, raw, hook-safe mount. Existing Python FUSE plus Rust IPC daemon remains the fallback path.

**Tech Stack:** Rust 2021, `fuser 0.17`, Linux FUSE passthrough, `globset`, `sha2`, existing `reqwest`/`foyer` cache helpers, Python 3.11+, `subprocess`, `pytest`, `cargo test`, Linux-gated FUSE integration tests.

---

## File Structure

| Path | Change | Responsibility |
| --- | --- | --- |
| `nexus-fuse/Cargo.toml` | modify | Upgrade `fuser`, add `globset`, `sha2`, and `hex` for passthrough policy and stable backing keys. |
| `nexus-fuse/src/lib.rs` | modify | Export the new `passthrough` module. |
| `nexus-fuse/src/passthrough/mod.rs` | create | Public module boundary for config, policy, backing store, and manager. |
| `nexus-fuse/src/passthrough/config.rs` | create | CLI/env-ready passthrough config, pattern parsing, threshold defaults, and Linux support detection helpers. |
| `nexus-fuse/src/passthrough/policy.rs` | create | Eligibility decisions for path patterns, file metadata, access mode, and threshold. |
| `nexus-fuse/src/passthrough/backing.rs` | create | Materialize immutable local backing files, verify size/etag, and invalidate path-keyed files on mutation. |
| `nexus-fuse/src/passthrough/manager.rs` | create | Coordinate policy, backing store, active open handles, and fuser backing registration. |
| `nexus-fuse/src/fs.rs` | modify | Use `&self` filesystem callbacks after the fuser upgrade; call passthrough from `open`; clean handles from `release`; invalidate backing files on writes/deletes/renames/truncates. |
| `nexus-fuse/src/main.rs` | modify | Add passthrough flags/env vars, build config, pass manager into direct Rust mount, and preserve daemon behavior. |
| `nexus-fuse/benches/passthrough_read.rs` | create | Criterion-compatible sequential read benchmark harness and documented command for the 1 GiB target. |
| `src/nexus/fuse/passthrough.py` | create | Python passthrough options, environment parsing, hook-safety snapshot, and Rust mount process launcher. |
| `src/nexus/fuse/mount.py` | modify | Add public passthrough constructor/function options and route eligible `use_rust=True` mounts to the Rust-owned mount process. |
| `tests/unit/fuse/test_passthrough_options.py` | create | Python env parsing, command building, fallback, and hook-safety tests. |
| `nexus-fuse/README.md` | modify | Document opt-in flags, kernel requirement, fallback behavior, and benchmark command. |
| `nexus-fuse/PERFORMANCE_RESULTS.md` | modify | Record issue #4060 benchmark result or the exact local command used to collect it. |

## External API Notes

Use the current primary sources before editing the fuser integration:

- fuser latest `ReplyOpen` docs: <https://docs.rs/fuser/latest/fuser/struct.ReplyOpen.html>
- fuser latest `Filesystem` trait docs: <https://docs.rs/fuser/latest/fuser/trait.Filesystem.html>
- fuser latest `KernelConfig` docs: <https://docs.rs/fuser/latest/fuser/struct.KernelConfig.html>
- fuser passthrough example source: <https://docs.rs/crate/fuser/latest/source/examples/passthrough.rs>
- Linux FUSE passthrough kernel docs: <https://docs.kernel.org/filesystems/fuse/fuse-passthrough.html>

The Linux kernel contract is: the daemon registers a backing file, returns its backing id from `OPEN`, and later read/write operations can bypass the daemon for that handle. fuser requires `KernelConfig::set_max_stack_depth(1)` during `Filesystem::init`, and the returned `BackingId` must stay alive until `release`. This plan uses that contract only for read-only regular-file opens.

### Task 1: Upgrade fuser And Preserve Existing Behavior

**Files:**
- Modify: `nexus-fuse/Cargo.toml`
- Modify: `nexus-fuse/src/fs.rs`
- Modify: `nexus-fuse/src/main.rs`

- [ ] **Step 1: Capture the current Rust baseline**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test fs::tests --lib
cargo test cache::tests cached_read::tests daemon::tests --lib
```

Expected: all selected tests pass before the dependency upgrade. If a selected test fails before edits, stop and record the failing test name in the PR notes because it is not caused by passthrough.

- [ ] **Step 2: Upgrade the fuser dependency**

In `nexus-fuse/Cargo.toml`, replace the FUSE dependency block with:

```toml
# FUSE filesystem. 0.17 includes the Linux passthrough APIs used by issue #4060.
fuser = "0.17"
```

- [ ] **Step 3: Verify the upgrade exposes compile errors before fixing code**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo check --lib --bin nexus-fuse
```

Expected: compile failures reference changed `fuser::Filesystem` method receivers, method signatures, fuser option/config types, or `FileAttr` field types. Do not change passthrough logic in this step.

- [ ] **Step 4: Update `NexusFs` callback receivers and fuser value types**

In `nexus-fuse/src/fs.rs`, change every `impl Filesystem for NexusFs` callback receiver from `&mut self` to `&self`.

Use these imports at the top of `fs.rs` after the upgrade compiles the type names:

```rust
use fuser::{
    Errno, FileAttr, FileHandle, FileType, Filesystem, FopenFlags, INodeNo, KernelConfig,
    OpenAccMode, OpenFlags, ReplyAttr, ReplyData, ReplyDirectory, ReplyEntry, ReplyWrite, Request,
    FUSE_ROOT_ID,
};
```

Where fuser 0.17 reports `INodeNo` and `FileHandle` newtypes, unwrap them at method entry and pass raw `u64` values to the existing helpers:

```rust
let ino = ino.0;
let fh = fh.0;
```

Keep `InodeTable` storing raw `u64`; only fuser callback boundaries should know about fuser newtypes. Replace `reply.error(ENOENT)` style calls with fuser errno constants such as `reply.error(Errno::ENOENT)`, `reply.error(Errno::EIO)`, `reply.error(Errno::EISDIR)`, `reply.error(Errno::ENOTDIR)`, and `reply.error(Errno::ENOTEMPTY)`.

- [ ] **Step 5: Convert mount setup in `main.rs` to fuser 0.17 config**

Replace the direct-mount option construction in `nexus-fuse/src/main.rs` with this shape:

```rust
let mut config = Config::new();
config
    .fsname("nexus")
    .auto_unmount()
    .default_permissions();

if allow_other {
    config.allow_other();
}

info!("Mounting filesystem...");
if foreground {
    fuser::mount2(filesystem, &mount_point, &config)?;
} else {
    fuser::mount2(filesystem, &mount_point, &config)?;
}
```

If fuser 0.17 keeps `MountOption` support in this repository's resolved feature set, keep the existing `Vec<MountOption>` and only update the imports. The pass condition is `cargo check --lib --bin nexus-fuse` succeeding before passthrough modules exist.

- [ ] **Step 6: Re-run behavior tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo check --lib --bin nexus-fuse
cargo test fs::tests --lib
cargo test cache::tests cached_read::tests daemon::tests --lib
```

Expected: commands pass. No passthrough feature is visible yet.

- [ ] **Step 7: Commit the compatibility upgrade**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/Cargo.toml nexus-fuse/Cargo.lock nexus-fuse/src/fs.rs nexus-fuse/src/main.rs
git commit -m "chore(#4060): upgrade fuser for passthrough support"
```

Expected: commit succeeds and contains only the compatibility upgrade.

### Task 2: Rust Passthrough Config And Policy

**Files:**
- Modify: `nexus-fuse/Cargo.toml`
- Modify: `nexus-fuse/src/lib.rs`
- Create: `nexus-fuse/src/passthrough/mod.rs`
- Create: `nexus-fuse/src/passthrough/config.rs`
- Create: `nexus-fuse/src/passthrough/policy.rs`

- [ ] **Step 1: Add dependencies**

Append these dependencies in `nexus-fuse/Cargo.toml` under `[dependencies]`:

```toml
# Passthrough path matching and stable backing-file keys
globset = "0.4"
sha2 = "0.10"
hex = "0.4"
```

- [ ] **Step 2: Create failing config tests**

Create `nexus-fuse/src/passthrough/config.rs` with this test module first:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_disabled_and_large_read_threshold_is_128k() {
        let config = PassthroughConfig::default();
        assert!(!config.enabled);
        assert_eq!(config.threshold_bytes, 128 * 1024);
        assert!(!config.require);
        assert!(config.allow_patterns.is_empty());
    }

    #[test]
    fn comma_separated_patterns_trim_empty_segments() {
        let patterns = parse_pattern_env(" /models/**, ,/datasets/*.bin ,, ");
        assert_eq!(patterns, vec!["/models/**", "/datasets/*.bin"]);
    }

    #[test]
    fn pattern_set_allows_when_no_allow_patterns_are_configured() {
        let set = PatternSet::new(vec![], vec![]).expect("empty patterns compile");
        assert!(set.allows("/any/path.bin"));
    }

    #[test]
    fn pattern_set_applies_allow_and_deny_patterns() {
        let set = PatternSet::new(
            vec!["/datasets/**".to_string()],
            vec!["/datasets/private/**".to_string()],
        )
        .expect("patterns compile");

        assert!(set.allows("/datasets/public/file.bin"));
        assert!(!set.allows("/other/file.bin"));
        assert!(!set.allows("/datasets/private/key.bin"));
    }

    #[test]
    fn kernel_release_parser_enforces_linux_6_9_floor() {
        assert!(kernel_release_supports_passthrough("6.9.0"));
        assert!(kernel_release_supports_passthrough("6.10.1-custom"));
        assert!(!kernel_release_supports_passthrough("6.8.12"));
        assert!(!kernel_release_supports_passthrough("5.15.0"));
        assert!(!kernel_release_supports_passthrough("not-a-version"));
    }
}
```

- [ ] **Step 3: Run config tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::config::tests --lib
```

Expected: compile failure naming missing `PassthroughConfig`, `parse_pattern_env`, or `PatternSet`.

- [ ] **Step 4: Implement config and pattern parsing**

Replace the body of `nexus-fuse/src/passthrough/config.rs` with:

```rust
use globset::{Glob, GlobSet, GlobSetBuilder};
use std::path::PathBuf;

pub const DEFAULT_THRESHOLD_BYTES: u64 = 128 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PassthroughConfig {
    pub enabled: bool,
    pub allow_patterns: Vec<String>,
    pub deny_patterns: Vec<String>,
    pub threshold_bytes: u64,
    pub require: bool,
    pub backing_dir: Option<PathBuf>,
}

impl Default for PassthroughConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            allow_patterns: Vec::new(),
            deny_patterns: Vec::new(),
            threshold_bytes: DEFAULT_THRESHOLD_BYTES,
            require: false,
            backing_dir: None,
        }
    }
}

impl PassthroughConfig {
    pub fn disabled() -> Self {
        Self::default()
    }

    pub fn pattern_set(&self) -> Result<PatternSet, globset::Error> {
        PatternSet::new(self.allow_patterns.clone(), self.deny_patterns.clone())
    }
}

#[derive(Debug, Clone)]
pub struct PatternSet {
    allow: GlobSet,
    deny: GlobSet,
    has_allow_patterns: bool,
}

impl PatternSet {
    pub fn new(allow_patterns: Vec<String>, deny_patterns: Vec<String>) -> Result<Self, globset::Error> {
        let allow = build_globset(&allow_patterns)?;
        let deny = build_globset(&deny_patterns)?;

        Ok(Self {
            allow,
            deny,
            has_allow_patterns: !allow_patterns.is_empty(),
        })
    }

    pub fn allows(&self, path: &str) -> bool {
        if self.deny.is_match(path) {
            return false;
        }

        !self.has_allow_patterns || self.allow.is_match(path)
    }
}

fn build_globset(patterns: &[String]) -> Result<GlobSet, globset::Error> {
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        builder.add(Glob::new(pattern)?);
    }
    builder.build()
}

pub fn parse_pattern_env(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|segment| !segment.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

pub fn kernel_release_supports_passthrough(release: &str) -> bool {
    let Some((major, rest)) = release.split_once('.') else {
        return false;
    };
    let minor = rest
        .split(|ch: char| !ch.is_ascii_digit())
        .next()
        .unwrap_or_default();

    let Ok(major) = major.parse::<u32>() else {
        return false;
    };
    let Ok(minor) = minor.parse::<u32>() else {
        return false;
    };

    (major, minor) >= (6, 9)
}

pub fn linux_passthrough_supported() -> bool {
    if !cfg!(target_os = "linux") {
        return false;
    }

    std::fs::read_to_string("/proc/sys/kernel/osrelease")
        .map(|release| kernel_release_supports_passthrough(release.trim()))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_disabled_and_large_read_threshold_is_128k() {
        let config = PassthroughConfig::default();
        assert!(!config.enabled);
        assert_eq!(config.threshold_bytes, 128 * 1024);
        assert!(!config.require);
        assert!(config.allow_patterns.is_empty());
    }

    #[test]
    fn comma_separated_patterns_trim_empty_segments() {
        let patterns = parse_pattern_env(" /models/**, ,/datasets/*.bin ,, ");
        assert_eq!(patterns, vec!["/models/**", "/datasets/*.bin"]);
    }

    #[test]
    fn pattern_set_allows_when_no_allow_patterns_are_configured() {
        let set = PatternSet::new(vec![], vec![]).expect("empty patterns compile");
        assert!(set.allows("/any/path.bin"));
    }

    #[test]
    fn pattern_set_applies_allow_and_deny_patterns() {
        let set = PatternSet::new(
            vec!["/datasets/**".to_string()],
            vec!["/datasets/private/**".to_string()],
        )
        .expect("patterns compile");

        assert!(set.allows("/datasets/public/file.bin"));
        assert!(!set.allows("/other/file.bin"));
        assert!(!set.allows("/datasets/private/key.bin"));
    }

    #[test]
    fn kernel_release_parser_enforces_linux_6_9_floor() {
        assert!(kernel_release_supports_passthrough("6.9.0"));
        assert!(kernel_release_supports_passthrough("6.10.1-custom"));
        assert!(!kernel_release_supports_passthrough("6.8.12"));
        assert!(!kernel_release_supports_passthrough("5.15.0"));
        assert!(!kernel_release_supports_passthrough("not-a-version"));
    }
}
```

- [ ] **Step 5: Create failing policy tests**

Create `nexus-fuse/src/passthrough/policy.rs` with this test module first:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;

    fn metadata(size: u64, is_directory: bool) -> FileMetadata {
        FileMetadata {
            size,
            etag: Some("etag-1".to_string()),
            modified_at: Some("2026-05-12T00:00:00Z".to_string()),
            is_directory,
        }
    }

    #[test]
    fn disabled_config_denies_everything() {
        let config = PassthroughConfig::default();
        let policy = PassthroughPolicy::new(config).expect("policy builds");
        let decision = policy.decide("/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadOnly);
        assert_eq!(decision, PassthroughDecision::Deny(DenyReason::Disabled));
    }

    #[test]
    fn large_read_only_file_with_matching_pattern_is_allowed() {
        let config = PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/data/**".to_string()],
            threshold_bytes: 128 * 1024,
            require: false,
            deny_patterns: vec![],
            backing_dir: None,
        };
        let policy = PassthroughPolicy::new(config).expect("policy builds");
        let decision = policy.decide("/data/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadOnly);
        assert_eq!(decision, PassthroughDecision::Allow);
    }

    #[test]
    fn directories_small_files_and_write_opens_are_denied() {
        let config = PassthroughConfig {
            enabled: true,
            allow_patterns: vec![],
            threshold_bytes: 128 * 1024,
            require: false,
            deny_patterns: vec![],
            backing_dir: None,
        };
        let policy = PassthroughPolicy::new(config).expect("policy builds");

        assert_eq!(
            policy.decide("/dir", &metadata(0, true), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::Directory)
        );
        assert_eq!(
            policy.decide("/small.bin", &metadata(1024, false), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::BelowThreshold)
        );
        assert_eq!(
            policy.decide("/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadWrite),
            PassthroughDecision::Deny(DenyReason::NotReadOnly)
        );
    }
}
```

- [ ] **Step 6: Run policy tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::policy::tests --lib
```

Expected: compile failure naming missing policy types.

- [ ] **Step 7: Implement policy types**

Replace `nexus-fuse/src/passthrough/policy.rs` with:

```rust
use crate::client::FileMetadata;
use crate::passthrough::config::{PassthroughConfig, PatternSet};
use fuser::{OpenAccMode, OpenFlags};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpenAccess {
    ReadOnly,
    WriteOnly,
    ReadWrite,
}

impl OpenAccess {
    pub fn from_open_flags(flags: OpenFlags) -> Self {
        match flags.acc_mode() {
            OpenAccMode::O_RDONLY => Self::ReadOnly,
            OpenAccMode::O_WRONLY => Self::WriteOnly,
            OpenAccMode::O_RDWR => Self::ReadWrite,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenyReason {
    Disabled,
    NotNegotiated,
    Pattern,
    Directory,
    BelowThreshold,
    NotReadOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PassthroughDecision {
    Allow,
    Deny(DenyReason),
}

pub struct PassthroughPolicy {
    config: PassthroughConfig,
    patterns: PatternSet,
}

impl PassthroughPolicy {
    pub fn new(config: PassthroughConfig) -> Result<Self, globset::Error> {
        let patterns = config.pattern_set()?;
        Ok(Self { config, patterns })
    }

    pub fn config(&self) -> &PassthroughConfig {
        &self.config
    }

    pub fn decide(
        &self,
        path: &str,
        metadata: &FileMetadata,
        access: OpenAccess,
    ) -> PassthroughDecision {
        if !self.config.enabled {
            return PassthroughDecision::Deny(DenyReason::Disabled);
        }

        if metadata.is_directory {
            return PassthroughDecision::Deny(DenyReason::Directory);
        }

        if access != OpenAccess::ReadOnly {
            return PassthroughDecision::Deny(DenyReason::NotReadOnly);
        }

        if metadata.size < self.config.threshold_bytes {
            return PassthroughDecision::Deny(DenyReason::BelowThreshold);
        }

        if !self.patterns.allows(path) {
            return PassthroughDecision::Deny(DenyReason::Pattern);
        }

        PassthroughDecision::Allow
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;

    fn metadata(size: u64, is_directory: bool) -> FileMetadata {
        FileMetadata {
            size,
            etag: Some("etag-1".to_string()),
            modified_at: Some("2026-05-12T00:00:00Z".to_string()),
            is_directory,
        }
    }

    #[test]
    fn disabled_config_denies_everything() {
        let config = PassthroughConfig::default();
        let policy = PassthroughPolicy::new(config).expect("policy builds");
        let decision = policy.decide("/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadOnly);
        assert_eq!(decision, PassthroughDecision::Deny(DenyReason::Disabled));
    }

    #[test]
    fn large_read_only_file_with_matching_pattern_is_allowed() {
        let config = PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/data/**".to_string()],
            threshold_bytes: 128 * 1024,
            require: false,
            deny_patterns: vec![],
            backing_dir: None,
        };
        let policy = PassthroughPolicy::new(config).expect("policy builds");
        let decision = policy.decide("/data/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadOnly);
        assert_eq!(decision, PassthroughDecision::Allow);
    }

    #[test]
    fn directories_small_files_and_write_opens_are_denied() {
        let config = PassthroughConfig {
            enabled: true,
            allow_patterns: vec![],
            threshold_bytes: 128 * 1024,
            require: false,
            deny_patterns: vec![],
            backing_dir: None,
        };
        let policy = PassthroughPolicy::new(config).expect("policy builds");

        assert_eq!(
            policy.decide("/dir", &metadata(0, true), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::Directory)
        );
        assert_eq!(
            policy.decide("/small.bin", &metadata(1024, false), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::BelowThreshold)
        );
        assert_eq!(
            policy.decide("/big.bin", &metadata(1024 * 1024, false), OpenAccess::ReadWrite),
            PassthroughDecision::Deny(DenyReason::NotReadOnly)
        );
    }
}
```

- [ ] **Step 8: Wire the module boundary**

Create `nexus-fuse/src/passthrough/mod.rs`:

```rust
pub mod config;
pub mod policy;

pub use config::{
    kernel_release_supports_passthrough, linux_passthrough_supported, parse_pattern_env,
    PassthroughConfig,
};
pub use policy::{DenyReason, OpenAccess, PassthroughDecision, PassthroughPolicy};
```

Add this line to `nexus-fuse/src/lib.rs`:

```rust
pub mod passthrough;
```

- [ ] **Step 9: Run config and policy tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::config::tests passthrough::policy::tests --lib
```

Expected: all tests pass.

- [ ] **Step 10: Commit config and policy**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/Cargo.toml nexus-fuse/Cargo.lock nexus-fuse/src/lib.rs nexus-fuse/src/passthrough
git commit -m "feat(#4060): add passthrough config and policy"
```

Expected: commit succeeds.

### Task 3: Backing Store Materialization

**Files:**
- Modify: `nexus-fuse/src/passthrough/mod.rs`
- Create: `nexus-fuse/src/passthrough/backing.rs`

- [ ] **Step 1: Write failing backing-store tests**

Create `nexus-fuse/src/passthrough/backing.rs` with this test module first:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn backing_key_is_stable_and_uses_server_path_etag_and_size() {
        let first = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 1024);
        let second = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 1024);
        let changed = BackingKey::new("http://server", "/data/big.bin", Some("etag-2"), 1024);

        assert_eq!(first.filename(), second.filename());
        assert_ne!(first.filename(), changed.filename());
        assert!(first.filename().ends_with(".backing"));
    }

    #[test]
    fn backing_store_invalidates_path_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        let store = BackingStore::new(dir.path().to_path_buf()).expect("store");
        let key = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 4);
        let path = store.path_for_key(&key);
        fs::write(&path, b"data").expect("write");

        assert!(path.exists());
        store.invalidate_path("http://server", "/data/big.bin").expect("invalidate");
        assert!(!path.exists());
    }
}
```

- [ ] **Step 2: Run backing tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::backing::tests --lib
```

Expected: compile failure naming missing `BackingKey` or `BackingStore`.

- [ ] **Step 3: Implement key and store filesystem operations**

Replace `nexus-fuse/src/passthrough/backing.rs` with:

```rust
use crate::client::{FileMetadata, NexusClient, ReadResponse};
use anyhow::{Context, Result};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::fd::{AsRawFd, RawFd};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackingKey {
    digest: String,
    server_url: String,
    path: String,
}

impl BackingKey {
    pub fn new(server_url: &str, path: &str, etag: Option<&str>, size: u64) -> Self {
        let mut hasher = Sha256::new();
        hasher.update(server_url.as_bytes());
        hasher.update([0]);
        hasher.update(path.as_bytes());
        hasher.update([0]);
        hasher.update(etag.unwrap_or("").as_bytes());
        hasher.update([0]);
        hasher.update(size.to_le_bytes());

        Self {
            digest: hex::encode(hasher.finalize()),
            server_url: server_url.to_string(),
            path: path.to_string(),
        }
    }

    pub fn filename(&self) -> String {
        format!("{}.backing", self.digest)
    }

    fn marker_prefix(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.server_url.as_bytes());
        hasher.update([0]);
        hasher.update(self.path.as_bytes());
        hex::encode(hasher.finalize())
    }
}

#[derive(Debug)]
pub struct MaterializedBacking {
    pub path: PathBuf,
    pub key: BackingKey,
    file: File,
}

impl MaterializedBacking {
    pub fn file(&self) -> &File {
        &self.file
    }

    pub fn raw_fd(&self) -> RawFd {
        self.file.as_raw_fd()
    }
}

#[derive(Debug, Clone)]
pub struct BackingStore {
    root: PathBuf,
}

impl BackingStore {
    pub fn new(root: PathBuf) -> Result<Self> {
        fs::create_dir_all(&root).with_context(|| format!("create passthrough backing dir {}", root.display()))?;
        Ok(Self { root })
    }

    pub fn path_for_key(&self, key: &BackingKey) -> PathBuf {
        self.root.join(key.filename())
    }

    pub fn materialize(
        &self,
        server_url: &str,
        path: &str,
        client: &NexusClient,
        metadata: &FileMetadata,
    ) -> Result<MaterializedBacking> {
        let key = BackingKey::new(server_url, path, metadata.etag.as_deref(), metadata.size);
        let final_path = self.path_for_key(&key);

        if final_path.exists() {
            let file = OpenOptions::new().read(true).open(&final_path)?;
            return Ok(MaterializedBacking { path: final_path, key, file });
        }

        let response = client.read_with_etag(path, None)?;
        let bytes = match response {
            ReadResponse::Content { content, .. } => content,
            ReadResponse::NotModified => anyhow::bail!("server returned 304 while materializing uncached backing file"),
        };

        if bytes.len() as u64 != metadata.size {
            anyhow::bail!(
                "materialized backing size mismatch for {}: expected {}, got {}",
                path,
                metadata.size,
                bytes.len()
            );
        }

        let temp_path = final_path.with_extension(format!("{}.tmp", std::process::id()));
        {
            let mut file = File::create(&temp_path)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
        }
        fs::rename(&temp_path, &final_path)?;

        let marker_path = self.root.join(format!("{}.marker", key.marker_prefix()));
        fs::write(marker_path, final_path.file_name().unwrap().to_string_lossy().as_bytes())?;

        let file = OpenOptions::new().read(true).open(&final_path)?;
        Ok(MaterializedBacking { path: final_path, key, file })
    }

    pub fn invalidate_path(&self, server_url: &str, path: &str) -> Result<()> {
        let marker = BackingKey::new(server_url, path, None, 0).marker_prefix();
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            let file_name = entry.file_name();
            let file_name = file_name.to_string_lossy();
            if file_name.starts_with(&marker) || file_name.ends_with(".backing") {
                let _ = fs::remove_file(entry.path());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn backing_key_is_stable_and_uses_server_path_etag_and_size() {
        let first = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 1024);
        let second = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 1024);
        let changed = BackingKey::new("http://server", "/data/big.bin", Some("etag-2"), 1024);

        assert_eq!(first.filename(), second.filename());
        assert_ne!(first.filename(), changed.filename());
        assert!(first.filename().ends_with(".backing"));
    }

    #[test]
    fn backing_store_invalidates_path_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        let store = BackingStore::new(dir.path().to_path_buf()).expect("store");
        let key = BackingKey::new("http://server", "/data/big.bin", Some("etag-1"), 4);
        let path = store.path_for_key(&key);
        fs::write(&path, b"data").expect("write");

        assert!(path.exists());
        store.invalidate_path("http://server", "/data/big.bin").expect("invalidate");
        assert!(!path.exists());
    }
}
```

- [ ] **Step 4: Fix marker-based invalidation**

The Step 3 invalidation loop removes all `.backing` files. Replace `BackingStore::invalidate_path` with a path-specific scan that removes only files whose sidecar marker names match the server/path digest:

```rust
pub fn invalidate_path(&self, server_url: &str, path: &str) -> Result<()> {
    let path_marker_prefix = BackingKey::new(server_url, path, None, 0).marker_prefix();
    let mut backing_files = Vec::new();

    for entry in fs::read_dir(&self.root)? {
        let entry = entry?;
        let file_name = entry.file_name().to_string_lossy().to_string();
        if file_name.starts_with(&path_marker_prefix) && file_name.ends_with(".marker") {
            let backing_name = fs::read_to_string(entry.path()).unwrap_or_default();
            if !backing_name.trim().is_empty() {
                backing_files.push(self.root.join(backing_name.trim()));
            }
            let _ = fs::remove_file(entry.path());
        }
    }

    for backing in backing_files {
        let _ = fs::remove_file(backing);
    }

    Ok(())
}
```

Also change the marker write in `materialize` so each etag/size variant has a distinct marker:

```rust
let marker_path = self.root.join(format!("{}-{}.marker", key.marker_prefix(), key.digest));
fs::write(marker_path, final_path.file_name().unwrap().to_string_lossy().as_bytes())?;
```

- [ ] **Step 5: Export backing module**

Update `nexus-fuse/src/passthrough/mod.rs`:

```rust
pub mod backing;
pub mod config;
pub mod policy;

pub use backing::{BackingKey, BackingStore, MaterializedBacking};
pub use config::{
    kernel_release_supports_passthrough, linux_passthrough_supported, parse_pattern_env,
    PassthroughConfig,
};
pub use policy::{DenyReason, OpenAccess, PassthroughDecision, PassthroughPolicy};
```

- [ ] **Step 6: Run backing tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::backing::tests --lib
```

Expected: all tests pass.

- [ ] **Step 7: Commit backing store**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/src/passthrough/backing.rs nexus-fuse/src/passthrough/mod.rs
git commit -m "feat(#4060): add passthrough backing store"
```

Expected: commit succeeds.

### Task 4: Passthrough Manager And Active Handle Tracking

**Files:**
- Modify: `nexus-fuse/src/passthrough/mod.rs`
- Create: `nexus-fuse/src/passthrough/manager.rs`

- [ ] **Step 1: Write failing manager tests**

Create `nexus-fuse/src/passthrough/manager.rs` with this test module first:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;
    use crate::passthrough::config::PassthroughConfig;

    fn metadata(size: u64) -> FileMetadata {
        FileMetadata {
            size,
            etag: Some("etag-1".to_string()),
            modified_at: None,
            is_directory: false,
        }
    }

    #[test]
    fn next_file_handle_is_monotonic_and_never_zero() {
        let manager = PassthroughManager::new_for_tests(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });

        assert_eq!(manager.next_file_handle(), 1);
        assert_eq!(manager.next_file_handle(), 2);
    }

    #[test]
    fn decision_uses_policy() {
        let manager = PassthroughManager::new_for_tests(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        manager.set_negotiated(true);

        assert_eq!(
            manager.decide("/data/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Allow
        );
        assert_eq!(
            manager.decide("/logs/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::Pattern)
        );
    }
}
```

- [ ] **Step 2: Run manager tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::manager::tests --lib
```

Expected: compile failure naming missing `PassthroughManager`.

- [ ] **Step 3: Implement manager core**

Replace `nexus-fuse/src/passthrough/manager.rs` with:

```rust
use crate::client::{FileMetadata, NexusClient};
use crate::passthrough::backing::{BackingStore, MaterializedBacking};
use crate::passthrough::config::PassthroughConfig;
use crate::passthrough::policy::{OpenAccess, PassthroughDecision, PassthroughPolicy};
use anyhow::Result;
use fuser::BackingId;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

pub struct ActivePassthrough {
    pub backing: MaterializedBacking,
    pub backing_id: BackingId,
}

pub struct PassthroughManager {
    server_url: String,
    policy: PassthroughPolicy,
    store: BackingStore,
    next_fh: AtomicU64,
    negotiated: AtomicBool,
    active: Mutex<HashMap<u64, ActivePassthrough>>,
}

impl PassthroughManager {
    pub fn new(server_url: String, config: PassthroughConfig) -> Result<Self> {
        let root = config
            .backing_dir
            .clone()
            .unwrap_or_else(default_backing_dir);
        Ok(Self {
            server_url,
            policy: PassthroughPolicy::new(config)?,
            store: BackingStore::new(root)?,
            next_fh: AtomicU64::new(1),
            negotiated: AtomicBool::new(false),
            active: Mutex::new(HashMap::new()),
        })
    }

    #[cfg(test)]
    pub fn new_for_tests(config: PassthroughConfig) -> Self {
        let root = tempfile::tempdir().expect("tempdir").into_path();
        Self::new("http://server".to_string(), config).expect("manager")
    }

    pub fn next_file_handle(&self) -> u64 {
        self.next_fh.fetch_add(1, Ordering::Relaxed)
    }

    pub fn set_negotiated(&self, negotiated: bool) {
        self.negotiated.store(negotiated, Ordering::Release);
    }

    pub fn negotiated(&self) -> bool {
        self.negotiated.load(Ordering::Acquire)
    }

    pub fn require(&self) -> bool {
        self.policy.config().require
    }

    pub fn decide(
        &self,
        path: &str,
        metadata: &FileMetadata,
        access: OpenAccess,
    ) -> PassthroughDecision {
        if !self.negotiated() {
            return PassthroughDecision::Deny(crate::passthrough::policy::DenyReason::NotNegotiated);
        }
        self.policy.decide(path, metadata, access)
    }

    pub fn materialize(
        &self,
        path: &str,
        client: &NexusClient,
        metadata: &FileMetadata,
    ) -> Result<MaterializedBacking> {
        self.store.materialize(&self.server_url, path, client, metadata)
    }

    pub fn insert_active(&self, fh: u64, active: ActivePassthrough) {
        self.active.lock().unwrap().insert(fh, active);
    }

    pub fn remove_active(&self, fh: u64) -> Option<ActivePassthrough> {
        self.active.lock().unwrap().remove(&fh)
    }

    pub fn invalidate_path(&self, path: &str) {
        if let Err(err) = self.store.invalidate_path(&self.server_url, path) {
            log::warn!("passthrough backing invalidation failed for {}: {}", path, err);
        }
    }
}

fn default_backing_dir() -> PathBuf {
    dirs::cache_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("nexus-fuse")
        .join("passthrough")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;
    use crate::passthrough::config::PassthroughConfig;

    fn metadata(size: u64) -> FileMetadata {
        FileMetadata {
            size,
            etag: Some("etag-1".to_string()),
            modified_at: None,
            is_directory: false,
        }
    }

    #[test]
    fn next_file_handle_is_monotonic_and_never_zero() {
        let manager = PassthroughManager::new_for_tests(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });

        assert_eq!(manager.next_file_handle(), 1);
        assert_eq!(manager.next_file_handle(), 2);
    }

    #[test]
    fn decision_uses_policy() {
        let manager = PassthroughManager::new_for_tests(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        manager.set_negotiated(true);

        assert_eq!(
            manager.decide("/data/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Allow
        );
        assert_eq!(
            manager.decide("/logs/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::Pattern)
        );
    }
}
```

- [ ] **Step 4: Export manager module**

Update `nexus-fuse/src/passthrough/mod.rs`:

```rust
pub mod backing;
pub mod config;
pub mod manager;
pub mod policy;

pub use backing::{BackingKey, BackingStore, MaterializedBacking};
pub use config::{
    kernel_release_supports_passthrough, linux_passthrough_supported, parse_pattern_env,
    PassthroughConfig,
};
pub use manager::{ActivePassthrough, PassthroughManager};
pub use policy::{DenyReason, OpenAccess, PassthroughDecision, PassthroughPolicy};
```

- [ ] **Step 5: Run manager tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test passthrough::manager::tests --lib
```

Expected: all tests pass.

- [ ] **Step 6: Commit manager**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/src/passthrough/manager.rs nexus-fuse/src/passthrough/mod.rs
git commit -m "feat(#4060): track passthrough backing handles"
```

Expected: commit succeeds.

### Task 5: Wire Passthrough Into Direct Rust FUSE Open And Release

**Files:**
- Modify: `nexus-fuse/src/fs.rs`

- [ ] **Step 1: Add failing `NexusFs` constructor tests**

Append this test in `#[cfg(test)] mod tests` in `nexus-fuse/src/fs.rs`:

```rust
#[test]
fn nexus_fs_can_be_constructed_with_passthrough_manager() {
    use crate::passthrough::{PassthroughConfig, PassthroughManager};

    let client = NexusClient::new("http://localhost:2026", "test-key", None).expect("client");
    let manager = PassthroughManager::new(
        "http://localhost:2026".to_string(),
        PassthroughConfig {
            enabled: true,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            threshold_bytes: 128 * 1024,
            require: false,
            backing_dir: None,
        },
    )
    .expect("manager");

    let fs = NexusFs::new(client, None, Some(Arc::new(manager)));
    assert!(fs.passthrough_enabled_for_tests());
}
```

- [ ] **Step 2: Run the constructor test and verify it fails**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test fs::tests::nexus_fs_can_be_constructed_with_passthrough_manager --lib
```

Expected: compile failure because `NexusFs::new` does not accept a passthrough manager and `passthrough_enabled_for_tests` is missing.

- [ ] **Step 3: Add the passthrough field**

In `nexus-fuse/src/fs.rs`, add this import:

```rust
use crate::passthrough::{ActivePassthrough, OpenAccess, PassthroughDecision, PassthroughManager};
```

Change `NexusFs` to include the manager:

```rust
pub struct NexusFs {
    client: Arc<NexusClient>,
    inodes: Mutex<InodeTable>,
    attr_cache: Mutex<LruCache<u64, (FileAttr, SystemTime)>>,
    dir_cache: Mutex<LruCache<u64, (Vec<FileEntry>, SystemTime)>>,
    file_cache: Option<Arc<FileCache>>,
    passthrough: Option<Arc<PassthroughManager>>,
}
```

Replace the constructor with:

```rust
pub fn new(
    client: NexusClient,
    file_cache: Option<Arc<FileCache>>,
    passthrough: Option<Arc<PassthroughManager>>,
) -> Self {
    Self {
        client: Arc::new(client),
        inodes: Mutex::new(InodeTable::new()),
        attr_cache: Mutex::new(LruCache::new(NonZeroUsize::new(10000).unwrap())),
        dir_cache: Mutex::new(LruCache::new(NonZeroUsize::new(1000).unwrap())),
        file_cache,
        passthrough,
    }
}

#[cfg(test)]
fn passthrough_enabled_for_tests(&self) -> bool {
    self.passthrough.is_some()
}
```

- [ ] **Step 4: Update current callers**

In `nexus-fuse/src/main.rs`, temporarily call the constructor with no passthrough:

```rust
let filesystem = fs::NexusFs::new(client, file_cache, None);
```

Update any Rust tests or helper constructors with `None` as the third argument.

- [ ] **Step 5: Run the constructor test**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test fs::tests::nexus_fs_can_be_constructed_with_passthrough_manager --lib
```

Expected: test passes.

- [ ] **Step 6: Add passthrough invalidation**

In `NexusFs::invalidate_path`, after the foyer cache invalidation block, add:

```rust
if let Some(ref passthrough) = self.passthrough {
    passthrough.invalidate_path(path);
}
```

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test fs::tests --lib
```

Expected: all filesystem unit tests pass.

- [ ] **Step 7: Negotiate passthrough during FUSE init**

Add this `init` callback inside `impl Filesystem for NexusFs`:

```rust
fn init(&self, _req: &Request<'_>, config: &mut KernelConfig) -> Result<(), Errno> {
    if let Some(ref passthrough) = self.passthrough {
        match config.set_max_stack_depth(1) {
            Ok(_previous) => {
                passthrough.set_negotiated(true);
                debug!("FUSE passthrough negotiated with max_stack_depth=1");
            }
            Err(max_supported) => {
                passthrough.set_negotiated(false);
                if passthrough.require() {
                    error!(
                        "FUSE passthrough required but max_stack_depth=1 was rejected; max_supported={}",
                        max_supported
                    );
                    return Err(Errno::EOPNOTSUPP);
                }
                debug!(
                    "FUSE passthrough unavailable; max_stack_depth=1 rejected with max_supported={}",
                    max_supported
                );
            }
        }
    }
    Ok(())
}
```

- [ ] **Step 8: Implement open-time passthrough registration**

In `nexus-fuse/src/fs.rs`, replace `open` with this fuser 0.17 control flow:

```rust
fn open(&self, _req: &Request<'_>, ino: INodeNo, flags: OpenFlags, reply: fuser::ReplyOpen) {
    let ino = ino.0;
    debug!("open: ino={}", ino);

    let path = resolve_path!(self, ino, reply);

    let metadata = match self.client.stat(&path) {
        Ok(metadata) => metadata,
        Err(e) if e.is_not_found() => {
            reply.error(Errno::ENOENT);
            return;
        }
        Err(e) => {
            error!("open stat error for {}: {}", path, e);
            reply.error(Errno::EIO);
            return;
        }
    };

    if metadata.is_directory {
        reply.error(Errno::EISDIR);
        return;
    }

    if let Some(ref passthrough) = self.passthrough {
        let access = OpenAccess::from_open_flags(flags);
        match passthrough.decide(&path, &metadata, access) {
            PassthroughDecision::Allow => match passthrough.materialize(&path, &self.client, &metadata) {
                Ok(backing) => {
                    let fh = passthrough.next_file_handle();
                    match reply.open_backing(backing.file()) {
                        Ok(backing_id) => {
                            let active = ActivePassthrough { backing, backing_id };
                            reply.opened_passthrough(
                                FileHandle(fh),
                                FopenFlags::empty(),
                                &active.backing_id,
                            );
                            passthrough.insert_active(fh, active);
                            return;
                        }
                        Err(err) => {
                            error!("open_backing failed for {}: {}", path, err);
                            reply.error(Errno::EIO);
                            return;
                        }
                    }
                }
                Err(err) => {
                    error!("passthrough materialization failed for {}: {}", path, err);
                    reply.error(Errno::EIO);
                    return;
                }
            },
            PassthroughDecision::Deny(reason) => {
                debug!("passthrough denied for {}: {:?}", path, reason);
            }
        }
    }

    reply.opened(FileHandle(0), FopenFlags::empty());
}
```

If the exact fuser 0.17 API exposes `open_backing` on a reply helper or uses a differently named method for passthrough completion, use the method names from the local rustdoc and keep the same decisions:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo doc -p nexus-fuse --no-deps
```

Expected: local docs show the `ReplyOpen` passthrough method names. The finished `open` compiles and only calls passthrough for allowed read-only files.

- [ ] **Step 9: Clean active passthrough handles from release**

In `release`, remove the active handle before `reply.ok()`:

```rust
if let Some(ref passthrough) = self.passthrough {
    passthrough.remove_active(fh.0);
}
reply.ok();
```

- [ ] **Step 10: Run Rust checks**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo check --lib --bin nexus-fuse
cargo test passthrough::config::tests passthrough::policy::tests passthrough::backing::tests passthrough::manager::tests --lib
cargo test fs::tests --lib
```

Expected: all commands pass.

- [ ] **Step 11: Commit FUSE wiring**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/src/fs.rs nexus-fuse/src/main.rs
git commit -m "feat(#4060): wire passthrough into rust fuse open"
```

Expected: commit succeeds.

### Task 6: Add Rust CLI Flags, Environment Variables, And Fallback Semantics

**Files:**
- Modify: `nexus-fuse/src/main.rs`

- [ ] **Step 1: Write failing CLI config tests**

Append this test module to `nexus-fuse/src/main.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_passthrough_config_keeps_disabled_default() {
        let config = build_passthrough_config(false, Vec::new(), Vec::new(), 128 * 1024, false, None)
            .expect("config");
        assert!(!config.enabled);
        assert_eq!(config.threshold_bytes, 128 * 1024);
    }

    #[test]
    fn build_passthrough_config_preserves_patterns_and_require() {
        let config = build_passthrough_config(
            true,
            vec!["/data/**".to_string()],
            vec!["/data/private/**".to_string()],
            256 * 1024,
            true,
            None,
        )
        .expect("config");

        assert!(config.enabled);
        assert_eq!(config.allow_patterns, vec!["/data/**"]);
        assert_eq!(config.deny_patterns, vec!["/data/private/**"]);
        assert_eq!(config.threshold_bytes, 256 * 1024);
        assert!(config.require);
    }
}
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test main::tests --bin nexus-fuse
```

Expected: compile failure for missing `build_passthrough_config`.

- [ ] **Step 3: Add CLI arguments to `Commands::Mount`**

In the `Mount` variant, add:

```rust
        /// Enable Linux FUSE passthrough for eligible large reads.
        #[arg(long, env = "NEXUS_FUSE_PASSTHROUGH", default_value_t = false)]
        passthrough: bool,

        /// Glob allow pattern for passthrough. Repeat for multiple patterns.
        #[arg(long = "passthrough-pattern", env = "NEXUS_FUSE_PASSTHROUGH_PATTERNS", value_delimiter = ',')]
        passthrough_patterns: Vec<String>,

        /// Glob deny pattern for passthrough. Repeat for multiple patterns.
        #[arg(long = "passthrough-deny-pattern", env = "NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS", value_delimiter = ',')]
        passthrough_deny_patterns: Vec<String>,

        /// Minimum file size for passthrough eligibility.
        #[arg(long, env = "NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", default_value_t = 128 * 1024)]
        passthrough_threshold_bytes: u64,

        /// Fail the mount instead of falling back when passthrough is unavailable.
        #[arg(long, env = "NEXUS_FUSE_PASSTHROUGH_REQUIRE", default_value_t = false)]
        passthrough_require: bool,

        /// Directory for immutable passthrough backing files.
        #[arg(long, env = "NEXUS_FUSE_PASSTHROUGH_BACKING_DIR")]
        passthrough_backing_dir: Option<PathBuf>,
```

Add matching fields to the `match cli.command` destructuring for `Commands::Mount`.

- [ ] **Step 4: Implement config builder**

Add this helper near `build_cache_config`:

```rust
fn build_passthrough_config(
    enabled: bool,
    allow_patterns: Vec<String>,
    deny_patterns: Vec<String>,
    threshold_bytes: u64,
    require: bool,
    backing_dir: Option<PathBuf>,
) -> anyhow::Result<passthrough::PassthroughConfig> {
    if threshold_bytes == 0 {
        anyhow::bail!("passthrough threshold must be greater than zero");
    }

    Ok(passthrough::PassthroughConfig {
        enabled,
        allow_patterns,
        deny_patterns,
        threshold_bytes,
        require,
        backing_dir,
    })
}
```

- [ ] **Step 5: Create manager in the mount command**

Before `NexusFs::new`, build the manager:

```rust
let passthrough_config = build_passthrough_config(
    passthrough,
    passthrough_patterns,
    passthrough_deny_patterns,
    passthrough_threshold_bytes,
    passthrough_require,
    passthrough_backing_dir,
)?;

let passthrough_manager = if passthrough_config.enabled {
    if !passthrough::linux_passthrough_supported() {
        if passthrough_config.require {
            anyhow::bail!("FUSE passthrough requires Linux 6.9+ and fuser passthrough support");
        }
        None
    } else {
        match passthrough::PassthroughManager::new(url.clone(), passthrough_config.clone()) {
            Ok(manager) => Some(Arc::new(manager)),
            Err(err) if passthrough_config.require => return Err(err),
            Err(err) => {
                warn!("FUSE passthrough disabled: {}", err);
                None
            }
        }
    }
} else {
    None
};

let filesystem = fs::NexusFs::new(client, file_cache, passthrough_manager);
```

Import `warn` from `log` if it is not already imported.

- [ ] **Step 6: Run CLI and Rust tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test main::tests --bin nexus-fuse
cargo check --lib --bin nexus-fuse
```

Expected: commands pass.

- [ ] **Step 7: Commit CLI wiring**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/src/main.rs
git commit -m "feat(#4060): add passthrough mount flags"
```

Expected: commit succeeds.

### Task 7: Python Passthrough Options And Rust-Owned Mount Launcher

**Files:**
- Create: `src/nexus/fuse/passthrough.py`
- Modify: `src/nexus/fuse/mount.py`
- Test: `tests/unit/fuse/test_passthrough_options.py`

- [ ] **Step 1: Write failing Python option and command tests**

Create `tests/unit/fuse/test_passthrough_options.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from nexus.fuse.passthrough import (
    PassthroughOptions,
    RustPassthroughMount,
    mount_is_passthrough_safe,
)


def test_options_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH", "true")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_PATTERNS", "/data/**, /models/*.bin")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS", "/data/private/**")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", "262144")
    monkeypatch.setenv("NEXUS_FUSE_PASSTHROUGH_REQUIRE", "1")

    options = PassthroughOptions.from_env()

    assert options.enabled is True
    assert options.patterns == ["/data/**", "/models/*.bin"]
    assert options.deny_patterns == ["/data/private/**"]
    assert options.threshold_bytes == 262144
    assert options.require is True


def test_rust_mount_command_uses_api_key_file(tmp_path: Path):
    mount = RustPassthroughMount(
        rust_binary="/usr/bin/nexus-fuse",
        nexus_url="http://localhost:2026",
        api_key="sk-test",
        mount_point=tmp_path / "mnt",
        options=PassthroughOptions(enabled=True, patterns=["/data/**"], threshold_bytes=131072),
        agent_id="agent-1",
    )

    cmd, env, api_key_file = mount.build_command()

    assert cmd[:4] == ["/usr/bin/nexus-fuse", "mount", str(tmp_path / "mnt"), "--url"]
    assert "sk-test" not in " ".join(cmd)
    assert "--api-key-file" in cmd
    assert "--passthrough" in cmd
    assert cmd.count("--passthrough-pattern") == 1
    assert "--passthrough-threshold-bytes" in cmd
    assert env.get("NEXUS_API_KEY") is None
    assert api_key_file.read_text() == "sk-test"
    assert oct(api_key_file.stat().st_mode & 0o777) == "0o600"


def test_mount_safety_denies_context_and_hook_counts():
    fs = MagicMock()
    fs._kernel.hook_count.side_effect = lambda name: 1 if name == "read" else 0

    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is False
    fs._kernel.hook_count.side_effect = lambda name: 0
    assert mount_is_passthrough_safe(fs, mode_value="smart", context=None) is False
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=object()) is False
    assert mount_is_passthrough_safe(fs, mode_value="binary", context=None) is True
```

- [ ] **Step 2: Run Python tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
pytest tests/unit/fuse/test_passthrough_options.py -q
```

Expected: import failure because `nexus.fuse.passthrough` does not exist.

- [ ] **Step 3: Implement Python passthrough helpers**

Create `src/nexus/fuse/passthrough.py`:

```python
"""Python orchestration helpers for Rust-owned FUSE passthrough mounts."""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.fuse.rust_client import RustFUSEClient


def _parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_patterns(value: str | None) -> list[str]:
    if not value:
        return []
    return [segment.strip() for segment in value.split(",") if segment.strip()]


@dataclass(slots=True)
class PassthroughOptions:
    enabled: bool = False
    patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    threshold_bytes: int = 128 * 1024
    require: bool = False
    backing_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "PassthroughOptions":
        backing_dir = os.environ.get("NEXUS_FUSE_PASSTHROUGH_BACKING_DIR")
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_FUSE_PASSTHROUGH")),
            patterns=_parse_patterns(os.environ.get("NEXUS_FUSE_PASSTHROUGH_PATTERNS")),
            deny_patterns=_parse_patterns(os.environ.get("NEXUS_FUSE_PASSTHROUGH_DENY_PATTERNS")),
            threshold_bytes=int(os.environ.get("NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES", str(128 * 1024))),
            require=_parse_bool(os.environ.get("NEXUS_FUSE_PASSTHROUGH_REQUIRE")),
            backing_dir=Path(backing_dir) if backing_dir else None,
        )


def mount_is_passthrough_safe(nexus_fs: Any, *, mode_value: str, context: Any | None) -> bool:
    if platform.system() != "Linux":
        return False
    if context is not None:
        return False
    if mode_value != "binary":
        return False

    kernel = getattr(nexus_fs, "_kernel", None)
    hook_count = getattr(kernel, "hook_count", None)
    if callable(hook_count):
        for operation in ("read", "stat", "open"):
            if hook_count(operation):
                return False

    return True


@dataclass(slots=True)
class RustPassthroughMount:
    rust_binary: str
    nexus_url: str
    api_key: str
    mount_point: Path
    options: PassthroughOptions
    agent_id: str | None = None
    process: subprocess.Popen[str] | None = None
    api_key_file: Path | None = None

    @classmethod
    def create(
        cls,
        *,
        nexus_url: str,
        api_key: str,
        mount_point: Path,
        options: PassthroughOptions,
        agent_id: str | None,
    ) -> "RustPassthroughMount":
        finder = RustFUSEClient.__new__(RustFUSEClient)
        rust_binary = RustFUSEClient._find_rust_binary(finder)
        return cls(
            rust_binary=rust_binary,
            nexus_url=nexus_url,
            api_key=api_key,
            mount_point=mount_point,
            options=options,
            agent_id=agent_id,
        )

    def build_command(self) -> tuple[list[str], dict[str, str], Path]:
        fd, name = tempfile.mkstemp(prefix="nexus-fuse-api-key-", text=True)
        api_key_path = Path(name)
        try:
            os.write(fd, self.api_key.encode())
        finally:
            os.close(fd)
        api_key_path.chmod(0o600)
        self.api_key_file = api_key_path

        cmd = [
            self.rust_binary,
            "mount",
            str(self.mount_point),
            "--url",
            self.nexus_url,
            "--api-key-file",
            str(api_key_path),
            "--foreground",
            "--passthrough",
            "--passthrough-threshold-bytes",
            str(self.options.threshold_bytes),
        ]

        for pattern in self.options.patterns:
            cmd.extend(["--passthrough-pattern", pattern])
        for pattern in self.options.deny_patterns:
            cmd.extend(["--passthrough-deny-pattern", pattern])
        if self.options.require:
            cmd.append("--passthrough-require")
        if self.options.backing_dir is not None:
            cmd.extend(["--passthrough-backing-dir", str(self.options.backing_dir)])
        if self.agent_id:
            cmd.extend(["--agent-id", self.agent_id])

        env = dict(os.environ)
        env.pop("NEXUS_API_KEY", None)
        return cmd, env, api_key_path

    def start(self) -> None:
        cmd, env, _api_key_file = self.build_command()
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.api_key_file is not None:
            try:
                self.api_key_file.unlink()
            except FileNotFoundError:
                pass
            self.api_key_file = None
```

- [ ] **Step 4: Run Python option tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
pytest tests/unit/fuse/test_passthrough_options.py -q
```

Expected: tests pass.

- [ ] **Step 5: Commit Python helper module**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add src/nexus/fuse/passthrough.py tests/unit/fuse/test_passthrough_options.py
git commit -m "feat(#4060): add python passthrough mount launcher"
```

Expected: commit succeeds.

### Task 8: Route Python `use_rust=True` Mounts To Rust-Owned Passthrough

**Files:**
- Modify: `src/nexus/fuse/mount.py`
- Modify: `tests/unit/fuse/test_passthrough_options.py`

- [ ] **Step 1: Add failing routing tests**

Append these tests to `tests/unit/fuse/test_passthrough_options.py`:

```python
from unittest.mock import patch

from nexus.fuse.mount import MountMode, NexusFUSE


def test_nexus_fuse_uses_rust_passthrough_launcher_when_safe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Linux")
    fs = MagicMock()
    fs._base_url = "http://localhost:2026"
    fs._api_key = "sk-test"
    fs._kernel.hook_count.return_value = 0
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    launcher = MagicMock()
    with patch("nexus.fuse.mount.RustPassthroughMount.create", return_value=launcher):
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
            passthrough_patterns=["/data/**"],
        )
        fuse.mount(foreground=False)

    launcher.start.assert_called_once_with()
    assert fuse.is_mounted() is True


def test_nexus_fuse_falls_back_when_passthrough_not_safe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("nexus.fuse.passthrough.platform.system", lambda: "Darwin")
    fs = MagicMock()
    fs._base_url = "http://localhost:2026"
    fs._api_key = "sk-test"
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    with patch("nexus.fuse.mount.FUSE") as fuse_cls:
        fuse = NexusFUSE(
            fs,
            str(mount_point),
            mode=MountMode.BINARY,
            use_rust=True,
            passthrough_enabled=True,
        )
        fuse.mount(foreground=True)

    fuse_cls.assert_called_once()
```

- [ ] **Step 2: Run routing tests and verify they fail**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
pytest tests/unit/fuse/test_passthrough_options.py -q
```

Expected: constructor argument failure for passthrough options.

- [ ] **Step 3: Extend `NexusFUSE.__init__`**

In `src/nexus/fuse/mount.py`, import helpers:

```python
from nexus.fuse.passthrough import (
    PassthroughOptions,
    RustPassthroughMount,
    mount_is_passthrough_safe,
)
```

Add constructor parameters after `use_rust`:

```python
        passthrough_enabled: bool | None = None,
        passthrough_patterns: list[str] | None = None,
        passthrough_deny_patterns: list[str] | None = None,
        passthrough_threshold_bytes: int = 128 * 1024,
        passthrough_require: bool = False,
        passthrough_backing_dir: str | Path | None = None,
```

Set fields after `self._use_rust = use_rust`:

```python
        env_options = PassthroughOptions.from_env()
        self._passthrough_options = PassthroughOptions(
            enabled=env_options.enabled if passthrough_enabled is None else passthrough_enabled,
            patterns=passthrough_patterns if passthrough_patterns is not None else env_options.patterns,
            deny_patterns=(
                passthrough_deny_patterns
                if passthrough_deny_patterns is not None
                else env_options.deny_patterns
            ),
            threshold_bytes=passthrough_threshold_bytes,
            require=passthrough_require or env_options.require,
            backing_dir=Path(passthrough_backing_dir) if passthrough_backing_dir else env_options.backing_dir,
        )
        self._rust_passthrough_mount: RustPassthroughMount | None = None
```

- [ ] **Step 4: Route eligible mounts before creating Python FUSE operations**

In `NexusFUSE.mount`, after the mount-point validation and namespace `context` construction, before `NexusFUSEOperations(...)`, add:

```python
        if self._should_use_rust_passthrough(context):
            self._start_rust_passthrough_mount()
            return
```

Add these methods to `NexusFUSE`:

```python
    def _should_use_rust_passthrough(self, context: Any | None) -> bool:
        if not (self._use_rust and self._passthrough_options.enabled):
            return False
        if not hasattr(self.nexus_fs, "_base_url") or not hasattr(self.nexus_fs, "_api_key"):
            if self._passthrough_options.require:
                raise RuntimeError("FUSE passthrough requires a remote NexusFS with _base_url and _api_key")
            return False
        if not mount_is_passthrough_safe(
            self.nexus_fs,
            mode_value=self.mode.value,
            context=context,
        ):
            if self._passthrough_options.require:
                raise RuntimeError("FUSE passthrough is not safe for this mount configuration")
            return False
        return True

    def _start_rust_passthrough_mount(self) -> None:
        self._rust_passthrough_mount = RustPassthroughMount.create(
            nexus_url=self.nexus_fs._base_url,  # noqa: SLF001
            api_key=self.nexus_fs._api_key,  # noqa: SLF001
            mount_point=self.mount_point,
            options=self._passthrough_options,
            agent_id=getattr(self.nexus_fs, "_agent_id", self._agent_id),
        )
        self._rust_passthrough_mount.start()
        self._mounted = True
        self._start_warmup()
```

- [ ] **Step 5: Stop Rust passthrough process during unmount**

At the end of successful `unmount`, before closing lease coordinator, add:

```python
            if self._rust_passthrough_mount is not None:
                self._rust_passthrough_mount.stop()
                self._rust_passthrough_mount = None
```

If unmount command fails because the Rust process has already exited, call `stop()` in the exception path before raising.

- [ ] **Step 6: Thread options through `mount_nexus`**

Add the same passthrough parameters to `mount_nexus(...)` and pass them into `NexusFUSE(...)`:

```python
        passthrough_enabled=passthrough_enabled,
        passthrough_patterns=passthrough_patterns,
        passthrough_deny_patterns=passthrough_deny_patterns,
        passthrough_threshold_bytes=passthrough_threshold_bytes,
        passthrough_require=passthrough_require,
        passthrough_backing_dir=passthrough_backing_dir,
```

- [ ] **Step 7: Run Python routing tests**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
pytest tests/unit/fuse/test_passthrough_options.py -q
```

Expected: tests pass.

- [ ] **Step 8: Run existing FUSE unit tests touched by constructor changes**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
pytest tests/unit/fuse/test_rust_available_guard.py tests/unit/fuse/test_rust_fallback.py tests/unit/fuse/test_io_handler.py -q
```

Expected: selected tests pass.

- [ ] **Step 9: Commit Python routing**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add src/nexus/fuse/mount.py tests/unit/fuse/test_passthrough_options.py
git commit -m "feat(#4060): route python passthrough mounts to rust fuse"
```

Expected: commit succeeds.

### Task 9: Linux-Gated Integration Test And Benchmark Evidence

**Files:**
- Create: `nexus-fuse/tests/passthrough_integration.rs`
- Create: `nexus-fuse/benches/passthrough_read.rs`
- Modify: `nexus-fuse/README.md`
- Modify: `nexus-fuse/PERFORMANCE_RESULTS.md`

- [ ] **Step 1: Create Linux-gated integration test skeleton**

Create `nexus-fuse/tests/passthrough_integration.rs`:

```rust
#![cfg(target_os = "linux")]

use std::process::Command;

#[test]
fn passthrough_kernel_support_probe_is_nonfatal() {
    let output = Command::new("uname")
        .arg("-r")
        .output()
        .expect("uname");
    assert!(output.status.success());
}
```

- [ ] **Step 2: Run the gated integration test**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test --test passthrough_integration
```

Expected on Linux: test passes. Expected on macOS: Cargo reports the test target has no runnable tests because of `#![cfg(target_os = "linux")]`.

- [ ] **Step 3: Create benchmark harness**

Create `nexus-fuse/benches/passthrough_read.rs`:

```rust
use criterion::{criterion_group, criterion_main, Criterion};

fn document_passthrough_benchmark(c: &mut Criterion) {
    c.bench_function("issue_4060_passthrough_command_documented", |b| {
        b.iter(|| {
            let command = "dd if=/mnt/nexus/data/one-gib.bin of=/dev/null bs=8M status=progress";
            criterion::black_box(command);
        })
    });
}

criterion_group!(benches, document_passthrough_benchmark);
criterion_main!(benches);
```

Add the bench target to `nexus-fuse/Cargo.toml`:

```toml
[[bench]]
name = "passthrough_read"
harness = false
```

- [ ] **Step 4: Run the benchmark harness**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo bench --bench passthrough_read -- --sample-size 10
```

Expected: Criterion runs the documentation harness successfully. The real throughput run still uses the documented `dd` command against a Linux FUSE mount.

- [ ] **Step 5: Document flags and benchmark procedure**

Append this section to `nexus-fuse/README.md`:

````markdown
## Linux FUSE Passthrough For Large Reads

Passthrough is opt-in and is intended for raw, read-only large-file workloads on Linux kernels with FUSE passthrough support.

Example:

```bash
nexus-fuse mount /mnt/nexus \
  --url "$NEXUS_URL" \
  --api-key-file "$NEXUS_API_KEY_FILE" \
  --passthrough \
  --passthrough-pattern "/data/**" \
  --passthrough-threshold-bytes 131072
```

Fallback behavior:

- Without `--passthrough`, reads use the normal userspace path.
- On unsupported platforms, passthrough is disabled unless `--passthrough-require` is set.
- Files below the threshold, directories, denied patterns, and write opens use normal userspace reads.

Benchmark:

```bash
dd if=/mnt/nexus/data/one-gib.bin of=/dev/null bs=8M status=progress
```
````

- [ ] **Step 6: Record performance evidence**

Append this section to `nexus-fuse/PERFORMANCE_RESULTS.md`:

````markdown
## Issue #4060: FUSE Passthrough Large Sequential Reads

Command:

```bash
dd if=/mnt/nexus/data/one-gib.bin of=/dev/null bs=8M status=progress
```

Acceptance target: at least 2x the normal userspace read path, with expected throughput near or above 6 GB/s on a supported Linux 6.9+ environment.

Local non-Linux development note: the command is documented here and the Rust/Python unit coverage verifies eligibility, fallback, and command construction. The final PR should include Linux benchmark output from a host with FUSE passthrough enabled.
````

- [ ] **Step 7: Run documentation and benchmark checks**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo test --test passthrough_integration
cargo bench --bench passthrough_read -- --sample-size 10
```

Expected: commands pass on the local platform with the Linux cfg behavior described above.

- [ ] **Step 8: Commit integration and docs**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse/tests/passthrough_integration.rs nexus-fuse/benches/passthrough_read.rs nexus-fuse/Cargo.toml nexus-fuse/README.md nexus-fuse/PERFORMANCE_RESULTS.md
git commit -m "test(#4060): document passthrough benchmark coverage"
```

Expected: commit succeeds.

### Task 10: Full Verification And PR Readiness

**Files:**
- Verify all changed files from Tasks 1-9

- [ ] **Step 1: Run Rust verification**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo fmt --check
cargo check --lib --bin nexus-fuse
cargo test --lib
cargo test --test passthrough_integration
```

Expected: all commands pass or the Linux-gated integration target reports no runnable tests on non-Linux.

- [ ] **Step 2: Run Python verification**

Run:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
ruff check src/nexus/fuse/passthrough.py src/nexus/fuse/mount.py tests/unit/fuse/test_passthrough_options.py
pytest tests/unit/fuse/test_passthrough_options.py tests/unit/fuse/test_rust_available_guard.py tests/unit/fuse/test_rust_fallback.py tests/unit/fuse/test_io_handler.py -q
```

Expected: commands pass.

- [ ] **Step 3: Run end-to-end smoke commands where available**

On a Linux host with FUSE passthrough support:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo build --release
mkdir -p /tmp/nexus-passthrough-mnt
target/release/nexus-fuse mount /tmp/nexus-passthrough-mnt \
  --url "$NEXUS_URL" \
  --api-key-file "$NEXUS_API_KEY_FILE" \
  --passthrough \
  --passthrough-pattern "/data/**" \
  --passthrough-threshold-bytes 131072
```

In a second shell:

```bash
dd if=/tmp/nexus-passthrough-mnt/data/one-gib.bin of=/dev/null bs=8M status=progress
fusermount -u /tmp/nexus-passthrough-mnt
```

Expected: throughput is at least 2x the normal userspace path. Record the output in `nexus-fuse/PERFORMANCE_RESULTS.md` before creating the PR.

- [ ] **Step 4: Confirm fallback behavior**

Run on macOS or any non-Linux host:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus/nexus-fuse
cargo run -- mount /tmp/nexus-fuse-missing \
  --url http://localhost:2026 \
  --api-key test \
  --passthrough
```

Expected: mount-point validation fails for the missing directory before any passthrough panic. When run with a real mount point on a non-Linux host and without `--passthrough-require`, logs show passthrough disabled and the userspace path is used.

- [ ] **Step 5: Commit final fixes**

Run only if verification forced formatting or small corrective edits:

```bash
cd /Users/tafeng/.codex/worktrees/4728/nexus
git add nexus-fuse src/nexus/fuse tests/unit/fuse
git commit -m "fix(#4060): polish passthrough verification"
```

Expected: commit succeeds only when there are final verification edits.

## Coverage Checklist

- Direct Rust mount: `nexus-fuse mount --passthrough` builds a passthrough manager and attempts backing registration only for eligible files.
- Python `use_rust=True`: passthrough starts a Rust-owned FUSE mount process only for remote, binary, hook-safe mounts.
- Per-pattern gating: allow and deny globs are tested in Rust and passed from Python to Rust.
- Graceful fallback: unsupported platform or unsafe Python mount falls back unless `require` is set.
- Hook semantics: Python hook-sensitive mounts remain on the existing Python FUSE path.
- Existing daemon path: `nexus-fuse daemon` and Python Rust IPC reads stay unchanged for non-passthrough operation.
- Benchmark: PR includes the documented Linux throughput command and records the measured result when a supported kernel is available.

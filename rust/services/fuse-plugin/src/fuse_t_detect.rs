//! Detect whether macOS' FUSE-T userspace driver is provisioned on this
//! host. Called from `create_fuse_plugin` on macOS before any
//! `fuser::spawn_mount2` attempt — if the driver isn't there, the mount
//! will fail with a confusing low-level error (`fuse: device not found`
//! or similar), so we surface a clean `MountOutcome::FuseTMissing`
//! instead and let the supervisor (sudowork on the desktop) handle the
//! one-shot install via `osascript`.
//!
//! Split into three layers, probed in order:
//!
//!   1. FSKit framework at `/Library/Frameworks/fuse_t.framework`
//!      — FUSE-T 1.2+ canonical location (Apple's kext-free FSKit
//!      replacement).
//!   2. Legacy filesystem bundle at `/Library/Filesystems/fuse-t.fs`
//!      — FUSE-T 1.0 / 1.1 layout, still present on dev machines that
//!      haven't upgraded.
//!   3. `pkgutil --pkgs --regexp '^org\.fuse-t\.'` — fallback for a
//!      future layout shift. Slow (subprocess), only runs when the disk
//!      probes miss.
//!
//! The three-tier shape mirrors the sudowork TS detector
//! (`src/process/services/fuset/FuseTInstallService.ts`) so the two
//! sides agree on what counts as "installed". The Rust copy is the
//! source of truth for in-cluster decisions; sudowork's copy is for
//! pre-install UI state only.

#![cfg(target_os = "macos")]

use std::path::{Path, PathBuf};
use std::process::Command;

/// Conventional install locations, probed in order. First match wins.
pub(crate) const FUSE_T_BUNDLE_CANDIDATES: &[&str] = &[
    "/Library/Frameworks/fuse_t.framework", // FUSE-T 1.2+ (FSKit)
    "/Library/Filesystems/fuse-t.fs",       // FUSE-T 1.0 / 1.1 (legacy bundle)
];

/// `pkgutil --pkgs` regex matching any version-suffixed FUSE-T package id
/// (`org.fuse-t.fskit.1.2.7`, `org.fuse-t.core.1.2.7`, future
/// `org.fuse-t.<whatever>`).
pub(crate) const FUSE_T_PKGUTIL_REGEX: &str = r"^org\.fuse-t\.";

/// Which layer confirmed the install — useful for logs / telemetry, and
/// caught in tests so a regression in the disk-path-vs-pkgutil ordering
/// doesn't silently make every detection fall through to the slow path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum DetectionHit {
    /// One of `FUSE_T_BUNDLE_CANDIDATES` exists on disk.
    DiskPath(PathBuf),
    /// pkgutil registry has at least one matching package receipt.
    PkgutilRegistry,
}

/// Result of a single detection pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum DetectionResult {
    Found(DetectionHit),
    NotFound,
}

/// Production probe — uses the real `FUSE_T_BUNDLE_CANDIDATES` and
/// shells out to `pkgutil`. Cheap on the happy path: two `Path::exists`
/// calls, short-circuits on the first hit. The subprocess only fires
/// when both disk paths miss.
pub(crate) fn is_fuse_t_installed() -> DetectionResult {
    detect(FUSE_T_BUNDLE_CANDIDATES.iter().map(Path::new), || {
        run_pkgutil_probe(FUSE_T_PKGUTIL_REGEX)
    })
}

/// Pure detection core — paths to probe + a function that runs the
/// pkgutil query. Lifted out of `is_fuse_t_installed` for tests; the
/// production caller passes the real constants, tests pass tempdir
/// paths + a closure that returns canned stdout.
pub(crate) fn detect<'a, I, F>(candidates: I, pkgutil: F) -> DetectionResult
where
    I: IntoIterator<Item = &'a Path>,
    F: FnOnce() -> bool,
{
    for candidate in candidates {
        if path_exists(candidate) {
            return DetectionResult::Found(DetectionHit::DiskPath(candidate.to_path_buf()));
        }
    }
    if pkgutil() {
        return DetectionResult::Found(DetectionHit::PkgutilRegistry);
    }
    DetectionResult::NotFound
}

/// Existence probe that treats every error (permission denied, broken
/// symlink, ...) as "not found" rather than propagating it. The
/// supervisor only needs a boolean here — diagnostic detail belongs in
/// tracing, not the return value. Matches the sudowork TS `fileExists`
/// helper's swallow-and-log shape.
fn path_exists(p: &Path) -> bool {
    match p.try_exists() {
        Ok(exists) => exists,
        Err(err) => {
            tracing::warn!(
                target: "nexus::fuse",
                path = %p.display(),
                error = %err,
                "fuse_t_detect: path probe errored; treating as not-found"
            );
            false
        }
    }
}

/// Shell out to `pkgutil --pkgs --regexp <pattern>`. Returns true iff
/// the command exits 0 with non-empty stdout. Any other outcome
/// (non-zero exit, missing pkgutil binary, IO error) is "not found".
fn run_pkgutil_probe(regex: &str) -> bool {
    match Command::new("pkgutil")
        .args(["--pkgs", "--regexp", regex])
        .output()
    {
        Ok(out) if out.status.success() => !out.stdout.iter().all(u8::is_ascii_whitespace),
        Ok(out) => {
            tracing::debug!(
                target: "nexus::fuse",
                exit_code = ?out.status.code(),
                stderr = %String::from_utf8_lossy(&out.stderr),
                "pkgutil probe returned non-success"
            );
            false
        }
        Err(err) => {
            tracing::warn!(
                target: "nexus::fuse",
                error = %err,
                "pkgutil probe failed to spawn — treating as not-found"
            );
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    /// Helper: build a tempdir with a single "framework directory"
    /// inside so the detector treats it as present.
    fn touch_dir(dir: &Path, name: &str) -> PathBuf {
        let p = dir.join(name);
        fs::create_dir_all(&p).expect("create dir candidate");
        p
    }

    #[test]
    fn detect_returns_first_disk_hit_when_framework_present() {
        let tmp = TempDir::new().unwrap();
        let framework = touch_dir(tmp.path(), "fuse_t.framework");
        let legacy = tmp.path().join("fuse-t.fs"); // not created
        let candidates = [framework.as_path(), legacy.as_path()];
        let result = detect(candidates.iter().copied(), || {
            panic!("pkgutil must not run when a disk candidate hits");
        });
        assert_eq!(
            result,
            DetectionResult::Found(DetectionHit::DiskPath(framework))
        );
    }

    #[test]
    fn detect_falls_through_to_legacy_when_framework_absent() {
        let tmp = TempDir::new().unwrap();
        let framework = tmp.path().join("fuse_t.framework"); // not created
        let legacy = touch_dir(tmp.path(), "fuse-t.fs");
        let candidates = [framework.as_path(), legacy.as_path()];
        let result = detect(candidates.iter().copied(), || {
            panic!("pkgutil must not run when a disk candidate hits");
        });
        assert_eq!(
            result,
            DetectionResult::Found(DetectionHit::DiskPath(legacy))
        );
    }

    #[test]
    fn detect_falls_through_to_pkgutil_when_both_disk_candidates_miss() {
        let tmp = TempDir::new().unwrap();
        let framework = tmp.path().join("fuse_t.framework");
        let legacy = tmp.path().join("fuse-t.fs");
        let candidates = [framework.as_path(), legacy.as_path()];
        let result = detect(candidates.iter().copied(), || true);
        assert_eq!(
            result,
            DetectionResult::Found(DetectionHit::PkgutilRegistry)
        );
    }

    #[test]
    fn detect_returns_not_found_when_every_layer_misses() {
        let tmp = TempDir::new().unwrap();
        let framework = tmp.path().join("fuse_t.framework");
        let legacy = tmp.path().join("fuse-t.fs");
        let candidates = [framework.as_path(), legacy.as_path()];
        let result = detect(candidates.iter().copied(), || false);
        assert_eq!(result, DetectionResult::NotFound);
    }

    #[test]
    fn pkgutil_probe_only_runs_on_disk_miss() {
        // Sanity: the iterator-based core MUST short-circuit. If it
        // doesn't, the production caller will run pkgutil on every
        // single mount even when the framework path is present —
        // perf gate.
        let tmp = TempDir::new().unwrap();
        let present = touch_dir(tmp.path(), "fuse_t.framework");
        let mut ran = false;
        let _ = detect([present.as_path()].iter().copied(), || {
            ran = true;
            true
        });
        assert!(!ran, "pkgutil ran despite a disk hit");
    }
}

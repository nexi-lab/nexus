//! `PathIndex` — bidirectional inode↔path map shared by the
//! fuser-based [`crate::fs`] (Linux / macOS) and the WinFsp-based
//! [`crate::fs_winfsp`] (Windows) adapters.
//!
//! Both adapters address files internally by `u64` inode (FUSE's
//! native shape, WinFsp's `FileContext` field) but Nexus VFS
//! addresses them by path string.  Every callback needs to translate
//! between the two on the hot path.
//!
//! ## Why bidirectional
//!
//! Forward (`ino → path`) translates an in-flight op's inode back to
//! the kernel-side path.  Reverse (`path → ino`) lets `lookup`
//! return a **stable** inode for a path it has seen before — without
//! it every LOOKUP minted a fresh inode and the kernel-side FUSE
//! driver couldn't tie its cached dentry to the one the next op
//! walked through.  Rename's `d_move` is the canonical case: source
//! dentry got inode `A` on create, dest lookup returned inode `B`;
//! the rename op's userspace handler ran fine but the kernel-side
//! `d_move(A → B)` linked the wrong inode and downstream
//! `cat <dest>` surfaced ENOENT despite the rename having succeeded
//! kernel-side.  See `crate::fs`'s commit history for the
//! reproduction that landed this design.
//!
//! ## Synchronisation
//!
//! Single mutex around the inner struct so both maps stay in
//! lockstep — every mutator updates both sides in one critical
//! section.  Callers wrap the [`PathIndex`] in a `Mutex` themselves
//! (`Mutex<PathIndex>`); the type does not enforce locking so unit
//! tests can exercise the mutators directly.

use std::collections::HashMap;

/// Join a parent VFS path and a child file name into the canonical
/// `parent/child` form the kernel expects.  Strips trailing slashes
/// from `parent` so `/` + `foo` == `/foo` (not `//foo`).  Lives in
/// the path-index module because both adapters that touch the index
/// also need this exact shape for child-path construction.
pub fn join_path(parent: &str, name: &str) -> String {
    if parent == "/" {
        format!("/{name}")
    } else {
        format!("{}/{}", parent.trim_end_matches('/'), name)
    }
}

/// Bidirectional inode↔path index.  Construct via
/// [`PathIndex::with_root`], then call [`Self::lookup_or_register`] /
/// [`Self::rename`] / [`Self::forget`] under whatever outer lock the
/// platform adapter uses.
pub struct PathIndex {
    ino_to_path: HashMap<u64, String>,
    path_to_ino: HashMap<String, u64>,
}

impl PathIndex {
    /// Seed the index with the root inode → VFS root prefix binding
    /// the platform adapter chose.  On Linux / macOS the root inode
    /// is fuser's `INodeNo::ROOT` (raw value `1`); on Windows we
    /// pick our own constant (the WinFsp filesystem context model
    /// doesn't expose an analogue, the inode space is fully ours).
    pub fn with_root(root_ino: u64, vfs_root: String) -> Self {
        let mut ino_to_path = HashMap::new();
        let mut path_to_ino = HashMap::new();
        ino_to_path.insert(root_ino, vfs_root.clone());
        path_to_ino.insert(vfs_root, root_ino);
        Self {
            ino_to_path,
            path_to_ino,
        }
    }

    /// Look up the path bound to an inode, returning a clone so the
    /// caller can drop its outer lock before issuing the kernel
    /// callback (avoids holding the index mutex across an FFI call
    /// that may take milliseconds on a cold path).
    pub fn path_for(&self, ino: u64) -> Option<String> {
        self.ino_to_path.get(&ino).cloned()
    }

    /// Reuse the existing inode for paths we have seen, mint fresh
    /// ones for paths we haven't.  `mint` is invoked at most once
    /// per call and only on the miss path — the caller passes its
    /// atomic counter's `fetch_add` closure.
    pub fn lookup_or_register(&mut self, path: &str, mint: impl FnOnce() -> u64) -> u64 {
        if let Some(&ino) = self.path_to_ino.get(path) {
            return ino;
        }
        let ino = mint();
        self.ino_to_path.insert(ino, path.to_string());
        self.path_to_ino.insert(path.to_string(), ino);
        ino
    }

    /// Move the inode currently bound to `old_path` over to
    /// `new_path`.  No-op when `old_path` isn't tracked (e.g. the
    /// kernel-side rename succeeded against a path the FUSE side
    /// never looked up, which can happen with bulk operators).
    ///
    /// Used by the `rename` op so subsequent `read` / `getattr` on
    /// the d_moved inode walks the new path through `sys_*` —
    /// otherwise the inode would still resolve to `old_path` and
    /// every read would surface ENOENT.
    pub fn rename(&mut self, old_path: &str, new_path: &str) {
        let Some(ino) = self.path_to_ino.remove(old_path) else {
            return;
        };
        self.ino_to_path.insert(ino, new_path.to_string());
        self.path_to_ino.insert(new_path.to_string(), ino);
    }

    /// Drop the bidirectional mapping for `path`.  Used by `unlink`
    /// and `rmdir` to release the inode after the kernel-side entry
    /// is gone — a subsequent `create` at the same path then mints
    /// a fresh inode instead of reusing one whose previous lifetime
    /// the kernel may have already forgotten.
    pub fn forget(&mut self, path: &str) {
        if let Some(ino) = self.path_to_ino.remove(path) {
            self.ino_to_path.remove(&ino);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Sentinel root inode used by every fuser-side test — matches
    /// `fuser::INodeNo::ROOT.0`.  Hard-coded here so the tests can
    /// run on every target_os without pulling in the fuser dep.
    const FUSER_ROOT: u64 = 1;

    #[test]
    fn with_root_registers_both_directions() {
        let idx = PathIndex::with_root(FUSER_ROOT, "/".to_string());
        assert_eq!(idx.path_for(FUSER_ROOT).as_deref(), Some("/"));
    }

    #[test]
    fn lookup_or_register_is_stable_per_path() {
        let mut idx = PathIndex::with_root(FUSER_ROOT, "/".to_string());
        let mut counter = FUSER_ROOT + 1;
        let mut mint = || {
            let i = counter;
            counter += 1;
            i
        };
        let a = idx.lookup_or_register("/foo", &mut mint);
        let b = idx.lookup_or_register("/foo", &mut mint);
        assert_eq!(a, b, "same path must yield the same inode");
        let c = idx.lookup_or_register("/bar", &mut mint);
        assert_ne!(a, c, "different paths must yield distinct inodes");
    }

    #[test]
    fn rename_moves_binding_to_new_path() {
        let mut idx = PathIndex::with_root(FUSER_ROOT, "/".to_string());
        let ino = idx.lookup_or_register("/old", || 2);
        idx.rename("/old", "/new");
        assert_eq!(
            idx.path_for(ino).as_deref(),
            Some("/new"),
            "inode must resolve to the new path",
        );
        // path→inode for `/old` is gone — next lookup re-mints a
        // fresh inode rather than colliding with the moved one.
        let fresh = idx.lookup_or_register("/old", || 3);
        assert_eq!(fresh, 3);
        assert_ne!(fresh, ino);
    }

    #[test]
    fn rename_unknown_old_path_is_a_noop() {
        let mut idx = PathIndex::with_root(FUSER_ROOT, "/".to_string());
        idx.rename("/never-seen", "/whatever");
        // /whatever must NOT have been minted — only the explicit
        // lookup-or-register path mints fresh inodes.
        assert!(idx.path_for(2).is_none());
    }

    #[test]
    fn forget_drops_both_directions() {
        let mut idx = PathIndex::with_root(FUSER_ROOT, "/".to_string());
        let ino = idx.lookup_or_register("/doomed", || 2);
        idx.forget("/doomed");
        assert!(idx.path_for(ino).is_none());
        // Re-create at the same path mints a fresh inode rather than
        // recycling the freed one — the kernel may already have
        // forgotten the old inode and re-using would confuse it.
        let fresh = idx.lookup_or_register("/doomed", || 3);
        assert_eq!(fresh, 3);
    }
}

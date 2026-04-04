//! KernelDispatch — dispatch traits, trie, hook/observer registries.
//!
//! Three dispatch trait families (PR 7c, hand-written -- PR 8 codegen replaces):
//!   - InterceptHook: pre/post hooks per syscall (INTERCEPT phase, LSM-style)
//!   - PathResolver: virtual path short-circuit (PRE-DISPATCH phase, procfs-style)
//!   - MutationObserver: fire-and-forget event notification (OBSERVE phase, fsnotify-style)
//!
//! Infrastructure:
//!   - PathTrie: O(path_depth) lookup (~50ns) for virtual path resolvers.
//!   - HookRegistry: cached metadata for INTERCEPT hooks.
//!   - ObserverRegistry: bitmask-filtered OBSERVE observers.
//!
//! All types are owned directly by Kernel (no Arc wrapper, no #[pyclass]).
//! PyO3 types (Py<PyAny>) are used only in HookEntry/ObserverEntry for storing
//! original Python objects returned to Python callers — these are opaque handles.
//!
//! Issue #1868: Kernel owns all dispatch state.

use parking_lot::RwLock;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};

// ── Dispatch Traits (Rust ABI — PR 8 codegen replaces hand-written adapters) ──

/// INTERCEPT hook — called before/after each syscall.
///
/// Rust equivalent of Python `VFSReadHook`/`VFSWriteHook`/etc.
/// Pre-hooks can abort by returning Err. Post-hooks are fire-and-forget.
///
/// Each method receives opaque context (PyObject) — kernel never inspects it.
/// PR 8 codegen will generate per-syscall typed context structs.
#[allow(dead_code)]
pub(crate) trait InterceptHook: Send + Sync {
    fn name(&self) -> &str;
    fn on_pre_read(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_read(&self, ctx: &Py<PyAny>);
    fn on_pre_write(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_write(&self, ctx: &Py<PyAny>);
    fn on_pre_delete(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_delete(&self, ctx: &Py<PyAny>);
    fn on_pre_rename(&self, ctx: &Py<PyAny>) -> Result<(), PyErr>;
    fn on_post_rename(&self, ctx: &Py<PyAny>);
}

/// PRE-DISPATCH resolver — virtual path short-circuit.
///
/// Rust equivalent of Python `VFSPathResolver`.
/// Returns Some(content) to claim the path, None to pass through.
#[allow(dead_code)]
pub(crate) trait PathResolver: Send + Sync {
    fn try_read(&self, path: &str) -> Option<Vec<u8>>;
    fn try_write(&self, path: &str, content: &[u8]) -> Option<()>;
    fn try_delete(&self, path: &str) -> Option<()>;
}

/// OBSERVE mutation observer — fire-and-forget event notification.
///
/// Rust equivalent of Python `VFSObserver`.
/// Receives event type + path after each mutation. Never aborts.
#[allow(dead_code)]
pub(crate) trait MutationObserver: Send + Sync {
    fn on_mutation(&self, event_type: u32, path: &str);
}

// ── TrieNode ──────────────────────────────────────────────────────────

/// Internal trie node — one per path segment.
struct TrieNode {
    /// Literal segment children.
    children: HashMap<String, TrieNode>,
    /// Wildcard child (`{}` matches any single segment).
    wildcard: Option<Box<TrieNode>>,
    /// Resolver index if this node terminates a pattern.
    resolver_idx: Option<usize>,
}

impl TrieNode {
    fn new() -> Self {
        Self {
            children: HashMap::new(),
            wildcard: None,
            resolver_idx: None,
        }
    }

    fn is_empty(&self) -> bool {
        self.children.is_empty() && self.wildcard.is_none() && self.resolver_idx.is_none()
    }

    /// Recursive lookup — literal match takes priority over wildcard.
    fn lookup(&self, segments: &[&str]) -> Option<usize> {
        if segments.is_empty() {
            return self.resolver_idx;
        }
        let seg = segments[0];
        let rest = &segments[1..];

        // Literal first (more specific)
        if let Some(child) = self.children.get(seg) {
            if let Some(idx) = child.lookup(rest) {
                return Some(idx);
            }
        }
        // Wildcard fallback
        if let Some(ref wc) = self.wildcard {
            if let Some(idx) = wc.lookup(rest) {
                return Some(idx);
            }
        }
        None
    }

    /// Insert a pattern.  Segments consumed left-to-right.
    fn insert(&mut self, segments: &[&str], resolver_idx: usize) {
        if segments.is_empty() {
            self.resolver_idx = Some(resolver_idx);
            return;
        }
        let seg = segments[0];
        let rest = &segments[1..];

        if seg == "{}" {
            if self.wildcard.is_none() {
                self.wildcard = Some(Box::new(TrieNode::new()));
            }
            self.wildcard
                .as_deref_mut()
                .unwrap()
                .insert(rest, resolver_idx);
        } else {
            self.children
                .entry(seg.to_string())
                .or_insert_with(TrieNode::new)
                .insert(rest, resolver_idx);
        }
    }

    /// Remove a pattern.  Returns `true` if this node is now empty (prune hint).
    fn remove(&mut self, segments: &[&str]) -> bool {
        if segments.is_empty() {
            self.resolver_idx = None;
            return self.is_empty();
        }
        let seg = segments[0];
        let rest = &segments[1..];

        if seg == "{}" {
            let child_empty = self
                .wildcard
                .as_deref_mut()
                .map(|wc| wc.remove(rest))
                .unwrap_or(false);
            if child_empty {
                self.wildcard = None;
            }
        } else {
            let child_empty = self
                .children
                .get_mut(seg)
                .map(|child| child.remove(rest))
                .unwrap_or(false);
            if child_empty {
                self.children.remove(seg);
            }
        }
        self.is_empty()
    }
}

// ── Trie (owned directly by Kernel) ─────────────────────────────────

pub(crate) struct Trie {
    root: RwLock<TrieNode>,
    count: AtomicUsize,
    patterns: RwLock<HashMap<usize, String>>,
}

impl Trie {
    pub(crate) fn new() -> Self {
        Self {
            root: RwLock::new(TrieNode::new()),
            count: AtomicUsize::new(0),
            patterns: RwLock::new(HashMap::new()),
        }
    }

    /// Lookup a concrete path.  Returns resolver index or None.
    pub(crate) fn lookup(&self, path: &str) -> Option<usize> {
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        self.root.read().lookup(&segments)
    }

    /// Register a path pattern with a resolver index.
    pub(crate) fn register(&self, pattern: &str, resolver_idx: usize) -> Result<(), String> {
        let mut patterns = self.patterns.write();
        if patterns.contains_key(&resolver_idx) {
            return Err(format!("resolver_idx {} already registered", resolver_idx));
        }
        let segments: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        self.root.write().insert(&segments, resolver_idx);
        patterns.insert(resolver_idx, pattern.to_string());
        self.count.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    /// Remove a resolver by index.  Returns true if found.
    pub(crate) fn unregister(&self, resolver_idx: usize) -> bool {
        let pattern = match self.patterns.write().remove(&resolver_idx) {
            Some(p) => p,
            None => return false,
        };
        let segments: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        self.root.write().remove(&segments);
        self.count.fetch_sub(1, Ordering::Relaxed);
        true
    }

    /// Number of registered patterns.
    pub(crate) fn len(&self) -> usize {
        self.count.load(Ordering::Relaxed)
    }
}

// ── HookRegistry (owned by PyKernel wrapper, not by pure Rust Kernel) ──

/// Cached metadata for a single hook.
pub(crate) struct HookEntry {
    /// Rust trait object — used by kernel dispatch (language-agnostic).
    pub(crate) hook: Box<dyn InterceptHook>,
    /// Original Python object — returned to Python callers via get_pre_hooks().
    pub(crate) hook_py: Py<PyAny>,
    pub(crate) has_pre: bool,
    pub(crate) is_async_post: bool,
    #[allow(dead_code)]
    pub(crate) name: String,
}

/// Registry that caches hook metadata at registration time.
///
/// Eliminates per-dispatch `getattr()` and `inspect.iscoroutinefunction()`
/// overhead by detecting these properties once at `register()` time.
pub(crate) struct HookRegistry {
    ops: HashMap<String, Vec<HookEntry>>,
}

impl HookRegistry {
    pub(crate) fn new() -> Self {
        Self {
            ops: HashMap::new(),
        }
    }

    /// Register a hook for the given operation.
    pub(crate) fn register(
        &mut self,
        op: &str,
        hook_impl: Box<dyn InterceptHook>,
        hook_py: Py<PyAny>,
        has_pre: bool,
        is_async_post: bool,
        name: String,
    ) {
        self.ops.entry(op.to_string()).or_default().push(HookEntry {
            hook: hook_impl,
            hook_py,
            has_pre,
            is_async_post,
            name,
        });
    }

    /// Remove a hook by identity (`is` check on original Python object).
    pub(crate) fn unregister(&mut self, py: Python<'_>, op: &str, hook: &Bound<'_, PyAny>) -> bool {
        if let Some(entries) = self.ops.get_mut(op) {
            let hook_ptr = hook.as_ptr();
            if let Some(pos) = entries
                .iter()
                .position(|e| e.hook_py.bind(py).as_ptr() == hook_ptr)
            {
                entries.remove(pos);
                return true;
            }
        }
        false
    }

    /// Return Python hook objects that have `on_pre_{op}` (for Python callers).
    pub(crate) fn get_pre_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| e.has_pre)
                    .map(|e| e.hook_py.clone_ref(py))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return Rust trait references for pre-hooks (for kernel dispatch).
    pub(crate) fn get_pre_hook_impls(&self, op: &str) -> Vec<&dyn InterceptHook> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| e.has_pre)
                    .map(|e| e.hook.as_ref())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return Rust trait references for sync post-hooks (kernel dispatch).
    pub(crate) fn get_post_hook_impls(&self, op: &str) -> Vec<&dyn InterceptHook> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| !e.is_async_post)
                    .map(|e| e.hook.as_ref())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return (sync_post_hooks, async_post_hooks) as Python objects.
    pub(crate) fn get_post_hooks(
        &self,
        py: Python<'_>,
        op: &str,
    ) -> (Vec<Py<PyAny>>, Vec<Py<PyAny>>) {
        let entries = match self.ops.get(op) {
            Some(e) => e,
            None => return (Vec::new(), Vec::new()),
        };
        let sync: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| !e.is_async_post)
            .map(|e| e.hook_py.clone_ref(py))
            .collect();
        let async_: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| e.is_async_post)
            .map(|e| e.hook_py.clone_ref(py))
            .collect();
        (sync, async_)
    }

    /// Return all Python hook objects for the given operation.
    pub(crate) fn get_all_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| entries.iter().map(|e| e.hook_py.clone_ref(py)).collect())
            .unwrap_or_default()
    }

    /// Number of hooks registered for the given operation.
    pub(crate) fn count(&self, op: &str) -> usize {
        self.ops.get(op).map(|e| e.len()).unwrap_or(0)
    }
}

// ── ObserverRegistry (owned by PyKernel wrapper) ────────────────────

pub(crate) struct ObserverEntry {
    pub(crate) observer: Py<PyAny>,
    pub(crate) name: String,
    pub(crate) event_mask: u32,
}

/// Rust-side observer registry with event-type bitmask filtering.
pub(crate) struct ObserverRegistry {
    observers: Vec<ObserverEntry>,
}

impl ObserverRegistry {
    pub(crate) fn new() -> Self {
        Self {
            observers: Vec::new(),
        }
    }

    /// Register observer with event_mask bitmask.
    pub(crate) fn register(
        &mut self,
        py: Python<'_>,
        obs: Py<PyAny>,
        event_mask: u32,
    ) -> PyResult<()> {
        let obs_ref = obs.bind(py);
        let name: String = obs_ref
            .get_type()
            .name()
            .map(|n| n.to_string())
            .unwrap_or_else(|_| "<?>".to_string());

        self.observers.push(ObserverEntry {
            observer: obs,
            name,
            event_mask,
        });
        Ok(())
    }

    /// Unregister by identity.
    pub(crate) fn unregister(&mut self, py: Python<'_>, obs: &Bound<'_, PyAny>) -> bool {
        let obs_ptr = obs.as_ptr();
        if let Some(pos) = self
            .observers
            .iter()
            .position(|e| e.observer.bind(py).as_ptr() == obs_ptr)
        {
            self.observers.remove(pos);
            return true;
        }
        false
    }

    /// Return (observer, name) pairs matching the event_type_bit.
    pub(crate) fn get_matching(
        &self,
        py: Python<'_>,
        event_type_bit: u32,
    ) -> Vec<(Py<PyAny>, String)> {
        self.observers
            .iter()
            .filter(|e| e.event_mask & event_type_bit != 0)
            .map(|e| (e.observer.clone_ref(py), e.name.clone()))
            .collect()
    }

    pub(crate) fn count(&self) -> usize {
        self.observers.len()
    }
}

// ── Tests (TrieNode only — no PyO3 linking required) ───────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: parse pattern into segments and insert into root node.
    fn insert(root: &mut TrieNode, pattern: &str, idx: usize) {
        let segs: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        root.insert(&segs, idx);
    }

    /// Helper: parse path into segments and lookup in root node.
    fn find(root: &TrieNode, path: &str) -> Option<usize> {
        let segs: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        root.lookup(&segs)
    }

    /// Helper: parse pattern into segments and remove from root node.
    fn del(root: &mut TrieNode, pattern: &str) {
        let segs: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        root.remove(&segs);
    }

    #[test]
    fn test_basic_literal_pattern() {
        let mut root = TrieNode::new();
        insert(&mut root, "/.tasks/status", 0);
        assert_eq!(find(&root, "/.tasks/status"), Some(0));
        assert_eq!(find(&root, "/.tasks/other"), None);
        assert_eq!(find(&root, "/foo"), None);
    }

    #[test]
    fn test_wildcard_pattern() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        assert_eq!(find(&root, "/myzone/proc/123/status"), Some(0));
        assert_eq!(find(&root, "/other/proc/abc/status"), Some(0));
        assert_eq!(find(&root, "/zone/proc/pid/other"), None);
        assert_eq!(find(&root, "/zone/notproc/pid/status"), None);
    }

    #[test]
    fn test_task_agent_pattern() {
        let mut root = TrieNode::new();
        insert(&mut root, "/.tasks/tasks/{}/agent/status", 1);
        assert_eq!(find(&root, "/.tasks/tasks/t42/agent/status"), Some(1));
        assert_eq!(find(&root, "/.tasks/tasks/abc-def/agent/status"), Some(1));
        assert_eq!(find(&root, "/.tasks/tasks/t42/agent/other"), None);
    }

    #[test]
    fn test_multiple_patterns() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        insert(&mut root, "/.tasks/tasks/{}/agent/status", 1);
        assert_eq!(find(&root, "/z/proc/p1/status"), Some(0));
        assert_eq!(find(&root, "/.tasks/tasks/t1/agent/status"), Some(1));
        assert_eq!(find(&root, "/random/path"), None);
    }

    #[test]
    fn test_literal_priority_over_wildcard() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        insert(&mut root, "/.tasks/proc/{}/status", 1);
        assert_eq!(find(&root, "/.tasks/proc/p1/status"), Some(1));
        assert_eq!(find(&root, "/zone/proc/p1/status"), Some(0));
    }

    #[test]
    fn test_unregister_existing() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        assert_eq!(find(&root, "/z/proc/p/status"), Some(0));
        del(&mut root, "/{}/proc/{}/status");
        assert_eq!(find(&root, "/z/proc/p/status"), None);
        assert!(root.is_empty());
    }

    #[test]
    fn test_unregister_preserves_other_patterns() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        insert(&mut root, "/.tasks/tasks/{}/agent/status", 1);
        del(&mut root, "/{}/proc/{}/status");
        assert_eq!(find(&root, "/z/proc/p/status"), None);
        assert_eq!(find(&root, "/.tasks/tasks/t1/agent/status"), Some(1));
    }

    #[test]
    fn test_re_insert_after_remove() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        del(&mut root, "/{}/proc/{}/status");
        insert(&mut root, "/{}/sysfs/{}/info", 7);
        assert_eq!(find(&root, "/z/sysfs/dev/info"), Some(7));
        assert_eq!(find(&root, "/z/proc/p/status"), None);
    }

    #[test]
    fn test_root_path() {
        let root = TrieNode::new();
        assert_eq!(find(&root, "/"), None);
    }

    #[test]
    fn test_empty_path() {
        let root = TrieNode::new();
        assert_eq!(find(&root, ""), None);
    }

    #[test]
    fn test_trailing_slash_ignored() {
        let mut root = TrieNode::new();
        insert(&mut root, "/a/b/c", 0);
        assert_eq!(find(&root, "/a/b/c/"), Some(0));
        assert_eq!(find(&root, "/a/b/c"), Some(0));
    }

    #[test]
    fn test_segment_count_mismatch() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        assert_eq!(find(&root, "/zone/proc"), None);
        assert_eq!(find(&root, "/zone/proc/pid/status/extra"), None);
    }

    #[test]
    fn test_unicode_segments() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        assert_eq!(find(&root, "/日本語/proc/进程/status"), Some(0));
    }

    #[test]
    fn test_single_segment_pattern() {
        let mut root = TrieNode::new();
        insert(&mut root, "/health", 0);
        assert_eq!(find(&root, "/health"), Some(0));
        assert_eq!(find(&root, "/other"), None);
    }

    #[test]
    fn test_all_wildcards() {
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/{}/{}", 0);
        assert_eq!(find(&root, "/a/b/c"), Some(0));
        assert_eq!(find(&root, "/a/b"), None);
        assert_eq!(find(&root, "/a/b/c/d"), None);
    }

    #[test]
    fn test_trie_register_and_lookup() {
        let trie = Trie::new();
        trie.register("/{}/proc/{}/status", 42).unwrap();
        assert_eq!(trie.lookup("/zone/proc/123/status"), Some(42));
        assert_eq!(trie.lookup("/missing"), None);
        assert_eq!(trie.len(), 1);
    }

    #[test]
    fn test_trie_unregister() {
        let trie = Trie::new();
        trie.register("/{}/proc/{}/status", 0).unwrap();
        assert!(trie.unregister(0));
        assert_eq!(trie.lookup("/z/proc/p/status"), None);
        assert_eq!(trie.len(), 0);
    }

    #[test]
    fn test_trie_duplicate_idx_error() {
        let trie = Trie::new();
        trie.register("/a", 0).unwrap();
        assert!(trie.register("/b", 0).is_err());
    }
}

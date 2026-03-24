//! KernelDispatch — segment-based trie for VFS path resolver routing.
//!
//! Provides O(path_depth) lookup (~50ns) for virtual path resolvers,
//! replacing O(N) linear regex scan.  `{}` segments match any single
//! path component (wildcard).
//!
//! Related: Issue #1317

use pyo3::prelude::*;
use pyo3::types::PyAny;
use std::collections::HashMap;

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

// ── PathTrie (PyO3 class) ─────────────────────────────────────────────

/// Segment-based trie for O(path_depth) VFS resolver routing.
///
/// Patterns like ``/{}/proc/{}/status`` are split by ``/`` into segments.
/// ``{}`` segments match any single path component (wildcard).
/// Literal segments take priority over wildcards during lookup.
///
/// Example::
///
///     trie = PathTrie()
///     trie.register("/{}/proc/{}/status", 0)
///     assert trie.lookup("/myzone/proc/123/status") == 0
///     assert trie.lookup("/other/path") is None
#[pyclass]
pub struct PathTrie {
    root: TrieNode,
    count: usize,
    /// resolver_idx → pattern string (for unregister).
    patterns: HashMap<usize, String>,
}

#[pymethods]
impl PathTrie {
    #[new]
    fn new() -> Self {
        Self {
            root: TrieNode::new(),
            count: 0,
            patterns: HashMap::new(),
        }
    }

    /// Register a path pattern with a resolver index.
    ///
    /// Pattern segments are split by ``/``.  ``{}`` matches any single segment.
    /// Raises ``ValueError`` if ``resolver_idx`` is already registered.
    fn register(&mut self, pattern: &str, resolver_idx: usize) -> PyResult<()> {
        if self.patterns.contains_key(&resolver_idx) {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "resolver_idx {} already registered",
                resolver_idx
            )));
        }
        let segments: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        self.root.insert(&segments, resolver_idx);
        self.patterns.insert(resolver_idx, pattern.to_string());
        self.count += 1;
        Ok(())
    }

    /// Remove a resolver by index.  Returns ``True`` if found.
    fn unregister(&mut self, resolver_idx: usize) -> bool {
        let pattern = match self.patterns.remove(&resolver_idx) {
            Some(p) => p,
            None => return false,
        };
        let segments: Vec<&str> = pattern.split('/').filter(|s| !s.is_empty()).collect();
        self.root.remove(&segments);
        self.count -= 1;
        true
    }

    /// Lookup a concrete path.  Returns resolver index or ``None``.
    fn lookup(&self, path: &str) -> Option<usize> {
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        self.root.lookup(&segments)
    }

    fn __len__(&self) -> usize {
        self.count
    }

    fn __repr__(&self) -> String {
        format!("PathTrie(count={})", self.count)
    }
}

// ── HookRegistry (Phase 2) ─────────────────────────────────────────────

/// Cached metadata for a single hook (detected once at registration).
#[allow(dead_code)]
struct HookEntry {
    hook: Py<PyAny>,
    has_pre: bool,
    is_async_post: bool,
    name: String,
}

/// Registry that caches hook metadata at registration time.
///
/// Eliminates per-dispatch ``getattr()`` and ``inspect.iscoroutinefunction()``
/// overhead by detecting these properties once at ``register()`` time.
///
/// Ops: ``"read"``, ``"write"``, ``"write_batch"``, ``"delete"``,
/// ``"rename"``, ``"mkdir"``, ``"rmdir"``.
///
/// Example::
///
///     reg = HookRegistry()
///     reg.register("write", hook)
///     pre_hooks = reg.get_pre_hooks("write")
///     sync_post, async_post = reg.get_post_hooks("write")
#[pyclass]
pub struct HookRegistry {
    ops: HashMap<String, Vec<HookEntry>>,
}

#[pymethods]
impl HookRegistry {
    #[new]
    fn new() -> Self {
        Self {
            ops: HashMap::new(),
        }
    }

    /// Register a hook for the given operation.
    ///
    /// Caches ``has_pre``, ``is_async_post``, and ``name`` at registration.
    fn register(&mut self, py: Python<'_>, op: &str, hook: Py<PyAny>) -> PyResult<()> {
        let hook_ref = hook.bind(py);

        // Cache name
        let name: String = hook_ref
            .getattr("name")
            .and_then(|n| n.extract())
            .unwrap_or_else(|_| {
                hook_ref
                    .get_type()
                    .name()
                    .map(|n| format!("<{}>", n))
                    .unwrap_or_else(|_| "<?>".to_string())
            });

        // Cache has_pre: does on_pre_{op} exist and is it not None?
        let pre_attr = format!("on_pre_{}", op);
        let has_pre = hook_ref
            .getattr(pre_attr.as_str())
            .map(|attr| !attr.is_none())
            .unwrap_or(false);

        // Cache is_async_post: inspect.iscoroutinefunction(hook.on_post_{op})
        let post_attr = format!("on_post_{}", op);
        let is_async_post = match hook_ref.getattr(post_attr.as_str()) {
            Ok(post_fn) => {
                let inspect = py.import("inspect")?;
                inspect
                    .call_method1("iscoroutinefunction", (post_fn,))?
                    .extract::<bool>()
                    .unwrap_or(false)
            }
            Err(_) => false,
        };

        self.ops.entry(op.to_string()).or_default().push(HookEntry {
            hook,
            has_pre,
            is_async_post,
            name,
        });

        Ok(())
    }

    /// Remove a hook by identity (``is`` check).  Returns ``True`` if found.
    fn unregister(&mut self, py: Python<'_>, op: &str, hook: &Bound<'_, PyAny>) -> bool {
        if let Some(entries) = self.ops.get_mut(op) {
            let hook_ptr = hook.as_ptr();
            if let Some(pos) = entries
                .iter()
                .position(|e| e.hook.bind(py).as_ptr() == hook_ptr)
            {
                entries.remove(pos);
                return true;
            }
        }
        false
    }

    /// Return hooks that have ``on_pre_{op}`` (for serial PRE dispatch).
    fn get_pre_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| {
                entries
                    .iter()
                    .filter(|e| e.has_pre)
                    .map(|e| e.hook.clone_ref(py))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return ``(sync_post_hooks, async_post_hooks)`` — pre-split for dispatch.
    fn get_post_hooks(&self, py: Python<'_>, op: &str) -> (Vec<Py<PyAny>>, Vec<Py<PyAny>>) {
        let entries = match self.ops.get(op) {
            Some(e) => e,
            None => return (Vec::new(), Vec::new()),
        };
        let sync: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| !e.is_async_post)
            .map(|e| e.hook.clone_ref(py))
            .collect();
        let async_: Vec<Py<PyAny>> = entries
            .iter()
            .filter(|e| e.is_async_post)
            .map(|e| e.hook.clone_ref(py))
            .collect();
        (sync, async_)
    }

    /// Return all hooks for the given operation (ordered).
    fn get_all_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {
        self.ops
            .get(op)
            .map(|entries| entries.iter().map(|e| e.hook.clone_ref(py)).collect())
            .unwrap_or_default()
    }

    /// Number of hooks registered for the given operation.
    fn count(&self, op: &str) -> usize {
        self.ops.get(op).map(|e| e.len()).unwrap_or(0)
    }

    fn __repr__(&self) -> String {
        let counts: Vec<String> = self
            .ops
            .iter()
            .filter(|(_, v)| !v.is_empty())
            .map(|(k, v)| format!("{}={}", k, v.len()))
            .collect();
        format!("HookRegistry({})", counts.join(", "))
    }
}

// ── ObserverRegistry (Phase 3 — Issue #1748) ─────────────────────────

/// Cached entry for one OBSERVE-phase observer.
struct ObserverEntry {
    observer: Py<PyAny>,
    name: String,
    event_mask: u32,
}

/// Rust-side observer registry with event-type bitmask filtering.
///
/// Observers are registered with a ``u32`` bitmask of ``FileEventType``
/// positions.  ``get_matching(bit)`` returns only those observers whose
/// mask includes the given event type — O(N) bitmask scan, but N is
/// typically ≤5 and the filter avoids crossing to Python for irrelevant
/// observers.
///
/// Example::
///
///     reg = ObserverRegistry()
///     reg.register(obs, 0x03)          # FILE_WRITE | FILE_DELETE
///     matches = reg.get_matching(0x01) # FILE_WRITE bit → returns obs
///     misses  = reg.get_matching(0x10) # DIR_CREATE bit → empty
#[pyclass]
pub struct ObserverRegistry {
    observers: Vec<ObserverEntry>,
}

#[pymethods]
impl ObserverRegistry {
    #[new]
    fn new() -> Self {
        Self {
            observers: Vec::new(),
        }
    }

    /// Register observer with event_mask bitmask.
    /// Name is cached from ``type(obs).__name__`` at registration time.
    fn register(&mut self, py: Python<'_>, obs: Py<PyAny>, event_mask: u32) -> PyResult<()> {
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

    /// Unregister by identity (``is`` check).  Returns ``True`` if found.
    fn unregister(&mut self, py: Python<'_>, obs: &Bound<'_, PyAny>) -> bool {
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

    /// Return ``(observer, name)`` pairs whose ``event_mask`` includes ``event_type_bit``.
    ///
    /// Rust-side O(N) bitmask filter — only matching observers cross to Python.
    fn get_matching(&self, py: Python<'_>, event_type_bit: u32) -> Vec<(Py<PyAny>, String)> {
        self.observers
            .iter()
            .filter(|e| e.event_mask & event_type_bit != 0)
            .map(|e| (e.observer.clone_ref(py), e.name.clone()))
            .collect()
    }

    fn count(&self) -> usize {
        self.observers.len()
    }

    fn __repr__(&self) -> String {
        format!("ObserverRegistry(count={})", self.observers.len())
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

    // -- register + lookup --

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
        assert_eq!(find(&root, "/.tasks/other/t42/agent/status"), None);
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
        // Literal should win for .tasks
        assert_eq!(find(&root, "/.tasks/proc/p1/status"), Some(1));
        // Wildcard for other zones
        assert_eq!(find(&root, "/zone/proc/p1/status"), Some(0));
    }

    // -- unregister --

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

    // -- edge cases --

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
    fn test_wildcard_backtrack() {
        // /{}/proc/{}/status and /.tasks/tasks/{}/agent/status
        // Path /.tasks/proc/123/status should match the wildcard pattern
        // because .tasks literal child has no "proc" subtree.
        let mut root = TrieNode::new();
        insert(&mut root, "/{}/proc/{}/status", 0);
        insert(&mut root, "/.tasks/tasks/{}/agent/status", 1);
        assert_eq!(find(&root, "/.tasks/proc/123/status"), Some(0));
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
}

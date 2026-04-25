//! KernelDispatch — pure Rust dispatch traits + PathTrie.
//!
//! Zero PyO3 dependency. All Python-dependent types (InterceptHook trait,
//! HookRegistry, ObserverRegistry) live in hook_registry.rs.
//!
//! Contains:
//!   - PathResolver: virtual path short-circuit (PRE-DISPATCH phase, procfs-style)
//!   - MutationObserver: fire-and-forget event notification (OBSERVE phase, fsnotify-style)
//!   - FileEvent / FileEventType: kernel I/O event mirror of Python `FileEvent`
//!   - PathTrie: O(path_depth) lookup (~50ns) for virtual path resolvers
//!
//! Issue #1868: PR 22 — dispatch.rs fully pure Rust.

use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};

// ── FileEvent / FileEventType ────────────────────────────────────────
//
// Rust mirror of `nexus.core.file_events.FileEvent` (Python frozen
// dataclass). The struct is constructed by Rust kernel sys_* methods and
// passed to `MutationObserver::on_mutation`. Fields and bitmask positions
// MUST match the Python definitions exactly — see file_events.py and the
// `FILE_EVENT_BIT` table in the same module.
//
// `FileEventType` is `repr(u32)` and the discriminants are exactly the
// bit positions used by `ObserverRegistry::event_mask`. This lets the
// dispatch loop do `(event_type as u32) & mask != 0` directly without a
// lookup table.
//
// Linux analogue: `fsnotify_event` carries a single event-type tag plus
// path metadata; consumers extract what they need.

/// Kernel file-system event type.
///
/// Bit positions are the source of truth — Python's `FILE_EVENT_BIT` table
/// is generated from the same positions. Adding a new variant requires
/// updating both this enum and `nexus.core.file_events.FILE_EVENT_BIT`.
#[allow(dead_code)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u32)]
pub enum FileEventType {
    FileWrite = 1 << 0,
    FileDelete = 1 << 1,
    FileRename = 1 << 2,
    MetadataChange = 1 << 3,
    DirCreate = 1 << 4,
    DirDelete = 1 << 5,
    ConflictDetected = 1 << 6,
    FileCopy = 1 << 7,
    Mount = 1 << 8,
    Unmount = 1 << 9,
}

#[allow(dead_code)]
impl FileEventType {
    /// Event-type bitmask matching `ObserverRegistry::event_mask` filters.
    #[inline]
    pub(crate) fn bit(self) -> u32 {
        self as u32
    }

    /// Stable string identifier matching the Python `FileEventType` StrEnum
    /// (`FileEventType.FILE_WRITE.value == "file_write"`). The boundary
    /// adapter passes this to Python so reconstructed `FileEvent` objects
    /// have the right `type` field.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            FileEventType::FileWrite => "file_write",
            FileEventType::FileDelete => "file_delete",
            FileEventType::FileRename => "file_rename",
            FileEventType::MetadataChange => "metadata_change",
            FileEventType::DirCreate => "dir_create",
            FileEventType::DirDelete => "dir_delete",
            FileEventType::ConflictDetected => "conflict_detected",
            FileEventType::FileCopy => "file_copy",
            FileEventType::Mount => "mount",
            FileEventType::Unmount => "unmount",
        }
    }
}

/// Bitmask matching `nexus.core.file_events.ALL_FILE_EVENTS`.
///
/// Computed as `(1 << N) - 1` where N is the number of variants. Adding
/// a new variant requires bumping the shift here and in Python.
#[allow(dead_code)]
pub(crate) const ALL_FILE_EVENTS: u32 = (1 << 10) - 1;

/// Kernel file-system event — Rust mirror of `nexus.core.file_events.FileEvent`.
///
/// Constructed by sys_* methods after a successful mutation, then passed
/// to `MutationObserver::on_mutation`. The struct is `Clone` so the OBSERVE
/// ThreadPool dispatch can hand each observer its own owned copy without
/// sharing references across threads.
///
/// Fields mirror the Python frozen dataclass field-by-field; see
/// `file_events.py`. Optional Python fields map to `Option<T>`. Strings
/// (`event_id`, `timestamp` ISO 8601) are stored as owned `String` so the
/// boundary adapter can clone cheaply.
#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct FileEvent {
    /// Event type — strongly typed; the boundary adapter converts to the
    /// Python `FileEventType` StrEnum.
    pub(crate) event_type: FileEventType,
    /// Primary path (for `file_rename`, this is the *old* path; the new
    /// path lives in `new_path`, mirroring the Python field naming).
    pub(crate) path: String,
    /// Kernel namespace partition; `None` for Layer 1 local events.
    pub(crate) zone_id: Option<String>,
    /// ISO 8601 timestamp (kept as string to match Python serialization).
    /// Generated at construction site if not provided.
    pub(crate) timestamp: String,
    /// UUID v4 string — generated at construction site.
    pub(crate) event_id: String,
    /// For renames: the source path is in `path`, destination in `new_path`.
    /// Some test/event consumers also stash a previous path here.
    pub(crate) old_path: Option<String>,
    pub(crate) size: Option<u64>,
    pub(crate) etag: Option<String>,
    pub(crate) agent_id: Option<String>,
    pub(crate) vector_clock: Option<String>,
    /// Monotonic ordering within a zone (#2755).
    pub(crate) sequence_number: Option<u64>,
    pub(crate) user_id: Option<String>,
    /// Write-specific: file version counter.
    pub(crate) version: Option<u32>,
    /// Write-specific: True if file was newly created (not overwritten).
    pub(crate) is_new: bool,
    /// Rename-specific: destination path.
    pub(crate) new_path: Option<String>,
    /// Write-specific: previous content hash (for overwrite detection).
    pub(crate) old_etag: Option<String>,
}

#[allow(dead_code)]
impl FileEvent {
    /// Serialize to compact JSON for DT_STREAM / audit trail.
    ///
    /// Uses `serde_json` so arbitrary control characters in path/new_path
    /// (e.g. `\n`, `\0`) produce valid JSON (§ review fix #6). Unset
    /// `Option` fields are omitted to match the previous wire shape; scalar
    /// fields continue to use the compact ordering the Python parsers expect.
    pub(crate) fn to_json(&self) -> String {
        let mut map = serde_json::Map::with_capacity(12);
        map.insert(
            "type".to_string(),
            serde_json::Value::String(self.event_type.as_str().to_string()),
        );
        map.insert(
            "path".to_string(),
            serde_json::Value::String(self.path.clone()),
        );
        map.insert(
            "event_id".to_string(),
            serde_json::Value::String(self.event_id.clone()),
        );
        map.insert(
            "timestamp".to_string(),
            serde_json::Value::String(self.timestamp.clone()),
        );
        if let Some(ref v) = self.zone_id {
            map.insert("zone_id".to_string(), serde_json::Value::String(v.clone()));
        }
        if let Some(ref v) = self.agent_id {
            map.insert("agent_id".to_string(), serde_json::Value::String(v.clone()));
        }
        if let Some(ref v) = self.user_id {
            map.insert("user_id".to_string(), serde_json::Value::String(v.clone()));
        }
        if let Some(ref v) = self.etag {
            map.insert("etag".to_string(), serde_json::Value::String(v.clone()));
        }
        if let Some(v) = self.size {
            map.insert("size".to_string(), serde_json::Value::from(v));
        }
        if let Some(v) = self.version {
            map.insert("version".to_string(), serde_json::Value::from(v));
        }
        if self.is_new {
            map.insert("is_new".to_string(), serde_json::Value::Bool(true));
        }
        if let Some(ref v) = self.old_path {
            map.insert("old_path".to_string(), serde_json::Value::String(v.clone()));
        }
        if let Some(ref v) = self.new_path {
            map.insert("new_path".to_string(), serde_json::Value::String(v.clone()));
        }
        serde_json::to_string(&serde_json::Value::Object(map))
            .unwrap_or_else(|_| String::from("{\"error\":\"serialization failed\"}"))
    }

    /// Minimal-constructor convenience for sys_* call sites that only
    /// know `(type, path, zone_id)`. Auto-generates `event_id` and
    /// `timestamp`. Other fields default to None / false.
    pub(crate) fn new(event_type: FileEventType, path: impl Into<String>) -> Self {
        Self {
            event_type,
            path: path.into(),
            zone_id: None,
            timestamp: now_iso8601(),
            event_id: new_event_id(),
            old_path: None,
            size: None,
            etag: None,
            agent_id: None,
            vector_clock: None,
            sequence_number: None,
            user_id: None,
            version: None,
            is_new: false,
            new_path: None,
            old_etag: None,
        }
    }
}

/// Generate a UUID v4 string matching Python `str(uuid.uuid4())`.
///
/// Uses `uuid` crate (already a transitive dep via redb). Kept private to
/// the dispatch module so callers always go through `FileEvent::new`.
fn new_event_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// ISO 8601 timestamp with `+00:00` UTC suffix matching Python
/// `datetime.now(UTC).isoformat()` exactly.
///
/// Note: we pass `use_z=false` to chrono so the suffix is `+00:00`, not
/// `Z`. Python's `datetime.isoformat()` always uses the `+HH:MM` form;
/// the boundary adapter passes this string directly to Python where it
/// must round-trip through `datetime.fromisoformat()`.
fn now_iso8601() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
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
/// Rust equivalent of Python `VFSObserver`. Receives a frozen
/// `FileEvent` after every successful mutation. Never aborts: the
/// dispatcher catches and logs any panic.
///
/// Contract: OBSERVE is fire-and-forget by definition. The dispatch
/// loop submits each call to the kernel's background `ThreadPool`
/// (`Kernel::observer_pool`) so `on_mutation` runs **off** the syscall
/// hot path. There is no other mode — `OBSERVE_INLINE` (the legacy
/// "run on caller's thread" flag) was deleted in §11 Phase 2 because
/// inline observers were functionally identical to INTERCEPT POST hooks
/// and violated dispatch-contract orthogonality. Linux's analogous
/// primitive (fsnotify) makes the same choice: fire-and-forget only.
/// Observers needing causal ordering or sync blocking on the syscall
/// return path belong in INTERCEPT POST, not OBSERVE.
#[allow(dead_code)]
pub trait MutationObserver: Send + Sync {
    fn on_mutation(&self, event: &FileEvent);
}

// ── INTERCEPT hook context structs (§11 Phase 9) ─────────────────────
//
// Pure Rust equivalents of Python vfs_hooks dataclasses.
// Used by InterceptHook trait — eliminates GIL crossing for Rust hooks.
// Python hooks use PyInterceptHookAdapter which converts to Py<PyAny>.

/// Caller identity extracted from OperationContext.
#[derive(Debug, Clone, Default)]
#[allow(dead_code)]
pub struct HookIdentity {
    pub user_id: String,
    pub zone_id: String,
    pub agent_id: String,
    pub is_admin: bool,
}

/// ReadHookContext — pre/post read intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct ReadHookCtx {
    pub path: String,
    pub identity: HookIdentity,
    pub content: Option<Vec<u8>>,
    pub content_hash: Option<String>,
}

/// WriteHookContext — pre/post write intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct WriteHookCtx {
    pub path: String,
    pub identity: HookIdentity,
    /// Both pre- and post-hook: empty vec. Hooks that inspect write content
    /// (e.g. DLP) must opt in explicitly — no content is ever cloned here.
    /// Use `size_bytes` for byte-count metadata in post-hook context.
    pub content: Vec<u8>,
    pub is_new_file: bool,
    pub content_hash: Option<String>,
    pub new_version: u64,
    /// Populated in post-hook context; None in pre-hook.
    pub size_bytes: Option<u64>,
}

/// DeleteHookContext — pre/post delete intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct DeleteHookCtx {
    pub path: String,
    pub identity: HookIdentity,
}

/// RenameHookContext — pre/post rename intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct RenameHookCtx {
    pub old_path: String,
    pub new_path: String,
    pub identity: HookIdentity,
    pub is_directory: bool,
}

/// MkdirHookContext — pre/post mkdir intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct MkdirHookCtx {
    pub path: String,
    pub identity: HookIdentity,
}

/// RmdirHookContext — pre/post rmdir intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct RmdirHookCtx {
    pub path: String,
    pub identity: HookIdentity,
    pub recursive: bool,
}

/// CopyHookContext — pre/post copy intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct CopyHookCtx {
    pub src_path: String,
    pub dst_path: String,
    pub identity: HookIdentity,
}

/// StatHookContext — pre/post stat intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct StatHookCtx {
    pub path: String,
    pub identity: HookIdentity,
    /// "TRAVERSE" or "READ"
    pub permission: String,
}

/// AccessHookContext — pre/post access intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct AccessHookCtx {
    pub path: String,
    pub identity: HookIdentity,
    /// "TRAVERSE" or "READ"
    pub permission: String,
}

/// WriteBatchHookContext — pre/post write_batch intercept.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct WriteBatchHookCtx {
    pub paths: Vec<String>,
    pub identity: HookIdentity,
}

/// Enum dispatching all hook context types for the InterceptHook trait.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub enum HookContext {
    Read(ReadHookCtx),
    Write(WriteHookCtx),
    Delete(DeleteHookCtx),
    Rename(RenameHookCtx),
    Mkdir(MkdirHookCtx),
    Rmdir(RmdirHookCtx),
    Copy(CopyHookCtx),
    Stat(StatHookCtx),
    Access(AccessHookCtx),
    WriteBatch(WriteBatchHookCtx),
}

#[allow(dead_code)]
impl HookContext {
    /// Extract the path from any context variant.
    pub fn path(&self) -> &str {
        match self {
            Self::Read(c) => &c.path,
            Self::Write(c) => &c.path,
            Self::Delete(c) => &c.path,
            Self::Rename(c) => &c.old_path,
            Self::Mkdir(c) => &c.path,
            Self::Rmdir(c) => &c.path,
            Self::Copy(c) => &c.src_path,
            Self::Stat(c) => &c.path,
            Self::Access(c) => &c.path,
            Self::WriteBatch(c) => c.paths.first().map(|s| s.as_str()).unwrap_or(""),
        }
    }

    /// Extract identity from any context variant.
    pub fn identity(&self) -> &HookIdentity {
        match self {
            Self::Read(c) => &c.identity,
            Self::Write(c) => &c.identity,
            Self::Delete(c) => &c.identity,
            Self::Rename(c) => &c.identity,
            Self::Mkdir(c) => &c.identity,
            Self::Rmdir(c) => &c.identity,
            Self::Copy(c) => &c.identity,
            Self::Stat(c) => &c.identity,
            Self::Access(c) => &c.identity,
            Self::WriteBatch(c) => &c.identity,
        }
    }
}

// ── INTERCEPT hook trait (§11 Phase 10) ──────────────────────────────
//
// Pure Rust hook trait — no GIL crossing for Rust-native hooks.
// Python hooks wrapped via PyInterceptHookAdapter (generated_pyo3.rs)
// which converts HookContext → Py<PyAny> for backward compat.

/// INTERCEPT hook — called before/after each syscall.
///
/// Pre-hooks can abort by returning Err (message becomes PermissionError).
/// Post-hooks are fire-and-forget (errors logged, never abort).
///
/// Default implementation: no-op for all operations.
#[allow(dead_code)]
pub(crate) trait NativeInterceptHook: Send + Sync {
    fn name(&self) -> &str;

    /// Pre-intercept: return Err(message) to abort the operation.
    fn on_pre(&self, _ctx: &HookContext) -> Result<(), String> {
        Ok(())
    }

    /// Post-intercept: fire-and-forget after operation completes.
    fn on_post(&self, _ctx: &HookContext) {}
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

// ── Tests (TrieNode only — pure Rust, no PyO3) ────────────────────────

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

    // ── FileEvent / FileEventType bit-position mirror tests ───────────
    //
    // These assertions are the load-bearing contract that Rust observers
    // see exactly the same event bits as the Python `FILE_EVENT_BIT`
    // table. If a new event type is added on either side, both tables
    // must be updated together — a mismatch will silently mis-route
    // events, so we pin the values explicitly here.

    #[test]
    fn test_file_event_type_bit_positions_match_python() {
        assert_eq!(FileEventType::FileWrite.bit(), 1 << 0);
        assert_eq!(FileEventType::FileDelete.bit(), 1 << 1);
        assert_eq!(FileEventType::FileRename.bit(), 1 << 2);
        assert_eq!(FileEventType::MetadataChange.bit(), 1 << 3);
        assert_eq!(FileEventType::DirCreate.bit(), 1 << 4);
        assert_eq!(FileEventType::DirDelete.bit(), 1 << 5);
        assert_eq!(FileEventType::ConflictDetected.bit(), 1 << 6);
        assert_eq!(FileEventType::FileCopy.bit(), 1 << 7);
        assert_eq!(FileEventType::Mount.bit(), 1 << 8);
        assert_eq!(FileEventType::Unmount.bit(), 1 << 9);
    }

    #[test]
    fn test_all_file_events_mask() {
        // Must equal `nexus.core.file_events.ALL_FILE_EVENTS == (1 << 10) - 1`.
        assert_eq!(ALL_FILE_EVENTS, 0x3FF);
        // Every variant bit must be present in the all-mask.
        let bits = [
            FileEventType::FileWrite.bit(),
            FileEventType::FileDelete.bit(),
            FileEventType::FileRename.bit(),
            FileEventType::MetadataChange.bit(),
            FileEventType::DirCreate.bit(),
            FileEventType::DirDelete.bit(),
            FileEventType::ConflictDetected.bit(),
            FileEventType::FileCopy.bit(),
            FileEventType::Mount.bit(),
            FileEventType::Unmount.bit(),
        ];
        let or_all: u32 = bits.iter().fold(0, |acc, b| acc | b);
        assert_eq!(or_all, ALL_FILE_EVENTS);
    }

    #[test]
    fn test_file_event_type_str_matches_python_strenum() {
        // String values must match Python `FileEventType(StrEnum)` `.value`
        // — these strings cross the PyO3 boundary verbatim and feed into
        // the reconstructed Python `FileEvent`.
        assert_eq!(FileEventType::FileWrite.as_str(), "file_write");
        assert_eq!(FileEventType::FileDelete.as_str(), "file_delete");
        assert_eq!(FileEventType::FileRename.as_str(), "file_rename");
        assert_eq!(FileEventType::MetadataChange.as_str(), "metadata_change");
        assert_eq!(FileEventType::DirCreate.as_str(), "dir_create");
        assert_eq!(FileEventType::DirDelete.as_str(), "dir_delete");
        assert_eq!(
            FileEventType::ConflictDetected.as_str(),
            "conflict_detected"
        );
        assert_eq!(FileEventType::FileCopy.as_str(), "file_copy");
        assert_eq!(FileEventType::Mount.as_str(), "mount");
        assert_eq!(FileEventType::Unmount.as_str(), "unmount");
    }

    #[test]
    fn test_file_event_new_minimal_constructor() {
        let ev = FileEvent::new(FileEventType::FileWrite, "/foo/bar.txt");
        assert_eq!(ev.event_type, FileEventType::FileWrite);
        assert_eq!(ev.path, "/foo/bar.txt");
        // event_id is a UUIDv4 (36 chars including dashes).
        assert_eq!(ev.event_id.len(), 36);
        // Timestamp must mirror Python `datetime.now(UTC).isoformat()` —
        // ends with `+00:00`, not `Z`.
        assert!(ev.timestamp.ends_with("+00:00"));
        assert!(ev.timestamp.contains('T'));
        // All optional fields default to None / false.
        assert!(ev.zone_id.is_none());
        assert!(ev.size.is_none());
        assert!(ev.etag.is_none());
        assert!(!ev.is_new);
    }
}

#![allow(dead_code)]

use std::sync::Arc;

use contracts::{OperationContext, WriteCoalescingPolicy};
use dashmap::mapref::entry::Entry;
use dashmap::DashMap;

use crate::abc::object_store::ObjectStore;
use crate::meta_store::{FileMetadata, MetaStore};
use crate::vfs_router::RouteResult;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct DirtyWriteKey {
    pub path: String,
    pub zone_id: String,
}

impl DirtyWriteKey {
    pub(crate) fn new(path: &str, zone_id: &str) -> Self {
        Self {
            path: path.to_string(),
            zone_id: zone_id.to_string(),
        }
    }
}

#[derive(Clone)]
pub(crate) struct DirtyWriteRoute {
    pub path: String,
    pub backend_path: String,
    pub mount_point: String,
    pub zone_id: String,
    pub is_external: bool,
    pub is_cas: bool,
    pub metastore: Option<Arc<dyn MetaStore>>,
    pub backend: Option<Arc<dyn ObjectStore>>,
}

impl std::fmt::Debug for DirtyWriteRoute {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DirtyWriteRoute")
            .field("path", &self.path)
            .field("backend_path", &self.backend_path)
            .field("mount_point", &self.mount_point)
            .field("zone_id", &self.zone_id)
            .field("is_external", &self.is_external)
            .field("is_cas", &self.is_cas)
            .field("metastore", &self.metastore.is_some())
            .field("backend", &self.backend.is_some())
            .finish()
    }
}

impl DirtyWriteRoute {
    pub(crate) fn new(path: &str, backend_path: &str, mount_point: &str) -> Self {
        Self {
            path: path.to_string(),
            backend_path: backend_path.to_string(),
            mount_point: mount_point.to_string(),
            zone_id: "root".to_string(),
            is_external: false,
            is_cas: false,
            metastore: None,
            backend: None,
        }
    }

    pub(crate) fn from_route(path: &str, route: &RouteResult) -> Self {
        Self {
            path: path.to_string(),
            backend_path: route.backend_path.clone(),
            mount_point: route.mount_point.clone(),
            zone_id: route.zone_id.clone(),
            is_external: route.is_external,
            is_cas: route.is_cas,
            metastore: route.metastore.clone(),
            backend: route.backend.clone(),
        }
    }

    pub(crate) fn to_route_result(&self) -> RouteResult {
        RouteResult {
            mount_point: self.mount_point.clone(),
            backend_path: self.backend_path.clone(),
            zone_id: self.zone_id.clone(),
            is_external: self.is_external,
            is_cas: self.is_cas,
            metastore: self.metastore.clone(),
            backend: self.backend.clone(),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct DirtyWrite {
    pub key: DirtyWriteKey,
    pub route: DirtyWriteRoute,
    pub context: OperationContext,
    pub content: Vec<u8>,
    pub old_metadata: Option<FileMetadata>,
    pub policy: WriteCoalescingPolicy,
    pub first_dirty_at_ms: u64,
    pub last_dirty_at_ms: u64,
    pub generation: u64,
    pub flushing_generation: Option<u64>,
}

impl DirtyWrite {
    pub(crate) fn dirty_bytes(&self) -> usize {
        self.content.len()
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct FlushSelection {
    pub path: Option<String>,
    pub zone_id: Option<String>,
}

impl FlushSelection {
    pub(crate) fn matches(&self, dirty: &DirtyWrite) -> bool {
        let path_matches = self
            .path
            .as_ref()
            .map(|p| prefix_matches_path(&normalize_prefix(p), &dirty.key.path))
            .unwrap_or(true);
        let zone_matches = self
            .zone_id
            .as_ref()
            .map(|z| dirty.key.zone_id == *z)
            .unwrap_or(true);
        path_matches && zone_matches
    }
}

#[derive(Default)]
pub(crate) struct WriteBuffer {
    dirty: DashMap<DirtyWriteKey, DirtyWrite>,
    policies: DashMap<String, WriteCoalescingPolicy>,
}

impl WriteBuffer {
    pub(crate) fn new() -> Self {
        let buffer = Self::default();
        buffer.set_policy("/", WriteCoalescingPolicy::strict());
        buffer
    }

    pub(crate) fn set_policy(&self, prefix: &str, policy: WriteCoalescingPolicy) {
        self.policies.insert(normalize_prefix(prefix), policy);
    }

    pub(crate) fn policy_for(&self, path: &str) -> WriteCoalescingPolicy {
        let mut best: Option<(usize, WriteCoalescingPolicy)> = None;
        for item in self.policies.iter() {
            let prefix = item.key();
            if prefix_matches_path(prefix, path) {
                let len = prefix.len();
                if best
                    .as_ref()
                    .map(|(best_len, _)| len > *best_len)
                    .unwrap_or(true)
                {
                    best = Some((len, item.value().clone()));
                }
            }
        }
        best.map(|(_, policy)| policy).unwrap_or_default()
    }

    pub(crate) fn dirty_len(&self) -> usize {
        self.dirty.len()
    }

    pub(crate) fn get_dirty_bytes(&self, path: &str, zone_id: &str) -> Option<Vec<u8>> {
        self.dirty
            .get(&DirtyWriteKey::new(path, zone_id))
            .map(|entry| entry.content.clone())
    }

    pub(crate) fn get_dirty(&self, key: &DirtyWriteKey) -> Option<DirtyWrite> {
        self.dirty.get(key).map(|entry| entry.value().clone())
    }

    pub(crate) fn contains_dirty_key(&self, key: &DirtyWriteKey) -> bool {
        self.dirty.contains_key(key)
    }

    pub(crate) fn selected_dirty(&self, selection: &FlushSelection) -> Vec<DirtyWrite> {
        let mut items: Vec<_> = self
            .dirty
            .iter()
            .filter(|entry| selection.matches(entry.value()))
            .map(|entry| entry.value().clone())
            .collect();
        items.sort_by(|a, b| {
            a.key
                .path
                .cmp(&b.key.path)
                .then(a.key.zone_id.cmp(&b.key.zone_id))
        });
        items
    }

    pub(crate) fn due_dirty(&self, now_ms: u64) -> Vec<DirtyWrite> {
        let mut items: Vec<_> = self
            .dirty
            .iter()
            .filter(|entry| {
                let dirty = entry.value();
                let policy = &dirty.policy;
                policy.enabled()
                    && policy.flush_window_ms > 0
                    && dirty.flushing_generation.is_none()
                    && now_ms.saturating_sub(dirty.last_dirty_at_ms) >= policy.flush_window_ms
            })
            .map(|entry| entry.value().clone())
            .collect();
        items.sort_by(|a, b| {
            a.key
                .path
                .cmp(&b.key.path)
                .then(a.key.zone_id.cmp(&b.key.zone_id))
        });
        items
    }

    pub(crate) fn remove_if_content_matches(&self, dirty: &DirtyWrite) -> bool {
        self.dirty
            .remove_if(&dirty.key, |_key, current| current.content == dirty.content)
            .is_some()
    }

    pub(crate) fn claim_dirty_generation(&self, dirty: &DirtyWrite) -> bool {
        let Some(mut current) = self.dirty.get_mut(&dirty.key) else {
            return false;
        };
        if current.generation != dirty.generation || current.flushing_generation.is_some() {
            return false;
        }
        current.flushing_generation = Some(dirty.generation);
        true
    }

    pub(crate) fn unclaim_dirty_generation(&self, dirty: &DirtyWrite) -> bool {
        let Some(mut current) = self.dirty.get_mut(&dirty.key) else {
            return false;
        };
        if current.generation != dirty.generation
            || current.flushing_generation != Some(dirty.generation)
        {
            return false;
        }
        current.flushing_generation = None;
        true
    }

    pub(crate) fn remove_if_generation_matches(&self, dirty: &DirtyWrite) -> bool {
        self.dirty
            .remove_if(&dirty.key, |_key, current| {
                current.generation == dirty.generation
                    && current.flushing_generation == Some(dirty.generation)
            })
            .is_some()
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn merge_write(
        &self,
        key: DirtyWriteKey,
        route: DirtyWriteRoute,
        old_metadata: Option<FileMetadata>,
        bytes: &[u8],
        offset: u64,
        policy: WriteCoalescingPolicy,
        now_ms: u64,
    ) -> Result<usize, String> {
        let context = fallback_write_buffer_context(&key.zone_id);
        self.merge_write_with_base_and_context(
            key,
            route,
            context,
            old_metadata,
            Vec::new(),
            bytes,
            offset,
            policy,
            now_ms,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn merge_write_with_base(
        &self,
        key: DirtyWriteKey,
        route: DirtyWriteRoute,
        old_metadata: Option<FileMetadata>,
        base_content: Vec<u8>,
        bytes: &[u8],
        offset: u64,
        policy: WriteCoalescingPolicy,
        now_ms: u64,
    ) -> Result<usize, String> {
        let context = fallback_write_buffer_context(&key.zone_id);
        self.merge_write_with_base_and_context(
            key,
            route,
            context,
            old_metadata,
            base_content,
            bytes,
            offset,
            policy,
            now_ms,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn merge_write_with_base_and_context(
        &self,
        key: DirtyWriteKey,
        route: DirtyWriteRoute,
        context: OperationContext,
        old_metadata: Option<FileMetadata>,
        base_content: Vec<u8>,
        bytes: &[u8],
        offset: u64,
        policy: WriteCoalescingPolicy,
        now_ms: u64,
    ) -> Result<usize, String> {
        let start = usize::try_from(offset)
            .map_err(|_| format!("write offset {offset} does not fit usize"))?;
        let end = start.checked_add(bytes.len()).ok_or_else(|| {
            format!(
                "write range overflows usize: offset={offset}, len={}",
                bytes.len()
            )
        })?;

        match self.dirty.entry(key.clone()) {
            Entry::Occupied(mut entry) => {
                let dirty = entry.get_mut();
                let next_generation = dirty.generation.saturating_add(1);
                merge_content(&mut dirty.content, bytes, offset, start, end)?;
                dirty.key = key;
                dirty.route = route;
                dirty.context = context;
                dirty.policy = policy;
                dirty.last_dirty_at_ms = now_ms;
                dirty.generation = next_generation;
                dirty.flushing_generation = None;
                Ok(dirty.content.len())
            }
            Entry::Vacant(entry) => {
                let mut content = base_content;
                merge_content(&mut content, bytes, offset, start, end)?;
                let size = content.len();
                entry.insert(DirtyWrite {
                    key,
                    route,
                    context,
                    content,
                    old_metadata,
                    policy,
                    first_dirty_at_ms: now_ms,
                    last_dirty_at_ms: now_ms,
                    generation: 1,
                    flushing_generation: None,
                });
                Ok(size)
            }
        }
    }
}

fn fallback_write_buffer_context(zone_id: &str) -> OperationContext {
    OperationContext::new("write-buffer", zone_id, true, None, true)
}

fn normalize_prefix(prefix: &str) -> String {
    let trimmed = prefix.trim_end_matches('/');
    if trimmed.is_empty() {
        "/".to_string()
    } else {
        trimmed.to_string()
    }
}

fn prefix_matches_path(prefix: &str, path: &str) -> bool {
    if prefix == "/" {
        return true;
    }

    path == prefix
        || path
            .strip_prefix(prefix)
            .map(|suffix| suffix.starts_with('/'))
            .unwrap_or(false)
}

fn merge_content(
    content: &mut Vec<u8>,
    bytes: &[u8],
    offset: u64,
    start: usize,
    end: usize,
) -> Result<(), String> {
    if offset == 0 {
        content.clear();
        content.extend_from_slice(bytes);
        return Ok(());
    }

    if content.len() < start {
        content
            .try_reserve(start - content.len())
            .map_err(|err| format!("failed to reserve sparse write gap: {err}"))?;
        content.resize(start, 0);
    }
    if content.len() < end {
        content
            .try_reserve(end - content.len())
            .map_err(|err| format!("failed to reserve write bytes: {err}"))?;
        content.resize(end, 0);
    }
    content[start..end].copy_from_slice(bytes);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use contracts::WriteCoalescingPolicy;

    fn meta(
        path: &str,
        content_id: &str,
        size: u64,
        version: u32,
    ) -> crate::meta_store::FileMetadata {
        crate::meta_store::FileMetadata {
            path: path.to_string(),
            size,
            content_id: Some(content_id.to_string()),
            gen: version as u64,
            version,
            entry_type: crate::meta_store::DT_REG,
            zone_id: Some("root".to_string()),
            mime_type: None,
            created_at_ms: Some(100),
            modified_at_ms: Some(200),
            last_writer_address: None,
            target_zone_id: None,
            link_target: None,
            owner_id: None,
        }
    }

    #[test]
    fn full_writes_replace_dirty_bytes() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer
            .merge_write(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"hello",
                0,
                policy.clone(),
                10,
            )
            .unwrap();
        buffer
            .merge_write(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"bye",
                0,
                policy,
                20,
            )
            .unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"bye");
        assert_eq!(buffer.dirty_len(), 1);
    }

    #[test]
    fn partial_write_splices_clean_bytes() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer
            .merge_write_with_base(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                Some(meta("/workspace/a.txt", "old", 5, 7)),
                b"hello".to_vec(),
                b"XX",
                1,
                policy,
                10,
            )
            .unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"hXXlo");
    }

    #[test]
    fn sparse_partial_write_zero_fills_gap() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer
            .merge_write_with_base(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                Some(meta("/workspace/a.txt", "old", 2, 7)),
                b"hi".to_vec(),
                b"!",
                4,
                policy,
                10,
            )
            .unwrap();

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        assert_eq!(dirty, b"hi\0\0!");
    }

    #[test]
    fn later_merges_preserve_original_metadata_snapshot() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        let key = DirtyWriteKey::new("/workspace/a.txt", "root");
        buffer
            .merge_write_with_base(
                key.clone(),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                Some(meta("/workspace/a.txt", "old", 5, 7)),
                b"hello".to_vec(),
                b"XX",
                1,
                policy.clone(),
                10,
            )
            .unwrap();
        buffer
            .merge_write_with_base(
                key.clone(),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                Some(meta("/workspace/a.txt", "newer", 5, 99)),
                b"ignored".to_vec(),
                b"YY",
                3,
                policy,
                20,
            )
            .unwrap();

        let dirty = buffer.dirty.get(&key).unwrap();
        assert_eq!(dirty.content, b"hXXYY");
        assert_eq!(dirty.old_metadata.as_ref().unwrap().version, 7);
        assert_eq!(
            dirty.old_metadata.as_ref().unwrap().content_id.as_deref(),
            Some("old")
        );
    }

    #[test]
    fn generation_safe_removal_keeps_newer_same_bytes() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        let key = DirtyWriteKey::new("/workspace/a.txt", "root");
        buffer
            .merge_write(
                key.clone(),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"same",
                0,
                policy.clone(),
                10,
            )
            .unwrap();

        let selection = FlushSelection {
            path: Some("/workspace/a.txt".to_string()),
            zone_id: Some("root".to_string()),
        };
        let dirty = buffer.selected_dirty(&selection).pop().unwrap();
        assert!(buffer.claim_dirty_generation(&dirty));

        buffer
            .merge_write(
                key,
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"same",
                0,
                policy,
                20,
            )
            .unwrap();

        assert!(!buffer.remove_if_generation_matches(&dirty));
        let current = buffer.selected_dirty(&selection).pop().unwrap();
        assert_eq!(current.generation, dirty.generation + 1);
        assert_eq!(current.content, b"same");
    }

    #[test]
    fn stale_due_snapshot_does_not_claim_fresh_generation() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        let key = DirtyWriteKey::new("/workspace/a.txt", "root");
        buffer
            .merge_write(
                key.clone(),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"old",
                0,
                policy.clone(),
                10,
            )
            .unwrap();

        let due = buffer.due_dirty(2_000);
        assert_eq!(due.len(), 1);
        let stale = due.into_iter().next().unwrap();

        buffer
            .merge_write(
                key.clone(),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"fresh",
                0,
                policy,
                2_001,
            )
            .unwrap();

        assert!(!buffer.claim_dirty_generation(&stale));
        assert!(!buffer.remove_if_generation_matches(&stale));
        let current = buffer
            .selected_dirty(&FlushSelection {
                path: Some("/workspace/a.txt".to_string()),
                zone_id: Some("root".to_string()),
            })
            .pop()
            .unwrap();
        assert_eq!(current.generation, stale.generation + 1);
        assert_eq!(current.content, b"fresh");
    }

    #[test]
    fn duplicate_flush_claims_skip_claimed_generation() {
        let buffer = WriteBuffer::new();
        let policy = WriteCoalescingPolicy::latency();
        buffer
            .merge_write(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                None,
                b"hello",
                0,
                policy,
                10,
            )
            .unwrap();

        let dirty = buffer
            .selected_dirty(&FlushSelection {
                path: Some("/workspace/a.txt".to_string()),
                zone_id: Some("root".to_string()),
            })
            .pop()
            .unwrap();

        assert!(buffer.claim_dirty_generation(&dirty));
        assert!(!buffer.claim_dirty_generation(&dirty));
        assert!(buffer.unclaim_dirty_generation(&dirty));
        assert!(buffer.claim_dirty_generation(&dirty));
    }

    #[test]
    fn prefix_policy_uses_longest_match() {
        let buffer = WriteBuffer::new();
        buffer.set_policy("/", WriteCoalescingPolicy::batch());
        buffer.set_policy("/workspace/latency", WriteCoalescingPolicy::latency());

        assert_eq!(
            buffer
                .policy_for("/workspace/latency/a.txt")
                .flush_window_ms,
            1_000
        );
        assert_eq!(
            buffer.policy_for("/workspace/other/a.txt").flush_window_ms,
            60_000
        );
    }

    #[test]
    fn workspace_prefix_does_not_match_workspace2() {
        let buffer = WriteBuffer::new();
        buffer.set_policy("/", WriteCoalescingPolicy::batch());
        buffer.set_policy("/workspace", WriteCoalescingPolicy::latency());

        assert_eq!(buffer.policy_for("/workspace/a.txt").flush_window_ms, 1_000);
        assert_eq!(
            buffer.policy_for("/workspace2/a.txt").flush_window_ms,
            60_000
        );
    }

    #[test]
    fn nested_prefix_does_not_match_same_text_sibling() {
        let buffer = WriteBuffer::new();
        buffer.set_policy("/", WriteCoalescingPolicy::batch());
        buffer.set_policy("/workspace/latency", WriteCoalescingPolicy::latency());

        assert_eq!(
            buffer
                .policy_for("/workspace/latency/a.txt")
                .flush_window_ms,
            1_000
        );
        assert_eq!(
            buffer
                .policy_for("/workspace/latency2/a.txt")
                .flush_window_ms,
            60_000
        );
    }

    #[test]
    fn trailing_slash_prefix_matches_child_paths() {
        let buffer = WriteBuffer::new();
        buffer.set_policy("/", WriteCoalescingPolicy::batch());
        buffer.set_policy("/workspace/latency/", WriteCoalescingPolicy::latency());

        assert_eq!(
            buffer
                .policy_for("/workspace/latency/a.txt")
                .flush_window_ms,
            1_000
        );
    }

    #[test]
    fn overflow_offset_returns_err_without_panicking() {
        let buffer = WriteBuffer::new();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            buffer.merge_write_with_base(
                DirtyWriteKey::new("/workspace/a.txt", "root"),
                DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                Some(meta("/workspace/a.txt", "old", 2, 7)),
                b"hi".to_vec(),
                b"!",
                u64::MAX,
                WriteCoalescingPolicy::latency(),
                10,
            )
        }));

        assert!(result.is_ok());
        assert!(result.unwrap().is_err());
    }

    #[test]
    fn concurrent_same_key_partial_writes_preserve_all_writes() {
        use std::sync::{Arc, Barrier};

        let buffer = Arc::new(WriteBuffer::new());
        let barrier = Arc::new(Barrier::new(17));
        let mut handles = Vec::new();

        for offset in 1..=16 {
            let buffer = Arc::clone(&buffer);
            let barrier = Arc::clone(&barrier);
            handles.push(std::thread::spawn(move || {
                barrier.wait();
                buffer
                    .merge_write_with_base(
                        DirtyWriteKey::new("/workspace/a.txt", "root"),
                        DirtyWriteRoute::new("/workspace/a.txt", "/a.txt", "/workspace"),
                        Some(meta("/workspace/a.txt", "old", 17, 7)),
                        vec![0; 17],
                        &[offset as u8],
                        offset,
                        WriteCoalescingPolicy::latency(),
                        10 + offset,
                    )
                    .unwrap();
            }));
        }

        barrier.wait();
        for handle in handles {
            handle.join().unwrap();
        }

        let dirty = buffer.get_dirty_bytes("/workspace/a.txt", "root").unwrap();
        let mut expected = vec![0];
        expected.extend(1u8..=16);
        assert_eq!(dirty, expected);
    }
}

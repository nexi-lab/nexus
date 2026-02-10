use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use fjall::{Database, Keyspace, KeyspaceCreateOptions, PersistMode};

use crate::error::{Result, TaskError};
use crate::priority::{
    decode_pending_key, decode_running_key, encode_pending_key, encode_running_key,
};
use crate::task::{TaskRecord, TaskStatus};

/// Fjall-backed task storage with 4 keyspaces (column families).
///
/// Keyspaces:
/// - `tasks`:       task_id (u64 BE)          -> TaskRecord (bincode)
/// - `pending_idx`: composite priority key     -> () (empty value)
/// - `running_idx`: [lease_expires][task_id]   -> () (empty value)
/// - `dead_letter`: task_id (u64 BE)           -> TaskRecord (bincode)
pub struct TaskStore {
    db: Database,
    tasks: Keyspace,
    pending_idx: Keyspace,
    running_idx: Keyspace,
    dead_letter: Keyspace,
    id_counter: AtomicU64,
}

impl TaskStore {
    /// Open or create the task store at the given path.
    pub fn open(path: &str) -> Result<Self> {
        let db = Database::builder(Path::new(path))
            .open()
            .map_err(|e| TaskError::Storage(e.to_string()))?;

        let tasks = db
            .keyspace("tasks", KeyspaceCreateOptions::default)
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        let pending_idx = db
            .keyspace("pending_idx", KeyspaceCreateOptions::default)
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        let running_idx = db
            .keyspace("running_idx", KeyspaceCreateOptions::default)
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        let dead_letter = db
            .keyspace("dead_letter", KeyspaceCreateOptions::default)
            .map_err(|e| TaskError::Storage(e.to_string()))?;

        // Initialize counter from existing max task_id
        let max_id = Self::find_max_task_id(&tasks);

        Ok(Self {
            db,
            tasks,
            pending_idx,
            running_idx,
            dead_letter,
            id_counter: AtomicU64::new(max_id + 1),
        })
    }

    /// Scan tasks keyspace to find highest existing task_id (for recovery).
    fn find_max_task_id(tasks: &Keyspace) -> u64 {
        let mut max_id = 0u64;
        for guard in tasks.iter() {
            if let Ok((key, _value)) = guard.into_inner() {
                let key_bytes: &[u8] = key.as_ref();
                if key_bytes.len() == 8 {
                    if let Ok(arr) = key_bytes.try_into() {
                        let id = u64::from_be_bytes(arr);
                        if id > max_id {
                            max_id = id;
                        }
                    }
                }
            }
        }
        max_id
    }

    /// Generate a monotonically increasing task ID.
    pub fn generate_id(&self) -> u64 {
        self.id_counter.fetch_add(1, Ordering::Relaxed)
    }

    /// Insert a new task. Atomically writes to both `tasks` and `pending_idx`.
    pub fn insert_task(&self, task: &TaskRecord) -> Result<()> {
        let task_key = task.task_id.to_be_bytes();
        let task_value = bincode::serialize(task)?;
        let pending_key = encode_pending_key(task.priority, task.run_at, task.task_id);

        let mut batch = self.db.batch();
        batch.insert(&self.tasks, task_key, task_value);
        batch.insert(&self.pending_idx, pending_key, vec![]);
        batch
            .commit()
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        Ok(())
    }

    /// Get a task by ID from the primary store.
    pub fn get_task(&self, task_id: u64) -> Result<Option<TaskRecord>> {
        let key = task_id.to_be_bytes();
        match self
            .tasks
            .get(key)
            .map_err(|e| TaskError::Storage(e.to_string()))?
        {
            Some(bytes) => {
                let record: TaskRecord = bincode::deserialize(bytes.as_ref())?;
                Ok(Some(record))
            }
            None => Ok(None),
        }
    }

    /// Update a task in the primary store.
    pub fn update_task(&self, task: &TaskRecord) -> Result<()> {
        let key = task.task_id.to_be_bytes();
        let value = bincode::serialize(task)?;
        self.tasks
            .insert(key, value)
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        Ok(())
    }

    /// Claim the next pending task. Atomically moves from pending_idx to running_idx.
    /// Returns None if no eligible tasks are available.
    pub fn claim_next(
        &self,
        worker_id: &str,
        lease_secs: u32,
        now: u64,
        max_wait_secs: u64,
    ) -> Result<Option<TaskRecord>> {
        // Check for anti-starvation: if the oldest low-priority task has waited too long, promote it
        let mut target_key = None;

        if max_wait_secs > 0 {
            // Check the last priority band (BestEffort = 4)
            let prefix_bytes = [4u8];
            if let Some(guard) = self.pending_idx.prefix(prefix_bytes).next() {
                if let Ok((key, _)) = guard.into_inner() {
                    if let Some((_, run_at, _)) = decode_pending_key(key.as_ref()) {
                        if crate::priority::should_promote_oldest(run_at, now, max_wait_secs) {
                            target_key = Some(key.as_ref().to_vec());
                        }
                    }
                }
            }
        }

        // Normal path: grab the first key in pending_idx (highest priority, earliest time)
        if target_key.is_none() {
            if let Some(guard) = self.pending_idx.first_key_value() {
                if let Ok((key, _)) = guard.into_inner() {
                    target_key = Some(key.as_ref().to_vec());
                }
            }
        }

        let Some(key_bytes) = target_key else {
            return Ok(None);
        };

        let Some((_, _, task_id)) = decode_pending_key(&key_bytes) else {
            return Ok(None);
        };

        // Load the task record
        let Some(mut task) = self.get_task(task_id)? else {
            // Stale index entry — remove and return None
            self.pending_idx
                .remove(&key_bytes)
                .map_err(|e| TaskError::Storage(e.to_string()))?;
            return Ok(None);
        };

        // Skip tasks scheduled for the future
        if task.run_at > now {
            return Ok(None);
        }

        // Update the task record
        let lease_expires = now + lease_secs as u64;
        task.status = TaskStatus::Running;
        task.claimed_at = Some(now);
        task.claimed_by = Some(worker_id.to_string());
        task.lease_secs = lease_secs;
        task.attempt += 1;

        let task_value = bincode::serialize(&task)?;
        let running_key = encode_running_key(lease_expires, task_id);

        // Atomic: remove from pending, add to running, update task
        let mut batch = self.db.batch();
        batch.remove(&self.pending_idx, &key_bytes);
        batch.insert(&self.running_idx, running_key, vec![]);
        batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
        batch
            .commit()
            .map_err(|e| TaskError::Storage(e.to_string()))?;

        Ok(Some(task))
    }

    /// Move a running task to completed state. Removes from running_idx.
    pub fn complete_task(&self, task_id: u64, result: &[u8], now: u64) -> Result<TaskRecord> {
        let mut task = self
            .get_task(task_id)?
            .ok_or(TaskError::NotFound(task_id))?;

        if task.status != TaskStatus::Running {
            return Err(TaskError::InvalidTransition {
                task_id,
                current: task.status.to_string(),
                target: "COMPLETED".to_string(),
            });
        }

        // Find and remove running index entry
        self.remove_from_running_idx(task_id)?;

        task.status = TaskStatus::Completed;
        task.result = Some(result.to_vec());
        task.completed_at = Some(now);

        self.update_task(&task)?;
        Ok(task)
    }

    /// Fail a running task. If retries remain, re-queue to pending; otherwise dead-letter.
    pub fn fail_task(
        &self,
        task_id: u64,
        error_message: &str,
        now: u64,
    ) -> Result<(TaskRecord, bool)> {
        let mut task = self
            .get_task(task_id)?
            .ok_or(TaskError::NotFound(task_id))?;

        if task.status != TaskStatus::Running {
            return Err(TaskError::InvalidTransition {
                task_id,
                current: task.status.to_string(),
                target: "FAILED".to_string(),
            });
        }

        // Remove from running index
        self.remove_from_running_idx(task_id)?;

        task.error_message = Some(error_message.to_string());

        let dead_lettered = crate::retry::should_dead_letter(task.attempt, task.max_retries);

        if dead_lettered {
            // Move to dead letter
            task.status = TaskStatus::DeadLetter;
            task.completed_at = Some(now);
            let task_value = bincode::serialize(&task)?;

            let mut batch = self.db.batch();
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value.clone());
            batch.insert(&self.dead_letter, task_id.to_be_bytes(), task_value);
            batch
                .commit()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
        } else {
            // Re-queue with backoff delay
            let delay = crate::retry::backoff_secs(task.attempt, task_id);
            task.status = TaskStatus::Pending;
            task.run_at = now + delay;
            task.claimed_at = None;
            task.claimed_by = None;

            let task_value = bincode::serialize(&task)?;
            let pending_key = encode_pending_key(task.priority, task.run_at, task_id);

            let mut batch = self.db.batch();
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
            batch.insert(&self.pending_idx, pending_key, vec![]);
            batch
                .commit()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
        }

        Ok((task, dead_lettered))
    }

    /// Cancel a task. Works for both pending and running tasks.
    pub fn cancel_task(&self, task_id: u64, now: u64) -> Result<TaskRecord> {
        let mut task = self
            .get_task(task_id)?
            .ok_or(TaskError::NotFound(task_id))?;

        match task.status {
            TaskStatus::Pending => {
                // Remove from pending index
                let pending_key = encode_pending_key(task.priority, task.run_at, task_id);
                self.pending_idx
                    .remove(pending_key)
                    .map_err(|e| TaskError::Storage(e.to_string()))?;
            }
            TaskStatus::Running => {
                // Remove from running index
                self.remove_from_running_idx(task_id)?;
            }
            _ => {
                return Err(TaskError::InvalidTransition {
                    task_id,
                    current: task.status.to_string(),
                    target: "CANCELLED".to_string(),
                });
            }
        }

        task.status = TaskStatus::Cancelled;
        task.completed_at = Some(now);
        self.update_task(&task)?;
        Ok(task)
    }

    /// Requeue tasks whose leases have expired. Returns count of requeued tasks.
    pub fn requeue_abandoned(&self, now: u64) -> Result<u32> {
        let mut count = 0u32;
        let upper_bound = encode_running_key(now, u64::MAX);

        // Collect expired entries first (avoid holding iterator across writes)
        let expired: Vec<(Vec<u8>, u64)> = self
            .running_idx
            .range(..=upper_bound)
            .filter_map(|guard| {
                let (key, _) = guard.into_inner().ok()?;
                let key_bytes = key.as_ref().to_vec();
                let (_, task_id) = decode_running_key(&key_bytes)?;
                Some((key_bytes, task_id))
            })
            .collect();

        for (running_key, task_id) in expired {
            let Some(mut task) = self.get_task(task_id)? else {
                // Stale index entry — just clean it up
                self.running_idx
                    .remove(&running_key)
                    .map_err(|e| TaskError::Storage(e.to_string()))?;
                continue;
            };

            if task.status != TaskStatus::Running {
                self.running_idx
                    .remove(&running_key)
                    .map_err(|e| TaskError::Storage(e.to_string()))?;
                continue;
            }

            // Re-queue as pending
            task.status = TaskStatus::Pending;
            task.claimed_at = None;
            task.claimed_by = None;
            task.error_message = Some("lease expired (abandoned)".to_string());

            let task_value = bincode::serialize(&task)?;
            let pending_key = encode_pending_key(task.priority, task.run_at, task_id);

            let mut batch = self.db.batch();
            batch.remove(&self.running_idx, &running_key);
            batch.insert(&self.pending_idx, pending_key, vec![]);
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
            batch
                .commit()
                .map_err(|e| TaskError::Storage(e.to_string()))?;

            count += 1;
        }

        Ok(count)
    }

    /// Remove completed/failed tasks older than max_age_secs. Returns count of cleaned tasks.
    pub fn cleanup(&self, max_age_secs: u64, now: u64) -> Result<u32> {
        let cutoff = now.saturating_sub(max_age_secs);
        let mut count = 0u32;

        // Scan all tasks and collect IDs of old terminal tasks
        let to_remove: Vec<(u64, bool)> = self
            .tasks
            .iter()
            .filter_map(|guard| {
                let (_, value) = guard.into_inner().ok()?;
                let record: TaskRecord = bincode::deserialize(value.as_ref()).ok()?;
                if record.status.is_terminal() {
                    if let Some(completed_at) = record.completed_at {
                        if completed_at < cutoff {
                            let is_dead_letter = record.status == TaskStatus::DeadLetter;
                            return Some((record.task_id, is_dead_letter));
                        }
                    }
                }
                None
            })
            .collect();

        for (task_id, is_dead_letter) in to_remove {
            let mut batch = self.db.batch();
            batch.remove(&self.tasks, task_id.to_be_bytes());
            if is_dead_letter {
                batch.remove(&self.dead_letter, task_id.to_be_bytes());
            }
            batch
                .commit()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
            count += 1;
        }

        Ok(count)
    }

    /// Count tasks in each status for stats.
    pub fn count_by_status(&self) -> Result<crate::task::QueueStats> {
        let mut stats = crate::task::QueueStats::default();

        for guard in self.tasks.iter() {
            let (_, value) = guard
                .into_inner()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
            let record: TaskRecord = bincode::deserialize(value.as_ref())?;
            match record.status {
                TaskStatus::Pending => stats.pending += 1,
                TaskStatus::Running => stats.running += 1,
                TaskStatus::Completed => stats.completed += 1,
                TaskStatus::Failed => stats.failed += 1,
                TaskStatus::DeadLetter => stats.dead_letter += 1,
                TaskStatus::Cancelled => stats.cancelled += 1,
            }
        }

        Ok(stats)
    }

    /// Count pending tasks (for admission control).
    pub fn count_pending(&self) -> Result<usize> {
        let mut count = 0usize;
        for guard in self.pending_idx.iter() {
            // Just need to verify the entry is valid
            let _ = guard
                .into_inner()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
            count += 1;
        }
        Ok(count)
    }

    /// List tasks with optional filters.
    pub fn list_tasks(
        &self,
        status_filter: Option<TaskStatus>,
        type_filter: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TaskRecord>> {
        let mut results = Vec::new();
        let mut skipped = 0usize;

        for guard in self.tasks.iter() {
            let (_, value) = guard
                .into_inner()
                .map_err(|e| TaskError::Storage(e.to_string()))?;
            let record: TaskRecord = bincode::deserialize(value.as_ref())?;

            // Apply filters
            if let Some(status) = status_filter {
                if record.status != status {
                    continue;
                }
            }
            if let Some(task_type) = type_filter {
                if record.task_type != task_type {
                    continue;
                }
            }

            // Apply offset
            if skipped < offset {
                skipped += 1;
                continue;
            }

            results.push(record);
            if results.len() >= limit {
                break;
            }
        }

        Ok(results)
    }

    /// Remove a task from the running index by scanning for its task_id.
    fn remove_from_running_idx(&self, task_id: u64) -> Result<()> {
        for guard in self.running_idx.iter() {
            if let Ok((key, _)) = guard.into_inner() {
                if let Some((_, tid)) = decode_running_key(key.as_ref()) {
                    if tid == task_id {
                        self.running_idx
                            .remove(key.as_ref())
                            .map_err(|e| TaskError::Storage(e.to_string()))?;
                        return Ok(());
                    }
                }
            }
        }
        Ok(())
    }

    /// Persist all in-memory data to disk.
    pub fn flush(&self) -> Result<()> {
        self.db
            .persist(PersistMode::SyncAll)
            .map_err(|e| TaskError::Storage(e.to_string()))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::task::{TaskPriority, TaskStatus};
    use tempfile::TempDir;

    fn test_store() -> (TaskStore, TempDir) {
        let dir = TempDir::new().unwrap();
        let store = TaskStore::open(dir.path().to_str().unwrap()).unwrap();
        (store, dir)
    }

    fn make_task(store: &TaskStore, task_type: &str, priority: TaskPriority) -> TaskRecord {
        let id = store.generate_id();
        TaskRecord {
            task_id: id,
            task_type: task_type.to_string(),
            params: vec![1, 2, 3],
            priority,
            status: TaskStatus::Pending,
            result: None,
            error_message: None,
            attempt: 0,
            max_retries: 3,
            created_at: 1700000000,
            run_at: 0,
            claimed_at: None,
            claimed_by: None,
            lease_secs: 300,
            completed_at: None,
            progress_pct: 0,
            progress_message: None,
        }
    }

    #[test]
    fn test_open_and_generate_id() {
        let (store, _dir) = test_store();
        let id1 = store.generate_id();
        let id2 = store.generate_id();
        assert!(id2 > id1);
    }

    #[test]
    fn test_insert_and_get() {
        let (store, _dir) = test_store();
        let task = make_task(&store, "test.echo", TaskPriority::Normal);
        let task_id = task.task_id;

        store.insert_task(&task).unwrap();
        let loaded = store.get_task(task_id).unwrap().unwrap();
        assert_eq!(loaded.task_id, task_id);
        assert_eq!(loaded.task_type, "test.echo");
        assert_eq!(loaded.status, TaskStatus::Pending);
    }

    #[test]
    fn test_get_nonexistent() {
        let (store, _dir) = test_store();
        assert!(store.get_task(999999).unwrap().is_none());
    }

    #[test]
    fn test_claim_next_priority_order() {
        let (store, _dir) = test_store();

        let low = make_task(&store, "low", TaskPriority::Low);
        let high = make_task(&store, "high", TaskPriority::High);
        let critical = make_task(&store, "critical", TaskPriority::Critical);

        // Insert in non-priority order
        store.insert_task(&low).unwrap();
        store.insert_task(&high).unwrap();
        store.insert_task(&critical).unwrap();

        // Should claim in priority order
        let claimed1 = store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .unwrap();
        assert_eq!(claimed1.task_type, "critical");

        let claimed2 = store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .unwrap();
        assert_eq!(claimed2.task_type, "high");

        let claimed3 = store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .unwrap();
        assert_eq!(claimed3.task_type, "low");

        // Queue is empty
        assert!(store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_complete_lifecycle() {
        let (store, _dir) = test_store();
        let task = make_task(&store, "test", TaskPriority::Normal);
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        let claimed = store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .unwrap();
        assert_eq!(claimed.status, TaskStatus::Running);
        assert_eq!(claimed.attempt, 1);

        let completed = store.complete_task(task_id, b"done", 1700000001).unwrap();
        assert_eq!(completed.status, TaskStatus::Completed);
        assert_eq!(completed.result.as_deref(), Some(b"done".as_slice()));

        let loaded = store.get_task(task_id).unwrap().unwrap();
        assert_eq!(loaded.status, TaskStatus::Completed);
    }

    #[test]
    fn test_fail_and_retry() {
        let (store, _dir) = test_store();
        let mut task = make_task(&store, "test", TaskPriority::Normal);
        task.max_retries = 3;
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        // Claim and fail — should re-queue (attempt 1 < max_retries 3)
        store.claim_next("w-0", 300, 1700000000, 0).unwrap();
        let (failed, dead) = store.fail_task(task_id, "oops", 1700000001).unwrap();
        assert!(!dead);
        assert_eq!(failed.status, TaskStatus::Pending);
        assert!(failed.run_at > 1700000001); // backoff applied
    }

    #[test]
    fn test_fail_dead_letter() {
        let (store, _dir) = test_store();
        let mut task = make_task(&store, "test", TaskPriority::Normal);
        task.max_retries = 1;
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        // Claim and fail — attempt 1 >= max_retries 1 → dead letter
        store.claim_next("w-0", 300, 1700000000, 0).unwrap();
        let (failed, dead) = store.fail_task(task_id, "fatal", 1700000001).unwrap();
        assert!(dead);
        assert_eq!(failed.status, TaskStatus::DeadLetter);
    }

    #[test]
    fn test_cancel_pending() {
        let (store, _dir) = test_store();
        let task = make_task(&store, "test", TaskPriority::Normal);
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        let cancelled = store.cancel_task(task_id, 1700000001).unwrap();
        assert_eq!(cancelled.status, TaskStatus::Cancelled);

        // Can't claim cancelled task
        assert!(store
            .claim_next("w-0", 300, 1700000000, 0)
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_requeue_abandoned() {
        let (store, _dir) = test_store();
        let task = make_task(&store, "test", TaskPriority::Normal);
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        // Claim with 300s lease at time 1000
        store.claim_next("w-0", 300, 1000, 0).unwrap();

        // At time 1200 (before expiry): nothing to requeue
        let count = store.requeue_abandoned(1200).unwrap();
        assert_eq!(count, 0);

        // At time 1400 (after expiry): should requeue
        let count = store.requeue_abandoned(1400).unwrap();
        assert_eq!(count, 1);

        // Task is pending again
        let loaded = store.get_task(task_id).unwrap().unwrap();
        assert_eq!(loaded.status, TaskStatus::Pending);
    }

    #[test]
    fn test_count_by_status() {
        let (store, _dir) = test_store();

        let t1 = make_task(&store, "a", TaskPriority::Normal);
        let t2 = make_task(&store, "b", TaskPriority::Normal);
        let t3 = make_task(&store, "c", TaskPriority::Normal);
        store.insert_task(&t1).unwrap();
        store.insert_task(&t2).unwrap();
        store.insert_task(&t3).unwrap();

        let stats = store.count_by_status().unwrap();
        assert_eq!(stats.pending, 3);
        assert_eq!(stats.running, 0);

        store.claim_next("w-0", 300, 1700000000, 0).unwrap();
        let stats = store.count_by_status().unwrap();
        assert_eq!(stats.pending, 2);
        assert_eq!(stats.running, 1);
    }

    #[test]
    fn test_list_with_filters() {
        let (store, _dir) = test_store();

        let t1 = make_task(&store, "type_a", TaskPriority::Normal);
        let t2 = make_task(&store, "type_b", TaskPriority::Normal);
        let t3 = make_task(&store, "type_a", TaskPriority::Normal);
        store.insert_task(&t1).unwrap();
        store.insert_task(&t2).unwrap();
        store.insert_task(&t3).unwrap();

        // Filter by type
        let results = store.list_tasks(None, Some("type_a"), 100, 0).unwrap();
        assert_eq!(results.len(), 2);

        // Filter by status
        let results = store
            .list_tasks(Some(TaskStatus::Pending), None, 100, 0)
            .unwrap();
        assert_eq!(results.len(), 3);

        // Pagination
        let results = store.list_tasks(None, None, 2, 0).unwrap();
        assert_eq!(results.len(), 2);
        let results = store.list_tasks(None, None, 100, 2).unwrap();
        assert_eq!(results.len(), 1);
    }

    #[test]
    fn test_persistence_across_reopen() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().to_str().unwrap();

        // Insert a task
        let task_id;
        {
            let store = TaskStore::open(path).unwrap();
            let task = make_task(&store, "persist", TaskPriority::Normal);
            task_id = task.task_id;
            store.insert_task(&task).unwrap();
            store.flush().unwrap();
        }

        // Reopen and verify
        {
            let store = TaskStore::open(path).unwrap();
            let loaded = store.get_task(task_id).unwrap().unwrap();
            assert_eq!(loaded.task_type, "persist");
        }
    }
}

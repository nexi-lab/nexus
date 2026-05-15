use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use fjall::{Database, Keyspace, KeyspaceCreateOptions, PersistMode};

use super::error::{Result, TaskError};
use super::priority::{
    decode_pending_key, decode_running_key, encode_pending_key, encode_running_key,
};
use super::task::{TaskPriority, TaskRecord, TaskStatus};

/// Fjall-backed task storage with 5 keyspaces (column families).
///
/// Keyspaces:
/// - `tasks`:            task_id (u64 BE)          -> TaskRecord (bincode)
/// - `pending_idx`:      composite priority key     -> () (empty value)
/// - `running_idx`:      [lease_expires][task_id]   -> () (empty value)
/// - `running_task_key`: task_id (u64 BE)           -> running_idx key bytes
/// - `dead_letter`:      task_id (u64 BE)           -> TaskRecord (bincode)
pub struct TaskStore {
    db: Database,
    tasks: Keyspace,
    pending_idx: Keyspace,
    running_idx: Keyspace,
    /// Reverse lookup: task_id -> running_idx key, for O(1) removal.
    running_task_key: Keyspace,
    dead_letter: Keyspace,
    id_counter: AtomicU64,
    /// Prevents concurrent claim_next races (Issue #3029 / Bug 2).
    claim_lock: Mutex<()>,
    // Atomic status counters — O(1) stats instead of full-table scans.
    pending_count: AtomicU64,
    running_count: AtomicU64,
    completed_count: AtomicU64,
    cancelled_count: AtomicU64,
    dead_letter_count: AtomicU64,
}

impl TaskStore {
    /// Open or create the task store at the given path.
    pub fn open(path: &str) -> Result<Self> {
        let db = Database::builder(Path::new(path)).open()?;

        let tasks = db.keyspace("tasks", KeyspaceCreateOptions::default)?;
        let pending_idx = db.keyspace("pending_idx", KeyspaceCreateOptions::default)?;
        let running_idx = db.keyspace("running_idx", KeyspaceCreateOptions::default)?;
        let running_task_key = db.keyspace("running_task_key", KeyspaceCreateOptions::default)?;
        let dead_letter = db.keyspace("dead_letter", KeyspaceCreateOptions::default)?;

        // Initialize counter from existing max task_id
        let max_id = Self::find_max_task_id(&tasks);

        // Scan once to initialize all status counters
        let (pending, running, completed, cancelled, dead_letter_n) =
            Self::count_all_statuses(&tasks);

        // Rebuild running_task_key reverse-lookup index from running_idx.
        // This is necessary after restart/upgrade since running_task_key is
        // an auxiliary index that may not have existed before this version.
        Self::rebuild_running_task_key(&running_idx, &running_task_key)?;

        Ok(Self {
            db,
            tasks,
            pending_idx,
            running_idx,
            running_task_key,
            dead_letter,
            id_counter: AtomicU64::new(max_id + 1),
            claim_lock: Mutex::new(()),
            pending_count: AtomicU64::new(pending),
            running_count: AtomicU64::new(running),
            completed_count: AtomicU64::new(completed),
            cancelled_count: AtomicU64::new(cancelled),
            dead_letter_count: AtomicU64::new(dead_letter_n),
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

    /// Scan all tasks to count by status. Used once at startup.
    fn count_all_statuses(tasks: &Keyspace) -> (u64, u64, u64, u64, u64) {
        let mut pending = 0u64;
        let mut running = 0u64;
        let mut completed = 0u64;
        let mut cancelled = 0u64;
        let mut dead_letter = 0u64;

        for guard in tasks.iter() {
            if let Ok((_, value)) = guard.into_inner() {
                if let Ok(record) = bincode::deserialize::<TaskRecord>(value.as_ref()) {
                    match record.status {
                        TaskStatus::Pending => pending += 1,
                        TaskStatus::Running => running += 1,
                        TaskStatus::Completed => completed += 1,
                        TaskStatus::Cancelled => cancelled += 1,
                        TaskStatus::DeadLetter => dead_letter += 1,
                        TaskStatus::Failed => {} // transient, not counted
                    }
                }
            }
        }

        (pending, running, completed, cancelled, dead_letter)
    }

    /// Rebuild the running_task_key reverse-lookup index by scanning running_idx.
    /// Ensures that after restart or upgrade from a version without this index,
    /// all running tasks have their reverse-lookup entry populated.
    fn rebuild_running_task_key(running_idx: &Keyspace, running_task_key: &Keyspace) -> Result<()> {
        for guard in running_idx.iter() {
            if let Ok((key, _)) = guard.into_inner() {
                if let Some((_, task_id)) = decode_running_key(key.as_ref()) {
                    let running_key_bytes = key.as_ref().to_vec();
                    running_task_key.insert(task_id.to_be_bytes(), running_key_bytes)?;
                }
            }
        }
        Ok(())
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
        batch.commit()?;
        self.pending_count.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    /// Get a task by ID from the primary store.
    pub fn get_task(&self, task_id: u64) -> Result<Option<TaskRecord>> {
        let key = task_id.to_be_bytes();
        match self.tasks.get(key)? {
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
        self.tasks.insert(key, value)?;
        Ok(())
    }

    /// Look up the running_idx key for a task via the reverse-lookup keyspace.
    /// Returns O(1) instead of scanning the running index.
    fn find_running_key(&self, task_id: u64) -> Result<Option<Vec<u8>>> {
        match self.running_task_key.get(task_id.to_be_bytes())? {
            Some(val) => Ok(Some(val.as_ref().to_vec())),
            None => Ok(None),
        }
    }

    /// Return the first pending index key for a given priority, if any.
    fn first_pending_key_for_priority(&self, priority: u8) -> Option<Vec<u8>> {
        self.pending_idx
            .prefix([priority])
            .next()
            .and_then(|guard| guard.into_inner().ok())
            .map(|(key, _)| key.as_ref().to_vec())
    }

    /// Whether there is a due Critical task that must not be preempted.
    ///
    /// If the first key is corrupt, conservatively treat it as due so we avoid
    /// promoting lower-priority work before self-healing the critical band.
    fn has_due_critical(&self, now: u64) -> bool {
        let Some(key_bytes) = self.first_pending_key_for_priority(TaskPriority::Critical as u8)
        else {
            return false;
        };
        match decode_pending_key(&key_bytes) {
            Some((_, run_at, _)) => run_at <= now,
            None => true,
        }
    }

    /// Select the highest-priority due key in O(priority bands), or return a
    /// corrupt key candidate so the caller can self-heal the index.
    fn first_due_or_corrupt_pending_key(&self, now: u64) -> Option<Vec<u8>> {
        for priority in TaskPriority::Critical as u8..=TaskPriority::BestEffort as u8 {
            let Some(key_bytes) = self.first_pending_key_for_priority(priority) else {
                continue;
            };
            match decode_pending_key(&key_bytes) {
                Some((_, run_at, _)) if run_at <= now => return Some(key_bytes),
                Some(_) => continue, // earliest key in this priority is future-scheduled
                None => return Some(key_bytes),
            }
        }
        None
    }

    /// Select a starving non-critical task for anti-starvation promotion.
    ///
    /// Promotion is disabled while any due Critical task exists.
    fn select_starved_pending_key(&self, now: u64, max_wait_secs: u64) -> Option<Vec<u8>> {
        if max_wait_secs == 0 || self.has_due_critical(now) {
            return None;
        }

        // Check non-Critical priority bands from lowest (BestEffort=4)
        // to highest (High=1). Promote the oldest starving task found.
        for priority in (TaskPriority::High as u8..=TaskPriority::BestEffort as u8).rev() {
            let Some(key_bytes) = self.first_pending_key_for_priority(priority) else {
                continue;
            };
            if let Some((_, run_at, _)) = decode_pending_key(&key_bytes) {
                if super::priority::should_promote_oldest(run_at, now, max_wait_secs) {
                    return Some(key_bytes);
                }
            }
        }

        None
    }

    /// Claim the next pending task. Atomically moves from pending_idx to running_idx.
    /// Returns None if no eligible tasks are available.
    ///
    /// Protected by a process-local mutex to prevent concurrent claim races
    /// (Issue #3029 / Bug 2).
    pub fn claim_next(
        &self,
        worker_id: &str,
        lease_secs: u32,
        now: u64,
        max_wait_secs: u64,
    ) -> Result<Option<TaskRecord>> {
        let _guard = self
            .claim_lock
            .lock()
            .map_err(|e| TaskError::Storage(format!("claim lock poisoned: {e}")))?;

        loop {
            // Normal path: select the first due task by checking the head entry
            // of each priority band (O(priority bands)).
            let target_key = self
                .select_starved_pending_key(now, max_wait_secs)
                .or_else(|| self.first_due_or_corrupt_pending_key(now));

            let Some(key_bytes) = target_key else {
                return Ok(None);
            };

            let Some((_, _, task_id)) = decode_pending_key(&key_bytes) else {
                // Corrupt key: remove and continue scanning.
                self.pending_idx.remove(&key_bytes)?;
                continue;
            };

            // Load the task record
            let Some(mut task) = self.get_task(task_id)? else {
                // Stale index entry — remove and continue scanning.
                self.pending_idx.remove(&key_bytes)?;
                continue;
            };

            // Self-heal stale pending index entries so terminal/running tasks
            // cannot be claimed again.
            if task.status != TaskStatus::Pending {
                self.pending_idx.remove(&key_bytes)?;
                continue;
            }

            // If the index key is stale (due in index, but future in record),
            // rewrite the key from canonical task data and keep scanning.
            if task.run_at > now {
                let corrected_key = encode_pending_key(task.priority, task.run_at, task_id);
                let mut repair = self.db.batch();
                repair.remove(&self.pending_idx, &key_bytes);
                repair.insert(&self.pending_idx, corrected_key, vec![]);
                repair.commit()?;
                continue;
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

            // Atomic: remove from pending, add to running + reverse lookup, update task
            let mut batch = self.db.batch();
            batch.remove(&self.pending_idx, &key_bytes);
            batch.insert(&self.running_idx, running_key, vec![]);
            batch.insert(&self.running_task_key, task_id.to_be_bytes(), running_key);
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
            batch.commit()?;
            self.pending_count.fetch_sub(1, Ordering::Relaxed);
            self.running_count.fetch_add(1, Ordering::Relaxed);

            return Ok(Some(task));
        }
    }

    /// Move a running task to completed state. Atomically removes from running_idx
    /// and updates the task record in a single batch (Issue #3029 / Bug 3).
    ///
    /// Verifies that `worker_id` matches the current owner to prevent stale workers
    /// from overwriting a re-claimed task after lease expiry.
    pub fn complete_task(
        &self,
        task_id: u64,
        result: &[u8],
        now: u64,
        worker_id: &str,
    ) -> Result<TaskRecord> {
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

        if task.claimed_by.as_deref() != Some(worker_id) {
            return Err(TaskError::NotOwner {
                task_id,
                worker_id: worker_id.to_string(),
            });
        }

        let running_key = self.find_running_key(task_id)?;

        task.status = TaskStatus::Completed;
        task.result = Some(result.to_vec());
        task.completed_at = Some(now);
        let task_value = bincode::serialize(&task)?;

        let mut batch = self.db.batch();
        if let Some(ref rk) = running_key {
            batch.remove(&self.running_idx, rk);
        }
        batch.remove(&self.running_task_key, task_id.to_be_bytes());
        batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
        batch.commit()?;

        self.running_count.fetch_sub(1, Ordering::Relaxed);
        self.completed_count.fetch_add(1, Ordering::Relaxed);

        Ok(task)
    }

    /// Fail a running task. If retries remain, re-queue to pending; otherwise dead-letter.
    /// All index updates and task writes are in a single atomic batch
    /// (Issue #3029 / Bug 3 + Bug 5).
    ///
    /// Verifies that `worker_id` matches the current owner to prevent stale workers
    /// from overwriting a re-claimed task after lease expiry.
    pub fn fail_task(
        &self,
        task_id: u64,
        error_message: &str,
        now: u64,
        worker_id: &str,
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

        if task.claimed_by.as_deref() != Some(worker_id) {
            return Err(TaskError::NotOwner {
                task_id,
                worker_id: worker_id.to_string(),
            });
        }

        let running_key = self.find_running_key(task_id)?;
        task.error_message = Some(error_message.to_string());

        let dead_lettered = super::retry::should_dead_letter(task.attempt, task.max_retries);

        let mut batch = self.db.batch();

        // Remove from running index (atomically in batch)
        if let Some(ref rk) = running_key {
            batch.remove(&self.running_idx, rk);
        }
        batch.remove(&self.running_task_key, task_id.to_be_bytes());

        if dead_lettered {
            task.status = TaskStatus::DeadLetter;
            task.completed_at = Some(now);
            let task_value = bincode::serialize(&task)?;
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value.clone());
            batch.insert(&self.dead_letter, task_id.to_be_bytes(), task_value);
        } else {
            let delay = super::retry::backoff_secs(task.attempt, task_id);
            task.status = TaskStatus::Pending;
            task.run_at = now + delay;
            task.claimed_at = None;
            task.claimed_by = None;
            let task_value = bincode::serialize(&task)?;
            let pending_key = encode_pending_key(task.priority, task.run_at, task_id);
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
            batch.insert(&self.pending_idx, pending_key, vec![]);
        }

        batch.commit()?;

        self.running_count.fetch_sub(1, Ordering::Relaxed);
        if dead_lettered {
            self.dead_letter_count.fetch_add(1, Ordering::Relaxed);
        } else {
            self.pending_count.fetch_add(1, Ordering::Relaxed);
        }

        Ok((task, dead_lettered))
    }

    /// Cancel a task. Works for both pending and running tasks.
    /// All index updates and task writes are in a single atomic batch
    /// (Issue #3029 / Bug 3).
    pub fn cancel_task(&self, task_id: u64, now: u64) -> Result<TaskRecord> {
        let mut task = self
            .get_task(task_id)?
            .ok_or(TaskError::NotFound(task_id))?;

        let prev_status = task.status;

        let mut batch = self.db.batch();

        match prev_status {
            TaskStatus::Pending => {
                let pending_key = encode_pending_key(task.priority, task.run_at, task_id);
                batch.remove(&self.pending_idx, pending_key);
            }
            TaskStatus::Running => {
                if let Some(rk) = self.find_running_key(task_id)? {
                    batch.remove(&self.running_idx, &rk);
                }
                batch.remove(&self.running_task_key, task_id.to_be_bytes());
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
        let task_value = bincode::serialize(&task)?;
        batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
        batch.commit()?;

        match prev_status {
            TaskStatus::Pending => {
                self.pending_count.fetch_sub(1, Ordering::Relaxed);
            }
            TaskStatus::Running => {
                self.running_count.fetch_sub(1, Ordering::Relaxed);
            }
            _ => {}
        }
        self.cancelled_count.fetch_add(1, Ordering::Relaxed);

        Ok(task)
    }

    /// Requeue tasks whose leases have expired. Returns count of requeued tasks.
    /// All mutations are committed in a single batch (Issue #3029 / Issue 15).
    pub fn requeue_abandoned(&self, now: u64) -> Result<u32> {
        let upper_bound = encode_running_key(now, u64::MAX);

        // Collect expired entries first (avoid holding iterator across writes)
        let expired: Vec<(Vec<u8>, u64)> = self
            .running_idx
            .range(..=upper_bound)
            .filter_map(|guard| {
                let (key, _) = match guard.into_inner() {
                    Ok(kv) => kv,
                    Err(e) => {
                        tracing::warn!("skipping task entry: storage iterator error: {}", e);
                        return None;
                    }
                };
                let key_bytes = key.as_ref().to_vec();
                let (_, task_id) = decode_running_key(&key_bytes)?;
                Some((key_bytes, task_id))
            })
            .collect();

        if expired.is_empty() {
            return Ok(0);
        }

        let mut batch = self.db.batch();
        let mut requeued = 0u32;

        for (running_key, task_id) in &expired {
            let Some(mut task) = self.get_task(*task_id)? else {
                // Stale index entry — clean up
                batch.remove(&self.running_idx, running_key);
                batch.remove(&self.running_task_key, task_id.to_be_bytes());
                continue;
            };

            if task.status != TaskStatus::Running {
                batch.remove(&self.running_idx, running_key);
                batch.remove(&self.running_task_key, task_id.to_be_bytes());
                continue;
            }

            // Re-queue as pending
            task.status = TaskStatus::Pending;
            task.claimed_at = None;
            task.claimed_by = None;
            task.error_message = Some("lease expired (abandoned)".to_string());

            let task_value = bincode::serialize(&task)?;
            let pending_key = encode_pending_key(task.priority, task.run_at, *task_id);

            batch.remove(&self.running_idx, running_key);
            batch.remove(&self.running_task_key, task_id.to_be_bytes());
            batch.insert(&self.pending_idx, pending_key, vec![]);
            batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);

            requeued += 1;
        }

        batch.commit()?;

        // Update counters for requeued tasks (Running → Pending)
        self.running_count
            .fetch_sub(requeued as u64, Ordering::Relaxed);
        self.pending_count
            .fetch_add(requeued as u64, Ordering::Relaxed);

        Ok(requeued)
    }

    /// Remove completed/failed tasks older than max_age_secs. Returns count of cleaned tasks.
    /// All removals are committed in a single batch (Issue #3029 / Issue 15).
    pub fn cleanup(&self, max_age_secs: u64, now: u64) -> Result<u32> {
        let cutoff = now.saturating_sub(max_age_secs);

        // Scan all tasks and collect IDs of old terminal tasks
        let to_remove: Vec<(u64, bool, TaskStatus)> = self
            .tasks
            .iter()
            .filter_map(|guard| {
                let (_, value) = match guard.into_inner() {
                    Ok(kv) => kv,
                    Err(e) => {
                        tracing::warn!("skipping task entry: storage iterator error: {}", e);
                        return None;
                    }
                };
                let record: TaskRecord = match bincode::deserialize(value.as_ref()) {
                    Ok(r) => r,
                    Err(e) => {
                        tracing::warn!("skipping task entry: deserialization error: {}", e);
                        return None;
                    }
                };
                if record.status.is_terminal() {
                    if let Some(completed_at) = record.completed_at {
                        if completed_at < cutoff {
                            return Some((
                                record.task_id,
                                record.status == TaskStatus::DeadLetter,
                                record.status,
                            ));
                        }
                    }
                }
                None
            })
            .collect();

        if to_remove.is_empty() {
            return Ok(0);
        }

        let mut batch = self.db.batch();
        for &(task_id, is_dead_letter, _) in &to_remove {
            batch.remove(&self.tasks, task_id.to_be_bytes());
            if is_dead_letter {
                batch.remove(&self.dead_letter, task_id.to_be_bytes());
            }
        }
        batch.commit()?;

        // Update counters
        for &(_, _, status) in &to_remove {
            match status {
                TaskStatus::Completed => {
                    self.completed_count.fetch_sub(1, Ordering::Relaxed);
                }
                TaskStatus::DeadLetter => {
                    self.dead_letter_count.fetch_sub(1, Ordering::Relaxed);
                }
                TaskStatus::Cancelled => {
                    self.cancelled_count.fetch_sub(1, Ordering::Relaxed);
                }
                _ => {}
            }
        }

        Ok(to_remove.len() as u32)
    }

    /// Count tasks in each status. Uses atomic counters for O(1) performance
    /// (Issue #3029 / Issue 14).
    pub fn count_by_status(&self) -> Result<super::task::QueueStats> {
        Ok(super::task::QueueStats {
            pending: self.pending_count.load(Ordering::Relaxed) as usize,
            running: self.running_count.load(Ordering::Relaxed) as usize,
            completed: self.completed_count.load(Ordering::Relaxed) as usize,
            failed: 0, // Failed is a transient state (→ Pending or DeadLetter)
            dead_letter: self.dead_letter_count.load(Ordering::Relaxed) as usize,
            cancelled: self.cancelled_count.load(Ordering::Relaxed) as usize,
        })
    }

    /// Count pending tasks (for admission control).
    /// Uses cached atomic counter for O(1) performance instead of scanning the index.
    pub fn count_pending(&self) -> Result<usize> {
        Ok(self.pending_count.load(Ordering::Relaxed) as usize)
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

    /// Renew the lease for a running task. Atomically removes old running_idx entry,
    /// inserts new one with updated expiry, and updates the task record.
    pub fn renew_lease(
        &self,
        task_id: u64,
        new_lease_expires: u64,
        task: &TaskRecord,
    ) -> Result<()> {
        let old_key = self.find_running_key(task_id)?;

        let new_running_key = encode_running_key(new_lease_expires, task_id);
        let task_value = bincode::serialize(task)?;

        let mut batch = self.db.batch();
        if let Some(ref old) = old_key {
            batch.remove(&self.running_idx, old);
        }
        batch.insert(&self.running_idx, new_running_key, vec![]);
        batch.insert(
            &self.running_task_key,
            task_id.to_be_bytes(),
            new_running_key,
        );
        batch.insert(&self.tasks, task_id.to_be_bytes(), task_value);
        batch.commit()?;

        Ok(())
    }

    /// Persist all in-memory data to disk.
    pub fn flush(&self) -> Result<()> {
        self.db.persist(PersistMode::SyncAll)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tasks::task::{TaskPriority, TaskStatus};
    use std::collections::HashSet;
    use std::sync::Arc;
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

    /// Verify that all indexes are consistent with task records.
    /// Every task in pending_idx must be Pending in the tasks keyspace,
    /// every task in running_idx must be Running, and vice versa.
    fn verify_index_consistency(store: &TaskStore) {
        // Collect all task IDs from pending_idx
        let mut pending_ids: HashSet<u64> = HashSet::new();
        for guard in store.pending_idx.iter() {
            if let Ok((key, _)) = guard.into_inner() {
                if let Some((_, _, task_id)) = decode_pending_key(key.as_ref()) {
                    pending_ids.insert(task_id);
                }
            }
        }

        // Collect all task IDs from running_idx
        let mut running_ids: HashSet<u64> = HashSet::new();
        for guard in store.running_idx.iter() {
            if let Ok((key, _)) = guard.into_inner() {
                if let Some((_, task_id)) = decode_running_key(key.as_ref()) {
                    running_ids.insert(task_id);
                }
            }
        }

        // Verify each pending task in the index is actually Pending
        for &task_id in &pending_ids {
            let task = store
                .get_task(task_id)
                .unwrap()
                .expect("pending index references nonexistent task");
            assert_eq!(
                task.status,
                TaskStatus::Pending,
                "task {} in pending_idx has status {:?}",
                task_id,
                task.status
            );
        }

        // Verify each running task in the index is actually Running
        for &task_id in &running_ids {
            let task = store
                .get_task(task_id)
                .unwrap()
                .expect("running index references nonexistent task");
            assert_eq!(
                task.status,
                TaskStatus::Running,
                "task {} in running_idx has status {:?}",
                task_id,
                task.status
            );
        }

        // Verify running_task_key reverse lookup is consistent with running_idx
        for &task_id in &running_ids {
            assert!(
                store.find_running_key(task_id).unwrap().is_some(),
                "task {} in running_idx has no reverse-lookup entry",
                task_id
            );
        }

        // Verify no Pending tasks are missing from pending_idx
        for guard in store.tasks.iter() {
            if let Ok((_, value)) = guard.into_inner() {
                if let Ok(record) = bincode::deserialize::<TaskRecord>(value.as_ref()) {
                    if record.status == TaskStatus::Pending && record.run_at == 0 {
                        assert!(
                            pending_ids.contains(&record.task_id),
                            "task {} is Pending but not in pending_idx",
                            record.task_id
                        );
                    }
                    if record.status == TaskStatus::Running {
                        assert!(
                            running_ids.contains(&record.task_id),
                            "task {} is Running but not in running_idx",
                            record.task_id
                        );
                    }
                }
            }
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
        verify_index_consistency(&store);
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

        verify_index_consistency(&store);
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
        verify_index_consistency(&store);

        let completed = store
            .complete_task(task_id, b"done", 1700000001, "w-0")
            .unwrap();
        assert_eq!(completed.status, TaskStatus::Completed);
        assert_eq!(completed.result.as_deref(), Some(b"done".as_slice()));

        let loaded = store.get_task(task_id).unwrap().unwrap();
        assert_eq!(loaded.status, TaskStatus::Completed);
        verify_index_consistency(&store);
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
        let (failed, dead) = store.fail_task(task_id, "oops", 1700000001, "w-0").unwrap();
        assert!(!dead);
        assert_eq!(failed.status, TaskStatus::Pending);
        assert!(failed.run_at > 1700000001); // backoff applied
        verify_index_consistency(&store);
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
        let (failed, dead) = store
            .fail_task(task_id, "fatal", 1700000001, "w-0")
            .unwrap();
        assert!(dead);
        assert_eq!(failed.status, TaskStatus::DeadLetter);
        verify_index_consistency(&store);
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
        verify_index_consistency(&store);
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
        verify_index_consistency(&store);
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

            // Verify counters are initialized correctly on reopen
            let stats = store.count_by_status().unwrap();
            assert_eq!(stats.pending, 1);
        }
    }

    /// Test that concurrent claim_next calls never produce duplicate claims
    /// (Issue #3029 / Bug 2 regression test).
    #[test]
    fn test_concurrent_claim_no_duplicates() {
        let dir = TempDir::new().unwrap();
        let store = Arc::new(TaskStore::open(dir.path().to_str().unwrap()).unwrap());

        let num_tasks = 50;
        let num_workers = 8;

        // Insert tasks
        for i in 0..num_tasks {
            let mut task = make_task(&store, &format!("task-{}", i), TaskPriority::Normal);
            task.run_at = 0;
            store.insert_task(&task).unwrap();
        }

        // Spawn workers that race to claim
        let claimed: Arc<Mutex<Vec<u64>>> = Arc::new(Mutex::new(Vec::new()));
        let mut handles = Vec::new();

        for w in 0..num_workers {
            let store = Arc::clone(&store);
            let claimed = Arc::clone(&claimed);
            handles.push(std::thread::spawn(move || {
                let worker_id = format!("worker-{}", w);
                while let Some(task) = store.claim_next(&worker_id, 300, 1700000000, 0).unwrap() {
                    claimed.lock().unwrap().push(task.task_id);
                }
            }));
        }

        for h in handles {
            h.join().unwrap();
        }

        let claimed = claimed.lock().unwrap();
        let unique: HashSet<u64> = claimed.iter().copied().collect();

        // Every task claimed exactly once: no duplicates, no losses
        assert_eq!(
            claimed.len(),
            num_tasks,
            "expected {} claims, got {}",
            num_tasks,
            claimed.len()
        );
        assert_eq!(unique.len(), num_tasks, "duplicate claims detected");

        verify_index_consistency(&store);
    }

    /// Test that complete_task, cancel_task, fail_task maintain index consistency
    /// (Issue #3029 / Bug 3 regression test).
    #[test]
    fn test_index_consistency_across_transitions() {
        let (store, _dir) = test_store();

        // Create tasks for each transition path
        let t_complete = make_task(&store, "complete", TaskPriority::Normal);
        let t_cancel_pending = make_task(&store, "cancel_p", TaskPriority::Normal);
        let t_cancel_running = make_task(&store, "cancel_r", TaskPriority::Normal);
        let t_fail_retry = make_task(&store, "fail_retry", TaskPriority::Normal);
        let mut t_fail_dl = make_task(&store, "fail_dl", TaskPriority::Normal);
        t_fail_dl.max_retries = 1;

        let id_complete = t_complete.task_id;
        let id_cancel_p = t_cancel_pending.task_id;
        let id_cancel_r = t_cancel_running.task_id;
        let id_fail_retry = t_fail_retry.task_id;
        let id_fail_dl = t_fail_dl.task_id;

        store.insert_task(&t_complete).unwrap();
        store.insert_task(&t_cancel_pending).unwrap();
        store.insert_task(&t_cancel_running).unwrap();
        store.insert_task(&t_fail_retry).unwrap();
        store.insert_task(&t_fail_dl).unwrap();
        verify_index_consistency(&store);

        // Cancel a pending task
        store.cancel_task(id_cancel_p, 1700000001).unwrap();
        verify_index_consistency(&store);

        // Claim remaining tasks
        store.claim_next("w-0", 300, 1700000000, 0).unwrap(); // complete
        store.claim_next("w-0", 300, 1700000000, 0).unwrap(); // cancel_r
        store.claim_next("w-0", 300, 1700000000, 0).unwrap(); // fail_retry
        store.claim_next("w-0", 300, 1700000000, 0).unwrap(); // fail_dl
        verify_index_consistency(&store);

        // Complete
        store
            .complete_task(id_complete, b"ok", 1700000001, "w-0")
            .unwrap();
        verify_index_consistency(&store);

        // Cancel running
        store.cancel_task(id_cancel_r, 1700000001).unwrap();
        verify_index_consistency(&store);

        // Fail with retry
        store
            .fail_task(id_fail_retry, "oops", 1700000001, "w-0")
            .unwrap();
        verify_index_consistency(&store);

        // Fail to dead letter
        store
            .fail_task(id_fail_dl, "fatal", 1700000001, "w-0")
            .unwrap();
        verify_index_consistency(&store);

        // Verify final status counts
        let stats = store.count_by_status().unwrap();
        assert_eq!(stats.completed, 1);
        assert_eq!(stats.cancelled, 2);
        assert_eq!(stats.dead_letter, 1);
        assert_eq!(stats.pending, 1); // fail_retry was re-queued
        assert_eq!(stats.running, 0);
    }

    #[test]
    fn test_claim_next_skips_stale_non_pending_entry() {
        let (store, _dir) = test_store();

        let done = make_task(&store, "done", TaskPriority::Normal);
        let done_id = done.task_id;
        let pending = make_task(&store, "pending", TaskPriority::Normal);

        store.insert_task(&done).unwrap();
        store.insert_task(&pending).unwrap();

        store.claim_next("w-0", 300, 1700000000, 0).unwrap();
        store
            .complete_task(done_id, b"ok", 1700000001, "w-0")
            .unwrap();

        // Inject a stale pending_idx entry for a completed task.
        let stale_key = encode_pending_key(TaskPriority::Critical, 0, done_id);
        store.pending_idx.insert(stale_key, vec![]).unwrap();

        // claim_next should self-heal stale index entries and return a truly pending task.
        let claimed = store
            .claim_next("w-1", 300, 1700000002, 0)
            .unwrap()
            .unwrap();
        assert_eq!(claimed.task_type, "pending");

        // Verify stale key is removed.
        let stale_still_present = store.pending_idx.iter().any(|guard| {
            guard
                .into_inner()
                .ok()
                .and_then(|(key, _)| decode_pending_key(key.as_ref()))
                .is_some_and(|(_, _, task_id)| task_id == done_id)
        });
        assert!(
            !stale_still_present,
            "stale pending index entry should be removed"
        );
    }

    #[test]
    fn test_claim_next_repairs_mismatched_pending_key() {
        let (store, _dir) = test_store();
        let mut task = make_task(&store, "future", TaskPriority::Normal);
        task.run_at = 2000;
        let task_id = task.task_id;
        store.insert_task(&task).unwrap();

        let canonical_key = encode_pending_key(task.priority, task.run_at, task_id);
        let stale_due_key = encode_pending_key(task.priority, 1000, task_id);
        store.pending_idx.remove(canonical_key).unwrap();
        store.pending_idx.insert(stale_due_key, vec![]).unwrap();

        // At now=1500, stale key looks due but task record is still scheduled for 2000.
        let claimed = store.claim_next("w-0", 300, 1500, 0).unwrap();
        assert!(claimed.is_none());

        // claim_next should rewrite stale key to canonical run_at.
        let mut has_canonical = false;
        let mut has_stale = false;
        for guard in store.pending_idx.iter() {
            if let Ok((key, _)) = guard.into_inner() {
                if key.as_ref() == canonical_key {
                    has_canonical = true;
                }
                if key.as_ref() == stale_due_key {
                    has_stale = true;
                }
            }
        }
        assert!(has_canonical, "canonical pending key should be restored");
        assert!(!has_stale, "stale pending key should be removed");

        // Once due, the task should be claimable.
        let claimed = store
            .claim_next("w-0", 300, 2000, 0)
            .unwrap()
            .expect("task should become claimable at run_at");
        assert_eq!(claimed.task_id, task_id);
    }

    #[test]
    fn test_claim_lock_poison_returns_error_not_panic() {
        let dir = TempDir::new().unwrap();
        let store = Arc::new(TaskStore::open(dir.path().to_str().unwrap()).unwrap());
        let store_for_panic = Arc::clone(&store);

        let h = std::thread::spawn(move || {
            let _guard = store_for_panic.claim_lock.lock().unwrap();
            panic!("poison claim lock");
        });
        assert!(h.join().is_err());

        let result = store.claim_next("w-0", 300, 0, 0);
        assert!(matches!(result, Err(TaskError::Storage(_))));
    }

    /// E2E anti-starvation test: a starved BestEffort task should be promoted
    /// over a newer High-priority task when max_wait_secs is exceeded.
    /// Also verifies multi-band promotion with a Low-priority starved task.
    #[test]
    fn test_anti_starvation_promotes_starved_task() {
        let (store, _dir) = test_store();
        let now = 1_700_000_100u64;

        // --- Part 1: BestEffort starved task promoted over High ---

        // Submit a BestEffort task with run_at=0 (very old — starved)
        let mut starved_be = make_task(&store, "starved_best_effort", TaskPriority::BestEffort);
        starved_be.run_at = 0;
        let starved_be_id = starved_be.task_id;
        store.insert_task(&starved_be).unwrap();

        // Submit a High priority task with run_at = now (fresh)
        let mut fresh_high = make_task(&store, "fresh_high", TaskPriority::High);
        fresh_high.run_at = now;
        store.insert_task(&fresh_high).unwrap();

        // Claim with max_wait_secs=60. The BestEffort task has waited
        // (now - 0) = 1_700_000_100 seconds, far exceeding 60s threshold.
        let claimed = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(
            claimed.task_id, starved_be_id,
            "starved BestEffort task should be promoted over fresh High task"
        );
        assert_eq!(claimed.task_type, "starved_best_effort");

        // The High task should still be claimable next
        let claimed2 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed2.task_type, "fresh_high");

        verify_index_consistency(&store);

        // --- Part 2: Low-priority starved task promoted over Normal ---

        let mut starved_low = make_task(&store, "starved_low", TaskPriority::Low);
        starved_low.run_at = 0; // very old
        let starved_low_id = starved_low.task_id;
        store.insert_task(&starved_low).unwrap();

        let mut fresh_normal = make_task(&store, "fresh_normal", TaskPriority::Normal);
        fresh_normal.run_at = now;
        store.insert_task(&fresh_normal).unwrap();

        // Claim with anti-starvation enabled
        let claimed3 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(
            claimed3.task_id, starved_low_id,
            "starved Low task should be promoted over fresh Normal task"
        );
        assert_eq!(claimed3.task_type, "starved_low");

        // The Normal task should still be claimable
        let claimed4 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed4.task_type, "fresh_normal");

        verify_index_consistency(&store);
    }

    #[test]
    fn test_anti_starvation_does_not_preempt_critical() {
        let (store, _dir) = test_store();
        let now = 1_700_000_100u64;

        // Insert a starved BestEffort task (very old)
        let mut starved_be = make_task(&store, "starved_be", TaskPriority::BestEffort);
        starved_be.run_at = 0; // run_at = 0 → very old
        let starved_id = starved_be.task_id;
        store.insert_task(&starved_be).unwrap();

        // Insert a Critical task (fresh)
        let mut critical = make_task(&store, "fresh_critical", TaskPriority::Critical);
        critical.run_at = now;
        let critical_id = critical.task_id;
        store.insert_task(&critical).unwrap();

        // Claim with anti-starvation enabled — Critical must still win
        let claimed = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(
            claimed.task_id, critical_id,
            "Critical task must be claimed first even with starved BestEffort pending"
        );
        assert_eq!(claimed.task_type, "fresh_critical");

        // Now the starved BestEffort should be claimable (no more Critical)
        let claimed2 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed2.task_id, starved_id);
        assert_eq!(claimed2.task_type, "starved_be");

        verify_index_consistency(&store);
    }

    #[test]
    fn test_anti_starvation_allows_promotion_when_critical_is_future() {
        let (store, _dir) = test_store();
        let now = 1_700_000_100u64;

        // Starved low-priority task.
        let mut starved_be = make_task(&store, "starved_be", TaskPriority::BestEffort);
        starved_be.run_at = 0;
        store.insert_task(&starved_be).unwrap();

        // Due High-priority task.
        let mut fresh_high = make_task(&store, "fresh_high", TaskPriority::High);
        fresh_high.run_at = now;
        store.insert_task(&fresh_high).unwrap();

        // Critical exists, but it's scheduled far in the future.
        let mut future_critical = make_task(&store, "future_critical", TaskPriority::Critical);
        future_critical.run_at = now + 3600;
        store.insert_task(&future_critical).unwrap();

        // Since no Critical task is due yet, anti-starvation may still promote.
        let claimed1 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed1.task_type, "starved_be");

        // Then due High should be claimed.
        let claimed2 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed2.task_type, "fresh_high");

        // No due tasks remain at 'now' (Critical is future scheduled).
        assert!(store.claim_next("w-0", 300, now, 60).unwrap().is_none());

        // When Critical becomes due, it should be claimable.
        let claimed3 = store
            .claim_next("w-0", 300, now + 3600, 60)
            .unwrap()
            .unwrap();
        assert_eq!(claimed3.task_type, "future_critical");

        verify_index_consistency(&store);
    }

    #[test]
    fn test_anti_starvation_recovers_after_stale_critical_cleanup() {
        let (store, _dir) = test_store();
        let now = 1_700_000_100u64;

        let mut starved_be = make_task(&store, "starved_be", TaskPriority::BestEffort);
        starved_be.run_at = 0;
        store.insert_task(&starved_be).unwrap();

        let mut fresh_high = make_task(&store, "fresh_high", TaskPriority::High);
        fresh_high.run_at = now;
        store.insert_task(&fresh_high).unwrap();

        // Inject a stale due Critical index entry that references no task.
        let stale_task_id = u64::MAX - 1;
        let stale_critical_key = encode_pending_key(TaskPriority::Critical, 0, stale_task_id);
        store
            .pending_idx
            .insert(stale_critical_key, vec![])
            .unwrap();

        // claim_next should remove stale Critical entry, then re-evaluate anti-starvation
        // and still promote the starving BestEffort task.
        let claimed1 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed1.task_type, "starved_be");

        // The due High task should be next.
        let claimed2 = store.claim_next("w-0", 300, now, 60).unwrap().unwrap();
        assert_eq!(claimed2.task_type, "fresh_high");

        verify_index_consistency(&store);
    }
}

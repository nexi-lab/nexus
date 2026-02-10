use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::{Result, TaskError};
use crate::store::TaskStore;
use crate::task::{QueueStats, TaskPriority, TaskRecord, TaskStatus};

/// Core task queue engine. Thread-safe via fjall's internal concurrency.
pub struct Engine {
    store: TaskStore,
    max_pending: usize,
    max_wait_secs: u64,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

impl Engine {
    /// Open or create a task engine at the given path.
    pub fn open(path: &str, max_pending: usize, max_wait_secs: u64) -> Result<Self> {
        let store = TaskStore::open(path)?;
        Ok(Self {
            store,
            max_pending,
            max_wait_secs,
        })
    }

    /// Submit a new task. Returns the assigned task ID.
    pub fn submit(
        &self,
        task_type: &str,
        params: &[u8],
        priority: TaskPriority,
        max_retries: u32,
        run_at: u64,
    ) -> Result<u64> {
        // Admission control
        if self.max_pending > 0 {
            let pending = self.store.count_pending()?;
            if pending >= self.max_pending {
                return Err(TaskError::QueueFull {
                    pending,
                    max_pending: self.max_pending,
                });
            }
        }

        let now = now_secs();
        let task_id = self.store.generate_id();
        let effective_run_at = if run_at == 0 { now } else { run_at };

        let task = TaskRecord {
            task_id,
            task_type: task_type.to_string(),
            params: params.to_vec(),
            priority,
            status: TaskStatus::Pending,
            result: None,
            error_message: None,
            attempt: 0,
            max_retries,
            created_at: now,
            run_at: effective_run_at,
            claimed_at: None,
            claimed_by: None,
            lease_secs: 0,
            completed_at: None,
            progress_pct: 0,
            progress_message: None,
        };

        self.store.insert_task(&task)?;
        Ok(task_id)
    }

    /// Claim the next available task for a worker.
    pub fn claim_next(&self, worker_id: &str, lease_secs: u32) -> Result<Option<TaskRecord>> {
        let now = now_secs();
        self.store
            .claim_next(worker_id, lease_secs, now, self.max_wait_secs)
    }

    /// Update heartbeat/progress for a running task.
    /// Returns false if the task was cancelled (worker should stop).
    pub fn heartbeat(&self, task_id: u64, progress_pct: u8, message: &str) -> Result<bool> {
        let mut task = self
            .store
            .get_task(task_id)?
            .ok_or(TaskError::NotFound(task_id))?;

        // If task was cancelled while running, signal the worker to stop
        if task.status == TaskStatus::Cancelled {
            return Ok(false);
        }

        if task.status != TaskStatus::Running {
            return Err(TaskError::InvalidTransition {
                task_id,
                current: task.status.to_string(),
                target: "RUNNING (heartbeat)".to_string(),
            });
        }

        task.progress_pct = progress_pct.min(100);
        if !message.is_empty() {
            task.progress_message = Some(message.to_string());
        }

        self.store.update_task(&task)?;
        Ok(true)
    }

    /// Mark a task as completed with a result payload.
    pub fn complete(&self, task_id: u64, result: &[u8]) -> Result<()> {
        let now = now_secs();
        self.store.complete_task(task_id, result, now)?;
        Ok(())
    }

    /// Mark a task as failed. Auto-retries if attempts remain; otherwise dead-letters.
    pub fn fail(&self, task_id: u64, error_message: &str) -> Result<()> {
        let now = now_secs();
        self.store.fail_task(task_id, error_message, now)?;
        Ok(())
    }

    /// Cancel a pending or running task.
    pub fn cancel(&self, task_id: u64) -> Result<()> {
        let now = now_secs();
        self.store.cancel_task(task_id, now)?;
        Ok(())
    }

    /// Get current status of a task.
    pub fn status(&self, task_id: u64) -> Result<Option<TaskRecord>> {
        self.store.get_task(task_id)
    }

    /// List tasks with optional filters.
    pub fn list_tasks(
        &self,
        status: Option<TaskStatus>,
        task_type: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TaskRecord>> {
        self.store.list_tasks(status, task_type, limit, offset)
    }

    /// Requeue tasks with expired leases. Returns count of requeued tasks.
    pub fn requeue_abandoned(&self) -> Result<u32> {
        let now = now_secs();
        self.store.requeue_abandoned(now)
    }

    /// Remove completed/failed tasks older than max_age_secs. Returns count of cleaned tasks.
    pub fn cleanup(&self, max_completed_age_secs: u64) -> Result<u32> {
        let now = now_secs();
        self.store.cleanup(max_completed_age_secs, now)
    }

    /// Get aggregate queue statistics.
    pub fn stats(&self) -> Result<QueueStats> {
        self.store.count_by_status()
    }

    /// Persist all data to disk.
    pub fn flush(&self) -> Result<()> {
        self.store.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn test_engine() -> (Engine, TempDir) {
        let dir = TempDir::new().unwrap();
        let engine = Engine::open(dir.path().to_str().unwrap(), 1000, 300).unwrap();
        (engine, dir)
    }

    #[test]
    fn test_submit_and_claim() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test.echo", b"hello", TaskPriority::Normal, 3, 0)
            .unwrap();
        assert!(tid > 0);

        let task = engine.claim_next("w-0", 300).unwrap().unwrap();
        assert_eq!(task.task_id, tid);
        assert_eq!(task.status, TaskStatus::Running);
        assert_eq!(task.attempt, 1);
        assert_eq!(task.claimed_by.as_deref(), Some("w-0"));
    }

    #[test]
    fn test_full_lifecycle_happy_path() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test.echo", b"data", TaskPriority::Normal, 3, 0)
            .unwrap();

        // Claim
        engine.claim_next("w-0", 300).unwrap().unwrap();

        // Heartbeat
        let alive = engine.heartbeat(tid, 50, "halfway").unwrap();
        assert!(alive);

        // Complete
        engine.complete(tid, b"result").unwrap();

        // Verify final state
        let task = engine.status(tid).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::Completed);
        assert_eq!(task.result.as_deref(), Some(b"result".as_slice()));
    }

    #[test]
    fn test_claim_returns_highest_priority() {
        let (engine, _dir) = test_engine();
        engine.submit("low", b"", TaskPriority::Low, 0, 0).unwrap();
        engine
            .submit("critical", b"", TaskPriority::Critical, 0, 0)
            .unwrap();
        engine
            .submit("normal", b"", TaskPriority::Normal, 0, 0)
            .unwrap();

        let t1 = engine.claim_next("w-0", 300).unwrap().unwrap();
        assert_eq!(t1.task_type, "critical");

        let t2 = engine.claim_next("w-0", 300).unwrap().unwrap();
        assert_eq!(t2.task_type, "normal");

        let t3 = engine.claim_next("w-0", 300).unwrap().unwrap();
        assert_eq!(t3.task_type, "low");
    }

    #[test]
    fn test_fail_and_retry() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test", b"", TaskPriority::Normal, 3, 0)
            .unwrap();

        engine.claim_next("w-0", 300).unwrap();
        engine.fail(tid, "oops").unwrap();

        // Should be back in pending (attempt 1 < max_retries 3)
        let task = engine.status(tid).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::Pending);
        assert_eq!(task.attempt, 1);
    }

    #[test]
    fn test_fail_dead_letter() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test", b"", TaskPriority::Normal, 1, 0)
            .unwrap();

        engine.claim_next("w-0", 300).unwrap();
        engine.fail(tid, "fatal").unwrap();

        let task = engine.status(tid).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::DeadLetter);
    }

    #[test]
    fn test_cancel_pending() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test", b"", TaskPriority::Normal, 0, 0)
            .unwrap();

        engine.cancel(tid).unwrap();

        let task = engine.status(tid).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::Cancelled);

        // Queue should be empty
        assert!(engine.claim_next("w-0", 300).unwrap().is_none());
    }

    #[test]
    fn test_cancel_running() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test", b"", TaskPriority::Normal, 0, 0)
            .unwrap();

        engine.claim_next("w-0", 300).unwrap();
        engine.cancel(tid).unwrap();

        let task = engine.status(tid).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::Cancelled);

        // Heartbeat should return false (cancelled)
        // The task is now cancelled, so heartbeat should detect it
    }

    #[test]
    fn test_heartbeat_cancelled_returns_false() {
        let (engine, _dir) = test_engine();
        let tid = engine
            .submit("test", b"", TaskPriority::Normal, 0, 0)
            .unwrap();

        engine.claim_next("w-0", 300).unwrap();
        engine.cancel(tid).unwrap();

        let alive = engine.heartbeat(tid, 50, "check").unwrap();
        assert!(!alive);
    }

    #[test]
    fn test_admission_control() {
        let dir = TempDir::new().unwrap();
        let engine = Engine::open(dir.path().to_str().unwrap(), 2, 0).unwrap();

        engine.submit("a", b"", TaskPriority::Normal, 0, 0).unwrap();
        engine.submit("b", b"", TaskPriority::Normal, 0, 0).unwrap();

        // Third should be rejected
        let result = engine.submit("c", b"", TaskPriority::Normal, 0, 0);
        assert!(matches!(result, Err(TaskError::QueueFull { .. })));
    }

    #[test]
    fn test_stats() {
        let (engine, _dir) = test_engine();

        engine.submit("a", b"", TaskPriority::Normal, 0, 0).unwrap();
        engine.submit("b", b"", TaskPriority::Normal, 0, 0).unwrap();

        let stats = engine.stats().unwrap();
        assert_eq!(stats.pending, 2);
        assert_eq!(stats.running, 0);

        engine.claim_next("w-0", 300).unwrap();

        let stats = engine.stats().unwrap();
        assert_eq!(stats.pending, 1);
        assert_eq!(stats.running, 1);
    }

    #[test]
    fn test_list_tasks() {
        let (engine, _dir) = test_engine();

        engine
            .submit("type_a", b"", TaskPriority::Normal, 0, 0)
            .unwrap();
        engine
            .submit("type_b", b"", TaskPriority::Normal, 0, 0)
            .unwrap();
        engine
            .submit("type_a", b"", TaskPriority::High, 0, 0)
            .unwrap();

        let all = engine.list_tasks(None, None, 100, 0).unwrap();
        assert_eq!(all.len(), 3);

        let type_a = engine.list_tasks(None, Some("type_a"), 100, 0).unwrap();
        assert_eq!(type_a.len(), 2);

        let pending = engine
            .list_tasks(Some(TaskStatus::Pending), None, 100, 0)
            .unwrap();
        assert_eq!(pending.len(), 3);
    }

    #[test]
    fn test_not_found_errors() {
        let (engine, _dir) = test_engine();

        assert!(matches!(
            engine.complete(999, b""),
            Err(TaskError::NotFound(999))
        ));
        assert!(matches!(
            engine.fail(999, "err"),
            Err(TaskError::NotFound(999))
        ));
        assert!(matches!(engine.cancel(999), Err(TaskError::NotFound(999))));
        assert!(matches!(
            engine.heartbeat(999, 0, ""),
            Err(TaskError::NotFound(999))
        ));
    }
}

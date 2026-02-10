pub mod engine;
pub mod error;
pub mod priority;
pub mod retry;
pub mod store;
pub mod task;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

use crate::engine::Engine;
use crate::task::{TaskPriority, TaskStatus};

/// Python-visible task record.
#[pyclass(frozen, name = "TaskRecord")]
pub struct PyTaskRecord {
    #[pyo3(get)]
    pub task_id: u64,
    #[pyo3(get)]
    pub task_type: String,
    #[pyo3(get)]
    pub params: Vec<u8>,
    #[pyo3(get)]
    pub priority: u8,
    #[pyo3(get)]
    pub status: u8,
    #[pyo3(get)]
    pub result: Option<Vec<u8>>,
    #[pyo3(get)]
    pub error_message: Option<String>,
    #[pyo3(get)]
    pub attempt: u32,
    #[pyo3(get)]
    pub max_retries: u32,
    #[pyo3(get)]
    pub created_at: u64,
    #[pyo3(get)]
    pub run_at: u64,
    #[pyo3(get)]
    pub claimed_by: Option<String>,
    #[pyo3(get)]
    pub progress_pct: u8,
    #[pyo3(get)]
    pub progress_message: Option<String>,
    #[pyo3(get)]
    pub completed_at: Option<u64>,
}

impl From<task::TaskRecord> for PyTaskRecord {
    fn from(r: task::TaskRecord) -> Self {
        Self {
            task_id: r.task_id,
            task_type: r.task_type,
            params: r.params,
            priority: r.priority as u8,
            status: r.status as u8,
            result: r.result,
            error_message: r.error_message,
            attempt: r.attempt,
            max_retries: r.max_retries,
            created_at: r.created_at,
            run_at: r.run_at,
            claimed_by: r.claimed_by,
            progress_pct: r.progress_pct,
            progress_message: r.progress_message,
            completed_at: r.completed_at,
        }
    }
}

#[pymethods]
impl PyTaskRecord {
    fn __repr__(&self) -> String {
        format!(
            "TaskRecord(id={}, type='{}', status={}, attempt={}/{})",
            self.task_id, self.task_type, self.status, self.attempt, self.max_retries
        )
    }
}

/// Python-visible queue statistics.
#[pyclass(frozen, name = "QueueStats")]
pub struct PyQueueStats {
    #[pyo3(get)]
    pub pending: usize,
    #[pyo3(get)]
    pub running: usize,
    #[pyo3(get)]
    pub completed: usize,
    #[pyo3(get)]
    pub failed: usize,
    #[pyo3(get)]
    pub dead_letter: usize,
    #[pyo3(get)]
    pub cancelled: usize,
}

#[pymethods]
impl PyQueueStats {
    fn __repr__(&self) -> String {
        format!(
            "QueueStats(pending={}, running={}, completed={}, failed={}, dead_letter={}, cancelled={})",
            self.pending, self.running, self.completed, self.failed, self.dead_letter, self.cancelled
        )
    }
}

/// Convert internal errors to Python RuntimeError.
fn to_py_err(e: error::TaskError) -> PyErr {
    PyRuntimeError::new_err(format!("{e}"))
}

/// The main task engine exposed to Python.
///
/// Thread-safe: all methods take &self. fjall handles internal concurrency.
#[pyclass(frozen, name = "TaskEngine")]
pub struct PyTaskEngine {
    engine: Engine,
}

#[pymethods]
impl PyTaskEngine {
    /// Create a new TaskEngine backed by fjall storage at db_path.
    ///
    /// Args:
    ///     db_path: Path to the fjall database directory
    ///     max_pending: Maximum number of pending tasks (0 = unlimited)
    ///     max_wait_secs: Anti-starvation threshold in seconds (0 = disabled)
    #[new]
    #[pyo3(signature = (db_path, max_pending=1000, max_wait_secs=300))]
    fn new(db_path: &str, max_pending: usize, max_wait_secs: u64) -> PyResult<Self> {
        let engine = Engine::open(db_path, max_pending, max_wait_secs).map_err(to_py_err)?;
        Ok(Self { engine })
    }

    /// Submit a new task to the queue.
    ///
    /// Args:
    ///     task_type: Identifier for the task handler (e.g. "sync.full")
    ///     params: Opaque payload bytes (Python serializes/deserializes)
    ///     priority: 0=Critical, 1=High, 2=Normal, 3=Low, 4=BestEffort
    ///     max_retries: Maximum retry attempts before dead-lettering
    ///     run_at: Unix timestamp for scheduled execution (0 = immediate)
    ///
    /// Returns: task_id (u64)
    #[pyo3(signature = (task_type, params, priority=2, max_retries=3, run_at=0))]
    fn submit(
        &self,
        task_type: &str,
        params: &[u8],
        priority: u8,
        max_retries: u32,
        run_at: u64,
    ) -> PyResult<u64> {
        let p = TaskPriority::from_u8(priority).ok_or_else(|| {
            PyRuntimeError::new_err(format!("invalid priority: {priority} (must be 0-4)"))
        })?;
        self.engine
            .submit(task_type, params, p, max_retries, run_at)
            .map_err(to_py_err)
    }

    /// Claim the next available task for this worker.
    ///
    /// Returns: TaskRecord or None if no tasks available.
    #[pyo3(signature = (worker_id, lease_secs=300))]
    fn claim_next(&self, worker_id: &str, lease_secs: u32) -> PyResult<Option<PyTaskRecord>> {
        self.engine
            .claim_next(worker_id, lease_secs)
            .map(|opt| opt.map(PyTaskRecord::from))
            .map_err(to_py_err)
    }

    /// Send a heartbeat / progress update for a running task.
    ///
    /// Returns: True if task is still active, False if it was cancelled.
    #[pyo3(signature = (task_id, progress_pct=0, message=""))]
    fn heartbeat(&self, task_id: u64, progress_pct: u8, message: &str) -> PyResult<bool> {
        self.engine
            .heartbeat(task_id, progress_pct, message)
            .map_err(to_py_err)
    }

    /// Mark a task as completed with an optional result payload.
    #[pyo3(signature = (task_id, result=vec![]))]
    fn complete(&self, task_id: u64, result: Vec<u8>) -> PyResult<()> {
        self.engine.complete(task_id, &result).map_err(to_py_err)
    }

    /// Mark a task as failed. Will auto-retry if attempts remain.
    fn fail(&self, task_id: u64, error_message: &str) -> PyResult<()> {
        self.engine.fail(task_id, error_message).map_err(to_py_err)
    }

    /// Cancel a pending or running task.
    fn cancel(&self, task_id: u64) -> PyResult<()> {
        self.engine.cancel(task_id).map_err(to_py_err)
    }

    /// Get the current status/details of a task.
    fn status(&self, task_id: u64) -> PyResult<Option<PyTaskRecord>> {
        self.engine
            .status(task_id)
            .map(|opt| opt.map(PyTaskRecord::from))
            .map_err(to_py_err)
    }

    /// List tasks with optional filters.
    ///
    /// Args:
    ///     status: Filter by status (0-5), or None for all
    ///     task_type: Filter by task type string, or None for all
    ///     limit: Maximum results to return
    ///     offset: Skip this many results
    #[pyo3(signature = (status=None, task_type=None, limit=100, offset=0))]
    fn list_tasks(
        &self,
        status: Option<u8>,
        task_type: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> PyResult<Vec<PyTaskRecord>> {
        let status_filter = match status {
            Some(v) => Some(TaskStatus::from_u8(v).ok_or_else(|| {
                PyRuntimeError::new_err(format!("invalid status: {v} (must be 0-5)"))
            })?),
            None => None,
        };
        self.engine
            .list_tasks(status_filter, task_type, limit, offset)
            .map(|v| v.into_iter().map(PyTaskRecord::from).collect())
            .map_err(to_py_err)
    }

    /// Requeue tasks with expired leases. Returns count of requeued tasks.
    fn requeue_abandoned(&self) -> PyResult<u32> {
        self.engine.requeue_abandoned().map_err(to_py_err)
    }

    /// Remove old completed/failed tasks. Returns count of cleaned tasks.
    #[pyo3(signature = (max_completed_age_secs=86400))]
    fn cleanup(&self, max_completed_age_secs: u64) -> PyResult<u32> {
        self.engine
            .cleanup(max_completed_age_secs)
            .map_err(to_py_err)
    }

    /// Get aggregate queue statistics.
    fn stats(&self) -> PyResult<PyQueueStats> {
        let s = self.engine.stats().map_err(to_py_err)?;
        Ok(PyQueueStats {
            pending: s.pending,
            running: s.running,
            completed: s.completed,
            failed: s.failed,
            dead_letter: s.dead_letter,
            cancelled: s.cancelled,
        })
    }
}

/// Python module: _nexus_tasks
#[pymodule]
fn _nexus_tasks(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyTaskEngine>()?;
    m.add_class::<PyTaskRecord>()?;
    m.add_class::<PyQueueStats>()?;
    Ok(())
}

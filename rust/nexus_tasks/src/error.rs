use thiserror::Error;

#[derive(Error, Debug)]
pub enum TaskError {
    #[error("storage error: {0}")]
    Storage(String),

    #[error("serialization error: {0}")]
    Serialization(#[from] bincode::Error),

    #[error("task not found: {0}")]
    NotFound(u64),

    #[error("invalid state transition: {current} -> {target} for task {task_id}")]
    InvalidTransition {
        task_id: u64,
        current: String,
        target: String,
    },

    #[error("queue full: {pending} pending tasks (max: {max_pending})")]
    QueueFull { pending: usize, max_pending: usize },
}

pub type Result<T> = std::result::Result<T, TaskError>;

// Convert fjall errors to our error type
impl From<fjall::Error> for TaskError {
    fn from(e: fjall::Error) -> Self {
        TaskError::Storage(e.to_string())
    }
}

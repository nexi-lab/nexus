use serde::{Deserialize, Serialize};

/// Task execution status. Repr values are used as storage discriminants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum TaskStatus {
    Pending = 0,
    Running = 1,
    Completed = 2,
    Failed = 3,
    DeadLetter = 4,
    Cancelled = 5,
}

impl TaskStatus {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Self::Pending),
            1 => Some(Self::Running),
            2 => Some(Self::Completed),
            3 => Some(Self::Failed),
            4 => Some(Self::DeadLetter),
            5 => Some(Self::Cancelled),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "PENDING",
            Self::Running => "RUNNING",
            Self::Completed => "COMPLETED",
            Self::Failed => "FAILED",
            Self::DeadLetter => "DEAD_LETTER",
            Self::Cancelled => "CANCELLED",
        }
    }

    /// Whether this status is a terminal state (no further transitions).
    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Completed | Self::DeadLetter | Self::Cancelled)
    }
}

impl std::fmt::Display for TaskStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Task priority levels. Lower u8 value = higher priority (for composite key sort).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[repr(u8)]
pub enum TaskPriority {
    Critical = 0,
    High = 1,
    Normal = 2,
    Low = 3,
    BestEffort = 4,
}

impl TaskPriority {
    pub fn from_u8(v: u8) -> Option<Self> {
        match v {
            0 => Some(Self::Critical),
            1 => Some(Self::High),
            2 => Some(Self::Normal),
            3 => Some(Self::Low),
            4 => Some(Self::BestEffort),
            _ => None,
        }
    }
}

/// The primary task record stored in fjall.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskRecord {
    pub task_id: u64,
    pub task_type: String,
    pub params: Vec<u8>,
    pub priority: TaskPriority,
    pub status: TaskStatus,
    pub result: Option<Vec<u8>>,
    pub error_message: Option<String>,
    pub attempt: u32,
    pub max_retries: u32,
    pub created_at: u64,
    pub run_at: u64,
    pub claimed_at: Option<u64>,
    pub claimed_by: Option<String>,
    pub lease_secs: u32,
    pub completed_at: Option<u64>,
    pub progress_pct: u8,
    pub progress_message: Option<String>,
}

/// Aggregate queue statistics.
#[derive(Debug, Clone, Default)]
pub struct QueueStats {
    pub pending: usize,
    pub running: usize,
    pub completed: usize,
    pub failed: usize,
    pub dead_letter: usize,
    pub cancelled: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_status_roundtrip() {
        for status in [
            TaskStatus::Pending,
            TaskStatus::Running,
            TaskStatus::Completed,
            TaskStatus::Failed,
            TaskStatus::DeadLetter,
            TaskStatus::Cancelled,
        ] {
            let v = status as u8;
            assert_eq!(TaskStatus::from_u8(v), Some(status));
        }
        assert_eq!(TaskStatus::from_u8(255), None);
    }

    #[test]
    fn test_status_is_terminal() {
        assert!(!TaskStatus::Pending.is_terminal());
        assert!(!TaskStatus::Running.is_terminal());
        assert!(TaskStatus::Completed.is_terminal());
        assert!(!TaskStatus::Failed.is_terminal());
        assert!(TaskStatus::DeadLetter.is_terminal());
        assert!(TaskStatus::Cancelled.is_terminal());
    }

    #[test]
    fn test_priority_ordering() {
        assert!(TaskPriority::Critical < TaskPriority::High);
        assert!(TaskPriority::High < TaskPriority::Normal);
        assert!(TaskPriority::Normal < TaskPriority::Low);
        assert!(TaskPriority::Low < TaskPriority::BestEffort);
    }

    #[test]
    fn test_serialization_roundtrip() {
        let record = TaskRecord {
            task_id: 42,
            task_type: "test.echo".to_string(),
            params: vec![1, 2, 3],
            priority: TaskPriority::Normal,
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
        };

        let bytes = bincode::serialize(&record).unwrap();
        let decoded: TaskRecord = bincode::deserialize(&bytes).unwrap();
        assert_eq!(decoded.task_id, 42);
        assert_eq!(decoded.task_type, "test.echo");
        assert_eq!(decoded.params, vec![1, 2, 3]);
        assert_eq!(decoded.priority, TaskPriority::Normal);
        assert_eq!(decoded.status, TaskStatus::Pending);
        assert_eq!(decoded.max_retries, 3);
    }
}

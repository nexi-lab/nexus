use crate::task::TaskPriority;

/// Composite pending-queue key: [priority: 1 byte][run_at: 8 bytes BE][task_id: 8 bytes BE]
/// Total: 17 bytes. Natural byte-order sort = highest priority first, then earliest time, then lowest ID.
pub const PENDING_KEY_LEN: usize = 17;

pub fn encode_pending_key(
    priority: TaskPriority,
    run_at: u64,
    task_id: u64,
) -> [u8; PENDING_KEY_LEN] {
    let mut key = [0u8; PENDING_KEY_LEN];
    key[0] = priority as u8;
    key[1..9].copy_from_slice(&run_at.to_be_bytes());
    key[9..17].copy_from_slice(&task_id.to_be_bytes());
    key
}

pub fn decode_pending_key(key: &[u8]) -> Option<(u8, u64, u64)> {
    if key.len() != PENDING_KEY_LEN {
        return None;
    }
    let priority = key[0];
    let run_at = u64::from_be_bytes(key[1..9].try_into().ok()?);
    let task_id = u64::from_be_bytes(key[9..17].try_into().ok()?);
    Some((priority, run_at, task_id))
}

/// Running-index key: [lease_expires: 8 bytes BE][task_id: 8 bytes BE]
/// Total: 16 bytes. Sorted by expiry time for efficient abandoned-task scanning.
pub const RUNNING_KEY_LEN: usize = 16;

pub fn encode_running_key(lease_expires: u64, task_id: u64) -> [u8; RUNNING_KEY_LEN] {
    let mut key = [0u8; RUNNING_KEY_LEN];
    key[0..8].copy_from_slice(&lease_expires.to_be_bytes());
    key[8..16].copy_from_slice(&task_id.to_be_bytes());
    key
}

pub fn decode_running_key(key: &[u8]) -> Option<(u64, u64)> {
    if key.len() != RUNNING_KEY_LEN {
        return None;
    }
    let lease_expires = u64::from_be_bytes(key[0..8].try_into().ok()?);
    let task_id = u64::from_be_bytes(key[8..16].try_into().ok()?);
    Some((lease_expires, task_id))
}

/// Anti-starvation: check if the oldest task has waited beyond the threshold.
pub fn should_promote_oldest(oldest_run_at: u64, now: u64, max_wait_secs: u64) -> bool {
    now.saturating_sub(oldest_run_at) > max_wait_secs
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pending_key_roundtrip() {
        let key = encode_pending_key(TaskPriority::High, 1700000000, 42);
        let (priority, run_at, task_id) = decode_pending_key(&key).unwrap();
        assert_eq!(priority, TaskPriority::High as u8);
        assert_eq!(run_at, 1700000000);
        assert_eq!(task_id, 42);
    }

    #[test]
    fn test_pending_key_sort_order() {
        let critical = encode_pending_key(TaskPriority::Critical, 100, 1);
        let high = encode_pending_key(TaskPriority::High, 100, 1);
        let normal = encode_pending_key(TaskPriority::Normal, 100, 1);
        assert!(critical < high);
        assert!(high < normal);

        // Same priority: earlier run_at wins
        let early = encode_pending_key(TaskPriority::Normal, 100, 1);
        let late = encode_pending_key(TaskPriority::Normal, 200, 1);
        assert!(early < late);

        // Same priority and run_at: lower task_id wins
        let id1 = encode_pending_key(TaskPriority::Normal, 100, 1);
        let id2 = encode_pending_key(TaskPriority::Normal, 100, 2);
        assert!(id1 < id2);
    }

    #[test]
    fn test_running_key_roundtrip() {
        let key = encode_running_key(1700000300, 42);
        let (lease_expires, task_id) = decode_running_key(&key).unwrap();
        assert_eq!(lease_expires, 1700000300);
        assert_eq!(task_id, 42);
    }

    #[test]
    fn test_anti_starvation() {
        assert!(!should_promote_oldest(100, 200, 300)); // waited 100s < 300s threshold
        assert!(should_promote_oldest(100, 500, 300)); // waited 400s > 300s threshold
        assert!(!should_promote_oldest(100, 400, 300)); // waited exactly 300s, not > 300s
    }

    #[test]
    fn test_decode_invalid_length() {
        assert!(decode_pending_key(&[0u8; 5]).is_none());
        assert!(decode_running_key(&[0u8; 5]).is_none());
    }
}

/// Calculate next retry delay using exponential backoff with deterministic jitter.
/// Delays: 1s, 5s, 30s, 5m, 30m (capped).
///
/// Uses task_id for deterministic jitter (up to 20%) to avoid thundering herd
/// without requiring a `rand` dependency.
pub fn backoff_secs(attempt: u32, task_id: u64) -> u64 {
    let base: u64 = match attempt {
        0 => 1,
        1 => 5,
        2 => 30,
        3 => 300,
        _ => 1800,
    };
    // Deterministic jitter: 0-20% based on task_id
    let jitter_frac = (task_id % 200) as f64 / 1000.0; // 0.000 to 0.199
    let jitter = (base as f64 * jitter_frac) as u64;
    base + jitter
}

/// Whether a task should be moved to the dead letter queue.
pub fn should_dead_letter(attempt: u32, max_retries: u32) -> bool {
    attempt >= max_retries
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_backoff_progression() {
        // Base delays (jitter = 0 when task_id % 200 == 0)
        assert_eq!(backoff_secs(0, 0), 1);
        assert_eq!(backoff_secs(1, 0), 5);
        assert_eq!(backoff_secs(2, 0), 30);
        assert_eq!(backoff_secs(3, 0), 300);
        assert_eq!(backoff_secs(4, 0), 1800);
        assert_eq!(backoff_secs(99, 0), 1800); // capped
    }

    #[test]
    fn test_backoff_with_jitter() {
        // task_id=100 → jitter_frac = 100/1000 = 0.1 → 10% jitter
        let delay = backoff_secs(2, 100); // base=30, +10% = 33
        assert_eq!(delay, 33);

        // task_id=199 → jitter_frac = 199/1000 = 0.199 → ~20% jitter
        let delay = backoff_secs(2, 199); // base=30, +19.9% ≈ 35
        assert_eq!(delay, 35);
    }

    #[test]
    fn test_dead_letter_logic() {
        assert!(!should_dead_letter(0, 3));
        assert!(!should_dead_letter(2, 3));
        assert!(should_dead_letter(3, 3));
        assert!(should_dead_letter(4, 3));
        assert!(should_dead_letter(0, 0)); // max_retries=0 means immediate dead letter
    }
}

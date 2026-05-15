use serde::{Deserialize, Serialize};

pub const DEFAULT_LATENCY_FLUSH_WINDOW_MS: u64 = 1_000;
pub const DEFAULT_BATCH_FLUSH_WINDOW_MS: u64 = 60_000;
pub const DEFAULT_WRITE_COALESCING_BYTE_BUDGET: usize = 4 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum WriteCoalescingMode {
    Strict,
    Latency,
    Batch,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct WriteCoalescingPolicy {
    pub mode: WriteCoalescingMode,
    pub flush_window_ms: u64,
    pub byte_budget: usize,
    pub flush_on_close: bool,
}

impl WriteCoalescingPolicy {
    pub fn strict() -> Self {
        Self {
            mode: WriteCoalescingMode::Strict,
            flush_window_ms: 0,
            byte_budget: 0,
            flush_on_close: true,
        }
    }

    pub fn latency() -> Self {
        Self {
            mode: WriteCoalescingMode::Latency,
            flush_window_ms: DEFAULT_LATENCY_FLUSH_WINDOW_MS,
            byte_budget: DEFAULT_WRITE_COALESCING_BYTE_BUDGET,
            flush_on_close: true,
        }
    }

    pub fn batch() -> Self {
        Self {
            mode: WriteCoalescingMode::Batch,
            flush_window_ms: DEFAULT_BATCH_FLUSH_WINDOW_MS,
            byte_budget: DEFAULT_WRITE_COALESCING_BYTE_BUDGET,
            flush_on_close: true,
        }
    }

    pub fn enabled(&self) -> bool {
        self.mode != WriteCoalescingMode::Strict
    }
}

impl Default for WriteCoalescingPolicy {
    fn default() -> Self {
        Self::strict()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latency_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::latency();
        assert_eq!(policy.mode, WriteCoalescingMode::Latency);
        assert_eq!(policy.flush_window_ms, 1_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn batch_defaults_match_issue_4059() {
        let policy = WriteCoalescingPolicy::batch();
        assert_eq!(policy.mode, WriteCoalescingMode::Batch);
        assert_eq!(policy.flush_window_ms, 60_000);
        assert_eq!(policy.byte_budget, 4 * 1024 * 1024);
        assert!(policy.flush_on_close);
        assert!(policy.enabled());
    }

    #[test]
    fn strict_policy_disables_buffering() {
        let policy = WriteCoalescingPolicy::strict();
        assert_eq!(policy.mode, WriteCoalescingMode::Strict);
        assert_eq!(policy.flush_window_ms, 0);
        assert_eq!(policy.byte_budget, 0);
        assert!(policy.flush_on_close);
        assert!(!policy.enabled());
    }

    #[test]
    fn default_policy_preserves_write_through_visibility() {
        assert_eq!(WriteCoalescingPolicy::default(), WriteCoalescingPolicy::strict());
    }
}

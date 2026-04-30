//! Bloom filter for fast set-membership checks.
//!
//! A simple hand-rolled Bloom filter using `ahash` for WASM compatibility.
//! The `bloomfilter` crate has uncertain WASM support, so we implement our own.

use ahash::AHasher;
use std::hash::{Hash, Hasher};

/// A Bloom filter for fast probabilistic set-membership testing.
///
/// - False positives possible (says "maybe" when item is absent)
/// - False negatives impossible (never says "absent" when item is present)
/// - O(1) lookup regardless of set size
pub struct BloomFilter {
    bits: Vec<u64>,
    num_bits: usize,
    num_hashes: u32,
    capacity: usize,
    fp_rate: f64,
}

impl BloomFilter {
    /// Create a new Bloom filter for `expected_items` items with target `fp_rate`.
    pub fn new(expected_items: usize, fp_rate: f64) -> Self {
        let expected_items = expected_items.max(1);
        let fp_rate = fp_rate.clamp(1e-10, 1.0);

        // Optimal number of bits: m = -n * ln(p) / (ln(2))^2
        let num_bits =
            (-(expected_items as f64) * fp_rate.ln() / (2.0_f64.ln().powi(2))).ceil() as usize;
        let num_bits = num_bits.max(64);

        // Optimal number of hashes: k = (m/n) * ln(2)
        let num_hashes = ((num_bits as f64 / expected_items as f64) * 2.0_f64.ln()).ceil() as u32;
        let num_hashes = num_hashes.clamp(1, 30);

        let num_words = num_bits.div_ceil(64);

        BloomFilter {
            bits: vec![0u64; num_words],
            num_bits,
            num_hashes,
            capacity: expected_items,
            fp_rate,
        }
    }

    /// Add an item to the filter.
    pub fn add<T: Hash>(&mut self, item: &T) {
        for i in 0..self.num_hashes {
            let bit_index = self.hash_index(item, i);
            let word = bit_index / 64;
            let bit = bit_index % 64;
            self.bits[word] |= 1u64 << bit;
        }
    }

    /// Check if an item might exist in the filter.
    ///
    /// Returns `false` if the item is definitely absent,
    /// `true` if it might be present (possible false positive).
    pub fn might_contain<T: Hash>(&self, item: &T) -> bool {
        for i in 0..self.num_hashes {
            let bit_index = self.hash_index(item, i);
            let word = bit_index / 64;
            let bit = bit_index % 64;
            if self.bits[word] & (1u64 << bit) == 0 {
                return false;
            }
        }
        true
    }

    /// Clear all entries, resetting to empty.
    pub fn clear(&mut self) {
        self.bits.fill(0);
    }

    /// Expected item capacity.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Target false positive rate.
    pub fn fp_rate(&self) -> f64 {
        self.fp_rate
    }

    /// Approximate memory usage in bytes.
    pub fn memory_bytes(&self) -> usize {
        self.bits.len() * 8
    }

    fn hash_index<T: Hash>(&self, item: &T, seed: u32) -> usize {
        let mut hasher = AHasher::default();
        seed.hash(&mut hasher);
        item.hash(&mut hasher);
        (hasher.finish() as usize) % self.num_bits
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_and_check_no_false_negatives() {
        let mut bloom = BloomFilter::new(1000, 0.01);
        for i in 0..100 {
            bloom.add(&format!("key-{i}"));
        }
        // No false negatives — every added item must return true
        for i in 0..100 {
            assert!(
                bloom.might_contain(&format!("key-{i}")),
                "false negative for key-{i}"
            );
        }
    }

    #[test]
    fn false_positive_rate_within_bounds() {
        let n = 10_000;
        let target_fp = 0.01;
        let mut bloom = BloomFilter::new(n, target_fp);

        for i in 0..n {
            bloom.add(&i);
        }

        // Test items never inserted
        let test_range = n..(n + 10_000);
        let false_positives = test_range
            .clone()
            .filter(|i| bloom.might_contain(i))
            .count();
        let actual_fp_rate = false_positives as f64 / test_range.len() as f64;

        // Allow 3x target (generous bound for probabilistic test)
        assert!(
            actual_fp_rate < target_fp * 3.0,
            "FP rate {actual_fp_rate:.4} exceeds 3x target {target_fp}"
        );
    }

    #[test]
    fn clear_and_recheck() {
        let mut bloom = BloomFilter::new(100, 0.01);
        bloom.add(&"hello");
        assert!(bloom.might_contain(&"hello"));

        bloom.clear();
        // After clear, the item should (very likely) not be found
        assert!(!bloom.might_contain(&"hello"));
    }
}

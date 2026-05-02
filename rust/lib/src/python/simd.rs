//! SIMD-accelerated vector similarity via SimSIMD.

use pyo3::prelude::*;
use rayon::prelude::*;
use simsimd::SpatialSimilarity;
use std::cmp::{Ordering, Reverse};
use std::collections::BinaryHeap;

// ---------------------------------------------------------------------------
// TotalF64: newtype for total ordering on f64 via std::f64::total_cmp()
// ---------------------------------------------------------------------------

/// Wrapper around f64 providing total ordering without an external crate.
///
/// NaN sorts after +Inf with `total_cmp`, but we filter NaN before heap
/// insertion in `top_k_by_similarity`, so this only affects tie-breaking.
#[derive(Debug, Clone, Copy, PartialEq)]
struct TotalF64(f64);

impl Eq for TotalF64 {}

impl PartialOrd for TotalF64 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for TotalF64 {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.total_cmp(&other.0)
    }
}

// ---------------------------------------------------------------------------
// Heap entry for top-K selection
// ---------------------------------------------------------------------------

/// Min-heap entry: reversed comparison so Rust's BinaryHeap (max-heap)
/// acts as a min-heap, popping the smallest score when size exceeds K.
#[derive(Debug, Clone, PartialEq, Eq)]
struct HeapEntry {
    score: TotalF64,
    index: usize,
}

impl Ord for HeapEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .score
            .cmp(&self.score)
            .then_with(|| other.index.cmp(&self.index))
    }
}

impl PartialOrd for HeapEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

// ---------------------------------------------------------------------------
// Generic top-K selection (heap-based, O(n log k))
// ---------------------------------------------------------------------------

/// Compute similarity for each vector and return top K results (descending).
///
/// - Filters NaN scores (from zero-length vectors or computation failures).
/// - Uses min-heap for O(n log k) instead of O(n log n) full sort.
/// - Parallelizes for batches above PARALLEL_THRESHOLD.
fn top_k_by_similarity<T: Sync>(
    query: &[T],
    vectors: &[Vec<T>],
    k: usize,
    sim_fn: impl Fn(&[T], &[T]) -> f64 + Sync,
) -> Vec<(usize, f64)> {
    const PARALLEL_THRESHOLD: usize = 100;

    if vectors.is_empty() || k == 0 {
        return Vec::new();
    }

    let scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| (i, sim_fn(query, v)))
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| (i, sim_fn(query, v)))
            .collect()
    };

    let mut heap: BinaryHeap<HeapEntry> = BinaryHeap::with_capacity(k + 1);

    for (index, score) in scores {
        if score.is_nan() {
            continue;
        }
        heap.push(HeapEntry {
            score: TotalF64(score),
            index,
        });
        if heap.len() > k {
            heap.pop();
        }
    }

    let mut result: Vec<(usize, f64)> = heap.into_iter().map(|e| (e.index, e.score.0)).collect();
    result.sort_by_key(|b| Reverse(TotalF64(b.1)));
    result
}

/// Cosine similarity helper: converts SimSIMD distance to similarity.
/// Returns 0.0 on SimSIMD failure to preserve backward compatibility
/// (callers expect exactly k results; batch_cosine_similarity also uses 0.0).
fn cosine_sim_f32(a: &[f32], b: &[f32]) -> f64 {
    <f32 as SpatialSimilarity>::cos(a, b)
        .map(|dist| 1.0 - dist)
        .unwrap_or(0.0)
}

fn cosine_sim_i8(a: &[i8], b: &[i8]) -> f64 {
    <i8 as SpatialSimilarity>::cos(a, b)
        .map(|dist| 1.0 - dist)
        .unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// PyO3 exports — f32
// ---------------------------------------------------------------------------

/// Compute cosine similarity between two f32 vectors using SIMD.
#[pyfunction]
pub fn cosine_similarity_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    <f32 as SpatialSimilarity>::cos(&a, &b)
        .map(|dist| 1.0 - dist)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD cosine computation failed"))
}

/// Compute dot product between two f32 vectors using SIMD.
#[pyfunction]
pub fn dot_product_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    <f32 as SpatialSimilarity>::dot(&a, &b).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("SIMD dot product computation failed")
    })
}

/// Compute squared Euclidean distance between two f32 vectors using SIMD.
#[pyfunction]
pub fn euclidean_sq_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    <f32 as SpatialSimilarity>::l2sq(&a, &b)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD L2 computation failed"))
}

/// Batch cosine similarity: compute similarity of query vs all vectors.
#[pyfunction]
pub fn batch_cosine_similarity_f32(
    py: Python<'_>,
    query: Vec<f32>,
    vectors: Vec<Vec<f32>>,
) -> PyResult<Vec<f64>> {
    if vectors.is_empty() {
        return Ok(vec![]);
    }

    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i,
                query_dim,
                v.len()
            )));
        }
    }

    Ok(py.detach(|| {
        const PARALLEL_THRESHOLD: usize = 100;

        if vectors.len() > PARALLEL_THRESHOLD {
            vectors
                .par_iter()
                .map(|v| {
                    <f32 as SpatialSimilarity>::cos(&query, v)
                        .map(|dist| 1.0 - dist)
                        .unwrap_or(0.0)
                })
                .collect()
        } else {
            vectors
                .iter()
                .map(|v| {
                    <f32 as SpatialSimilarity>::cos(&query, v)
                        .map(|dist| 1.0 - dist)
                        .unwrap_or(0.0)
                })
                .collect()
        }
    }))
}

/// Top-K similarity search using SIMD (f32).
#[pyfunction]
pub fn top_k_similar_f32(
    py: Python<'_>,
    query: Vec<f32>,
    vectors: Vec<Vec<f32>>,
    k: usize,
) -> PyResult<Vec<(usize, f64)>> {
    if vectors.is_empty() || k == 0 {
        return Ok(vec![]);
    }

    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i,
                query_dim,
                v.len()
            )));
        }
    }

    Ok(py.detach(|| top_k_by_similarity(&query, &vectors, k, cosine_sim_f32)))
}

// ---------------------------------------------------------------------------
// PyO3 exports — i8
// ---------------------------------------------------------------------------

/// Cosine similarity for int8 quantized vectors using SIMD.
#[pyfunction]
pub fn cosine_similarity_i8(a: Vec<i8>, b: Vec<i8>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    <i8 as SpatialSimilarity>::cos(&a, &b)
        .map(|dist| 1.0 - dist)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD i8 cosine computation failed"))
}

/// Batch cosine similarity for int8 quantized vectors.
#[pyfunction]
pub fn batch_cosine_similarity_i8(
    py: Python<'_>,
    query: Vec<i8>,
    vectors: Vec<Vec<i8>>,
) -> PyResult<Vec<f64>> {
    if vectors.is_empty() {
        return Ok(vec![]);
    }

    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i,
                query_dim,
                v.len()
            )));
        }
    }

    Ok(py.detach(|| {
        const PARALLEL_THRESHOLD: usize = 100;

        if vectors.len() > PARALLEL_THRESHOLD {
            vectors
                .par_iter()
                .map(|v| {
                    <i8 as SpatialSimilarity>::cos(&query, v)
                        .map(|dist| 1.0 - dist)
                        .unwrap_or(0.0)
                })
                .collect()
        } else {
            vectors
                .iter()
                .map(|v| {
                    <i8 as SpatialSimilarity>::cos(&query, v)
                        .map(|dist| 1.0 - dist)
                        .unwrap_or(0.0)
                })
                .collect()
        }
    }))
}

/// Top-K similarity search for int8 quantized vectors.
#[pyfunction]
pub fn top_k_similar_i8(
    py: Python<'_>,
    query: Vec<i8>,
    vectors: Vec<Vec<i8>>,
    k: usize,
) -> PyResult<Vec<(usize, f64)>> {
    if vectors.is_empty() || k == 0 {
        return Ok(vec![]);
    }

    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i,
                query_dim,
                v.len()
            )));
        }
    }

    Ok(py.detach(|| top_k_by_similarity(&query, &vectors, k, cosine_sim_i8)))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- TotalF64 ordering --------------------------------------------------

    #[test]
    fn test_total_f64_basic_ordering() {
        assert!(TotalF64(1.0) > TotalF64(0.5));
        assert!(TotalF64(-1.0) < TotalF64(0.0));
        assert!(TotalF64(0.0) == TotalF64(0.0));
    }

    #[test]
    fn test_total_f64_nan_ordering() {
        // NaN sorts after +Inf with total_cmp.
        assert!(TotalF64(f64::NAN) > TotalF64(f64::INFINITY));
        assert!(TotalF64(f64::NAN) > TotalF64(0.0));
    }

    #[test]
    fn test_total_f64_infinity() {
        assert!(TotalF64(f64::INFINITY) > TotalF64(f64::MAX));
        assert!(TotalF64(f64::NEG_INFINITY) < TotalF64(f64::MIN));
    }

    // -- HeapEntry ordering (reversed for min-heap) -------------------------

    #[test]
    fn test_heap_entry_min_heap_behavior() {
        let low = HeapEntry {
            score: TotalF64(0.1),
            index: 0,
        };
        let high = HeapEntry {
            score: TotalF64(0.9),
            index: 1,
        };
        // Reversed: low score > high score (so BinaryHeap pops low first).
        assert!(low > high);
    }

    // -- top_k_by_similarity ------------------------------------------------

    fn dummy_sim(a: &[f32], b: &[f32]) -> f64 {
        // Simple dot product as a test similarity function.
        a.iter()
            .zip(b.iter())
            .map(|(x, y)| (*x as f64) * (*y as f64))
            .sum()
    }

    #[test]
    fn test_top_k_basic() {
        let query = vec![1.0f32, 0.0];
        let vectors = vec![
            vec![1.0, 0.0], // sim = 1.0
            vec![0.0, 1.0], // sim = 0.0
            vec![0.5, 0.0], // sim = 0.5
        ];
        let result = top_k_by_similarity(&query, &vectors, 2, dummy_sim);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].0, 0); // index 0, score 1.0
        assert_eq!(result[1].0, 2); // index 2, score 0.5
    }

    #[test]
    fn test_top_k_returns_descending_order() {
        let query = vec![1.0f32];
        let vectors = vec![vec![3.0], vec![1.0], vec![5.0], vec![2.0], vec![4.0]];
        let result = top_k_by_similarity(&query, &vectors, 5, dummy_sim);
        for i in 1..result.len() {
            assert!(
                result[i - 1].1 >= result[i].1,
                "Results not in descending order"
            );
        }
    }

    #[test]
    fn test_top_k_nan_filtered() {
        // NaN scores (if produced by the sim function) are filtered out.
        fn sim_with_nan(a: &[f32], b: &[f32]) -> f64 {
            if b[0] == 0.0 {
                f64::NAN
            } else {
                dummy_sim(a, b)
            }
        }

        let query = vec![1.0f32];
        let vectors = vec![
            vec![3.0], // sim = 3.0
            vec![0.0], // sim = NaN (filtered)
            vec![2.0], // sim = 2.0
        ];
        let result = top_k_by_similarity(&query, &vectors, 10, sim_with_nan);
        assert_eq!(result.len(), 2); // NaN entry filtered out
        assert_eq!(result[0].0, 0); // index 0, score 3.0
        assert_eq!(result[1].0, 2); // index 2, score 2.0
    }

    #[test]
    fn test_top_k_all_nan() {
        fn always_nan(_a: &[f32], _b: &[f32]) -> f64 {
            f64::NAN
        }
        let query = vec![1.0f32];
        let vectors = vec![vec![1.0], vec![2.0]];
        let result = top_k_by_similarity(&query, &vectors, 10, always_nan);
        assert!(result.is_empty());
    }

    #[test]
    #[ignore = "requires Python interpreter (run via pytest or maturin develop)"]
    fn test_top_k_simsimd_failure_returns_zero() {
        // SimSIMD failures use unwrap_or(0.0) for backward compatibility.
        // Callers always get exactly min(k, n) results.
        let query = vec![1.0f32, 0.0, 0.0];
        let vectors = vec![
            vec![1.0, 0.0, 0.0], // valid
            vec![0.5, 0.5, 0.0], // valid
        ];
        Python::attach(|py| {
            let result = top_k_similar_f32(py, query, vectors, 5).unwrap();
            // Should always return all vectors (2), not fewer.
            assert_eq!(result.len(), 2);
        });
    }

    #[test]
    fn test_top_k_k_greater_than_n() {
        let query = vec![1.0f32];
        let vectors = vec![vec![3.0], vec![1.0]];
        let result = top_k_by_similarity(&query, &vectors, 100, dummy_sim);
        assert_eq!(result.len(), 2); // Returns all vectors, not 100
    }

    #[test]
    fn test_top_k_k_zero() {
        let query = vec![1.0f32];
        let vectors = vec![vec![1.0]];
        let result = top_k_by_similarity(&query, &vectors, 0, dummy_sim);
        assert!(result.is_empty());
    }

    #[test]
    fn test_top_k_empty_vectors() {
        let query = vec![1.0f32];
        let vectors: Vec<Vec<f32>> = vec![];
        let result = top_k_by_similarity(&query, &vectors, 10, dummy_sim);
        assert!(result.is_empty());
    }

    #[test]
    fn test_top_k_single_vector() {
        let query = vec![1.0f32, 2.0];
        let vectors = vec![vec![3.0, 4.0]];
        let result = top_k_by_similarity(&query, &vectors, 1, dummy_sim);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].0, 0);
        assert!((result[0].1 - 11.0).abs() < 1e-10); // 1*3 + 2*4 = 11
    }

    #[test]
    fn test_top_k_negative_scores() {
        let query = vec![1.0f32];
        let vectors = vec![
            vec![-5.0], // sim = -5.0
            vec![-1.0], // sim = -1.0
            vec![2.0],  // sim = 2.0
        ];
        let result = top_k_by_similarity(&query, &vectors, 2, dummy_sim);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].0, 2); // 2.0
        assert_eq!(result[1].0, 1); // -1.0
    }

    #[test]
    fn test_top_k_equal_scores() {
        let query = vec![1.0f32];
        let vectors = vec![vec![5.0], vec![5.0], vec![5.0]];
        let result = top_k_by_similarity(&query, &vectors, 2, dummy_sim);
        assert_eq!(result.len(), 2);
        // All scores are equal; any two of the three are valid results.
        assert!((result[0].1 - 5.0).abs() < 1e-10);
        assert!((result[1].1 - 5.0).abs() < 1e-10);
    }

    // -- Integration: top_k_similar_f32 uses the generic --------------------

    #[test]
    #[ignore = "requires Python interpreter (run via pytest or maturin develop)"]
    fn test_top_k_similar_f32_basic() {
        // Normalized vectors for meaningful cosine similarity.
        let query = vec![1.0f32, 0.0, 0.0];
        let vectors = vec![
            vec![1.0, 0.0, 0.0], // identical → similarity ≈ 1.0
            vec![0.0, 1.0, 0.0], // orthogonal → similarity ≈ 0.0
            vec![
                std::f32::consts::FRAC_1_SQRT_2,
                std::f32::consts::FRAC_1_SQRT_2,
                0.0,
            ], // 45 degrees
        ];
        Python::attach(|py| {
            let result = top_k_similar_f32(py, query, vectors, 2).unwrap();
            assert_eq!(result.len(), 2);
            // First result should be the identical vector.
            assert_eq!(result[0].0, 0);
            assert!(result[0].1 > 0.99);
        });
    }

    // -- Integration: top_k_similar_i8 uses the generic ---------------------

    #[test]
    #[ignore = "requires Python interpreter (run via pytest or maturin develop)"]
    fn test_top_k_similar_i8_basic() {
        let query = vec![100i8, 0, 0];
        let vectors = vec![
            vec![100, 0, 0], // identical
            vec![0, 100, 0], // orthogonal
            vec![70, 70, 0], // ~45 degrees
        ];
        Python::attach(|py| {
            let result = top_k_similar_i8(py, query, vectors, 2).unwrap();
            assert_eq!(result.len(), 2);
            assert_eq!(result[0].0, 0);
        });
    }
}

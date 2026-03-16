//! SIMD-accelerated vector similarity via SimSIMD.

use pyo3::prelude::*;
use rayon::prelude::*;
use simsimd::SpatialSimilarity;

// ---------------------------------------------------------------------------
// Generic helpers (pure Rust, no PyO3 dependency — testable directly)
// ---------------------------------------------------------------------------

const PARALLEL_THRESHOLD: usize = 100;

/// Validate that two vectors have the same length.
fn check_pair_len<T>(a: &[T], b: &[T]) -> PyResult<()> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }
    Ok(())
}

/// Validate that all vectors in a batch have the expected dimension.
fn check_batch_dims<T>(query_dim: usize, vectors: &[Vec<T>]) -> PyResult<()> {
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
    Ok(())
}

/// Compute batch cosine similarities (query vs all vectors).
fn batch_cosine<T>(query: &[T], vectors: &[Vec<T>]) -> Vec<f64>
where
    T: SpatialSimilarity + Sync,
{
    if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .map(|v| T::cos(query, v).map(|dist| 1.0 - dist).unwrap_or(0.0))
            .collect()
    } else {
        vectors
            .iter()
            .map(|v| T::cos(query, v).map(|dist| 1.0 - dist).unwrap_or(0.0))
            .collect()
    }
}

/// Compute top-K cosine similarity search.
fn top_k_cosine<T>(query: &[T], vectors: &[Vec<T>], k: usize) -> Vec<(usize, f64)>
where
    T: SpatialSimilarity + Sync,
{
    let mut scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = T::cos(query, v).map(|dist| 1.0 - dist).unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = T::cos(query, v).map(|dist| 1.0 - dist).unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    };

    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores.truncate(k);
    scores
}

// ---------------------------------------------------------------------------
// PyO3 wrappers (thin; delegate to generic helpers)
// ---------------------------------------------------------------------------

/// Compute cosine similarity between two f32 vectors using SIMD.
#[pyfunction]
pub fn cosine_similarity_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    check_pair_len(&a, &b)?;
    <f32 as SpatialSimilarity>::cos(&a, &b)
        .map(|dist| 1.0 - dist)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD cosine computation failed"))
}

/// Compute dot product between two f32 vectors using SIMD.
#[pyfunction]
pub fn dot_product_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    check_pair_len(&a, &b)?;
    <f32 as SpatialSimilarity>::dot(&a, &b).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("SIMD dot product computation failed")
    })
}

/// Compute squared Euclidean distance between two f32 vectors using SIMD.
#[pyfunction]
pub fn euclidean_sq_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    check_pair_len(&a, &b)?;
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
    check_batch_dims(query.len(), &vectors)?;
    Ok(py.detach(|| batch_cosine(&query, &vectors)))
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
    check_batch_dims(query.len(), &vectors)?;
    Ok(py.detach(|| top_k_cosine(&query, &vectors, k)))
}

/// Cosine similarity for int8 quantized vectors using SIMD.
#[pyfunction]
pub fn cosine_similarity_i8(a: Vec<i8>, b: Vec<i8>) -> PyResult<f64> {
    check_pair_len(&a, &b)?;
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
    check_batch_dims(query.len(), &vectors)?;
    Ok(py.detach(|| batch_cosine(&query, &vectors)))
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
    check_batch_dims(query.len(), &vectors)?;
    Ok(py.detach(|| top_k_cosine(&query, &vectors, k)))
}

// ---------------------------------------------------------------------------
// Tests (pure Rust — no PyO3 interpreter needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_batch_cosine_identical_vectors() {
        let query = vec![1.0f32, 0.0, 0.0];
        let vectors = vec![
            vec![1.0, 0.0, 0.0], // identical
            vec![0.0, 1.0, 0.0], // orthogonal
        ];
        let sims = batch_cosine(&query, &vectors);
        assert!(
            (sims[0] - 1.0).abs() < 1e-6,
            "identical vectors should have similarity ~1.0"
        );
        assert!(
            sims[1].abs() < 1e-6,
            "orthogonal vectors should have similarity ~0.0"
        );
    }

    #[test]
    fn test_batch_cosine_empty() {
        let query = vec![1.0f32, 0.0];
        let vectors: Vec<Vec<f32>> = vec![];
        let sims = batch_cosine(&query, &vectors);
        assert!(sims.is_empty());
    }

    #[test]
    fn test_top_k_cosine_ordering() {
        let query = vec![1.0f32, 0.0, 0.0];
        let vectors = vec![
            vec![0.0, 1.0, 0.0], // orthogonal
            vec![1.0, 0.0, 0.0], // identical
            vec![0.5, 0.5, 0.0], // partial match
        ];
        let top2 = top_k_cosine(&query, &vectors, 2);
        assert_eq!(top2.len(), 2);
        assert_eq!(top2[0].0, 1, "most similar should be index 1 (identical)");
        assert_eq!(
            top2[1].0, 2,
            "second most similar should be index 2 (partial)"
        );
    }

    #[test]
    fn test_top_k_cosine_k_larger_than_vectors() {
        let query = vec![1.0f32, 0.0];
        let vectors = vec![vec![1.0, 0.0]];
        let result = top_k_cosine(&query, &vectors, 10);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_batch_cosine_i8() {
        let query = vec![127i8, 0, 0];
        let vectors = vec![
            vec![127, 0, 0], // same direction
            vec![0, 127, 0], // orthogonal
        ];
        let sims = batch_cosine(&query, &vectors);
        assert!(
            sims[0] > 0.9,
            "same-direction i8 vectors should have high similarity"
        );
        assert!(
            sims[1].abs() < 0.1,
            "orthogonal i8 vectors should have low similarity"
        );
    }

    #[test]
    fn test_check_pair_len_mismatch() {
        let result = check_pair_len(&[1.0f32, 2.0], &[1.0f32]);
        assert!(result.is_err());
    }

    #[test]
    fn test_check_batch_dims_mismatch() {
        let vectors = vec![vec![1.0f32, 2.0], vec![1.0f32]];
        let result = check_batch_dims(2, &vectors);
        assert!(result.is_err());
    }
}

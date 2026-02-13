//! SIMD-accelerated vector similarity via SimSIMD.

use pyo3::prelude::*;
use rayon::prelude::*;
use simsimd::SpatialSimilarity;

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
pub fn batch_cosine_similarity_f32(query: Vec<f32>, vectors: Vec<Vec<f32>>) -> PyResult<Vec<f64>> {
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

    const PARALLEL_THRESHOLD: usize = 100;

    let similarities: Vec<f64> = if vectors.len() > PARALLEL_THRESHOLD {
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
    };

    Ok(similarities)
}

/// Top-K similarity search using SIMD (f32).
#[pyfunction]
pub fn top_k_similar_f32(
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

    const PARALLEL_THRESHOLD: usize = 100;

    let mut scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = <f32 as SpatialSimilarity>::cos(&query, v)
                    .map(|dist| 1.0 - dist)
                    .unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = <f32 as SpatialSimilarity>::cos(&query, v)
                    .map(|dist| 1.0 - dist)
                    .unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    };

    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores.truncate(k);

    Ok(scores)
}

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
pub fn batch_cosine_similarity_i8(query: Vec<i8>, vectors: Vec<Vec<i8>>) -> PyResult<Vec<f64>> {
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

    const PARALLEL_THRESHOLD: usize = 100;

    let similarities: Vec<f64> = if vectors.len() > PARALLEL_THRESHOLD {
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
    };

    Ok(similarities)
}

/// Top-K similarity search for int8 quantized vectors.
#[pyfunction]
pub fn top_k_similar_i8(
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

    const PARALLEL_THRESHOLD: usize = 100;

    let mut scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = <i8 as SpatialSimilarity>::cos(&query, v)
                    .map(|dist| 1.0 - dist)
                    .unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let sim = <i8 as SpatialSimilarity>::cos(&query, v)
                    .map(|dist| 1.0 - dist)
                    .unwrap_or(0.0);
                (i, sim)
            })
            .collect()
    };

    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores.truncate(k);

    Ok(scores)
}

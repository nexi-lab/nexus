#![allow(clippy::useless_conversion)]

use ahash::{AHashMap, AHashSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use serde::Deserialize;
use simsimd::SpatialSimilarity;
use std::collections::HashMap as StdHashMap;

/// Entity represents a subject or object in ReBAC
#[derive(Debug, Clone, Hash, Eq, PartialEq)]
struct Entity {
    entity_type: String,
    entity_id: String,
}

/// Tuple represents a relationship between entities
#[derive(Debug, Clone)]
struct ReBACTuple {
    subject_type: String,
    subject_id: String,
    /// When set, this is a userset-as-subject tuple:
    /// "members of subject_type:subject_id have this relation on the object"
    /// e.g., group:eng#member -> editor -> file:readme
    /// means "members of group:eng have editor on file:readme"
    subject_relation: Option<String>,
    relation: String,
    object_type: String,
    object_id: String,
}

/// Namespace configuration for permission expansion (uses std HashMap for serde)
#[derive(Debug, Clone, Deserialize)]
struct NamespaceConfig {
    relations: StdHashMap<String, RelationConfig>,
    permissions: StdHashMap<String, Vec<String>>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum RelationConfig {
    #[allow(dead_code)]
    Direct(String), // Matches "direct" string
    Union {
        union: Vec<String>,
    },
    TupleToUserset {
        #[serde(rename = "tupleToUserset")]
        tuple_to_userset: TupleToUsersetConfig,
    },
    #[allow(dead_code)]
    EmptyDict(serde_json::Map<String, serde_json::Value>), // Matches {} (empty dict means direct)
}

#[derive(Debug, Clone, Deserialize)]
struct TupleToUsersetConfig {
    tupleset: String,
    #[serde(rename = "computedUserset")]
    computed_userset: String,
}

/// Memoization cache for permission checks (using AHashMap for speed)
type MemoCache = AHashMap<(String, String, String, String, String), bool>;

/// Permission check request: (subject_type, subject_id, permission, object_type, object_id)
type CheckRequest = (String, String, String, String, String);

/// Main function: compute permissions in bulk using Rust
/// Note: tuple_version is accepted for API compatibility with nexus_fast,
/// but this simpler implementation doesn't implement graph caching.
#[pyfunction]
fn compute_permissions_bulk<'py>(
    py: Python<'py>,
    checks: &Bound<PyList>,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
    #[allow(unused_variables)]
    tuple_version: u64,
) -> PyResult<Bound<'py, PyDict>> {
    // Parse inputs from Python
    let check_requests: Vec<CheckRequest> = checks
        .iter()
        .map(|item| {
            let tuple: Bound<'_, PyTuple> = item.extract()?;
            let subject_item = tuple.get_item(0)?;
            let subject: Bound<'_, PyTuple> = subject_item.extract()?;
            let permission = tuple.get_item(1)?.extract::<String>()?;
            let object_item = tuple.get_item(2)?;
            let object: Bound<'_, PyTuple> = object_item.extract()?;

            Ok((
                subject.get_item(0)?.extract::<String>()?, // subject_type
                subject.get_item(1)?.extract::<String>()?, // subject_id
                permission,
                object.get_item(0)?.extract::<String>()?, // object_type
                object.get_item(1)?.extract::<String>()?, // object_id
            ))
        })
        .collect::<PyResult<Vec<_>>>()?;

    let rebac_tuples: Vec<ReBACTuple> = tuples
        .iter()
        .map(|item| {
            let dict: Bound<'_, PyDict> = item.extract()?;
            Ok(ReBACTuple {
                subject_type: dict.get_item("subject_type")?.unwrap().extract()?,
                subject_id: dict.get_item("subject_id")?.unwrap().extract()?,
                subject_relation: dict
                    .get_item("subject_relation")?
                    .and_then(|v| v.extract().ok()),
                relation: dict.get_item("relation")?.unwrap().extract()?,
                object_type: dict.get_item("object_type")?.unwrap().extract()?,
                object_id: dict.get_item("object_id")?.unwrap().extract()?,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;

    // Parse namespace configs
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        // Convert Python dict to JSON via Python's json module
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;
        let config: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
        })?;
        namespaces.insert(obj_type, config);
    }

    // Release GIL for computation
    let results = py.detach(|| {
        let mut results = AHashMap::new();
        let mut memo_cache: MemoCache = AHashMap::new();

        for check in check_requests {
            let (subject_type, subject_id, permission, object_type, object_id) = &check;

            let subject = Entity {
                entity_type: subject_type.clone(),
                entity_id: subject_id.clone(),
            };

            let object = Entity {
                entity_type: object_type.clone(),
                entity_id: object_id.clone(),
            };

            let allowed = compute_permission(
                &subject,
                permission,
                &object,
                &rebac_tuples,
                &namespaces,
                &mut memo_cache,
                &mut AHashSet::new(),
                0,
            );

            results.insert(check.clone(), allowed);
        }

        results
    });

    // Convert AHashMap to PyDict
    let py_dict = PyDict::new(py);
    for (key, value) in results {
        py_dict.set_item(key, value)?;
    }

    Ok(py_dict)
}

/// Compute a single permission check with memoization
#[allow(clippy::too_many_arguments)]
fn compute_permission(
    subject: &Entity,
    permission: &str,
    object: &Entity,
    tuples: &[ReBACTuple],
    namespaces: &AHashMap<String, NamespaceConfig>,
    memo_cache: &mut MemoCache,
    visited: &mut AHashSet<(String, String, String, String, String)>,
    depth: u32,
) -> bool {
    const MAX_DEPTH: u32 = 50;

    if depth > MAX_DEPTH {
        return false;
    }

    // Check memo cache
    let memo_key = (
        subject.entity_type.clone(),
        subject.entity_id.clone(),
        permission.to_string(),
        object.entity_type.clone(),
        object.entity_id.clone(),
    );

    if let Some(&result) = memo_cache.get(&memo_key) {
        return result;
    }

    // Cycle detection
    if visited.contains(&memo_key) {
        return false;
    }
    visited.insert(memo_key.clone());

    // Get namespace config
    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            // No namespace, check direct relation AND userset membership
            let result = check_relation_with_usersets(
                subject, permission, object, tuples, namespaces, memo_cache, visited, depth,
            );
            memo_cache.insert(memo_key, result);
            return result;
        }
    };

    // Check if permission is defined
    let result = if let Some(usersets) = namespace.permissions.get(permission) {
        // Permission -> usersets (OR semantics)
        let mut allowed = false;
        for userset in usersets {
            if compute_permission(
                subject,
                userset,
                object,
                tuples,
                namespaces,
                memo_cache,
                &mut visited.clone(),
                depth + 1,
            ) {
                allowed = true;
                break;
            }
        }
        allowed
    } else if let Some(relation_config) = namespace.relations.get(permission) {
        // Relation expansion
        match relation_config {
            RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                // Both "direct" string and {} empty dict mean direct relation
                // Check direct AND userset-based permissions
                check_relation_with_usersets(
                    subject, permission, object, tuples, namespaces, memo_cache, visited, depth,
                )
            }
            RelationConfig::Union { union } => {
                // Union (OR semantics)
                let mut allowed = false;
                for rel in union {
                    if compute_permission(
                        subject,
                        rel,
                        object,
                        tuples,
                        namespaces,
                        memo_cache,
                        &mut visited.clone(),
                        depth + 1,
                    ) {
                        allowed = true;
                        break;
                    }
                }
                allowed
            }
            RelationConfig::TupleToUserset { tuple_to_userset } => {
                // TupleToUserset: find related objects, check permission on them
                let related_objects =
                    find_related_objects(object, &tuple_to_userset.tupleset, tuples);

                let mut allowed = false;
                for related_obj in related_objects {
                    if compute_permission(
                        subject,
                        &tuple_to_userset.computed_userset,
                        &related_obj,
                        tuples,
                        namespaces,
                        memo_cache,
                        &mut visited.clone(),
                        depth + 1,
                    ) {
                        allowed = true;
                        break;
                    }
                }
                allowed
            }
        }
    } else {
        // Permission not in namespace config, check direct relation AND userset membership
        check_relation_with_usersets(
            subject, permission, object, tuples, namespaces, memo_cache, visited, depth,
        )
    };

    memo_cache.insert(memo_key, result);
    result
}

/// Check for direct relation in tuple graph
fn check_direct_relation(
    subject: &Entity,
    relation: &str,
    object: &Entity,
    tuples: &[ReBACTuple],
) -> bool {
    for tuple in tuples {
        // Only check tuples WITHOUT subject_relation (direct tuples)
        if tuple.subject_relation.is_none()
            && tuple.object_type == object.entity_type
            && tuple.object_id == object.entity_id
            && tuple.relation == relation
            && tuple.subject_type == subject.entity_type
            && tuple.subject_id == subject.entity_id
        {
            return true;
        }
    }
    false
}

/// Check if subject has a relation on object via direct tuple OR userset membership
/// This handles the userset-as-subject pattern: group:eng#member -> editor -> file:readme
#[allow(clippy::too_many_arguments)]
fn check_relation_with_usersets(
    subject: &Entity,
    relation: &str,
    object: &Entity,
    tuples: &[ReBACTuple],
    namespaces: &AHashMap<String, NamespaceConfig>,
    memo_cache: &mut MemoCache,
    visited: &mut AHashSet<(String, String, String, String, String)>,
    depth: u32,
) -> bool {
    // First check direct relation
    if check_direct_relation(subject, relation, object, tuples) {
        return true;
    }

    // Then check userset-based permissions
    // e.g., if group:eng#member -> editor -> file:readme exists,
    // check if subject has "member" relation on group:eng
    for tuple in tuples {
        // Find userset tuples matching the object and relation
        if let Some(ref subject_relation) = tuple.subject_relation {
            if tuple.object_type == object.entity_type
                && tuple.object_id == object.entity_id
                && tuple.relation == relation
            {
                // Check if subject is a member of this userset
                // e.g., does user:alice have "member" on group:eng?
                let userset_entity = Entity {
                    entity_type: tuple.subject_type.clone(),
                    entity_id: tuple.subject_id.clone(),
                };

                if compute_permission(
                    subject,
                    subject_relation,
                    &userset_entity,
                    tuples,
                    namespaces,
                    memo_cache,
                    &mut visited.clone(),
                    depth + 1,
                ) {
                    return true;
                }
            }
        }
    }

    false
}

/// Find related objects via a relation
fn find_related_objects(object: &Entity, relation: &str, tuples: &[ReBACTuple]) -> Vec<Entity> {
    let mut related = Vec::new();

    for tuple in tuples {
        if tuple.subject_type == object.entity_type
            && tuple.subject_id == object.entity_id
            && tuple.relation == relation
        {
            related.push(Entity {
                entity_type: tuple.object_type.clone(),
                entity_id: tuple.object_id.clone(),
            });
        }
    }

    related
}

/// Grep search result
#[derive(Debug)]
struct GrepMatch {
    file: String,
    line: usize,
    content: String,
    match_text: String,
}

/// Fast content search using Rust regex
///
/// Optimized approach: search whole content first, then extract line info only for matches.
/// This avoids iterating every line when matches are sparse.
#[pyfunction]
#[pyo3(signature = (pattern, file_contents, ignore_case=false, max_results=1000))]
fn grep_bulk<'py>(
    py: Python<'py>,
    pattern: &str,
    file_contents: &Bound<'py, PyDict>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    use pyo3::types::PyBytes;

    // Compile regex pattern once
    let regex = RegexBuilder::new(pattern)
        .case_insensitive(ignore_case)
        .build()
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
        })?;

    let mut results: Vec<GrepMatch> = Vec::new();

    for (file_path_py, content_py) in file_contents.iter() {
        if results.len() >= max_results {
            break;
        }

        let file_path: String = match file_path_py.extract() {
            Ok(p) => p,
            Err(_) => continue,
        };

        // Try to get bytes with zero-copy from PyBytes
        if let Ok(py_bytes) = content_py.extract::<Bound<'_, PyBytes>>() {
            let content_bytes = py_bytes.as_bytes();
            let file_results = search_content_optimized(
                &file_path,
                content_bytes,
                &regex,
                max_results - results.len(),
            );
            results.extend(file_results);
        }
        // Fallback: extract as Vec<u8> (copies data)
        else if let Ok(bytes_vec) = content_py.extract::<Vec<u8>>() {
            let file_results = search_content_optimized(
                &file_path,
                &bytes_vec,
                &regex,
                max_results - results.len(),
            );
            results.extend(file_results);
        }
    }

    // Convert results to Python list of dicts
    let py_list = PyList::empty(py);
    for m in results {
        let dict = PyDict::new(py);
        dict.set_item("file", m.file)?;
        dict.set_item("line", m.line)?;
        dict.set_item("content", m.content)?;
        dict.set_item("match", m.match_text)?;
        py_list.append(dict)?;
    }

    Ok(py_list)
}

/// Optimized search: find matches first, then compute line numbers only for matches
fn search_content_optimized(
    file_path: &str,
    content_bytes: &[u8],
    regex: &regex::bytes::Regex,
    max_results: usize,
) -> Vec<GrepMatch> {
    use memchr::memchr_iter;

    let mut results = Vec::new();

    // Quick check: any matches at all?
    if !regex.is_match(content_bytes) {
        return results;
    }

    // Build line index using memchr (SIMD-accelerated newline search)
    let mut line_starts: Vec<usize> = Vec::with_capacity(content_bytes.len() / 40); // estimate
    line_starts.push(0);
    for pos in memchr_iter(b'\n', content_bytes) {
        line_starts.push(pos + 1);
    }

    // Find all matches and map to lines
    for mat in regex.find_iter(content_bytes) {
        if results.len() >= max_results {
            break;
        }

        let match_start = mat.start();

        // Binary search for line number
        let line_idx = match line_starts.binary_search(&match_start) {
            Ok(i) => i,
            Err(i) => i.saturating_sub(1),
        };

        let line_start = line_starts[line_idx];
        let line_end = line_starts
            .get(line_idx + 1)
            .map(|&e| e.saturating_sub(1)) // exclude newline
            .unwrap_or(content_bytes.len());

        // Extract line content
        let line_bytes = &content_bytes[line_start..line_end];
        let line_content = std::str::from_utf8(line_bytes).unwrap_or("").to_string();
        let match_text = std::str::from_utf8(mat.as_bytes()).unwrap_or("").to_string();

        results.push(GrepMatch {
            file: file_path.to_string(),
            line: line_idx + 1,
            content: line_content,
            match_text,
        });
    }

    results
}

/// Simple test: just regex is_match on bytes (for benchmarking)
#[pyfunction]
fn test_regex_match(pattern: &str, content: &[u8]) -> PyResult<bool> {
    let regex = RegexBuilder::new(pattern)
        .build()
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid regex: {}", e))
        })?;
    Ok(regex.is_match(content))
}

// === BLAKE3 Hashing for Content-Addressable Storage ===

/// Compute BLAKE3 hash of content (full hash)
///
/// BLAKE3 is ~3x faster than SHA-256 and uses SIMD acceleration.
/// Returns 64-character hex string (256-bit hash).
#[pyfunction]
fn hash_content(content: &[u8]) -> String {
    blake3::hash(content).to_hex().to_string()
}

/// Compute BLAKE3 hash with strategic sampling for large files
///
/// For files < 256KB: full hash (same as hash_content)
/// For files >= 256KB: samples first 64KB + middle 64KB + last 64KB
///
/// This provides ~10x speedup for large files while maintaining
/// good collision resistance for deduplication purposes.
///
/// NOTE: This is NOT suitable for cryptographic integrity verification,
/// only for content-addressable storage fingerprinting.
#[pyfunction]
fn hash_content_smart(content: &[u8]) -> String {
    const THRESHOLD: usize = 256 * 1024; // 256KB
    const SAMPLE_SIZE: usize = 64 * 1024; // 64KB per sample

    if content.len() < THRESHOLD {
        // Small file: full hash
        blake3::hash(content).to_hex().to_string()
    } else {
        // Large file: strategic sampling
        let mut hasher = blake3::Hasher::new();

        // First 64KB
        hasher.update(&content[..SAMPLE_SIZE]);

        // Middle 64KB
        let mid_start = content.len() / 2 - SAMPLE_SIZE / 2;
        hasher.update(&content[mid_start..mid_start + SAMPLE_SIZE]);

        // Last 64KB
        hasher.update(&content[content.len() - SAMPLE_SIZE..]);

        // Also include file size to differentiate files with same samples
        hasher.update(&content.len().to_le_bytes());

        hasher.finalize().to_hex().to_string()
    }
}

// =============================================================================
// SIMD-Accelerated Vector Similarity (Issue #952)
// =============================================================================

/// Compute cosine similarity between two f32 vectors using SIMD.
/// Returns similarity (1.0 = identical, 0.0 = orthogonal, -1.0 = opposite)
#[pyfunction]
fn cosine_similarity_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
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
fn dot_product_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }
    <f32 as SpatialSimilarity>::dot(&a, &b)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD dot product computation failed"))
}

/// Compute squared Euclidean distance between two f32 vectors using SIMD.
#[pyfunction]
fn euclidean_sq_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
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
/// Uses parallel processing for large batches (>100 vectors).
#[pyfunction]
fn batch_cosine_similarity_f32(query: Vec<f32>, vectors: Vec<Vec<f32>>) -> PyResult<Vec<f64>> {
    if vectors.is_empty() {
        return Ok(vec![]);
    }
    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i, query_dim, v.len()
            )));
        }
    }
    const PARALLEL_THRESHOLD: usize = 100;
    let similarities: Vec<f64> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .map(|v| <f32 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0))
            .collect()
    } else {
        vectors
            .iter()
            .map(|v| <f32 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0))
            .collect()
    };
    Ok(similarities)
}

/// Top-K similarity search using SIMD. Returns (index, similarity) pairs sorted descending.
#[pyfunction]
fn top_k_similar_f32(query: Vec<f32>, vectors: Vec<Vec<f32>>, k: usize) -> PyResult<Vec<(usize, f64)>> {
    if vectors.is_empty() || k == 0 {
        return Ok(vec![]);
    }
    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i, query_dim, v.len()
            )));
        }
    }
    const PARALLEL_THRESHOLD: usize = 100;
    let mut scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| (i, <f32 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0)))
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| (i, <f32 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0)))
            .collect()
    };
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores.truncate(k);
    Ok(scores)
}

/// Cosine similarity for int8 quantized vectors (4x smaller memory).
#[pyfunction]
fn cosine_similarity_i8(a: Vec<i8>, b: Vec<i8>) -> PyResult<f64> {
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
fn batch_cosine_similarity_i8(query: Vec<i8>, vectors: Vec<Vec<i8>>) -> PyResult<Vec<f64>> {
    if vectors.is_empty() {
        return Ok(vec![]);
    }
    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i, query_dim, v.len()
            )));
        }
    }
    const PARALLEL_THRESHOLD: usize = 100;
    let similarities: Vec<f64> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .map(|v| <i8 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0))
            .collect()
    } else {
        vectors
            .iter()
            .map(|v| <i8 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0))
            .collect()
    };
    Ok(similarities)
}

/// Top-K similarity search for int8 quantized vectors.
#[pyfunction]
fn top_k_similar_i8(query: Vec<i8>, vectors: Vec<Vec<i8>>, k: usize) -> PyResult<Vec<(usize, f64)>> {
    if vectors.is_empty() || k == 0 {
        return Ok(vec![]);
    }
    let query_dim = query.len();
    for (i, v) in vectors.iter().enumerate() {
        if v.len() != query_dim {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "Vector {} dimension mismatch: expected {}, got {}",
                i, query_dim, v.len()
            )));
        }
    }
    const PARALLEL_THRESHOLD: usize = 100;
    let mut scores: Vec<(usize, f64)> = if vectors.len() > PARALLEL_THRESHOLD {
        vectors
            .par_iter()
            .enumerate()
            .map(|(i, v)| (i, <i8 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0)))
            .collect()
    } else {
        vectors
            .iter()
            .enumerate()
            .map(|(i, v)| (i, <i8 as SpatialSimilarity>::cos(&query, v).map(|d| 1.0 - d).unwrap_or(0.0)))
            .collect()
    };
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scores.truncate(k);
    Ok(scores)
}

/// Python module definition
#[pymodule]
fn _nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(test_regex_match, m)?)?;
    m.add_function(wrap_pyfunction!(hash_content, m)?)?;
    m.add_function(wrap_pyfunction!(hash_content_smart, m)?)?;
    // SIMD-accelerated vector similarity (Issue #952)
    m.add_function(wrap_pyfunction!(cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(dot_product_f32, m)?)?;
    m.add_function(wrap_pyfunction!(euclidean_sq_f32, m)?)?;
    m.add_function(wrap_pyfunction!(batch_cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(top_k_similar_f32, m)?)?;
    m.add_function(wrap_pyfunction!(cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(batch_cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(top_k_similar_i8, m)?)?;
    Ok(())
}

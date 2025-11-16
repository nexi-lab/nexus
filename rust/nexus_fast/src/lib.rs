#![allow(clippy::useless_conversion)]

use ahash::{AHashMap, AHashSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use regex::bytes::RegexBuilder;
use serde::Deserialize;
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
    #[allow(dead_code)]
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

/// Key for tuple index: (object_type, object_id, relation, subject_type, subject_id)
type TupleKey = (String, String, String, String, String);

/// Key for adjacency list: (subject_type, subject_id, relation)
type AdjacencyKey = (String, String, String);

/// Graph indexing structure for fast lookups
#[derive(Debug, Clone)]
struct ReBACGraph {
    /// Hash index for direct tuple lookups: O(1) instead of O(n)
    /// Key: (object_type, object_id, relation, subject_type, subject_id)
    tuple_index: AHashMap<TupleKey, bool>,

    /// Adjacency list for finding related objects: O(1) instead of O(n)
    /// Key: (subject_type, subject_id, relation)
    /// Value: List of objects related via that relation
    adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>>,
}

impl ReBACGraph {
    /// Build graph indexes from tuples for fast lookups
    fn from_tuples(tuples: &[ReBACTuple]) -> Self {
        let mut tuple_index = AHashMap::new();
        let mut adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();

        for tuple in tuples {
            // Build tuple index for direct relation checks
            let tuple_key = (
                tuple.object_type.clone(),
                tuple.object_id.clone(),
                tuple.relation.clone(),
                tuple.subject_type.clone(),
                tuple.subject_id.clone(),
            );
            tuple_index.insert(tuple_key, true);

            // Build adjacency list for finding related objects
            // This is used for tupleToUserset traversal
            let adj_key = (
                tuple.subject_type.clone(),
                tuple.subject_id.clone(),
                tuple.relation.clone(),
            );
            adjacency_list.entry(adj_key).or_default().push(Entity {
                entity_type: tuple.object_type.clone(),
                entity_id: tuple.object_id.clone(),
            });
        }

        ReBACGraph {
            tuple_index,
            adjacency_list,
        }
    }

    /// Check for direct relation in O(1) time using hash index
    fn check_direct_relation(&self, subject: &Entity, relation: &str, object: &Entity) -> bool {
        let tuple_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
            subject.entity_type.clone(),
            subject.entity_id.clone(),
        );
        self.tuple_index.contains_key(&tuple_key)
    }

    /// Find related objects in O(1) time using adjacency list
    fn find_related_objects(&self, object: &Entity, relation: &str) -> Vec<Entity> {
        let adj_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
        );
        self.adjacency_list
            .get(&adj_key)
            .cloned()
            .unwrap_or_default()
    }
}

/// Main function: compute permissions in bulk using Rust
#[pyfunction]
fn compute_permissions_bulk<'py>(
    py: Python<'py>,
    checks: &Bound<PyList>,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    // Parse inputs from Python
    let check_requests: Vec<CheckRequest> = checks
        .iter()
        .map(|item| {
            let tuple = item.downcast::<PyTuple>()?;
            let subject_item = tuple.get_item(0)?;
            let subject = subject_item.downcast::<PyTuple>()?;
            let permission = tuple.get_item(1)?.extract::<String>()?;
            let object_item = tuple.get_item(2)?;
            let object = object_item.downcast::<PyTuple>()?;

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
            let dict = item.downcast::<PyDict>()?;
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
        let config_dict = value.downcast::<PyDict>()?;
        // Convert Python dict to JSON via Python's json module
        let json_module = py.import_bound("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;
        let config: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
        })?;
        namespaces.insert(obj_type, config);
    }

    // Release GIL for computation
    let results = py.allow_threads(|| {
        let mut results = AHashMap::new();
        let mut memo_cache: MemoCache = AHashMap::new();

        // Build graph indexes once for all checks - massive speedup!
        let graph = ReBACGraph::from_tuples(&rebac_tuples);

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
                &graph,
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
    let py_dict = PyDict::new_bound(py);
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
    graph: &ReBACGraph,
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
            // No namespace, check direct relation using O(1) graph index
            let result = graph.check_direct_relation(subject, permission, object);
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
                graph,
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
                // Use O(1) graph index instead of O(n) scan
                graph.check_direct_relation(subject, permission, object)
            }
            RelationConfig::Union { union } => {
                // Union (OR semantics)
                let mut allowed = false;
                for rel in union {
                    if compute_permission(
                        subject,
                        rel,
                        object,
                        graph,
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
                // TupleToUserset: find related objects using O(1) adjacency list
                let related_objects =
                    graph.find_related_objects(object, &tuple_to_userset.tupleset);

                let mut allowed = false;
                for related_obj in related_objects {
                    if compute_permission(
                        subject,
                        &tuple_to_userset.computed_userset,
                        &related_obj,
                        graph,
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
        false
    };

    memo_cache.insert(memo_key, result);
    result
}

/// Python function: compute a single permission check
/// This is for interactive/single-check use cases (faster than Python, slower than bulk)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn compute_permission_single(
    py: Python<'_>,
    subject_type: String,
    subject_id: String,
    permission: String,
    object_type: String,
    object_id: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<bool> {
    // Parse tuples from Python
    let rebac_tuples: Vec<ReBACTuple> = tuples
        .iter()
        .map(|item| {
            let dict = item.downcast::<PyDict>()?;
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
        let config_dict = value.downcast::<PyDict>()?;
        let json_module = py.import_bound("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;
        let config: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
        })?;
        namespaces.insert(obj_type, config);
    }

    // Release GIL for computation
    let result = py.allow_threads(|| {
        let subject = Entity {
            entity_type: subject_type,
            entity_id: subject_id,
        };
        let object = Entity {
            entity_type: object_type,
            entity_id: object_id,
        };

        // Build graph indexes for fast lookups
        let graph = ReBACGraph::from_tuples(&rebac_tuples);
        let mut memo_cache: MemoCache = AHashMap::new();

        compute_permission(
            &subject,
            &permission,
            &object,
            &graph,
            &namespaces,
            &mut memo_cache,
            &mut AHashSet::new(),
            0,
        )
    });

    Ok(result)
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
#[pyfunction]
#[pyo3(signature = (pattern, file_contents, ignore_case=false, max_results=1000))]
fn grep_bulk<'py>(
    py: Python<'py>,
    pattern: &str,
    file_contents: &Bound<PyDict>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    // Compile regex pattern
    let regex = RegexBuilder::new(pattern)
        .case_insensitive(ignore_case)
        .build()
        .map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
        })?;

    // Extract all file contents from Python objects first
    let mut files_data: Vec<(String, Vec<u8>)> = Vec::new();
    for (file_path_py, content_py) in file_contents.iter() {
        let file_path = match file_path_py.extract::<String>() {
            Ok(p) => p,
            Err(_) => continue,
        };

        let content_bytes = match content_py.extract::<Vec<u8>>() {
            Ok(b) => b,
            Err(_) => continue,
        };

        files_data.push((file_path, content_bytes));
    }

    // Release GIL for computation
    let matches = py.allow_threads(|| {
        let mut results = Vec::new();

        // Iterate over extracted file contents
        for (file_path, content_bytes) in files_data {
            if results.len() >= max_results {
                break;
            }

            // Try to decode as UTF-8 (skip binary files)
            let content_str = match std::str::from_utf8(&content_bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };

            // Search line by line
            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();
                if let Some(mat) = regex.find(line_bytes) {
                    let match_text = std::str::from_utf8(&line_bytes[mat.start()..mat.end()])
                        .unwrap_or("")
                        .to_string();

                    results.push(GrepMatch {
                        file: file_path.clone(),
                        line: line_num + 1, // 1-indexed
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }

        results
    });

    // Convert results to Python list of dicts
    let py_list = PyList::empty_bound(py);
    for m in matches {
        let dict = PyDict::new_bound(py);
        dict.set_item("file", m.file)?;
        dict.set_item("line", m.line)?;
        dict.set_item("content", m.content)?;
        dict.set_item("match", m.match_text)?;
        py_list.append(dict)?;
    }

    Ok(py_list)
}

/// Fast glob pattern matching using Rust globset
#[pyfunction]
#[pyo3(signature = (patterns, paths))]
fn glob_match_bulk(
    py: Python<'_>,
    patterns: Vec<String>,
    paths: Vec<String>,
) -> PyResult<Bound<'_, PyList>> {
    use globset::{Glob, GlobSetBuilder};

    // Build glob set from patterns
    let globset = py.allow_threads(|| {
        let mut builder = GlobSetBuilder::new();
        for pattern in &patterns {
            match Glob::new(pattern) {
                Ok(glob) => {
                    builder.add(glob);
                }
                Err(e) => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "Invalid glob pattern '{}': {}",
                        pattern, e
                    )));
                }
            }
        }
        builder.build().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to build globset: {}", e))
        })
    })?;

    // Match paths against the glob set
    let matches = py.allow_threads(|| {
        let mut results = Vec::new();
        for path in paths {
            if globset.is_match(&path) {
                results.push(path);
            }
        }
        results
    });

    // Convert results to Python list
    let py_list = PyList::empty_bound(py);
    for path in matches {
        py_list.append(path)?;
    }

    Ok(py_list)
}

/// Fast path filtering using Rust glob patterns
#[pyfunction]
fn filter_paths(
    py: Python<'_>,
    paths: Vec<String>,
    exclude_patterns: Vec<String>,
) -> PyResult<Vec<String>> {
    use globset::{Glob, GlobSetBuilder};

    // Build glob set from exclude patterns
    let globset = py.allow_threads(|| {
        let mut builder = GlobSetBuilder::new();
        for pattern in &exclude_patterns {
            match Glob::new(pattern) {
                Ok(glob) => {
                    builder.add(glob);
                }
                Err(e) => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "Invalid glob pattern '{}': {}",
                        pattern, e
                    )));
                }
            }
        }
        builder.build().map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to build globset: {}", e))
        })
    })?;

    // Filter paths against exclude patterns
    let filtered = py.allow_threads(|| {
        let mut results = Vec::new();
        for path in paths {
            // Extract filename from path
            let filename = if let Some(pos) = path.rfind('/') {
                &path[pos + 1..]
            } else {
                &path
            };

            // Check if filename matches any exclude pattern
            if !globset.is_match(filename) {
                results.push(path);
            }
        }
        results
    });

    Ok(filtered)
}

/// Python module definition
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(filter_paths, m)?)?;
    Ok(())
}

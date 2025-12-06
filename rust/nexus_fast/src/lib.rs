#![allow(clippy::useless_conversion)]

use ahash::{AHashMap, AHashSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use serde::Deserialize;
use std::collections::HashMap as StdHashMap;

/// Threshold for parallelization: only use rayon for lists larger than this
const GLOB_PARALLEL_THRESHOLD: usize = 500;
const PERMISSION_PARALLEL_THRESHOLD: usize = 50;

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

/// Key for tuple index: (object_type, object_id, relation, subject_type, subject_id)
type TupleKey = (String, String, String, String, String);

/// Key for adjacency list: (subject_type, subject_id, relation)
type AdjacencyKey = (String, String, String);

/// Key for userset index: (object_type, object_id, relation)
type UsersetKey = (String, String, String);

/// Userset entry: when subject_relation is set, the permission is granted through group membership
/// e.g., (group, eng, member) means "members of group:eng"
#[derive(Debug, Clone)]
struct UsersetEntry {
    subject_type: String,
    subject_id: String,
    subject_relation: String,
}

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

    /// Userset index for group-based permissions: O(1) lookup
    /// Key: (object_type, object_id, relation)
    /// Value: List of usersets that grant this permission (e.g., group:eng#member)
    userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>>,
}

impl ReBACGraph {
    /// Build graph indexes from tuples for fast lookups
    fn from_tuples(tuples: &[ReBACTuple]) -> Self {
        let mut tuple_index = AHashMap::new();
        let mut adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();
        let mut userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>> = AHashMap::new();

        for tuple in tuples {
            // Check if this is a userset-as-subject tuple (has subject_relation)
            if let Some(ref subject_relation) = tuple.subject_relation {
                // This is a userset tuple: group:eng#member -> editor -> file:readme
                // Index by (object_type, object_id, relation) for fast lookup
                let userset_key = (
                    tuple.object_type.clone(),
                    tuple.object_id.clone(),
                    tuple.relation.clone(),
                );
                userset_index
                    .entry(userset_key)
                    .or_default()
                    .push(UsersetEntry {
                        subject_type: tuple.subject_type.clone(),
                        subject_id: tuple.subject_id.clone(),
                        subject_relation: subject_relation.clone(),
                    });
            } else {
                // Direct tuple: user:alice -> editor -> file:readme
                // Build tuple index for direct relation checks
                let tuple_key = (
                    tuple.object_type.clone(),
                    tuple.object_id.clone(),
                    tuple.relation.clone(),
                    tuple.subject_type.clone(),
                    tuple.subject_id.clone(),
                );
                tuple_index.insert(tuple_key, true);
            }

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
            userset_index,
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

    /// Get usersets that grant a relation on an object in O(1) time
    /// Returns list of (subject_type, subject_id, subject_relation) tuples
    /// e.g., for file:readme#editor, might return [(group, eng, member)]
    /// meaning "members of group:eng have editor on file:readme"
    fn get_usersets(&self, object: &Entity, relation: &str) -> &[UsersetEntry] {
        let userset_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
        );
        self.userset_index
            .get(&userset_key)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
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
    // Use parallel iteration for large check lists, sequential for small lists
    let results = py.detach(|| {
        // Build graph indexes once for all checks - massive speedup!
        let graph = ReBACGraph::from_tuples(&rebac_tuples);

        if check_requests.len() < PERMISSION_PARALLEL_THRESHOLD {
            // Sequential for small batches (shared memoization cache)
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
                    &graph,
                    &namespaces,
                    &mut memo_cache,
                    &mut AHashSet::new(),
                    0,
                );

                results.insert(check.clone(), allowed);
            }

            results
        } else {
            // Parallel for large batches (per-thread memoization caches)
            // Collect into Vec first, then convert to AHashMap
            let results_vec: Vec<_> = check_requests
                .into_par_iter()
                .map(|check| {
                    let (subject_type, subject_id, permission, object_type, object_id) = &check;

                    let subject = Entity {
                        entity_type: subject_type.clone(),
                        entity_id: subject_id.clone(),
                    };

                    let object = Entity {
                        entity_type: object_type.clone(),
                        entity_id: object_id.clone(),
                    };

                    // Each thread gets its own memo cache
                    let mut local_memo_cache: MemoCache = AHashMap::new();

                    let allowed = compute_permission(
                        &subject,
                        permission,
                        &object,
                        &graph,
                        &namespaces,
                        &mut local_memo_cache,
                        &mut AHashSet::new(),
                        0,
                    );

                    (check, allowed)
                })
                .collect();

            // Convert Vec to AHashMap
            results_vec.into_iter().collect()
        }
    });

    // Convert AHashMap to PyDict
    let py_dict = PyDict::new(py);
    for (key, value) in results {
        py_dict.set_item(key, value)?;
    }

    Ok(py_dict)
}

/// Check if subject has a relation on object via direct tuple OR userset membership
/// This handles the userset-as-subject pattern: group:eng#member -> editor -> file:readme
#[allow(clippy::too_many_arguments)]
fn check_relation_with_usersets(
    subject: &Entity,
    relation: &str,
    object: &Entity,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    memo_cache: &mut MemoCache,
    visited: &mut AHashSet<(String, String, String, String, String)>,
    depth: u32,
) -> bool {
    // First check direct relation: user:alice -> editor -> file:readme
    if graph.check_direct_relation(subject, relation, object) {
        return true;
    }

    // Then check userset-based permissions
    // e.g., if group:eng#member -> editor -> file:readme exists,
    // check if subject has "member" relation on group:eng
    for userset in graph.get_usersets(object, relation) {
        let userset_entity = Entity {
            entity_type: userset.subject_type.clone(),
            entity_id: userset.subject_id.clone(),
        };

        // Check if subject is a member of this userset
        // e.g., does user:alice have "member" on group:eng?
        if compute_permission(
            subject,
            &userset.subject_relation,
            &userset_entity,
            graph,
            namespaces,
            memo_cache,
            &mut visited.clone(),
            depth + 1,
        ) {
            return true;
        }
    }

    false
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
            // No namespace, check direct relation AND userset membership
            let result = check_relation_with_usersets(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
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
                // Check direct AND userset-based permissions
                check_relation_with_usersets(
                    subject, permission, object, graph, namespaces, memo_cache, visited, depth,
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
        // Permission not in namespace config, check direct relation AND userset membership
        check_relation_with_usersets(
            subject, permission, object, graph, namespaces, memo_cache, visited, depth,
        )
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
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;
        let config: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
        })?;
        namespaces.insert(obj_type, config);
    }

    // Release GIL for computation
    let result = py.detach(|| {
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
    let matches = py.detach(|| {
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
    let py_list = PyList::empty(py);
    for m in matches {
        let dict = PyDict::new(py);
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
    let globset = py.detach(|| {
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
    // Use parallel iteration for large lists, sequential for small lists
    let matches: Vec<String> = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
            // Sequential for small lists (avoid rayon overhead)
            paths
                .into_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        } else {
            // Parallel for large lists
            paths
                .into_par_iter()
                .filter(|path| globset.is_match(path))
                .collect()
        }
    });

    // Convert results to Python list
    let py_list = PyList::empty(py);
    for path in matches {
        py_list.append(path)?;
    }

    Ok(py_list)
}

/// Fast path filtering using Rust glob patterns
/// Uses rayon parallelization for large path lists (>500 paths)
#[pyfunction]
fn filter_paths(
    py: Python<'_>,
    paths: Vec<String>,
    exclude_patterns: Vec<String>,
) -> PyResult<Vec<String>> {
    use globset::{Glob, GlobSetBuilder};

    // Build glob set from exclude patterns
    let globset = py.detach(|| {
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
    // Use parallel iteration for large lists, sequential for small lists
    let filtered = py.detach(|| {
        if paths.len() < GLOB_PARALLEL_THRESHOLD {
            // Sequential for small lists
            paths
                .into_iter()
                .filter(|path| {
                    let filename = if let Some(pos) = path.rfind('/') {
                        &path[pos + 1..]
                    } else {
                        path.as_str()
                    };
                    !globset.is_match(filename)
                })
                .collect()
        } else {
            // Parallel for large lists
            paths
                .into_par_iter()
                .filter(|path| {
                    let filename = if let Some(pos) = path.rfind('/') {
                        &path[pos + 1..]
                    } else {
                        path.as_str()
                    };
                    !globset.is_match(filename)
                })
                .collect()
        }
    });

    Ok(filtered)
}

/// Expand subjects: find all subjects that have a given permission on an object
/// This is the inverse of check_permission - instead of "does X have permission on Y",
/// it answers "who has permission on Y"
#[pyfunction]
fn expand_subjects<'py>(
    py: Python<'py>,
    permission: String,
    object_type: String,
    object_id: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<Bound<'py, PyList>> {
    // Parse tuples from Python
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
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;
        let config: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
        })?;
        namespaces.insert(obj_type, config);
    }

    // Release GIL for computation
    let subjects = py.detach(|| {
        let object = Entity {
            entity_type: object_type,
            entity_id: object_id,
        };

        // Build graph indexes for fast lookups
        let graph = ReBACGraph::from_tuples(&rebac_tuples);
        let mut subjects: AHashSet<(String, String)> = AHashSet::new();
        let mut visited: AHashSet<(String, String, String)> = AHashSet::new();

        expand_permission(
            &permission,
            &object,
            &graph,
            &namespaces,
            &mut subjects,
            &mut visited,
            0,
        );

        subjects
    });

    // Convert to Python list of tuples
    let py_list = PyList::empty(py);
    for (subj_type, subj_id) in subjects {
        let tuple = PyTuple::new(py, &[subj_type, subj_id])?;
        py_list.append(tuple)?;
    }

    Ok(py_list)
}

/// Internal expand function - finds all subjects with a permission on an object
fn expand_permission(
    permission: &str,
    object: &Entity,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    subjects: &mut AHashSet<(String, String)>,
    visited: &mut AHashSet<(String, String, String)>,
    depth: u32,
) {
    const MAX_DEPTH: u32 = 50;

    if depth > MAX_DEPTH {
        return;
    }

    // Cycle detection
    let visit_key = (
        permission.to_string(),
        object.entity_type.clone(),
        object.entity_id.clone(),
    );
    if visited.contains(&visit_key) {
        return;
    }
    visited.insert(visit_key);

    // Get namespace config
    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            // No namespace - add direct subjects only
            add_direct_subjects(permission, object, graph, subjects);
            return;
        }
    };

    // Check if permission is defined in permissions map
    if let Some(usersets) = namespace.permissions.get(permission) {
        // Permission -> usersets (OR semantics) - expand each userset
        for userset in usersets {
            expand_permission(
                userset,
                object,
                graph,
                namespaces,
                subjects,
                &mut visited.clone(),
                depth + 1,
            );
        }
        return;
    }

    // Check if permission is a relation
    if let Some(relation_config) = namespace.relations.get(permission) {
        match relation_config {
            RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                // Direct relation - add all direct subjects
                add_direct_subjects(permission, object, graph, subjects);
            }
            RelationConfig::Union { union } => {
                // Union (OR semantics) - expand each relation
                for rel in union {
                    expand_permission(
                        rel,
                        object,
                        graph,
                        namespaces,
                        subjects,
                        &mut visited.clone(),
                        depth + 1,
                    );
                }
            }
            RelationConfig::TupleToUserset { tuple_to_userset } => {
                // TupleToUserset: find related objects, expand on them
                let related_objects =
                    graph.find_related_objects(object, &tuple_to_userset.tupleset);
                for related_obj in related_objects {
                    expand_permission(
                        &tuple_to_userset.computed_userset,
                        &related_obj,
                        graph,
                        namespaces,
                        subjects,
                        &mut visited.clone(),
                        depth + 1,
                    );
                }
            }
        }
        return;
    }

    // Permission not in namespace - add direct subjects
    add_direct_subjects(permission, object, graph, subjects);
}

/// Add all direct subjects that have a relation on an object
fn add_direct_subjects(
    relation: &str,
    object: &Entity,
    graph: &ReBACGraph,
    subjects: &mut AHashSet<(String, String)>,
) {
    // Get direct subjects from tuple index
    // We need to find all tuples where (object_type, object_id, relation) matches
    // and extract (subject_type, subject_id)
    for (key, _) in graph.tuple_index.iter() {
        let (obj_type, obj_id, rel, subj_type, subj_id) = key;
        if obj_type == &object.entity_type && obj_id == &object.entity_id && rel == relation {
            subjects.insert((subj_type.clone(), subj_id.clone()));
        }
    }

    // Also check userset subjects (group memberships that grant this relation)
    for userset in graph.get_usersets(object, relation) {
        // The userset itself is also a subject (e.g., group:eng#member)
        // Add the userset as a subject - the caller can expand further if needed
        subjects.insert((
            format!("{}#{}", userset.subject_type, userset.subject_relation),
            userset.subject_id.clone(),
        ));
    }
}

/// Python module definition
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(filter_paths, m)?)?;
    Ok(())
}

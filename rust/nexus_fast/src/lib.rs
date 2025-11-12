#![allow(clippy::useless_conversion)]

use ahash::{AHashMap, AHashSet};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
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
            // No namespace, check direct relation
            let result = check_direct_relation(subject, permission, object, tuples);
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
                check_direct_relation(subject, permission, object, tuples)
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
        false
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
        if tuple.object_type == object.entity_type
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

/// Python module definition
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_permissions_bulk, m)?)?;
    Ok(())
}

//! ReBAC engine — PyO3 wrappers with interned graph caching and parallel computation.
//!
//! Domain types, graph structures, and core algorithms are imported from `nexus_core`.
//! This module provides: thread-local caching, DashMap-based parallel computation,
//! Python dict parsing, and #[pyfunction] exports.

use ahash::{AHashMap, AHashSet};
use dashmap::DashMap;
use lru::LruCache;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use rayon::prelude::*;
use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::num::NonZeroUsize;
use string_interner::DefaultStringInterner;

// Re-use all domain types and algorithms from nexus_core.
use nexus_core::rebac::graph::{compute_permission_interned, InternedGraph};
use nexus_core::rebac::{
    collect_candidate_objects_for_subject, compute_permission, expand_permission,
    find_subject_groups, ReBACGraph, MAX_DEPTH,
};
use nexus_core::types::{
    CheckRequest, Entity, InternedEntity, InternedMemoCache, InternedMemoKey,
    InternedNamespaceConfig, InternedRelationConfig, InternedTuple, MemoCache, NamespaceConfig,
    ReBACTuple, Sym,
};

// ============================================================================
// Thread-local caches (PyO3-specific, not in nexus_core)
// ============================================================================

thread_local! {
    static GRAPH_CACHE: RefCell<Option<(u64, DefaultStringInterner, InternedGraph)>> =
        const { RefCell::new(None) };
}

const NAMESPACE_CACHE_CAPACITY: usize = 256;

thread_local! {
    static NAMESPACE_CONFIG_CACHE: RefCell<LruCache<u64, (String, NamespaceConfig)>> =
        RefCell::new(LruCache::new(NonZeroUsize::new(NAMESPACE_CACHE_CAPACITY).unwrap()));
}

/// Threshold for parallelization.
const PERMISSION_PARALLEL_THRESHOLD: usize = 50;

/// DashMap-based memo cache for parallel permission computation.
type SharedInternedMemoCache = DashMap<InternedMemoKey, bool, ahash::RandomState>;

// ============================================================================
// Parallel permission computation (DashMap shared cache — PyO3-specific)
// ============================================================================

#[allow(clippy::too_many_arguments)]
fn check_relation_with_usersets_interned_shared(
    subject: InternedEntity,
    relation: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &SharedInternedMemoCache,
    visited: &mut AHashSet<InternedMemoKey>,
    depth: u32,
) -> bool {
    if graph.check_direct_relation(subject, relation, object) {
        return true;
    }
    for userset in graph.get_usersets(object, relation) {
        let userset_entity = InternedEntity {
            entity_type: userset.subject_type,
            entity_id: userset.subject_id,
        };
        if compute_permission_interned_shared(
            subject,
            userset.subject_relation,
            userset_entity,
            graph,
            namespaces,
            memo_cache,
            visited,
            depth + 1,
        ) {
            return true;
        }
    }
    false
}

#[allow(clippy::too_many_arguments)]
fn compute_permission_interned_shared(
    subject: InternedEntity,
    permission: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &SharedInternedMemoCache,
    visited: &mut AHashSet<InternedMemoKey>,
    depth: u32,
) -> bool {
    if depth > MAX_DEPTH {
        return false;
    }

    let memo_key = (
        subject.entity_type,
        subject.entity_id,
        permission,
        object.entity_type,
        object.entity_id,
    );

    if let Some(result) = memo_cache.get(&memo_key) {
        return *result;
    }

    if visited.contains(&memo_key) {
        return false;
    }
    visited.insert(memo_key);

    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            let result = check_relation_with_usersets_interned_shared(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
            );
            memo_cache.insert(memo_key, result);
            return result;
        }
    };

    let result = if let Some(usersets) = namespace.permissions.get(&permission) {
        usersets.iter().any(|&userset| {
            compute_permission_interned_shared(
                subject,
                userset,
                object,
                graph,
                namespaces,
                memo_cache,
                &mut visited.clone(),
                depth + 1,
            )
        })
    } else if let Some(relation_config) = namespace.relations.get(&permission) {
        match relation_config {
            InternedRelationConfig::Direct => check_relation_with_usersets_interned_shared(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
            ),
            InternedRelationConfig::Union { union } => union.iter().any(|&rel| {
                compute_permission_interned_shared(
                    subject,
                    rel,
                    object,
                    graph,
                    namespaces,
                    memo_cache,
                    &mut visited.clone(),
                    depth + 1,
                )
            }),
            InternedRelationConfig::TupleToUserset {
                tupleset,
                computed_userset,
            } => graph
                .find_related_objects(object, *tupleset)
                .iter()
                .any(|&obj| {
                    compute_permission_interned_shared(
                        subject,
                        *computed_userset,
                        obj,
                        graph,
                        namespaces,
                        memo_cache,
                        &mut visited.clone(),
                        depth + 1,
                    )
                }),
        }
    } else {
        check_relation_with_usersets_interned_shared(
            subject, permission, object, graph, namespaces, memo_cache, visited, depth,
        )
    };

    memo_cache.insert(memo_key, result);
    result
}

// ============================================================================
// Python dict parsing helpers
// ============================================================================

/// Extract a required string field from a Python dict, returning PyKeyError on missing.
fn required_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    dict.get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(format!("Missing key: {}", key)))?
        .extract()
}

fn parse_tuples_from_py(tuples: &Bound<PyList>) -> PyResult<Vec<ReBACTuple>> {
    tuples
        .iter()
        .map(|item| {
            let dict: Bound<'_, PyDict> = item.extract()?;
            Ok(ReBACTuple {
                subject_type: required_str(&dict, "subject_type")?,
                subject_id: required_str(&dict, "subject_id")?,
                subject_relation: dict
                    .get_item("subject_relation")?
                    .and_then(|v| v.extract().ok()),
                relation: required_str(&dict, "relation")?,
                object_type: required_str(&dict, "object_type")?,
                object_id: required_str(&dict, "object_id")?,
            })
        })
        .collect()
}

fn parse_namespace_configs_from_py(
    py: Python<'_>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<AHashMap<String, NamespaceConfig>> {
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        let config: NamespaceConfig = NAMESPACE_CONFIG_CACHE.with(|cache| {
            let mut cache_ref = cache.borrow_mut();
            if let Some((cached_type, cached_config)) = cache_ref.get(&cache_key) {
                if cached_type == &obj_type {
                    return Ok::<NamespaceConfig, pyo3::PyErr>(cached_config.clone());
                }
            }
            let parsed: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
            })?;
            cache_ref.put(cache_key, (obj_type.clone(), parsed.clone()));
            Ok(parsed)
        })?;
        namespaces.insert(obj_type, config);
    }
    Ok(namespaces)
}

// ============================================================================
// PyO3 exported functions
// ============================================================================

/// Compute permissions in bulk using interned graph + optional parallelism.
#[pyfunction]
pub fn compute_permissions_bulk<'py>(
    py: Python<'py>,
    checks: &Bound<PyList>,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
    tuple_version: u64,
) -> PyResult<Bound<'py, PyDict>> {
    let (mut interner, cached_graph) = GRAPH_CACHE.with(|cache| {
        let mut cache_ref = cache.borrow_mut();
        if let Some((cached_version, cached_interner, cached_graph)) = cache_ref.take() {
            if cached_version == tuple_version {
                return (cached_interner, Some(cached_graph));
            }
        }
        (DefaultStringInterner::new(), None)
    });

    let check_requests: Vec<(CheckRequest, InternedEntity, Sym, InternedEntity)> = checks
        .iter()
        .map(|item| {
            let tuple: Bound<'_, PyTuple> = item.extract()?;
            let subject_item = tuple.get_item(0)?;
            let subject: Bound<'_, PyTuple> = subject_item.extract()?;
            let permission: String = tuple.get_item(1)?.extract()?;
            let object_item = tuple.get_item(2)?;
            let object: Bound<'_, PyTuple> = object_item.extract()?;

            let subject_type: String = subject.get_item(0)?.extract()?;
            let subject_id: String = subject.get_item(1)?.extract()?;
            let object_type: String = object.get_item(0)?.extract()?;
            let object_id: String = object.get_item(1)?.extract()?;

            let subject_entity = InternedEntity {
                entity_type: interner.get_or_intern(&subject_type),
                entity_id: interner.get_or_intern(&subject_id),
            };
            let permission_sym = interner.get_or_intern(&permission);
            let object_entity = InternedEntity {
                entity_type: interner.get_or_intern(&object_type),
                entity_id: interner.get_or_intern(&object_id),
            };

            let original_request = (subject_type, subject_id, permission, object_type, object_id);

            Ok((
                original_request,
                subject_entity,
                permission_sym,
                object_entity,
            ))
        })
        .collect::<PyResult<Vec<_>>>()?;

    let graph = if let Some(g) = cached_graph {
        g
    } else {
        let interned_tuples: Vec<InternedTuple> = tuples
            .iter()
            .map(|item| {
                let dict: Bound<'_, PyDict> = item.extract()?;
                let subject_type: String = required_str(&dict, "subject_type")?;
                let subject_id: String = required_str(&dict, "subject_id")?;
                let subject_relation: Option<String> = dict
                    .get_item("subject_relation")?
                    .and_then(|v| v.extract().ok());
                let relation: String = required_str(&dict, "relation")?;
                let object_type: String = required_str(&dict, "object_type")?;
                let object_id: String = required_str(&dict, "object_id")?;

                Ok(InternedTuple {
                    subject_type: interner.get_or_intern(&subject_type),
                    subject_id: interner.get_or_intern(&subject_id),
                    subject_relation: subject_relation.map(|s| interner.get_or_intern(&s)),
                    relation: interner.get_or_intern(&relation),
                    object_type: interner.get_or_intern(&object_type),
                    object_id: interner.get_or_intern(&object_id),
                })
            })
            .collect::<PyResult<Vec<_>>>()?;

        InternedGraph::from_tuples(&interned_tuples, &mut interner)
    };

    let mut interned_namespaces: AHashMap<Sym, InternedNamespaceConfig> = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        let config: NamespaceConfig = NAMESPACE_CONFIG_CACHE.with(|cache| {
            let mut cache_ref = cache.borrow_mut();
            if let Some((cached_type, cached_config)) = cache_ref.get(&cache_key) {
                if cached_type == &obj_type {
                    return Ok::<NamespaceConfig, pyo3::PyErr>(cached_config.clone());
                }
            }
            let parsed: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
            })?;
            cache_ref.put(cache_key, (obj_type.clone(), parsed.clone()));
            Ok(parsed)
        })?;

        let interned_config = InternedNamespaceConfig::from_config(&config, &mut interner);
        interned_namespaces.insert(interner.get_or_intern(&obj_type), interned_config);
    }

    let graph_for_cache = graph.clone();

    let results = py.detach(|| {
        if check_requests.len() < PERMISSION_PARALLEL_THRESHOLD {
            let mut results = AHashMap::new();
            let mut memo_cache: InternedMemoCache = AHashMap::new();

            for (original_request, subject, permission, object) in check_requests {
                let allowed = compute_permission_interned(
                    subject,
                    permission,
                    object,
                    &graph,
                    &interned_namespaces,
                    &mut memo_cache,
                    &mut AHashSet::new(),
                    0,
                );

                results.insert(original_request, allowed);
            }

            results
        } else {
            let shared_memo_cache: SharedInternedMemoCache =
                DashMap::with_hasher(ahash::RandomState::new());

            let results_vec: Vec<_> = check_requests
                .into_par_iter()
                .map(|(original_request, subject, permission, object)| {
                    let allowed = compute_permission_interned_shared(
                        subject,
                        permission,
                        object,
                        &graph,
                        &interned_namespaces,
                        &shared_memo_cache,
                        &mut AHashSet::new(),
                        0,
                    );

                    (original_request, allowed)
                })
                .collect();

            results_vec.into_iter().collect()
        }
    });

    GRAPH_CACHE.with(|cache| {
        *cache.borrow_mut() = Some((tuple_version, interner, graph_for_cache));
    });

    let py_dict = PyDict::new(py);
    for (key, value) in results {
        py_dict.set_item(key, value)?;
    }

    Ok(py_dict)
}

/// Check a single permission.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn compute_permission_single(
    py: Python<'_>,
    subject_type: String,
    subject_id: String,
    permission: String,
    object_type: String,
    object_id: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<bool> {
    let rebac_tuples = parse_tuples_from_py(tuples)?;
    let namespaces = parse_namespace_configs_from_py(py, namespace_configs)?;

    let result = py.detach(|| {
        let subject = Entity {
            entity_type: subject_type,
            entity_id: subject_id,
        };
        let object = Entity {
            entity_type: object_type,
            entity_id: object_id,
        };

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

/// Expand subjects: find all subjects that have a given permission on an object.
#[pyfunction]
pub fn expand_subjects<'py>(
    py: Python<'py>,
    permission: String,
    object_type: String,
    object_id: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<Bound<'py, PyList>> {
    let rebac_tuples = parse_tuples_from_py(tuples)?;
    let namespaces = parse_namespace_configs_from_py(py, namespace_configs)?;

    let subjects = py.detach(|| {
        let object = Entity {
            entity_type: object_type,
            entity_id: object_id,
        };

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

    let py_list = PyList::empty(py);
    for (subj_type, subj_id) in subjects {
        let tuple = PyTuple::new(py, &[subj_type, subj_id])?;
        py_list.append(tuple)?;
    }

    Ok(py_list)
}

/// List objects that a subject can access with a given permission.
#[pyfunction]
#[pyo3(signature = (subject_type, subject_id, permission, object_type, tuples, namespace_configs, path_prefix=None, limit=1000, offset=0))]
#[allow(clippy::too_many_arguments)]
pub fn list_objects_for_subject<'py>(
    py: Python<'py>,
    subject_type: String,
    subject_id: String,
    permission: String,
    object_type: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
    path_prefix: Option<String>,
    limit: usize,
    offset: usize,
) -> PyResult<Bound<'py, PyList>> {
    let rebac_tuples = parse_tuples_from_py(tuples)?;
    let namespaces = parse_namespace_configs_from_py(py, namespace_configs)?;

    let objects = py.detach(|| {
        let subject = Entity {
            entity_type: subject_type,
            entity_id: subject_id,
        };

        let graph = ReBACGraph::from_tuples(&rebac_tuples);

        let mut candidate_objects: AHashSet<Entity> = AHashSet::new();

        collect_candidate_objects_for_subject(
            &subject,
            &permission,
            &object_type,
            &graph,
            &namespaces,
            &mut candidate_objects,
        );

        let groups = find_subject_groups(&subject, &graph);
        for group in &groups {
            collect_candidate_objects_for_subject(
                group,
                &permission,
                &object_type,
                &graph,
                &namespaces,
                &mut candidate_objects,
            );
        }

        let mut verified_objects: Vec<Entity> = Vec::new();
        let mut memo_cache: MemoCache = AHashMap::new();

        for candidate in candidate_objects {
            if let Some(ref prefix) = path_prefix {
                if !candidate.entity_id.starts_with(prefix) {
                    continue;
                }
            }

            if compute_permission(
                &subject,
                &permission,
                &candidate,
                &graph,
                &namespaces,
                &mut memo_cache,
                &mut AHashSet::new(),
                0,
            ) {
                verified_objects.push(candidate);
            }
        }

        verified_objects.sort_by(|a, b| a.entity_id.cmp(&b.entity_id));

        verified_objects
            .into_iter()
            .skip(offset)
            .take(limit)
            .collect::<Vec<_>>()
    });

    let py_list = PyList::empty(py);
    for obj in objects {
        let tuple = PyTuple::new(py, &[obj.entity_type, obj.entity_id])?;
        py_list.append(tuple)?;
    }

    Ok(py_list)
}

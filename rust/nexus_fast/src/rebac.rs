// =============================================================================
// ReBAC Permission Engine - String Interning, Graph Indexing, Permission Checks
// =============================================================================

use ahash::{AHashMap, AHashSet};
use dashmap::DashMap;
use lru::LruCache;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use rayon::prelude::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap as StdHashMap;
use std::hash::{Hash, Hasher};
use std::num::NonZeroUsize;
use string_interner::{DefaultStringInterner, DefaultSymbol};

/// Type alias for interned string symbol - 4 bytes, O(1) equality, Copy
type Sym = DefaultSymbol;

// Thread-local cache for the ReBAC graph to avoid rebuilding on every call.
// Stores (tuple_version, interner, graph) - when tuple_version matches, we reuse the cached graph.
// The interner is cached alongside because the graph's symbols are only valid within that interner.
thread_local! {
    static GRAPH_CACHE: RefCell<Option<(u64, DefaultStringInterner, InternedGraph)>> =
        const { RefCell::new(None) };
}

// Thread-local LRU cache for parsed namespace configurations (Issue #861).
// Stores raw NamespaceConfig (post-JSON parsing, pre-interning) to avoid
// repeated Python json.dumps() + serde_json parsing overhead.
// Key: (object_type, config_hash) - hash ensures cache invalidation on config changes.
// Capacity: 256 entries (typical deployments have <50 object types).
const NAMESPACE_CACHE_CAPACITY: usize = 256;

thread_local! {
    static NAMESPACE_CONFIG_CACHE: RefCell<LruCache<u64, (String, NamespaceConfig)>> =
        RefCell::new(LruCache::new(NonZeroUsize::new(NAMESPACE_CACHE_CAPACITY).unwrap()));
}

/// Threshold for parallelization of permission checks
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

// ============================================================================
// INTERNED TYPES - String interning for O(1) equality and reduced allocations
// ============================================================================

/// Interned entity with symbols for O(1) equality
#[derive(Debug, Clone, Copy, Hash, Eq, PartialEq)]
struct InternedEntity {
    entity_type: Sym,
    entity_id: Sym,
}

/// Interned ReBAC tuple with symbols
#[derive(Debug, Clone, Copy)]
struct InternedTuple {
    subject_type: Sym,
    subject_id: Sym,
    subject_relation: Option<Sym>,
    relation: Sym,
    object_type: Sym,
    object_id: Sym,
}

/// Interned userset entry
#[derive(Debug, Clone, Copy)]
struct InternedUsersetEntry {
    subject_type: Sym,
    subject_id: Sym,
    subject_relation: Sym,
}

/// Interned key types
type InternedMemoKey = (Sym, Sym, Sym, Sym, Sym);
type InternedMemoCache = AHashMap<InternedMemoKey, bool>;
/// Shared memoization cache for parallel execution - lock-free concurrent access
type SharedInternedMemoCache = DashMap<InternedMemoKey, bool, ahash::RandomState>;
type InternedTupleKey = (Sym, Sym, Sym, Sym, Sym);
type InternedAdjacencyKey = (Sym, Sym, Sym);
type InternedUsersetKey = (Sym, Sym, Sym);

/// Interned namespace config for fast lookups
#[derive(Debug, Clone)]
struct InternedNamespaceConfig {
    relations: AHashMap<Sym, InternedRelationConfig>,
    permissions: AHashMap<Sym, Vec<Sym>>,
}

#[derive(Debug, Clone)]
enum InternedRelationConfig {
    Direct,
    Union {
        union: Vec<Sym>,
    },
    TupleToUserset {
        tupleset: Sym,
        computed_userset: Sym,
    },
}

impl InternedNamespaceConfig {
    fn from_config(config: &NamespaceConfig, interner: &mut DefaultStringInterner) -> Self {
        let relations = config
            .relations
            .iter()
            .map(|(k, v)| {
                let key = interner.get_or_intern(k);
                let value = match v {
                    RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                        InternedRelationConfig::Direct
                    }
                    RelationConfig::Union { union } => InternedRelationConfig::Union {
                        union: union.iter().map(|s| interner.get_or_intern(s)).collect(),
                    },
                    RelationConfig::TupleToUserset { tuple_to_userset } => {
                        InternedRelationConfig::TupleToUserset {
                            tupleset: interner.get_or_intern(&tuple_to_userset.tupleset),
                            computed_userset: interner
                                .get_or_intern(&tuple_to_userset.computed_userset),
                        }
                    }
                };
                (key, value)
            })
            .collect();

        let permissions = config
            .permissions
            .iter()
            .map(|(k, v)| {
                let key = interner.get_or_intern(k);
                let values: Vec<Sym> = v.iter().map(|s| interner.get_or_intern(s)).collect();
                (key, values)
            })
            .collect();

        InternedNamespaceConfig {
            relations,
            permissions,
        }
    }
}

/// Graph with interned symbols for fast lookups
#[derive(Debug, Clone)]
struct InternedGraph {
    tuple_index: AHashMap<InternedTupleKey, bool>,
    adjacency_list: AHashMap<InternedAdjacencyKey, Vec<InternedEntity>>,
    userset_index: AHashMap<InternedUsersetKey, Vec<InternedUsersetEntry>>,
    /// Wildcard subject (*:*) symbol - Issue #1064
    wildcard_subject: Option<InternedEntity>,
}

impl InternedGraph {
    fn from_tuples(tuples: &[InternedTuple], interner: &mut DefaultStringInterner) -> Self {
        // Intern the wildcard subject "*:*" for fast comparison - Issue #1064
        let wildcard_type = interner.get_or_intern("*");
        let wildcard_id = interner.get_or_intern("*");
        let wildcard_subject = Some(InternedEntity {
            entity_type: wildcard_type,
            entity_id: wildcard_id,
        });
        let mut tuple_index = AHashMap::new();
        let mut adjacency_list: AHashMap<InternedAdjacencyKey, Vec<InternedEntity>> =
            AHashMap::new();
        let mut userset_index: AHashMap<InternedUsersetKey, Vec<InternedUsersetEntry>> =
            AHashMap::new();

        for tuple in tuples {
            if let Some(subject_relation) = tuple.subject_relation {
                let userset_key = (tuple.object_type, tuple.object_id, tuple.relation);
                userset_index
                    .entry(userset_key)
                    .or_default()
                    .push(InternedUsersetEntry {
                        subject_type: tuple.subject_type,
                        subject_id: tuple.subject_id,
                        subject_relation,
                    });
            } else {
                let tuple_key = (
                    tuple.object_type,
                    tuple.object_id,
                    tuple.relation,
                    tuple.subject_type,
                    tuple.subject_id,
                );
                tuple_index.insert(tuple_key, true);
            }

            let adj_key = (tuple.subject_type, tuple.subject_id, tuple.relation);
            adjacency_list
                .entry(adj_key)
                .or_default()
                .push(InternedEntity {
                    entity_type: tuple.object_type,
                    entity_id: tuple.object_id,
                });
        }

        InternedGraph {
            tuple_index,
            adjacency_list,
            userset_index,
            wildcard_subject,
        }
    }

    fn check_direct_relation(
        &self,
        subject: InternedEntity,
        relation: Sym,
        object: InternedEntity,
    ) -> bool {
        // Check 1: Exact subject match
        let tuple_key = (
            object.entity_type,
            object.entity_id,
            relation,
            subject.entity_type,
            subject.entity_id,
        );
        if self.tuple_index.contains_key(&tuple_key) {
            return true;
        }

        // Check 2: Wildcard subject match (*:*) - Issue #1064
        // Wildcard tuples grant access to ALL subjects
        if let Some(wildcard) = &self.wildcard_subject {
            let wildcard_key = (
                object.entity_type,
                object.entity_id,
                relation,
                wildcard.entity_type,
                wildcard.entity_id,
            );
            if self.tuple_index.contains_key(&wildcard_key) {
                return true;
            }
        }

        false
    }

    fn find_related_objects(&self, object: InternedEntity, relation: Sym) -> Vec<InternedEntity> {
        // For tupleToUserset: find objects that 'object' has relation TO
        // e.g., file:doc1 -[parent]-> folder:folder1, we need to find folder:folder1
        let adj_key = (object.entity_type, object.entity_id, relation);
        self.adjacency_list
            .get(&adj_key)
            .cloned()
            .unwrap_or_default()
    }

    fn get_usersets(&self, object: InternedEntity, relation: Sym) -> &[InternedUsersetEntry] {
        let userset_key = (object.entity_type, object.entity_id, relation);
        self.userset_index
            .get(&userset_key)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }
}

/// Check relation with interned types - no allocations!
#[allow(clippy::too_many_arguments)]
fn check_relation_with_usersets_interned(
    subject: InternedEntity,
    relation: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &mut InternedMemoCache,
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

        if compute_permission_interned(
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

/// Compute permission with interned types - O(1) key operations, no allocations!
#[allow(clippy::too_many_arguments)]
fn compute_permission_interned(
    subject: InternedEntity,
    permission: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &mut InternedMemoCache,
    visited: &mut AHashSet<InternedMemoKey>,
    depth: u32,
) -> bool {
    const MAX_DEPTH: u32 = 50;

    if depth > MAX_DEPTH {
        return false;
    }

    // Check memo cache - O(1) with interned symbols!
    let memo_key = (
        subject.entity_type,
        subject.entity_id,
        permission,
        object.entity_type,
        object.entity_id,
    );

    if let Some(&result) = memo_cache.get(&memo_key) {
        return result;
    }

    // Cycle detection - O(1) with interned symbols!
    if visited.contains(&memo_key) {
        return false;
    }
    visited.insert(memo_key);

    // Get namespace config - O(1) with interned symbols!
    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            let result = check_relation_with_usersets_interned(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
            );
            memo_cache.insert(memo_key, result);
            return result;
        }
    };

    let result = if let Some(usersets) = namespace.permissions.get(&permission) {
        let mut allowed = false;
        for &userset in usersets {
            if compute_permission_interned(
                subject,
                userset,
                object,
                graph,
                namespaces,
                memo_cache,
                visited,
                depth + 1,
            ) {
                allowed = true;
                break;
            }
        }
        allowed
    } else if let Some(relation_config) = namespace.relations.get(&permission) {
        match relation_config {
            InternedRelationConfig::Direct => check_relation_with_usersets_interned(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
            ),
            InternedRelationConfig::Union { union } => {
                let mut allowed = false;
                for &rel in union {
                    if compute_permission_interned(
                        subject,
                        rel,
                        object,
                        graph,
                        namespaces,
                        memo_cache,
                        visited,
                        depth + 1,
                    ) {
                        allowed = true;
                        break;
                    }
                }
                allowed
            }
            InternedRelationConfig::TupleToUserset {
                tupleset,
                computed_userset,
            } => {
                let related_objects = graph.find_related_objects(object, *tupleset);
                let mut allowed = false;
                for related_obj in related_objects {
                    if compute_permission_interned(
                        subject,
                        *computed_userset,
                        related_obj,
                        graph,
                        namespaces,
                        memo_cache,
                        visited,
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
        check_relation_with_usersets_interned(
            subject, permission, object, graph, namespaces, memo_cache, visited, depth,
        )
    };

    memo_cache.insert(memo_key, result);
    result
}

/// Check relation with shared cache for parallel execution
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

/// Compute permission with shared DashMap cache for parallel execution
/// Uses lock-free concurrent access across threads
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
    const MAX_DEPTH: u32 = 50;
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

    // Lock-free cache lookup
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
// END INTERNED TYPES
// ============================================================================

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

            // Build adjacency list for finding related objects (subject -> objects)
            // Used for tupleToUserset: given subject, find objects they have relation to
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
        // Check 1: Exact subject match
        let tuple_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
            subject.entity_type.clone(),
            subject.entity_id.clone(),
        );
        if self.tuple_index.contains_key(&tuple_key) {
            return true;
        }

        // Check 2: Wildcard subject match (*:*) - Issue #1064
        // Wildcard tuples grant access to ALL subjects
        let wildcard_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
            "*".to_string(),
            "*".to_string(),
        );
        self.tuple_index.contains_key(&wildcard_key)
    }

    /// Find related objects in O(1) time using adjacency list
    /// For tupleToUserset: find objects that 'object' has relation TO
    /// e.g., file:doc1 -[parent]-> folder:folder1, we need to find folder:folder1
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

// ============================================================================
// DRY HELPERS - Extracted from duplicated blocks
// ============================================================================

/// Parse namespace configs from Python dict with LRU caching.
/// Replaces 4 duplicated blocks across compute_permissions_bulk,
/// compute_permission_single, expand_subjects, list_objects_for_subject.
fn parse_namespace_configs(
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

/// Parse ReBAC tuples from Python list of dicts.
/// Replaces 3 duplicated blocks across compute_permission_single,
/// expand_subjects, list_objects_for_subject.
fn parse_tuples_from_py(tuples: &Bound<PyList>) -> PyResult<Vec<ReBACTuple>> {
    tuples
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
        .collect()
}

// ============================================================================
// NON-INTERNED PERMISSION ENGINE (for single checks / expand / list)
// ============================================================================

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

// ============================================================================
// PYFUNCTION EXPORTS
// ============================================================================

/// Main function: compute permissions in bulk using Rust
/// Uses string interning for O(1) string operations and minimal allocations
/// Now with graph caching: when tuple_version matches the cached version,
/// we reuse the cached graph instead of rebuilding it (Issue #862)
#[pyfunction]
pub fn compute_permissions_bulk<'py>(
    py: Python<'py>,
    checks: &Bound<PyList>,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
    tuple_version: u64,
) -> PyResult<Bound<'py, PyDict>> {
    // Check cache and get interner + graph (if cached version matches)
    let (mut interner, cached_graph) = GRAPH_CACHE.with(|cache| {
        let mut cache_ref = cache.borrow_mut();
        if let Some((cached_version, cached_interner, cached_graph)) = cache_ref.take() {
            if cached_version == tuple_version {
                // Cache hit - reuse the interner and graph
                // The interner already has all tuple strings, so new check strings
                // will be added to it, and existing strings will get the same symbols
                return (cached_interner, Some(cached_graph));
            }
            // Version mismatch - discard cached data and start fresh
        }
        // Cache miss - create new interner
        (DefaultStringInterner::new(), None)
    });

    // Parse and intern check requests from Python
    // Keep original strings for result keys, create interned versions for computation
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

            // Create interned entities - O(1) operations from now on!
            let subject_entity = InternedEntity {
                entity_type: interner.get_or_intern(&subject_type),
                entity_id: interner.get_or_intern(&subject_id),
            };
            let permission_sym = interner.get_or_intern(&permission);
            let object_entity = InternedEntity {
                entity_type: interner.get_or_intern(&object_type),
                entity_id: interner.get_or_intern(&object_id),
            };

            // Keep original request for result key
            let original_request = (subject_type, subject_id, permission, object_type, object_id);

            Ok((
                original_request,
                subject_entity,
                permission_sym,
                object_entity,
            ))
        })
        .collect::<PyResult<Vec<_>>>()?;

    // Build or reuse graph based on cache
    let graph = if let Some(g) = cached_graph {
        // Cache hit - reuse the graph (skip tuple parsing and graph building)
        g
    } else {
        // Cache miss - parse tuples and build graph
        let interned_tuples: Vec<InternedTuple> = tuples
            .iter()
            .map(|item| {
                let dict: Bound<'_, PyDict> = item.extract()?;
                let subject_type: String = dict.get_item("subject_type")?.unwrap().extract()?;
                let subject_id: String = dict.get_item("subject_id")?.unwrap().extract()?;
                let subject_relation: Option<String> = dict
                    .get_item("subject_relation")?
                    .and_then(|v| v.extract().ok());
                let relation: String = dict.get_item("relation")?.unwrap().extract()?;
                let object_type: String = dict.get_item("object_type")?.unwrap().extract()?;
                let object_id: String = dict.get_item("object_id")?.unwrap().extract()?;

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

    // Parse namespace configs and convert to interned versions
    // Uses LRU cache to avoid repeated JSON parsing (Issue #861)
    let mut interned_namespaces: AHashMap<Sym, InternedNamespaceConfig> = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;

        // Convert PyDict to JSON string (needed for both cache key and parsing)
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        // Compute hash of (obj_type, config_json) for cache key
        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        // Try to get from cache, otherwise parse and cache
        let config: NamespaceConfig = NAMESPACE_CONFIG_CACHE.with(|cache| {
            let mut cache_ref = cache.borrow_mut();
            if let Some((cached_type, cached_config)) = cache_ref.get(&cache_key) {
                if cached_type == &obj_type {
                    // Cache hit - return cloned config
                    return Ok::<NamespaceConfig, pyo3::PyErr>(cached_config.clone());
                }
            }
            // Cache miss - parse JSON and store in cache
            let parsed: NamespaceConfig = serde_json::from_str(&config_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {}", e))
            })?;
            cache_ref.put(cache_key, (obj_type.clone(), parsed.clone()));
            Ok(parsed)
        })?;

        let interned_config = InternedNamespaceConfig::from_config(&config, &mut interner);
        interned_namespaces.insert(interner.get_or_intern(&obj_type), interned_config);
    }

    // Clone graph for caching before moving into computation closure
    let graph_for_cache = graph.clone();

    // Release GIL for computation - all string operations are now O(1) symbol operations!
    let results = py.detach(|| {
        if check_requests.len() < PERMISSION_PARALLEL_THRESHOLD {
            // Sequential for small batches (shared memoization cache)
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
            // Parallel for large batches with SHARED memoization cache
            // Uses DashMap for lock-free concurrent access across threads
            let shared_memo_cache: SharedInternedMemoCache =
                DashMap::with_hasher(ahash::RandomState::new());

            let results_vec: Vec<_> = check_requests
                .into_par_iter()
                .map(|(original_request, subject, permission, object)| {
                    // Use shared cache - threads share computed results!
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

    // Store the interner and graph in cache for next call
    GRAPH_CACHE.with(|cache| {
        *cache.borrow_mut() = Some((tuple_version, interner, graph_for_cache));
    });

    // Convert AHashMap to PyDict
    let py_dict = PyDict::new(py);
    for (key, value) in results {
        py_dict.set_item(key, value)?;
    }

    Ok(py_dict)
}

/// Python function: compute a single permission check
/// This is for interactive/single-check use cases (faster than Python, slower than bulk)
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
    // Parse tuples from Python
    let rebac_tuples = parse_tuples_from_py(tuples)?;

    // Parse namespace configs (with LRU caching - Issue #861)
    let namespaces = parse_namespace_configs(py, namespace_configs)?;

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

/// Expand subjects: find all subjects that have a given permission on an object
/// This is the inverse of check_permission - instead of "does X have permission on Y",
/// it answers "who has permission on Y"
#[pyfunction]
pub fn expand_subjects<'py>(
    py: Python<'py>,
    permission: String,
    object_type: String,
    object_id: String,
    tuples: &Bound<PyList>,
    namespace_configs: &Bound<PyDict>,
) -> PyResult<Bound<'py, PyList>> {
    // Parse tuples from Python
    let rebac_tuples = parse_tuples_from_py(tuples)?;

    // Parse namespace configs (with LRU caching - Issue #861)
    let namespaces = parse_namespace_configs(py, namespace_configs)?;

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

/// List objects that a subject can access with a given permission
/// This is the inverse of expand_subjects - instead of "who has permission on Y",
/// it answers "what objects can subject X access"
///
/// Uses the adjacency_list index for O(1) lookups per relation, then validates
/// each candidate object with full permission expansion.
///
/// Args:
///   subject_type: Type of subject (e.g., "user", "agent")
///   subject_id: ID of subject (e.g., "alice")
///   permission: Permission to check (e.g., "read", "write")
///   object_type: Type of objects to find (e.g., "file")
///   tuples: List of ReBAC relationship tuples
///   namespace_configs: Namespace configuration for permission expansion
///   path_prefix: Optional path prefix filter (e.g., "/workspace/")
///   limit: Maximum number of results to return
///   offset: Number of results to skip (for pagination)
///
/// Returns:
///   List of (object_type, object_id) tuples that subject can access
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
    // Parse tuples from Python
    let rebac_tuples = parse_tuples_from_py(tuples)?;

    // Parse namespace configs (with LRU caching - Issue #861)
    let namespaces = parse_namespace_configs(py, namespace_configs)?;

    // Release GIL for computation
    let objects = py.detach(|| {
        let subject = Entity {
            entity_type: subject_type,
            entity_id: subject_id,
        };

        // Build graph indexes for fast lookups
        let graph = ReBACGraph::from_tuples(&rebac_tuples);

        // Find all candidate objects the subject might have access to
        let mut candidate_objects: AHashSet<Entity> = AHashSet::new();

        // Step 1: Find direct relations from subject to objects
        // Look up adjacency list for all relations that might grant the permission
        collect_candidate_objects_for_subject(
            &subject,
            &permission,
            &object_type,
            &graph,
            &namespaces,
            &mut candidate_objects,
        );

        // Step 2: Find objects accessible through group membership
        // First, find all groups the subject belongs to
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

        // Step 3: Verify each candidate with full permission check
        // This handles complex permission rules (union, tupleToUserset, etc.)
        let mut verified_objects: Vec<Entity> = Vec::new();
        let mut memo_cache: MemoCache = AHashMap::new();

        for candidate in candidate_objects {
            // Apply path prefix filter early (before expensive permission check)
            if let Some(ref prefix) = path_prefix {
                if !candidate.entity_id.starts_with(prefix) {
                    continue;
                }
            }

            // Verify permission with full expansion
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

        // Sort by object_id for consistent pagination
        verified_objects.sort_by(|a, b| a.entity_id.cmp(&b.entity_id));

        // Apply pagination
        verified_objects
            .into_iter()
            .skip(offset)
            .take(limit)
            .collect::<Vec<_>>()
    });

    // Convert to Python list of tuples
    let py_list = PyList::empty(py);
    for obj in objects {
        let tuple = PyTuple::new(py, &[obj.entity_type, obj.entity_id])?;
        py_list.append(tuple)?;
    }

    Ok(py_list)
}

/// Collect candidate objects that a subject might have access to via direct relations
fn collect_candidate_objects_for_subject(
    subject: &Entity,
    permission: &str,
    object_type: &str,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    candidates: &mut AHashSet<Entity>,
) {
    // Get all relations that might grant this permission
    let relations = get_permission_relations(permission, object_type, namespaces);

    for relation in relations {
        // Look up adjacency list: (subject_type, subject_id, relation) -> objects
        let adj_key = (
            subject.entity_type.clone(),
            subject.entity_id.clone(),
            relation.clone(),
        );
        if let Some(objects) = graph.adjacency_list.get(&adj_key) {
            for obj in objects {
                if obj.entity_type == object_type {
                    candidates.insert(obj.clone());
                }
            }
        }
    }
}

/// Get all relations that can grant a permission (including via union/inheritance)
fn get_permission_relations(
    permission: &str,
    object_type: &str,
    namespaces: &AHashMap<String, NamespaceConfig>,
) -> Vec<String> {
    let mut expanded: AHashSet<String> = AHashSet::new();
    let mut to_expand: Vec<String> = vec![permission.to_string()];

    // Check namespace config for permission expansion
    if let Some(namespace) = namespaces.get(object_type) {
        // Step 1: Get usersets that grant this permission
        // e.g., "read" -> ["viewer", "editor", "owner"]
        if let Some(usersets) = namespace.permissions.get(permission) {
            to_expand.extend(usersets.iter().cloned());
        }

        // Step 2: Recursively expand each userset through unions
        // e.g., "owner" -> ["direct_owner", "parent_owner", "group_owner"]
        while let Some(rel) = to_expand.pop() {
            if expanded.contains(&rel) {
                continue;
            }
            expanded.insert(rel.clone());

            // Check if this relation has a union
            if let Some(RelationConfig::Union { union }) = namespace.relations.get(&rel) {
                for member in union {
                    if !expanded.contains(member) {
                        to_expand.push(member.clone());
                    }
                }
            }
        }
    }

    expanded.into_iter().collect()
}

/// Find all groups that a subject belongs to
fn find_subject_groups(subject: &Entity, graph: &ReBACGraph) -> Vec<Entity> {
    let mut groups = Vec::new();

    // Look for membership relations: subject -> member -> group
    let membership_relations = ["member", "member-of"];
    for rel in membership_relations {
        let adj_key = (
            subject.entity_type.clone(),
            subject.entity_id.clone(),
            rel.to_string(),
        );
        if let Some(group_entities) = graph.adjacency_list.get(&adj_key) {
            groups.extend(group_entities.iter().cloned());
        }
    }

    groups
}

#![allow(clippy::useless_conversion)]

use ahash::{AHashMap, AHashSet};
use bloomfilter::Bloom;
use dashmap::DashMap;
use lru::LruCache;
use memchr::memmem;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use rayon::prelude::*;
use regex::bytes::RegexBuilder;
use roaring::RoaringBitmap;
use serde::Deserialize;
use simdutf8::basic::from_utf8 as simd_from_utf8;
use simsimd::SpatialSimilarity;
use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::collections::HashMap as StdHashMap;
use std::fs::File;
use std::hash::{Hash, Hasher};
use std::num::NonZeroUsize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};
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

/// Main function: compute permissions in bulk using Rust
/// Uses string interning for O(1) string operations and minimal allocations
/// Now with graph caching: when tuple_version matches the cached version,
/// we reuse the cached graph instead of rebuilding it (Issue #862)
#[pyfunction]
fn compute_permissions_bulk<'py>(
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

    // Parse namespace configs (with LRU caching - Issue #861)
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        // Compute hash for cache key
        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        // Try cache, otherwise parse and cache
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

/// Check if a pattern is a literal string (no regex metacharacters).
/// Literal patterns can use SIMD-accelerated memchr search (Issue #863).
fn is_literal_pattern(pattern: &str) -> bool {
    !pattern.chars().any(|c| {
        matches!(
            c,
            '.' | '*' | '+' | '?' | '(' | ')' | '[' | ']' | '{' | '}' | '|' | '^' | '$' | '\\'
        )
    })
}

/// Search mode for grep_bulk - either SIMD-accelerated literal or regex
enum SearchMode<'a> {
    /// SIMD-accelerated literal search using memchr (4-10x faster)
    Literal {
        finder: memmem::Finder<'a>,
        pattern: &'a str,
    },
    /// Case-insensitive literal search (converts line to lowercase)
    LiteralIgnoreCase {
        finder: memmem::Finder<'a>,
        pattern_lower: String,
    },
    /// Full regex search for complex patterns
    Regex(regex::bytes::Regex),
}

/// Fast content search using Rust regex or SIMD-accelerated memchr for literals
#[pyfunction]
#[pyo3(signature = (pattern, file_contents, ignore_case=false, max_results=1000))]
fn grep_bulk<'py>(
    py: Python<'py>,
    pattern: &str,
    file_contents: &Bound<PyDict>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    // Determine search mode: use SIMD-accelerated memchr for literal patterns (Issue #863)
    let is_literal = is_literal_pattern(pattern);

    // For case-insensitive literal search, we need to own the lowercase pattern
    let pattern_lower: String;
    let search_mode = if is_literal {
        if ignore_case {
            pattern_lower = pattern.to_lowercase();
            SearchMode::LiteralIgnoreCase {
                finder: memmem::Finder::new(pattern_lower.as_bytes()),
                pattern_lower: pattern_lower.clone(),
            }
        } else {
            SearchMode::Literal {
                finder: memmem::Finder::new(pattern.as_bytes()),
                pattern,
            }
        }
    } else {
        // Fall back to regex for complex patterns
        let regex = RegexBuilder::new(pattern)
            .case_insensitive(ignore_case)
            .build()
            .map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
            })?;
        SearchMode::Regex(regex)
    };

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

            // Try to decode as UTF-8 using SIMD-accelerated validation (Issue #864)
            // simdutf8 is ~8x faster than std::str::from_utf8
            let content_str = match simd_from_utf8(&content_bytes) {
                Ok(s) => s,
                Err(_) => continue,
            };

            // Search line by line
            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();

                // Use appropriate search mode
                let match_result: Option<(usize, usize)> = match &search_mode {
                    SearchMode::Literal { finder, pattern } => {
                        // SIMD-accelerated literal search
                        finder
                            .find(line_bytes)
                            .map(|start| (start, start + pattern.len()))
                    }
                    SearchMode::LiteralIgnoreCase {
                        finder,
                        pattern_lower,
                    } => {
                        // Case-insensitive: convert line to lowercase and search
                        let line_lower = line.to_lowercase();
                        finder
                            .find(line_lower.as_bytes())
                            .map(|start| (start, start + pattern_lower.len()))
                    }
                    SearchMode::Regex(regex) => {
                        // Full regex search
                        regex.find(line_bytes).map(|m| (m.start(), m.end()))
                    }
                };

                if let Some((start, end)) = match_result {
                    // For case-insensitive literal, extract match from original line
                    let match_text = if matches!(&search_mode, SearchMode::LiteralIgnoreCase { .. })
                    {
                        // Get character boundaries for the match
                        line.chars()
                            .skip(line[..start].chars().count())
                            .take(end - start)
                            .collect::<String>()
                    } else {
                        simd_from_utf8(&line_bytes[start..end])
                            .unwrap_or("")
                            .to_string()
                    };

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

/// Threshold for using parallel processing in grep_files_mmap
const GREP_MMAP_PARALLEL_THRESHOLD: usize = 10;

/// Maximum file size to mmap (avoid excessive memory usage for huge files)
const GREP_MMAP_MAX_FILE_SIZE: u64 = 1024 * 1024 * 1024; // 1GB

/// Fast content search using memory-mapped I/O for zero-copy file access (Issue #893)
///
/// This function reads files directly from disk using mmap, avoiding the overhead
/// of passing file contents through Python. Best for searching large local files.
///
/// Performance characteristics:
/// - Small files (<4KB): Similar to grep_bulk (mmap overhead vs copy overhead)
/// - Medium files (4KB-10MB): 20-40% faster than grep_bulk
/// - Large files (>10MB): 50-70% faster than grep_bulk
/// - Parallel processing for batches of 10+ files
///
/// Args:
///     pattern: Regex pattern or literal string to search for
///     file_paths: List of absolute paths to search
///     ignore_case: Whether to ignore case in pattern matching
///     max_results: Maximum number of results to return
///
/// Returns:
///     List of match dicts with keys: file, line, content, match
///     Files that don't exist or can't be read are silently skipped.
#[pyfunction]
#[pyo3(signature = (pattern, file_paths, ignore_case=false, max_results=1000))]
fn grep_files_mmap<'py>(
    py: Python<'py>,
    pattern: &str,
    file_paths: Vec<String>,
    ignore_case: bool,
    max_results: usize,
) -> PyResult<Bound<'py, PyList>> {
    // Determine search mode: use SIMD-accelerated memchr for literal patterns
    let is_literal = is_literal_pattern(pattern);

    // Build the search pattern/regex
    let pattern_owned = pattern.to_string();

    // For parallel processing, we need to create thread-safe search components
    let regex_opt: Option<regex::bytes::Regex> = if !is_literal {
        Some(
            RegexBuilder::new(pattern)
                .case_insensitive(ignore_case)
                .build()
                .map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!("Invalid regex pattern: {}", e))
                })?,
        )
    } else {
        None
    };

    let pattern_lower: String = if is_literal && ignore_case {
        pattern.to_lowercase()
    } else {
        String::new()
    };

    // Process files - parallel for large batches, sequential for small
    let matches: Vec<GrepMatch> = py.detach(|| {
        if file_paths.len() < GREP_MMAP_PARALLEL_THRESHOLD {
            // Sequential processing for small batches
            grep_files_mmap_sequential(
                &file_paths,
                &pattern_owned,
                &pattern_lower,
                is_literal,
                ignore_case,
                regex_opt.as_ref(),
                max_results,
            )
        } else {
            // Parallel processing for large batches
            grep_files_mmap_parallel(
                file_paths,
                &pattern_owned,
                &pattern_lower,
                is_literal,
                ignore_case,
                regex_opt.as_ref(),
                max_results,
            )
        }
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

/// Sequential grep with mmap for small file batches
fn grep_files_mmap_sequential(
    file_paths: &[String],
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Vec<GrepMatch> {
    let mut results = Vec::new();

    for file_path in file_paths {
        if results.len() >= max_results {
            break;
        }

        if let Some(mut file_matches) = grep_single_file_mmap(
            file_path,
            pattern,
            pattern_lower,
            is_literal,
            ignore_case,
            regex_opt,
            max_results - results.len(),
        ) {
            results.append(&mut file_matches);
        }
    }

    results
}

/// Parallel grep with mmap for large file batches
fn grep_files_mmap_parallel(
    file_paths: Vec<String>,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Vec<GrepMatch> {
    use std::sync::atomic::{AtomicUsize, Ordering};

    let result_count = AtomicUsize::new(0);

    let all_matches: Vec<Vec<GrepMatch>> = file_paths
        .into_par_iter()
        .filter_map(|file_path| {
            // Early exit if we've hit max results
            if result_count.load(Ordering::Relaxed) >= max_results {
                return None;
            }

            let remaining = max_results.saturating_sub(result_count.load(Ordering::Relaxed));
            if remaining == 0 {
                return None;
            }

            let matches = grep_single_file_mmap(
                &file_path,
                pattern,
                pattern_lower,
                is_literal,
                ignore_case,
                regex_opt,
                remaining,
            )?;

            if !matches.is_empty() {
                result_count.fetch_add(matches.len(), Ordering::Relaxed);
                Some(matches)
            } else {
                None
            }
        })
        .collect();

    // Flatten and truncate to max_results
    let mut results: Vec<GrepMatch> = all_matches.into_iter().flatten().collect();
    results.truncate(max_results);
    results
}

/// Grep a single file using memory-mapped I/O
fn grep_single_file_mmap(
    file_path: &str,
    pattern: &str,
    pattern_lower: &str,
    is_literal: bool,
    ignore_case: bool,
    regex_opt: Option<&regex::bytes::Regex>,
    max_results: usize,
) -> Option<Vec<GrepMatch>> {
    // Open the file
    let file = File::open(file_path).ok()?;
    let metadata = file.metadata().ok()?;
    let file_size = metadata.len();

    // Skip empty files
    if file_size == 0 {
        return Some(Vec::new());
    }

    // For very large files, skip mmap to avoid memory pressure
    if file_size > GREP_MMAP_MAX_FILE_SIZE {
        return None; // Let caller fall back to chunked reading
    }

    // Memory-map the file
    // SAFETY: The file is opened read-only and we only read from the mmap.
    // External modifications could cause undefined behavior, but this is
    // acceptable for grep operations (same approach as ripgrep).
    let mmap = unsafe { Mmap::map(&file).ok()? };

    // Try to decode as UTF-8 using SIMD-accelerated validation
    let content_str = simd_from_utf8(&mmap).ok()?;

    let mut results = Vec::new();

    // Create search mode based on pattern type
    if is_literal {
        if ignore_case {
            // Case-insensitive literal search
            let finder = memmem::Finder::new(pattern_lower.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_lower = line.to_lowercase();
                if let Some(start) = finder.find(line_lower.as_bytes()) {
                    let end = start + pattern_lower.len();
                    // Extract match from original line (preserving case)
                    let match_text = line
                        .chars()
                        .skip(line[..start].chars().count())
                        .take(end - start)
                        .collect::<String>();

                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        } else {
            // Case-sensitive literal search (SIMD-accelerated)
            let finder = memmem::Finder::new(pattern.as_bytes());

            for (line_num, line) in content_str.lines().enumerate() {
                if results.len() >= max_results {
                    break;
                }

                let line_bytes = line.as_bytes();
                if let Some(start) = finder.find(line_bytes) {
                    let end = start + pattern.len();
                    let match_text = simd_from_utf8(&line_bytes[start..end])
                        .unwrap_or("")
                        .to_string();

                    results.push(GrepMatch {
                        file: file_path.to_string(),
                        line: line_num + 1,
                        content: line.to_string(),
                        match_text,
                    });
                }
            }
        }
    } else if let Some(regex) = regex_opt {
        // Regex search
        for (line_num, line) in content_str.lines().enumerate() {
            if results.len() >= max_results {
                break;
            }

            let line_bytes = line.as_bytes();
            if let Some(m) = regex.find(line_bytes) {
                let match_text = simd_from_utf8(&line_bytes[m.start()..m.end()])
                    .unwrap_or("")
                    .to_string();

                results.push(GrepMatch {
                    file: file_path.to_string(),
                    line: line_num + 1,
                    content: line.to_string(),
                    match_text,
                });
            }
        }
    }

    Some(results)
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

    // Parse namespace configs (with LRU caching - Issue #861)
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        // Compute hash for cache key
        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        // Try cache, otherwise parse and cache
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
fn list_objects_for_subject<'py>(
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

    // Parse namespace configs (with LRU caching - Issue #861)
    let mut namespaces = AHashMap::new();
    for (key, value) in namespace_configs.iter() {
        let obj_type: String = key.extract()?;
        let config_dict: Bound<'_, PyDict> = value.extract()?;
        let json_module = py.import("json")?;
        let config_json_py = json_module.call_method1("dumps", (config_dict,))?;
        let config_json: String = config_json_py.extract()?;

        // Compute hash for cache key
        let mut hasher = DefaultHasher::new();
        obj_type.hash(&mut hasher);
        config_json.hash(&mut hasher);
        let cache_key = hasher.finish();

        // Try cache, otherwise parse and cache
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

/// Read a file using memory-mapped I/O for zero-copy performance
///
/// Uses mmap to map the file directly into memory, avoiding the overhead of
/// copying file contents to a separate buffer. The OS page cache handles
/// efficient caching automatically.
///
/// Args:
///     path: Absolute path to the file to read
///
/// Returns:
///     File contents as bytes, or None if the file doesn't exist
///
/// Performance:
///     - Small files (<1MB): ~5% faster than read_bytes()
///     - Medium files (1-100MB): 20-40% faster
///     - Large files (>100MB): 50-70% faster
///     - Benefits from OS page cache for repeated reads
#[pyfunction]
fn read_file(py: Python<'_>, path: &str) -> PyResult<Option<Py<PyBytes>>> {
    // Check if file exists first
    let file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => {
            return Err(pyo3::exceptions::PyIOError::new_err(format!(
                "Failed to open file '{}': {}",
                path, e
            )))
        }
    };

    // Get file size - if empty, return empty bytes
    let metadata = file.metadata().map_err(|e| {
        pyo3::exceptions::PyIOError::new_err(format!("Failed to get file metadata: {}", e))
    })?;

    if metadata.len() == 0 {
        return Ok(Some(PyBytes::new(py, &[]).into()));
    }

    // Memory-map the file
    // SAFETY: The file is opened read-only and we don't modify it.
    // The mmap is valid for the lifetime of this function call.
    let mmap = unsafe {
        Mmap::map(&file).map_err(|e| {
            pyo3::exceptions::PyIOError::new_err(format!("Failed to mmap file '{}': {}", path, e))
        })?
    };

    // Create PyBytes from mmap data
    // This copies the data into Python's memory, but the mmap read is still
    // faster than read_bytes() because mmap uses the OS page cache efficiently
    Ok(Some(PyBytes::new(py, &mmap).into()))
}

/// Read multiple files using memory-mapped I/O in parallel
///
/// Uses rayon for parallel file reading when there are many files.
/// Falls back to sequential reading for small numbers of files.
///
/// Args:
///     paths: List of absolute paths to read
///
/// Returns:
///     Dict mapping path -> bytes for files that exist (missing files omitted)
#[pyfunction]
fn read_files_bulk(py: Python<'_>, paths: Vec<String>) -> PyResult<Bound<'_, PyDict>> {
    const PARALLEL_THRESHOLD: usize = 10;

    // Read files (parallel for large batches, sequential for small)
    let results: Vec<(String, Vec<u8>)> = if paths.len() < PARALLEL_THRESHOLD {
        // Sequential for small batches
        paths
            .into_iter()
            .filter_map(|path| {
                let file = File::open(&path).ok()?;
                let metadata = file.metadata().ok()?;

                if metadata.len() == 0 {
                    return Some((path, Vec::new()));
                }

                let mmap = unsafe { Mmap::map(&file).ok()? };
                Some((path, mmap.to_vec()))
            })
            .collect()
    } else {
        // Parallel for large batches - release GIL
        py.detach(|| {
            paths
                .into_par_iter()
                .filter_map(|path| {
                    let file = File::open(&path).ok()?;
                    let metadata = file.metadata().ok()?;

                    if metadata.len() == 0 {
                        return Some((path, Vec::new()));
                    }

                    let mmap = unsafe { Mmap::map(&file).ok()? };
                    Some((path, mmap.to_vec()))
                })
                .collect()
        })
    };

    // Convert to Python dict
    let py_dict = PyDict::new(py);
    for (path, content) in results {
        py_dict.set_item(path, PyBytes::new(py, &content))?;
    }

    Ok(py_dict)
}

/// Bloom filter for fast cache miss detection
///
/// A probabilistic data structure that can quickly determine if an element
/// is definitely NOT in a set, avoiding expensive disk I/O for cache misses.
///
/// Properties:
/// - False positives possible (says "maybe exists" when it doesn't)
/// - False negatives impossible (never says "doesn't exist" when it does)
/// - O(1) lookup time regardless of set size
/// - Memory efficient: ~1.2 bytes per item at 1% false positive rate
///
/// Usage:
/// ```python
/// from nexus_fast import BloomFilter
///
/// # Create filter for 100k items with 1% false positive rate
/// bloom = BloomFilter(100000, 0.01)
///
/// # Add items
/// bloom.add("tenant1:/path/to/file.txt")
///
/// # Check existence (fast path)
/// if not bloom.might_exist("tenant1:/path/to/file.txt"):
///     return None  # Definitely not in cache, skip disk I/O
/// # else: might exist, check disk
/// ```
#[pyclass]
pub struct BloomFilter {
    bloom: RwLock<Bloom<String>>,
    capacity: usize,
    fp_rate: f64,
}

#[pymethods]
impl BloomFilter {
    /// Create a new Bloom filter
    ///
    /// Args:
    ///     expected_items: Expected number of items to store (default: 100000)
    ///     fp_rate: Target false positive rate (default: 0.01 = 1%)
    ///
    /// Memory usage: ~1.2 bytes per item at 1% FP rate
    /// Example: 100k items = ~120KB, 1M items = ~1.2MB
    #[new]
    #[pyo3(signature = (expected_items=100000, fp_rate=0.01))]
    fn new(expected_items: usize, fp_rate: f64) -> PyResult<Self> {
        let bloom = Bloom::new_for_fp_rate(expected_items, fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to create Bloom filter: {}", e))
        })?;
        Ok(Self {
            bloom: RwLock::new(bloom),
            capacity: expected_items,
            fp_rate,
        })
    }

    /// Add a key to the Bloom filter
    ///
    /// Args:
    ///     key: String key to add (e.g., "tenant_id:virtual_path" or "content_hash")
    fn add(&self, key: &str) {
        self.bloom.write().unwrap().set(&key.to_string());
    }

    /// Add multiple keys to the Bloom filter in bulk
    ///
    /// More efficient than calling add() repeatedly due to reduced lock overhead.
    ///
    /// Args:
    ///     keys: List of string keys to add
    fn add_bulk(&self, keys: Vec<String>) {
        let mut bloom = self.bloom.write().unwrap();
        for key in keys {
            bloom.set(&key);
        }
    }

    /// Check if a key might exist in the filter
    ///
    /// Returns:
    ///     False: Key definitely does NOT exist (skip disk I/O)
    ///     True: Key MIGHT exist (need to check disk to confirm)
    ///
    /// Note: False positives are possible but false negatives are not.
    fn might_exist(&self, key: &str) -> bool {
        self.bloom.read().unwrap().check(&key.to_string())
    }

    /// Check multiple keys in bulk
    ///
    /// More efficient than calling might_exist() repeatedly.
    ///
    /// Args:
    ///     keys: List of string keys to check
    ///
    /// Returns:
    ///     List of booleans indicating if each key might exist
    fn check_bulk(&self, keys: Vec<String>) -> Vec<bool> {
        let bloom = self.bloom.read().unwrap();
        keys.iter().map(|k| bloom.check(k)).collect()
    }

    /// Clear all entries from the Bloom filter
    ///
    /// Resets to empty state with same capacity and false positive rate.
    /// Useful when rebuilding the filter from scratch.
    fn clear(&self) -> PyResult<()> {
        let new_bloom = Bloom::new_for_fp_rate(self.capacity, self.fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to clear Bloom filter: {}", e))
        })?;
        *self.bloom.write().unwrap() = new_bloom;
        Ok(())
    }

    /// Get the capacity (expected items) of this filter
    #[getter]
    fn capacity(&self) -> usize {
        self.capacity
    }

    /// Get the target false positive rate
    #[getter]
    fn fp_rate(&self) -> f64 {
        self.fp_rate
    }

    /// Get approximate memory usage in bytes
    #[getter]
    fn memory_bytes(&self) -> usize {
        // Bloom filter uses ~1.44 * n * ln(1/p) / ln(2) bits
        // Simplified: ~10 bits per item at 1% FP rate = 1.25 bytes per item
        let bits_per_item = (-1.44 * (self.fp_rate).ln() / (2.0_f64).ln()) as usize;
        (self.capacity * bits_per_item).div_ceil(8)
    }
}

/// Metadata for a cached file entry (L1 cache)
///
/// Stores only metadata (~100 bytes per entry) instead of full content.
/// Content is read via mmap from disk when needed.
#[derive(Clone, Debug)]
struct CacheMetadata {
    /// Path ID from database (foreign key to file_paths table)
    path_id: String,
    /// BLAKE3 content hash for ETag support
    content_hash: String,
    /// Path to cached content on disk (for mmap access)
    disk_path: PathBuf,
    /// Original file size in bytes
    original_size: u64,
    /// When this entry was synced (Unix timestamp in seconds)
    synced_at: u64,
    /// TTL in seconds (0 = no expiration, use version check)
    ttl_seconds: u32,
    /// Whether content is text (true) or binary (false)
    is_text: bool,
    /// Tenant ID for multi-tenant isolation
    #[allow(dead_code)]
    tenant_id: String,
}

/// L1 Metadata Cache - Lock-free in-memory cache for connector content metadata
///
/// This cache stores only metadata (~100 bytes per entry) instead of full content.
/// Content is accessed via mmap from disk (using OS page cache efficiently).
///
/// Key features:
/// - Lock-free concurrent access via DashMap
/// - TTL-based expiration (no backend version checks needed)
/// - Memory efficient: O(100 bytes) per entry instead of O(content size)
/// - Zero-copy content access via mmap
///
/// Performance:
/// - Lookup: <1μs (vs ~100μs for Python pickle-based L1)
/// - Concurrent access: No blocking (vs Python threading.Lock)
/// - Memory: ~100 bytes per entry (vs megabytes for content)
///
/// Usage:
/// ```python
/// from nexus_fast import L1MetadataCache
///
/// cache = L1MetadataCache(max_entries=100000, default_ttl=300)
///
/// # Store metadata (after writing content to disk)
/// cache.put(
///     key="/mnt/gcs/data/file.txt",
///     path_id="uuid-123",
///     content_hash="abc123...",
///     disk_path="/app/data/.cache/tenant/ab/c1/abc123.bin",
///     original_size=1024,
///     ttl_seconds=300,
///     is_text=True,
///     tenant_id="tenant-1",
/// )
///
/// # Get metadata (fast, lock-free)
/// metadata = cache.get("/mnt/gcs/data/file.txt")
/// if metadata:
///     path_id, content_hash, disk_path, original_size, is_fresh = metadata
///     if is_fresh:
///         content = read_file(disk_path)  # mmap-based read
/// ```
#[pyclass]
pub struct L1MetadataCache {
    /// Lock-free concurrent hashmap: key -> CacheMetadata
    cache: DashMap<String, CacheMetadata>,
    /// Maximum number of entries (for LRU-style eviction)
    max_entries: usize,
    /// Default TTL in seconds (0 = no expiration)
    default_ttl: u32,
    /// Statistics: total hits
    hits: AtomicU64,
    /// Statistics: total misses
    misses: AtomicU64,
}

#[pymethods]
impl L1MetadataCache {
    /// Create a new L1 metadata cache
    ///
    /// Args:
    ///     max_entries: Maximum number of entries (default: 100000)
    ///     default_ttl: Default TTL in seconds (default: 300 = 5 minutes)
    #[new]
    #[pyo3(signature = (max_entries=100000, default_ttl=300))]
    fn new(max_entries: usize, default_ttl: u32) -> Self {
        Self {
            cache: DashMap::with_capacity(max_entries),
            max_entries,
            default_ttl,
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    /// Store metadata for a cache entry
    ///
    /// Args:
    ///     key: Cache key (typically virtual_path like "/mnt/gcs/file.txt")
    ///     path_id: Database path_id (UUID from file_paths table)
    ///     content_hash: BLAKE3 hash of content (for ETag)
    ///     disk_path: Absolute path to cached content on disk
    ///     original_size: Original file size in bytes
    ///     ttl_seconds: TTL in seconds (0 = use default_ttl, -1 = no expiration)
    ///     is_text: Whether content is text (true) or binary (false)
    ///     tenant_id: Tenant ID for multi-tenant isolation
    #[pyo3(signature = (key, path_id, content_hash, disk_path, original_size, ttl_seconds=0, is_text=true, tenant_id="default"))]
    #[allow(clippy::too_many_arguments)]
    fn put(
        &self,
        key: &str,
        path_id: &str,
        content_hash: &str,
        disk_path: &str,
        original_size: u64,
        ttl_seconds: i32,
        is_text: bool,
        tenant_id: &str,
    ) {
        // Evict random entries if at capacity (simple eviction strategy)
        // DashMap doesn't support ordered eviction, so we do random eviction
        if self.cache.len() >= self.max_entries {
            // Remove ~10% of entries to make room
            let to_remove = self.max_entries / 10;
            let keys_to_remove: Vec<String> = self
                .cache
                .iter()
                .take(to_remove)
                .map(|entry| entry.key().clone())
                .collect();
            for k in keys_to_remove {
                self.cache.remove(&k);
            }
        }

        let ttl = match ttl_seconds.cmp(&0) {
            std::cmp::Ordering::Equal => self.default_ttl,
            std::cmp::Ordering::Less => 0, // No expiration
            std::cmp::Ordering::Greater => ttl_seconds as u32,
        };

        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let metadata = CacheMetadata {
            path_id: path_id.to_string(),
            content_hash: content_hash.to_string(),
            disk_path: PathBuf::from(disk_path),
            original_size,
            synced_at: now,
            ttl_seconds: ttl,
            is_text,
            tenant_id: tenant_id.to_string(),
        };

        self.cache.insert(key.to_string(), metadata);
    }

    /// Get metadata for a cache entry
    ///
    /// Returns a tuple of (path_id, content_hash, disk_path, original_size, is_text, is_fresh)
    /// or None if not found.
    ///
    /// The is_fresh field indicates whether the entry has not expired (TTL check).
    /// If is_fresh is False, the caller should refresh the entry from L2/backend.
    ///
    /// Args:
    ///     key: Cache key (typically virtual_path)
    ///
    /// Returns:
    ///     Tuple of (path_id, content_hash, disk_path, original_size, is_text, is_fresh) or None
    fn get(&self, key: &str) -> Option<(String, String, String, u64, bool, bool)> {
        match self.cache.get(key) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);

                let metadata = entry.value();
                let is_fresh = if metadata.ttl_seconds == 0 {
                    true // No expiration
                } else {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(metadata.synced_at);
                    age < metadata.ttl_seconds as u64
                };

                Some((
                    metadata.path_id.clone(),
                    metadata.content_hash.clone(),
                    metadata.disk_path.to_string_lossy().to_string(),
                    metadata.original_size,
                    metadata.is_text,
                    is_fresh,
                ))
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    /// Get metadata and read content via mmap in one operation
    ///
    /// This combines get() + mmap read for convenience.
    /// Returns None if not found or expired.
    ///
    /// Args:
    ///     key: Cache key
    ///     py: Python interpreter (for creating PyBytes)
    ///
    /// Returns:
    ///     Tuple of (content_bytes, content_hash, is_text) or None
    fn get_content(
        &self,
        py: Python<'_>,
        key: &str,
    ) -> PyResult<Option<(Py<PyBytes>, String, bool)>> {
        let metadata = match self.cache.get(key) {
            Some(entry) => {
                let m = entry.value();
                // Check TTL
                if m.ttl_seconds > 0 {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(m.synced_at);
                    if age >= m.ttl_seconds as u64 {
                        self.misses.fetch_add(1, Ordering::Relaxed);
                        return Ok(None); // Expired
                    }
                }
                self.hits.fetch_add(1, Ordering::Relaxed);
                (m.disk_path.clone(), m.content_hash.clone(), m.is_text)
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            }
        };

        let (disk_path, content_hash, is_text) = metadata;

        // Read content via mmap
        let file = match File::open(&disk_path) {
            Ok(f) => f,
            Err(_) => return Ok(None), // File doesn't exist
        };

        let file_metadata = match file.metadata() {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        if file_metadata.len() == 0 {
            return Ok(Some((PyBytes::new(py, &[]).into(), content_hash, is_text)));
        }

        let mmap = match unsafe { Mmap::map(&file) } {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        Ok(Some((
            PyBytes::new(py, &mmap).into(),
            content_hash,
            is_text,
        )))
    }

    /// Remove an entry from the cache
    ///
    /// Args:
    ///     key: Cache key to remove
    ///
    /// Returns:
    ///     True if entry was removed, False if not found
    fn remove(&self, key: &str) -> bool {
        self.cache.remove(key).is_some()
    }

    /// Remove all entries matching a prefix
    ///
    /// Useful for invalidating entire directories or mounts.
    ///
    /// Args:
    ///     prefix: Key prefix to match (e.g., "/mnt/gcs/")
    ///
    /// Returns:
    ///     Number of entries removed
    fn remove_prefix(&self, prefix: &str) -> usize {
        let keys_to_remove: Vec<String> = self
            .cache
            .iter()
            .filter(|entry| entry.key().starts_with(prefix))
            .map(|entry| entry.key().clone())
            .collect();

        let count = keys_to_remove.len();
        for key in keys_to_remove {
            self.cache.remove(&key);
        }
        count
    }

    /// Clear all entries from the cache
    fn clear(&self) {
        self.cache.clear();
        self.hits.store(0, Ordering::Relaxed);
        self.misses.store(0, Ordering::Relaxed);
    }

    /// Get cache statistics
    ///
    /// Returns:
    ///     Dict with entries, hits, misses, hit_rate, max_entries, default_ttl
    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let hits = self.hits.load(Ordering::Relaxed);
        let misses = self.misses.load(Ordering::Relaxed);
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };

        let dict = PyDict::new(py);
        dict.set_item("entries", self.cache.len())?;
        dict.set_item("hits", hits)?;
        dict.set_item("misses", misses)?;
        dict.set_item("hit_rate", hit_rate)?;
        dict.set_item("max_entries", self.max_entries)?;
        dict.set_item("default_ttl", self.default_ttl)?;
        Ok(dict.into())
    }

    /// Get number of entries in the cache
    #[getter]
    fn len(&self) -> usize {
        self.cache.len()
    }

    /// Check if cache is empty
    fn is_empty(&self) -> bool {
        self.cache.is_empty()
    }

    /// Get approximate memory usage in bytes
    ///
    /// Estimates ~150 bytes per entry (key + metadata struct overhead)
    #[getter]
    fn memory_bytes(&self) -> usize {
        // Approximate: 100 bytes per entry + DashMap overhead
        self.cache.len() * 150
    }
}

// =============================================================================
// Tiger Cache Roaring Bitmap Integration (Issue #896)
// =============================================================================

/// Filter path IDs using a pre-materialized Tiger Cache bitmap.
///
/// This provides O(1) permission filtering by using Roaring Bitmap membership
/// checks instead of O(n) ReBAC graph traversal.
///
/// Args:
///     path_int_ids: List of path integer IDs to filter
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs that are present in the bitmap (i.e., accessible)
///
/// Performance:
///     - O(n) where n is the number of path_int_ids
///     - Each membership check is O(1) via bitmap.contains()
///     - Expected 100-1000x speedup vs graph traversal for large lists
#[pyfunction]
fn filter_paths_with_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    // Deserialize the Roaring Bitmap from Python's pyroaring format
    // Both pyroaring and roaring-rs use the standard RoaringFormatSpec
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    // Filter paths using O(1) bitmap membership checks
    let accessible: Vec<u32> = path_int_ids
        .into_iter()
        .filter(|&id| bitmap.contains(id))
        .collect();

    Ok(accessible)
}

/// Filter path IDs using a Tiger Cache bitmap with parallel processing.
///
/// Uses rayon for parallel filtering on large path lists (>1000 paths).
///
/// Args:
///     path_int_ids: List of path integer IDs to filter
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs that are present in the bitmap (i.e., accessible)
#[pyfunction]
fn filter_paths_with_tiger_cache_parallel(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    // Use parallel iterator for large lists
    const PARALLEL_THRESHOLD: usize = 1000;

    let accessible: Vec<u32> = if path_int_ids.len() > PARALLEL_THRESHOLD {
        path_int_ids
            .into_par_iter()
            .filter(|&id| bitmap.contains(id))
            .collect()
    } else {
        path_int_ids
            .into_iter()
            .filter(|&id| bitmap.contains(id))
            .collect()
    };

    Ok(accessible)
}

/// Compute the intersection of path IDs with a Tiger Cache bitmap.
///
/// More efficient than filter when the bitmap is smaller than the path list,
/// as it iterates over the bitmap instead of the path list.
///
/// Args:
///     path_int_ids: Set of path integer IDs to intersect
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs present in both the input set and the bitmap
#[pyfunction]
fn intersect_paths_with_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    // Create a bitmap from the input path IDs for set intersection
    let input_bitmap: RoaringBitmap = path_int_ids.into_iter().collect();

    // Perform bitmap intersection (very efficient for Roaring Bitmaps)
    let result = input_bitmap & bitmap;

    Ok(result.iter().collect())
}

/// Check if any path IDs are accessible via Tiger Cache bitmap.
///
/// Fast early-exit check - useful for permission gates.
///
/// Args:
///     path_int_ids: List of path integer IDs to check
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     True if at least one path ID is in the bitmap
#[pyfunction]
fn any_path_accessible_tiger_cache(path_int_ids: Vec<u32>, bitmap_bytes: &[u8]) -> PyResult<bool> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    // Early exit on first match
    Ok(path_int_ids.iter().any(|&id| bitmap.contains(id)))
}

/// Get statistics about a Tiger Cache bitmap.
///
/// Args:
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     Dict with cardinality, serialized_bytes, is_empty
#[pyfunction]
fn tiger_cache_bitmap_stats(py: Python<'_>, bitmap_bytes: &[u8]) -> PyResult<Py<PyAny>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let dict = PyDict::new(py);
    dict.set_item("cardinality", bitmap.len())?;
    dict.set_item("serialized_bytes", bitmap_bytes.len())?;
    dict.set_item("is_empty", bitmap.is_empty())?;
    Ok(dict.into())
}

// =============================================================================
// SIMD-Accelerated Vector Similarity (Issue #952)
// =============================================================================

/// Compute cosine similarity between two f32 vectors using SIMD.
///
/// Uses SimSIMD for 100x speedup over naive implementation.
/// ~10ns per 1536-dim vector comparison vs ~1μs naive.
///
/// Args:
///     a: First vector
///     b: Second vector
///
/// Returns:
///     Cosine similarity (1.0 = identical, 0.0 = orthogonal, -1.0 = opposite)
#[pyfunction]
fn cosine_similarity_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    // SimSIMD returns cosine distance (1 - similarity), so we convert
    // Use explicit trait syntax to avoid conflict with std::f32::cos
    <f32 as SpatialSimilarity>::cos(&a, &b)
        .map(|dist| 1.0 - dist)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD cosine computation failed"))
}

/// Compute dot product between two f32 vectors using SIMD.
///
/// Uses SimSIMD for 100x speedup over naive implementation.
///
/// Args:
///     a: First vector
///     b: Second vector
///
/// Returns:
///     Dot product value
#[pyfunction]
fn dot_product_f32(a: Vec<f32>, b: Vec<f32>) -> PyResult<f64> {
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
///
/// Uses SimSIMD for 100x speedup over naive implementation.
///
/// Args:
///     a: First vector
///     b: Second vector
///
/// Returns:
///     Squared Euclidean distance (L2²)
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
///
/// Uses SimSIMD + Rayon for parallel SIMD computation.
/// Expected 100x speedup: 10ms for 10K vectors → 100μs.
///
/// Args:
///     query: Query vector (f32)
///     vectors: List of vectors to compare against (f32)
///
/// Returns:
///     List of cosine similarities (same order as input vectors)
#[pyfunction]
fn batch_cosine_similarity_f32(query: Vec<f32>, vectors: Vec<Vec<f32>>) -> PyResult<Vec<f64>> {
    if vectors.is_empty() {
        return Ok(vec![]);
    }

    // Validate dimensions
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

    // Use parallel iteration for large batches
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

/// Top-K similarity search using SIMD.
///
/// Finds the K most similar vectors to the query.
/// Uses parallel SIMD scoring + efficient top-K selection.
///
/// Args:
///     query: Query vector (f32)
///     vectors: List of vectors to search (f32)
///     k: Number of top results to return
///
/// Returns:
///     List of (index, similarity) tuples, sorted by similarity descending
#[pyfunction]
fn top_k_similar_f32(
    query: Vec<f32>,
    vectors: Vec<Vec<f32>>,
    k: usize,
) -> PyResult<Vec<(usize, f64)>> {
    if vectors.is_empty() || k == 0 {
        return Ok(vec![]);
    }

    // Validate dimensions
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

    // Compute all similarities in parallel
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

    // Sort by similarity descending
    scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    // Truncate to top-K
    scores.truncate(k);

    Ok(scores)
}

/// Cosine similarity for int8 quantized vectors using SIMD.
///
/// 166x faster than naive + 4x smaller memory footprint.
/// Use for quantized embeddings to reduce memory and increase throughput.
///
/// Args:
///     a: First vector (i8)
///     b: Second vector (i8)
///
/// Returns:
///     Cosine similarity
#[pyfunction]
fn cosine_similarity_i8(a: Vec<i8>, b: Vec<i8>) -> PyResult<f64> {
    if a.len() != b.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Vector length mismatch: {} vs {}",
            a.len(),
            b.len()
        )));
    }

    // SimSIMD returns cosine distance, convert to similarity
    <i8 as SpatialSimilarity>::cos(&a, &b)
        .map(|dist| 1.0 - dist)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("SIMD i8 cosine computation failed"))
}

/// Batch cosine similarity for int8 quantized vectors.
///
/// Args:
///     query: Query vector (i8)
///     vectors: List of vectors to compare against (i8)
///
/// Returns:
///     List of cosine similarities
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
///
/// Args:
///     query: Query vector (i8)
///     vectors: List of vectors to search (i8)
///     k: Number of top results to return
///
/// Returns:
///     List of (index, similarity) tuples, sorted by similarity descending
#[pyfunction]
fn top_k_similar_i8(
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

// =============================================================================
// BLAKE3 Hashing for Content-Addressable Storage (Issue #1395)
// =============================================================================

/// Compute BLAKE3 hash of content (full hash).
///
/// BLAKE3 is ~3x faster than SHA-256 and uses SIMD acceleration.
/// Returns 64-character hex string (256-bit hash).
#[pyfunction]
fn hash_content(content: &[u8]) -> String {
    blake3::hash(content).to_hex().to_string()
}

/// Compute BLAKE3 hash with strategic sampling for large files.
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
        blake3::hash(content).to_hex().to_string()
    } else {
        let mut hasher = blake3::Hasher::new();

        // First 64KB
        hasher.update(&content[..SAMPLE_SIZE]);

        // Middle 64KB
        let mid_start = content.len() / 2 - SAMPLE_SIZE / 2;
        hasher.update(&content[mid_start..mid_start + SAMPLE_SIZE]);

        // Last 64KB
        hasher.update(&content[content.len() - SAMPLE_SIZE..]);

        // Include file size to differentiate files with same samples.
        // Cast to u64 for cross-platform consistency with Python's
        // len(content).to_bytes(8, byteorder="little").
        hasher.update(&(content.len() as u64).to_le_bytes());

        hasher.finalize().to_hex().to_string()
    }
}

/// Python module definition
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(list_objects_for_subject, m)?)?;
    m.add_function(wrap_pyfunction!(grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(grep_files_mmap, m)?)?;
    m.add_function(wrap_pyfunction!(glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(filter_paths, m)?)?;
    m.add_function(wrap_pyfunction!(read_file, m)?)?;
    m.add_function(wrap_pyfunction!(read_files_bulk, m)?)?;
    // Tiger Cache Roaring Bitmap functions (Issue #896)
    m.add_function(wrap_pyfunction!(filter_paths_with_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(filter_paths_with_tiger_cache_parallel, m)?)?;
    m.add_function(wrap_pyfunction!(intersect_paths_with_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(any_path_accessible_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(tiger_cache_bitmap_stats, m)?)?;
    // SIMD-accelerated vector similarity functions (Issue #952)
    m.add_function(wrap_pyfunction!(cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(dot_product_f32, m)?)?;
    m.add_function(wrap_pyfunction!(euclidean_sq_f32, m)?)?;
    m.add_function(wrap_pyfunction!(batch_cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(top_k_similar_f32, m)?)?;
    m.add_function(wrap_pyfunction!(cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(batch_cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(top_k_similar_i8, m)?)?;
    // BLAKE3 hashing for content-addressable storage (Issue #1395)
    m.add_function(wrap_pyfunction!(hash_content, m)?)?;
    m.add_function(wrap_pyfunction!(hash_content_smart, m)?)?;
    m.add_class::<BloomFilter>()?;
    m.add_class::<L1MetadataCache>()?;
    Ok(())
}

//! Interned graph — zero-allocation ReBAC with string interning.

use ahash::{AHashMap, AHashSet};
use string_interner::DefaultStringInterner;

use crate::types::*;

/// Graph with interned symbols for fast lookups.
#[derive(Debug, Clone)]
pub struct InternedGraph {
    pub tuple_index: AHashSet<InternedTupleKey>,
    pub adjacency_list: AHashMap<InternedAdjacencyKey, Vec<InternedEntity>>,
    pub userset_index: AHashMap<InternedUsersetKey, Vec<InternedUsersetEntry>>,
    /// Wildcard subject (*:*) symbol.
    pub wildcard_subject: Option<InternedEntity>,
}

impl InternedGraph {
    /// Build from interned tuples.
    pub fn from_tuples(tuples: &[InternedTuple], interner: &mut DefaultStringInterner) -> Self {
        let wildcard_type = interner.get_or_intern("*");
        let wildcard_id = interner.get_or_intern("*");
        let wildcard_subject = Some(InternedEntity {
            entity_type: wildcard_type,
            entity_id: wildcard_id,
        });

        let mut tuple_index = AHashSet::new();
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
                tuple_index.insert(tuple_key);
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

    /// Check for direct relation in O(1) time.
    pub fn check_direct_relation(
        &self,
        subject: InternedEntity,
        relation: Sym,
        object: InternedEntity,
    ) -> bool {
        let tuple_key = (
            object.entity_type,
            object.entity_id,
            relation,
            subject.entity_type,
            subject.entity_id,
        );
        if self.tuple_index.contains(&tuple_key) {
            return true;
        }

        // Wildcard subject match (*:*)
        if let Some(wildcard) = &self.wildcard_subject {
            let wildcard_key = (
                object.entity_type,
                object.entity_id,
                relation,
                wildcard.entity_type,
                wildcard.entity_id,
            );
            if self.tuple_index.contains(&wildcard_key) {
                return true;
            }
        }

        false
    }

    /// Find related objects via adjacency list.
    pub fn find_related_objects(
        &self,
        object: InternedEntity,
        relation: Sym,
    ) -> Vec<InternedEntity> {
        let adj_key = (object.entity_type, object.entity_id, relation);
        self.adjacency_list
            .get(&adj_key)
            .cloned()
            .unwrap_or_default()
    }

    /// Get usersets that grant a relation on an object.
    pub fn get_usersets(&self, object: InternedEntity, relation: Sym) -> &[InternedUsersetEntry] {
        let userset_key = (object.entity_type, object.entity_id, relation);
        self.userset_index
            .get(&userset_key)
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }
}

/// Compute permission with interned types — O(1) key operations.
#[allow(clippy::too_many_arguments)]
pub fn compute_permission_interned(
    subject: InternedEntity,
    permission: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &mut InternedMemoCache,
    visited: &mut InternedVisitedSet,
    depth: u32,
) -> bool {
    use super::MAX_DEPTH;

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

    if let Some(&result) = memo_cache.get(&memo_key) {
        return result;
    }

    if visited.contains(&memo_key) {
        return false;
    }
    visited.insert(memo_key);

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

/// Check relation with interned types — no allocations.
#[allow(clippy::too_many_arguments)]
pub fn check_relation_with_usersets_interned(
    subject: InternedEntity,
    relation: Sym,
    object: InternedEntity,
    graph: &InternedGraph,
    namespaces: &AHashMap<Sym, InternedNamespaceConfig>,
    memo_cache: &mut InternedMemoCache,
    visited: &mut InternedVisitedSet,
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

//! Relationship-Based Access Control (ReBAC) engine.
//!
//! Provides permission computation using Zanzibar-style tuple-based ACLs.
//! Supports direct relations, union expansion, tupleToUserset, and wildcard subjects.

pub mod config;
pub mod graph;

use ahash::{AHashMap, AHashSet};

use crate::types::*;

/// Maximum recursion depth for permission checks.
pub const MAX_DEPTH: u32 = 50;

// ============================================================================
// String-keyed ReBAC (used by compute_permission_single / expand_subjects)
// ============================================================================

/// String-keyed ReBAC graph with O(1) lookups.
#[derive(Debug, Clone)]
pub struct ReBACGraph {
    pub tuple_index: AHashSet<TupleKey>,
    pub adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>>,
    pub userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>>,
}

impl ReBACGraph {
    /// Build graph indexes from tuples.
    pub fn from_tuples(tuples: &[ReBACTuple]) -> Self {
        let mut tuple_index = AHashSet::new();
        let mut adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();
        let mut userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>> = AHashMap::new();

        for tuple in tuples {
            if let Some(ref subject_relation) = tuple.subject_relation {
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
                let tuple_key = (
                    tuple.object_type.clone(),
                    tuple.object_id.clone(),
                    tuple.relation.clone(),
                    tuple.subject_type.clone(),
                    tuple.subject_id.clone(),
                );
                tuple_index.insert(tuple_key);
            }

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

    /// Check for direct relation in O(1) time.
    pub fn check_direct_relation(&self, subject: &Entity, relation: &str, object: &Entity) -> bool {
        let tuple_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
            subject.entity_type.clone(),
            subject.entity_id.clone(),
        );
        if self.tuple_index.contains(&tuple_key) {
            return true;
        }

        // Wildcard subject match (*:*)
        let wildcard_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
            "*".to_string(),
            "*".to_string(),
        );
        self.tuple_index.contains(&wildcard_key)
    }

    /// Find related objects in O(1) time using adjacency list.
    pub fn find_related_objects(&self, object: &Entity, relation: &str) -> Vec<Entity> {
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

    /// Get usersets that grant a relation on an object.
    pub fn get_usersets(&self, object: &Entity, relation: &str) -> &[UsersetEntry] {
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

/// Compute a single permission check with memoization (string-keyed).
#[allow(clippy::too_many_arguments)]
pub fn compute_permission(
    subject: &Entity,
    permission: &str,
    object: &Entity,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    memo_cache: &mut MemoCache,
    visited: &mut VisitedSet,
    depth: u32,
) -> bool {
    if depth > MAX_DEPTH {
        return false;
    }

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

    if visited.contains(&memo_key) {
        return false;
    }
    visited.insert(memo_key.clone());

    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            let result = check_relation_with_usersets(
                subject, permission, object, graph, namespaces, memo_cache, visited, depth,
            );
            memo_cache.insert(memo_key, result);
            return result;
        }
    };

    let result = if let Some(usersets) = namespace.permissions.get(permission) {
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
        match relation_config {
            RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                check_relation_with_usersets(
                    subject, permission, object, graph, namespaces, memo_cache, visited, depth,
                )
            }
            RelationConfig::Union { union } => {
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
                // Also check direct relations — Zanzibar: direct tuples always apply
                if !allowed {
                    allowed = check_relation_with_usersets(
                        subject, permission, object, graph, namespaces, memo_cache, visited, depth,
                    );
                }
                allowed
            }
        }
    } else {
        check_relation_with_usersets(
            subject, permission, object, graph, namespaces, memo_cache, visited, depth,
        )
    };

    memo_cache.insert(memo_key, result);
    result
}

/// Check relation with direct + userset-based permissions (string-keyed).
#[allow(clippy::too_many_arguments)]
pub fn check_relation_with_usersets(
    subject: &Entity,
    relation: &str,
    object: &Entity,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    memo_cache: &mut MemoCache,
    visited: &mut VisitedSet,
    depth: u32,
) -> bool {
    if graph.check_direct_relation(subject, relation, object) {
        return true;
    }

    for userset in graph.get_usersets(object, relation) {
        let userset_entity = Entity {
            entity_type: userset.subject_type.clone(),
            entity_id: userset.subject_id.clone(),
        };

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

/// Expand subjects: find all subjects with a permission on an object.
pub fn expand_permission(
    permission: &str,
    object: &Entity,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    subjects: &mut AHashSet<(String, String)>,
    visited: &mut AHashSet<(String, String, String)>,
    depth: u32,
) {
    if depth > MAX_DEPTH {
        return;
    }

    let visit_key = (
        permission.to_string(),
        object.entity_type.clone(),
        object.entity_id.clone(),
    );
    if visited.contains(&visit_key) {
        return;
    }
    visited.insert(visit_key);

    let namespace = match namespaces.get(&object.entity_type) {
        Some(ns) => ns,
        None => {
            add_direct_subjects(permission, object, graph, subjects);
            return;
        }
    };

    if let Some(usersets) = namespace.permissions.get(permission) {
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

    if let Some(relation_config) = namespace.relations.get(permission) {
        match relation_config {
            RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                add_direct_subjects(permission, object, graph, subjects);
            }
            RelationConfig::Union { union } => {
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

    add_direct_subjects(permission, object, graph, subjects);
}

/// Add all direct subjects that have a relation on an object.
fn add_direct_subjects(
    relation: &str,
    object: &Entity,
    graph: &ReBACGraph,
    subjects: &mut AHashSet<(String, String)>,
) {
    for key in graph.tuple_index.iter() {
        let (obj_type, obj_id, rel, subj_type, subj_id) = key;
        if obj_type == &object.entity_type && obj_id == &object.entity_id && rel == relation {
            subjects.insert((subj_type.clone(), subj_id.clone()));
        }
    }

    for userset in graph.get_usersets(object, relation) {
        subjects.insert((
            format!("{}#{}", userset.subject_type, userset.subject_relation),
            userset.subject_id.clone(),
        ));
    }
}

/// Get all relations that can grant a permission.
pub fn get_permission_relations(
    permission: &str,
    object_type: &str,
    namespaces: &AHashMap<String, NamespaceConfig>,
) -> Vec<String> {
    let mut expanded: AHashSet<String> = AHashSet::new();
    let mut to_expand: Vec<String> = vec![permission.to_string()];

    if let Some(namespace) = namespaces.get(object_type) {
        if let Some(usersets) = namespace.permissions.get(permission) {
            to_expand.extend(usersets.iter().cloned());
        }

        while let Some(rel) = to_expand.pop() {
            if expanded.contains(&rel) {
                continue;
            }
            expanded.insert(rel.clone());

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

/// Find all groups that a subject belongs to.
pub fn find_subject_groups(subject: &Entity, graph: &ReBACGraph) -> Vec<Entity> {
    let mut groups = Vec::new();
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

/// Collect candidate objects a subject might access via direct relations.
pub fn collect_candidate_objects_for_subject(
    subject: &Entity,
    permission: &str,
    object_type: &str,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    candidates: &mut AHashSet<Entity>,
) {
    let relations = get_permission_relations(permission, object_type, namespaces);
    for relation in relations {
        let adj_key = (
            subject.entity_type.clone(),
            subject.entity_id.clone(),
            relation,
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

#[cfg(test)]
mod tests;

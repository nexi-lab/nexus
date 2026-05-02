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
    /// Reverse adjacency: (object_type, object_id, relation) → [subjects].
    /// Enables tupleToUserset resolution which needs "find subjects with relation on object".
    pub reverse_adjacency: AHashMap<AdjacencyKey, Vec<Entity>>,
    pub userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>>,
    /// Direct-only reverse adjacency: (object_type, object_id, relation) → [subjects].
    /// Built only from tuples WITHOUT subject_relation (direct relations).
    /// Used by add_direct_subjects() to avoid conflating userset subjects with direct ones.
    pub direct_reverse: AHashMap<AdjacencyKey, Vec<Entity>>,
}

impl ReBACGraph {
    /// Build graph indexes from tuples.
    pub fn from_tuples(tuples: &[ReBACTuple]) -> Self {
        let mut tuple_index = AHashSet::new();
        let mut adjacency_list: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();
        let mut reverse_adjacency: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();
        let mut userset_index: AHashMap<UsersetKey, Vec<UsersetEntry>> = AHashMap::new();
        let mut direct_reverse: AHashMap<AdjacencyKey, Vec<Entity>> = AHashMap::new();

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

                let direct_rev_key = (
                    tuple.object_type.clone(),
                    tuple.object_id.clone(),
                    tuple.relation.clone(),
                );
                direct_reverse
                    .entry(direct_rev_key)
                    .or_default()
                    .push(Entity {
                        entity_type: tuple.subject_type.clone(),
                        entity_id: tuple.subject_id.clone(),
                    });
            }

            // Forward adjacency: subject → objects
            let adj_key = (
                tuple.subject_type.clone(),
                tuple.subject_id.clone(),
                tuple.relation.clone(),
            );
            adjacency_list.entry(adj_key).or_default().push(Entity {
                entity_type: tuple.object_type.clone(),
                entity_id: tuple.object_id.clone(),
            });

            // Reverse adjacency: object → subjects
            let rev_key = (
                tuple.object_type.clone(),
                tuple.object_id.clone(),
                tuple.relation.clone(),
            );
            reverse_adjacency.entry(rev_key).or_default().push(Entity {
                entity_type: tuple.subject_type.clone(),
                entity_id: tuple.subject_id.clone(),
            });
        }

        ReBACGraph {
            tuple_index,
            adjacency_list,
            reverse_adjacency,
            userset_index,
            direct_reverse,
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

    /// Find objects that a subject has a relation on (forward: subject → objects).
    pub fn find_related_objects(&self, subject: &Entity, relation: &str) -> Vec<Entity> {
        let adj_key = (
            subject.entity_type.clone(),
            subject.entity_id.clone(),
            relation.to_string(),
        );
        self.adjacency_list
            .get(&adj_key)
            .cloned()
            .unwrap_or_default()
    }

    /// Find subjects that have a relation on an object (reverse: object → subjects).
    /// Required for tupleToUserset: "find who has `tupleset` relation on this object".
    pub fn find_subjects_for_object(&self, object: &Entity, relation: &str) -> Vec<Entity> {
        let rev_key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
        );
        self.reverse_adjacency
            .get(&rev_key)
            .cloned()
            .unwrap_or_default()
    }

    pub fn find_direct_subjects_for_object(&self, object: &Entity, relation: &str) -> Vec<Entity> {
        let key = (
            object.entity_type.clone(),
            object.entity_id.clone(),
            relation.to_string(),
        );
        self.direct_reverse.get(&key).cloned().unwrap_or_default()
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
                visited,
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
                        visited,
                        depth + 1,
                    ) {
                        allowed = true;
                        break;
                    }
                }
                allowed
            }
            RelationConfig::TupleToUserset { tuple_to_userset } => {
                // tupleToUserset checks BOTH directions:
                //
                // Forward (parent pattern): object acts as subject with tupleset relation
                //   parent_viewer = {tupleset: "parent", computedUserset: "viewer"}
                //   file:doc → parent → folder:docs, then check viewer on folder:docs
                //
                // Reverse (group pattern): others have tupleset relation ON object
                //   group_viewer = {tupleset: "direct_viewer", computedUserset: "member"}
                //   group:team → direct_viewer → file:/path, then check member on group:team
                let mut allowed = false;

                // Forward: object as subject → find objects it points to
                let forward_targets =
                    graph.find_related_objects(object, &tuple_to_userset.tupleset);
                for target in &forward_targets {
                    if compute_permission(
                        subject,
                        &tuple_to_userset.computed_userset,
                        target,
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

                // Reverse: find subjects that have tupleset relation ON object.
                //
                // Fix nexi-lab/nexus#3733 Bug A: skip the reverse (group)
                // pattern when the tupleset relation is "parent". For a
                // parent relation, the forward pattern is the ONLY correct
                // direction — "alice is parent_owner of Y iff alice owns
                // parent(Y)". The reverse pattern finds Y's CHILDREN
                // instead and incorrectly grants parent permission based
                // on owning any child, causing a privilege escalation
                // where owning /workspace/public grants access to all
                // sibling files under /workspace/.
                //
                // The equivalent Python guards are in
                // bricks/rebac/graph/bulk_evaluator.py,
                // bricks/rebac/graph/traversal.py, and
                // bricks/rebac/graph/zone_traversal.py.
                if !allowed && tuple_to_userset.tupleset != "parent" {
                    let reverse_targets =
                        graph.find_subjects_for_object(object, &tuple_to_userset.tupleset);
                    for target in &reverse_targets {
                        if compute_permission(
                            subject,
                            &tuple_to_userset.computed_userset,
                            target,
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
            visited,
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
                visited,
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
                    expand_permission(rel, object, graph, namespaces, subjects, visited, depth + 1);
                }
            }
            RelationConfig::TupleToUserset { tuple_to_userset } => {
                // Forward: object as subject → find objects it points to
                let forward_targets =
                    graph.find_related_objects(object, &tuple_to_userset.tupleset);
                for target in &forward_targets {
                    expand_permission(
                        &tuple_to_userset.computed_userset,
                        target,
                        graph,
                        namespaces,
                        subjects,
                        visited,
                        depth + 1,
                    );
                }

                // Reverse: find subjects that have tupleset relation ON object.
                // Skip for "parent" tuplesets to match compute_permission() and
                // avoid the known Bug A privilege-escalation direction.
                if tuple_to_userset.tupleset != "parent" {
                    let reverse_targets =
                        graph.find_subjects_for_object(object, &tuple_to_userset.tupleset);
                    for target in &reverse_targets {
                        expand_permission(
                            &tuple_to_userset.computed_userset,
                            target,
                            graph,
                            namespaces,
                            subjects,
                            visited,
                            depth + 1,
                        );
                    }
                }

                // Direct tuples always apply (Zanzibar: direct fallback)
                add_direct_subjects(permission, object, graph, subjects);
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
    for entity in graph.find_direct_subjects_for_object(object, relation) {
        subjects.insert((entity.entity_type, entity.entity_id));
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

    while let Some(rel) = to_expand.pop() {
        if !expanded.insert(rel.clone()) {
            continue;
        }

        if let Some(namespace) = namespaces.get(object_type) {
            if let Some(usersets) = namespace.permissions.get(&rel) {
                for userset in usersets {
                    if !expanded.contains(userset) {
                        to_expand.push(userset.clone());
                    }
                }
            }
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

#[derive(Default)]
struct TupleToUsersetCandidateIndex {
    forward_keys_by_relation: AHashMap<String, Vec<AdjacencyKey>>,
    reverse_keys_by_relation: AHashMap<String, Vec<AdjacencyKey>>,
}

#[derive(Default)]
struct UsersetRelationCandidateIndex {
    keys_by_relation: AHashMap<String, Vec<UsersetKey>>,
}

fn build_userset_relation_candidate_index(
    object_type: &str,
    relations: &[String],
    graph: &ReBACGraph,
) -> UsersetRelationCandidateIndex {
    let relation_set: AHashSet<String> = relations.iter().cloned().collect();
    if relation_set.is_empty() {
        return UsersetRelationCandidateIndex::default();
    }

    let mut index = UsersetRelationCandidateIndex::default();
    for key in graph.userset_index.keys() {
        if key.0 == object_type && relation_set.contains(&key.2) {
            index
                .keys_by_relation
                .entry(key.2.clone())
                .or_default()
                .push(key.clone());
        }
    }

    index
}

fn build_tuple_to_userset_candidate_index(
    object_type: &str,
    relations: &[String],
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
) -> TupleToUsersetCandidateIndex {
    let mut tuplesets: AHashSet<String> = AHashSet::new();

    if let Some(namespace) = namespaces.get(object_type) {
        for relation in relations {
            if let Some(RelationConfig::TupleToUserset { tuple_to_userset }) =
                namespace.relations.get(relation)
            {
                tuplesets.insert(tuple_to_userset.tupleset.clone());
            }
        }
    }

    if tuplesets.is_empty() {
        return TupleToUsersetCandidateIndex::default();
    }

    let mut index = TupleToUsersetCandidateIndex::default();

    for key in graph.adjacency_list.keys() {
        if key.0 == object_type && tuplesets.contains(&key.2) {
            index
                .forward_keys_by_relation
                .entry(key.2.clone())
                .or_default()
                .push(key.clone());
        }
    }

    for key in graph.reverse_adjacency.keys() {
        if key.0 == object_type && tuplesets.contains(&key.2) {
            index
                .reverse_keys_by_relation
                .entry(key.2.clone())
                .or_default()
                .push(key.clone());
        }
    }

    index
}

fn add_userset_relation_candidates(
    subject: &Entity,
    relation: &str,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    index: &UsersetRelationCandidateIndex,
    memo_cache: &mut MemoCache,
    candidates: &mut AHashSet<Entity>,
) {
    let Some(keys) = index.keys_by_relation.get(relation) else {
        return;
    };

    for key in keys {
        if let Some(usersets) = graph.userset_index.get(key) {
            if usersets.iter().any(|userset| {
                let userset_entity = Entity {
                    entity_type: userset.subject_type.clone(),
                    entity_id: userset.subject_id.clone(),
                };
                compute_permission(
                    subject,
                    &userset.subject_relation,
                    &userset_entity,
                    graph,
                    namespaces,
                    memo_cache,
                    &mut AHashSet::new(),
                    0,
                )
            }) {
                candidates.insert(Entity {
                    entity_type: key.0.clone(),
                    entity_id: key.1.clone(),
                });
            }
        }
    }
}

/// Add candidates reachable through tupleToUserset relations.
///
/// This complements direct adjacency-based candidate collection and prevents
/// false negatives for inheritance/group patterns where the subject does not
/// hold the final relation directly on the candidate object.
#[allow(clippy::too_many_arguments)]
fn add_tuple_to_userset_candidates(
    subject: &Entity,
    relation: &str,
    object_type: &str,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    index: &TupleToUsersetCandidateIndex,
    memo_cache: &mut MemoCache,
    candidates: &mut AHashSet<Entity>,
) {
    let tuple_to_userset = match namespaces
        .get(object_type)
        .and_then(|ns| ns.relations.get(relation))
    {
        Some(RelationConfig::TupleToUserset { tuple_to_userset }) => tuple_to_userset,
        _ => return,
    };

    let tupleset = &tuple_to_userset.tupleset;
    let computed_userset = &tuple_to_userset.computed_userset;

    // Forward pattern: object --tupleset--> target, then subject has
    // computed_userset on target.
    if let Some(forward_keys) = index.forward_keys_by_relation.get(tupleset) {
        for key in forward_keys {
            if let Some(targets) = graph.adjacency_list.get(key) {
                if targets.iter().any(|target| {
                    compute_permission(
                        subject,
                        computed_userset,
                        target,
                        graph,
                        namespaces,
                        memo_cache,
                        &mut AHashSet::new(),
                        0,
                    )
                }) {
                    candidates.insert(Entity {
                        entity_type: key.0.clone(),
                        entity_id: key.1.clone(),
                    });
                }
            }
        }
    }

    // Reverse pattern: related_subject --tupleset--> object, then subject has
    // computed_userset on related_subject. Skip for parent tuplesets (Bug A).
    if tupleset != "parent" {
        if let Some(reverse_keys) = index.reverse_keys_by_relation.get(tupleset) {
            for key in reverse_keys {
                if let Some(related_subjects) = graph.reverse_adjacency.get(key) {
                    if related_subjects.iter().any(|related_subject| {
                        compute_permission(
                            subject,
                            computed_userset,
                            related_subject,
                            graph,
                            namespaces,
                            memo_cache,
                            &mut AHashSet::new(),
                            0,
                        )
                    }) {
                        candidates.insert(Entity {
                            entity_type: key.0.clone(),
                            entity_id: key.1.clone(),
                        });
                    }
                }
            }
        }
    }
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
    collect_candidate_objects_for_subjects(
        std::slice::from_ref(subject),
        permission,
        object_type,
        graph,
        namespaces,
        candidates,
    );
}

/// Collect candidate objects for one or more subjects with shared indexes.
pub fn collect_candidate_objects_for_subjects(
    subjects: &[Entity],
    permission: &str,
    object_type: &str,
    graph: &ReBACGraph,
    namespaces: &AHashMap<String, NamespaceConfig>,
    candidates: &mut AHashSet<Entity>,
) {
    let relations = get_permission_relations(permission, object_type, namespaces);
    let userset_relation_index =
        build_userset_relation_candidate_index(object_type, &relations, graph);
    let tuple_to_userset_index =
        build_tuple_to_userset_candidate_index(object_type, &relations, graph, namespaces);
    let mut candidate_eval_memo: MemoCache = AHashMap::new();

    for subject in subjects {
        for relation in &relations {
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

            // Include wildcard grants (*:*) for this relation.
            let wildcard_key = ("*".to_string(), "*".to_string(), relation.clone());
            if let Some(objects) = graph.adjacency_list.get(&wildcard_key) {
                for obj in objects {
                    if obj.entity_type == object_type {
                        candidates.insert(obj.clone());
                    }
                }
            }

            add_userset_relation_candidates(
                subject,
                relation,
                graph,
                namespaces,
                &userset_relation_index,
                &mut candidate_eval_memo,
                candidates,
            );

            add_tuple_to_userset_candidates(
                subject,
                relation,
                object_type,
                graph,
                namespaces,
                &tuple_to_userset_index,
                &mut candidate_eval_memo,
                candidates,
            );
        }
    }
}

#[cfg(test)]
mod tests;

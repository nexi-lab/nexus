//! Comprehensive tests for the ReBAC engine.

use ahash::{AHashMap, AHashSet};
use string_interner::DefaultStringInterner;

use crate::rebac::graph::*;
use crate::rebac::*;

// ============================================================================
// Helper builders
// ============================================================================

fn entity(t: &str, id: &str) -> Entity {
    Entity {
        entity_type: t.to_string(),
        entity_id: id.to_string(),
    }
}

fn tuple_direct(
    subj_type: &str,
    subj_id: &str,
    relation: &str,
    obj_type: &str,
    obj_id: &str,
) -> ReBACTuple {
    ReBACTuple {
        subject_type: subj_type.to_string(),
        subject_id: subj_id.to_string(),
        subject_relation: None,
        relation: relation.to_string(),
        object_type: obj_type.to_string(),
        object_id: obj_id.to_string(),
    }
}

fn tuple_userset(
    subj_type: &str,
    subj_id: &str,
    subj_relation: &str,
    relation: &str,
    obj_type: &str,
    obj_id: &str,
) -> ReBACTuple {
    ReBACTuple {
        subject_type: subj_type.to_string(),
        subject_id: subj_id.to_string(),
        subject_relation: Some(subj_relation.to_string()),
        relation: relation.to_string(),
        object_type: obj_type.to_string(),
        object_id: obj_id.to_string(),
    }
}

fn ns_config(json: &str) -> NamespaceConfig {
    serde_json::from_str(json).unwrap()
}

// ============================================================================
// Basic permission checks
// ============================================================================

#[test]
fn direct_relation_grant() {
    let tuples = vec![tuple_direct("user", "alice", "editor", "file", "readme")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();
    let mut memo = MemoCache::new();

    let result = compute_permission(
        &entity("user", "alice"),
        "editor",
        &entity("file", "readme"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn direct_relation_deny() {
    let tuples = vec![tuple_direct("user", "alice", "editor", "file", "readme")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();
    let mut memo = MemoCache::new();

    // bob has no relation
    let result = compute_permission(
        &entity("user", "bob"),
        "editor",
        &entity("file", "readme"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(!result);
}

#[test]
fn userset_permission_via_group() {
    // group:eng#member -> editor -> file:readme
    // user:alice -> member -> group:eng
    let tuples = vec![
        tuple_userset("group", "eng", "member", "editor", "file", "readme"),
        tuple_direct("user", "alice", "member", "group", "eng"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();
    let mut memo = MemoCache::new();

    let result = compute_permission(
        &entity("user", "alice"),
        "editor",
        &entity("file", "readme"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn tuple_to_userset_parent_folder() {
    // file:doc1 -> parent -> folder:docs
    // user:alice -> viewer -> folder:docs
    // file namespace: viewer uses tupleToUserset(parent, viewer)
    let tuples = vec![
        tuple_direct("file", "doc1", "parent", "folder", "docs"),
        tuple_direct("user", "alice", "viewer", "folder", "docs"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    let result = compute_permission(
        &entity("user", "alice"),
        "read",
        &entity("file", "doc1"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn union_relation_expansion() {
    // namespace: editor = union(owner, collaborator)
    // user:alice -> owner -> file:readme
    let tuples = vec![tuple_direct("user", "alice", "owner", "file", "readme")];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{"editor":{"union":["owner","collaborator"]},"owner":"direct","collaborator":"direct"},"permissions":{"write":["editor"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    // alice has write via: write -> editor -> owner (union member)
    let result = compute_permission(
        &entity("user", "alice"),
        "write",
        &entity("file", "readme"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

// ============================================================================
// Edge cases
// ============================================================================

#[test]
fn cycle_detection_at_max_depth() {
    // Create a cycle: A -> member -> B, B -> member -> A
    let tuples = vec![
        tuple_direct("group", "a", "member", "group", "b"),
        tuple_direct("group", "b", "member", "group", "a"),
    ];

    let config_json = r#"{"relations":{"member":"direct","viewer":{"union":["member"]}},"permissions":{"read":["viewer"]}}"#;
    let graph = ReBACGraph::from_tuples(&tuples);
    let mut namespaces = AHashMap::new();
    namespaces.insert("group".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    // Should return false without stack overflow
    let result = compute_permission(
        &entity("user", "charlie"),
        "read",
        &entity("group", "a"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(!result);
}

#[test]
fn wildcard_subject_grants_all() {
    // *:* -> viewer -> file:public
    let tuples = vec![tuple_direct("*", "*", "viewer", "file", "public")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();
    let mut memo = MemoCache::new();

    // Any user should have viewer
    let result = compute_permission(
        &entity("user", "anyone"),
        "viewer",
        &entity("file", "public"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn wildcard_with_userset_chain() {
    // *:* -> viewer -> file:public
    // namespace: read -> [viewer]
    let tuples = vec![tuple_direct("*", "*", "viewer", "file", "public")];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{"viewer":"direct"},"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    let result = compute_permission(
        &entity("user", "stranger"),
        "read",
        &entity("file", "public"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn empty_tuple_set_denies_all() {
    let graph = ReBACGraph::from_tuples(&[]);
    let namespaces = AHashMap::new();
    let mut memo = MemoCache::new();

    let result = compute_permission(
        &entity("user", "alice"),
        "viewer",
        &entity("file", "secret"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(!result);
}

#[test]
fn namespace_with_empty_relations() {
    let tuples = vec![tuple_direct("user", "alice", "viewer", "file", "doc")];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{},"permissions":{}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    // viewer not in relations or permissions => falls through to check_relation_with_usersets
    let result = compute_permission(
        &entity("user", "alice"),
        "viewer",
        &entity("file", "doc"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn deeply_nested_tuple_to_userset() {
    // file:doc -> parent -> folder:a
    // folder:a -> parent -> folder:b
    // folder:b -> parent -> folder:c
    // folder:c -> parent -> folder:root
    // user:alice -> viewer -> folder:root
    let tuples = vec![
        tuple_direct("file", "doc", "parent", "folder", "a"),
        tuple_direct("folder", "a", "parent", "folder", "b"),
        tuple_direct("folder", "b", "parent", "folder", "c"),
        tuple_direct("folder", "c", "parent", "folder", "root"),
        tuple_direct("user", "alice", "viewer", "folder", "root"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));
    namespaces.insert("folder".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    // alice -> viewer -> folder:root => viewer -> folder:c => ... => viewer -> file:doc
    let result = compute_permission(
        &entity("user", "alice"),
        "read",
        &entity("file", "doc"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

#[test]
fn namespace_referencing_nonexistent_relation() {
    // permissions reference "admin" but it's not in relations
    let tuples = vec![tuple_direct("user", "alice", "admin", "file", "doc")];
    let graph = ReBACGraph::from_tuples(&tuples);

    let config_json = r#"{"relations":{},"permissions":{"manage":["admin"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();

    // "manage" -> expand "admin" -> not in relations, falls to direct check -> found
    let result = compute_permission(
        &entity("user", "alice"),
        "manage",
        &entity("file", "doc"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    );
    assert!(result);
}

// ============================================================================
// Interned graph tests
// ============================================================================

#[test]
fn interned_graph_basic() {
    let mut interner = DefaultStringInterner::new();

    let tuples = vec![InternedTuple {
        subject_type: interner.get_or_intern("user"),
        subject_id: interner.get_or_intern("alice"),
        subject_relation: None,
        relation: interner.get_or_intern("editor"),
        object_type: interner.get_or_intern("file"),
        object_id: interner.get_or_intern("readme"),
    }];

    let graph = InternedGraph::from_tuples(&tuples, &mut interner);

    let subject = InternedEntity {
        entity_type: interner.get_or_intern("user"),
        entity_id: interner.get_or_intern("alice"),
    };
    let object = InternedEntity {
        entity_type: interner.get_or_intern("file"),
        entity_id: interner.get_or_intern("readme"),
    };
    let editor = interner.get_or_intern("editor");

    assert!(graph.check_direct_relation(subject, editor, object));
}

#[test]
fn interned_graph_wildcard() {
    let mut interner = DefaultStringInterner::new();

    let tuples = vec![InternedTuple {
        subject_type: interner.get_or_intern("*"),
        subject_id: interner.get_or_intern("*"),
        subject_relation: None,
        relation: interner.get_or_intern("viewer"),
        object_type: interner.get_or_intern("file"),
        object_id: interner.get_or_intern("public"),
    }];

    let graph = InternedGraph::from_tuples(&tuples, &mut interner);

    let anyone = InternedEntity {
        entity_type: interner.get_or_intern("user"),
        entity_id: interner.get_or_intern("anyone"),
    };
    let object = InternedEntity {
        entity_type: interner.get_or_intern("file"),
        entity_id: interner.get_or_intern("public"),
    };
    let viewer = interner.get_or_intern("viewer");

    assert!(graph.check_direct_relation(anyone, viewer, object));
}

#[test]
fn interned_permission_computation() {
    let mut interner = DefaultStringInterner::new();

    let tuples = vec![InternedTuple {
        subject_type: interner.get_or_intern("user"),
        subject_id: interner.get_or_intern("alice"),
        subject_relation: None,
        relation: interner.get_or_intern("owner"),
        object_type: interner.get_or_intern("file"),
        object_id: interner.get_or_intern("doc"),
    }];

    let graph = InternedGraph::from_tuples(&tuples, &mut interner);

    let config_json = r#"{"relations":{"owner":"direct"},"permissions":{"write":["owner"]}}"#;
    let config: NamespaceConfig = serde_json::from_str(config_json).unwrap();
    let interned_config = InternedNamespaceConfig::from_config(&config, &mut interner);

    let mut ns_map = AHashMap::new();
    ns_map.insert(interner.get_or_intern("file"), interned_config);

    let subject = InternedEntity {
        entity_type: interner.get_or_intern("user"),
        entity_id: interner.get_or_intern("alice"),
    };
    let object = InternedEntity {
        entity_type: interner.get_or_intern("file"),
        entity_id: interner.get_or_intern("doc"),
    };
    let write = interner.get_or_intern("write");

    let mut memo = InternedMemoCache::new();
    let mut visited = InternedVisitedSet::new();

    let result = compute_permission_interned(
        subject,
        write,
        object,
        &graph,
        &ns_map,
        &mut memo,
        &mut visited,
        0,
    );
    assert!(result);
}

// ============================================================================
// expand_permission / find_subject_groups / collect_candidate_objects
// ============================================================================

#[test]
fn expand_subjects_finds_direct() {
    let tuples = vec![
        tuple_direct("user", "alice", "viewer", "file", "doc"),
        tuple_direct("user", "bob", "viewer", "file", "doc"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();
    let mut subjects = AHashSet::new();
    let mut visited = AHashSet::new();

    expand_permission(
        "viewer",
        &entity("file", "doc"),
        &graph,
        &namespaces,
        &mut subjects,
        &mut visited,
        0,
    );

    assert!(subjects.contains(&("user".to_string(), "alice".to_string())));
    assert!(subjects.contains(&("user".to_string(), "bob".to_string())));
    assert_eq!(subjects.len(), 2);
}

#[test]
fn find_groups_for_subject() {
    let tuples = vec![
        tuple_direct("user", "alice", "member", "group", "eng"),
        tuple_direct("user", "alice", "member", "group", "admins"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);

    let groups = find_subject_groups(&entity("user", "alice"), &graph);
    assert_eq!(groups.len(), 2);
    let group_ids: Vec<&str> = groups.iter().map(|g| g.entity_id.as_str()).collect();
    assert!(group_ids.contains(&"eng"));
    assert!(group_ids.contains(&"admins"));
}

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

#[test]
fn expand_subjects_skips_parent_reverse_pattern() {
    // file:/child --parent--> file:/parent
    // user:alice --viewer--> file:/child
    //
    // With parent reverse traversal disabled (Bug A), expanding read on /parent
    // must NOT include alice via child ownership/viewership.
    let tuples = vec![
        tuple_direct("file", "/child", "parent", "file", "/parent"),
        tuple_direct("user", "alice", "viewer", "file", "/child"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut subjects = AHashSet::new();
    let mut visited = AHashSet::new();
    expand_permission(
        "read",
        &entity("file", "/parent"),
        &graph,
        &namespaces,
        &mut subjects,
        &mut visited,
        0,
    );

    assert!(!subjects.contains(&("user".to_string(), "alice".to_string())));
}

#[test]
fn collect_candidates_without_namespace_uses_permission_directly() {
    let tuples = vec![tuple_direct("user", "alice", "viewer", "doc", "d1")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let namespaces = AHashMap::new();

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "alice"),
        "viewer",
        "doc",
        &graph,
        &namespaces,
        &mut candidates,
    );

    assert!(candidates.contains(&entity("doc", "d1")));
}

#[test]
fn collect_candidates_include_wildcard_grants() {
    let tuples = vec![tuple_direct("*", "*", "viewer", "file", "/public.txt")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{"viewer":"direct"},"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "anyone"),
        "read",
        "file",
        &graph,
        &namespaces,
        &mut candidates,
    );

    assert!(candidates.contains(&entity("file", "/public.txt")));
}

#[test]
fn collect_candidates_include_parent_inheritance() {
    let tuples = vec![
        tuple_direct("user", "alice", "viewer", "file", "/parent"),
        tuple_direct("file", "/child", "parent", "file", "/parent"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "alice"),
        "read",
        "file",
        &graph,
        &namespaces,
        &mut candidates,
    );

    assert!(candidates.contains(&entity("file", "/parent")));
    assert!(candidates.contains(&entity("file", "/child")));
}

#[test]
fn collect_candidates_include_group_tuple_to_userset_reverse() {
    // user:alice --member--> group:eng
    // group:eng --direct_viewer--> file:/shared.txt
    // viewer = union(direct_viewer, group_viewer)
    // group_viewer = tupleToUserset(direct_viewer, member)
    let tuples = vec![
        tuple_direct("user", "alice", "member", "group", "eng"),
        tuple_direct("group", "eng", "direct_viewer", "file", "/shared.txt"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{
        "direct_viewer":"direct",
        "group_viewer":{"tupleToUserset":{"tupleset":"direct_viewer","computedUserset":"member"}},
        "viewer":{"union":["direct_viewer","group_viewer"]}
    },"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "alice"),
        "read",
        "file",
        &graph,
        &namespaces,
        &mut candidates,
    );

    assert!(candidates.contains(&entity("file", "/shared.txt")));
}

#[test]
fn collect_candidates_expand_nested_permission_aliases() {
    // read -> can_read -> viewer -> direct tuple
    // Candidate discovery must recursively expand permission aliases.
    let tuples = vec![tuple_direct("user", "alice", "viewer", "file", "/nested.txt")];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{"viewer":"direct"},"permissions":{
        "read":["can_read"],
        "can_read":["viewer"]
    }}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();
    assert!(compute_permission(
        &entity("user", "alice"),
        "read",
        &entity("file", "/nested.txt"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    ));

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "alice"),
        "read",
        "file",
        &graph,
        &namespaces,
        &mut candidates,
    );

    assert!(candidates.contains(&entity("file", "/nested.txt")));
}

#[test]
fn collect_candidates_include_transitive_userset_subject_chain() {
    // group:A#member -> member -> group:B
    // group:B#member -> viewer -> file:/doc
    // user:alice -> member -> group:A
    //
    // compute_permission(alice, read, /doc) is true via recursive usersets.
    // Candidate collection must include /doc as well.
    let tuples = vec![
        tuple_userset("group", "A", "member", "member", "group", "B"),
        tuple_userset("group", "B", "member", "viewer", "file", "/doc"),
        tuple_direct("user", "alice", "member", "group", "A"),
    ];
    let graph = ReBACGraph::from_tuples(&tuples);
    let config_json = r#"{"relations":{"viewer":"direct"},"permissions":{"read":["viewer"]}}"#;
    let mut namespaces = AHashMap::new();
    namespaces.insert("file".to_string(), ns_config(config_json));

    let mut memo = MemoCache::new();
    assert!(compute_permission(
        &entity("user", "alice"),
        "read",
        &entity("file", "/doc"),
        &graph,
        &namespaces,
        &mut memo,
        &mut AHashSet::new(),
        0,
    ));

    let mut candidates = AHashSet::new();
    collect_candidate_objects_for_subject(
        &entity("user", "alice"),
        "read",
        "file",
        &graph,
        &namespaces,
        &mut candidates,
    );
    assert!(candidates.contains(&entity("file", "/doc")));
}

// ============================================================================
// Cross-implementation parity: string-keyed vs interned must agree
// ============================================================================

/// Helper to run the same permission check against both string-keyed and
/// interned implementations, asserting they produce identical results.
fn assert_parity(
    tuples: &[ReBACTuple],
    namespaces_json: &[(&str, &str)],
    checks: &[(&str, &str, &str, &str, &str, bool)],
) {
    // --- String-keyed setup ---
    let graph = ReBACGraph::from_tuples(tuples);
    let mut namespaces: AHashMap<String, NamespaceConfig> = AHashMap::new();
    for (name, json) in namespaces_json {
        namespaces.insert(name.to_string(), ns_config(json));
    }

    // --- Interned setup ---
    let mut interner = DefaultStringInterner::new();
    let interned_tuples: Vec<InternedTuple> = tuples
        .iter()
        .map(|t| InternedTuple {
            subject_type: interner.get_or_intern(&t.subject_type),
            subject_id: interner.get_or_intern(&t.subject_id),
            subject_relation: t
                .subject_relation
                .as_ref()
                .map(|r| interner.get_or_intern(r)),
            relation: interner.get_or_intern(&t.relation),
            object_type: interner.get_or_intern(&t.object_type),
            object_id: interner.get_or_intern(&t.object_id),
        })
        .collect();
    let interned_graph = InternedGraph::from_tuples(&interned_tuples, &mut interner);
    let mut interned_ns: AHashMap<Sym, InternedNamespaceConfig> = AHashMap::new();
    for (name, json) in namespaces_json {
        let config: NamespaceConfig = serde_json::from_str(json).unwrap();
        let interned_config = InternedNamespaceConfig::from_config(&config, &mut interner);
        interned_ns.insert(interner.get_or_intern(*name), interned_config);
    }

    // --- Run checks against both ---
    for (subj_type, subj_id, permission, obj_type, obj_id, expected) in checks {
        let subject = entity(subj_type, subj_id);
        let object = entity(obj_type, obj_id);

        // String-keyed
        let mut memo = MemoCache::new();
        let mut visited = AHashSet::new();
        let string_result = compute_permission(
            &subject,
            permission,
            &object,
            &graph,
            &namespaces,
            &mut memo,
            &mut visited,
            0,
        );

        // Interned
        let i_subject = InternedEntity {
            entity_type: interner.get_or_intern(*subj_type),
            entity_id: interner.get_or_intern(*subj_id),
        };
        let i_object = InternedEntity {
            entity_type: interner.get_or_intern(*obj_type),
            entity_id: interner.get_or_intern(*obj_id),
        };
        let i_perm = interner.get_or_intern(*permission);

        let mut i_memo = InternedMemoCache::new();
        let mut i_visited = InternedVisitedSet::new();
        let interned_result = compute_permission_interned(
            i_subject,
            i_perm,
            i_object,
            &interned_graph,
            &interned_ns,
            &mut i_memo,
            &mut i_visited,
            0,
        );

        assert_eq!(
            string_result, interned_result,
            "Parity mismatch for ({subj_type}:{subj_id}, {permission}, {obj_type}:{obj_id}): \
             string={string_result}, interned={interned_result}"
        );
        assert_eq!(
            string_result, *expected,
            "Unexpected result for ({subj_type}:{subj_id}, {permission}, {obj_type}:{obj_id}): \
             got={string_result}, expected={expected}"
        );
    }
}

#[test]
fn parity_direct_relations() {
    assert_parity(
        &[
            tuple_direct("user", "alice", "editor", "file", "readme"),
            tuple_direct("user", "bob", "viewer", "file", "readme"),
        ],
        &[],
        &[
            ("user", "alice", "editor", "file", "readme", true),
            ("user", "bob", "editor", "file", "readme", false),
            ("user", "bob", "viewer", "file", "readme", true),
            ("user", "charlie", "viewer", "file", "readme", false),
        ],
    );
}

#[test]
fn parity_userset_permissions() {
    assert_parity(
        &[
            tuple_userset("group", "eng", "member", "editor", "file", "readme"),
            tuple_direct("user", "alice", "member", "group", "eng"),
        ],
        &[],
        &[
            ("user", "alice", "editor", "file", "readme", true),
            ("user", "bob", "editor", "file", "readme", false),
        ],
    );
}

#[test]
fn parity_tuple_to_userset() {
    let ns_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;

    assert_parity(
        &[
            tuple_direct("file", "doc1", "parent", "folder", "docs"),
            tuple_direct("user", "alice", "viewer", "folder", "docs"),
        ],
        &[("file", ns_json), ("folder", ns_json)],
        &[
            ("user", "alice", "read", "file", "doc1", true),
            ("user", "bob", "read", "file", "doc1", false),
        ],
    );
}

#[test]
fn parity_union_relations() {
    let ns_json = r#"{"relations":{"editor":{"union":["owner","collaborator"]},"owner":"direct","collaborator":"direct"},"permissions":{"write":["editor"]}}"#;

    assert_parity(
        &[
            tuple_direct("user", "alice", "owner", "file", "readme"),
            tuple_direct("user", "bob", "collaborator", "file", "readme"),
        ],
        &[("file", ns_json)],
        &[
            ("user", "alice", "write", "file", "readme", true),
            ("user", "bob", "write", "file", "readme", true),
            ("user", "charlie", "write", "file", "readme", false),
        ],
    );
}

#[test]
fn parity_wildcard_subject() {
    assert_parity(
        &[tuple_direct("*", "*", "viewer", "file", "public")],
        &[],
        &[
            ("user", "anyone", "viewer", "file", "public", true),
            ("agent", "bot", "viewer", "file", "public", true),
        ],
    );
}

#[test]
fn parity_deeply_nested() {
    let ns_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;

    assert_parity(
        &[
            tuple_direct("file", "doc", "parent", "folder", "a"),
            tuple_direct("folder", "a", "parent", "folder", "b"),
            tuple_direct("folder", "b", "parent", "folder", "c"),
            tuple_direct("folder", "c", "parent", "folder", "root"),
            tuple_direct("user", "alice", "viewer", "folder", "root"),
        ],
        &[("file", ns_json), ("folder", ns_json)],
        &[
            ("user", "alice", "read", "file", "doc", true),
            ("user", "bob", "read", "file", "doc", false),
        ],
    );
}

#[test]
fn parity_cycle_detection() {
    let ns_json = r#"{"relations":{"member":"direct","viewer":{"union":["member"]}},"permissions":{"read":["viewer"]}}"#;

    assert_parity(
        &[
            tuple_direct("group", "a", "member", "group", "b"),
            tuple_direct("group", "b", "member", "group", "a"),
        ],
        &[("group", ns_json)],
        &[("user", "charlie", "read", "group", "a", false)],
    );
}

#[test]
fn parity_tuple_to_userset_with_direct_fallback() {
    // Exercises the Zanzibar direct-relation fallthrough in TupleToUserset
    // (graph.rs:305). User has a direct "viewer" tuple on the file, and the
    // namespace also defines viewer as tupleToUserset(parent, viewer).
    // Both paths should grant access.
    let ns_json = r#"{"relations":{
        "parent":"direct",
        "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
    },"permissions":{"read":["viewer"]}}"#;

    assert_parity(
        &[
            // Direct viewer on the file (exercises the fallback)
            tuple_direct("user", "alice", "viewer", "file", "doc1"),
            // Indirect via parent folder (exercises tupleToUserset forward)
            tuple_direct("file", "doc2", "parent", "folder", "docs"),
            tuple_direct("user", "bob", "viewer", "folder", "docs"),
        ],
        &[("file", ns_json), ("folder", ns_json)],
        &[
            // alice: direct viewer on doc1
            ("user", "alice", "read", "file", "doc1", true),
            // bob: indirect via folder
            ("user", "bob", "read", "file", "doc2", true),
            // alice has no access to doc2 (no direct or indirect path)
            ("user", "alice", "read", "file", "doc2", false),
        ],
    );
}

#[test]
fn parity_shared_visited_cycle_path() {
    // Exercises the shared visited set with a cycle reachable from
    // multiple branches. Group A and B form a cycle. User has member
    // on group A. The permission check must terminate (not loop) and
    // the cycle must not block the successful direct path.
    let ns_json = r#"{"relations":{
        "member":"direct",
        "viewer":{"union":["member","admin"]}
    },"permissions":{"read":["viewer"]}}"#;

    assert_parity(
        &[
            // Cycle: A ↔ B via member
            tuple_direct("group", "a", "member", "group", "b"),
            tuple_direct("group", "b", "member", "group", "a"),
            // User is direct member of group A
            tuple_direct("user", "alice", "member", "group", "a"),
        ],
        &[("group", ns_json)],
        &[
            // alice is a member of group A → viewer via union(member)
            ("user", "alice", "read", "group", "a", true),
            // bob has no relation → should be false, not loop
            ("user", "bob", "read", "group", "a", false),
        ],
    );
}

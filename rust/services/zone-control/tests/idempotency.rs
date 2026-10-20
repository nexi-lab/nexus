use nexus_zone_control::contract::ContractCatalog;
use nexus_zone_control::idempotency::{request_hash, scope, NonIntegerMutation};
use nexus_zone_control::model::{PrincipalRef, SubjectType};
use serde_json::{json, Value};

#[test]
fn persisted_request_fingerprints_and_scope_encodings_remain_stable() {
    let fixtures: Value = serde_json::from_str(include_str!(
        "../../../../contracts/fixtures/idempotency/cases.json"
    ))
    .unwrap();
    for case in fixtures["requests"].as_array().unwrap() {
        assert_eq!(
            request_hash(&case["payload"]).unwrap(),
            case["sha256"].as_str().unwrap(),
            "{}",
            case["name"]
        );
    }
    let catalog = ContractCatalog::new().unwrap();
    for case in fixtures["scopes"].as_array().unwrap() {
        let subject: PrincipalRef = catalog
            .decode(json!({
                "subject_type": case["subject_type"],
                "subject_id": case["subject_id"],
            }))
            .unwrap();
        let targets: Vec<_> = case["targets"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap())
            .collect();
        assert_eq!(
            scope(&subject, case["action"].as_str().unwrap(), &targets),
            case["encoded"].as_str().unwrap()
        );
    }
}

#[test]
fn equal_ids_under_distinct_subject_types_cannot_share_an_operation_scope() {
    let user = PrincipalRef {
        subject_type: SubjectType::User,
        subject_id: "same-id".into(),
        trust_domain: None,
    };
    let agent = PrincipalRef {
        subject_type: SubjectType::Agent,
        ..user.clone()
    };
    assert_ne!(
        scope(&user, "zone.mount", &["a", "b", "/docs"]),
        scope(&agent, "zone.mount", &["a", "b", "/docs"])
    );
    assert_ne!(
        scope(&user, "zone.transfer", &["a", "b"]),
        scope(&agent, "zone.transfer", &["a", "b"])
    );
}

#[test]
fn object_order_does_not_change_a_hash_and_defaults_remain_part_of_the_request() {
    let left: Value = serde_json::from_str(r#"{"z":{"b":2,"a":1},"a":[true,null]}"#).unwrap();
    let right: Value = serde_json::from_str(r#"{"a":[true,null],"z":{"a":1,"b":2}}"#).unwrap();
    assert_eq!(request_hash(&left), request_hash(&right));
    assert_ne!(
        request_hash(&json!({})),
        request_hash(&json!({"description": null}))
    );
    assert_eq!(
        request_hash(&json!({"unsupported": [1.0]})),
        Err(NonIntegerMutation)
    );
}

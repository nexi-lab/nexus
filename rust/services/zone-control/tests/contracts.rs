use nexus_zone_control::contract::{CanonicalModel, ContractCatalog, ValidationFailure};
use nexus_zone_control::model::PrincipalRef;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::PathBuf;

#[test]
fn the_shared_fixture_corpus_matches_the_rust_contract_boundary() {
    let catalog = ContractCatalog::new().unwrap();
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../contracts/fixtures");
    let mut checked = 0;
    for (group, expected) in [
        ("valid/cases.json", true),
        ("invalid/cases.json", false),
        ("roundtrip/cases.json", true),
        ("secret-negative/cases.json", true),
        ("path-traversal/cases.json", false),
        ("compatibility/previous-minor.json", true),
    ] {
        let cases: Value =
            serde_json::from_slice(&std::fs::read(root.join(group)).unwrap()).unwrap();
        let cases = cases["cases"].as_array().unwrap();
        assert!(
            !cases.is_empty(),
            "canonical fixture group is empty: {group}"
        );
        for case in cases {
            let result = catalog.validate(case["schema"].as_str().unwrap(), &case["payload"]);
            assert_eq!(
                result.is_ok(),
                case["expected"]
                    .as_str()
                    .map_or(expected, |verdict| verdict == "valid"),
                "{group}/{}: {result:?}",
                case["name"]
            );
            checked += 1;
        }
    }
    assert!(checked > 0);
    println!("validated {checked} shared contract fixtures");
}

#[test]
fn principal_decoding_uses_the_schema_and_drops_unknown_credential_fields() {
    let catalog = ContractCatalog::new().unwrap();
    let schema: Value = serde_json::from_str(include_str!(
        "../../../../contracts/common/v1/principal-ref.schema.json"
    ))
    .unwrap();
    for kind in schema["properties"]["subject_type"]["enum"]
        .as_array()
        .unwrap()
    {
        let principal: PrincipalRef = catalog
            .decode(
                json!({"subject_type": kind, "subject_id": "id", "api_key": "fixture-only-secret"}),
            )
            .unwrap();
        let encoded = serde_json::to_value(&principal).unwrap();
        assert_eq!(principal.subject_type.as_str(), kind.as_str().unwrap());
        assert!(encoded.get("api_key").is_none());
        catalog.validate(PrincipalRef::SCHEMA, &encoded).unwrap();
    }
    for principal in [
        json!({"subject_type": "node", "subject_id": "id"}),
        json!({"subject_type": "user", "subject_id": ""}),
    ] {
        assert!(matches!(
            catalog.decode::<PrincipalRef>(principal),
            Err(ValidationFailure::InvalidDocument { .. })
        ));
    }
}

#[test]
fn adapter_drift_is_distinct_from_invalid_input_and_does_not_reflect_it() {
    #[derive(Deserialize)]
    struct BrokenAdapter {
        #[serde(rename = "subject_id")]
        _subject_id: u64,
    }
    impl CanonicalModel for BrokenAdapter {
        const SCHEMA: &'static str = PrincipalRef::SCHEMA;
    }
    let catalog = ContractCatalog::new().unwrap();
    let error = catalog
        .decode::<BrokenAdapter>(
            json!({"subject_type": "user", "subject_id": "fixture-private-value"}),
        )
        .err()
        .unwrap();
    assert_eq!(
        error,
        ValidationFailure::AdapterMismatch(PrincipalRef::SCHEMA)
    );
    assert!(!error.to_string().contains("fixture-private-value"));
}

use nexus_zone_control::contract::ContractCatalog;
use serde_json::Value;
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
        for case in cases["cases"].as_array().unwrap() {
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

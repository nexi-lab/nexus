//! Namespace configuration deserialization helpers.
//!
//! This module re-exports the config types from `types.rs` and provides
//! convenience functions for parsing namespace configs from JSON.

use crate::types::NamespaceConfig;

/// Parse a namespace config from a JSON string.
pub fn parse_namespace_config(json: &str) -> Result<NamespaceConfig, serde_json::Error> {
    serde_json::from_str(json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::RelationConfig;

    #[test]
    fn parse_direct_relation() {
        let json = r#"{"relations":{"owner":"direct"},"permissions":{"read":["owner"]}}"#;
        let config = parse_namespace_config(json).unwrap();
        assert!(config.relations.contains_key("owner"));
        assert!(matches!(
            config.relations.get("owner").unwrap(),
            RelationConfig::Direct(_)
        ));
        assert_eq!(config.permissions.get("read").unwrap(), &vec!["owner"]);
    }

    #[test]
    fn parse_empty_dict_relation() {
        let json = r#"{"relations":{"viewer":{}},"permissions":{"read":["viewer"]}}"#;
        let config = parse_namespace_config(json).unwrap();
        assert!(matches!(
            config.relations.get("viewer").unwrap(),
            RelationConfig::EmptyDict(_)
        ));
    }

    #[test]
    fn parse_union_relation() {
        let json =
            r#"{"relations":{"editor":{"union":["owner","collaborator"]}},"permissions":{}}"#;
        let config = parse_namespace_config(json).unwrap();
        match config.relations.get("editor").unwrap() {
            RelationConfig::Union { union } => {
                assert_eq!(union, &vec!["owner", "collaborator"]);
            }
            other => panic!("expected Union, got {:?}", other),
        }
    }

    #[test]
    fn parse_tuple_to_userset() {
        let json = r#"{
            "relations":{
                "parent":"direct",
                "viewer":{"tupleToUserset":{"tupleset":"parent","computedUserset":"viewer"}}
            },
            "permissions":{"read":["viewer"]}
        }"#;
        let config = parse_namespace_config(json).unwrap();
        match config.relations.get("viewer").unwrap() {
            RelationConfig::TupleToUserset { tuple_to_userset } => {
                assert_eq!(tuple_to_userset.tupleset, "parent");
                assert_eq!(tuple_to_userset.computed_userset, "viewer");
            }
            other => panic!("expected TupleToUserset, got {:?}", other),
        }
    }
}

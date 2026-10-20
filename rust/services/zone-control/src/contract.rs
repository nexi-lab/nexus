use jsonschema::{Registry, Validator};
use serde_json::Value;
use std::collections::BTreeMap;

#[derive(Debug, thiserror::Error)]
#[error("cannot compile canonical contract {schema}: {reason}")]
pub struct CatalogError {
    schema: String,
    reason: String,
}

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum ValidationFailure {
    #[error("unknown canonical contract: {0}")]
    UnknownContract(String),
    #[error("invalid {schema} document at {path}")]
    InvalidDocument { schema: String, path: String },
}

/// Compile the embedded owner schemas once when assembling the product service.
/// Validation reads neither the filesystem nor external reference servers.
pub struct ContractCatalog {
    validators: BTreeMap<String, Validator>,
}

impl ContractCatalog {
    pub fn new() -> Result<Self, CatalogError> {
        let documents = serde_json::from_str(include_str!(concat!(
            env!("OUT_DIR"),
            "/zone-contracts.json"
        )))
        .map_err(|error| CatalogError {
            schema: "embedded bundle".into(),
            reason: error.to_string(),
        })?;
        Self::compile(documents)
    }

    fn compile(documents: BTreeMap<String, Value>) -> Result<Self, CatalogError> {
        let mut resources = Registry::new();
        for (path, document) in &documents {
            let id = document["$id"].as_str().ok_or_else(|| CatalogError {
                schema: path.clone(),
                reason: "schema identity is missing".into(),
            })?;
            resources = resources
                .add(id, document.clone())
                .map_err(|error| CatalogError {
                    schema: path.clone(),
                    reason: error.to_string(),
                })?;
        }
        let registry = resources.prepare().map_err(|error| CatalogError {
            schema: "reference registry".into(),
            reason: error.to_string(),
        })?;
        let mut validators = BTreeMap::new();
        for (path, document) in documents {
            let validator = jsonschema::draft202012::options()
                .with_registry(&registry)
                .offline()
                .build(&document)
                .map_err(|error| CatalogError {
                    schema: path.clone(),
                    reason: error.to_string(),
                })?;
            validators.insert(path, validator);
        }
        Ok(Self { validators })
    }

    pub fn validate(&self, schema: &str, document: &Value) -> Result<(), ValidationFailure> {
        let validator = self
            .validators
            .get(schema)
            .ok_or_else(|| ValidationFailure::UnknownContract(schema.to_owned()))?;
        validator
            .validate(document)
            .map_err(|error| ValidationFailure::InvalidDocument {
                schema: schema.to_owned(),
                path: error.instance_path().to_string(),
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn missing_reference_refuses_catalog_assembly() {
        let documents = BTreeMap::from([(
            "unresolved.json".into(),
            json!({
                "$id": "https://contracts.sudo.dev/unresolved",
                "$ref": "http://127.0.0.1:9/never-fetch"
            }),
        )]);
        assert!(ContractCatalog::compile(documents).is_err());
    }

    #[test]
    fn validation_errors_report_locations_without_reflecting_values() {
        let catalog = ContractCatalog::new().unwrap();
        let value = json!({"subject_type": "user", "subject_id": {"secret": "fixture-only-value"}});
        let error = catalog
            .validate("common/v1/principal-ref.schema.json", &value)
            .unwrap_err();
        assert_eq!(
            error,
            ValidationFailure::InvalidDocument {
                schema: "common/v1/principal-ref.schema.json".into(),
                path: "/subject_id".into(),
            }
        );
        assert!(!error.to_string().contains("fixture-only-value"));
    }
}

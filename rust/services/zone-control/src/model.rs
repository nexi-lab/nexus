use serde::{Deserialize, Serialize};

/// A wire identity reference. Ingress owns authentication of the caller.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct PrincipalRef {
    pub subject_type: SubjectType,
    pub subject_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trust_domain: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SubjectType {
    Organization,
    User,
    Agent,
    Service,
}

impl SubjectType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Organization => "organization",
            Self::User => "user",
            Self::Agent => "agent",
            Self::Service => "service",
        }
    }
}

impl crate::contract::CanonicalModel for PrincipalRef {
    const SCHEMA: &'static str = "common/v1/principal-ref.schema.json";
}

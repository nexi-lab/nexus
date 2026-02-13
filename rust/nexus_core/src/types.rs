//! Domain types shared across nexus_core modules.

use ahash::{AHashMap, AHashSet};
use serde::Deserialize;
use std::collections::HashMap as StdHashMap;
use string_interner::{DefaultStringInterner, DefaultSymbol};

/// Type alias for interned string symbol — 4 bytes, O(1) equality, Copy.
pub type Sym = DefaultSymbol;

/// Entity represents a subject or object in ReBAC.
#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct Entity {
    pub entity_type: String,
    pub entity_id: String,
}

/// Tuple represents a relationship between entities.
#[derive(Debug, Clone)]
pub struct ReBACTuple {
    pub subject_type: String,
    pub subject_id: String,
    /// When set, this is a userset-as-subject tuple:
    /// "members of subject_type:subject_id have this relation on the object"
    pub subject_relation: Option<String>,
    pub relation: String,
    pub object_type: String,
    pub object_id: String,
}

/// Namespace configuration for permission expansion (uses std HashMap for serde).
#[derive(Debug, Clone, Deserialize)]
pub struct NamespaceConfig {
    pub relations: StdHashMap<String, RelationConfig>,
    pub permissions: StdHashMap<String, Vec<String>>,
}

/// Configuration for a single relation.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum RelationConfig {
    #[allow(dead_code)]
    Direct(String),
    Union {
        union: Vec<String>,
    },
    TupleToUserset {
        #[serde(rename = "tupleToUserset")]
        tuple_to_userset: TupleToUsersetConfig,
    },
    #[allow(dead_code)]
    EmptyDict(serde_json::Map<String, serde_json::Value>),
}

/// TupleToUserset expansion configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct TupleToUsersetConfig {
    pub tupleset: String,
    #[serde(rename = "computedUserset")]
    pub computed_userset: String,
}

/// Memoization cache for permission checks (string-keyed).
pub type MemoCache = AHashMap<(String, String, String, String, String), bool>;

/// Permission check request tuple.
pub type CheckRequest = (String, String, String, String, String);

// ============================================================================
// Interned types — zero-allocation permission checks
// ============================================================================

/// Interned entity with symbols for O(1) equality.
#[derive(Debug, Clone, Copy, Hash, Eq, PartialEq)]
pub struct InternedEntity {
    pub entity_type: Sym,
    pub entity_id: Sym,
}

/// Interned ReBAC tuple with symbols.
#[derive(Debug, Clone, Copy)]
pub struct InternedTuple {
    pub subject_type: Sym,
    pub subject_id: Sym,
    pub subject_relation: Option<Sym>,
    pub relation: Sym,
    pub object_type: Sym,
    pub object_id: Sym,
}

/// Interned userset entry.
#[derive(Debug, Clone, Copy)]
pub struct InternedUsersetEntry {
    pub subject_type: Sym,
    pub subject_id: Sym,
    pub subject_relation: Sym,
}

/// Key for userset index: (object_type, object_id, relation).
pub type UsersetKey = (String, String, String);

/// Userset entry for string-keyed graph.
#[derive(Debug, Clone)]
pub struct UsersetEntry {
    pub subject_type: String,
    pub subject_id: String,
    pub subject_relation: String,
}

/// Interned memoization key.
pub type InternedMemoKey = (Sym, Sym, Sym, Sym, Sym);

/// Interned memoization cache.
pub type InternedMemoCache = AHashMap<InternedMemoKey, bool>;

/// Interned namespace config for fast lookups.
#[derive(Debug, Clone)]
pub struct InternedNamespaceConfig {
    pub relations: AHashMap<Sym, InternedRelationConfig>,
    pub permissions: AHashMap<Sym, Vec<Sym>>,
}

/// Interned relation config.
#[derive(Debug, Clone)]
pub enum InternedRelationConfig {
    Direct,
    Union {
        union: Vec<Sym>,
    },
    TupleToUserset {
        tupleset: Sym,
        computed_userset: Sym,
    },
}

impl InternedNamespaceConfig {
    /// Build from a raw `NamespaceConfig` plus an interner.
    pub fn from_config(config: &NamespaceConfig, interner: &mut DefaultStringInterner) -> Self {
        let relations = config
            .relations
            .iter()
            .map(|(k, v)| {
                let key = interner.get_or_intern(k);
                let value = match v {
                    RelationConfig::Direct(_) | RelationConfig::EmptyDict(_) => {
                        InternedRelationConfig::Direct
                    }
                    RelationConfig::Union { union } => InternedRelationConfig::Union {
                        union: union.iter().map(|s| interner.get_or_intern(s)).collect(),
                    },
                    RelationConfig::TupleToUserset { tuple_to_userset } => {
                        InternedRelationConfig::TupleToUserset {
                            tupleset: interner.get_or_intern(&tuple_to_userset.tupleset),
                            computed_userset: interner
                                .get_or_intern(&tuple_to_userset.computed_userset),
                        }
                    }
                };
                (key, value)
            })
            .collect();

        let permissions = config
            .permissions
            .iter()
            .map(|(k, v)| {
                let key = interner.get_or_intern(k);
                let values: Vec<Sym> = v.iter().map(|s| interner.get_or_intern(s)).collect();
                (key, values)
            })
            .collect();

        InternedNamespaceConfig {
            relations,
            permissions,
        }
    }
}

/// Key types for string-keyed graph.
pub type TupleKey = (String, String, String, String, String);
pub type AdjacencyKey = (String, String, String);

/// Interned key types.
pub type InternedTupleKey = (Sym, Sym, Sym, Sym, Sym);
pub type InternedAdjacencyKey = (Sym, Sym, Sym);
pub type InternedUsersetKey = (Sym, Sym, Sym);

/// Visited set for cycle detection.
pub type VisitedSet = AHashSet<(String, String, String, String, String)>;
pub type InternedVisitedSet = AHashSet<InternedMemoKey>;

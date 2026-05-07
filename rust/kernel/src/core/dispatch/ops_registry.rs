use parking_lot::RwLock;
use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct OpName(Arc<str>);

impl OpName {
    pub fn new(name: impl AsRef<str>) -> Self {
        Self(Arc::from(name.as_ref().to_ascii_lowercase()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<&str> for OpName {
    fn from(value: &str) -> Self {
        Self::new(value)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum FileType {
    Json,
    Parquet,
    Unknown,
    Other(Arc<str>),
}

impl FileType {
    pub fn from_path_and_mime(path: &str, mime_type: Option<&str>) -> Self {
        let mime = mime_type.unwrap_or("").trim().to_ascii_lowercase();
        if matches!(mime.as_str(), "application/json" | "text/json") {
            return Self::Json;
        }
        if matches!(
            mime.as_str(),
            "application/parquet" | "application/x-parquet" | "application/vnd.apache.parquet"
        ) {
            return Self::Parquet;
        }

        let ext = path
            .rsplit_once('.')
            .map(|(_, ext)| ext.trim().to_ascii_lowercase())
            .unwrap_or_default();
        match ext.as_str() {
            "json" | "jsonl" | "ndjson" => Self::Json,
            "parquet" | "pq" => Self::Parquet,
            "" => Self::Unknown,
            other => Self::Other(Arc::from(other)),
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            Self::Json => "json",
            Self::Parquet => "parquet",
            Self::Unknown => "unknown",
            Self::Other(value) => value,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum BackendKind {
    S3,
    Slack,
    GitHub,
    Local,
    Unknown,
    Other(Arc<str>),
}

impl BackendKind {
    pub fn from_backend_name(name: &str) -> Self {
        let normalized = name.trim().to_ascii_lowercase().replace('-', "_");
        match normalized.as_str() {
            "path_s3" | "s3" | "s3_connector" => Self::S3,
            "slack" | "path_slack" | "slack_connector" => Self::Slack,
            "github" | "github_connector" | "gws_github" => Self::GitHub,
            "local" | "path_local" | "cas_local" => Self::Local,
            "" => Self::Unknown,
            other => Self::Other(Arc::from(other)),
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            Self::S3 => "s3",
            Self::Slack => "slack",
            Self::GitHub => "github",
            Self::Local => "local",
            Self::Unknown => "unknown",
            Self::Other(value) => value,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct OpKey {
    pub name: OpName,
    pub filetype: Option<FileType>,
    pub backend: Option<BackendKind>,
}

impl OpKey {
    pub fn new(name: OpName, filetype: Option<FileType>, backend: Option<BackendKind>) -> Self {
        Self {
            name,
            filetype,
            backend,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CatHandlerKind {
    Default,
    JsonPretty,
    ParquetJson,
    GitHubJson,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GrepHandlerKind {
    Default,
    SlackSearch,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RawReadHandlerKind {
    GitHub,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FingerprintHandlerKind {
    S3,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OpHandler {
    Cat(CatHandlerKind),
    Grep(GrepHandlerKind),
    RawRead(RawReadHandlerKind),
    Fingerprint(FingerprintHandlerKind),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OpsRegistryErrorKind {
    DuplicateKey,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpsRegistryError {
    pub kind: OpsRegistryErrorKind,
    pub key: OpKey,
}

impl fmt::Display for OpsRegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "operation handler already registered for {:?}", self.key)
    }
}

impl std::error::Error for OpsRegistryError {}

pub struct OpsRegistry {
    table: RwLock<HashMap<OpKey, OpHandler>>,
}

impl OpsRegistry {
    pub fn new() -> Self {
        Self {
            table: RwLock::new(HashMap::new()),
        }
    }

    pub fn register(&self, key: OpKey, handler: OpHandler) -> Result<(), OpsRegistryError> {
        let mut table = self.table.write();
        if table.contains_key(&key) {
            return Err(OpsRegistryError {
                kind: OpsRegistryErrorKind::DuplicateKey,
                key,
            });
        }
        table.insert(key, handler);
        Ok(())
    }

    pub fn replace(&self, key: OpKey, handler: OpHandler) {
        self.table.write().insert(key, handler);
    }

    pub fn resolve(
        &self,
        op: &str,
        filetype: &FileType,
        backend: &BackendKind,
    ) -> Option<OpHandler> {
        let name = OpName::new(op);
        let table = self.table.read();
        let probes = [
            OpKey::new(name.clone(), Some(filetype.clone()), Some(backend.clone())),
            OpKey::new(name.clone(), None, Some(backend.clone())),
            OpKey::new(name.clone(), Some(filetype.clone()), None),
            OpKey::new(name, None, None),
        ];
        probes.iter().find_map(|key| table.get(key).copied())
    }

    pub fn len(&self) -> usize {
        self.table.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl Default for OpsRegistry {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(op: &str, ft: Option<FileType>, be: Option<BackendKind>) -> OpKey {
        OpKey::new(OpName::new(op), ft, be)
    }

    #[test]
    fn resolve_prefers_exact_backend_then_filetype_then_default() {
        let registry = OpsRegistry::new();
        registry
            .register(
                key("cat", None, None),
                OpHandler::Cat(CatHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key("cat", Some(FileType::Json), None),
                OpHandler::Cat(CatHandlerKind::JsonPretty),
            )
            .unwrap();
        registry
            .register(
                key("cat", None, Some(BackendKind::GitHub)),
                OpHandler::RawRead(RawReadHandlerKind::GitHub),
            )
            .unwrap();
        registry
            .register(
                key("cat", Some(FileType::Json), Some(BackendKind::GitHub)),
                OpHandler::Cat(CatHandlerKind::GitHubJson),
            )
            .unwrap();

        assert_eq!(
            registry.resolve("cat", &FileType::Json, &BackendKind::GitHub),
            Some(OpHandler::Cat(CatHandlerKind::GitHubJson))
        );
        assert_eq!(
            registry.resolve("cat", &FileType::Json, &BackendKind::Local),
            Some(OpHandler::Cat(CatHandlerKind::JsonPretty))
        );
        assert_eq!(
            registry.resolve("cat", &FileType::Unknown, &BackendKind::Local),
            Some(OpHandler::Cat(CatHandlerKind::Default))
        );
    }

    #[test]
    fn backend_wildcard_precedes_filetype_wildcard() {
        let registry = OpsRegistry::new();
        registry
            .register(
                key("grep", Some(FileType::Json), None),
                OpHandler::Grep(GrepHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key("grep", None, Some(BackendKind::Slack)),
                OpHandler::Grep(GrepHandlerKind::SlackSearch),
            )
            .unwrap();

        assert_eq!(
            registry.resolve("grep", &FileType::Json, &BackendKind::Slack),
            Some(OpHandler::Grep(GrepHandlerKind::SlackSearch))
        );
    }

    #[test]
    fn duplicate_register_rejects_and_replace_overwrites() {
        let registry = OpsRegistry::new();
        let key = key("cat", None, None);
        registry
            .register(key.clone(), OpHandler::Cat(CatHandlerKind::Default))
            .unwrap();

        let err = registry
            .register(key.clone(), OpHandler::Cat(CatHandlerKind::JsonPretty))
            .unwrap_err();
        assert_eq!(err.kind, OpsRegistryErrorKind::DuplicateKey);

        registry.replace(key, OpHandler::Cat(CatHandlerKind::JsonPretty));
        assert_eq!(
            registry.resolve("cat", &FileType::Unknown, &BackendKind::Unknown),
            Some(OpHandler::Cat(CatHandlerKind::JsonPretty))
        );
    }

    #[test]
    fn normalizes_filetypes_and_backends() {
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a.json", None),
            FileType::Json
        );
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a.parquet", None),
            FileType::Parquet
        );
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a", Some("application/json")),
            FileType::Json
        );
        assert_eq!(BackendKind::from_backend_name("path_s3"), BackendKind::S3);
        assert_eq!(
            BackendKind::from_backend_name("slack_connector"),
            BackendKind::Slack
        );
        assert_eq!(
            BackendKind::from_backend_name("github_connector"),
            BackendKind::GitHub
        );
    }
}

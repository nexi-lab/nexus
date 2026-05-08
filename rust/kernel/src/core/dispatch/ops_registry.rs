use std::fmt;
use std::sync::Arc;

const OP_CAT: usize = 0;
const OP_GREP: usize = 1;
const OP_RAW_READ: usize = 2;
const OP_FINGERPRINT: usize = 3;
const OP_SLOTS: usize = 4;

const FILETYPE_JSON: usize = 0;
const FILETYPE_PARQUET: usize = 1;
const FILETYPE_UNKNOWN: usize = 2;
const FILETYPE_SLOTS: usize = 3;

const BACKEND_S3: usize = 0;
const BACKEND_SLACK: usize = 1;
const BACKEND_GITHUB: usize = 2;
const BACKEND_LOCAL: usize = 3;
const BACKEND_UNKNOWN: usize = 4;
const BACKEND_SLOTS: usize = 5;

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum OpName {
    Cat,
    Grep,
    RawRead,
    Fingerprint,
    Other(Arc<str>),
}

impl OpName {
    pub fn new(name: impl AsRef<str>) -> Self {
        let name = name.as_ref().trim();
        match Self::slot_for_name(name) {
            Some(OP_CAT) => Self::Cat,
            Some(OP_GREP) => Self::Grep,
            Some(OP_RAW_READ) => Self::RawRead,
            Some(OP_FINGERPRINT) => Self::Fingerprint,
            _ => Self::Other(Arc::from(name.to_ascii_lowercase())),
        }
    }

    #[inline(always)]
    pub fn as_str(&self) -> &str {
        match self {
            Self::Cat => "cat",
            Self::Grep => "grep",
            Self::RawRead => "raw_read",
            Self::Fingerprint => "fingerprint",
            Self::Other(value) => value,
        }
    }

    #[inline(always)]
    fn matches(&self, name: &str) -> bool {
        self.as_str().eq_ignore_ascii_case(name.trim())
    }

    #[inline(always)]
    fn slot(&self) -> Option<usize> {
        match self {
            Self::Cat => Some(OP_CAT),
            Self::Grep => Some(OP_GREP),
            Self::RawRead => Some(OP_RAW_READ),
            Self::Fingerprint => Some(OP_FINGERPRINT),
            Self::Other(_) => None,
        }
    }

    #[inline(always)]
    fn slot_for_name(name: &str) -> Option<usize> {
        match name.as_bytes() {
            b"cat" => Some(OP_CAT),
            b"grep" => Some(OP_GREP),
            b"raw_read" => Some(OP_RAW_READ),
            b"fingerprint" => Some(OP_FINGERPRINT),
            _ => Self::slot_for_name_slow(name),
        }
    }

    #[cold]
    fn slot_for_name_slow(name: &str) -> Option<usize> {
        let name = name.trim();
        if name.eq_ignore_ascii_case("cat") {
            Some(OP_CAT)
        } else if name.eq_ignore_ascii_case("grep") {
            Some(OP_GREP)
        } else if name.eq_ignore_ascii_case("raw_read") {
            Some(OP_RAW_READ)
        } else if name.eq_ignore_ascii_case("fingerprint") {
            Some(OP_FINGERPRINT)
        } else {
            None
        }
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

    #[inline(always)]
    fn slot(&self) -> Option<usize> {
        match self {
            Self::Json => Some(FILETYPE_JSON),
            Self::Parquet => Some(FILETYPE_PARQUET),
            Self::Unknown => Some(FILETYPE_UNKNOWN),
            Self::Other(_) => None,
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

    #[inline(always)]
    fn slot(&self) -> Option<usize> {
        match self {
            Self::S3 => Some(BACKEND_S3),
            Self::Slack => Some(BACKEND_SLACK),
            Self::GitHub => Some(BACKEND_GITHUB),
            Self::Local => Some(BACKEND_LOCAL),
            Self::Unknown => Some(BACKEND_UNKNOWN),
            Self::Other(_) => None,
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
            name: OpName::new(name.as_str()),
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FastSlot {
    Exact(usize, usize, usize),
    Backend(usize, usize),
    Filetype(usize, usize),
    Default(usize),
}

impl FastSlot {
    #[inline(always)]
    fn op_slot(self) -> usize {
        match self {
            Self::Exact(op, _, _)
            | Self::Backend(op, _)
            | Self::Filetype(op, _)
            | Self::Default(op) => op,
        }
    }
}

#[derive(Default)]
struct SlowMatches {
    exact: Option<OpHandler>,
    backend: Option<OpHandler>,
    filetype: Option<OpHandler>,
    default: Option<OpHandler>,
}

impl SlowMatches {
    #[inline]
    fn best(self) -> Option<OpHandler> {
        self.exact
            .or(self.backend)
            .or(self.filetype)
            .or(self.default)
    }
}

pub struct OpsRegistry {
    exact: [[[Option<OpHandler>; BACKEND_SLOTS]; FILETYPE_SLOTS]; OP_SLOTS],
    backend: [[Option<OpHandler>; BACKEND_SLOTS]; OP_SLOTS],
    filetype: [[Option<OpHandler>; FILETYPE_SLOTS]; OP_SLOTS],
    defaults: [Option<OpHandler>; OP_SLOTS],
    resolved: [[[Option<OpHandler>; BACKEND_SLOTS]; FILETYPE_SLOTS]; OP_SLOTS],
    slow_entries: Vec<(OpKey, OpHandler)>,
    len: usize,
}

impl OpsRegistry {
    pub fn new() -> Self {
        Self {
            exact: [[[None; BACKEND_SLOTS]; FILETYPE_SLOTS]; OP_SLOTS],
            backend: [[None; BACKEND_SLOTS]; OP_SLOTS],
            filetype: [[None; FILETYPE_SLOTS]; OP_SLOTS],
            defaults: [None; OP_SLOTS],
            resolved: [[[None; BACKEND_SLOTS]; FILETYPE_SLOTS]; OP_SLOTS],
            slow_entries: Vec::new(),
            len: 0,
        }
    }

    pub fn register(&mut self, key: OpKey, handler: OpHandler) -> Result<(), OpsRegistryError> {
        if let Some(slot) = Self::fast_slot(&key) {
            {
                let target = self.fast_slot_mut(slot);
                if target.is_some() {
                    return Err(OpsRegistryError {
                        kind: OpsRegistryErrorKind::DuplicateKey,
                        key,
                    });
                }
                *target = Some(handler);
            }
            self.rebuild_resolved(slot.op_slot());
            self.len += 1;
            return Ok(());
        }

        if self
            .slow_entries
            .iter()
            .any(|(registered, _)| registered == &key)
        {
            return Err(OpsRegistryError {
                kind: OpsRegistryErrorKind::DuplicateKey,
                key,
            });
        }

        self.slow_entries.push((key, handler));
        self.len += 1;
        Ok(())
    }

    pub fn replace(&mut self, key: OpKey, handler: OpHandler) {
        if let Some(slot) = Self::fast_slot(&key) {
            let was_empty = {
                let target = self.fast_slot_mut(slot);
                let was_empty = target.is_none();
                *target = Some(handler);
                was_empty
            };
            self.rebuild_resolved(slot.op_slot());
            if was_empty {
                self.len += 1;
            }
            return;
        }

        if let Some((_, registered_handler)) = self
            .slow_entries
            .iter_mut()
            .find(|(registered, _)| registered == &key)
        {
            *registered_handler = handler;
            return;
        }
        self.slow_entries.push((key, handler));
        self.len += 1;
    }

    #[inline(always)]
    pub fn resolve(
        &self,
        op: &str,
        filetype: &FileType,
        backend: &BackendKind,
    ) -> Option<OpHandler> {
        let op_slot = OpName::slot_for_name(op);
        let filetype_slot = filetype.slot();
        let backend_slot = backend.slot();

        match (op_slot, self.slow_entries.is_empty()) {
            (Some(op_slot), true) => {
                self.resolve_fast_precomputed(op_slot, filetype_slot, backend_slot)
            }
            (Some(op_slot), false) => {
                let slow = self.resolve_slow(op, filetype, backend);
                self.resolve_fast_exact(op_slot, filetype_slot, backend_slot)
                    .or(slow.exact)
                    .or_else(|| self.resolve_fast_backend(op_slot, backend_slot))
                    .or(slow.backend)
                    .or_else(|| self.resolve_fast_filetype(op_slot, filetype_slot))
                    .or(slow.filetype)
                    .or_else(|| self.resolve_fast_default(op_slot))
                    .or(slow.default)
            }
            (None, false) => self.resolve_slow(op, filetype, backend).best(),
            (None, true) => None,
        }
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[inline(always)]
    fn fast_slot(key: &OpKey) -> Option<FastSlot> {
        let op_slot = key.name.slot()?;
        match (&key.filetype, &key.backend) {
            (Some(filetype), Some(backend)) => {
                Some(FastSlot::Exact(op_slot, filetype.slot()?, backend.slot()?))
            }
            (None, Some(backend)) => Some(FastSlot::Backend(op_slot, backend.slot()?)),
            (Some(filetype), None) => Some(FastSlot::Filetype(op_slot, filetype.slot()?)),
            (None, None) => Some(FastSlot::Default(op_slot)),
        }
    }

    #[inline(always)]
    fn fast_slot_mut(&mut self, slot: FastSlot) -> &mut Option<OpHandler> {
        match slot {
            FastSlot::Exact(op, filetype, backend) => &mut self.exact[op][filetype][backend],
            FastSlot::Backend(op, backend) => &mut self.backend[op][backend],
            FastSlot::Filetype(op, filetype) => &mut self.filetype[op][filetype],
            FastSlot::Default(op) => &mut self.defaults[op],
        }
    }

    fn rebuild_resolved(&mut self, op: usize) {
        for filetype in 0..FILETYPE_SLOTS {
            for backend in 0..BACKEND_SLOTS {
                self.resolved[op][filetype][backend] = self.exact[op][filetype][backend]
                    .or(self.backend[op][backend])
                    .or(self.filetype[op][filetype])
                    .or(self.defaults[op]);
            }
        }
    }

    #[inline(always)]
    fn resolve_fast_precomputed(
        &self,
        op: usize,
        filetype: Option<usize>,
        backend: Option<usize>,
    ) -> Option<OpHandler> {
        match (filetype, backend) {
            (Some(filetype), Some(backend)) => self.resolved[op][filetype][backend],
            _ => self.resolve_fast(op, filetype, backend),
        }
    }

    #[inline(always)]
    fn resolve_fast(
        &self,
        op: usize,
        filetype: Option<usize>,
        backend: Option<usize>,
    ) -> Option<OpHandler> {
        self.resolve_fast_exact(op, filetype, backend)
            .or_else(|| self.resolve_fast_backend(op, backend))
            .or_else(|| self.resolve_fast_filetype(op, filetype))
            .or_else(|| self.resolve_fast_default(op))
    }

    #[inline(always)]
    fn resolve_fast_exact(
        &self,
        op: usize,
        filetype: Option<usize>,
        backend: Option<usize>,
    ) -> Option<OpHandler> {
        let filetype = filetype?;
        let backend = backend?;
        self.exact[op][filetype][backend]
    }

    #[inline(always)]
    fn resolve_fast_backend(&self, op: usize, backend: Option<usize>) -> Option<OpHandler> {
        self.backend[op][backend?]
    }

    #[inline(always)]
    fn resolve_fast_filetype(&self, op: usize, filetype: Option<usize>) -> Option<OpHandler> {
        self.filetype[op][filetype?]
    }

    #[inline(always)]
    fn resolve_fast_default(&self, op: usize) -> Option<OpHandler> {
        self.defaults[op]
    }

    fn resolve_slow(&self, op: &str, filetype: &FileType, backend: &BackendKind) -> SlowMatches {
        let mut matches = SlowMatches::default();

        for (key, handler) in &self.slow_entries {
            if !key.name.matches(op) {
                continue;
            }

            match (&key.filetype, &key.backend) {
                (Some(registered_ft), Some(registered_be))
                    if registered_ft == filetype && registered_be == backend =>
                {
                    matches.exact.get_or_insert(*handler);
                }
                (None, Some(registered_be)) if registered_be == backend => {
                    matches.backend.get_or_insert(*handler);
                }
                (Some(registered_ft), None) if registered_ft == filetype => {
                    matches.filetype.get_or_insert(*handler);
                }
                (None, None) => {
                    matches.default.get_or_insert(*handler);
                }
                _ => {}
            }
        }

        matches
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
    fn op_name_uses_fixed_variants_for_known_ops() {
        assert_eq!(OpName::new("CAT"), OpName::Cat);
        assert_eq!(OpName::new("grep"), OpName::Grep);
        assert_eq!(OpName::new("raw_read"), OpName::RawRead);
        assert_eq!(OpName::new("fingerprint"), OpName::Fingerprint);
    }

    #[test]
    fn resolve_prefers_exact_backend_then_filetype_then_default() {
        let mut registry = OpsRegistry::new();
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
        let mut registry = OpsRegistry::new();
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
    fn custom_filetype_exact_precedes_fast_default() {
        let mut registry = OpsRegistry::new();
        registry
            .register(
                key("cat", None, None),
                OpHandler::Cat(CatHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key(
                    "cat",
                    Some(FileType::Other(Arc::from("csv"))),
                    Some(BackendKind::Local),
                ),
                OpHandler::Cat(CatHandlerKind::ParquetJson),
            )
            .unwrap();

        assert_eq!(
            registry.resolve(
                "cat",
                &FileType::Other(Arc::from("csv")),
                &BackendKind::Local
            ),
            Some(OpHandler::Cat(CatHandlerKind::ParquetJson))
        );
        assert_eq!(
            registry.resolve(
                "cat",
                &FileType::Other(Arc::from("tsv")),
                &BackendKind::Local
            ),
            Some(OpHandler::Cat(CatHandlerKind::Default))
        );
    }

    #[test]
    fn custom_backend_keeps_specificity_order() {
        let mut registry = OpsRegistry::new();
        let warehouse = BackendKind::Other(Arc::from("warehouse"));

        registry
            .register(
                key("grep", Some(FileType::Json), None),
                OpHandler::Grep(GrepHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key("grep", None, Some(warehouse.clone())),
                OpHandler::Grep(GrepHandlerKind::SlackSearch),
            )
            .unwrap();

        assert_eq!(
            registry.resolve("grep", &FileType::Json, &warehouse),
            Some(OpHandler::Grep(GrepHandlerKind::SlackSearch))
        );
    }

    #[test]
    fn duplicate_register_rejects_and_replace_overwrites() {
        let mut registry = OpsRegistry::new();
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

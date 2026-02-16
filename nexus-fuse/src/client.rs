//! Nexus HTTP client for communicating with the Nexus server.
//! Uses JSON-RPC style API.

#![allow(dead_code)]

use crate::error::NexusClientError;
use log::debug;
use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE, IF_NONE_MATCH};
use serde::Deserialize;
use serde_json::{json, Value};

/// User information returned by whoami endpoint.
#[derive(Debug, Deserialize)]
pub struct UserInfo {
    #[serde(alias = "user_id", alias = "subject_id", default)]
    pub user_id: Option<String>,
    #[serde(default)]
    pub tenant_id: Option<String>,
    #[serde(default)]
    pub is_admin: bool,
    #[serde(default)]
    pub user: Option<serde_json::Value>,
}

/// File/directory entry from listing.
#[derive(Debug, Deserialize, Clone)]
pub struct FileEntry {
    pub name: String,
    #[serde(rename = "type")]
    pub entry_type: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

/// File metadata from stat.
#[derive(Debug, Deserialize, Clone)]
pub struct FileMetadata {
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub etag: Option<String>,
    #[serde(default)]
    pub modified_at: Option<String>,
    #[serde(default)]
    pub is_directory: bool,
}

/// JSON-RPC response wrapper.
#[derive(Debug, Deserialize)]
struct JsonRpcResponse<T> {
    #[allow(dead_code)]
    jsonrpc: String,
    #[allow(dead_code)]
    id: Option<Value>,
    result: Option<T>,
    error: Option<JsonRpcError>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i32,
    message: String,
}

/// Response from read operations with ETag support.
#[derive(Debug)]
pub enum ReadResponse {
    /// Content was returned (possibly with ETag for caching).
    Content {
        content: Vec<u8>,
        etag: Option<String>,
    },
    /// Content not modified (304 response).
    NotModified,
}

/// Nexus HTTP client.
pub struct NexusClient {
    client: Client,
    base_url: String,
    api_key: String,
    agent_id: Option<String>,
}

impl NexusClient {
    /// Create a new Nexus client.
    pub fn new(base_url: &str, api_key: &str, agent_id: Option<String>) -> Result<Self, NexusClientError> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .connect_timeout(std::time::Duration::from_secs(5))
            .build()?;

        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            agent_id,
        })
    }

    /// Map HTTP status code to NexusClientError.
    fn status_to_error(status: reqwest::StatusCode, body: String) -> NexusClientError {
        match status.as_u16() {
            404 => NexusClientError::NotFound(body),
            429 => NexusClientError::RateLimited,
            500..=599 => NexusClientError::ServerError {
                status: status.as_u16(),
                message: body,
            },
            _ => NexusClientError::ServerError {
                status: status.as_u16(),
                message: body,
            },
        }
    }

    /// Build headers for requests.
    fn headers(&self) -> HeaderMap {
        let mut headers = HeaderMap::new();
        // Note: HeaderValue::from_str can fail on non-ASCII characters
        // In practice, API keys and agent IDs should be ASCII-safe
        if let Ok(auth_value) = HeaderValue::from_str(&format!("Bearer {}", self.api_key)) {
            headers.insert(AUTHORIZATION, auth_value);
        }
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        if let Some(ref agent_id) = self.agent_id {
            if let Ok(agent_value) = HeaderValue::from_str(agent_id) {
                headers.insert("X-Nexus-Agent-Id", agent_value);
            }
        }
        headers
    }

    /// Call a JSON-RPC method.
    fn rpc_call<T: for<'de> Deserialize<'de>>(&self, method: &str, params: Value) -> Result<T, NexusClientError> {
        let url = format!("{}/api/nfs/{}", self.base_url, method);

        // Build proper JSON-RPC request
        let rpc_request = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        });
        debug!("POST {} {:?}", url, rpc_request);

        let resp = self
            .client
            .post(&url)
            .headers(self.headers())
            .json(&rpc_request)
            .send()?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        let rpc_resp: JsonRpcResponse<T> = resp.json()?;

        if let Some(err) = rpc_resp.error {
            if err.message.contains("not found") || err.message.contains("Not Found") {
                return Err(NexusClientError::NotFound(err.message));
            }
            return Err(NexusClientError::InvalidResponse(format!(
                "RPC error {}: {}",
                err.code, err.message
            )));
        }

        rpc_resp
            .result
            .ok_or_else(|| NexusClientError::InvalidResponse("no result in response".to_string()))
    }

    /// Get current user info.
    pub fn whoami(&self) -> Result<UserInfo, NexusClientError> {
        let url = format!("{}/api/auth/whoami", self.base_url);
        debug!("GET {}", url);

        let resp = self
            .client
            .get(&url)
            .headers(self.headers())
            .send()?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        Ok(resp.json()?)
    }

    /// List directory contents.
    pub fn list(&self, path: &str) -> Result<Vec<FileEntry>, NexusClientError> {
        // Use details=true, recursive=false to get entry types from server
        #[derive(Deserialize)]
        struct DetailedEntry {
            path: String,
            #[serde(default)]
            is_directory: bool,
            #[serde(default)]
            size: u64,
            // These fields have complex nested types from the API, so we ignore them
            #[serde(default)]
            modified_at: serde_json::Value,
            #[serde(default)]
            created_at: serde_json::Value,
        }

        #[derive(Deserialize)]
        struct ListResult {
            files: Vec<DetailedEntry>,
        }

        let result: ListResult = self.rpc_call("list", json!({
            "path": path,
            "recursive": false,
            "details": true
        }))?;

        // Convert to FileEntry objects - extract immediate children only
        let parent_prefix = if path == "/" { "/" } else { path };
        let mut seen_names = std::collections::HashSet::new();

        let entries = result.files.iter().filter_map(|entry| {
            // Strip parent prefix to get relative path
            let relative = if path == "/" {
                entry.path.strip_prefix('/').unwrap_or(&entry.path)
            } else {
                entry.path.strip_prefix(parent_prefix)
                    .and_then(|s| s.strip_prefix('/'))
                    .unwrap_or(&entry.path)
            };

            // Get immediate child (first path component)
            let name = relative.split('/').next()?.to_string();
            if name.is_empty() {
                return None;
            }

            // Deduplicate (same directory may appear multiple times from nested files)
            if !seen_names.insert(name.clone()) {
                return None;
            }

            // If there are more path components, this is a directory
            let is_nested = relative.contains('/');
            let entry_type = if is_nested || entry.is_directory {
                "directory".to_string()
            } else {
                "file".to_string()
            };

            Some(FileEntry {
                name,
                entry_type,
                size: if is_nested { 0 } else { entry.size },
                created_at: None,  // Complex type from API, not used
                updated_at: None,  // Complex type from API, not used
            })
        }).collect();

        Ok(entries)
    }

    /// Get file/directory metadata.
    pub fn stat(&self, path: &str) -> Result<FileMetadata, NexusClientError> {
        self.rpc_call("stat", json!({"path": path}))
    }

    /// Read file contents.
    pub fn read(&self, path: &str) -> Result<Vec<u8>, NexusClientError> {
        match self.read_with_etag(path, None)? {
            ReadResponse::Content { content, .. } => Ok(content),
            ReadResponse::NotModified => Err(NexusClientError::InvalidResponse("Unexpected 304 without etag".to_string())),
        }
    }

    /// Read file contents with ETag support for conditional requests.
    /// If `if_none_match` is provided and content hasn't changed, returns NotModified.
    pub fn read_with_etag(&self, path: &str, if_none_match: Option<&str>) -> Result<ReadResponse, NexusClientError> {
        use base64::{engine::general_purpose::STANDARD, Engine};

        let url = format!("{}/api/nfs/read", self.base_url);

        let rpc_request = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "read",
            "params": {"path": path}
        });

        let mut headers = self.headers();
        if let Some(etag) = if_none_match {
            headers.insert(
                IF_NONE_MATCH,
                HeaderValue::from_str(&format!("\"{}\"", etag)).unwrap(),
            );
        }

        debug!("POST {} (etag: {:?})", url, if_none_match);

        let resp = self
            .client
            .post(&url)
            .headers(headers)
            .json(&rpc_request)
            .send()?;

        // Handle 304 Not Modified
        if resp.status().as_u16() == 304 {
            debug!("Server returned 304 Not Modified for {}", path);
            return Ok(ReadResponse::NotModified);
        }

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().unwrap_or_default();
            if status.as_u16() == 404 || text.contains("not found") || text.contains("Not Found") {
                return Err(NexusClientError::NotFound("not found".to_string()));
            }
            return Err(Self::status_to_error(status, text));
        }

        // Extract ETag from response headers
        let etag = resp
            .headers()
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim_matches('"').to_string());

        // API returns {"__type__":"bytes","data":"base64..."} format
        #[derive(Deserialize)]
        struct BytesResult {
            #[serde(rename = "__type__")]
            type_tag: String,
            data: String, // base64 encoded
        }

        #[derive(Deserialize)]
        struct JsonRpcReadResponse {
            result: Option<BytesResult>,
            error: Option<JsonRpcError>,
        }

        let rpc_resp: JsonRpcReadResponse = resp.json()?;

        if let Some(err) = rpc_resp.error {
            if err.message.contains("not found") || err.message.contains("Not Found")
                || err.message.contains("CAS content not found") {
                return Err(NexusClientError::NotFound(err.message));
            }
            return Err(NexusClientError::InvalidResponse(format!(
                "RPC error {}: {}",
                err.code, err.message
            )));
        }

        let result = rpc_resp.result.ok_or_else(|| NexusClientError::InvalidResponse("no result in response".to_string()))?;
        let content = STANDARD
            .decode(&result.data)
            .map_err(|e| NexusClientError::InvalidResponse(format!("base64 decode error: {}", e)))?;

        Ok(ReadResponse::Content { content, etag })
    }

    /// Write file contents.
    pub fn write(&self, path: &str, content: &[u8]) -> Result<(), NexusClientError> {
        use base64::{engine::general_purpose::STANDARD, Engine};

        // API expects {"__type__": "bytes", "data": "base64..."} format
        let _: Value = self.rpc_call("write", json!({
            "path": path,
            "content": {
                "__type__": "bytes",
                "data": STANDARD.encode(content)
            }
        }))?;
        Ok(())
    }

    /// Create directory.
    pub fn mkdir(&self, path: &str) -> Result<(), NexusClientError> {
        let _: Value = self.rpc_call("mkdir", json!({"path": path}))?;
        Ok(())
    }

    /// Delete file or directory.
    pub fn delete(&self, path: &str) -> Result<(), NexusClientError> {
        let _: Value = self.rpc_call("delete", json!({"path": path}))?;
        Ok(())
    }

    /// Rename/move file or directory.
    pub fn rename(&self, old_path: &str, new_path: &str) -> Result<(), NexusClientError> {
        let _: Value = self.rpc_call("rename", json!({
            "old_path": old_path,
            "new_path": new_path
        }))?;
        Ok(())
    }

    /// Check if path exists.
    pub fn exists(&self, path: &str) -> bool {
        #[derive(Deserialize)]
        struct ExistsResult {
            exists: bool,
        }

        match self.rpc_call::<ExistsResult>("exists", json!({"path": path})) {
            Ok(result) => result.exists,
            Err(_) => false,
        }
    }
}

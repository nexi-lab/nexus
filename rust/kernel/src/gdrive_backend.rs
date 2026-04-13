//! Google Drive Connector — pure Rust ObjectStore via reqwest + Drive REST v3 (§10 D4).
//!
//! Implements ObjectStore for Google Drive using the Drive REST API v3.
//! Auth: OAuth2 access token (same infrastructure as GCS).
//! Multipart upload for writes, media download for reads.
//!
//! `add_mount(backend_type="gdrive")`

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::io;

const DRIVE_API: &str = "https://www.googleapis.com/drive/v3";
const UPLOAD_API: &str = "https://www.googleapis.com/upload/drive/v3";

/// Google Drive backend.
pub(crate) struct GDriveBackend {
    backend_name: String,
    access_token: parking_lot::RwLock<String>,
    /// Root folder ID (Drive uses folder IDs, not paths).
    root_folder_id: String,
    runtime: tokio::runtime::Runtime,
}

impl GDriveBackend {
    pub(crate) fn new(
        name: &str,
        access_token: &str,
        root_folder_id: &str,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            access_token: parking_lot::RwLock::new(access_token.to_string()),
            root_folder_id: root_folder_id.to_string(),
            runtime,
        })
    }

    pub(crate) fn set_access_token(&self, token: &str) {
        *self.access_token.write() = token.to_string();
    }

    fn token(&self) -> String {
        self.access_token.read().clone()
    }

    /// Find file by name in root folder. Returns file ID if found.
    fn find_file(&self, name: &str) -> Result<Option<String>, StorageError> {
        let token = self.token();
        let query = format!(
            "name='{}' and '{}' in parents and trashed=false",
            name.replace('\'', "\\'"),
            self.root_folder_id
        );
        let url = format!(
            "{}/files?q={}&fields=files(id,name)",
            DRIVE_API,
            urlencoding::encode(&query)
        );

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .get(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive list: {e}"))))?;
            let body: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive JSON: {e}"))))?;
            Ok(body
                .get("files")
                .and_then(|f| f.get(0))
                .and_then(|f| f.get("id"))
                .and_then(|id| id.as_str())
                .map(|s| s.to_string()))
        })
    }
}

impl ObjectStore for GDriveBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<WriteResult, StorageError> {
        let token = self.token();
        let size = content.len() as u64;
        let file_name = if content_id.is_empty() {
            blake3::hash(content).to_hex().to_string()
        } else {
            content_id.to_string()
        };

        // Simple upload (< 5MB) via multipart
        let metadata = serde_json::json!({
            "name": file_name,
            "parents": [self.root_folder_id],
        });
        let url = format!(
            "{}/files?uploadType=multipart&fields=id,name,size",
            UPLOAD_API
        );
        let content_owned = content.to_vec();

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let boundary = "nexus_multipart_boundary";
            let mut body = Vec::new();
            body.extend_from_slice(
                format!("--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n")
                    .as_bytes(),
            );
            body.extend_from_slice(metadata.to_string().as_bytes());
            body.extend_from_slice(
                format!("\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n")
                    .as_bytes(),
            );
            body.extend_from_slice(&content_owned);
            body.extend_from_slice(format!("\r\n--{boundary}--").as_bytes());

            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .header(
                    "Content-Type",
                    format!("multipart/related; boundary={boundary}"),
                )
                .body(body)
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive PUT: {e}"))))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Drive PUT {status}: {text}"
                ))));
            }

            let result: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive JSON: {e}"))))?;
            let file_id = result
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or(&file_name)
                .to_string();

            Ok(WriteResult {
                content_id: file_id,
                version: file_name,
                size,
            })
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let token = self.token();
        // content_id is file ID for Drive
        let url = format!("{}/files/{}?alt=media", DRIVE_API, content_id);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .get(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive GET: {e}"))))?;
            if resp.status().as_u16() == 404 {
                return Err(StorageError::NotFound(content_id.to_string()));
            }
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Drive GET {status}: {body}"
                ))));
            }
            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Drive read: {e}"))))
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let token = self.token();
        let url = format!("{}/files/{}", DRIVE_API, content_id);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .delete(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("Drive DELETE: {e}")))
                })?;
            if !resp.status().is_success() && resp.status().as_u16() != 404 {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Drive DELETE {status}: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        let token = self.token();
        let metadata = serde_json::json!({
            "name": path.split('/').next_back().unwrap_or(path),
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self.root_folder_id],
        });
        let url = format!("{}/files", DRIVE_API);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .json(&metadata)
                .send()
                .await
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("Drive mkdir: {e}")))
                })?;
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Drive mkdir {status}: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn rmdir(&self, path: &str, _recursive: bool) -> Result<(), StorageError> {
        // Find folder by name, then delete
        if let Some(folder_id) = self.find_file(path.split('/').next_back().unwrap_or(path))? {
            self.delete_content(&folder_id)?;
        }
        Ok(())
    }
}

mod urlencoding {
    pub fn encode(s: &str) -> String {
        let mut result = String::with_capacity(s.len() * 3);
        for b in s.bytes() {
            match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    result.push(b as char);
                }
                _ => {
                    result.push('%');
                    result.push_str(&format!("{b:02X}"));
                }
            }
        }
        result
    }
}

//! GCS Connector — pure Rust ObjectStore impl via reqwest + OAuth2 (§10 D2).
//!
//! Implements ObjectStore for Google Cloud Storage using the JSON API.
//! Auth: OAuth2 access token (injected at mount time from Python credential store).
//!
//! `add_mount(backend_type="gcs", gcs_bucket="...", gcs_prefix="...")`

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::io;

/// Google Cloud Storage backend.
pub(crate) struct GcsBackend {
    backend_name: String,
    bucket: String,
    prefix: String,
    access_token: parking_lot::RwLock<String>,
    runtime: tokio::runtime::Runtime,
}

impl GcsBackend {
    pub(crate) fn new(
        name: &str,
        bucket: &str,
        prefix: &str,
        access_token: &str,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            bucket: bucket.to_string(),
            prefix: prefix.trim_matches('/').to_string(),
            access_token: parking_lot::RwLock::new(access_token.to_string()),
            runtime,
        })
    }

    fn object_name(&self, content_id: &str) -> String {
        if self.prefix.is_empty() {
            content_id.to_string()
        } else {
            format!("{}/{}", self.prefix, content_id)
        }
    }

    /// Refresh access token (called from Python when token expires).
    pub(crate) fn set_access_token(&self, token: &str) {
        *self.access_token.write() = token.to_string();
    }

    fn token(&self) -> String {
        self.access_token.read().clone()
    }
}

impl ObjectStore for GcsBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn is_external(&self) -> bool {
        true
    }

    fn is_remote(&self) -> bool {
        true
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            // R20.10: GCS object upload replaces the whole object; no
            // native pwrite equivalent (resumable uploads are for append-
            // style streaming, not seekable writes).
            return Err(StorageError::NotSupported(
                "gcs backend does not support offset writes (API limitation)",
            ));
        }
        let object_name = self.object_name(content_id);
        let encoded = urlencoding::encode(&object_name);
        let url = format!(
            "https://storage.googleapis.com/upload/storage/v1/b/{}/o?uploadType=media&name={}",
            self.bucket, encoded
        );
        let token = self.token();
        let content_owned = content.to_vec();
        let size = content.len() as u64;

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .header("Content-Type", "application/octet-stream")
                .body(content_owned)
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("GCS PUT: {e}"))))?;
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "GCS PUT {status}: {body}"
                ))));
            }
            Ok(WriteResult {
                content_id: content_id.to_string(),
                version: content_id.to_string(),
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
        let object_name = self.object_name(content_id);
        let encoded = urlencoding::encode(&object_name);
        let url = format!(
            "https://storage.googleapis.com/storage/v1/b/{}/o/{}?alt=media",
            self.bucket, encoded
        );
        let token = self.token();

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .get(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("GCS GET: {e}"))))?;
            if resp.status().as_u16() == 404 {
                return Err(StorageError::NotFound(content_id.to_string()));
            }
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "GCS GET {status}: {body}"
                ))));
            }
            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| StorageError::IOError(io::Error::other(format!("GCS read: {e}"))))
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let object_name = self.object_name(content_id);
        let encoded = urlencoding::encode(&object_name);
        let url = format!(
            "https://storage.googleapis.com/storage/v1/b/{}/o/{}",
            self.bucket, encoded
        );
        let token = self.token();

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .delete(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("GCS DELETE: {e}"))))?;
            if !resp.status().is_success() && resp.status().as_u16() != 404 {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "GCS DELETE {status}: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        Ok(()) // GCS prefixes are virtual
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        Ok(())
    }
}

/// Inline URL encoding (avoiding extra dependency).
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

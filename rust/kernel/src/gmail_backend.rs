//! Gmail Connector — pure Rust ObjectStore via reqwest + Gmail REST v1 (§10 D5).
//!
//! Implements ObjectStore for Gmail using the Gmail REST API v1.
//! Auth: OAuth2 access token (same infrastructure as GDrive/GCS).
//! Messages are stored/read as RFC 2822 raw format.
//!
//! `add_mount(backend_type="gmail")`

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::io;

const GMAIL_API: &str = "https://gmail.googleapis.com/gmail/v1/users/me";

/// Gmail backend — message read/write/delete via REST API.
pub(crate) struct GmailBackend {
    backend_name: String,
    access_token: parking_lot::RwLock<String>,
    runtime: tokio::runtime::Runtime,
}

impl GmailBackend {
    pub(crate) fn new(name: &str, access_token: &str) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            access_token: parking_lot::RwLock::new(access_token.to_string()),
            runtime,
        })
    }

    pub(crate) fn set_access_token(&self, token: &str) {
        *self.access_token.write() = token.to_string();
    }

    fn token(&self) -> String {
        self.access_token.read().clone()
    }
}

impl ObjectStore for GmailBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        _content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "gmail backend does not support offset writes (messages are immutable)",
            ));
        }
        // Send a new message (content is RFC 2822 raw email or JSON)
        let token = self.token();
        let url = format!("{}/messages/send", GMAIL_API);
        let size = content.len() as u64;

        // Base64url-encode the raw message
        use base64::Engine;
        let encoded = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(content);
        let body = serde_json::json!({ "raw": encoded });

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .json(&body)
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Gmail send: {e}"))))?;
            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Gmail send {status}: {text}"
                ))));
            }
            let result: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Gmail JSON: {e}"))))?;
            let msg_id = result
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Ok(WriteResult {
                content_id: msg_id,
                version: String::new(),
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
        // Get message in full format (JSON with payload)
        let url = format!("{}/messages/{}?format=full", GMAIL_API, content_id);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .get(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Gmail GET: {e}"))))?;
            if resp.status().as_u16() == 404 {
                return Err(StorageError::NotFound(content_id.to_string()));
            }
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Gmail GET {status}: {body}"
                ))));
            }
            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Gmail read: {e}"))))
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let token = self.token();
        // Trash (not permanent delete)
        let url = format!("{}/messages/{}/trash", GMAIL_API, content_id);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("Gmail trash: {e}")))
                })?;
            if !resp.status().is_success() && resp.status().as_u16() != 404 {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Gmail trash {status}: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // Gmail uses labels, not directories. Label creation via separate API.
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        Ok(())
    }
}

/// Base64 URL-safe encoding (no padding) — minimal inline impl.
mod base64 {
    pub mod engine {
        pub mod general_purpose {
            pub const URL_SAFE_NO_PAD: UrlSafeNoPad = UrlSafeNoPad;

            pub struct UrlSafeNoPad;
        }
    }
    pub trait Engine {
        fn encode(&self, input: &[u8]) -> String;
    }
    impl Engine for engine::general_purpose::UrlSafeNoPad {
        fn encode(&self, input: &[u8]) -> String {
            const CHARS: &[u8; 64] =
                b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let mut result = String::with_capacity(input.len().div_ceil(3) * 4);
            for chunk in input.chunks(3) {
                let b0 = chunk[0] as u32;
                let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
                let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
                let triple = (b0 << 16) | (b1 << 8) | b2;
                result.push(CHARS[((triple >> 18) & 0x3F) as usize] as char);
                result.push(CHARS[((triple >> 12) & 0x3F) as usize] as char);
                if chunk.len() > 1 {
                    result.push(CHARS[((triple >> 6) & 0x3F) as usize] as char);
                }
                if chunk.len() > 2 {
                    result.push(CHARS[(triple & 0x3F) as usize] as char);
                }
            }
            result
        }
    }
}

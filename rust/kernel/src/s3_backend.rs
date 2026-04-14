//! S3 Connector — pure Rust ObjectStore impl via reqwest + AWS Sigv4 (§10 D1).
//!
//! Implements ObjectStore trait for Amazon S3 (and compatible: MinIO, R2, etc.).
//! Auth: AWS credentials chain (env vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY,
//! or IAM role via instance metadata).
//!
//! `add_mount(backend_type="s3", s3_bucket="...", s3_prefix="...", aws_region="...")`

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use std::io;

type HmacSha256 = Hmac<Sha256>;

/// S3-compatible object storage backend.
pub(crate) struct S3Backend {
    backend_name: String,
    bucket: String,
    prefix: String,
    region: String,
    access_key: String,
    secret_key: String,
    endpoint: Option<String>,
    runtime: tokio::runtime::Runtime,
}

impl S3Backend {
    pub(crate) fn new(
        name: &str,
        bucket: &str,
        prefix: &str,
        region: &str,
        access_key: &str,
        secret_key: &str,
        endpoint: Option<&str>,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            bucket: bucket.to_string(),
            prefix: prefix.trim_matches('/').to_string(),
            region: region.to_string(),
            access_key: access_key.to_string(),
            secret_key: secret_key.to_string(),
            endpoint: endpoint.map(|s| s.to_string()),
            runtime,
        })
    }

    fn object_key(&self, content_id: &str) -> String {
        if self.prefix.is_empty() {
            content_id.to_string()
        } else {
            format!("{}/{}", self.prefix, content_id)
        }
    }

    fn base_url(&self) -> String {
        self.endpoint
            .clone()
            .unwrap_or_else(|| format!("https://{}.s3.{}.amazonaws.com", self.bucket, self.region))
    }

    /// AWS Sigv4 signing for S3 requests.
    fn sign_request(
        &self,
        method: &str,
        path: &str,
        content_sha256: &str,
        now: &chrono::DateTime<chrono::Utc>,
    ) -> Vec<(String, String)> {
        let date_stamp = now.format("%Y%m%d").to_string();
        let amz_date = now.format("%Y%m%dT%H%M%SZ").to_string();
        let credential_scope = format!("{}/{}/s3/aws4_request", date_stamp, self.region);

        let canonical_headers = format!(
            "host:{}.s3.{}.amazonaws.com\nx-amz-content-sha256:{}\nx-amz-date:{}\n",
            self.bucket, self.region, content_sha256, amz_date
        );
        let signed_headers = "host;x-amz-content-sha256;x-amz-date";

        let canonical_request = format!(
            "{}\n{}\n\n{}\n{}\n{}",
            method, path, canonical_headers, signed_headers, content_sha256
        );

        let string_to_sign = format!(
            "AWS4-HMAC-SHA256\n{}\n{}\n{}",
            amz_date,
            credential_scope,
            hex::encode(Sha256::digest(canonical_request.as_bytes()))
        );

        // Derive signing key
        let k_date = hmac_sha256(
            format!("AWS4{}", self.secret_key).as_bytes(),
            date_stamp.as_bytes(),
        );
        let k_region = hmac_sha256(&k_date, self.region.as_bytes());
        let k_service = hmac_sha256(&k_region, b"s3");
        let k_signing = hmac_sha256(&k_service, b"aws4_request");
        let signature = hex::encode(hmac_sha256(&k_signing, string_to_sign.as_bytes()));

        let auth = format!(
            "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}",
            self.access_key, credential_scope, signed_headers, signature
        );

        vec![
            ("Authorization".to_string(), auth),
            ("x-amz-date".to_string(), amz_date),
            (
                "x-amz-content-sha256".to_string(),
                content_sha256.to_string(),
            ),
        ]
    }
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC key");
    mac.update(data);
    mac.finalize().into_bytes().to_vec()
}

mod hex {
    pub fn encode(bytes: impl AsRef<[u8]>) -> String {
        bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
    }
}

impl ObjectStore for S3Backend {
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
            // R20.10: S3 PutObject replaces the whole object; there is no
            // native pwrite(2) equivalent. A read-splice-PUT fallback
            // would silently turn O(content.len()) writes into O(blob)
            // network I/O — never acceptable for a file-system surface.
            // Caller should use CAS or a local PAS mount when partial
            // writes are required.
            return Err(StorageError::NotSupported(
                "s3 backend does not support offset writes (API limitation — use CAS or local PAS)",
            ));
        }
        let key = self.object_key(content_id);
        let url = format!("{}/{}", self.base_url(), key);
        let content_sha256 = hex::encode(Sha256::digest(content));
        let now = chrono::Utc::now();
        let headers = self.sign_request("PUT", &format!("/{key}"), &content_sha256, &now);
        let content_owned = content.to_vec();
        let size = content.len() as u64;

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let mut req = client.put(&url).body(content_owned);
            for (k, v) in &headers {
                req = req.header(k.as_str(), v.as_str());
            }
            let resp = req
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("S3 PUT: {e}"))))?;
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "S3 PUT {status}: {body}"
                ))));
            }
            Ok(WriteResult {
                content_id: content_id.to_string(),
                version: content_sha256,
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
        let key = self.object_key(content_id);
        let url = format!("{}/{}", self.base_url(), key);
        let now = chrono::Utc::now();
        let headers = self.sign_request("GET", &format!("/{key}"), "UNSIGNED-PAYLOAD", &now);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let mut req = client.get(&url);
            for (k, v) in &headers {
                req = req.header(k.as_str(), v.as_str());
            }
            let resp = req
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("S3 GET: {e}"))))?;
            if resp.status().as_u16() == 404 {
                return Err(StorageError::NotFound(content_id.to_string()));
            }
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "S3 GET {status}: {body}"
                ))));
            }
            resp.bytes()
                .await
                .map(|b| b.to_vec())
                .map_err(|e| StorageError::IOError(io::Error::other(format!("S3 read: {e}"))))
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let key = self.object_key(content_id);
        let url = format!("{}/{}", self.base_url(), key);
        let now = chrono::Utc::now();
        let headers = self.sign_request("DELETE", &format!("/{key}"), "UNSIGNED-PAYLOAD", &now);

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let mut req = client.delete(&url);
            for (k, v) in &headers {
                req = req.header(k.as_str(), v.as_str());
            }
            let resp = req
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("S3 DELETE: {e}"))))?;
            if !resp.status().is_success() && resp.status().as_u16() != 404 {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "S3 DELETE {status}: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // S3 has no real directories — prefixes are virtual
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        // S3 has no real directories
        Ok(())
    }
}

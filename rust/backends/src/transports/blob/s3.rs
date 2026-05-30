//! S3 Connector — pure Rust ObjectStore impl via reqwest + AWS Sigv4 (§10 D1).
//!
//! Implements ObjectStore trait for Amazon S3 (and compatible: MinIO, R2, etc.).
//! Auth: AWS credentials chain (env vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY,
//! or IAM role via instance metadata).
//!
//! `add_mount(backend_type="s3", s3_bucket="...", s3_prefix="...", aws_region="...")`

#![allow(dead_code)]

use hmac::{Hmac, KeyInit, Mac};
use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::OperationContext;
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

    /// Request path (also the SigV4 canonical URI) for `key`.
    ///
    /// - **Path-style** (`/{bucket}/{key}`) for an *account-style* custom
    ///   endpoint (S3-compatible Cloudflare R2 / MinIO / Tencent COS:
    ///   `acct.r2.cloudflarestorage.com`, `minio.local:9000`) whose host has
    ///   no bucket — else the request hits the account root and R2/MinIO
    ///   return 404 / `NoSuchBucket` / a SigV4 signature mismatch.
    /// - **Virtual-hosted** (`/{key}`) when the bucket is already in the host:
    ///   AWS (no endpoint, `{bucket}.s3…`) and bucket-scoped custom endpoints
    ///   (`{bucket}.cos…`). Prepending the bucket there would address
    ///   `/{bucket}/{bucket}/…`, the wrong object.
    ///
    /// The URL and the signed canonical path MUST use this same value or the
    /// signature is invalid.
    fn request_path(&self, key: &str) -> String {
        // Percent-encode each `/`-delimited segment (RFC 3986) and use the
        // SAME string for the request URL and the SigV4 canonical URI.
        // `content_id` keys are hex (already safe), but an operator-set
        // `prefix` — or any non-hex key — may carry bytes the HTTP client and
        // the signer would otherwise encode differently, breaking the
        // signature or addressing the wrong object. `/` stays a separator.
        let enc_key = encode_s3_path(key);
        // Path-style ONLY when the bucket is not already in the endpoint
        // host. R2 / MinIO account endpoints (`acct.r2.cloudflarestorage.com`,
        // `minio.local:9000`) carry no bucket → prepend `/{bucket}/`. A
        // bucket-scoped (virtual-hosted) custom endpoint already has the
        // bucket in the host (`mybucket.cos…`, matched by `host()`), and AWS
        // (no endpoint) is virtual-hosted too — prepending the bucket there
        // would address `/{bucket}/{bucket}/…`, the wrong object.
        let bucket_in_host = self.host().starts_with(&format!("{}.", self.bucket));
        if self.endpoint.is_some() && !bucket_in_host {
            format!("/{}/{}", s3_uri_encode(&self.bucket), enc_key)
        } else {
            format!("/{enc_key}")
        }
    }

    /// Canonical host for SigV4 signing (must match the ``Host`` header of
    /// the actual request, otherwise the signature is invalid).
    ///
    /// - With a custom endpoint (S3-compatible providers like Tencent COS,
    ///   MinIO, Cloudflare R2): parses the host portion out of the URL.
    /// - Without an endpoint: defaults to AWS virtual-hosted-style host.
    fn host(&self) -> String {
        if let Some(ref ep) = self.endpoint {
            let after_scheme = ep
                .strip_prefix("https://")
                .or_else(|| ep.strip_prefix("http://"))
                .unwrap_or(ep);
            match after_scheme.find('/') {
                Some(i) => after_scheme[..i].to_string(),
                None => after_scheme.to_string(),
            }
        } else {
            format!("{}.s3.{}.amazonaws.com", self.bucket, self.region)
        }
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
            "host:{}\nx-amz-content-sha256:{}\nx-amz-date:{}\n",
            self.host(),
            content_sha256,
            amz_date
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

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            // S3 PutObject replaces the whole object; there is no
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
        let path = self.request_path(&key);
        let url = format!("{}{}", self.base_url().trim_end_matches('/'), path);
        let content_sha256 = hex::encode(Sha256::digest(content));
        let now = chrono::Utc::now();
        let headers = self.sign_request("PUT", &path, &content_sha256, &now);
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
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let key = self.object_key(content_id);
        let path = self.request_path(&key);
        let url = format!("{}{}", self.base_url().trim_end_matches('/'), path);
        let now = chrono::Utc::now();
        let headers = self.sign_request("GET", &path, "UNSIGNED-PAYLOAD", &now);

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
        let path = self.request_path(&key);
        let url = format!("{}{}", self.base_url().trim_end_matches('/'), path);
        let now = chrono::Utc::now();
        let headers = self.sign_request("DELETE", &path, "UNSIGNED-PAYLOAD", &now);

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

/// Percent-encode an S3 object-key path: encode each `/`-delimited segment
/// with [`s3_uri_encode`], preserving `/` as the separator. The result is
/// used verbatim as both the request URL path and the SigV4 canonical URI,
/// so the two always agree.
fn encode_s3_path(key: &str) -> String {
    key.split('/')
        .map(s3_uri_encode)
        .collect::<Vec<_>>()
        .join("/")
}

/// S3-safe URI encoding (RFC 3986 unreserved chars only).
fn s3_uri_encode(s: &str) -> String {
    let mut result = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                result.push(b as char);
            }
            _ => {
                result.push_str(&format!("%{b:02X}"));
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mk(endpoint: Option<&str>) -> S3Backend {
        S3Backend::new(
            "test",
            "mybucket",
            "",
            "us-east-1",
            "AKIA",
            "secret",
            endpoint,
        )
        .unwrap()
    }

    #[test]
    fn host_aws_default() {
        let b = mk(None);
        assert_eq!(b.host(), "mybucket.s3.us-east-1.amazonaws.com");
    }

    #[test]
    fn host_custom_endpoint_https() {
        let b = mk(Some("https://mybucket.cos.ap-beijing.myqcloud.com"));
        assert_eq!(b.host(), "mybucket.cos.ap-beijing.myqcloud.com");
    }

    #[test]
    fn host_custom_endpoint_http() {
        let b = mk(Some("http://minio.local:9000"));
        assert_eq!(b.host(), "minio.local:9000");
    }

    #[test]
    fn host_custom_endpoint_with_trailing_path_stripped() {
        let b = mk(Some("https://example.com/some/path"));
        assert_eq!(b.host(), "example.com");
    }

    #[test]
    fn host_custom_endpoint_no_scheme() {
        let b = mk(Some("bucket.cos.example/path"));
        assert_eq!(b.host(), "bucket.cos.example");
    }

    #[test]
    fn signed_headers_use_canonical_host() {
        // Canonical host in SigV4 must match what we'll send as ``Host`` —
        // for COS that is the endpoint host, not ``*.amazonaws.com``.
        let b = mk(Some("https://mybucket.cos.ap-beijing.myqcloud.com"));
        let now = chrono::DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let headers = b.sign_request("GET", "/key", "UNSIGNED-PAYLOAD", &now);
        let auth = headers.iter().find(|(k, _)| k == "Authorization").unwrap();
        // The signature itself is opaque, but the SignedHeaders list must
        // include ``host`` and the canonical-headers block (rebuilt during
        // verification by the server) must use the COS host. This test
        // pins the regression behavior — if someone re-introduces a
        // hardcoded ``amazonaws.com`` in canonical_headers, the produced
        // signature changes and the test below catches it.
        assert!(auth
            .1
            .contains("SignedHeaders=host;x-amz-content-sha256;x-amz-date"));

        // Re-sign with the same inputs but no endpoint and assert the
        // signature differs — proves canonical_headers actually depends on
        // host().
        let b_aws = mk(None);
        let headers_aws = b_aws.sign_request("GET", "/key", "UNSIGNED-PAYLOAD", &now);
        let auth_aws = headers_aws
            .iter()
            .find(|(k, _)| k == "Authorization")
            .unwrap();
        let sig = |a: &str| {
            a.split("Signature=")
                .nth(1)
                .map(|s| s.trim().to_string())
                .unwrap()
        };
        assert_ne!(sig(&auth.1), sig(&auth_aws.1));
    }

    #[test]
    fn request_path_custom_endpoint_is_path_style_with_bucket() {
        // R2 / MinIO: the endpoint host carries no bucket, so the bucket MUST
        // appear in the request path (and the SigV4 canonical path), else the
        // request hits the account root → 404 / NoSuchBucket / bad signature.
        let b = mk(Some("https://acct.r2.cloudflarestorage.com"));
        assert_eq!(b.request_path("blob123"), "/mybucket/blob123");
        let url = format!(
            "{}{}",
            b.base_url().trim_end_matches('/'),
            b.request_path("blob123")
        );
        assert_eq!(
            url,
            "https://acct.r2.cloudflarestorage.com/mybucket/blob123"
        );
    }

    #[test]
    fn request_path_bucket_scoped_endpoint_stays_virtual_hosted() {
        // A virtual-hosted custom endpoint already carries the bucket in the
        // host (`host()` == "mybucket.cos…"), so the path must NOT prepend the
        // bucket again — that would address `/mybucket/mybucket/key`.
        let b = mk(Some("https://mybucket.cos.ap-beijing.myqcloud.com"));
        assert_eq!(b.request_path("blob123"), "/blob123");
        let url = format!(
            "{}{}",
            b.base_url().trim_end_matches('/'),
            b.request_path("blob123")
        );
        assert_eq!(url, "https://mybucket.cos.ap-beijing.myqcloud.com/blob123");
    }

    #[test]
    fn request_path_aws_is_virtual_hosted_no_bucket_in_path() {
        // AWS virtual-hosted style carries the bucket in the host, so the
        // path is just the key.
        let b = mk(None);
        assert_eq!(b.request_path("blob123"), "/blob123");
        let url = format!(
            "{}{}",
            b.base_url().trim_end_matches('/'),
            b.request_path("blob123")
        );
        assert_eq!(url, "https://mybucket.s3.us-east-1.amazonaws.com/blob123");
    }

    #[test]
    fn request_path_percent_encodes_segments_preserving_slash() {
        // Reserved / non-unreserved bytes in a key segment must be
        // percent-encoded in BOTH the URL and the SigV4 canonical path (they
        // come from one string), while `/` stays a separator. Real keys are
        // hex content-ids, but a prefix or non-hex key must still sign.
        let b = mk(Some("https://acct.r2.cloudflarestorage.com"));
        assert_eq!(b.request_path("a b/c#d%e"), "/mybucket/a%20b/c%23d%25e");
        let b_aws = mk(None);
        assert_eq!(b_aws.request_path("a b/c#d%e"), "/a%20b/c%23d%25e");
        // Unreserved chars (hex content-id) pass through unchanged.
        assert_eq!(b_aws.request_path("deadBEEF09-_.~"), "/deadBEEF09-_.~");
    }

    /// Live round-trip against real S3-compatible storage (Cloudflare R2).
    /// Skipped unless `NEXUS_R2_*` env creds are set, so CI / dev without
    /// creds is unaffected. Exercises the actual `S3Backend` SigV4 signing +
    /// path-style `request_path` against the configured endpoint — the
    /// end-to-end proof that the bridge-2 (#4262) S3 path works against R2.
    #[test]
    fn live_r2_round_trip() {
        let (Ok(endpoint), Ok(bucket), Ok(ak), Ok(sk)) = (
            std::env::var("NEXUS_R2_ENDPOINT"),
            std::env::var("NEXUS_R2_BUCKET"),
            std::env::var("NEXUS_R2_ACCESS_KEY_ID"),
            std::env::var("NEXUS_R2_SECRET_ACCESS_KEY"),
        ) else {
            eprintln!("live_r2_round_trip: NEXUS_R2_* not set — skipping");
            return;
        };
        let region = std::env::var("NEXUS_R2_REGION").unwrap_or_else(|_| "auto".into());
        let backend = S3Backend::new(
            "r2-live",
            &bucket,
            "bridge2-e2e",
            &region,
            &ak,
            &sk,
            Some(&endpoint),
        )
        .expect("S3Backend::new against R2");
        let ctx = kernel::kernel::OperationContext::new("test", "root", true, None, true);
        let content_id = format!("bridge2e2e{}", std::process::id());
        let body = b"cloudflare-r2-through-rust-s3backend";

        let w = backend
            .write_content(body, &content_id, &ctx, 0)
            .expect("PUT to R2");
        assert_eq!(w.size, body.len() as u64);
        let got = backend
            .read_content(&content_id, &ctx)
            .expect("GET from R2");
        assert_eq!(got, body, "R2 round-trip bytes differ");
        backend.delete_content(&content_id).expect("DELETE from R2");
        eprintln!("live_r2_round_trip: OK — PUT/GET/DELETE {content_id} in {bucket}");
    }
}

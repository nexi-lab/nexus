use base64::{engine::general_purpose::STANDARD, Engine};
use reqwest::blocking::Client;
use serde_json::{json, Value};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone)]
pub struct HttpTarget {
    client: Client,
    base_url: String,
    api_key: String,
}

impl HttpTarget {
    pub fn new(base_url: String, api_key: String) -> BenchResult<Self> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .connect_timeout(std::time::Duration::from_secs(5))
            .no_proxy()
            .build()
            .map_err(|err| BenchError::Http(err.to_string()))?;
        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key,
        })
    }
}

impl BenchTarget for HttpTarget {
    fn name(&self) -> &'static str {
        "http"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let payload = rpc_payload(op)?;
        let method = payload["method"].as_str().unwrap_or("unknown");
        let url = format!("{}/api/nfs/{}", self.base_url, method);
        let response = self
            .client
            .post(url)
            .bearer_auth(&self.api_key)
            .json(&payload)
            .send()
            .map_err(|err| BenchError::Http(err.to_string()))?;
        let status = response.status();
        let body = response
            .bytes()
            .map_err(|err| BenchError::Http(err.to_string()))?;
        if !status.is_success() {
            return Err(BenchError::Http(format!(
                "{}: {}",
                status,
                String::from_utf8_lossy(&body)
            )));
        }
        validate_rpc_response_body(&body)?;
        Ok(OperationMetrics {
            logical_bytes_read: op.logical_read_len(),
            logical_bytes_written: op.logical_write_len(),
            rpc_count: 1,
            egress_bytes: if op.op == OpKind::Read {
                body.len() as u64
            } else {
                0
            },
            cache_hit: None,
        })
    }
}

pub fn rpc_payload(op: &TraceOp) -> BenchResult<Value> {
    let (method, params) = match op.op {
        OpKind::Read => (
            "read",
            json!({
                "path": op.path,
                "offset": op.offset.unwrap_or(0),
                "count": op.length.unwrap_or(0),
            }),
        ),
        OpKind::Write => {
            let payload = seeded_payload(
                op.payload_seed.unwrap_or(0),
                op.length.unwrap_or(0) as usize,
            );
            (
                "write",
                json!({
                    "path": op.path,
                    "offset": op.offset.unwrap_or(0),
                    "content": {"__type__": "bytes", "data": STANDARD.encode(payload)}
                }),
            )
        }
        OpKind::Getattr => ("stat", json!({"path": op.path})),
        OpKind::Lookup => ("exists", json!({"path": op.path})),
        OpKind::Readdir => (
            "list",
            json!({"path": op.path, "recursive": false, "details": true}),
        ),
        OpKind::Delete => ("delete", json!({"path": op.path})),
        OpKind::Rename => (
            "rename",
            json!({"old_path": op.path, "new_path": op.to_path.clone().unwrap_or_default()}),
        ),
        OpKind::Mkdir => ("mkdir", json!({"path": op.path})),
    };

    Ok(json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }))
}

fn validate_rpc_response_body(body: &[u8]) -> BenchResult<()> {
    if body.is_empty() {
        return Ok(());
    }
    let payload: Value = serde_json::from_slice(body)
        .map_err(|err| BenchError::Http(format!("invalid json-rpc response: {err}")))?;
    let Some(error) = payload.get("error").filter(|error| !error.is_null()) else {
        return Ok(());
    };
    Err(BenchError::Http(format_rpc_error(error)))
}

fn format_rpc_error(error: &Value) -> String {
    let code = error
        .get("code")
        .map(Value::to_string)
        .unwrap_or_else(|| "unknown".to_string());
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown json-rpc error");
    let mut rendered = format!("json-rpc error {code}: {message}");
    if let Some(data) = error.get("data") {
        rendered.push_str(&format!(" data: {data}"));
    }
    rendered
}

fn seeded_payload(seed: u64, len: usize) -> Vec<u8> {
    (0..len)
        .map(|idx| seed.wrapping_add(idx as u64) as u8)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

    #[test]
    fn rpc_payload_for_write_uses_nexus_bytes_shape() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Write,
            path: "/file.bin".to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(3),
            payload_seed: Some(1),
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("write payload should build");
        assert_eq!(payload["method"], "write");
        assert_eq!(payload["params"]["path"], "/file.bin");
        assert_eq!(payload["params"]["content"]["__type__"], "bytes");
        assert!(payload["params"]["content"]["data"].as_str().unwrap().len() >= 4);
    }

    #[test]
    fn rpc_payload_for_read_includes_range() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Read,
            path: "/file.bin".to_string(),
            to_path: None,
            offset: Some(12),
            length: Some(34),
            payload_seed: None,
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("read payload should build");
        assert_eq!(payload["method"], "read");
        assert_eq!(payload["params"]["path"], "/file.bin");
        assert_eq!(payload["params"]["offset"], 12);
        assert_eq!(payload["params"]["count"], 34);
    }

    #[test]
    fn rpc_payload_for_write_includes_offset() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Write,
            path: "/file.bin".to_string(),
            to_path: None,
            offset: Some(99),
            length: Some(3),
            payload_seed: Some(1),
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("write payload should build");
        assert_eq!(payload["method"], "write");
        assert_eq!(payload["params"]["offset"], 99);
    }

    #[test]
    fn rpc_error_body_returns_http_error_with_code_and_message() {
        let body = br#"{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"not found","data":{"path":"/missing"}}}"#;
        let err =
            validate_rpc_response_body(body).expect_err("json-rpc error envelope should fail");
        let message = err.to_string();
        assert!(message.contains("-32001"));
        assert!(message.contains("not found"));
    }

    #[test]
    fn rpc_method_for_rename_matches_nexus_fuse_client() {
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Rename,
            path: "/old".to_string(),
            to_path: Some("/new".to_string()),
            offset: None,
            length: None,
            payload_seed: None,
            parallel_group: None,
        };
        let payload = rpc_payload(&op).expect("rename payload should build");
        assert_eq!(payload["method"], "rename");
        assert_eq!(payload["params"]["old_path"], "/old");
        assert_eq!(payload["params"]["new_path"], "/new");
    }
}

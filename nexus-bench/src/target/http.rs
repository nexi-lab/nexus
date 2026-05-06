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
        OpKind::Read => ("read", json!({"path": op.path})),
        OpKind::Write => {
            let payload = seeded_payload(
                op.payload_seed.unwrap_or(0),
                op.length.unwrap_or(0) as usize,
            );
            (
                "write",
                json!({
                    "path": op.path,
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

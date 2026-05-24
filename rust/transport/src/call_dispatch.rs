//! Generic `Call` RPC dispatcher — control-plane only.
//!
//! Every syscall is now a typed RPC. This file retains the remaining
//! non-syscall Call methods: the service-lifecycle no-ops the Python
//! factory emits during boot, and explicit error stubs for the
//! lookup-shaped ops the subprocess kernel does not expose over the
//! wire (service / trie / agent registries). Anything else hits the
//! unknown-method error path.

use std::collections::HashMap;
use std::sync::Arc;

use kernel::core::agents::registry::{
    AgentDescriptor, AgentError, AgentKind, AgentSignal, AgentState, ExternalProcessInfo,
};
use kernel::kernel::vfs_proto::CallResponse;
use kernel::kernel::{Kernel, OperationContext};
use tonic::{Response, Status};

use crate::grpc::{encode_rpc_error, RpcErrorCode};

/// Dispatch a generic Call RPC. After the typed-RPC migration only the
/// non-syscall control plane stays here.
pub fn dispatch(
    kernel: &Arc<Kernel>,
    _ctx: &OperationContext,
    method: &str,
    payload: &[u8],
) -> Result<Response<CallResponse>, Status> {
    let params: serde_json::Value =
        serde_json::from_slice(payload).unwrap_or(serde_json::Value::Object(Default::default()));

    let result = match method {
        "get_mount_points" => ok_json(serde_json::json!(kernel.get_mount_points())),

        // Service lifecycle — no-ops for subprocess mode (the Rust
        // binary manages its own service lifecycle).
        "service_start_all"
        | "service_mark_bootstrapped"
        | "service_stop_all"
        | "service_close_all" => ok_json(serde_json::json!(null)),

        // Lookup-shaped ops the subprocess kernel doesn't expose.
        "service_lookup" | "service_swap" => Err(encode_rpc_error(
            RpcErrorCode::InternalError,
            &format!("{method} is not available in subprocess mode"),
        )),

        // Trie — not exposed via gRPC.
        "trie_register" | "trie_lookup" | "trie_unregister" => Err(call_err(
            RpcErrorCode::InternalError,
            &format!("{method} is not available in subprocess mode"),
        )),

        // Agent registry
        "agent_register" | "agent_register_external" => do_agent_register(kernel, &params),
        "agent_unregister" => do_agent_unregister(kernel, &params),
        "agent_unregister_external" => do_agent_unregister_external(kernel, &params),
        "agent_get" => do_agent_get(kernel, &params),
        "agent_list" => do_agent_list(kernel, &params),
        "agent_update_state" => do_agent_update_state(kernel, &params),
        "agent_signal" => do_agent_signal(kernel, &params),
        "agent_heartbeat" => do_agent_heartbeat(kernel, &params),

        // Xattr (file metadata side-car)

        _ => Err(call_err(
            RpcErrorCode::InternalError,
            &format!("unknown Call method: {method}"),
        )),
    };

    match result {
        Ok(payload_bytes) => Ok(Response::new(CallResponse {
            payload: payload_bytes,
            is_error: false,
        })),
        Err(err_payload) => Ok(Response::new(CallResponse {
            payload: err_payload,
            is_error: true,
        })),
    }
}

// ── Helpers ──────────────────────────────────────────────────────────

fn s(v: &serde_json::Value, key: &str) -> String {
    v.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn opt_s(v: &serde_json::Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
}

fn labels_map(v: &serde_json::Value, key: &str) -> HashMap<String, String> {
    v.get(key)
        .and_then(|v| v.as_object())
        .map(|obj| {
            obj.iter()
                .map(|(k, v)| {
                    let value = v
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| v.to_string());
                    (k.clone(), value)
                })
                .collect()
        })
        .unwrap_or_default()
}

fn ok_json(val: serde_json::Value) -> Result<Vec<u8>, Vec<u8>> {
    let wrapped = serde_json::json!({"result": val});
    Ok(serde_json::to_vec(&wrapped).unwrap_or_else(|_| b"{}".to_vec()))
}

fn call_err(code: RpcErrorCode, msg: &str) -> Vec<u8> {
    encode_rpc_error(code, msg)
}

fn agent_err_to_payload(err: AgentError) -> Vec<u8> {
    let code = match &err {
        AgentError::NotFound(_) => RpcErrorCode::FileNotFound,
        AgentError::AlreadyExists(_) | AgentError::InvalidTransition { .. } => {
            RpcErrorCode::Conflict
        }
        AgentError::InvalidKind(_) | AgentError::Protocol(_) => RpcErrorCode::ValidationError,
        AgentError::PidExhausted => RpcErrorCode::InternalError,
    };
    encode_rpc_error(code, &err.to_string())
}

fn agent_descriptor_to_json(desc: &AgentDescriptor) -> serde_json::Value {
    let external_info = desc.external_info.as_ref().map(|info| {
        serde_json::json!({
            "connection_id": &info.connection_id,
            "host_pid": info.host_pid,
            "remote_addr": &info.remote_addr,
            "protocol": &info.protocol,
            "last_heartbeat_ms": info.last_heartbeat_ms,
        })
    });
    let repos: Vec<serde_json::Value> = desc
        .repos
        .iter()
        .map(|repo| {
            serde_json::json!({
                "alias": &repo.alias,
                "mount_path": &repo.mount_path,
            })
        })
        .collect();

    serde_json::json!({
        "pid": &desc.pid,
        "name": &desc.name,
        "kind": desc.kind.as_str(),
        "owner_id": &desc.owner_id,
        "zone_id": &desc.zone_id,
        "parent_pid": &desc.parent_pid,
        "state": desc.state.as_str(),
        "exit_code": desc.exit_code,
        "generation": desc.generation,
        "cwd": &desc.cwd,
        "root": &desc.root,
        "children": &desc.children,
        "created_at_ms": desc.created_at_ms,
        "updated_at_ms": desc.updated_at_ms,
        "last_heartbeat_ms": desc.last_heartbeat_ms,
        "connection_id": &desc.connection_id,
        "external_info": external_info,
        "labels": &desc.labels,
        "repos": repos,
    })
}

// ── Agent registry handlers ─────────────────────────────────────────

fn do_agent_register(kernel: &Arc<Kernel>, params: &serde_json::Value) -> Result<Vec<u8>, Vec<u8>> {
    let name = s(params, "name");
    let owner_id = s(params, "owner_id");
    let zone_id = s(params, "zone_id");
    let connection_id = opt_s(params, "connection_id");
    let parent_pid = opt_s(params, "parent_pid");
    let labels = labels_map(params, "labels");

    let desc = if let Some(connection_id) = connection_id {
        let host_pid = params.get("host_pid").and_then(|v| v.as_i64());
        let remote_addr = opt_s(params, "remote_addr");
        let protocol = opt_s(params, "protocol").unwrap_or_else(|| "grpc".to_string());
        kernel
            .agent_registry()
            .register_external(
                name,
                owner_id,
                zone_id,
                connection_id,
                host_pid,
                remote_addr,
                protocol,
                parent_pid,
                labels,
            )
            .map_err(agent_err_to_payload)?
    } else {
        let kind = opt_s(params, "kind")
            .and_then(|k| AgentKind::from_str(&k))
            .unwrap_or(AgentKind::Managed);
        let pid = opt_s(params, "pid");
        let cwd = opt_s(params, "cwd").unwrap_or_else(|| "/".to_string());
        let external_info =
            opt_s(params, "external_connection_id").map(|connection_id| ExternalProcessInfo {
                connection_id,
                host_pid: params.get("host_pid").and_then(|v| v.as_i64()),
                remote_addr: opt_s(params, "remote_addr"),
                protocol: opt_s(params, "protocol").unwrap_or_else(|| "grpc".to_string()),
                last_heartbeat_ms: None,
            });
        kernel
            .agent_registry()
            .spawn(
                name,
                owner_id,
                zone_id,
                kind,
                parent_pid,
                pid,
                cwd,
                external_info,
                labels,
            )
            .map_err(agent_err_to_payload)?
    };
    ok_json(agent_descriptor_to_json(&desc))
}

fn do_agent_unregister(
    kernel: &Arc<Kernel>,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    let removed = kernel.agent_registry().unregister(&pid).is_some();
    ok_json(serde_json::json!(removed))
}

fn do_agent_unregister_external(
    kernel: &Arc<Kernel>,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    kernel
        .agent_registry()
        .unregister_external(&pid)
        .map_err(agent_err_to_payload)?;
    ok_json(serde_json::json!(true))
}

fn do_agent_get(kernel: &Arc<Kernel>, params: &serde_json::Value) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    match kernel.agent_registry().get(&pid) {
        Some(desc) => ok_json(agent_descriptor_to_json(&desc)),
        None => ok_json(serde_json::Value::Null),
    }
}

fn do_agent_list(kernel: &Arc<Kernel>, params: &serde_json::Value) -> Result<Vec<u8>, Vec<u8>> {
    let zone_id = opt_s(params, "zone_id");
    let owner_id = opt_s(params, "owner_id");
    let kind = opt_s(params, "kind").and_then(|k| AgentKind::from_str(&k));
    let state = opt_s(params, "state").and_then(|s| AgentState::from_str(&s));
    let records = kernel.agent_registry().list(
        zone_id.as_deref(),
        owner_id.as_deref(),
        kind.as_ref(),
        state.as_ref(),
    );
    let values: Vec<serde_json::Value> = records.iter().map(agent_descriptor_to_json).collect();
    ok_json(serde_json::json!(values))
}

fn do_agent_update_state(
    kernel: &Arc<Kernel>,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    let state = opt_s(params, "state")
        .or_else(|| opt_s(params, "new_state"))
        .and_then(|s| AgentState::from_str(&s))
        .ok_or_else(|| {
            call_err(
                RpcErrorCode::ValidationError,
                "invalid or missing agent state",
            )
        })?;
    match kernel.agent_registry().update_state(&pid, state) {
        Ok(true) => match kernel.agent_registry().get(&pid) {
            Some(desc) => ok_json(agent_descriptor_to_json(&desc)),
            None => Err(call_err(
                RpcErrorCode::FileNotFound,
                &format!("process not found: {pid}"),
            )),
        },
        Ok(false) => Err(call_err(
            RpcErrorCode::FileNotFound,
            &format!("process not found: {pid}"),
        )),
        Err(err) => Err(agent_err_to_payload(err)),
    }
}

fn do_agent_signal(kernel: &Arc<Kernel>, params: &serde_json::Value) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    let sig = opt_s(params, "sig")
        .or_else(|| opt_s(params, "signal"))
        .and_then(|s| AgentSignal::from_str(&s))
        .ok_or_else(|| {
            call_err(
                RpcErrorCode::ValidationError,
                "invalid or missing agent signal",
            )
        })?;
    let payload = params
        .get("payload")
        .and_then(|v| v.as_object())
        .map(|obj| {
            obj.iter()
                .map(|(k, v)| {
                    let value = v
                        .as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| v.to_string());
                    (k.clone(), value)
                })
                .collect::<HashMap<String, String>>()
        });

    let desc = kernel
        .agent_registry()
        .signal(&pid, sig, payload)
        .map_err(agent_err_to_payload)?;
    ok_json(agent_descriptor_to_json(&desc))
}

fn do_agent_heartbeat(
    kernel: &Arc<Kernel>,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let pid = s(params, "pid");
    kernel
        .agent_registry()
        .heartbeat(&pid)
        .map_err(agent_err_to_payload)?;
    match kernel.agent_registry().get(&pid) {
        Some(desc) => ok_json(agent_descriptor_to_json(&desc)),
        None => Err(call_err(
            RpcErrorCode::FileNotFound,
            &format!("process not found: {pid}"),
        )),
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    fn result_payload(response: kernel::kernel::vfs_proto::CallResponse) -> serde_json::Value {
        let payload: serde_json::Value =
            serde_json::from_slice(&response.payload).expect("response JSON");
        payload.get("result").cloned().expect("result envelope")
    }

    fn error_payload(response: kernel::kernel::vfs_proto::CallResponse) -> serde_json::Value {
        assert!(response.is_error, "response did not carry an error payload");
        serde_json::from_slice(&response.payload).expect("error JSON")
    }

    #[test]
    fn agent_registry_dispatch_routes_to_kernel_ssot() {
        let kernel = Arc::new(Kernel::new());
        let ctx = OperationContext::new("admin", kernel::ROOT_ZONE_ID, true, None, true);
        let payload = serde_json::to_vec(&serde_json::json!({
            "name": "E2E Agent",
            "owner_id": "admin",
            "zone_id": kernel::ROOT_ZONE_ID,
            "connection_id": "admin,e2e",
            "labels": {"capabilities": "test"},
        }))
        .expect("payload");

        let registered = dispatch(&kernel, &ctx, "agent_register_external", &payload)
            .expect("dispatch")
            .into_inner();
        assert!(!registered.is_error, "register returned error payload");
        let registered = result_payload(registered);
        assert_eq!(registered["pid"], "admin,e2e");
        assert_eq!(registered["state"], "REGISTERED");

        let list_payload = serde_json::to_vec(&serde_json::json!({
            "zone_id": kernel::ROOT_ZONE_ID,
        }))
        .expect("payload");
        let listed = dispatch(&kernel, &ctx, "agent_list", &list_payload)
            .expect("dispatch")
            .into_inner();
        let listed = result_payload(listed);
        assert_eq!(listed.as_array().expect("agent list").len(), 1);

        let update_payload = serde_json::to_vec(&serde_json::json!({
            "pid": "admin,e2e",
            "state": "warming_up",
        }))
        .expect("payload");
        let warming = dispatch(&kernel, &ctx, "agent_update_state", &update_payload)
            .expect("dispatch")
            .into_inner();
        assert_eq!(result_payload(warming)["state"], "WARMING_UP");

        let signal_payload = serde_json::to_vec(&serde_json::json!({
            "pid": "admin,e2e",
            "sig": "SIGCONT",
        }))
        .expect("payload");
        let ready = dispatch(&kernel, &ctx, "agent_signal", &signal_payload)
            .expect("dispatch")
            .into_inner();
        let ready = result_payload(ready);
        assert_eq!(ready["state"], "READY");
        assert_eq!(ready["generation"], 2);

        let heartbeat_payload = serde_json::to_vec(&serde_json::json!({
            "pid": "admin,e2e",
        }))
        .expect("payload");
        let heartbeat = dispatch(&kernel, &ctx, "agent_heartbeat", &heartbeat_payload)
            .expect("dispatch")
            .into_inner();
        let heartbeat = result_payload(heartbeat);
        assert!(heartbeat["external_info"]["last_heartbeat_ms"].is_number());

        let unregister = dispatch(
            &kernel,
            &ctx,
            "agent_unregister_external",
            &heartbeat_payload,
        )
        .expect("dispatch")
        .into_inner();
        assert_eq!(result_payload(unregister), serde_json::json!(true));
        assert!(kernel.agent_registry().get("admin,e2e").is_none());
    }

    #[test]
    fn agent_registry_dispatch_maps_lifecycle_errors_to_client_codes() {
        let kernel = Arc::new(Kernel::new());
        let ctx = OperationContext::new("admin", kernel::ROOT_ZONE_ID, true, None, true);
        let payload = serde_json::to_vec(&serde_json::json!({
            "name": "E2E Agent",
            "owner_id": "admin",
            "zone_id": kernel::ROOT_ZONE_ID,
            "connection_id": "admin,e2e",
        }))
        .expect("payload");

        let registered = dispatch(&kernel, &ctx, "agent_register_external", &payload)
            .expect("dispatch")
            .into_inner();
        assert!(!registered.is_error, "register returned error payload");

        let duplicate = dispatch(&kernel, &ctx, "agent_register_external", &payload)
            .expect("dispatch")
            .into_inner();
        let duplicate = error_payload(duplicate);
        assert_eq!(duplicate["code"], serde_json::json!(-32006));

        let invalid_signal_payload = serde_json::to_vec(&serde_json::json!({
            "pid": "admin,e2e",
            "sig": "NOPE",
        }))
        .expect("payload");
        let invalid_signal = dispatch(&kernel, &ctx, "agent_signal", &invalid_signal_payload)
            .expect("dispatch")
            .into_inner();
        let invalid_signal = error_payload(invalid_signal);
        assert_eq!(invalid_signal["code"], serde_json::json!(-32005));

        let invalid_transition_payload = serde_json::to_vec(&serde_json::json!({
            "pid": "admin,e2e",
            "sig": "SIGSTOP",
        }))
        .expect("payload");
        let invalid_transition =
            dispatch(&kernel, &ctx, "agent_signal", &invalid_transition_payload)
                .expect("dispatch")
                .into_inner();
        let invalid_transition = error_payload(invalid_transition);
        assert_eq!(invalid_transition["code"], serde_json::json!(-32006));
    }
}

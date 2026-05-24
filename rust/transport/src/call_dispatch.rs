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
use std::time::Duration;

use kernel::abi::KernelAbi;
use kernel::core::agents::registry::{
    AgentDescriptor, AgentError, AgentKind, AgentSignal, AgentState, ExternalProcessInfo,
};
use kernel::kernel::convenience::KernelConvenience;
use kernel::kernel::vfs_proto::CallResponse;
use kernel::kernel::{Kernel, KernelError, OperationContext};
use kernel::meta_store::remote::RemoteMetaStore;
use kernel::rpc_transport::RpcTransport;
use tonic::{Response, Status};

use crate::grpc::{encode_rpc_error, RpcErrorCode};

/// Dispatch a generic Call RPC. After the typed-RPC migration only the
/// non-syscall control plane stays here.
pub fn dispatch(
    kernel: &Arc<Kernel>,
    ctx: &OperationContext,
    method: &str,
    payload: &[u8],
) -> Result<Response<CallResponse>, Status> {
    let params: serde_json::Value =
        serde_json::from_slice(payload).unwrap_or(serde_json::Value::Object(Default::default()));

    let result = match method {
        "sys_read" => do_sys_read(kernel, &params, ctx),
        "sys_setattr" => do_sys_setattr(kernel, &params, ctx),
        "sys_mkdir" => do_sys_mkdir(kernel, &params, ctx),
        "sys_unlink" => do_sys_unlink(kernel, &params, ctx),
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

fn i64_or(v: &serde_json::Value, key: &str, default: i64) -> i64 {
    v.get(key).and_then(|v| v.as_i64()).unwrap_or(default)
}

fn u64_or(v: &serde_json::Value, key: &str, default: u64) -> u64 {
    v.get(key).and_then(|v| v.as_u64()).unwrap_or(default)
}

fn bool_or(v: &serde_json::Value, key: &str, default: bool) -> bool {
    v.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
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

fn kernel_err_to_payload(err: KernelError) -> Vec<u8> {
    let (code, msg) = match err {
        KernelError::FileNotFound(p) => (RpcErrorCode::FileNotFound, p),
        KernelError::PermissionDenied(m) => (RpcErrorCode::PermissionError, m),
        KernelError::InvalidPath(m) => (RpcErrorCode::InvalidPath, m),
        other => (RpcErrorCode::InternalError, format!("{:?}", other)),
    };
    encode_rpc_error(code, &msg)
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

// ── Syscall handlers ────────────────────────────────────────────────

fn do_sys_read(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let timeout_ms = u64_or(params, "timeout_ms", 5000);
    let offset = u64_or(params, "offset", 0);
    match KernelAbi::sys_read(kernel, &path, ctx, timeout_ms, offset) {
        Ok(result) => {
            let data = result.data.as_deref().map(encode_bytes);
            ok_json(serde_json::json!({
                "data": data,
                "content_id": result.content_id,
                "gen": result.gen,
                "entry_type": result.entry_type,
                "stream_next_offset": result.stream_next_offset,
                "post_hook_needed": result.post_hook_needed,
            }))
        }
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_setattr(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let entry_type = i64_or(params, "entry_type", 0) as i32;
    let zone_id_str = s(params, "zone_id");
    let zone_id = if zone_id_str.is_empty() {
        kernel::ROOT_ZONE_ID
    } else {
        &zone_id_str
    };

    if entry_type == 2 {
        let backend_type = s(params, "backend_type");
        let local_root = s(params, "local_root");
        let backend_name_str = s(params, "backend_name");
        let backend_name = if backend_name_str.is_empty() {
            backend_type.as_str()
        } else {
            backend_name_str.as_str()
        };
        let is_external = bool_or(params, "is_external", false);
        let fsync = bool_or(params, "fsync", false);

        // The Python subprocess client can only describe mounts as JSON.
        // Honor the sandbox/workspace path-local case by constructing the
        // Rust backend here. Keep returning synthetic success for other
        // mount calls, especially the root CAS mount sent during generic
        // NexusFS boot, because the cluster process already mounted its
        // own root filesystem at startup.
        if backend_type == "path_local" && !local_root.is_empty() {
            if !ctx.is_admin && !ctx.is_system {
                return Err(call_err(
                    RpcErrorCode::PermissionError,
                    "sys_setattr DT_MOUNT path_local requires admin or system context",
                ));
            }
            let backend = backends::storage::path_local::PathLocalBackend::new(
                std::path::Path::new(&local_root),
                fsync,
            )
            .map_err(|e| {
                call_err(
                    RpcErrorCode::InternalError,
                    &format!("path_local mount init failed for {local_root}: {e}"),
                )
            })?;
            match kernel.sys_setattr(
                &path,
                entry_type,
                backend_name,
                Some(Arc::new(backend)),
                None,
                None,
                "",
                zone_id,
                is_external,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ) {
                Ok(r) => {
                    return ok_json(serde_json::json!({
                        "path": r.path,
                        "created": r.created,
                        "entry_type": r.entry_type,
                        "backend_name": r.backend_name,
                    }));
                }
                Err(e) => return Err(kernel_err_to_payload(e)),
            }
        }

        if backend_type == "remote" {
            if !ctx.is_admin && !ctx.is_system {
                return Err(call_err(
                    RpcErrorCode::PermissionError,
                    "sys_setattr DT_MOUNT remote requires admin or system context",
                ));
            }
            let server_address = s(params, "server_address");
            if server_address.is_empty() {
                return Err(call_err(
                    RpcErrorCode::InternalError,
                    "sys_setattr DT_MOUNT remote requires server_address",
                ));
            }
            let remote_auth_token = s(params, "remote_auth_token");
            let remote_timeout = params
                .get("remote_timeout")
                .and_then(|v| v.as_f64())
                .unwrap_or(90.0);
            let transport = Arc::new(
                RpcTransport::new(
                    Arc::clone(kernel.runtime()),
                    &server_address,
                    &remote_auth_token,
                    None,
                    Duration::from_secs_f64(remote_timeout),
                )
                .map_err(|e| {
                    call_err(
                        RpcErrorCode::InternalError,
                        &format!("remote transport init failed for {server_address}: {e}"),
                    )
                })?,
            );
            let backend = backends::storage::remote::RemoteBackend::with_zone_path(
                Arc::clone(&transport),
                path.clone(),
            );
            let remote_metastore = RemoteMetaStore::new(transport);
            match kernel.sys_setattr(
                &path,
                entry_type,
                backend_name,
                Some(Arc::new(backend)),
                None,
                None,
                "",
                zone_id,
                is_external,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                Some(Arc::new(remote_metastore)),
            ) {
                Ok(r) => {
                    return ok_json(serde_json::json!({
                        "path": r.path,
                        "created": r.created,
                        "entry_type": r.entry_type,
                        "backend_name": r.backend_name,
                    }));
                }
                Err(e) => return Err(kernel_err_to_payload(e)),
            }
        }

        return ok_json(serde_json::json!({
            "path": path,
            "created": false,
            "entry_type": entry_type,
        }));
    }

    let mime_type_str = params
        .get("mime_type")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let content_id_str = params
        .get("content_id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let modified_at_ms = params.get("modified_at_ms").and_then(|v| v.as_i64());
    let created_at_ms = params.get("created_at_ms").and_then(|v| v.as_i64());
    let size = params.get("size").and_then(|v| v.as_u64());
    let version = params
        .get("version")
        .and_then(|v| v.as_u64())
        .map(|v| v as u32);
    let backend_name_str = s(params, "backend_name");
    let backend_name = if backend_name_str.is_empty() {
        ""
    } else {
        &backend_name_str
    };
    let io_profile_str = s(params, "io_profile");
    let io_profile = if io_profile_str.is_empty() {
        ""
    } else {
        &io_profile_str
    };
    let is_external = bool_or(params, "is_external", false);
    let capacity = u64_or(params, "capacity", 0) as usize;

    match kernel.sys_setattr(
        &path,
        entry_type,
        backend_name,
        None, // backend (non-mount entry types don't need one)
        None, // metastore
        None, // raft_backend
        io_profile,
        zone_id,
        is_external,
        capacity,
        None, // read_fd
        None, // write_fd
        mime_type_str.as_deref(),
        modified_at_ms,
        content_id_str.as_deref(),
        size,
        version,
        created_at_ms,
        None, // link_target
        None, // source
        None, // remote_metastore
    ) {
        Ok(r) => ok_json(serde_json::json!({
            "path": r.path,
            "created": r.created,
            "entry_type": r.entry_type,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_mkdir(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let parents = bool_or(params, "parents", false);
    let exist_ok = bool_or(params, "exist_ok", true);
    match KernelConvenience::mkdir(kernel, &path, ctx, parents, exist_ok) {
        Ok(r) => ok_json(serde_json::json!({
            "hit": r.hit,
            "post_hook_needed": r.post_hook_needed,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_unlink(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let recursive = bool_or(params, "recursive", false);
    match KernelAbi::sys_unlink(kernel, &path, ctx, recursive) {
        Ok(r) => ok_json(serde_json::json!({
            "hit": r.hit,
            "entry_type": r.entry_type,
            "post_hook_needed": r.post_hook_needed,
            "path": r.path,
            "content_id": r.content_id,
            "size": r.size,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}
// ── Bytes encoding/decoding ─────────────────────────────────────────
// Python rpc_codec sends bytes as {"__type__": "bytes", "data": "<base64>"}

fn encode_bytes(data: &[u8]) -> serde_json::Value {
    use base64::Engine;
    serde_json::json!({
        "__type__": "bytes",
        "data": base64::engine::general_purpose::STANDARD.encode(data),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn path_local_mount_payload(root: &std::path::Path) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "path": "/zone/local",
            "entry_type": 2,
            "backend_type": "path_local",
            "backend_name": "path_local",
            "local_root": root.to_string_lossy(),
            "zone_id": kernel::ROOT_ZONE_ID,
        }))
        .expect("payload")
    }

    fn remote_mount_payload() -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({
            "path": "/zone/company",
            "entry_type": 2,
            "backend_type": "remote",
            "backend_name": "remote_zone:company",
            "server_address": "127.0.0.1:9",
            "remote_auth_token": "sk-test",
            "zone_id": kernel::ROOT_ZONE_ID,
        }))
        .expect("payload")
    }

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
    fn sys_setattr_path_local_mount_from_json_routes_io_to_local_root() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let kernel = Arc::new(Kernel::new());
        let ctx = OperationContext::new("test", kernel::ROOT_ZONE_ID, true, None, true);
        let payload = path_local_mount_payload(tmp.path());

        let response = dispatch(&kernel, &ctx, "sys_setattr", &payload)
            .expect("dispatch")
            .into_inner();

        assert!(!response.is_error, "mount returned error payload");
        KernelAbi::sys_write(&*kernel, "/zone/local/live/a.txt", &ctx, b"abc", 0)
            .expect("write through path-local mount");
        assert_eq!(
            std::fs::read(tmp.path().join("live/a.txt")).unwrap(),
            b"abc"
        );
    }

    #[test]
    fn sys_setattr_path_local_mount_from_json_requires_admin_or_system() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let kernel = Arc::new(Kernel::new());
        let ctx = OperationContext::new("user", kernel::ROOT_ZONE_ID, false, None, false);
        let payload = path_local_mount_payload(tmp.path());

        let response = dispatch(&kernel, &ctx, "sys_setattr", &payload)
            .expect("dispatch")
            .into_inner();

        assert!(response.is_error, "non-admin path_local mount succeeded");
    }

    #[test]
    fn sys_setattr_remote_mount_from_json_installs_mount() {
        let kernel = Arc::new(Kernel::new());
        let ctx = OperationContext::new("test", kernel::ROOT_ZONE_ID, true, None, true);

        let response = dispatch(&kernel, &ctx, "sys_setattr", &remote_mount_payload())
            .expect("dispatch")
            .into_inner();

        assert!(!response.is_error, "remote mount returned error payload");
        assert!(
            kernel.has_mount("/zone/company", kernel::ROOT_ZONE_ID),
            "remote mount call returned success without installing a route"
        );
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

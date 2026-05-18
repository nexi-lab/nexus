//! Generic `Call` RPC dispatcher.
//!
//! Parses JSON payload → dispatches to `Kernel::sys_*` → serializes
//! result back to JSON. The Python `rpc_codec` uses plain JSON with
//! a `{"result": <value>}` envelope for success and
//! `{"code": N, "message": "..."}` for errors.

use std::sync::Arc;

use kernel::abi::KernelAbi;
use kernel::kernel::{Kernel, KernelError, OperationContext};
use kernel::kernel::vfs_proto::{CallResponse};
use tonic::{Response, Status};

use crate::grpc::{encode_rpc_error, RpcErrorCode};

/// Dispatch a generic Call RPC to the appropriate kernel method.
pub fn dispatch(
    kernel: &Arc<Kernel>,
    ctx: &OperationContext,
    method: &str,
    payload: &[u8],
) -> Result<Response<CallResponse>, Status> {
    let params: serde_json::Value =
        serde_json::from_slice(payload).unwrap_or(serde_json::Value::Object(Default::default()));

    let result = match method {
        "sys_stat" => do_sys_stat(kernel, &params, ctx),
        "sys_setattr" => do_sys_setattr(kernel, &params, ctx),
        "sys_mkdir" => do_sys_mkdir(kernel, &params, ctx),
        "sys_unlink" => do_sys_unlink(kernel, &params, ctx),
        "sys_rename" => do_sys_rename(kernel, &params, ctx),
        "sys_copy" => do_sys_copy(kernel, &params, ctx),
        "sys_readdir" => do_sys_readdir(kernel, &params, ctx),
        "sys_lock" => do_sys_lock(kernel, &params),
        "sys_unlock" => do_sys_unlock(kernel, &params),
        "sys_watch" => do_sys_watch(kernel, &params),
        "stat_batch" => do_stat_batch(kernel, &params, ctx),

        // Service lifecycle — no-ops for subprocess mode (the Rust
        // binary manages its own service lifecycle).
        "service_start_all" | "service_mark_bootstrapped" | "service_stop_all"
        | "service_close_all" => ok_json(serde_json::json!(null)),

        // Service lookup/swap — not available via gRPC.
        "service_lookup" | "service_swap" => Err(call_err(
            RpcErrorCode::InternalError,
            &format!("{method} is not available in subprocess mode"),
        )),

        // Trie — not exposed via gRPC.
        "trie_register" | "trie_lookup" | "trie_unregister" => Err(call_err(
            RpcErrorCode::InternalError,
            &format!("{method} is not available in subprocess mode"),
        )),

        // IPC pipes
        "create_pipe" => do_create_pipe(kernel, &params),
        "destroy_pipe" | "close_pipe" => do_destroy_pipe(kernel, &params),
        "has_pipe" => do_has_pipe(kernel, &params),
        "close_all_pipes" => {
            kernel.close_all_pipes();
            ok_json(serde_json::json!(null))
        }

        // IPC streams
        "create_stream" => do_create_stream(kernel, &params),
        "has_stream" => do_has_stream(kernel, &params),
        "close_stream" | "destroy_stream" => do_close_stream(kernel, &params),
        "stream_write_nowait" => do_stream_write(kernel, &params),
        "stream_read_at" => do_stream_read_at(kernel, &params),
        "stream_read_at_blocking" => do_stream_read_at_blocking(kernel, &params),
        "stream_collect_all" => do_stream_collect_all(kernel, &params),

        // Agent registry
        "agent_register" | "agent_unregister" | "agent_list" => Err(call_err(
            RpcErrorCode::InternalError,
            &format!("{method} is not available in subprocess mode"),
        )),

        // Write batch — delegate to individual writes
        "write_batch" => do_write_batch(kernel, &params, ctx),

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
    v.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
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

fn stat_to_json(s: &kernel::kernel::StatResult) -> serde_json::Value {
    serde_json::json!({
        "path": s.path,
        "size": s.size,
        "content_id": s.content_id,
        "mime_type": s.mime_type,
        "is_directory": s.is_directory,
        "entry_type": s.entry_type,
        "mode": s.mode,
        "version": s.version,
        "gen": s.gen,
        "zone_id": s.zone_id,
        "created_at_ms": s.created_at_ms,
        "modified_at_ms": s.modified_at_ms,
        "last_writer_address": s.last_writer_address,
        "link_target": s.link_target,
        "owner_id": s.owner_id,
    })
}

// ── Syscall handlers ────────────────────────────────────────────────

fn do_sys_stat(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let zone_id = if let Some(zid) = params.get("zone_id").and_then(|v| v.as_str()) {
        zid.to_string()
    } else {
        ctx.zone_id.clone()
    };
    match kernel.sys_stat(&path, &zone_id) {
        Some(stat) => ok_json(stat_to_json(&stat)),
        None => Err(call_err(RpcErrorCode::FileNotFound, &path)),
    }
}

fn do_sys_setattr(
    kernel: &Kernel,
    params: &serde_json::Value,
    _ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let entry_type = i64_or(params, "entry_type", 0) as i32;
    let zone_id_str = s(params, "zone_id");
    let zone_id = if zone_id_str.is_empty() {
        kernel::ROOT_ZONE_ID
    } else {
        &zone_id_str
    };
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
    let version = params.get("version").and_then(|v| v.as_u64()).map(|v| v as u32);

    match kernel.sys_setattr(
        &path,
        entry_type,
        "",    // backend_name
        None,  // backend
        None,  // metastore
        None,  // raft_backend
        "",    // io_profile
        zone_id,
        false, // is_external
        0,     // capacity
        None,  // read_fd
        None,  // write_fd
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
    match KernelAbi::sys_mkdir(kernel, &path, ctx, parents, exist_ok) {
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

fn do_sys_rename(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let new_path = s(params, "new_path");
    match KernelAbi::sys_rename(kernel, &path, &new_path, ctx) {
        Ok(r) => ok_json(serde_json::json!({
            "hit": r.hit,
            "success": r.success,
            "post_hook_needed": r.post_hook_needed,
            "is_directory": r.is_directory,
            "old_content_id": r.old_content_id,
            "old_size": r.old_size,
            "old_version": r.old_version,
            "old_modified_at_ms": r.old_modified_at_ms,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_copy(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let src = s(params, "src");
    let dst = s(params, "dst");
    match KernelAbi::sys_copy(kernel, &src, &dst, ctx) {
        Ok(r) => ok_json(serde_json::json!({
            "hit": r.hit,
            "post_hook_needed": r.post_hook_needed,
            "dst_path": r.dst_path,
            "content_id": r.content_id,
            "size": r.size,
            "version": r.version,
            "gen": r.gen,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_readdir(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let entries = KernelAbi::sys_readdir(kernel, &path, &ctx.zone_id, ctx.is_admin);
    let arr: Vec<serde_json::Value> = entries
        .into_iter()
        .map(|(name, dtype)| serde_json::json!({"name": name, "entry_type": dtype}))
        .collect();
    ok_json(serde_json::json!(arr))
}

fn do_sys_lock(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let timeout_ms = u64_or(params, "timeout_ms", 5000);
    let lock_id_param = s(params, "lock_id");
    let lock_id = if lock_id_param.is_empty() {
        ""
    } else {
        &lock_id_param
    };
    match kernel.sys_lock(
        &path,
        lock_id,
        kernel::lock_manager::KernelLockMode::Exclusive,
        1,
        timeout_ms / 1000 + 1,
        "",
    ) {
        Ok(Some(id)) => ok_json(serde_json::json!({"lock_id": id})),
        Ok(None) => Err(call_err(
            RpcErrorCode::InternalError,
            "lock acquisition failed (contention)",
        )),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_unlock(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let lock_id = s(params, "lock_id");
    let force = bool_or(params, "force", false);
    match kernel.sys_unlock(&path, &lock_id, force) {
        Ok(released) => ok_json(serde_json::json!({"released": released})),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_sys_watch(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let timeout_ms = u64_or(params, "timeout_ms", 30000);
    match kernel.sys_watch(&path, timeout_ms) {
        Some(evt) => ok_json(serde_json::json!({
            "path": evt.path,
            "event_type": format!("{:?}", evt.event_type),
        })),
        None => ok_json(serde_json::json!(null)),
    }
}

fn do_stat_batch(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let paths: Vec<String> = params
        .get("paths")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let zone_id = if let Some(zid) = params.get("zone_id").and_then(|v| v.as_str()) {
        zid.to_string()
    } else {
        ctx.zone_id.clone()
    };
    let results: Vec<serde_json::Value> = paths
        .iter()
        .map(|p| match kernel.sys_stat(p, &zone_id) {
            Some(st) => stat_to_json(&st),
            None => serde_json::json!(null),
        })
        .collect();
    ok_json(serde_json::json!(results))
}

// ── IPC: Pipes ──────────────────────────────────────────────────────

fn do_create_pipe(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let capacity = u64_or(params, "capacity", 64) as usize;
    match kernel.sys_setattr(
        &path, 3, "", None, None, None, "", kernel::ROOT_ZONE_ID, false, capacity, None, None,
        None, None, None, None, None, None, None, None, None,
    ) {
        Ok(_) => ok_json(serde_json::json!(null)),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_destroy_pipe(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    match kernel.close_pipe(&path) {
        Ok(()) => ok_json(serde_json::json!(null)),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_has_pipe(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    ok_json(serde_json::json!(kernel.has_pipe(&path)))
}

// ── IPC: Streams ────────────────────────────────────────────────────

fn do_create_stream(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let capacity = u64_or(params, "capacity", 1024) as usize;
    match kernel.sys_setattr(
        &path, 4, "", None, None, None, "", kernel::ROOT_ZONE_ID, false, capacity, None, None,
        None, None, None, None, None, None, None, None, None,
    ) {
        Ok(_) => ok_json(serde_json::json!(null)),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_has_stream(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    ok_json(serde_json::json!(kernel.has_stream(&path)))
}

fn do_close_stream(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    match kernel.close_stream(&path) {
        Ok(()) => ok_json(serde_json::json!(null)),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_stream_write(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let data = decode_bytes_field(params, "data");
    match kernel.stream_write_nowait(&path, &data) {
        Ok(offset) => ok_json(serde_json::json!({"offset": offset})),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_stream_read_at(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let offset = u64_or(params, "offset", 0) as usize;
    match kernel.stream_read_at(&path, offset) {
        Ok(Some((data, next))) => ok_json(serde_json::json!({
            "data": encode_bytes(&data),
            "next_offset": next,
        })),
        Ok(None) => ok_json(serde_json::json!(null)),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_stream_read_at_blocking(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    let offset = u64_or(params, "offset", 0) as usize;
    let timeout_ms = u64_or(params, "timeout_ms", 30000);
    match kernel.stream_read_at_blocking(&path, offset, timeout_ms) {
        Ok((data, next)) => ok_json(serde_json::json!({
            "data": encode_bytes(&data),
            "next_offset": next,
        })),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_stream_collect_all(
    kernel: &Kernel,
    params: &serde_json::Value,
) -> Result<Vec<u8>, Vec<u8>> {
    let path = s(params, "path");
    match kernel.stream_collect_all(&path) {
        Ok(data) => ok_json(serde_json::json!(encode_bytes(&data))),
        Err(e) => Err(kernel_err_to_payload(e)),
    }
}

fn do_write_batch(
    kernel: &Kernel,
    params: &serde_json::Value,
    ctx: &OperationContext,
) -> Result<Vec<u8>, Vec<u8>> {
    let files = params
        .get("files")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut results = Vec::new();
    for item in &files {
        let path = item
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let data = item
            .as_array()
            .and_then(|a| a.get(1))
            .map(|v| decode_bytes_value(v))
            .unwrap_or_default();
        match KernelAbi::sys_write(kernel, path, ctx, &data, 0) {
            Ok(r) => results.push(serde_json::json!({
                "content_id": r.content_id,
                "size": r.size,
                "gen": r.gen,
                "version": r.version,
            })),
            Err(e) => {
                return Err(kernel_err_to_payload(e));
            }
        }
    }
    ok_json(serde_json::json!(results))
}

// ── Bytes encoding/decoding ─────────────────────────────────────────
// Python rpc_codec sends bytes as {"__type__": "bytes", "data": "<base64>"}

fn decode_bytes_field(params: &serde_json::Value, key: &str) -> Vec<u8> {
    params
        .get(key)
        .map(decode_bytes_value)
        .unwrap_or_default()
}

fn decode_bytes_value(val: &serde_json::Value) -> Vec<u8> {
    if let Some(obj) = val.as_object() {
        if obj.get("__type__").and_then(|v| v.as_str()) == Some("bytes") {
            if let Some(b64) = obj.get("data").and_then(|v| v.as_str()) {
                use base64::Engine;
                return base64::engine::general_purpose::STANDARD
                    .decode(b64)
                    .unwrap_or_default();
            }
        }
    }
    if let Some(s) = val.as_str() {
        s.as_bytes().to_vec()
    } else {
        Vec::new()
    }
}

fn encode_bytes(data: &[u8]) -> serde_json::Value {
    use base64::Engine;
    serde_json::json!({
        "__type__": "bytes",
        "data": base64::engine::general_purpose::STANDARD.encode(data),
    })
}

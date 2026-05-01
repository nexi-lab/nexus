"""Main RPC endpoint and helpers.

Extracted from fastapi_server.py (#1602).
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from nexus.contracts.exceptions import (
    ConflictError,
    ConnectorError,
    DatabaseError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.core.hash_fast import hash_content
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.lib.zone_scoping import ZoneScopingError, scope_params_for_zone
from nexus.server.dependencies import require_auth
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
)
from nexus.server.rate_limiting import RATE_LIMIT_AUTHENTICATED, limiter

logger = logging.getLogger(__name__)

router = APIRouter()

# Zone-scoping logic is in nexus.lib.zone_scoping (shared with gRPC servicer).
# The reverse operation (stripping the prefix from results) is handled
# by ``unscope_internal_path`` in ``path_utils.py``.


@router.post("/api/nfs/{method}")
@limiter.limit(RATE_LIMIT_AUTHENTICATED)
async def rpc_endpoint(
    method: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Response:
    """Handle RPC method calls.

    .. deprecated::
        This HTTP RPC endpoint is deprecated. Use gRPC ``Call`` RPC instead.
        Sunset date: 2026-06-25. See Issue #1133.
    """
    import json as _json
    import time as _time
    from types import SimpleNamespace

    from nexus.server._auth_ctx_local import clear_auth, set_auth
    from nexus.server.dependencies import get_operation_context

    logger.debug("Deprecated HTTP RPC called: method=%s (use gRPC Call RPC instead)", method)
    _rpc_start = _time.time()

    try:
        # Parse request body using decode_rpc_message to handle bytes encoding
        _parse_start = _time.time()
        body_bytes = await request.body()
        body = decode_rpc_message(body_bytes) if body_bytes else {}
        rpc_request = RPCRequest.from_dict(body)
        _parse_elapsed = (_time.time() - _parse_start) * 1000

        # Validate method matches URL
        if rpc_request.method and rpc_request.method != method:
            return _error_response(
                rpc_request.id,
                RPCErrorCode.INVALID_REQUEST,
                f"Method mismatch: URL={method}, body={rpc_request.method}",
            )

        # Set method from URL if not in body
        if not rpc_request.method:
            rpc_request.method = method

        # Get operation context
        context = get_operation_context(auth_result)

        # Build a SimpleNamespace view for zone scoping + ETag short-circuit;
        # the dispatch path takes the raw dict.
        params_dict = dict(rpc_request.params or {})
        params_ns = SimpleNamespace(**params_dict)
        scope_params_for_zone(params_ns, context.zone_id)
        params_dict = vars(params_ns)

        _setup_elapsed = (_time.time() - _rpc_start) * 1000 - _parse_elapsed

        state = request.app.state

        # Early 304 check for read operations
        if_none_match = request.headers.get("If-None-Match")
        _read_path = params_dict.get("path")
        if method == "read" and if_none_match and _read_path and state.nexus_fs:
            try:
                cached_content_id = state.nexus_fs.get_content_id(_read_path, context=context)
                if cached_content_id:
                    client_etag = if_none_match.strip('"')
                    if client_etag == cached_content_id:
                        logger.debug(f"Early 304: {_read_path} (ETag match, no content read)")
                        return Response(
                            status_code=304,
                            headers={
                                "ETag": f'"{cached_content_id}"',
                                "Cache-Control": "private, max-age=60",
                            },
                        )
            except Exception as e:
                logger.debug(f"Early ETag check failed for {_read_path}: {e}")

        # Dispatch through the Rust kernel `python_ffi` router
        # (the same path the gRPC Call handler uses).  Auth context
        # is plumbed via `_auth_ctx_local` so the Python service
        # methods that need OperationContext receive it.
        import nexus_runtime as _nr

        _dispatch_start = _time.time()
        set_auth(auth_result)
        try:
            payload_bytes = _json.dumps(params_dict).encode("utf-8")
            kernel = getattr(state.nexus_fs, "_kernel", None)
            if kernel is None:
                raise RuntimeError("HTTP RPC: kernel handle missing on nexus_fs")
            response_bytes = _nr.nx_kernel_dispatch_rust_call(
                kernel, "python_ffi", method, payload_bytes
            )
            if response_bytes is None:
                raise ValueError(f"Method not found: {method}")
            result = _json.loads(bytes(response_bytes).decode("utf-8"))
        finally:
            clear_auth()
        _dispatch_elapsed = (_time.time() - _dispatch_start) * 1000

        # Build response with cache headers + deprecation (Issue #1133)
        headers = get_cache_headers(method, result)
        headers["Deprecation"] = "true"
        headers["Sunset"] = "Wed, 25 Jun 2026 00:00:00 GMT"
        headers["X-Migration-Guide"] = "Use gRPC Call RPC (Issue #1133)"

        # Late 304 check
        if if_none_match and "ETag" in headers:
            client_etag = if_none_match.strip('"')
            server_etag = headers["ETag"].strip('"')
            if client_etag == server_etag:
                return Response(
                    status_code=304,
                    headers={
                        "ETag": headers["ETag"],
                        "Cache-Control": headers.get("Cache-Control", ""),
                    },
                )

        # Success response
        _encode_start = _time.time()
        success_response = {
            "jsonrpc": "2.0",
            "id": rpc_request.id,
            "result": result,
        }
        encoded = encode_rpc_message(success_response)
        _encode_elapsed = (_time.time() - _encode_start) * 1000
        _total_rpc = (_time.time() - _rpc_start) * 1000

        # Log API timing
        _auth_time = auth_result.get("_auth_time_ms", 0) if auth_result else 0
        _full_server_time = _auth_time + _total_rpc
        if _full_server_time > 20:
            logger.info(
                f"[RPC-TIMING] method={method}, auth={_auth_time:.1f}ms, parse={_parse_elapsed:.1f}ms, "
                f"setup={_setup_elapsed:.1f}ms, dispatch={_dispatch_elapsed:.1f}ms, "
                f"encode={_encode_elapsed:.1f}ms, rpc={_total_rpc:.1f}ms, server_total={_full_server_time:.1f}ms"
            )

        return Response(content=encoded, media_type="application/json", headers=headers)

    except ZoneScopingError as e:
        return _error_response(None, RPCErrorCode.PERMISSION_ERROR, str(e))
    except ValueError as e:
        return _error_response(None, RPCErrorCode.INVALID_PARAMS, f"Invalid parameters: {e}")
    except NexusFileNotFoundError as e:
        return _error_response(None, RPCErrorCode.FILE_NOT_FOUND, str(e))
    except InvalidPathError as e:
        return _error_response(None, RPCErrorCode.INVALID_PATH, str(e))
    except NexusPermissionError as e:
        return _error_response(None, RPCErrorCode.PERMISSION_ERROR, str(e))
    except ValidationError as e:
        return _error_response(None, RPCErrorCode.VALIDATION_ERROR, str(e))
    except ConflictError as e:
        return _error_response(
            None,
            RPCErrorCode.CONFLICT,
            str(e),
            data={
                "path": e.path,
                "expected_content_id": e.expected_content_id,
                "current_content_id": e.current_content_id,
            },
        )
    except DatabaseError as e:
        logger.warning("Database error in method %s: %s", method, e)
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, "Internal server error")
    except ConnectorError as e:
        logger.warning("Connector error in method %s: %s", method, e)
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, "Internal server error")
    except NexusError as e:
        logger.warning("NexusError in method %s: %s", method, e)
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, "Internal server error")
    except Exception:
        logger.exception(f"Error executing method {method}")
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, "Internal server error")


def get_cache_headers(method: str, result: Any) -> dict[str, str]:
    """Generate appropriate cache headers based on method and result."""
    headers: dict[str, str] = {}

    if method == "read":
        if isinstance(result, bytes):
            etag = hash_content(result)
            headers["ETag"] = f'"{etag}"'
            headers["Cache-Control"] = "private, max-age=60"
        elif isinstance(result, dict):
            if "content_id" in result:
                headers["ETag"] = f'"{result["content_id"]}"'
            elif "content" in result and isinstance(result["content"], bytes):
                etag = hash_content(result["content"])
                headers["ETag"] = f'"{etag}"'
            if "download_url" in result:
                headers["Cache-Control"] = "private, max-age=300"
            else:
                headers["Cache-Control"] = "private, max-age=60"
    elif method in ("list", "glob", "search"):
        headers["Cache-Control"] = "private, max-age=30"
    elif method in ("get_metadata", "exists", "is_directory"):
        headers["Cache-Control"] = "private, max-age=60"
    elif method in ("write", "delete", "rename", "copy", "mkdir", "rmdir", "delta_write", "edit"):
        headers["Cache-Control"] = "no-store"
    elif method == "delta_read":
        headers["Cache-Control"] = "private, max-age=60"
    else:
        headers["Cache-Control"] = "private, no-cache"

    return headers


def _error_response(
    request_id: Any,
    code: RPCErrorCode,
    message: str,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    """Create JSON-RPC error response."""
    error_dict = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code.value if hasattr(code, "value") else code,
            "message": message,
        },
    }
    if data:
        error_dict["error"]["data"] = data
    return JSONResponse(content=error_dict)

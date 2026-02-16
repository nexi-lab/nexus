"""Main RPC endpoint and helpers.

Extracted from fastapi_server.py (#1602).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.server.dependencies import require_auth
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)
from nexus.server.rate_limiting import RATE_LIMIT_AUTHENTICATED, limiter

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/nfs/{method}")
@limiter.limit(RATE_LIMIT_AUTHENTICATED)
async def rpc_endpoint(
    method: str,
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> Response:
    """Handle RPC method calls."""
    import time as _time

    from nexus.server.dependencies import get_operation_context
    from nexus.server.rpc.dispatch import dispatch_method

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

        # Parse parameters
        params = parse_method_params(method, rpc_request.params)

        # Get operation context
        context = get_operation_context(auth_result)

        _setup_elapsed = (_time.time() - _rpc_start) * 1000 - _parse_elapsed

        state = request.app.state

        # Early 304 check for read operations
        if_none_match = request.headers.get("If-None-Match")
        if method == "read" and if_none_match and hasattr(params, "path") and state.nexus_fs:
            try:
                cached_etag = state.nexus_fs.get_etag(params.path, context=context)
                if cached_etag:
                    client_etag = if_none_match.strip('"')
                    if client_etag == cached_etag:
                        logger.debug(f"Early 304: {params.path} (ETag match, no content read)")
                        return Response(
                            status_code=304,
                            headers={
                                "ETag": f'"{cached_etag}"',
                                "Cache-Control": "private, max-age=60",
                            },
                        )
            except Exception as e:
                logger.debug(f"Early ETag check failed for {params.path}: {e}")

        # Dispatch method
        _dispatch_start = _time.time()
        result = await dispatch_method(
            method,
            params,
            context,
            nexus_fs=state.nexus_fs,
            exposed_methods=state.exposed_methods,
            auth_provider=state.auth_provider,
            subscription_manager=state.subscription_manager,
        )
        _dispatch_elapsed = (_time.time() - _dispatch_start) * 1000

        # Build response with cache headers
        headers = get_cache_headers(method, result)

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
                "expected_etag": e.expected_etag,
                "current_etag": e.current_etag,
            },
        )
    except NexusError as e:
        logger.warning(f"NexusError in method {method}: {e}")
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, f"Nexus error: {e}")
    except Exception as e:
        logger.exception(f"Error executing method {method}")
        return _error_response(None, RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}")


def get_cache_headers(method: str, result: Any) -> dict[str, str]:
    """Generate appropriate cache headers based on method and result."""
    headers: dict[str, str] = {}

    if method == "read":
        if isinstance(result, bytes):
            etag = hashlib.md5(result).hexdigest()
            headers["ETag"] = f'"{etag}"'
            headers["Cache-Control"] = "private, max-age=60"
        elif isinstance(result, dict):
            if "etag" in result:
                headers["ETag"] = f'"{result["etag"]}"'
            elif "content" in result and isinstance(result["content"], bytes):
                etag = hashlib.md5(result["content"]).hexdigest()
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

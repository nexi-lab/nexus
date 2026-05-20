"""Main RPC endpoint and helpers.

Extracted from fastapi_server.py (#1602).
"""

import asyncio
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
from nexus.runtime.zone_resolution import target_zone_for_context
from nexus.server.dependencies import require_auth
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    parse_method_params,
)
from nexus.server.rate_limiting import RATE_LIMIT_AUTHENTICATED, limiter
from nexus.server.zone_execution import context_for_target_zone, run_zone_scoped

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
    import time as _time

    from nexus.server._kernel_syscall_dispatch import (
        KERNEL_SYSCALL_NAMES,
        dispatch_kernel_syscall,
    )
    from nexus.server.dependencies import get_operation_context
    from nexus.server.rpc.dispatch import dispatch_method

    # #4005 round-3: route the full kernel-syscall set through the thin
    # dispatcher. Round-2 narrowing silently broke read/write/list/...
    # (no METHOD_PARAMS entry on develop). Round-2 to_thread offload
    # already prevents loop-park, so the broader path is safe.
    #
    # #4005 round-4: only idempotent reads get an asyncio.wait_for
    # timeout. Cancelling a wait_for does NOT cancel the worker thread
    # asyncio.to_thread spawned, so a mutating syscall could time out
    # at the HTTP layer while the underlying write/delete/rename still
    # commits — the caller would then retry and double-mutate. For the
    # mutating set we accept the same "no per-request timeout" stance
    # the gRPC ``Call`` servicer already takes; per-backend deadlines
    # remain the right place to bound long-running mutations.
    _HTTP_TIMEOUT_SAFE_SYSCALLS: frozenset[str] = frozenset(
        {
            "access",
            "exists",
            "list",
            "read",
            "sys_read",
            "sys_readdir",
            "sys_stat",
        }
    )

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

        # #4005 round-3: route ALL kernel syscall wire names (sys_* +
        # legacy aliases) through dispatch_kernel_syscall before
        # parse_method_params. Wraps with asyncio.wait_for so a slow
        # backend can't tie up an HTTP request indefinitely (parity
        # with dispatch_method's to_thread_with_timeout).
        if method in KERNEL_SYSCALL_NAMES:
            from types import SimpleNamespace

            context = get_operation_context(auth_result)
            raw_params = dict(rpc_request.params or {})
            # ``scope_params_for_zone`` mutates via setattr — mirror the gRPC
            # servicer pattern (``servicer.py`` ~line 227): wrap in
            # SimpleNamespace, scope, then unwrap back to dict for
            # ``dispatch_kernel_syscall`` (which decodes via inspect.signature).
            _params_ns = SimpleNamespace(**raw_params)
            scope_params_for_zone(_params_ns, context.zone_id)
            raw_params = vars(_params_ns)
            target_zone = target_zone_for_context(context, raw_params)
            context = context_for_target_zone(context, target_zone)
            state = request.app.state

            # OCC header → param translation for write methods. Shared
            # parser in ``nexus.lib.http_etag`` is the single source of
            # truth for this route AND ``/api/v2/files/write``. Body
            # params win; headers fill in when missing.
            if method in ("write", "sys_write"):
                from nexus.lib.http_etag import parse_write_preconditions

                _hdr_preconds = parse_write_preconditions(
                    request.headers.get("If-Match"),
                    request.headers.get("If-None-Match"),
                )
                for _k, _v in _hdr_preconds.items():
                    raw_params.setdefault(_k, _v)

            # #4005 round-5: NO early 304 in the kernel branch.
            # ``state.nexus_fs.get_content_id`` ignores OperationContext
            # (see nexus_fs_metadata.py: ``noqa: ARG002`` on the context
            # argument), so a preflight 304 would bypass the read
            # permission hooks that ``sys_read`` / ``read`` enforce. The
            # late 304 below (after dispatch) is safe — the read had to
            # succeed first, and matching ETags then suppress the body.
            if_none_match = request.headers.get("If-None-Match")

            async def _work() -> Any:
                return await dispatch_kernel_syscall(
                    state.nexus_fs,
                    method,
                    raw_params,
                    context,
                    subscription_manager=state.subscription_manager,
                )

            _dispatch_coro = run_zone_scoped(
                getattr(state, "zone_registry", None),
                target_zone,
                _work,
            )
            if method in _HTTP_TIMEOUT_SAFE_SYSCALLS:
                _timeout = getattr(state, "operation_timeout", 30.0)
                result = await asyncio.wait_for(_dispatch_coro, timeout=_timeout)
            else:
                # Mutating syscalls: no wait_for — see comment above.
                result = await _dispatch_coro

            headers = _kernel_cache_headers(method, result)
            headers["Deprecation"] = "true"
            headers["Sunset"] = "Wed, 25 Jun 2026 00:00:00 GMT"
            headers["X-Migration-Guide"] = "Use gRPC Call RPC (Issue #1133)"

            # Late 304 (after dispatch) so a fresh ETag still suppresses body.
            if (
                if_none_match
                and "ETag" in headers
                and if_none_match.strip('"') == headers["ETag"].strip('"')
            ):
                return Response(
                    status_code=304,
                    headers={
                        "ETag": headers["ETag"],
                        "Cache-Control": headers.get("Cache-Control", ""),
                    },
                )

            success_response = {
                "jsonrpc": "2.0",
                "id": rpc_request.id,
                "result": result,
            }
            encoded = encode_rpc_message(success_response)
            return Response(content=encoded, media_type="application/json", headers=headers)

        # Parse parameters — fall through to SimpleNamespace for dynamically
        # discovered @rpc_expose methods that lack pre-generated Params classes
        # (e.g., llm_read, llm_read_detailed, llm_read_stream).
        try:
            params = parse_method_params(method, rpc_request.params)
        except ValueError:
            _exposed = getattr(request.app.state, "exposed_methods", {})
            if method in _exposed:
                from types import SimpleNamespace

                params = SimpleNamespace(**(rpc_request.params or {}))
            else:
                raise

        # Get operation context
        context = get_operation_context(auth_result)

        # Scope paths for zone isolation (prefix with /zone/{zone_id}/)
        scope_params_for_zone(params, context.zone_id)

        _setup_elapsed = (_time.time() - _rpc_start) * 1000 - _parse_elapsed

        state = request.app.state

        # Early 304 check for read operations
        if_none_match = request.headers.get("If-None-Match")
        if method == "read" and if_none_match and hasattr(params, "path") and state.nexus_fs:
            try:
                cached_content_id = state.nexus_fs.get_content_id(params.path, context=context)
                if cached_content_id:
                    client_etag = if_none_match.strip('"')
                    if client_etag == cached_content_id:
                        logger.debug(f"Early 304: {params.path} (ETag match, no content read)")
                        return Response(
                            status_code=304,
                            headers={
                                "ETag": f'"{cached_content_id}"',
                                "Cache-Control": "private, max-age=60",
                            },
                        )
            except Exception as e:
                logger.debug(f"Early ETag check failed for {params.path}: {e}")

        # Dispatch method
        _dispatch_start = _time.time()
        target_zone = target_zone_for_context(context, params)
        context = context_for_target_zone(context, target_zone)

        async def _work() -> Any:
            return await dispatch_method(
                method,
                params,
                context,
                nexus_fs=state.nexus_fs,
                exposed_methods=state.exposed_methods,
                auth_provider=state.auth_provider,
                subscription_manager=state.subscription_manager,
            )

        result = await run_zone_scoped(
            getattr(state, "zone_registry", None),
            target_zone,
            _work,
        )
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

    except TimeoutError:
        # #4005 round-3: HTTP kernel-syscall short-circuit timeout —
        # surface as INTERNAL_ERROR matching the legacy
        # ``to_thread_with_timeout`` behavior in dispatch_method.
        return _error_response(
            None,
            RPCErrorCode.INTERNAL_ERROR,
            f"Operation timed out (method={method})",
        )
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


# #4005 round-4: kernel syscall wire-name → legacy cache category so the
# new HTTP short-circuit reuses ``get_cache_headers`` semantics correctly.
# Mutating sys_* must land in the ``no-store`` arm; reads/stat/list keep
# their cache-friendly arms; lock primitives are no-cache.
_KERNEL_CACHE_CATEGORY: dict[str, str] = {
    "sys_read": "read",
    # #4005 round-5: ``sys_stat`` returns metadata + content_id, but
    # ``sys_setattr`` can mutate metadata (mtime, mode) WITHOUT changing
    # content_id. Synthesizing an ETag from content_id alone would let
    # a revalidating client get 304 and keep stale stat metadata across
    # touch / chmod-like ops. Use ``get_metadata``'s arm (Cache-Control
    # only, no ETag) until we have a metadata-versioned validator.
    "sys_stat": "get_metadata",
    "sys_readdir": "list",
    "sys_write": "write",
    "sys_unlink": "delete",
    "sys_rename": "rename",
    "sys_copy": "copy",
    "sys_mkdir": "mkdir",
    "sys_setattr": "write",
    "sys_lock": "lock_acquire",
    "sys_unlock": "lock_acquire",
}


def _kernel_cache_headers(method: str, result: Any) -> dict[str, str]:
    """Cache headers for the kernel-syscall HTTP short-circuit.

    Re-keys sys_* wire names onto the legacy categories that
    ``get_cache_headers`` understands so mutating syscalls get
    ``no-store``, reads get an ETag from ``content_id`` / bytes,
    and ``sys_readdir`` aligns with ``list``.
    """
    canonical = _KERNEL_CACHE_CATEGORY.get(method, method)
    return get_cache_headers(canonical, result)


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

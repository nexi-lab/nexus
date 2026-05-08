from __future__ import annotations

import inspect
import io
import json
from collections.abc import Callable
from copy import copy as shallow_copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, cast


class FileType(StrEnum):
    JSON = "json"
    PARQUET = "parquet"
    UNKNOWN = "unknown"


class BackendKind(StrEnum):
    S3 = "s3"
    SLACK = "slack"
    GITHUB = "github"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OpKey:
    op: str
    filetype: FileType | None
    backend: BackendKind | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "op", self.op.lower())


@dataclass
class OperationRequest:
    op: str
    path: str
    filetype: FileType
    backend: BackendKind
    content: bytes | None = None
    kernel: Any = None
    context: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    strict: bool = True
    pattern: str | None = None
    ignore_case: bool = False
    max_results: int = 1000


Handler = Callable[[OperationRequest], Any]


def normalize_filetype(path: str, mime_type: str | None = None) -> FileType:
    mime = (mime_type or "").strip().lower()
    if mime in {"application/json", "text/json"}:
        return FileType.JSON
    if mime in {
        "application/parquet",
        "application/x-parquet",
        "application/vnd.apache.parquet",
    }:
        return FileType.PARQUET

    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in {"json", "jsonl", "ndjson"}:
        return FileType.JSON
    if suffix in {"parquet", "pq"}:
        return FileType.PARQUET
    return FileType.UNKNOWN


def normalize_backend(name: str | None) -> BackendKind:
    normalized = (name or "").strip().lower().replace("-", "_")
    if normalized in {"path_s3", "s3", "s3_connector"}:
        return BackendKind.S3
    if normalized in {"slack", "path_slack", "slack_connector"}:
        return BackendKind.SLACK
    if normalized in {"github", "github_connector", "gws_github"}:
        return BackendKind.GITHUB
    if normalized in {"local", "path_local", "cas_local"}:
        return BackendKind.LOCAL
    return BackendKind.UNKNOWN


class OpsRegistry:
    def __init__(self) -> None:
        self._handlers: dict[OpKey, Handler] = {}

    def register(self, key: OpKey, handler: Handler) -> None:
        if key in self._handlers:
            raise ValueError(f"operation handler already registered for {key}")
        self._handlers[key] = handler

    def replace(self, key: OpKey, handler: Handler) -> None:
        self._handlers[key] = handler

    def resolve(self, op: str, filetype: FileType, backend: BackendKind) -> Handler | None:
        normalized_op = op.lower()
        probes = (
            OpKey(normalized_op, filetype, backend),
            OpKey(normalized_op, None, backend),
            OpKey(normalized_op, filetype, None),
            OpKey(normalized_op, None, None),
        )
        for key in probes:
            handler = self._handlers.get(key)
            if handler is not None:
                return handler
        return None


def default_cat(req: OperationRequest) -> bytes:
    return req.content or b""


def json_cat(req: OperationRequest) -> bytes:
    raw = req.content or b""
    suffix = req.path.rsplit(".", 1)[-1].lower() if "." in req.path else ""
    if suffix in {"jsonl", "ndjson"}:
        return raw
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if req.strict:
            raise
        return raw
    return (json.dumps(parsed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def parquet_cat(req: OperationRequest) -> bytes:
    raw = req.content or b""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return raw

    try:
        table = pq.read_table(io.BytesIO(raw))
    except Exception:
        if req.strict:
            raise
        return raw

    rows = table.to_pylist()
    return (json.dumps(rows, indent=2, default=str, ensure_ascii=False) + "\n").encode("utf-8")


def slack_grep(req: OperationRequest) -> list[dict[str, Any]]:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "grep_messages", None)
    if not callable(method):
        raise RuntimeError("Slack grep dispatch requires a backend with grep_messages")
    backend_path = _backend_path_for_request(req)
    context = _context_with_backend_path(req.context, backend_path)
    return cast(
        list[dict[str, Any]],
        method(
            req.pattern or "",
            context=context,
            max_results=req.max_results,
            ignore_case=req.ignore_case,
            backend_path=backend_path,
            mount_path=str(req.metadata.get("mount_path") or "/slack"),
        ),
    )


def github_raw_read(req: OperationRequest) -> bytes:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "raw_read", None)
    if not callable(method):
        raise RuntimeError("GitHub raw_read dispatch requires a backend with raw_read")
    backend_path = _backend_path_for_request(req)
    context = _context_with_backend_path(req.context, backend_path)
    return cast(bytes, method(backend_path, context=context))


def s3_fingerprint(req: OperationRequest) -> str | None:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "fingerprint", None)
    if callable(method):
        backend_path = _backend_path_for_request(req)
        return cast(
            str | None,
            method(backend_path, context=_context_with_backend_path(req.context, backend_path)),
        )
    kernel = req.kernel
    if kernel is not None and hasattr(kernel, "_kernel"):
        zone_id = getattr(req.context, "zone_id", None) or getattr(kernel, "_zone_id", "root")
        return cast(str | None, kernel._kernel.backend_fingerprint(req.path, zone_id))
    return None


def _backend_path_for_request(req: OperationRequest) -> str:
    if "backend_path" in req.metadata:
        return str(req.metadata.get("backend_path") or "").lstrip("/")
    return req.path


def _context_with_backend_path(context: Any, backend_path: str) -> Any:
    if not backend_path or context is None:
        return context
    if getattr(context, "backend_path", None) == backend_path:
        return context
    try:
        return replace(context, backend_path=backend_path)
    except (TypeError, ValueError):
        pass
    try:
        cloned = shallow_copy(context)
        cloned.backend_path = backend_path
        return cloned
    except Exception:
        return context


def _call_kernel_sys_read(kernel: Any, path: str, context: Any = None) -> Any:
    return _call_kernel_path_method(kernel.sys_read, path, context)


def _call_kernel_sys_stat(kernel: Any, path: str, context: Any = None) -> Any:
    return _call_kernel_path_method(kernel.sys_stat, path, context)


def _call_kernel_path_method(method: Callable[..., Any], path: str, context: Any = None) -> Any:
    if context is None:
        return method(path)

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(path, context=context)

    parameters = signature.parameters.values()
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
        return method(path, context=context)
    context_param = signature.parameters.get("context")
    if context_param is not None and context_param.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return method(path, context=context)
    if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters):
        return method(path, context)
    positional_params = [
        param
        for param in parameters
        if param.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if len(positional_params) >= 2:
        return method(path, context)
    return method(path)


def _read_result_to_bytes(result: Any) -> bytes:
    if isinstance(result, bytes):
        return result
    if isinstance(result, (bytearray, memoryview)):
        return bytes(result)
    if isinstance(result, dict):
        if "data" in result:
            return _read_result_to_bytes(result["data"])
        if "content" in result:
            return _read_result_to_bytes(result["content"])
        msg = "sys_read result dict must contain bytes under 'data' or 'content'"
        raise TypeError(msg)
    msg = f"sys_read returned unsupported result type: {type(result).__name__}"
    raise TypeError(msg)


def _metadata_from_kernel(kernel: Any, path: str, context: Any = None) -> dict[str, Any]:
    py_kernel = getattr(kernel, "_kernel", None)
    zone_id = getattr(context, "zone_id", None) or getattr(kernel, "_zone_id", "root")
    metadata_for_path = getattr(py_kernel, "op_metadata_for_path", None)
    if callable(metadata_for_path):
        try:
            return _attach_mounted_backend_instance(
                dict(metadata_for_path(path, zone_id)), kernel, path
            )
        except FileNotFoundError:
            pass
    stat = (
        _call_kernel_sys_stat(kernel, path, context=context) if hasattr(kernel, "sys_stat") else {}
    )
    mime_type = stat.get("mime_type") if isinstance(stat, dict) else None
    return _attach_mounted_backend_instance(
        {
            "filetype": normalize_filetype(path, mime_type).value,
            "backend": BackendKind.UNKNOWN.value,
            "mime_type": mime_type,
            "backend_name": "",
        },
        kernel,
        path,
    )


def _attach_mounted_backend_instance(
    metadata: dict[str, Any], kernel: Any, path: str
) -> dict[str, Any]:
    if metadata.get("backend_instance") is not None:
        return metadata
    mounted = getattr(kernel, "_mounted_backend_instances", None)
    if not isinstance(mounted, dict) or not mounted:
        return metadata

    normalized_path = "/" + path.strip("/")
    best_mount = ""
    best_backend: Any = None
    for raw_mount, backend in mounted.items():
        mount = "/" + str(raw_mount).strip("/")
        if mount == "/":
            matches = True
        else:
            mount_prefix = mount.rstrip("/")
            matches = normalized_path == mount_prefix or normalized_path.startswith(
                mount_prefix + "/"
            )
        if matches and len(mount) > len(best_mount):
            best_mount = mount
            best_backend = backend

    if best_backend is None:
        return metadata

    backend_path = (
        normalized_path.removeprefix(best_mount.rstrip("/")).lstrip("/")
        if best_mount != "/"
        else normalized_path.lstrip("/")
    )
    metadata.setdefault("backend_instance", best_backend)
    metadata.setdefault("backend_path", backend_path)
    metadata.setdefault("mount_path", best_mount)
    raw_backend = str(metadata.get("backend") or "")
    raw_backend_name = str(metadata.get("backend_name") or "")
    if normalize_backend(raw_backend or raw_backend_name) is BackendKind.UNKNOWN:
        inferred_backend_name = getattr(best_backend, "name", None)
        if not isinstance(inferred_backend_name, str) or not inferred_backend_name:
            inferred_backend_name = type(best_backend).__name__
        inferred_backend = normalize_backend(inferred_backend_name)
        if inferred_backend is BackendKind.UNKNOWN:
            if callable(getattr(best_backend, "grep_messages", None)):
                inferred_backend = BackendKind.SLACK
                inferred_backend_name = "slack"
            elif callable(getattr(best_backend, "raw_read", None)):
                inferred_backend = BackendKind.GITHUB
                inferred_backend_name = "github"
            elif callable(getattr(best_backend, "fingerprint", None)):
                inferred_backend = BackendKind.S3
                inferred_backend_name = "s3"
        if inferred_backend is not BackendKind.UNKNOWN:
            metadata["backend"] = inferred_backend.value
            metadata["backend_name"] = inferred_backend_name
    return metadata


def _filetype_from_metadata(metadata: dict[str, Any], path: str) -> FileType:
    mime_type = metadata.get("mime_type")
    raw_filetype = metadata.get("filetype")
    try:
        return FileType(raw_filetype or FileType.UNKNOWN)
    except ValueError:
        return normalize_filetype(path, str(mime_type) if mime_type is not None else None)


def cat_path(kernel: Any, path: str, *, context: Any = None, strict: bool = True) -> bytes:
    metadata = _metadata_from_kernel(kernel, path, context=context)
    filetype = _filetype_from_metadata(metadata, path)
    backend = normalize_backend(str(metadata.get("backend") or metadata.get("backend_name") or ""))
    content = _read_result_to_bytes(_call_kernel_sys_read(kernel, path, context=context))
    req = OperationRequest(
        op="cat",
        path=path,
        filetype=filetype,
        backend=backend,
        content=content,
        kernel=kernel,
        context=context,
        metadata=metadata,
        strict=strict,
    )
    handler = get_global_registry().resolve("cat", filetype, backend)
    if handler is None:
        return content
    return cast(bytes, handler(req))


def grep_path(
    kernel: Any,
    pattern: str,
    path: str = "/",
    *,
    context: Any = None,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    metadata = _metadata_from_kernel(kernel, path, context=context)
    filetype = _filetype_from_metadata(metadata, path)
    backend = normalize_backend(str(metadata.get("backend") or metadata.get("backend_name") or ""))
    handler = get_global_registry().resolve("grep", filetype, backend)
    if handler is None:
        return None
    if handler is slack_grep and metadata.get("backend_instance") is None:
        return None
    req = OperationRequest(
        op="grep",
        path=path,
        filetype=filetype,
        backend=backend,
        kernel=kernel,
        context=context,
        metadata=metadata,
        pattern=pattern,
        ignore_case=ignore_case,
        max_results=max_results,
    )
    return cast(list[dict[str, Any]], handler(req))


def register_default_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("cat", None, None), default_cat)


def register_parser_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("cat", FileType.JSON, None), json_cat)
    registry.register(OpKey("cat", FileType.PARQUET, None), parquet_cat)


def register_backend_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("grep", None, BackendKind.SLACK), slack_grep)
    registry.register(OpKey("raw_read", None, BackendKind.GITHUB), github_raw_read)
    registry.register(OpKey("fingerprint", None, BackendKind.S3), s3_fingerprint)


_GLOBAL_REGISTRY: OpsRegistry | None = None


def get_global_registry() -> OpsRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        registry = OpsRegistry()
        register_default_ops(registry)
        register_parser_ops(registry)
        register_backend_ops(registry)
        _GLOBAL_REGISTRY = registry
    return _GLOBAL_REGISTRY


def reset_global_registry_for_tests() -> None:
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = None

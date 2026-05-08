from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
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
        if req.strict:
            raise RuntimeError("pyarrow is required for parquet cat") from None
        return raw

    table = pq.read_table(io.BytesIO(raw))
    rows = table.to_pylist()
    return (json.dumps(rows, indent=2, default=str, ensure_ascii=False) + "\n").encode("utf-8")


def slack_grep(req: OperationRequest) -> list[dict[str, Any]]:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "grep_messages", None)
    if not callable(method):
        raise RuntimeError("Slack grep dispatch requires a backend with grep_messages")
    return cast(
        list[dict[str, Any]],
        method(
            req.pattern or "",
            context=req.context,
            max_results=req.max_results,
            ignore_case=req.ignore_case,
        ),
    )


def github_raw_read(req: OperationRequest) -> bytes:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "raw_read", None)
    if not callable(method):
        raise RuntimeError("GitHub raw_read dispatch requires a backend with raw_read")
    return cast(bytes, method(req.path, context=req.context))


def s3_fingerprint(req: OperationRequest) -> str | None:
    backend = req.metadata.get("backend_instance")
    method = getattr(backend, "fingerprint", None)
    if callable(method):
        return cast(str | None, method(req.path, context=req.context))
    kernel = req.kernel
    if kernel is not None and hasattr(kernel, "_kernel"):
        zone_id = getattr(req.context, "zone_id", None) or getattr(kernel, "_zone_id", "root")
        return cast(str | None, kernel._kernel.backend_fingerprint(req.path, zone_id))
    return None


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

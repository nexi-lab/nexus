"""Normalize per-transport surface names to canonical op-id <module>.<verb>.

Op-id is stable across transports. The first token after the module is the verb;
additional path/name segments are joined with underscore.

Examples:
    CLI  "nexus fs read"                 -> "fs.read"
    CLI  "nexus workspace snapshot create" -> "workspace.snapshot_create"
    gRPC "VFS.Read"                      -> "fs.read"
    HTTP POST /api/v1/fs/read            -> "fs.read"
    MCP  "nexus_fs_read"                 -> "fs.read"
    MCP  "nexus_grep"                    -> "search.grep"
    SDK  NexusClient.read                -> "fs.read"

Unmapped names should be added as overrides in api-rpc-surface-overrides.yaml.
"""

from __future__ import annotations

import re

# gRPC typed service name -> canonical module
_GRPC_SERVICE_TO_MODULE = {
    "VFS": "fs",
    "ReBAC": "rebac",
    "Workspace": "workspace",
    "Search": "search",
    "SearchService": "search",
    "MCP": "mcp",
}

# SDK method name prefix -> module (when method doesn't carry module explicitly)
_SDK_DEFAULT_MODULE = "fs"

# Some early MCP tools predate the `nexus_<module>_<verb>` naming convention.
_BARE_MCP_TOOL_OVERRIDES = {
    "glob": "search.glob",
    "grep": "search.grep",
    "mkdir": "filesystem.mkdir",
    "rmdir": "filesystem.rmdir",
}

_HTTP_OPERATION_OVERRIDES = {"search.query_batch": "search.batch_query"}
_HTTP_RESOURCE_OPERATIONS = {
    ("POST", "/v2/documents/index"): "search.index",
    ("POST", "/v2/documents/refresh"): "search.refresh",
    ("POST", "/v2/documents/batch"): "search.index_documents",
    ("POST", "/v2/documents/file"): "search.index_file",
    ("DELETE", "/v2/documents/file"): "search.notify_file_change",
    ("GET", "/v2/documents/stats"): "search.stats",
    ("GET", "/v2/documents/directories"): "search.list_indexed_directories",
    ("POST", "/v2/documents/directories"): "search.add_indexed_directory",
    ("DELETE", "/v2/documents/directories"): "search.remove_indexed_directory",
    ("GET", "/v2/documents/indexing-modes"): "search.list_zone_indexing_modes",
    ("PUT", "/v2/documents/indexing-modes"): "search.set_zone_indexing_mode",
}


def _to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def normalize_cli(cli_invocation: str) -> str:
    """`nexus <module> <verb> [<more>...]` -> `<module>.<verb>[_<more>...]`"""
    parts = cli_invocation.strip().split()
    if len(parts) < 3 or parts[0] != "nexus":
        raise ValueError(f"unrecognized CLI form: {cli_invocation!r}")
    module = parts[1].replace("-", "_")
    verb_parts = [p.replace("-", "_") for p in parts[2:]]
    return f"{module}.{'_'.join(verb_parts)}"


def normalize_grpc_typed(method: str) -> str:
    """`<Service>.<Method>` -> `<module>.<verb>` via service->module mapping."""
    if "." not in method:
        raise ValueError(f"expected '<Service>.<Method>', got: {method!r}")
    service, m = method.split(".", 1)
    module = _GRPC_SERVICE_TO_MODULE.get(service, _to_snake(service))
    return f"{module}.{_to_snake(m)}"


def normalize_grpc_call(call_name: str) -> str:
    """Generic gRPC `Call` names are already canonical."""
    return call_name


def normalize_http(method: str, path: str) -> str:
    """Map `/api/vN/` and native `/vN/` routes onto the same operation."""
    resource = _HTTP_RESOURCE_OPERATIONS.get((method.upper(), path.rstrip("/")))
    if resource is not None:
        return resource
    m = re.match(r"^/(?:api/)?v\d+/([A-Za-z][A-Za-z0-9_-]*)/(.+?)/?$", path)
    if not m:
        raise ValueError(f"unrecognized HTTP path: {path!r}")
    module = m.group(1).replace("-", "_")
    verb_parts = [p.replace("-", "_") for p in m.group(2).split("/")]
    op_id = f"{module}.{'_'.join(verb_parts)}"
    return _HTTP_OPERATION_OVERRIDES.get(op_id, op_id)


def normalize_mcp(tool_name: str) -> str:
    """`nexus_<module>_<verb>[_<more>]` -> `<module>.<verb>[_<more>]`."""
    if not tool_name.startswith("nexus_"):
        raise ValueError(f"unrecognized MCP tool name: {tool_name!r}")
    rest = tool_name[len("nexus_") :]
    if rest in _BARE_MCP_TOOL_OVERRIDES:
        return _BARE_MCP_TOOL_OVERRIDES[rest]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"MCP name needs module+verb: {tool_name!r}")
    return f"{parts[0]}.{parts[1]}"


def normalize_sdk(class_name: str, method_name: str) -> str:
    """`NexusClient.<method>` -> `<module>.<verb>`.

    If method contains '_', first segment is module; otherwise default module 'fs'.
    """
    if "_" in method_name:
        module, _, verb = method_name.partition("_")
        return f"{module}.{verb}"
    return f"{_SDK_DEFAULT_MODULE}.{method_name}"

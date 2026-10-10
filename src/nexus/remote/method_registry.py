"""Method registry for RPC proxy dispatch configuration.

Defines MethodSpec dataclass and METHOD_REGISTRY dict that configure how
the RPC proxy dispatches and transforms method calls. Methods NOT in the
registry use default pass-through behavior (call _call_rpc, return result).

Issue #1289: Protocol + RPC Proxy pattern.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """Configuration for how a proxy method dispatches and transforms RPC calls.

    Attributes:
        rpc_name: Override RPC method name (default: use method name).
        response_key: Extract this key from result dict (e.g., "files", "matches").
        custom_timeout: Fixed read timeout override for this method.
        context_zone: Use context.zone_id as the default RPC zone selector.
    """

    rpc_name: str | None = None
    response_key: str | None = None
    custom_timeout: float | None = None
    context_zone: bool = False


def strip_context(
    params: dict[str, Any], spec: MethodSpec | None, default_context: Any = None
) -> None:
    """Keep a declared zone selector; caller authority stays in the credential."""
    context = params.pop("context", None)
    alternate = params.pop("_context", None)
    if context is None:
        context = alternate
    if context is None:
        context = default_context
    if spec is not None and spec.context_zone and params.get("zone_id") is None:
        zone = getattr(context, "zone_id", None)
        if zone is not None:
            params["zone_id"] = zone


# Registry: method_name -> MethodSpec
# Methods NOT in this registry use default pass-through behavior:
#   result = self._call_rpc(method_name, params)
#   return result
#
# Only methods with non-default behavior need entries here:
#   - response_key: extract a specific key from the result dict
#   - custom_timeout: override the default read timeout
#   - rpc_name: use a different RPC method name than the Python method name
#
# Methods with complex logic (negative cache, content encoding, streaming,
# dynamic timeouts) are hand-written overrides in client.py/async_client.py
# and are NOT in this registry.
METHOD_REGISTRY: dict[str, MethodSpec] = {
    # --- Discovery (response_key extraction) ---
    "sys_readdir": MethodSpec(response_key="files"),
    "glob": MethodSpec(response_key="matches"),
    "grep": MethodSpec(response_key="results"),
    "semantic_search": MethodSpec(response_key="results", context_zone=True),
    "semantic_search_index": MethodSpec(context_zone=True),
    "semantic_search_stats": MethodSpec(context_zone=True),
    # --- Boolean result extraction ---
    "access": MethodSpec(response_key="exists"),
    "is_directory": MethodSpec(response_key="is_directory"),
}

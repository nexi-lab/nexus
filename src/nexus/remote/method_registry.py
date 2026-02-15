"""Method registry for RPC proxy dispatch configuration.

Defines MethodSpec dataclass and METHOD_REGISTRY dict that configure how
the RPC proxy dispatches and transforms method calls. Methods NOT in the
registry use default pass-through behavior (call _call_rpc, return result).

Issue #1289: Protocol + RPC Proxy pattern.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """Configuration for how a proxy method dispatches and transforms RPC calls.

    Attributes:
        rpc_name: Override RPC method name (default: use method name).
        response_key: Extract this key from result dict (e.g., "files", "matches").
        custom_timeout: Fixed read timeout override for this method.
        deprecated_message: If set, method raises NotImplementedError with this message.
    """

    rpc_name: str | None = None
    response_key: str | None = None
    custom_timeout: float | None = None
    deprecated_message: str | None = None


# Registry: method_name -> MethodSpec
# Methods NOT in this registry use default pass-through behavior:
#   result = self._call_rpc(method_name, params)
#   return result
#
# Only methods with non-default behavior need entries here:
#   - response_key: extract a specific key from the result dict
#   - custom_timeout: override the default read timeout
#   - deprecated_message: raise NotImplementedError
#   - rpc_name: use a different RPC method name than the Python method name
#
# Methods with complex logic (negative cache, content encoding, streaming,
# dynamic timeouts) are hand-written overrides in client.py/async_client.py
# and are NOT in this registry.
METHOD_REGISTRY: dict[str, MethodSpec] = {
    # --- Discovery (response_key extraction) ---
    "list": MethodSpec(response_key="files"),
    "glob": MethodSpec(response_key="matches"),
    "grep": MethodSpec(response_key="results"),
    # --- Boolean result extraction ---
    "is_directory": MethodSpec(response_key="is_directory"),
    "is_file": MethodSpec(response_key="is_file"),
    # --- Deprecated POSIX permissions ---
    "chmod": MethodSpec(deprecated_message="Use rebac_create() for ReBAC permissions"),
    "chown": MethodSpec(deprecated_message="Use rebac_create() for ReBAC permissions"),
    "chgrp": MethodSpec(deprecated_message="Use rebac_create() for ReBAC permissions"),
    "grant_user": MethodSpec(deprecated_message="Use rebac_create() or share_with_user()"),
    "grant_group": MethodSpec(deprecated_message="Use rebac_create() or share_with_group()"),
    "deny_user": MethodSpec(deprecated_message="Use rebac_create() for ReBAC deny tuples"),
    "revoke_acl": MethodSpec(deprecated_message="Use rebac_delete() to remove tuples"),
    "get_acl": MethodSpec(deprecated_message="Use rebac_list_tuples() or rebac_expand()"),
}

"""RPC protocol definitions — thin re-export layer.

This module re-exports all Param classes, METHOD_PARAMS, and RPC types so
existing ``from nexus.server.protocol import ReadParams`` still works.

Implementation details:
  - _rpc_params_generated.py — auto-generated from @rpc_expose signatures
  - _rpc_param_overrides.py  — hand-maintained overrides (RPC-only fields,
    constant defaults, and methods not on NexusFS class)
  - rpc_results.py           — response-side result types
"""

from __future__ import annotations

from typing import Any

# Issue #1519, 1A: RPC types extracted to core/rpc_types.py so core/ modules
# (rpc_transport, rpc_codec) can use them without importing from server/.
# Re-exported here for backward compatibility.
from nexus.core.rpc_types import RPCErrorCode, RPCRequest, RPCResponse  # noqa: F401

# ============================================================
# Generated Param classes + METHOD_PARAMS
# ============================================================
from nexus.server._rpc_param_overrides import *  # noqa: F401, F403, E402
from nexus.server._rpc_param_overrides import (  # noqa: E402
    OVERRIDE_METHOD_PARAMS as _OVERRIDE_METHOD_PARAMS,
)
from nexus.server._rpc_params_generated import *  # noqa: F401, F403, E402
from nexus.server._rpc_params_generated import METHOD_PARAMS as _GEN_METHOD_PARAMS  # noqa: E402
from nexus.server.rpc_results import RebacCheckResult, RebacCreateResult  # noqa: F401, E402

# Issue #1519, 1A: RPC types extracted to core/rpc_types.py so core/ modules
# (rpc_transport, rpc_codec) can use them without importing from server/.
# Re-exported here for backward compatibility.

# ============================================================
# Merged METHOD_PARAMS (overrides take precedence over generated)
# ============================================================
METHOD_PARAMS: dict[str, type] = {**_GEN_METHOD_PARAMS, **_OVERRIDE_METHOD_PARAMS}


def parse_method_params(method: str, params: dict[str, Any] | None) -> Any:
    """Parse and validate method parameters.

    Args:
        method: Method name
        params: Parameter dict

    Returns:
        Parameter dataclass instance

    Raises:
        ValueError: If method is unknown or params are invalid
    """
    if method not in METHOD_PARAMS:
        raise ValueError(f"Unknown method: {method}")

    param_class = METHOD_PARAMS[method]
    if params is None:
        params = {}

    try:
        return param_class(**params)
    except TypeError as e:
        raise ValueError(f"Invalid parameters for {method}: {e}") from e

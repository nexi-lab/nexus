"""RPC protocol definitions — thin re-export layer.

Wire-format RPC types (RPCErrorCode / RPCRequest / RPCResponse) live
in ``nexus.contracts.rpc_types`` and are re-exported here for
backward compat with ``from nexus.server.protocol import RPCErrorCode``.

The auto-generated Param dataclasses (``_rpc_params_generated.py`` +
``_rpc_param_overrides.py``) are also re-exported — they're tests-only
dependencies after the wire dispatch migration to
``services::python_ffi``; ``parse_method_params`` was the bridge from
those dataclasses into the legacy ``dispatch_method`` path and is
now production-dead, so it's gone.
"""

# Issue #1519, 1A: RPC types live in contracts/rpc_types.py (pure data types).
# Re-exported here for backward compatibility.
from nexus.contracts.rpc_types import RPCErrorCode, RPCRequest, RPCResponse  # noqa: F401

# Param classes + METHOD_PARAMS — kept as re-exports for tests that
# still import them; production wire dispatch no longer reads them.
from nexus.server._rpc_param_overrides import *  # noqa: F401, F403, E402
from nexus.server._rpc_param_overrides import (  # noqa: E402
    OVERRIDE_METHOD_PARAMS as _OVERRIDE_METHOD_PARAMS,
)
from nexus.server._rpc_params_generated import *  # noqa: F401, F403, E402
from nexus.server._rpc_params_generated import METHOD_PARAMS as _GEN_METHOD_PARAMS  # noqa: E402
from nexus.server.rpc_results import RebacCheckResult, RebacCreateResult  # noqa: F401, E402

METHOD_PARAMS: dict[str, type] = {**_GEN_METHOD_PARAMS, **_OVERRIDE_METHOD_PARAMS}

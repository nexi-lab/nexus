"""Test parity between FastAPI and WebSocket memory method dispatchers (#1203).

Ensures both dispatchers handle the same set of memory methods,
preventing the drift that caused the original 'Unknown method: list_memories' bug.
"""

from __future__ import annotations

import re

# The canonical set of memory RPC methods that both dispatchers must handle.
EXPECTED_MEMORY_METHODS = {
    "store_memory",
    "list_memories",
    "query_memories",
    "retrieve_memory",
    "delete_memory",
    "approve_memory",
    "deactivate_memory",
    "approve_memory_batch",
    "deactivate_memory_batch",
    "delete_memory_batch",
}

# Methods that require specific params (others have all-optional defaults)
_REQUIRED_PARAMS = {
    "store_memory": {"content": "test"},
    "delete_memory": {"memory_id": "mem_123"},
    "approve_memory": {"memory_id": "mem_123"},
    "deactivate_memory": {"memory_id": "mem_123"},
    "approve_memory_batch": {"memory_ids": ["mem_1"]},
    "deactivate_memory_batch": {"memory_ids": ["mem_1"]},
    "delete_memory_batch": {"memory_ids": ["mem_1"]},
}


def _extract_memory_methods(source: str) -> set[str]:
    """Extract memory-related method names from dispatch chains.

    Matches patterns like: elif method == "store_memory"
    Captures any method name containing 'memor' (covers memory/memories).
    """
    pattern = r'(?:el)?if\s+method\s*==\s*"(\w*memor\w*)"'
    return set(re.findall(pattern, source))


class TestMemoryDispatchParity:
    """Verify both dispatchers handle the same memory methods."""

    def test_fastapi_has_all_memory_methods(self):
        """FastAPI dispatcher handles all expected memory methods."""
        import inspect

        from nexus.server import fastapi_server

        source = inspect.getsource(fastapi_server._dispatch_method)
        memory_methods = _extract_memory_methods(source)

        missing = EXPECTED_MEMORY_METHODS - memory_methods
        assert not missing, f"FastAPI dispatcher missing memory methods: {missing}"

    def test_rpc_server_has_all_memory_methods(self):
        """WebSocket RPC dispatcher handles all expected memory methods."""
        import inspect

        from nexus.server.rpc_server import RPCRequestHandler

        source = inspect.getsource(RPCRequestHandler._dispatch_method)
        memory_methods = _extract_memory_methods(source)

        missing = EXPECTED_MEMORY_METHODS - memory_methods
        assert not missing, f"WS RPC dispatcher missing memory methods: {missing}"

    def test_both_dispatchers_match(self):
        """Both dispatchers handle exactly the same memory methods."""
        import inspect

        from nexus.server import fastapi_server
        from nexus.server.rpc_server import RPCRequestHandler

        fastapi_source = inspect.getsource(fastapi_server._dispatch_method)
        rpc_source = inspect.getsource(RPCRequestHandler._dispatch_method)

        fastapi_methods = _extract_memory_methods(fastapi_source)
        rpc_methods = _extract_memory_methods(rpc_source)

        only_fastapi = fastapi_methods - rpc_methods
        only_rpc = rpc_methods - fastapi_methods

        assert not only_fastapi, f"Methods only in FastAPI (not WS): {only_fastapi}"
        assert not only_rpc, f"Methods only in WS (not FastAPI): {only_rpc}"

    def test_all_memory_methods_in_protocol(self):
        """All memory methods have parameter classes in METHOD_PARAMS."""
        from nexus.server.protocol import METHOD_PARAMS

        missing = EXPECTED_MEMORY_METHODS - set(METHOD_PARAMS.keys())
        assert not missing, f"Memory methods missing from METHOD_PARAMS: {missing}"

    def test_all_memory_methods_parseable(self):
        """All memory methods can be parsed with appropriate params."""
        from nexus.server.protocol import parse_method_params

        for method in EXPECTED_MEMORY_METHODS:
            params = _REQUIRED_PARAMS.get(method, {})
            result = parse_method_params(method, params)
            assert result is not None, f"parse_method_params returned None for {method}"

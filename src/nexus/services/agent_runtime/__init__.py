"""Agent Runtime — agent loop and process communication.

Shared base for managed (nexusd-spawned) and unmanaged (3rd-party gRPC)
agent subprocess communication over PipeBackend (DT_PIPE).

    agent_runtime/loop.py = JSON-RPC 2.0 over PipeBackend base class
"""

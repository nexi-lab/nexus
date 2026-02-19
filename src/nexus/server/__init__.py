"""Nexus RPC server.

This module provides an HTTP server that exposes all NexusFileSystem operations
through a JSON-RPC API. This allows remote clients (including FUSE mounts) to
access Nexus over the network.
"""

from nexus.contracts.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.server.protocol import RPCErrorCode, RPCRequest, RPCResponse

__all__ = [
    "RPCRequest",
    "RPCResponse",
    "RPCErrorCode",
    "encode_rpc_message",
    "decode_rpc_message",
]
